"""Сборщик заказов с фриланс-бирж в SQLite для анализа рынка.

Kwork — внутренний JSON ленты с полными полями (бюджет, срок, отклики, просмотры,
% найма у заказчика). FL.ru — sources/flru.py, Freelance.ru — sources/freelanceru.py,
pchel.net, freelancejob.ru и Telegram-каналы — sources/extra.py.
Список Telegram-каналов — в settings.json.

Каждый запуск дописывает новые заказы, а в snapshots — число откликов, если оно
изменилось, так что со временем видно, как быстро растёт конкуренция.
Все отметки времени (first_seen, last_seen, snapshots.ts) — по Москве, как и created у Kwork.

    python3 collect.py            # один проход
    python3 collect.py --full     # пройти всю ленту Kwork (~40 стр. × 12 с ≈ 8–10 мин)
    python3 collect.py --no-kwork # без Kwork

Kwork отдаёт 403 при частых запросах, поэтому между страницами 12 с,
а после 403 пауза 2 мин и одна повторная попытка. Обычный проход читает ленту,
пока не встретит страницу без новых заказов (чаще всего это одна страница);
раз в 30 минут — на 2 страницы глубже, чтобы обновить отклики у заказов постарше.
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
from sources import extra, flru, freelanceru  # noqa: E402

DB = os.path.join(HERE, 'market.db')
SETTINGS = os.path.join(HERE, 'settings.json')
KWORK_PAUSE = 12
KWORK_BACKOFF = 120  # после 403 ждём и пробуем ещё раз
KWORK_DEEP_EVERY = 30 * 60  # как часто проход по Kwork идёт на 2 страницы глубже
# Площадки, где новые заказы появляются раз в несколько дней: опрашиваем их не каждый проход
SLOW_SOURCES_EVERY = 6 * 3600
MSK = datetime.timezone(datetime.timedelta(hours=3))

SCHEMA = """
create table if not exists orders (
    source text, id text, url text, title text, description text,
    category text, price real, price_max real, days integer,
    created text, expires text, buyer_orders integer, buyer_hired_pct integer,
    responses integer, views integer, first_seen text, last_seen text,
    buyer_id text, buyer_active integer, buyer_purchases integer,
    primary key (source, id)
);
create table if not exists snapshots (
    source text, id text, ts text, responses integer, views integer
);
create table if not exists feedback (
    source text, id text, label integer, ts text, primary key (source, id)
);
create table if not exists kv (key text primary key, value text);
"""


# Колонки, добавленные после первой версии базы: в старой базе их создаёт ensure_schema
ADDED_COLUMNS = {'buyer_id': 'text', 'buyer_active': 'integer', 'buyer_purchases': 'integer'}
ORDER_COLUMNS = ('source', 'id', 'url', 'title', 'description', 'category', 'price', 'price_max', 'days',
                 'created', 'expires', 'buyer_orders', 'buyer_hired_pct', 'responses', 'views',
                 'first_seen', 'last_seen', *ADDED_COLUMNS)
# Сведения о заказчике, которые меняются со временем: обновляем при каждой встрече заказа
BUYER_FIELDS = ('buyer_orders', 'buyer_hired_pct', 'buyer_active', 'buyer_purchases')


def ensure_schema(con):
    con.executescript(SCHEMA)
    have = {r[1] for r in con.execute('pragma table_info(orders)')}
    for name, kind in ADDED_COLUMNS.items():
        if name not in have:
            con.execute(f'alter table orders add column {name} {kind}')
    if not con.execute("select 1 from kv where key = 'migrated_0923'").fetchone():
        con.commit()
        con.execute('begin immediate')  # бот мог начать ту же миграцию из соседнего потока
        if con.execute("select 1 from kv where key = 'migrated_0923'").fetchone():
            con.commit()
        else:
            migrate_0923(con)


# Запуски на GitHub Actions до 23.09 писали время в UTC: всё с 22.09 10:50, кроме локального
# полного прохода 22.09 13:07–13:20 МСК (в Actions с 12:11 до 15:21 UTC был перерыв)
_WAS_UTC = "{c} >= '2026-09-22 10:50' and {c} not between '2026-09-22 12:30' and '2026-09-22 15:00'"
_DUPLICATE_SNAPSHOTS = """
delete from snapshots where rowid in (
    select rowid from (
        select rowid, responses, lag(responses) over w prev, row_number() over w n
        from snapshots window w as (partition by source, id order by ts))
    where n > 1 and responses is prev)
