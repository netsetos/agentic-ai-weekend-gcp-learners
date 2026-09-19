#!/usr/bin/env python3
"""Exact demo inventory and opt-in generation-checked upload. No cloud access by default.

Run from deploy/: python evals/demo_corpus_gate.py --out operator-evidence/corpus.json
Use --upload --project PROJECT to upload only the selected baseline assets, waiting
for each exact source generation to become current. --check-live is read-only.
This gate writes no chunks, vectors, claims or ledger rows; documind-ingest owns those.
"""
from __future__ import annotations
import argparse
from collections import Counter
import hashlib
import importlib.util
import json
from pathlib import Path
import time

HERE = Path(__file__).resolve().parent
MIME = {'.pdf':'application/pdf', '.md':'text/plain', '.png':'image/png', '.mp4':'video/mp4'}
TENANTS = ('acme', 'zeta', 'globex')


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def inventory() -> dict:
    spec = importlib.util.spec_from_file_location('demo_corpus_builder', HERE/'build_corpus.py')
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    real = json.loads((HERE/'real_sources.json').read_text(encoding='utf-8'))
    manifest = json.loads((HERE/'manifest.json').read_text(encoding='utf-8'))
    errors, assets, expected_files = [], [], set()
    def add(tenant, name, kind, expected=None):
        path = HERE/'corpus'/tenant/name
        expected_files.add((tenant, name))
        if not path.is_file() or not path.stat().st_size:
            errors.append(f'Missing/empty: {tenant}/{name}')
            return
        sha = digest(path.read_bytes())
        if expected and sha != expected:
            errors.append(f'Hash/content mismatch: {tenant}/{name}')
        assets.append({'tenant':tenant, 'name':name, 'path':str(path.relative_to(HERE.parent)),
                       'sha256':sha, 'bytes':path.stat().st_size,
                       'content_type':MIME[path.suffix], 'kind':kind})
    counts = Counter(t for src in real for t in src['tenants'])
    if len(real)!=13 or dict(counts)!={'acme':13,'zeta':5,'globex':2}:
        errors.append(f'Real-source assignments changed: {dict(counts)}')
    for src in real:
        for tenant in src['tenants']:
            add(tenant, src['slug']+'.pdf', 'pdf', src['sha256'])
            mirror = HERE/'corpus'/tenant/(src['slug']+'.md')
            if src['text_layer'] and not mirror.is_file():
                errors.append(f'Missing offline text mirror: {mirror.name} ({tenant})')
            if mirror.exists():
                expected_files.add((tenant, mirror.name))
    if {t:len(v) for t,v in builder.DOCS.items()}!={'acme':5,'zeta':2,'globex':1}:
        errors.append('Synthetic document assignments changed')
    for tenant, docs in builder.DOCS.items():
        for name, build, kind in docs:
            expected_files.add((tenant, name))
            path = HERE/'corpus'/tenant/name
            if not path.is_file() or path.read_bytes()!=build().encode('utf-8'):
                errors.append(f'Synthetic source missing or changed: {tenant}/{name}')
            video = path.with_suffix('.mp4')
            if name=='townhall_2026_q1.md' and video.is_file():
                add(tenant, video.name, 'video')
            else:
                add(tenant, name, kind, digest(build().encode('utf-8')))
    for tenant, name, kind, note in builder.RENDERED:
        add(tenant, name, kind)
    whiteboard = HERE/'corpus/acme/whiteboard_arch.png'
    if whiteboard.is_file():
        add('acme', whiteboard.name, 'image')
    declared = Counter(row['tenant_id'] for row in manifest)
    if len(manifest)!=33 or dict(declared)!={'acme':23,'zeta':7,'globex':3}:
        errors.append(f'Manifest coverage changed: {dict(declared)}')
    declared_paths = {(row['tenant_id'], Path(row['file']).name) for row in manifest}
    for asset in assets:
        if (asset['tenant'], asset['name']) not in declared_paths:
            errors.append(f"Selected asset is absent from manifest: {asset['tenant']}/{asset['name']}")
    for tenant in TENANTS:
        folder = HERE/'corpus'/tenant
        for path in folder.iterdir() if folder.is_dir() else []:
            if path.is_file() and (tenant,path.name) not in expected_files and not path.name.endswith('.segments.json'):
                errors.append(f'Unexpected corpus file; review before upload: {tenant}/{path.name}')
    selected = Counter(row['tenant'] for row in assets)
    expected = {'acme':22 if whiteboard.is_file() else 21,'zeta':7,'globex':3}
    if dict(selected)!=expected:
        errors.append(f'Upload selection {dict(selected)} != {expected}')
    if errors:
        raise RuntimeError('\n'.join(errors))
    return {'scope':'demo baseline only; smoke fixtures are separate', 'pdf_copies':dict(counts),
            'unique_pdf_sources':13, 'manifest_entries':33, 'upload_counts':dict(selected),
            'upload_total':len(assets), 'assets':sorted(assets,key=lambda x:(x['tenant'],x['name'])),
            'optional_video':(HERE/'corpus/acme/townhall_2026_q1.mp4').is_file(),
            'optional_whiteboard':whiteboard.is_file()}


