#!/usr/bin/env python3
"""Archive routes from public Telegram posts and Yandex Disk, without OAuth."""
import hashlib
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

BASE = Path(__file__).resolve().parent
DATA = BASE / 'data'
FILES = DATA / 'tracks'
MANIFEST = DATA / 'manifest.json'
TG_CHANNEL = os.getenv('TELEGRAM_CHANNEL', 'dikiye_sochi').strip('@')
FALLBACK = os.getenv('YANDEX_DISK_URL', 'https://disk.yandex.ru/d/msmGZTv0QtuLhA')
API = 'https://cloud-api.yandex.net/v1/disk/public/resources'
SESSION = requests.Session()
SESSION.headers.update({'User-Agent': 'Mozilla/5.0 (compatible; DikiyeArchive/1.1)'})
# An explicit no-op auth handler prevents implicit .netrc credentials.
SESSION.auth = lambda request: request
TIMEOUT = (10, 35)
MAX_BYTES = 90 * 1024 * 1024
EXTENSIONS = ('.gpx', '.kml', '.kmz', '.fit', '.tcx', '.zip')
DISK_HOSTS = {'disk.yandex.ru', 'disk.yandex.com', 'disk.360.yandex.ru', 'yadi.sk'}


def get(url, **kw):
    for attempt in range(4):
        try:
            response = SESSION.get(url, timeout=TIMEOUT, **kw)
        except (requests.Timeout, requests.ConnectionError):
            if attempt == 3:
                raise
        else:
            if response.status_code not in (429, 500, 502, 503, 504) or attempt == 3:
                try:
                    response.raise_for_status()
                except requests.RequestException:
                    response.close()
                    raise
                return response
            response.close()
        time.sleep(2 ** attempt)


def get_json(url, **kw):
    with get(url, **kw) as response:
        return response.json()


def disk_link(url):
    """Canonicalize root public-share links; strip tracking and URL fragments."""
    parsed = urlparse(url.strip())
    if (parsed.scheme not in ('http', 'https') or parsed.netloc.lower() not in DISK_HOSTS
            or not re.fullmatch(r'/[di]/[A-Za-z0-9_-]+/?', parsed.path)):
        return None
    return 'https://disk.yandex.ru' + parsed.path.rstrip('/')


def parse_posts(markup):
    posts = {}
    ids = []
    soup = BeautifulSoup(markup, 'html.parser')
    for element in soup.select('.tgme_widget_message[data-post]'):
        key = element.get('data-post', '')
        if '/' not in key:
            continue
        channel, msgid = key.rsplit('/', 1)
        if channel.lower() != TG_CHANNEL.lower() or not msgid.isdigit():
            continue
        msgid = int(msgid)
        ids.append(msgid)
        body = element.select_one('.tgme_widget_message_text')
        if body is None:
            continue
        body_text = body.get_text(' ', strip=True)
        candidates = [a.get('href', '') for a in body.select('a[href]')]
        candidates += re.findall(r'https?://[^\s<>"\']+', body_text)
        links = []
        for candidate in candidates:
            link = disk_link(candidate.rstrip('.,);!'))
            if link and link not in links:
                links.append(link)
        date_element = element.select_one('time[datetime]')
        match = re.search(r'\b\d{2}\.\d{2}\.\d{4}\b', body_text)
        posts[msgid] = {
            'id': msgid, 'url': f'https://t.me/{channel}/{msgid}',
            'published_at': date_element.get('datetime', '') if date_element else '',
            'text': body_text[:1200], 'yandex_links': links,
            'event_date': match.group() if match else '',
        }
    return posts, ids


