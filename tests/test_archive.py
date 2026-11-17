import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

import build_site
import sync

URL = 'https://disk.yandex.ru/d/msmGZTv0QtuLhA'
NEW_URL = 'https://disk.yandex.ru/d/new_share'
GPX = b'<?xml version="1.0"?><gpx xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg><trkpt lat="43" lon="39"/></trkseg></trk></gpx>'


def entry(payload=GPX, name='Test.gpx', path='/Test.gpx'):
    return {'type': 'file', 'name': name, 'path': path, 'size': len(payload),
            'md5': hashlib.md5(payload).hexdigest(), 'sha256': hashlib.sha256(payload).hexdigest()}


def message(number, body='', text=True):
    content = f'<div class="tgme_widget_message_text">{body}</div>' if text else ''
    return f'<div class="tgme_widget_message" data-post="dikiye_sochi/{number}">{content}<time datetime="2026-11-17T12:00:00Z"></time></div>'


def response(text='', payload=b'', status=200, failure=None):
    result = Mock(status_code=status, text=text)
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    result.iter_content.return_value = iter([payload])
    if failure:
        result.raise_for_status.side_effect = failure
    return result


class TelegramTests(unittest.TestCase):
    def test_numeric_order_new_share_text_and_alias_deduplication(self):
        markup = message(99, URL) + message(140, f'15.11.2026 <a href="https://yadi.sk/d/new_share?utm_source=tg">скачать</a> {NEW_URL}.')
        posts, ids = sync.parse_posts(markup)
        self.assertEqual(sorted(posts), [99, 140])
        self.assertEqual(posts[140]['yandex_links'], [NEW_URL])
        self.assertEqual(posts[140]['event_date'], '15.11.2026')
        self.assertEqual(ids, [99, 140])

    def test_captionless_posts_still_advance_pagination(self):
        with patch.object(sync, 'get', side_effect=[response(message(140, text=False)), response(message(100, NEW_URL))]) as get:
            posts = sync.collect_posts(since_id=100)
        self.assertEqual(posts[0]['yandex_links'], [NEW_URL])
        self.assertEqual(get.call_args_list[1].kwargs['params'], {'before': 140})

    def test_empty_or_nonprogressing_telegram_is_not_success(self):
        with patch.object(sync, 'get', return_value=response('<html>captcha</html>')):
            with self.assertRaisesRegex(RuntimeError, 'no message'):
                sync.collect_posts()
        with patch.object(sync, 'get', side_effect=[response(message(140)), response(message(140))]):
            with self.assertRaisesRegex(RuntimeError, 'no progress'):
                sync.collect_posts()

    def test_scan_limit_cannot_silently_skip_new_posts(self):
        with patch.object(sync, 'get', return_value=response(message(140))):
            with self.assertRaisesRegex(RuntimeError, 'scan limit'):
                sync.collect_posts(page_limit=1, since_id=100)

    def test_canonical_links_reject_lookalikes_and_credentials(self):
        self.assertEqual(sync.disk_link('https://disk.360.yandex.ru/d/abc/#x'), 'https://disk.yandex.ru/d/abc')
        for link in ['https://disk.yandex.ru.evil.org/d/abc', 'https://user@disk.yandex.ru/d/abc',
                     'https://disk.yandex.ru/', 'file:///d/abc']:
            self.assertIsNone(sync.disk_link(link))


