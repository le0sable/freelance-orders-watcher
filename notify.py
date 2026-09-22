"""Live-уведомления в Telegram о новых заказах в своих нишах.

Каждый запуск: собирает свежие заказы (как collect.py, Kwork — только новые
страницы), пишет их в market.db и шлёт в Telegram те новые, что попали в ниши:
    [Данные] — парсинг, сбор баз, таблицы, обработка/анализ данных
    [3D]     — low-poly, Blender, Unity/Godot, персонажи, риг, текстуры
    [ИИ]     — ИИ-агенты и ассистенты, RAG, подключение API моделей, вайбкодинг
    [Игры]   — моды, игровые серверы, Roblox/Lua-скрипты
    [Приложения] — Flutter/Android/iOS, публикация в Google Play и RuStore
    [Маркетплейсы] — автоматизация для селлеров: API WB/Ozon, карточки, отчёты

    python3 notify.py --setup <токен>  # один раз: сохранить токен и найти chat_id
    python3 notify.py                  # один проход (его запускает LaunchAgent)
    python3 notify.py --test 5         # прислать 5 последних подходящих заказов из базы

Настройки лежат в tg.json: token, chat_id и необязательный proxy — http-прокси
вида "http://127.0.0.1:7890", если api.telegram.org недоступен напрямую
(socks5 стандартная библиотека не умеет).
"""
import argparse
import datetime
import html
import json
import os
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request

import collect

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, 'tg.json')

# Ищем по началу слова. strong — достаточно упоминания где угодно, включая описание;
# weak — слова пошире, их учитываем только в заголовке и рубрике, иначе много шума.
NICHES = {
    'Данные': {
        'strong': [r'парс', r'спарс', r'скрап', r'scrap', r'сбор\w* данн',
                   r'собрат\w* (данн|баз\w* (компан|контакт|сайт|товар)|каталог)'],
        'weak': [r'выгруз', r'excel', r'эксель', r'exel', r'xlsx', r'csv', r'(google|гугл)[- ]?(таблиц|sheets)',
                 r'таблиц', r'баз\w* (данн|контакт|компан)', r'обработ\w* данн', r'анализ\w* данн',
                 r'дашборд', r'dashboard', r'сбор\w* (баз|информ|контакт|товар)', r'баз[аыу]\b'],
    },
    '3D': {
        'strong': [r'blender', r'блендер', r'low[- ]?poly', r'лоу[- ]?поли', r'лоуполи', r'unity', r'юнити',
                   r'unreal', r'\bue\d?\b', r'godot', r'ретопол', r'ретаргет', r'риггинг', r'риг лица',
                   r'psx', r'ps1'],
        'weak': [r'3d', r'3д', r'текстур', r'level design', r'левел', r'ассет', r'моделир', r'моделлинг',
                 r'модел\w* персонаж'],
    },
    # Моды, игровые серверы и скрипты — рядом с 3D, конкуренции почти нет
    'Игры': {
        'strong': [r'roblox', r'роблокс', r'glua', r'gmod', r'garry', r'\bmta\b', r'multitheftauto', r'samp',
                   r'minecraft (плагин|мод|сервер)', r'spigot', r'paper ?mc'],
        'weak': [r'мод\w* (для|под|на)', r'\bмод[ыа]?\b', r'сервер\w* (игр|ark|rust|cs|minecraft|майнкрафт|дискорд)',
                 r'маппер', r'lua\b', r'игр\w* (на|для) (unity|godot|телеграм|браузер)'],
    },
    # Мобильные приложения: Flutter/Android с агентами, публикация в сторы
    'Приложения': {
        'strong': [r'flutter', r'react native', r'kotlin', r'rustore', r'google play', r'app store', r'apk\b'],
        'weak': [r'android', r'андроид', r'\bios\b', r'мобильн\w* приложен', r'приложени\w* (для|на) (android|ios|телефон)'],
        'exclude': [r'скачать', r'скачиван', r'дизайн', r'скриншот', r'aso\b', r'презентац', r'тестиров',
                    r'протестир', r'тестер', r'тесты\b', r'тестов\w*'],
    },
    # Автоматизация для селлеров: API WB/Ozon, массовое заполнение и перенос карточек, отчёты
    'Маркетплейсы': {
        'strong': [r'api (wb|wildberries|ozon|озон|вб)', r'(wb|wildberries|ozon|озон|вб) api'],
        'weak': [r'(карточ\w*|товар\w*).{0,30}(заполн|перенос|выгруз|загруз|импорт|массов)',
                 r'(заполн|перенос|выгруз|загруз|импорт|массов)\w*.{0,30}(карточ|товар)',
                 r'(wb|wildberries|ozon|озон|вб|маркетплейс)\w*.{0,30}(отчет|отчёт|аналитик|остатк|автоматиз|скрипт|сервис)',
                 r'(отчет|отчёт|аналитик|остатк|автоматиз|скрипт|сервис)\w*.{0,30}(wb|wildberries|ozon|озон|вб|маркетплейс)'],
        'exclude': [r'инфографик', r'дизайн', r'rich', r'фото', r'авито', r'объявлен'],
    },
    # Внедрение ИИ и работа с агентами: RAG, ассистенты, подключение API, вайбкодинг.
    # Генерацию картинок/видео/текстов сюда не берём (см. exclude).
    'ИИ': {
        'title_only': True,  # рубрики вроде «ИИ и Нейросети» сами по себе ничего не значат
        'strong': [r'dify', r'n8n', r'langchain', r'rag\b', r'claude', r'клод', r'codex', r'cursor',
                   r'вайб[- ]?код', r'vibe[- ]?cod', r'mcp\b', r'llm'],
        'weak': [r'(ии|ai)[- ]?(агент|ассистент|бот|автоматизац|систем|платформ|интеграц)',
                 r'агент\w* (на|для|с) (ии|ai)', r'(api|апи) (ии|ai|нейросет|gpt|openai)', r'chatgpt', r'gpt',
                 r'openai', r'deepseek', r'gemini', r'notebook ?lm', r'нейросет', r'промт', r'промпт',
                 r'баз\w* знаний'],
        'exclude': [r'видео', r'ролик', r'изображени', r'картин', r'фото', r'музык', r'песн', r'трек',
                    r'клип', r'анимац', r'shorts', r'reels', r'подкаст', r'текст', r'стать', r'дизайн',
                    r'контент', r'ии-генерация', r'нарис', r'комьюнити'],
    },
}
# Рубрики, которые целиком относятся к нише
NICHE_CATEGORIES = {
    'Базы данных и клиентов': 'Данные',
    'Программирование / Парсинг данных': 'Данные',
    'Игры': 'Игры',
    'Мобильные приложения': 'Приложения',
    'Программирование / Google Android': 'Приложения',
    'Программирование / iOS': 'Приложения',
    'Mobile / Приложения для Android': 'Приложения',
    'Игры / Программирование игр': 'Игры',
    'AI — искусственный интеллект / AI-агенты': 'ИИ',
    'AI — искусственный интеллект / RAG / Базы знаний': 'ИИ',
    'AI — искусственный интеллект / Боты с AI': 'ИИ',
    'Программирование / Vibe coding': 'ИИ',
}
EXCLUDE = [
    r'лайк', r'отзыв', r'подписчик', r'скачать приложени', r'(найти|поиск\w*|привлеч\w*|привед\w*) (\w+ )?(клиент|заказчик)',
    r'найти заказчик', r'лидогенерац', r'лид[аоы]?\b', r'обзвон', r'прозвон', r'рассылк', r'outreach',
    r'продаж', r'продвижен', r'ссылк', r'seo', r'набор текста', r'перепечат', r'1с', r'bas\b',
    r'solidworks', r'компас', r'чертеж', r'чертёж', r'\bкж\b', r'интерьер', r'инфографик', r'упаковк',
]


