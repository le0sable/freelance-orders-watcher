"""Сборщик заказов с фриланс-бирж в SQLite для анализа рынка.

Kwork — вся лента со всеми страницами и полными полями (бюджет, срок,
отклики, просмотры, % найма у заказчика). FL.ru и Freelance.ru — через
источники в sources/ (взяты из github.com/IsWake77/FreelanceParser).

Каждый запуск дописывает новые заказы и снимок откликов/просмотров для уже
известных, так что со временем видно, как быстро растёт конкуренция.

    python3 collect.py            # один проход
    python3 collect.py --full     # пройти всю ленту Kwork (52 стр. × 12 с ≈ 12 мин)
    python3 collect.py --no-kwork # без Kwork

Kwork отдаёт 403 при частых запросах, поэтому между страницами 12 с,
а после 403 пауза 2 мин и одна повторная попытка.
"""
import argparse
import datetime
import json
import os
import re
import sqlite3
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
# python.org-сборка Python на macOS не видит системные сертификаты
if sys.platform == 'darwin' and os.path.exists('/etc/ssl/cert.pem'):
    os.environ.setdefault('SSL_CERT_FILE', '/etc/ssl/cert.pem')

from sources.base import HttpClient, HttpError  # noqa: E402
from sources import flru, freelanceru  # noqa: E402

DB = os.path.join(HERE, 'market.db')
KWORK_PAUSE = 12
KWORK_BACKOFF = 120  # после 403 ждём и пробуем ещё раз

SCHEMA = """
create table if not exists orders (
    source text, id text, url text, title text, description text,
    category text, price real, price_max real, days integer,
    created text, expires text, buyer_orders integer, buyer_hired_pct integer,
    responses integer, views integer, first_seen text, last_seen text,
    primary key (source, id)
);
create table if not exists snapshots (
    source text, id text, ts text, responses integer, views integer
);
"""


def now():
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def to_int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


CATS_CACHE = os.path.join(HERE, 'kwork_categories.json')


def kwork_categories(client):
    """id подрубрики -> название (из HTML страницы проектов, кэш на неделю)."""
    if os.path.exists(CATS_CACHE) and time.time() - os.path.getmtime(CATS_CACHE) < 7 * 86400:
        with open(CATS_CACHE, encoding='utf-8') as f:
            return json.load(f)
    try:
        html = client.text('https://kwork.ru/projects')
    except HttpError:
        return {}
    pairs = re.findall(r'\{"CATID":"(\d+)","id":"\d+","name":"([^"]+)"', html)
    cats = {k: json.loads(f'"{v}"') for k, v in pairs}
    if cats:
        with open(CATS_CACHE, 'w', encoding='utf-8') as f:
            json.dump(cats, f, ensure_ascii=False)
    return cats


def known_ids(source):
    if not os.path.exists(DB):
        return set()
    con = sqlite3.connect(DB)
    ids = {r[0] for r in con.execute('select id from orders where source=?', (source,))}
    con.close()
    return ids


def fetch_kwork(log, full=False):
    """Лента отсортирована от новых к старым: без --full идём, пока на странице
    есть незнакомые заказы (плюс 2 страницы для снимков откликов)."""
    known = set() if full else known_ids('kwork')
    stale_pages = 0
    client = HttpClient(retries=2, pause=10)
    cats = kwork_categories(client)
    headers = {
        'X-Requested-With': 'XMLHttpRequest',
        'Referer': 'https://kwork.ru/projects',
        'Origin': 'https://kwork.ru',
        'Accept': 'application/json, text/plain, */*',
    }
    orders, page, last, retried = [], 1, 1, False
    while page <= last:
        boundary = '----' + uuid.uuid4().hex
        try:
            data = client.json('https://kwork.ru/projects', method='POST',
                               as_multipart=(boundary, [('page', str(page))]),
                               headers=headers)['data']
        except (HttpError, KeyError, ValueError) as e:
            if retried:
                log(f'  kwork стр. {page}: {e}, останавливаюсь')
                break
            log(f'  kwork стр. {page}: {e}, жду {KWORK_BACKOFF} с')
            retried = True
            time.sleep(KWORK_BACKOFF)
            continue
        retried = False
        last = (data.get('pagination') or {}).get('last_page') or page
        for w in data.get('wants') or []:
            buyer = ((w.get('user') or {}).get('data')) or {}
            orders.append({
                'source': 'kwork', 'id': str(w['id']),
                'url': f"https://kwork.ru/projects/{w['id']}",
                'title': w.get('name'), 'description': w.get('description'),
                'category': cats.get(str(w.get('category_id')), w.get('category_id')),
                'price': to_int(w.get('priceLimit')),
                'price_max': to_int(w.get('possiblePriceLimit')),
                'days': to_int(w.get('max_days')),
                'created': w.get('date_create'), 'expires': w.get('date_expire'),
                'buyer_orders': to_int(buyer.get('wants_count')),
                'buyer_hired_pct': to_int(buyer.get('wants_hired_percent')),
                'responses': to_int(w.get('kwork_count')),
                'views': to_int(w.get('views_dirty')),
            })
        page += 1
        if known and all(str(w['id']) in known for w in data.get('wants') or []):
            stale_pages += 1
            if stale_pages > 2:
                break
        time.sleep(KWORK_PAUSE)
    log(f'  kwork: {len(orders)} заказов, пройдено страниц {page - 1} из {last}')
    return orders


