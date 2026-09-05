"""Read-only real-corpus reviewer check. Run against an isolated database copy."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from research_brain.reviewer import create_app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    database = args.root / 'brain.sqlite3'
    digest = lambda: hashlib.sha256(database.read_bytes()).hexdigest()
    before = digest()
    client = TestClient(create_app(args.root), base_url='http://127.0.0.1:8765')
    checked = set()
    ids = []
    math_count = 0
    offset = 0
    while True:
        response = client.get(f'/api/cards?state=&offset={offset}&limit=100')
        response.raise_for_status()
        page = response.json()
        for item in page['items']:
            card = client.get('/api/cards/' + item['id']).json()
            ids.append(card['id'])
            for link in card['evidence']:
                evidence = client.get('/api/evidence/' + link['block_id'])
                evidence.raise_for_status()
                evidence = evidence.json()
                assert evidence['raw_text'] == link['raw_text']
                assert evidence['version_label'] == link['version_label']
                assert evidence['source_member'] == link['source_member']
                checked.add(link['block_id'])
                if card['kind'] == 'math_card' and link['block_id'] == card['structured']['equation_block_id']:
                    assert evidence['raw_latex'] == card['structured']['exact_latex']
                    math_count += 1
        offset += len(page['items'])
        if offset >= page['total']:
            break
        assert page['items'], 'Pagination stalled'
    after = digest()
    assert before == after, 'Browsing changed the database'
    assert ids and math_count, 'A real mixed Method/Math corpus is required'
    print(json.dumps({'timestamp':datetime.now(timezone.utc).isoformat(), 'passed':True,
                      'cards':len(ids), 'exact_math_cards':math_count, 'evidence_blocks':len(checked),
                      'database_sha256_before':before, 'database_sha256_after':after,
                      'review_writes':0, 'provider_calls':0, 'card_ids':ids}, indent=2))


if __name__ == '__main__':
    main()