def collect_posts(page_limit=14, since_id=None):
    """Read recent history, or continue until overlapping the previous scan."""
    if page_limit < 1:
        raise ValueError('TELEGRAM_PAGE_LIMIT must be positive')
    posts = {}
    before = None
    for _ in range(page_limit):
        params = {'before': before} if before else {}
        with get(f'https://t.me/s/{TG_CHANNEL}', params=params) as response:
            page, ids = parse_posts(response.text)
        if not ids:
            if before is None:
                raise RuntimeError('Telegram returned no message elements')
            break
        if before is not None and min(ids) >= before:
            raise RuntimeError('Telegram pagination made no progress')
        posts.update(page)
        before = min(ids)
        if before == 1 or (since_id is not None and before <= since_id):
            break
    else:
        if since_id is not None and before > since_id:
            raise RuntimeError('Telegram scan limit reached before previous scan; increase TELEGRAM_PAGE_LIMIT')
    return [posts[k] for k in sorted(posts)]


def public_items(public_url, path=None):
    params = {'public_key': public_url, 'limit': 1000, 'offset': 0}
    if path is not None:
        params['path'] = path
    while True:
        info = get_json(API, params=params)
        if info.get('type') == 'file':
            yield info
            return
        if info.get('type') != 'dir':
            raise RuntimeError('Unknown Yandex resource type')
        embedded = info['_embedded']
        entries = embedded['items']
        for item in entries:
            if item.get('type') == 'dir':
                yield from public_items(public_url, item['path'])
            elif item.get('type') == 'file':
                yield item
        params['offset'] += len(entries)
        if not entries:
            if params['offset'] < embedded.get('total', params['offset']):
                raise RuntimeError('Yandex listing ended before total')
            return
        if 'total' in embedded:
            if params['offset'] >= embedded['total']:
                return
        elif len(entries) < embedded.get('limit', params['limit']):
            return


def safe_part(value):
    value = re.sub(r'[^\w.\- ]+', '_', value, flags=re.UNICODE).strip(' ._')
    return value[:100] or 'route'


def archive_path(relative):
    candidate = (BASE / relative).resolve()
    if not candidate.is_relative_to(FILES.resolve()):
        raise ValueError('Manifest path is outside data/tracks')
    return candidate


def verify_file(path, entry):
    size = path.stat().st_size
    if size == 0 or size > MAX_BYTES:
        raise ValueError('Empty file or file exceeds archive limit')
    if entry.get('size') is not None and size != entry['size']:
        raise ValueError('Downloaded size differs from Yandex metadata')
    payload = path.read_bytes()
    hashes = {'md5': hashlib.md5(payload).hexdigest(), 'sha256': hashlib.sha256(payload).hexdigest()}
    for algorithm, actual in hashes.items():
        if entry.get(algorithm) and actual != entry[algorithm].lower():
            raise ValueError(f'{algorithm.upper()} mismatch')
    if entry['name'].lower().endswith('.gpx'):
        root = ET.fromstring(payload)
        if root.tag.split('}')[-1] != 'gpx':
            raise ValueError('Downloaded XML is not GPX')
    return hashes


def download_file(public_url, entry, destination):
    if entry.get('size', 0) > MAX_BYTES:
        raise ValueError('File exceeds 90 MiB archive limit')
    params = {'public_key': public_url, 'path': entry['path']}
    direct = get_json(API + '/download', params=params)['href']
    if urlparse(direct).scheme != 'https':
        raise ValueError('Yandex returned a non-HTTPS download URL')
    tmp = destination.with_suffix(destination.suffix + '.part')
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with get(direct, stream=True) as response, tmp.open('wb') as out:
            size = 0
            for chunk in response.iter_content(chunk_size=256 * 1024):
                size += len(chunk)
                if size > MAX_BYTES:
                    raise ValueError('Download exceeds 90 MiB archive limit')
                out.write(chunk)
        hashes = verify_file(tmp, entry)
        tmp.replace(destination)
        return hashes
    finally:
        tmp.unlink(missing_ok=True)


def write_manifest(manifest):
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = MANIFEST.with_suffix('.json.part')
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    tmp.replace(MANIFEST)


def error_text(exc):
    # Request exceptions can contain signed download URLs; keep those out of Git.
    if isinstance(exc, requests.RequestException):
        status = exc.response.status_code if exc.response is not None else None
        return f'{type(exc).__name__}' + (f' (HTTP {status})' if status else '')
    return f'{type(exc).__name__}: {exc}'


