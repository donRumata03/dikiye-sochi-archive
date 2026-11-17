#!/usr/bin/env python3
"""Explicit live test: download into an empty temporary archive, then run again.

No GitHub writes. Run manually, not as part of offline unit tests.
"""
import argparse
import contextlib
import functools
import hashlib
import io
import importlib.metadata
import json
import platform
import shutil
import tempfile
import threading
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urljoin, urlparse
from unittest.mock import patch

import requests
from bs4 import BeautifulSoup

import build_site
import sync

ROOT = Path(__file__).resolve().parent


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def check_site(root, manifest):
    publish = root / 'publish'
    project = publish / 'dikiye-sochi-archive'
    project.parent.mkdir()
    shutil.copytree(root / 'site', project)
    handler = functools.partial(QuietHandler, directory=str(publish))
    checks = []
    with ThreadingHTTPServer(('127.0.0.1', 0), handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f'http://127.0.0.1:{server.server_port}/dikiye-sochi-archive/'
            page = requests.get(base, timeout=10)
            page.raise_for_status()
            soup = BeautifulSoup(page.content, 'html.parser')
            anchors = soup.select('a[download]')
            assert len(anchors) == len(manifest['tracks'])
            expected = {row['name']: row['sha256'] for row in manifest['tracks']}
            for anchor in anchors:
                href = anchor['href']
                assert not href.startswith('/')
                result = requests.get(urljoin(base, href), timeout=10)
                result.raise_for_status()
                digest = hashlib.sha256(result.content).hexdigest()
                assert digest == expected[anchor.text]
                checks.append({'href': href, 'http_status': result.status_code, 'sha256_matches': True})
        finally:
            server.shutdown()
            thread.join(timeout=5)
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'evidence/live-check.json')
    args = parser.parse_args()
    report = {'checked_at': datetime.now(timezone.utc).isoformat(), 'authorization_header_count': 0,
              'python_version': platform.python_version(),
              'packages': {name: importlib.metadata.version(name) for name in ('requests', 'beautifulsoup4')}}
    requests_seen = []
    original_send = sync.SESSION.send

    def audit_send(request, **kwargs):
        has_auth = 'Authorization' in request.headers
        report['authorization_header_count'] += int(has_auth)
        assert not has_auth, 'Live test must be anonymous'
        response = original_send(request, **kwargs)
        # Never persist temporary signed URLs or cookies.
        requests_seen.append({'host': urlparse(request.url).hostname, 'status': response.status_code})
        return response

    log = io.StringIO()
    with tempfile.TemporaryDirectory(prefix='dikiye-live-') as temporary:
        root = Path(temporary)
        with patch.multiple(sync, BASE=root, DATA=root / 'data', FILES=root / 'data/tracks',
                            MANIFEST=root / 'data/manifest.json'), \
                patch.object(sync.SESSION, 'send', side_effect=audit_send), \
                contextlib.redirect_stdout(log):
            first_code = sync.main()
            first = json.loads(sync.MANIFEST.read_text(encoding='utf-8'))
            report['first_run'] = first['last_run']
            assert first_code == 0, 'First live sync failed'
            assert first['last_run']['downloaded'], 'Live test did not download any files'
            files = []
            for row in first['tracks']:
                path = root / row['file']
                points = None
                if path.suffix.lower() == '.gpx':
                    gpx = ET.parse(path).getroot()
                    points = sum(1 for node in gpx.iter() if node.tag.split('}')[-1] in ('trkpt', 'rtept', 'wpt'))
                delivered = ROOT / row['file']
                files.append({**row, 'gpx_points': points,
                              'matches_delivered_archive': delivered.is_file() and
                              hashlib.sha256(delivered.read_bytes()).hexdigest() == row['sha256']})
            report['files'] = files
            posts = first['posts']
            report['telegram'] = {
                'posts_saved': len(posts), 'min_id': min(p['id'] for p in posts),
                'max_id': max(p['id'] for p in posts),
                'links_with_example_post': {
                    url: next(p['url'] for p in reversed(posts) if url in p['yandex_links'])
                    for url in first['sources'] if any(url in p['yandex_links'] for p in posts)
                },
            }
            second_code = sync.main()
            second = json.loads(sync.MANIFEST.read_text(encoding='utf-8'))
            report['repeat_run'] = second['last_run']
            assert second_code == 0
            assert not second['last_run']['downloaded']
            assert second['last_run']['reused'] == len(first['tracks'])
            assert len(second['tracks']) == len(first['tracks'])
            with patch.multiple(build_site, ROOT=root, SITE=root / 'site', MANIFEST=sync.MANIFEST):
                build_site.main()
            report['project_site_http_checks'] = check_site(root, second)
    report['requests'] = requests_seen
    report['ok'] = True
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    args.output.with_suffix('.log').write_text(log.getvalue(), encoding='utf-8')
    print(log.getvalue(), end='')
    print(f'Live verification passed: {len(report["files"])} downloads, no auth, repeat run without duplicates')
    print(f'Report: {args.output}')


if __name__ == '__main__':
    main()
