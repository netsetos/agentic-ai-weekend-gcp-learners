"""Offline checks for document encoding and retryable ANN/Firestore repair.

Run: python -m unittest discover -s commands/tests -p test_document_embeddings.py
Cloud SDK doubles isolate the write ordering; these are not live-cloud quality tests.
"""
from copy import deepcopy
import importlib.util
import os
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def module(name, **attrs):
    result = ModuleType(name)
    result.__dict__.update(attrs)
    return result


class Point(SimpleNamespace):
    Restriction = SimpleNamespace
    SparseEmbedding = SimpleNamespace


fs = module('google.cloud.firestore', Client=object, SERVER_TIMESTAMP='SERVER_TIMESTAMP')
cloud = module('google.cloud', firestore=fs, aiplatform=SimpleNamespace())
genai = module('google.genai', Client=Mock())
stubs = {
    'google': module('google', genai=genai, cloud=cloud),
    'google.cloud': cloud, 'google.cloud.firestore': fs, 'google.genai': genai,
    'google.cloud.aiplatform_v1.types': module('types', IndexDatapoint=Point),
    'google.cloud.firestore_v1.vector': module('vector', Vector=list),
    'contracts': module('contracts', SCHEMA_VERSION=2),
}
with patch.dict(sys.modules, stubs), patch.dict(os.environ, {'GOOGLE_CLOUD_PROJECT': 'test-project'}), \
        patch.object(sys, 'path', [str(ROOT), *sys.path]):
    indexer = load_module('embedding_indexer', ROOT / 'services/ingest/indexer.py')
    idempotency = load_module('embedding_idempotency', ROOT / 'services/ingest/idempotency.py')


class Snapshot:
    def __init__(self, db, key):
        self.id, self.reference = key, key
        self.row = deepcopy(db.rows[key])
        self.update_time = db.versions[key]
    def to_dict(self):
        return deepcopy(self.row)


class Query:
    def __init__(self, db, filters=()):
        self.db, self.filters = db, filters
    def where(self, key, operator, value):
        assert operator == '=='
        return Query(self.db, (*self.filters, (key, value)))
    def stream(self):
        return iter([Snapshot(self.db, k) for k, row in self.db.rows.items()
                     if all(row.get(field) == value for field, value in self.filters)])


class Batch:
    def __init__(self, db):
        self.db, self.updates = db, []
    def update(self, ref, fields, option):
        self.updates.append((ref, fields, option))
    def commit(self):
        self.db.events.append('firestore')
        if self.db.fail_commit:
            raise RuntimeError('commit failed')
        if any(self.db.versions[k] != version for k, _, version in self.updates):
            raise RuntimeError('concurrent modification')
        for key, fields, _ in self.updates:
            self.db.rows[key].update(fields)
            self.db.versions[key] += 1


class DB:
    def __init__(self, rows):
        self.rows = deepcopy(rows)
        self.versions = dict.fromkeys(rows, 1)
        self.events, self.fail_commit = [], False
    def collection(self, name):
        assert name == 'chunks'
        return Query(self)
    def batch(self):
        return Batch(self)
    def write_option(self, *, last_update_time):
        return last_update_time


def row(**updates):
    return dict({'tenant_id': 'acme', 'current': True, 'text': 'PB-02 — Probation: six months.',
                 'source_uri': 'gs://test/acme/hr.md', 'doc_key': 'acme_hash',
                 'chunk_hash': 'hash', 'embedding': [0.1] * 768,
                 'embedding_model': indexer.EMBEDDING_MODEL, 'embedding_version': indexer.EMBEDDING_VERSION}, **updates)