def fetch_flru(log):
    # вся лента + программирование, дизайн, сайты (в каждой RSS не больше 60 штук)
    raw = _flru_all() + flru.fetch({'categories': ['5', '3', '2'], 'max_items': 500})
    orders = []
    for o in raw:
        m = re.search(r'/projects/(\d+)', o['url'])
        if m:
            orders.append({**o, 'id': m.group(1), 'price': None})
    log(f'  fl.ru: {len(orders)} записей')
    return orders


def _flru_all():
    """RSS без категории — последние заказы по всем рубрикам."""
    import xml.etree.ElementTree as ET
    import html as htmllib
    try:
        _, body = HttpClient(retries=1).request('https://www.fl.ru/rss/all.xml')
        root = ET.fromstring(body)
    except (HttpError, ET.ParseError):
        return []
    return [{
        'source': 'fl.ru',
        'title': htmllib.unescape(i.findtext('title') or '').strip(),
        'url': (i.findtext('link') or '').strip(),
        'description': re.sub(r'\s+', ' ', htmllib.unescape(i.findtext('description') or '')),
        'category': htmllib.unescape(i.findtext('category') or '').strip(),
        'date': (i.findtext('pubDate') or '').strip(),
    } for i in root.iter('item')]


def fetch_freelanceru(log):
    orders = []
    for o in freelanceru.fetch({'categories': ['4', '724'], 'max_items': 200}):
        m = re.search(r'/task/view/(\d+)', o['url'])
        if m:
            price = re.sub(r'[^\d]', '', o.get('price') or '')
            orders.append({**o, 'id': m.group(1), 'price': int(price) if price else None})
    log(f'  freelance.ru: {len(orders)} заказов')
    return orders


def save(orders):
    """Пишет заказы в базу. Возвращает (список новых заказов, всего в базе)."""
    ts = now()
    con = sqlite3.connect(DB)
    con.executescript(SCHEMA)
    new = []
    for o in orders:
        cur = con.execute(
            'update orders set last_seen=?, responses=coalesce(?, responses), '
            'views=coalesce(?, views) where source=? and id=?',
            (ts, o.get('responses'), o.get('views'), o['source'], o['id']))
        if cur.rowcount == 0:
            new.append(o)
            con.execute(
                'insert into orders values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (o['source'], o['id'], o['url'], o.get('title'), o.get('description'),
                 o.get('category'), o.get('price'), o.get('price_max'), o.get('days'),
                 o.get('created') or o.get('date'), o.get('expires'),
                 o.get('buyer_orders'), o.get('buyer_hired_pct'),
                 o.get('responses'), o.get('views'), ts, ts))
        if o.get('responses') is not None or o.get('views') is not None:
            con.execute('insert into snapshots values (?,?,?,?,?)',
                        (o['source'], o['id'], ts, o.get('responses'), o.get('views')))
    con.commit()
    total = con.execute('select count(*) from orders').fetchone()[0]
    con.close()
    return new, total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--no-kwork', action='store_true')
    ap.add_argument('--full', action='store_true', help='пройти всю ленту Kwork (~12 мин)')
    args = ap.parse_args()
    log = lambda s: print(s, flush=True)  # noqa: E731
    log(f'=== {now()} ===')
    for name, fn in (('fl.ru', fetch_flru), ('freelance.ru', fetch_freelanceru),
                     ('kwork', None if args.no_kwork else lambda l: fetch_kwork(l, args.full))):
        if fn is None:
            continue
        try:
            orders = fn(log)
        except Exception as e:  # один упавший источник не должен ронять остальные
            log(f'  {name}: ошибка {e!r}')
            continue
        # сохраняем после каждого источника, чтобы прерванный запуск не терял уже собранное
        new, total = save(orders)
        log(f'  {name}: новых {len(new)}, всего в базе {total}')


if __name__ == '__main__':
    main()
