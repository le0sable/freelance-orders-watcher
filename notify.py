"""Live-уведомления в Telegram о новых заказах в своих нишах.

Каждый запуск: собирает свежие заказы (как collect.py, Kwork — только новые
страницы), пишет их в market.db и шлёт в Telegram те новые, что попали в ниши:
    [ИИ], [Данные], [3D], [Игры], [Приложения], [Маркетплейсы] — см. NICHES ниже;
    [★] — сработал белый список

Под каждым заказом кнопки 👍/👎, а в чате — команды (/help): чёрный и белый списки слов,
минимальный бюджет, выключение ниш, Telegram-каналы. Всё это хранится в settings.json.
По оценкам 👍/👎 считается 🎯 — насколько заказ похож на понравившиеся.

    python3 notify.py --setup <токен>  # один раз: сохранить токен и найти chat_id
    python3 notify.py                  # один проход
    python3 notify.py --loop 540       # 9 минут: площадки раз в 3 мин, команды сразу (так в GitHub Actions)
    python3 notify.py --test 5         # прислать 5 последних подходящих заказов из базы

Доступ к боту лежит в tg.json (или в TG_TOKEN / TG_CHAT_ID): token, chat_id и необязательный proxy — http-прокси
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


def _word_rx(words):
    """Слова из settings.json: ищем по началу слова, без учёта регистра."""
    words = [w.strip() for w in words if w.strip()]
    return _compile([re.escape(w.lower().replace('ё', 'е')) for w in words]) if words else None


def classify(order, settings=None):
    """Возвращает список ниш заказа (пустой, если не подходит).

    Порядок: белый список слов → всегда присылать; чёрный список и общие исключения →
    не присылать; дальше ниши по словам и рубрикам, минимальный бюджет, выключенные ниши.
    """
    settings = settings or {}
    title = order.get('title') or ''
    head = f"{title} {order.get('category') or ''}"
    full = f"{head} {order.get('description') or ''}"
    norm = full.lower().replace('ё', 'е')
    white = _word_rx(settings.get('whitelist', []))
    if white and white.search(norm):
        return ['★']
    black = _word_rx(settings.get('blacklist', []))
    if (black and black.search(norm)) or EXCLUDE_RE.search(head):
        return []
    min_price = settings.get('min_price') or 0
    if order.get('price') and order['price'] < min_price:
        return []
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
    off = {n.lower() for n in settings.get('niches_off', [])}
    return [n for n in found if n.lower() not in off]


# ---------- оценка по твоим 👍/👎 (наивный Байес) ----------

MIN_LABELS = 10  # столько 👍 и столько же 👎 нужно, чтобы оценка включилась


def _tokens(order):
    text = f"{order.get('title') or ''} {order.get('title') or ''} {order.get('category') or ''} " \
           f"{(order.get('description') or '')[:400]}"
    words = re.findall(r'[a-zа-яё0-9]{3,}', text.lower().replace('ё', 'е'))
    return {w[:6] for w in words}  # грубый стемминг: первые 6 букв


class Scorer:
    def __init__(self, con):
        rows = con.execute('select o.title, o.category, o.description, f.label from feedback f '
                           'join orders o on o.source = f.source and o.id = f.id').fetchall()
        self.counts = {1: {}, 0: {}}
        self.docs = {1: 0, 0: 0}
        for title, cat, desc, label in rows:
            label = 1 if label > 0 else 0
            self.docs[label] += 1
            for t in _tokens({'title': title, 'category': cat, 'description': desc}):
                self.counts[label][t] = self.counts[label].get(t, 0) + 1
        self.ready = min(self.docs.values()) >= MIN_LABELS

    def score(self, order):
        """Вероятность, что заказ тебе понравится (0..1), или None, пока мало оценок."""
        if not self.ready:
            return None
        import math
        logp = {c: math.log(self.docs[c] / sum(self.docs.values())) for c in (0, 1)}
        for t in _tokens(order):
            for c in (0, 1):
                logp[c] += math.log((self.counts[c].get(t, 0) + 1) / (self.docs[c] + 2))
        return 1 / (1 + math.exp(logp[0] - logp[1]))


# ---------- время ----------

MSK = datetime.timezone(datetime.timedelta(hours=3))


def parse_created(o):
    """Время публикации как aware datetime (у каждой площадки свой формат) или None."""
    raw = (o.get('created') or o.get('date') or '').strip()
    if not raw:
        return None
    for fmt, tz in (('%Y-%m-%d %H:%M:%S', MSK), ('%d.%m.%Y %H:%M', MSK)):
        try:
            return datetime.datetime.strptime(raw, fmt).replace(tzinfo=tz)
        except ValueError:
            pass
    try:
        return datetime.datetime.fromisoformat(raw)
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None


def _ago(dt):
    minutes = int((datetime.datetime.now(datetime.timezone.utc) - dt).total_seconds() // 60)
    if minutes < 0:
        return 'только что'
    if minutes < 60:
        return f'{minutes} мин назад'
    if minutes < 48 * 60:
        return f'{minutes // 60} ч назад'
    return f'{minutes // 1440} дн назад'


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
    with opener.open(url, data=data, timeout=30 + int(params.get('timeout', 0))) as r:
        return json.loads(r.read())


def _rub(v):
    return f"{int(v):,} ₽".replace(',', ' ')


def format_order(o, niches, score=None):
    lines = []
    tags = ' '.join(f'[{n}]' for n in niches)
    lines.append(f"<b>{html.escape(tags)} {html.escape(html.unescape(o.get('title') or ''))}</b>")

    money = _rub(o['price']) if o.get('price') else 'бюджет не указан'
    if o.get('price_max') and o.get('price') and o['price_max'] > o['price']:
        money += f" (допустимо до {_rub(o['price_max'])})"
    first = [f'💰 {money}']
    if o.get('responses') is not None:
        first.append(f"👥 откликов: {o['responses']}")
    lines.append(html.escape(' · '.join(first)))

    dt = parse_created(o)
    when = f"🕒 {dt.astimezone(MSK):%d.%m %H:%M} МСК ({_ago(dt)})" if dt else '🕒 время не указано'
    second = [when, o['source']]
    if o.get('category'):
        second.append(str(o['category']))
    lines.append(html.escape(' · '.join(second)))

    if o.get('buyer_orders') is not None:
        buyer = f"👤 заказчик: проектов {o['buyer_orders']}"
        if o.get('buyer_hired_pct') is not None:
            buyer += f", нанимает в {o['buyer_hired_pct']}%"
        lines.append(html.escape(buyer))
    if score is not None:
        lines.append(f'🎯 похоже на твои 👍: {round(score * 100)}%')

    desc = re.sub(r'<[^>]+>', ' ', html.unescape(o.get('description') or ''))
    desc = re.sub(r'\s+', ' ', desc).strip()
    if len(desc) > 350:
        desc = desc[:350].rsplit(' ', 1)[0] + '…'
    return '\n'.join(lines) + f"\n\n{html.escape(desc)}\n\n{o['url']}"


def _buttons(o):
    key = f"{o['source']}:{o['id']}"
    return json.dumps({'inline_keyboard': [[
        {'text': '👍 интересно', 'callback_data': f'fb:1:{key}'[:64]},
        {'text': '👎 мимо', 'callback_data': f'fb:-1:{key}'[:64]},
    ]]})


def send(cfg, text, markup=None):
    params = {'chat_id': cfg['chat_id'], 'text': text, 'parse_mode': 'HTML',
              'disable_web_page_preview': 'true'}
    if markup:
        params['reply_markup'] = markup
    try:
        tg(cfg, 'sendMessage', **params)
        return True
    except Exception as e:
        print(f'  telegram: ошибка {e!r}', flush=True)
        return False


# ---------- команды и кнопки из Telegram ----------

HELP = """Команды:
/ban слово — не присылать заказы с этим словом
/unban слово — убрать из чёрного списка
/allow слово — присылать всегда, даже вне ниш
/unallow слово — убрать из белого списка
/minprice 3000 — не присылать заказы дешевле (0 — выключить)
/hide 20 — прятать заказы с оценкой 🎯 ниже 20% (0 — выключить)
/off Данные, /on Данные — выключить или включить нишу
/channel add имя, /channel del имя — Telegram-каналы с заказами
/rules — текущие настройки
/stats — сколько оценок 👍/👎 и заказов по нишам

