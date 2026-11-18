#!/usr/bin/env python3
"""Build a static catalog suitable for GitHub Pages."""
import html
import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from sync import verify_file

ROOT = Path(__file__).resolve().parent
SITE = ROOT / 'site'
MANIFEST = ROOT / 'data' / 'manifest.json'


def format_moscow(value):
    if not value:
        return 'не выполнялась'
    checked = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=timezone.utc)
    return checked.astimezone(timezone(timedelta(hours=3))).strftime('%d.%m.%Y в %H:%M МСК')


def main():
    manifest = json.loads(MANIFEST.read_text(encoding='utf-8'))
    SITE.mkdir(exist_ok=True)
    tracks = manifest.get('tracks', [])
    posts = [p for p in sorted(manifest.get('posts', []), key=lambda p: int(p['id']), reverse=True) if p.get('event_date')]
    rows = []
    for track in tracks:
        source = (ROOT / track['file']).resolve()
        if not source.is_relative_to((ROOT / 'data' / 'tracks').resolve()):
            raise ValueError('Track path is outside data/tracks')
        verify_file(source, track)
        target = SITE / 'tracks' / source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        link = 'tracks/' + quote(source.name)
        rows.append('<tr><td><a download href="'+link+'">'+html.escape(track['name'])+'</a></td>'
                    '<td>'+html.escape(track['first_seen'])+'</td>'
                    '<td>'+str(round(track['size']/1024, 1))+' КБ</td></tr>')
    announcements = []
    for post in posts[:40]:
        text = post['text']
        location = text.split('Локация:',1)[-1].split('Место сбора',1)[0].strip() if 'Локация:' in text else ''
        title = (post.get('event_date') or post.get('published_at')[:10])+' · '+location[:70]
        announcements.append('<li><a href="'+html.escape(post['url'], quote=True)+'">'+html.escape(title)+'</a></li>')
    last_run = manifest.get('last_run', {})
    status = ('Проверка завершена успешно' if last_run.get('ok') else
              'Последняя проверка завершилась с ошибками; архив может быть неполным')
    if not last_run:
        status = 'Результат последней проверки неизвестен'
    page = '''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Архив треков · Дикие Сочи</title><style>
    body{font:16px/1.55 system-ui,sans-serif;max-width:900px;margin:30px auto;padding:0 18px;color:#1b2731;background:#fafbfc}
    h1{font-size:2rem}a{color:#075cb6}table{width:100%;border-collapse:collapse;background:white}td,th{padding:12px 8px;border-bottom:1px solid #ddd;text-align:left}tr:hover{background:#eef6ff}small{color:#586574}li{padding:4px 0}.card{background:#fff;border:1px solid #e5eaf0;border-radius:12px;padding:18px;margin-bottom:20px}input{box-sizing:border-box;width:100%;padding:12px;border:1px solid #ccd;border-radius:8px;font-size:1rem}
    </style></head><body><h1>🥾 Архив треков · Дикие Сочи</h1><p>Сохраняем опубликованные маршруты, даже когда организаторы удаляют их из исходной папки.</p><div class="card"><strong>Найдено маршрутов: '''+str(len(tracks))+'''</strong><p><small>Последняя проверка: '''+html.escape(manifest.get('last_checked') or 'не выполнялась')+''' · Источник: <a href="https://t.me/dikiye_sochi">@dikiye_sochi</a></small></p></div><h2>Скачать треки</h2><input id="search" placeholder="Поиск маршрута по названию…" aria-label="Поиск маршрута"><table id="tracks"><thead><tr><th>Название</th><th>В архиве с</th><th>Размер</th></tr></thead><tbody>'''+''.join(rows)+'''</tbody></table><h2>Анонсы из Telegram</h2><p><small>Даты и описания приведены отдельно от файлов: нельзя гарантировать, что файл в папке соответствует последнему анонсу.</small></p><ul>'''+''.join(announcements)+'''</ul><script>document.querySelector('#search').addEventListener('input',e=>{const q=e.target.value.toLowerCase();document.querySelectorAll('#tracks tbody tr').forEach(r=>r.hidden=!r.textContent.toLowerCase().includes(q))})</script></body></html>'''
    raw_checked = html.escape(manifest.get('last_checked') or 'не выполнялась')
    page = page.replace('Последняя проверка: ' + raw_checked,
                        'Последнее обновление: ' + html.escape(format_moscow(manifest.get('last_checked'))))
    page = page.replace('<h2>Скачать треки</h2>', '<p role="status">' + status + '</p><h2>Скачать треки</h2>')
    (SITE / 'index.html').write_text(page, encoding='utf-8')
    (SITE / '.nojekyll').touch()
    print('Built site with',len(tracks),'tracks and',len(posts),'announcements')

if __name__ == '__main__':
    main()