def main():
    if not re.fullmatch(r'[A-Za-z0-9_]+', TG_CHANNEL):
        raise ValueError('Invalid TELEGRAM_CHANNEL')
    manifest = json.loads(MANIFEST.read_text(encoding='utf-8')) if MANIFEST.exists() else {}
    report = {'started_at': datetime.now(timezone.utc).isoformat(), 'telegram_ok': False,
              'posts_scanned': 0, 'sources': [], 'downloaded': [], 'reused': 0, 'errors': []}
    older = {int(p['id']): dict(p, id=int(p['id'])) for p in manifest.get('posts', [])}
    try:
        posts = collect_posts(page_limit=int(os.getenv('TELEGRAM_PAGE_LIMIT', '14')),
                              since_id=max(older, default=None))
        report.update(telegram_ok=True, posts_scanned=len(posts))
        older.update({p['id']: p for p in posts})
        print(f'Found {len(posts)} Telegram posts')
    except Exception as exc:
        report['errors'].append('Telegram: ' + error_text(exc))
    manifest['posts'] = sorted(older.values(), key=lambda p: p['id'])
    urls = []
    candidates = [u for p in reversed(manifest['posts']) for u in p['yandex_links']]
    candidates += [FALLBACK] + manifest.get('sources', [])
    for candidate in candidates:
        url = disk_link(candidate)
        if url and url not in urls:
            urls.append(url)
    manifest['sources'] = urls
    known = {row['fingerprint']: row for row in manifest.get('tracks', [])}
    today = datetime.now(timezone.utc).date().isoformat()
    for url in urls:
        source = {'url': url, 'ok': False}
        report['sources'].append(source)
        try:
            entries = list(public_items(url))
            source.update(files=len(entries), ok=True)
            print(f'{url}: {len(entries)} files')
        except Exception as exc:
            source['error'] = error_text(exc)
            report['errors'].append(f'{url}: {source["error"]}')
            continue
        for item in entries:
            if not item['name'].lower().endswith(EXTENSIONS):
                continue
            signature = item.get('md5') or item.get('sha256') or f'{item.get("size", "")}-{item.get("modified", "")}'
            fingerprint = hashlib.sha256((url + '\0' + item['path'] + '\0' + signature).encode()).hexdigest()
            row = known.get(fingerprint)
            try:
                if row:
                    try:
                        hashes = verify_file(archive_path(row['file']), item)
                    except (OSError, ValueError, ET.ParseError):
                        pass  # Missing/corrupt local copy: fetch it again.
                    else:
                        row.update(hashes)
                        report['reused'] += 1
                        continue
                suffix = Path(item['name']).suffix.lower()
                dest = archive_path(row['file']) if row else FILES / f'{today}__{safe_part(Path(item["name"]).stem)}__{fingerprint[:10]}{suffix}'
                hashes = download_file(url, item, dest)
                known[fingerprint] = {
                    'fingerprint': fingerprint, 'name': item['name'],
                    'file': dest.relative_to(BASE).as_posix(),
                    'first_seen': row['first_seen'] if row else today,
                    'downloaded_at': datetime.now(timezone.utc).isoformat(),
                    'size': dest.stat().st_size, 'source': url, 'source_path': item['path'], **hashes,
                }
                report['downloaded'].append(known[fingerprint]['file'])
                print(f'  Saved {dest.name}')
            except Exception as exc:
                source['ok'] = False
                report['errors'].append(f'{url} / {item["name"]}: {error_text(exc)}')
    if not urls:
        report['errors'].append('No valid Yandex sources')
    manifest['tracks'] = sorted(known.values(), key=lambda row: (row['first_seen'], row['name']), reverse=True)
    manifest['last_checked'] = datetime.now(timezone.utc).isoformat()
    report['finished_at'] = manifest['last_checked']
    report['ok'] = not report['errors']
    manifest['last_run'] = report
    if report['ok']:
        manifest['last_success'] = manifest['last_checked']
    write_manifest(manifest)
    for error in report['errors']:
        print('ERROR: ' + error)
    print(f'New files: {len(report["downloaded"])}; verified existing: {report["reused"]}; total archived: {len(known)}')
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
