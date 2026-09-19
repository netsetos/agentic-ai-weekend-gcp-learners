#!/usr/bin/env python3
"""Preview missing managed copies; pass --apply after inspecting the preview.

Run from deploy/ with PYTHONPATH="$PWD:$PWD/services/ingest" and the runbook's
PROJECT, GOOGLE_CLOUD_PROJECT, AUDIT_BUCKET, RAG_LOCATION and SEARCH_LOCATION.
This retries current text copies only; it does not ingest or rewrite source PDFs.
"""
import argparse, json, os
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from managed import Mirror, VertexSearchStore, STRUCT_FIELDS, version_rows
from shared.tenancy import policy_for

class RetentionAwareSearchRetry(VertexSearchStore):
    """Reuse identical retained staging bytes; never overwrite an audit object."""
    def upsert(self, tenant_id, doc_key, source_uri, text, meta):
        c=self.clients()
        self.ensure(tenant_id)
        assert self.bucket, 'AUDIT_BUCKET is required'
        obj=self.object_name(tenant_id,doc_key)
        bucket=c.gcs.bucket(self.bucket)
        blob=bucket.get_blob(obj)
        data=text.encode('utf-8')
        if blob is None:
            bucket.blob(obj).upload_from_string(data,content_type='text/plain; charset=utf-8',if_generation_match=0)
        else:
            assert blob.download_as_bytes(if_generation_match=int(blob.generation))==data, 'Retained staging bytes differ; review without overwriting'
        fields={'tenant_id':tenant_id,'doc_key':doc_key,'source_uri':source_uri,'kind':'text',
                'title':source_uri.rsplit('/',1)[-1],
                **{k:v for k,v in meta.items() if k in STRUCT_FIELDS and v is not None}}
        doc=c.de.Document(id=doc_key,struct_data=fields,
                          content=c.de.Document.Content(uri=f'gs://{self.bucket}/{obj}',mime_type='text/plain'))
        op=c.docs.import_documents(request=c.de.ImportDocumentsRequest(parent=self.branch(tenant_id),
            inline_source=c.de.ImportDocumentsRequest.InlineSource(documents=[doc]),
            reconciliation_mode=c.de.ImportDocumentsRequest.ReconciliationMode.INCREMENTAL))
        result=op.result(timeout=300)
        assert not getattr(result,'error_samples',[]) and not getattr(result,'failure_count',0), 'Search import reported errors'
        return getattr(getattr(op,'operation',None),'name','') or 'import completed'

def main(argv=None):
    ap=argparse.ArgumentParser(description='Retry missing managed copies of current text versions; preview by default')
    ap.add_argument('--tenant',choices=['acme','zeta'],required=True)
    ap.add_argument('--apply',action='store_true')
    a=ap.parse_args(argv)
    db=firestore.Client(project=os.environ['PROJECT'])
    read_policy = lambda tenant_id: policy_for(tenant_id, db=db)
    mirror=Mirror.from_env(db,mode='both',policy_for=read_policy)
    mirror.stores=[RetentionAwareSearchRetry(st.project,st.location,st.bucket) if st.name=='vertex_search' else st for st in mirror.stores]
    assert mirror.policy(a.tenant)=='any', 'Current tenant policy does not permit these outside-India stores'
    held={store.name:set(store.listing(a.tenant)) for store in mirror.stores}
    sources=list(db.collection('sources').where(filter=FieldFilter('tenant_id','==',a.tenant)).stream())
    for snap in sources:
        row=snap.to_dict() or {}
        if row.get('status')!='indexed' or not row.get('doc_key'): continue
        text,meta=version_rows(db,a.tenant,row['doc_key'])
        if not text.strip(): continue
        missing=[store for store in mirror.stores if row['doc_key'] not in held[store.name]]
        if not missing: continue
        print(json.dumps({'source':row.get('name'),'doc_key':row['doc_key'],'missing':[st.name for st in missing],'apply':a.apply}),flush=True)
        if not a.apply: continue
        latest=snap.reference.get().to_dict() or {}
        assert latest.get('doc_key')==row['doc_key'] and latest.get('generation')==row.get('generation') and latest.get('status')=='indexed', 'Source changed; pause ingestion and restart preview'
        scoped=Mirror(db,missing,mode='both',policy_for=read_policy)
        done=scoped.upsert(a.tenant,row['doc_key'],row['gcs_uri'],text,meta)
        assert set(done)=={st.name for st in missing}, 'One or more managed writes failed; inspect logs before retry'
        confirmed={st.name:st.region for st in mirror.stores if row['doc_key'] in held[st.name]}
        confirmed.update(done)
        latest=snap.reference.get().to_dict() or {}
        assert latest.get('doc_key')==row['doc_key'] and latest.get('status')=='indexed', 'Source changed during mirror; inspect drift before stamping'
        mirror.stamp(a.tenant,row['name'],row['doc_key'],confirmed)
    print('Finished. Read --status for both stores and prove query readiness; imports may settle asynchronously.')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