"""


def migrate_0923(con):
    """Разовая чистка базы, накопленной до 23.09.2026: время из UTC в Москву, снимки без
    изменений откликов, бюджет FL.ru из заголовка у старых заказов. Вызывается внутри транзакции."""
    if con.execute('select count(*) from orders').fetchone()[0]:
        for table, col in (('orders', 'first_seen'), ('orders', 'last_seen'),
                           ('snapshots', 'ts'), ('feedback', 'ts')):
            con.execute(f"update {table} set {col} = datetime({col}, '+3 hours') "
                        f"where {_WAS_UTC.format(c=col)}")
        con.execute(_DUPLICATE_SNAPSHOTS)
        rows = con.execute("select id, title from orders where source = 'fl.ru' and price is null")
        con.executemany("update orders set price = ? where source = 'fl.ru' and id = ?",
                        [(p, i) for i, t in rows.fetchall() if (p := flru.title_budget(t))])
    con.execute("insert or replace into kv values ('migrated_0923', ?)", (now(),))
    con.commit()
    try:
        con.execute('vacuum')
    except sqlite3.OperationalError:  # база занята соседним потоком — место освободится позже
        pass


def load_settings():
    with open(SETTINGS, encoding='utf-8') as f:
        return json.load(f)


def save_settings(settings):
    with open(SETTINGS, 'w', encoding='utf-8') as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)
        f.write('\n')


def now():
    """Время по Москве независимо от часового пояса машины (на GitHub Actions там UTC)."""
    return datetime.datetime.now(MSK).strftime('%Y-%m-%d %H:%M:%S')


def seconds_since(ts):
    """Сколько секунд прошло с отметки now()."""
    return (datetime.datetime.now(MSK).replace(tzinfo=None) - datetime.datetime.fromisoformat(ts)).total_seconds()


def query(sql, *args):
    """Один запрос к базе. None, если базы ещё нет: её создаёт save()."""
    if not os.path.exists(DB):
        return None
    con = sqlite3.connect(DB, timeout=60)
    try:
        ensure_schema(con)
        rows = con.execute(sql, args).fetchall()
        con.commit()
        return rows
    finally:
        con.close()


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
    return {r[0] for r in query('select id from orders where source = ?', source) or []}


def kwork_purchases(user):
    """Сколько покупок у заказчика в каталоге Kwork (нижняя граница по значкам), 0 — ни одного значка."""
    n = 0
    for b in user.get('badges') or []:
        badge = b.get('badge') or {}
        m = re.search(r'(?:более|не менее) (\d+) покуп|(\d+) покупки', f"{badge.get('title')} {badge.get('description')}")
        if m:
            n = max(n, int(m.group(1) or m.group(2)))
    return n


def kwork_deep_pass():
    """Пора ли пройти Kwork на 2 страницы глубже (раз в KWORK_DEEP_EVERY). Отмечает проход в kv."""
    last = query("select value from kv where key = 'kwork_deep_at'")
    if last and seconds_since(last[0][0]) < KWORK_DEEP_EVERY:
        return False
    query("insert or replace into kv values ('kwork_deep_at', ?)", now())
    return True


def fetch_kwork(log, full=False):
    """Лента отсортирована от новых к старым: без --full идём, пока на странице есть незнакомые
    заказы, а раз в KWORK_DEEP_EVERY — ещё 2 страницы, чтобы обновить отклики у заказов постарше."""
    known = set() if full else known_ids('kwork')
    extra_pages = 2 if known and kwork_deep_pass() else 0
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
            user = w.get('user') or {}
            buyer = user.get('data') or {}
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
                'buyer_id': str(user['USERID']) if user.get('USERID') else None,
                'buyer_active': to_int(w.get('getWantsActiveCount')),
                'buyer_purchases': kwork_purchases(user),
                'responses': to_int(w.get('kwork_count')),
                'views': to_int(w.get('views_dirty')),
            })
        page += 1
        if known and all(str(w['id']) in known for w in data.get('wants') or []):
            stale_pages += 1
            if stale_pages > extra_pages:
                break
        time.sleep(KWORK_PAUSE)
    log(f'  kwork: {len(orders)} заказов, пройдено страниц {page - 1} из {last}')
    return orders


def fetch_flru(log):
    # вся лента + программирование, дизайн, сайты (в каждой RSS не больше 60 штук);
    # заказ из нескольких лент save() запишет один раз
    orders = flru.fetch({'categories': ['', '5', '3', '2']})
    log(f'  fl.ru: {len(orders)} записей')
    return orders


def fetch_freelanceru(log):
    orders = freelanceru.fetch({'categories': ['4', '724'], 'max_items': 200})
    log(f'  freelance.ru: {len(orders)} заказов')
    return orders


def _simple(name, fn, every=0):
    """every — опрашивать не чаще раза в столько секунд (по last_seen площадки в базе)."""
    def run(log):
        if every:
            last = (query('select max(last_seen) from orders where source = ?', name) or [[None]])[0][0]
            if last and seconds_since(last) < every:
                return []
        orders = fn()
        log(f'  {name}: {len(orders)} заказов')
        return orders
    return run


def all_sources(settings, full=False, kwork=True):
    """[(название, функция(log) -> заказы)]; Kwork последним — он самый медленный."""
    sources = [
        ('fl.ru', fetch_flru),
        ('freelance.ru', fetch_freelanceru),
        ('freelancejob.ru', _simple('freelancejob.ru', extra.fetch_freelancejob, SLOW_SOURCES_EVERY)),
        ('pchel.net', _simple('pchel.net', extra.fetch_pchel, SLOW_SOURCES_EVERY)),
        ('telegram', _simple('telegram', lambda: extra.fetch_telegram(settings.get('telegram_channels', [])))),
    ]
    if kwork:
        sources.append(('kwork', lambda log: fetch_kwork(log, full)))
    return sources


def save(orders):
    """Пишет заказы в базу. Возвращает (список новых заказов, всего в базе).
    В snapshots попадает новый заказ и заказ, у которого изменилось число откликов."""
    ts = now()
    con = sqlite3.connect(DB, timeout=60)  # бот в notify.py пишет оценки из соседнего потока
    ensure_schema(con)
    new = []
    buyer_set = ', '.join(f'{c}=coalesce(?, {c})' for c in BUYER_FIELDS)
    for o in orders:
        prev = con.execute('select responses from orders where source=? and id=?',
                           (o['source'], o['id'])).fetchone()
        if prev:
            con.execute(
                f'update orders set last_seen=?, responses=coalesce(?, responses), '
                f'views=coalesce(?, views), {buyer_set} where source=? and id=?',
                (ts, o.get('responses'), o.get('views'), *(o.get(c) for c in BUYER_FIELDS), o['source'], o['id']))
        else:
            new.append(o)
            row = {**o, 'created': o.get('created') or o.get('date'), 'first_seen': ts, 'last_seen': ts}
            con.execute(f"insert into orders ({', '.join(ORDER_COLUMNS)}) "
                        f"values ({', '.join('?' * len(ORDER_COLUMNS))})",
                        [row.get(c) for c in ORDER_COLUMNS])
        if o.get('responses') is not None and (not prev or o['responses'] != prev[0]):
            con.execute('insert into snapshots values (?,?,?,?,?)',
                        (o['source'], o['id'], ts, o['responses'], o.get('views')))
    con.commit()
    total = con.execute('select count(*) from orders').fetchone()[0]
    con.close()
    return new, total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--no-kwork', action='store_true')
    ap.add_argument('--full', action='store_true', help='пройти всю ленту Kwork (~8–10 мин)')
    args = ap.parse_args()
    log = lambda s: print(s, flush=True)  # noqa: E731
    log(f'=== {now()} ===')
    for name, fn in all_sources(load_settings(), full=args.full, kwork=not args.no_kwork):
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