class YandexTests(unittest.TestCase):
    def test_pagination_uses_total_and_recurse_nested_folders(self):
        first = {'type': 'dir', '_embedded': {'items': [{'type': 'dir', 'path': '/nested'}], 'total': 2}}
        nested = {'type': 'dir', '_embedded': {'items': [entry(path='/nested/a.gpx')], 'total': 1}}
        second = {'type': 'dir', '_embedded': {'items': [entry(path='/b.gpx')], 'total': 2}}
        snapshots = []
        def listing(url, params):
            snapshots.append(dict(params))
            return [first, nested, second][len(snapshots) - 1]
        with patch.object(sync, 'get_json', side_effect=listing):
            items = list(sync.public_items(URL))
        self.assertEqual([x['path'] for x in items], ['/nested/a.gpx', '/b.gpx'])
        self.assertEqual(snapshots[2]['offset'], 1)
        self.assertEqual(snapshots[1]['path'], '/nested')

    def test_single_file_and_incomplete_listing(self):
        with patch.object(sync, 'get_json', return_value=entry()):
            self.assertEqual(list(sync.public_items(URL)), [entry()])
        broken = {'type': 'dir', '_embedded': {'items': [], 'total': 3}}
        with patch.object(sync, 'get_json', return_value=broken):
            with self.assertRaisesRegex(RuntimeError, 'before total'):
                list(sync.public_items(URL))

    def test_404_is_not_retried_and_503_is_retried(self):
        bad = response(status=404, failure=requests.HTTPError('404'))
        with patch.object(sync.SESSION, 'get', return_value=bad) as get, patch.object(sync.time, 'sleep') as sleep:
            with self.assertRaises(requests.HTTPError):
                sync.get(URL)
            self.assertEqual(get.call_count, 1)
            sleep.assert_not_called()
            bad.close.assert_called_once()
        transient, good = response(status=503), response()
        with patch.object(sync.SESSION, 'get', side_effect=[transient, good]), patch.object(sync.time, 'sleep') as sleep:
            self.assertIs(sync.get(URL), good)
            transient.close.assert_called_once()
            sleep.assert_called_once_with(1)

    def test_no_implicit_oauth_or_basic_auth(self):
        with patch('requests.sessions.get_netrc_auth', return_value=('user', 'secret')):
            prepared = sync.SESSION.prepare_request(requests.Request('GET', URL))
        self.assertNotIn('Authorization', prepared.headers)


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        for name, value in {'BASE': self.root, 'DATA': self.root / 'data', 'FILES': self.root / 'data/tracks',
                            'MANIFEST': self.root / 'data/manifest.json', 'FALLBACK': URL}.items():
            started = patch.object(sync, name, value)
            started.start()
            self.addCleanup(started.stop)

    def download(self, item, payload):
        destination = sync.FILES / 'saved.gpx'
        with patch.object(sync, 'get_json', return_value={'href': 'https://downloader.disk.yandex.ru/file'}), \
                patch.object(sync, 'get', return_value=response(payload=payload)):
            result = sync.download_file(URL, item, destination)
        return destination, result

    def test_download_checks_both_hashes_and_gpx(self):
        path, hashes = self.download(entry(), GPX)
        self.assertEqual(path.read_bytes(), GPX)
        self.assertEqual(hashes['sha256'], hashlib.sha256(GPX).hexdigest())

    def test_wrong_hash_wrong_size_and_html_are_never_archived(self):
        for item, payload in [(dict(entry(), md5='0' * 32), GPX),
                              (dict(entry(), sha256='0' * 64), GPX),
                              (dict(entry(), size=1), GPX),
                              (entry(b'<html>captcha</html>'), b'<html>captcha</html>')]:
            with self.assertRaises(ValueError):
                self.download(item, payload)
            self.assertFalse((sync.FILES / 'saved.gpx').exists())
            self.assertFalse((sync.FILES / 'saved.gpx.part').exists())

    def test_repeat_run_and_corrupt_local_copy(self):
        with patch.object(sync, 'collect_posts', return_value=[]), patch.object(sync, 'public_items', return_value=[entry()]), \
                patch.object(sync, 'download_file', wraps=sync.download_file) as download, \
                patch.object(sync, 'get_json', return_value={'href': 'https://downloader.disk.yandex.ru/file'}), \
                patch.object(sync, 'get', side_effect=lambda *a, **k: response(payload=GPX)), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sync.main(), 0)
            first = json.loads(sync.MANIFEST.read_text(encoding='utf-8'))
            self.assertEqual(sync.main(), 0)
            self.assertEqual(download.call_count, 1)
            sync.archive_path(first['tracks'][0]['file']).write_bytes(b'broken')
            self.assertEqual(sync.main(), 0)
            self.assertEqual(download.call_count, 2)
        repaired = json.loads(sync.MANIFEST.read_text(encoding='utf-8'))
        self.assertEqual(len(repaired['tracks']), 1)
        self.assertEqual(repaired['tracks'][0]['file'], first['tracks'][0]['file'])

    def test_deleted_remote_files_stay_archived(self):
        path, hashes = self.download(entry(), GPX)
        row = {'fingerprint': 'x', 'file': path.relative_to(self.root).as_posix(),
               'name': 'Test.gpx', 'first_seen': '2026-11-17', 'size': len(GPX), **hashes}
        sync.write_manifest({'tracks': [row]})
        with patch.object(sync, 'collect_posts', return_value=[]), patch.object(sync, 'public_items', return_value=[]), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sync.main(), 0)
        manifest = json.loads(sync.MANIFEST.read_text(encoding='utf-8'))
        self.assertTrue(sync.archive_path(manifest['tracks'][0]['file']).exists())

    def test_string_ids_migrate_and_newer_source_has_priority(self):
        sync.write_manifest({'posts': [{'id': '99', 'yandex_links': [URL]}], 'sources': [URL]})
        with patch.object(sync, 'collect_posts', return_value=[{'id': 140, 'yandex_links': [NEW_URL]}]), \
                patch.object(sync, 'public_items', return_value=[]), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sync.main(), 0)
        manifest = json.loads(sync.MANIFEST.read_text(encoding='utf-8'))
        self.assertEqual([p['id'] for p in manifest['posts']], [99, 140])
        self.assertEqual(manifest['sources'], [NEW_URL, URL])

    def test_partial_failure_saves_successes_and_returns_failure(self):
        good, bad = entry(), entry(name='bad.gpx', path='/bad.gpx')
        def save(url, item, destination):
            if item['name'] == 'bad.gpx':
                raise ValueError('checksum mismatch')
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(GPX)
            return sync.verify_file(destination, item)
        with patch.object(sync, 'collect_posts', side_effect=RuntimeError('Telegram unavailable')), \
                patch.object(sync, 'public_items', return_value=[good, bad]), \
                patch.object(sync, 'download_file', side_effect=save), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sync.main(), 1)
        manifest = json.loads(sync.MANIFEST.read_text(encoding='utf-8'))
        self.assertEqual(len(manifest['tracks']), 1)
        self.assertFalse(manifest['last_run']['ok'])
        self.assertEqual(len(manifest['last_run']['errors']), 2)
        self.assertNotIn('last_success', manifest)

    def test_site_links_work_under_project_prefix(self):
        path, hashes = self.download(entry(), GPX)
        sync.write_manifest({'tracks': [{'name': 'Test.gpx', 'file': path.relative_to(self.root).as_posix(),
                                        'first_seen': '2026-11-17', 'size': len(GPX), **hashes}], 'last_run': {'ok': True}})
        with patch.multiple(build_site, ROOT=self.root, SITE=self.root / 'site', MANIFEST=sync.MANIFEST), contextlib.redirect_stdout(io.StringIO()):
            build_site.main()
        from bs4 import BeautifulSoup
        from urllib.parse import unquote
        soup = BeautifulSoup((self.root / 'site/index.html').read_text(encoding='utf-8'), 'html.parser')
        link = soup.select_one('a[download]')['href']
        self.assertFalse(link.startswith('/'))
        self.assertTrue((self.root / 'site' / unquote(link)).is_file())
        self.assertIn('успешно', soup.select_one('[role=status]').text)

    def test_changed_remote_content_keeps_both_versions(self):
        updated = GPX.replace(b'lat="43"', b'lat="44"')
        def save(url, item, destination):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(GPX if item['md5'] == entry()['md5'] else updated)
            return sync.verify_file(destination, item)
        with patch.object(sync, 'collect_posts', return_value=[]), \
                patch.object(sync, 'public_items', side_effect=[[entry()], [entry(updated)]]), \
                patch.object(sync, 'download_file', side_effect=save), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sync.main(), 0)
            self.assertEqual(sync.main(), 0)
        manifest = json.loads(sync.MANIFEST.read_text(encoding='utf-8'))
        self.assertEqual(len(manifest['tracks']), 2)
        self.assertEqual({sync.archive_path(t['file']).read_bytes() for t in manifest['tracks']}, {GPX, updated})

    def test_all_sources_unavailable_preserves_previous_archive_and_success_time(self):
        path, hashes = self.download(entry(), GPX)
        row = {'fingerprint': 'x', 'file': path.relative_to(self.root).as_posix(),
               'name': 'Test.gpx', 'first_seen': '2026-11-17', 'size': len(GPX), **hashes}
        sync.write_manifest({'tracks': [row], 'last_success': 'previous-success'})
        with patch.object(sync, 'collect_posts', return_value=[]), \
                patch.object(sync, 'public_items', side_effect=requests.HTTPError('secret signed URL')), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sync.main(), 1)
        manifest = json.loads(sync.MANIFEST.read_text(encoding='utf-8'))
        self.assertEqual(manifest['last_success'], 'previous-success')
        self.assertEqual(len(manifest['tracks']), 1)
        self.assertFalse(manifest['last_run']['sources'][0]['ok'])
        self.assertNotIn('secret', str(manifest['last_run']))

    def test_manifest_path_cannot_escape_archive(self):
        with self.assertRaises(ValueError):
            sync.archive_path('../outside.gpx')


if __name__ == '__main__':
    unittest.main()