Кнопки 👍/👎 под заказами учат оценку 🎯: после 10 👍 и 10 👎 она появится в сообщениях.
Бот отвечает сразу, пока идёт очередной запуск на GitHub, и с задержкой до 10–20 минут,
если запуск ещё не начался."""


def _rules_text(settings):
    def fmt(xs):
        return ', '.join(xs) if xs else '—'
    return (f"Чёрный список: {fmt(settings.get('blacklist', []))}\n"
            f"Белый список: {fmt(settings.get('whitelist', []))}\n"
            f"Мин. бюджет: {settings.get('min_price') or 0} ₽\n"
            f"Прятать при 🎯 ниже: {settings.get('hide_below_score') or 0}%\n"
            f"Выключенные ниши: {fmt(settings.get('niches_off', []))}\n"
            f"Ниши: {', '.join(NICHES)}\n"
            f"Telegram-каналы: {fmt(['@' + c for c in settings.get('telegram_channels', [])])}")


def _toggle(lst, value, add):
    value = value.strip().lstrip('@')
    if add and value and value not in lst:
        lst.append(value)
        return True
    if not add and value in lst:
        lst.remove(value)
        return True
    return False


def handle_command(text, settings, con):
    """Возвращает (ответ, изменились ли настройки)."""
    cmd, _, arg = text.strip().partition(' ')
    cmd = cmd.split('@')[0].lower()
    arg = arg.strip()
    lists = {'/ban': ('blacklist', True), '/unban': ('blacklist', False),
             '/allow': ('whitelist', True), '/unallow': ('whitelist', False)}
    if cmd in lists and arg:
        key, add = lists[cmd]
        changed = _toggle(settings.setdefault(key, []), arg.lower(), add)
        return (f"{'Готово' if changed else 'Без изменений'}.\n\n{_rules_text(settings)}", changed)
    if cmd in ('/minprice', '/hide') and arg.isdigit():
        key = 'min_price' if cmd == '/minprice' else 'hide_below_score'
        settings[key] = int(arg)
        return (f'Готово.\n\n{_rules_text(settings)}', True)
    if cmd in ('/off', '/on') and arg:
        name = next((n for n in NICHES if n.lower() == arg.lower()), None)
        if not name:
            return (f"Нет такой ниши. Есть: {', '.join(NICHES)}", False)
        changed = _toggle(settings.setdefault('niches_off', []), name, cmd == '/off')
        return (f'Готово.\n\n{_rules_text(settings)}', changed)
    if cmd == '/channel':
        action, _, name = arg.partition(' ')
        if action in ('add', 'del') and name:
            changed = _toggle(settings.setdefault('telegram_channels', []), name, action == 'add')
            return (f'Готово.\n\n{_rules_text(settings)}', changed)
    if cmd == '/rules':
        return (_rules_text(settings), False)
    if cmd == '/stats':
        likes, dislikes = (con.execute('select count(*) from feedback where label=?', (v,)).fetchone()[0]
                           for v in (1, -1))
        return (f'Оценок: 👍 {likes}, 👎 {dislikes}. Оценка 🎯 включается с {MIN_LABELS} каждого вида.', False)
    return (HELP, False)


def process_updates(cfg, settings, log, wait=0):
    """Забирает нажатия кнопок и команды (ждёт до wait секунд, если их нет).
    Возвращает True, если изменились настройки или оценки."""
    con = sqlite3.connect(collect.DB)
    con.executescript(collect.SCHEMA)
    row = con.execute("select value from kv where key='tg_offset'").fetchone()
    offset = int(row[0]) if row else 0
    try:
        updates = tg(cfg, 'getUpdates', offset=offset, timeout=wait)['result']
    except Exception as e:
        log(f'  telegram getUpdates: ошибка {e!r}')
        return False
    changed = False
    chat = str(cfg['chat_id'])
    for u in updates:
        offset = u['update_id'] + 1
        cb = u.get('callback_query')
        msg = u.get('message')
        if cb and str(cb['message']['chat']['id']) == chat and cb.get('data', '').startswith('fb:'):
            _, label, key = cb['data'].split(':', 2)
            source, oid = key.split(':', 1)
            con.execute('insert or replace into feedback values (?,?,?,?)',
                        (source, oid, int(label), collect.now()))
            changed = True
            mark = '👍 отмечено' if label == '1' else '👎 отмечено'
            try:
                tg(cfg, 'answerCallbackQuery', callback_query_id=cb['id'], text=mark)
            except Exception:
                pass  # если ответить не успели (запрос устарел), просто меняем кнопки
            try:
                tg(cfg, 'editMessageReplyMarkup', chat_id=chat, message_id=cb['message']['message_id'],
                   reply_markup=json.dumps({'inline_keyboard': [[{'text': mark, 'callback_data': 'noop'}]]}))
            except Exception:
                pass
        elif msg and str(msg['chat']['id']) == chat and msg.get('text', '').startswith('/'):
            reply, settings_changed = handle_command(msg['text'], settings, con)
            if settings_changed:
                collect.save_settings(settings)
                changed = True
            send(cfg, html.escape(reply))
    con.execute("insert or replace into kv values ('tg_offset', ?)", (str(offset),))
    con.commit()
    con.close()
    if updates:
        log(f'  telegram: обработано обновлений {len(updates)}')
    return changed


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
    send(cfg, f"Уведомления о заказах подключены. Ниши: {', '.join(NICHES)}.")
    print(f"Готово, chat_id={cfg['chat_id']}. Тестовое сообщение отправлено.")


def notify_orders(cfg, orders, settings, scorer):
    sent = 0
    for o in orders:
        niches = classify(o, settings)
        if not niches:
            continue
        score = scorer.score(o) if niches != ['★'] else None
        hide = settings.get('hide_below_score') or 0
        if score is not None and score * 100 < hide:
            continue
        send(cfg, format_order(o, niches, score), _buttons(o))
        sent += 1
        time.sleep(0.5)
    return sent


def test(cfg, n):
    settings = collect.load_settings()
    con = sqlite3.connect(collect.DB)
    con.executescript(collect.SCHEMA)
    con.row_factory = sqlite3.Row
    orders = [dict(r) for r in con.execute('select * from orders order by first_seen desc, created desc')]
    scorer = Scorer(con)
    picked = [o for o in orders if classify(o, settings)][:n]
    print(f'отправлено {notify_orders(cfg, picked, settings, scorer)}')


def collect_pass(cfg, settings, log, silent=False):
    """Один проход по всем площадкам: сохранить новые заказы и прислать подходящие."""
    con = sqlite3.connect(collect.DB)
    con.executescript(collect.SCHEMA)
    known_sources = {r[0] for r in con.execute('select distinct source from orders')}
    scorer = Scorer(con)
    con.close()
    for name, fn in collect.all_sources(settings):
        try:
            orders = fn(log)
        except Exception as e:
            log(f'  {name}: ошибка {e!r}')
            continue
        new, _ = collect.save(orders)
        # Новая площадка: первый раз только запоминаем её заказы, чтобы не прислать всю ленту разом
        first_time = bool(orders) and orders[0]['source'] not in known_sources
        if silent or first_time:
            log(f'  {name}: новых {len(new)}, первый проход — без уведомлений')
            continue
        log(f'  {name}: новых {len(new)}, отправлено {notify_orders(cfg, new, settings, scorer)}')


def run(cfg, duration=0, every=180):
    """Один проход, или (duration > 0) работа в течение duration секунд: площадки
    опрашиваются раз в every секунд, а в паузах бот сразу отвечает на команды и кнопки."""
    log = lambda s: print(s, flush=True)  # noqa: E731
    log(f"=== {datetime.datetime.now():%Y-%m-%d %H:%M:%S} ===")
    # Без базы все заказы выглядят новыми: первый проход только наполняет её, без уведомлений
    silent = not os.path.exists(collect.DB)
    if silent:
        log('  базы нет — тихий проход, уведомления со следующего запуска')
    settings = collect.load_settings()
    changed = process_updates(cfg, settings, log)
    collect_pass(cfg, settings, log, silent)
    deadline = time.time() + duration
    next_pass = time.time() + every
    while time.time() < deadline - 5:
        wait = int(max(1, min(25, next_pass - time.time(), deadline - time.time() - 5)))
        changed |= process_updates(cfg, settings, log, wait=wait)
        if time.time() >= next_pass and time.time() < deadline - 60:
            log(f"--- {datetime.datetime.now():%H:%M:%S} ---")
            collect_pass(cfg, settings, log)
            next_pass = time.time() + every
    if changed:
        # сигнал для GitHub Actions: закоммитить настройки и базу с оценками
        open(os.path.join(HERE, '.changed'), 'w').close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--setup', metavar='TOKEN')
    ap.add_argument('--test', type=int, metavar='N')
    ap.add_argument('--loop', type=int, default=0, metavar='SECONDS',
                    help='работать столько секунд: площадки раз в 3 мин, команды сразу')
    args = ap.parse_args()
    if args.setup:
        setup(args.setup)
    elif args.test:
        test(load_config(), args.test)
    else:
        run(load_config(), duration=args.loop)


if __name__ == '__main__':
    main()