def _compile(words):
    return re.compile('|'.join(rf'(?<![a-zа-яё0-9])(?:{w})' for w in words), re.I)


NICHE_RE = {name: (_compile(w['strong']), _compile(w['weak']),
                   _compile(w['exclude']) if w.get('exclude') else None, w.get('title_only', False))
            for name, w in NICHES.items()}
EXCLUDE_RE = _compile(EXCLUDE)


def classify(order):
    """Возвращает список ниш заказа (пустой, если не подходит)."""
    head = f"{order.get('title') or ''} {order.get('category') or ''}"
    full = f"{head} {order.get('description') or ''}"
    if EXCLUDE_RE.search(head):
        return []
    title = order.get('title') or ''
    found = []
    for name, (strong, weak, excl, title_only) in NICHE_RE.items():
        where = title if title_only else head
        if (strong.search(title if title_only else full) or weak.search(where)) \
                and not (excl and excl.search(where)):
            found.append(name)
    by_cat = NICHE_CATEGORIES.get(order.get('category'))
    excl = NICHE_RE[by_cat][2] if by_cat else None
    if by_cat and by_cat not in found and not (excl and excl.search(title)):
        found.append(by_cat)
    return found


# ---------- Telegram ----------

def load_config():
    """tg.json локально или переменные окружения TG_TOKEN / TG_CHAT_ID (GitHub Actions)."""
    if os.environ.get('TG_TOKEN') and os.environ.get('TG_CHAT_ID'):
        return {'token': os.environ['TG_TOKEN'], 'chat_id': os.environ['TG_CHAT_ID'],
                'proxy': os.environ.get('TG_PROXY', '')}
    if not os.path.exists(CONFIG):
        sys.exit('Нет tg.json. Сначала: python3 notify.py --setup <токен от @BotFather>')
    with open(CONFIG, encoding='utf-8') as f:
        return json.load(f)


