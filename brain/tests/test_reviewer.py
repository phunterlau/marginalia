"""Offline reviewer boundary and audit tests; no real cards are accepted."""
import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

pytest.importorskip('fastapi')
pytest.importorskip('httpx')
from fastapi.testclient import TestClient
from research_brain import Brain
from research_brain.reviewer import create_app
from test_milestones import FakeExtractionProvider


@pytest.fixture
def setup(tmp_path):
    brain = Brain(tmp_path / 'brain', extraction_provider_factory=FakeExtractionProvider)
    paper = tmp_path / 'paper.md'
    paper.write_text('# Contrast\n\nA paired contrast estimates a direction.\n\n$$x=y$$\n\nLimits apply.\n')
    doc = brain.ingest(paper)
    run = brain.extract('methods', doc.document_id, live=True)
    app = create_app(brain.root)
    client = TestClient(app, base_url='http://127.0.0.1:8765')
    headers = {'origin':'http://127.0.0.1:8765','x-review-token':client.get('/api/session').json()['token']}
    return brain, doc, run, client, headers


def test_read_only_and_context(setup):
    brain, doc, run, client, _ = setup
    digest = lambda: hashlib.sha256(brain.store.path.read_bytes()).hexdigest()
    before = digest()
    assert client.get('/api/documents').status_code == 200
    queue = client.get('/api/cards').json()
    assert queue['total'] == 1
    card = client.get('/api/cards/'+run.object_ids[0]).json()
    evidence = client.get('/api/evidence/'+card['evidence'][0]['block_id']).json()
    assert evidence['compilation_id']
    assert len(evidence['context']) <= 4
    assert 'source_path' not in evidence
    assert digest() == before
    assert client.get('/api/cards/missing').status_code == 404
    assert client.get('/api/evidence/missing').status_code == 404
    assert client.get('/api/cards?limit=101').status_code == 400


def test_decision_conflict_audit_and_recall(setup):
    brain, _, run, client, headers = setup
    oid = run.object_ids[0]
    original = client.get('/api/cards/'+oid).json()
    payload = dict(decision='ACCEPTED', note='', expected_version=original['updated_at'])
    assert not any(h.record_id == oid for h in brain.recall('paired contrast'))
    response = client.post('/api/cards/'+oid+'/review', json=payload, headers=headers)
    assert response.status_code == 200, response.text
    assert client.post('/api/cards/'+oid+'/review', json=payload, headers=headers).status_code == 409
    updated = response.json()
    assert updated['structured'] == original['structured']
    assert updated['origin'] == original['origin']
    assert len([e for e in updated['history'] if e['event_type'] == 'object_reviewed']) == 1
    assert any(h.record_id == oid for h in brain.recall('paired contrast'))
    reopened = TestClient(create_app(brain.root),base_url='http://127.0.0.1:8765')
    assert reopened.get('/api/cards/'+oid).json()['review_state'] == 'ACCEPTED'


def test_security_and_validation(setup):
    brain, _, run, client, headers = setup
    oid = run.object_ids[0]
    version = brain.get_research_object(oid)['updated_at']
    payload = dict(decision='REJECTED',note='',expected_version=version)
    path = '/api/cards/'+oid+'/review'
    assert client.post(path,json=payload).status_code == 403
    assert client.post(path,json=payload,headers={**headers,'origin':'https://evil.example'}).status_code == 403
    assert client.get('/api/session',headers={'host':'evil.example'}).status_code == 403
    assert client.post(path,json=payload,headers=headers).status_code == 400
    assert client.post(path,content='x'*17000,headers=headers).status_code == 413
    assert client.post(path,json=[],headers=headers).status_code == 400
    assert client.post('/api/ingest',json={},headers=headers).status_code in {404,405}
    assert brain.get_research_object(oid)['review_state'] == 'UNREVIEWED'


def test_latest_before_state_and_pagination(setup):
    brain, doc, first, client, headers = setup
    with patch('research_brain.extraction.PROMPT_VERSION','next-reviewer-test'):
        second = brain.extract('methods',doc.document_id,live=True)
    brain.review_research_object(second.object_ids[0],review_state='ACCEPTED')
    assert client.get('/api/cards').json()['items'] == []
    assert brain.list_research_objects(kinds=['method_card'],review_states=['UNREVIEWED'],latest_extraction_only=True) == []
    assert client.get('/api/cards?latest=false').json()['items'][0]['id'] == first.object_ids[0]
    pages=[client.get(f'/api/cards?latest=false&state=&limit=1&offset={i}').json()['items'][0]['id'] for i in range(2)]
    assert len(set(pages)) == 2


def test_atomic_failure_and_concurrent_writers(setup):
    brain, _, run, _, _ = setup
    oid = run.object_ids[0]
    version = brain.get_research_object(oid)['updated_at']
    with patch.object(brain.store,'_append_event',side_effect=RuntimeError('injected')):
        with pytest.raises(RuntimeError):
            brain.review_research_object(oid,review_state='ACCEPTED',expected_version=version)
    assert brain.get_research_object(oid)['updated_at'] == version
    def write():
        try:
            brain.review_research_object(oid,review_state='ACCEPTED',expected_version=version)
            return 'saved'
        except ValueError:
            return 'conflict'
    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(lambda _:write(),range(2))) == ['conflict','saved']


def test_no_initialization_or_migration(tmp_path):
    with pytest.raises(sqlite3.OperationalError):
        create_app(tmp_path/'missing')
    assert not (tmp_path/'missing').exists()
    brain = Brain(tmp_path/'brain')
    with brain.store.connect() as conn:
        conn.execute('DELETE FROM schema_migrations WHERE version=2')
    with pytest.raises(ValueError,match='Incompatible'):
        create_app(brain.root)