def source_ready(db, bucket_name, asset, generation, model, version) -> tuple[bool, str, int]:
    from google.cloud.firestore_v1.base_query import FieldFilter
    tenant, name, sha = asset['tenant'], asset['name'], asset['sha256']
    object_name = tenant+'/'+name
    uri = f'gs://{bucket_name}/{object_name}'
    snap = db.collection('sources').document(object_name.replace('/','~')).get()
    row = snap.to_dict() or {}
    if (row.get('tenant_id')!=tenant or row.get('status')!='indexed' or row.get('sha256')!=sha
            or row.get('doc_key')!=f'{tenant}_{sha}' or str(row.get('generation'))!=str(generation)):
        return False, f"ledger status={row.get('status')}, generation={row.get('generation')}", 0
    query = db.collection('chunks').where(filter=FieldFilter('tenant_id','==',tenant))
    query = query.where(filter=FieldFilter('source_uri','==',uri)).where(filter=FieldFilter('current','==',True))
    chunks = [s.to_dict() or {} for s in query.stream()]
    if not chunks or len(chunks)!=row.get('chunks'):
        return False, f"current chunks={len(chunks)}, ledger chunks={row.get('chunks')}", len(chunks)
    for chunk in chunks:
        dense = chunk.get('embedding')
        if (chunk.get('doc_key')!=f'{tenant}_{sha}' or not (chunk.get('text') or '').strip()
                or dense is None or len(list(dense))!=768 or chunk.get('embedding_model')!=model
                or str(chunk.get('embedding_version'))!=str(version)):
            return False, 'chunk identity/text/dense embedding stamp mismatch', len(chunks)
    return True, 'current ledger and payloads verified', len(chunks)


def cloud_gate(plan, project, upload, timeout, model, version):
    from google.cloud import firestore, storage
    db, bucket = firestore.Client(project=project), storage.Client(project=project).bucket(project+'-uploads')
    results = []
    for asset in plan['assets']:
        name = asset['tenant']+'/'+asset['name']
        blob = bucket.blob(name)
        exists = blob.exists()
        previous = None
        identical = False
        if exists:
            blob.reload()
            previous = int(blob.generation)
            identical = digest(blob.download_as_bytes(if_generation_match=previous))==asset['sha256']
        if upload and not identical:
            blob.upload_from_filename(str(HERE.parent/asset['path']), content_type=asset['content_type'],
                                      if_generation_match=previous if previous is not None else 0, timeout=300)
            blob.reload()
            print(f'UPLOADED {name}: generation={blob.generation}', flush=True)
        elif not identical:
            raise RuntimeError(f'GCS file missing/different: {name}; no writes in --check-live')
        else:
            print(f'REUSE {name}: existing bytes match; no new generation', flush=True)
        if blob.content_type != asset['content_type']:
            raise RuntimeError(f'Unexpected MIME for {name}: {blob.content_type}; review and repair explicitly')
        generation = str(blob.generation)
        deadline = time.monotonic()+timeout
        while True:
            ok, reason, count = source_ready(db,bucket.name,asset,generation,model,version)
            if ok:
                results.append({**asset,'generation':generation,'chunks':count,'status':'verified'})
                print(f'INDEXED {name}: {count} current chunks', flush=True)
                break
            if not upload or time.monotonic()>=deadline:
                raise RuntimeError(f'STOP {name}: {reason}. Inspect worker logs, claims, retries and DLQ; do not delete claims blindly.')
            print(f'WAIT {name}: {reason}', flush=True)
            time.sleep(10)
    return results


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    modes=ap.add_mutually_exclusive_group()
    modes.add_argument('--upload',action='store_true')
    modes.add_argument('--check-live',action='store_true')
    ap.add_argument('--project')
    ap.add_argument('--timeout',type=int,default=1200)
    ap.add_argument('--model',default='text-embedding-005')
    ap.add_argument('--version',default='1')
    ap.add_argument('--out',default='operator-evidence/corpus-inventory.json')
    a=ap.parse_args()
    if (a.upload or a.check_live) and not a.project:
        ap.error('--project is required for cloud access')
    if a.timeout<1:
        ap.error('--timeout must be positive')
    plan=inventory()
    print(json.dumps({k:v for k,v in plan.items() if k!='assets'},indent=2),flush=True)
    if a.upload or a.check_live:
        plan['live_evidence']=cloud_gate(plan,a.project,a.upload,a.timeout,a.model,a.version)
    path=Path(a.out)
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(plan,indent=2)+'\n',encoding='utf-8')
    print('PASS:',path,flush=True)

if __name__=='__main__':
    main()