def tg(cfg, method, **params):
    handlers = []
    if cfg.get('proxy'):
        handlers.append(urllib.request.ProxyHandler({'https': cfg['proxy'], 'http': cfg['proxy']}))
    opener = urllib.request.build_opener(*handlers)
    data = urllib.parse.urlencode(params).encode()
    url = f"https://api.telegram.org/bot{cfg['token']}/{method}"
    with opener.open(url, data=data, timeout=30) as r:
        return json.loads(r.read())


def format_order(o, niches):
    price = f"{int(o['price']):,} ₽".replace(',', ' ') if o.get('price') else 'бюджет не указан'
    parts = [price]
    if o.get('responses') is not None:
        parts.append(f"откликов: {o['responses']}")
    parts.append(o['source'])
    if o.get('category'):
        parts.append(str(o['category']))
    desc = re.sub(r'\s+', ' ', html.unescape(o.get('description') or '')).strip()
    if len(desc) > 350:
        desc = desc[:350].rsplit(' ', 1)[0] + '…'
    tags = ' '.join(f'[{n}]' for n in niches)
    return (f"<b>{html.escape(tags)} {html.escape(html.unescape(o.get('title') or ''))}</b>\n"
            f"{html.escape(' · '.join(parts))}\n\n"
            f"{html.escape(desc)}\n\n{o['url']}")


def send(cfg, text):
    try:
        tg(cfg, 'sendMessage', chat_id=cfg['chat_id'], text=text, parse_mode='HTML',
           disable_web_page_preview='true')
        return True
    except Exception as e:
        print(f'  telegram: ошибка {e!r}', flush=True)
        return False


# ---------- режимы ----------

def setup(token):
    cfg = {'token': token.strip()}
    if os.path.exists(CONFIG):
        with open(CONFIG, encoding='utf-8') as f:
            cfg = {**json.load(f), 'token': token.strip()}
    me = tg(cfg, 'getMe')['result']
    print(f"Бот: @{me['username']}")
    chats = [u['message']['chat'] for u in tg(cfg, 'getUpdates')['result'] if 'message' in u]
    if not chats:
        with open(CONFIG, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        sys.exit(f"Напиши боту @{me['username']} любое сообщение (например /start) "
                 f"и запусти --setup ещё раз.")
    cfg['chat_id'] = chats[-1]['id']
    with open(CONFIG, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.chmod(CONFIG, 0o600)
    send(cfg, 'Уведомления о заказах подключены. Ниши: [ИИ], [Данные], [3D], [Игры], [Приложения], [Маркетплейсы].')
    print(f"Готово, chat_id={cfg['chat_id']}. Тестовое сообщение отправлено.")


def test(cfg, n):
    con = sqlite3.connect(collect.DB)
    con.row_factory = sqlite3.Row
    sent = 0
    for row in con.execute('select * from orders order by first_seen desc, created desc'):
        o = dict(row)
        niches = classify(o)
        if niches:
            send(cfg, format_order(o, niches))
            sent += 1
            time.sleep(0.5)
            if sent >= n:
                break
    print(f'отправлено {sent}')


def run(cfg):
    log = lambda s: print(s, flush=True)  # noqa: E731
    log(f"=== {datetime.datetime.now():%Y-%m-%d %H:%M:%S} ===")
    # Без базы все заказы выглядят новыми: первый проход только наполняет её, без уведомлений
    silent = not os.path.exists(collect.DB)
    if silent:
        log('  базы нет — тихий проход, уведомления со следующего запуска')
    sources = (('fl.ru', collect.fetch_flru), ('freelance.ru', collect.fetch_freelanceru),
               ('kwork', collect.fetch_kwork))
    for name, fn in sources:
        try:
            orders = fn(log)
        except Exception as e:
            log(f'  {name}: ошибка {e!r}')
            continue
        new, _ = collect.save(orders)
        hits = [(o, classify(o)) for o in new]
        hits = [(o, n) for o, n in hits if n]
        log(f'  {name}: новых {len(new)}, в нишах {len(hits)}')
        if silent:
            continue
        for o, niches in hits:
            send(cfg, format_order(o, niches))
            time.sleep(0.5)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--setup', metavar='TOKEN')
    ap.add_argument('--test', type=int, metavar='N')
    args = ap.parse_args()
    if args.setup:
        setup(args.setup)
    elif args.test:
        test(load_config(), args.test)
    else:
        run(load_config())


if __name__ == '__main__':
    main()