class DocumentEmbeddingTests(unittest.TestCase):
    def setUp(self):
        self.sdk = patch.object(indexer, '_embed').start()
        self.sdk.models.embed_content.side_effect = lambda **kw: SimpleNamespace(
            embeddings=[SimpleNamespace(values=[0.2] * 768) for _ in kw['contents']])
        self.addCleanup(patch.stopall)
        patch.object(indexer, 'DRY_RUN', False).start()

    def test_documents_request_document_task_and_dimension(self):
        self.assertEqual(len(indexer.embed_all(['one', 'two'])), 2)
        self.assertEqual(self.sdk.models.embed_content.call_args.kwargs['config'],
                         {'task_type': 'RETRIEVAL_DOCUMENT', 'output_dimensionality': 768})

    def test_bad_or_incomplete_embedding_response_is_rejected(self):
        self.sdk.models.embed_content.side_effect = None
        for vectors in ([], [[0.0]], [[float('nan')] * 768], [[float('inf')] * 768]):
            self.sdk.models.embed_content.return_value = SimpleNamespace(
                embeddings=[SimpleNamespace(values=v) for v in vectors])
            with self.subTest(vectors=len(vectors)), self.assertRaises(ValueError):
                indexer.embed_all(['one'])

    def test_carry_over_rejects_legacy_task_version_and_bad_dimension(self):
        good = row(embedding_task_type='RETRIEVAL_DOCUMENT')
        doc = SimpleNamespace(tenant_id='acme', gcs_uri='gs://test/acme/hr.md')
        self.assertEqual(indexer.held_vectors(DB({'id': good}), doc), {'hash': [0.1] * 768})
        for change in ({'embedding_task_type': None}, {'embedding_task_type': 'RETRIEVAL_QUERY'},
                       {'embedding_version': 'old'}, {'embedding_model': 'other'}, {'embedding': [0.1]}):
            with self.subTest(change=change):
                self.assertEqual(indexer.held_vectors(DB({'id': dict(good, **change)}), doc), {})

    def test_repair_preserves_ids_and_content_then_reuses_on_retry(self):
        db = DB({'acme:hash#2': row(), 'zeta:hash#1': row(tenant_id='zeta'),
                 'old': row(current=False)})
        def upsert(index, points):
            db.events.append('ann')
            self.assertEqual([p.datapoint_id for p in points], ['acme:hash#2'])
            self.assertEqual(points[0].restricts[0].allow_list, ['acme'])
        with patch.object(indexer, 'upsert', side_effect=upsert):
            self.assertEqual(indexer.backfill('index', db, 'acme'), 1)
            self.assertEqual(db.events, ['ann', 'firestore'])
            self.assertEqual(db.rows['acme:hash#2']['text'], row()['text'])
            self.assertEqual(db.rows['acme:hash#2']['embedding'], [0.2] * 768)
            self.assertEqual(db.rows['acme:hash#2']['embedding_task_type'], 'RETRIEVAL_DOCUMENT')
            self.assertNotIn('embedding_task_type', db.rows['zeta:hash#1'])
            self.assertNotIn('embedding_task_type', db.rows['old'])
            indexer.backfill('index', db, 'acme')
            self.assertEqual(self.sdk.models.embed_content.call_count, 1)
            self.assertEqual(db.events, ['ann', 'firestore', 'ann'])

    def test_failed_ann_never_marks_migrated(self):
        db = DB({'id': row()})
        with patch.object(indexer, 'upsert', side_effect=RuntimeError('ann failed')):
            with self.assertRaisesRegex(RuntimeError, 'ann failed'):
                indexer.backfill('index', db)
        self.assertNotIn('embedding_task_type', db.rows['id'])
        self.assertEqual(db.events, [])

    def test_failed_commit_is_retryable(self):
        db = DB({'id': row()}); db.fail_commit = True
        with patch.object(indexer, 'upsert'):
            with self.assertRaisesRegex(RuntimeError, 'commit failed'):
                indexer.backfill('index', db)
            self.assertNotIn('embedding_task_type', db.rows['id'])
            db.fail_commit = False
            indexer.backfill('index', db)
            self.assertTrue(indexer.document_embedding_matches(db.rows['id']))

    def test_concurrent_changes_are_not_overwritten(self):
        db = DB({'id': row()})
        def change(*args):
            db.rows['id']['current'] = False
            db.versions['id'] += 1
        with patch.object(indexer, 'upsert', side_effect=change):
            with self.assertRaisesRegex(RuntimeError, 'concurrent modification'):
                indexer.backfill('index', db)
        self.assertFalse(db.rows['id']['current'])
        self.assertNotIn('embedding_task_type', db.rows['id'])

    def test_dry_run_cannot_checkpoint_unpublished_vectors(self):
        with patch.object(indexer, 'DRY_RUN', True), self.assertRaises(ValueError):
            indexer.backfill('index', DB({'id': row()}))
        self.sdk.models.embed_content.assert_not_called()

    def test_invalid_rows_fail_before_writing(self):
        for change in ({'text': ''}, {'tenant_id': ''}, {'staged': True}):
            with self.subTest(change=change), patch.object(indexer, 'upsert') as upsert:
                with self.assertRaises(ValueError):
                    indexer.backfill('index', DB({'id': row(**change)}))
                upsert.assert_not_called()

    def test_undo_repairs_legacy_vectors_and_persists_them(self):
        db = DB({'acme:hash#2': row(), 'other': row(doc_key='other')})
        with patch.object(indexer, 'upsert') as upsert:
            self.assertEqual(indexer.reupsert('index', db, 'acme', 'gs://test/acme/hr.md', 'acme_hash'), 1)
            self.assertEqual(upsert.call_args.args[1][0].datapoint_id, 'acme:hash#2')
        self.assertTrue(indexer.document_embedding_matches(db.rows['acme:hash#2']))
        self.assertNotIn('embedding_task_type', db.rows['other'])

    def test_notebook_adoption_requires_document_stamp(self):
        db = DB({'id': row()})
        args = (db, 'acme', 'gs://test/acme/hr.md', 'acme_hash', indexer.EMBEDDING_MODEL, indexer.EMBEDDING_VERSION)
        self.assertEqual(idempotency.current_chunks(*args), 0)
        db.rows['id']['embedding_task_type'] = 'RETRIEVAL_DOCUMENT'
        self.assertEqual(idempotency.current_chunks(*args), 1)

    def test_later_batch_failure_keeps_earlier_progress(self):
        db = DB({str(i): row() for i in range(3)})
        with patch.object(indexer, 'upsert', side_effect=[None, RuntimeError('second batch')]):
            with self.assertRaisesRegex(RuntimeError, 'second batch'):
                indexer.backfill('index', db, batch_size=2)
        self.assertTrue(indexer.document_embedding_matches(db.rows['0']))
        self.assertTrue(indexer.document_embedding_matches(db.rows['1']))
        self.assertNotIn('embedding_task_type', db.rows['2'])


if __name__ == '__main__':
    unittest.main()
