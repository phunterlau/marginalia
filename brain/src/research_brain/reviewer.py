"""Optional, loopback-only human review boundary. No provider or ingestion calls."""
from __future__ import annotations

import json
from pathlib import Path
import re
import secrets

from .store.sqlite import SQLiteStore, ORIGINS, REVIEW_STATES


def create_app(root: Path, port: int = 8765, *, static_dir: Path | None = None):
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import JSONResponse
    from fastapi.staticfiles import StaticFiles

    # FastAPI evaluates route annotations in module globals.
    globals()['Request'] = Request
    store = SQLiteStore(root.expanduser().resolve() / 'brain.sqlite3', initialize=False)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    token = secrets.token_urlsafe(32)
    host = f'127.0.0.1:{port}'
    origin = f'http://{host}'

    @app.middleware('http')
    async def boundary(request: Request, call_next):
        if request.headers.get('host') != host or request.headers.get('origin', origin) != origin:
            return JSONResponse({'detail': 'Invalid Host or Origin'}, status_code=403)
        if request.method not in {'GET', 'HEAD'}:
            if request.headers.get('origin') != origin or not secrets.compare_digest(request.headers.get('x-review-token', ''), token):
                return JSONResponse({'detail': 'Review session required'}, status_code=403)
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 16384:
                    return JSONResponse({'detail': 'Request too large'}, status_code=413)
            request._body = bytes(body)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'"
        return response

    def identifier(value):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value):
            raise HTTPException(400, 'Invalid ID')
        return value

    def card(value):
        record = store.get_object_record(identifier(value))
        if not record or record['kind'] not in {'method_card', 'math_card'}:
            raise HTTPException(404, 'Card not found')
        return record

    @app.get('/api/session')
    def session():
        return {'token': token, 'origins': sorted(ORIGINS), 'review_states': sorted(REVIEW_STATES)}

    @app.get('/api/documents')
    def documents():
        with store.connect() as conn:
            return [dict(r) for r in conn.execute('SELECT id,title FROM documents ORDER BY title,id')]

    @app.get('/api/cards')
    def cards(kind: str | None = None, document: str | None = None, origin: str | None = None,
              state: str | None = 'UNREVIEWED', latest: bool = True, offset: int = 0, limit: int = 25):
        if not 1 <= limit <= 100 or not 0 <= offset <= 100000:
            raise HTTPException(400, 'Invalid pagination')
        if kind and kind not in {'method_card', 'math_card'} or origin and origin not in ORIGINS or state and state not in REVIEW_STATES:
            raise HTTPException(400, 'Invalid filter')
        if document and store.get_document(identifier(document)) is None:
            raise HTTPException(404, 'Document not found')
        records = store.list_object_records(kinds=[kind] if kind else ['method_card', 'math_card'],
                                            document_id=document, limit=None)
        # Cohorts are selected independently of review state and origin.
        if latest:
            runs = store.get_generation_runs(sorted({r['extraction_run_id'] for r in records if r.get('extraction_run_id')}))
            def cohort(r):
                run = runs.get(r.get('extraction_run_id'))
                return ((run['task'], tuple(sorted({e['document_id'] for e in r['evidence']}))),
                        (run.get('completed_at') or run['created_at'], run['id'])) if run else (None, None)
            newest = {}
            for r in records:
                key, value = cohort(r)
                if key is not None:
                    newest[key] = max(newest.get(key, value), value)
            records = [r for r in records if cohort(r)[0] is None or newest[cohort(r)[0]] == cohort(r)[1]]
        records = [r for r in records if (not state or r['review_state'] == state) and (not origin or r['origin'] == origin)]
        records.sort(key=lambda r: (r['created_at'], r['id']))
        return {'total': len(records), 'items': [{k: r[k] for k in ('id', 'title', 'kind', 'origin', 'review_state', 'updated_at')} for r in records[offset:offset+limit]]}

    @app.get('/api/cards/{object_id}')
    def read_card(object_id: str):
        result = card(object_id)
        with store.connect() as conn:
            result['history'] = [dict(r) for r in conn.execute('SELECT * FROM events WHERE object_id=? ORDER BY created_at,id', (object_id,))]
        run = store.get_generation_runs([result['extraction_run_id']]).get(result['extraction_run_id']) if result.get('extraction_run_id') else None
        result['generation'] = {k: run.get(k) for k in ('id','model','provider','prompt_version','schema_version','completed_at')} if run else None
        return result

    @app.get('/api/evidence/{block_id}')
    def evidence(block_id: str):
        result = store.get_evidence(identifier(block_id))
        if result is None:
            raise HTTPException(404, 'Evidence not found')
        result.pop('source_path', None)
        with store.connect() as conn:
            neighbors = []
            for op, order in [('<', 'DESC'), ('>', 'ASC')]:
                neighbors.extend(dict(r) for r in conn.execute(f'SELECT id,raw_text,raw_latex,source_member,line_start,line_end,page,ordinal FROM document_blocks WHERE compilation_id=? AND section_path IS ? AND ordinal {op} ? ORDER BY ordinal {order} LIMIT 2', (result['compilation_id'], result['section_path'], result['ordinal'])))
        result['context'] = sorted(neighbors, key=lambda r: r['ordinal'])
        return result

    @app.post('/api/cards/{object_id}/review')
    async def review(object_id: str, request: Request):
        card(object_id)
        try:
            payload = await request.json()
            if not isinstance(payload, dict) or set(payload) != {'decision','note','expected_version'}:
                raise ValueError('Expected decision, note, expected_version')
            decision, note, version = payload['decision'], payload['note'], payload['expected_version']
            if decision not in {'ACCEPTED','DISPUTED','REJECTED'} or not isinstance(note, str) or len(note) > 4000 or not isinstance(version, str) or not 1 <= len(version) <= 100:
                raise ValueError('Invalid review')
            if decision != 'ACCEPTED' and not note.strip():
                raise ValueError('A note is required for dispute or rejection')
            store.review_object(object_id, review_state=decision, note=note.strip() or None,
                                expected_version=version, actor='user')
        except (ValueError, TypeError) as exc:
            raise HTTPException(409 if 'conflict' in str(exc) else 400, str(exc)) from exc
        return read_card(object_id)

    assets = static_dir or Path(__file__).parent / 'reviewer_static'
    if assets.is_dir():
        app.mount('/', StaticFiles(directory=assets, html=True), name='reader')
    return app


def serve(root: Path, port: int):
    if not 1024 <= port <= 65535:
        raise ValueError('Port must be between 1024 and 65535')
    import uvicorn
    assets = Path(__file__).parent / 'reviewer_static'
    if not (assets / 'index.html').is_file():
        raise ValueError('Build the reviewer frontend first: cd reviewer-ui && npm ci && npm run build')
    app = create_app(root, port)
    print(f'Memory Review Workbench: http://127.0.0.1:{port}', flush=True)
    uvicorn.run(app, host='127.0.0.1', port=port, access_log=False)
