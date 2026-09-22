"""Дополнительные источники: pchel.net, freelancejob.ru и публичные Telegram-каналы.

Каждая функция возвращает список заказов в формате collect.py:
source, id, url, title, description, category, price, responses, created.
"""
import datetime
import html
import re

from .base import HttpClient, HttpError

MSK = datetime.timezone(datetime.timedelta(hours=3))


def _text(s):
    s = re.sub(r'<br\s*/?>', '\n', s or '')
    s = html.unescape(re.sub(r'<[^>]+>', ' ', s))
    return re.sub(r'[ \t\xa0]+', ' ', s).strip()


def _int(s):
    digits = re.sub(r'[^\d]', '', s or '')
    return int(digits) if digits else None


def fetch_pchel():
    """pchel.net — лента проектов. Даты в списке нет, бюджет в долларах."""
    page = HttpClient(retries=1).text('https://pchel.net/jobs/')
    orders = []
    for block in page.split('<div class="project-block project-block')[1:]:
        pid = re.search(r'name="project_id" value="(\d+)"', block)
        link = re.search(r'<div class="project-title">.*?<a href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not (pid and link):
            continue
        desc = re.search(r'<div class="project-text">(.*?)</div>', block, re.S)
        cats = re.findall(r'<a href="/jobs/[^"]+/" class="b-link">([^<]+)</a>',
                          block.split('project-tags', 1)[-1])
        price = re.search(r'<div class="price">([^<]+)</div>', block)
        offers = re.search(r'title="(\d+) заяв', block)
        price_text = _text(price.group(1)) if price else ''
        orders.append({
            'source': 'pchel.net', 'id': pid.group(1),
            'url': 'https://pchel.net' + link.group(1),
            'title': _text(link.group(2)),
            'description': (f'Бюджет: {price_text}. ' if price_text else '') + _text(desc.group(1) if desc else ''),
            'category': ' / '.join(cats[:2]),
            'price': None,  # в долларах, в рубли не переводим
            'responses': int(offers.group(1)) if offers else None,
            'created': None,
        })
    return orders


def fetch_freelancejob():
    """freelancejob.ru — список проектов на главной ленте."""
    page = HttpClient(retries=1).text('https://www.freelancejob.ru/projects/')
    orders = []
    for block in page.split('<div class="x17">')[1:]:
        link = re.search(r'<a href="(/vacancy/(\d+)/)" class="big">(.*?)</a>', block, re.S)
        if not link:
            continue
        divs = re.findall(r'<div>(.*?)</div>', block, re.S)
        price = re.search(r'<span class="x19">([^<]+)</span>', block)
        date = re.search(r'добавлен:\s*(\d{2}\.\d{2}\.\d{4}) в (\d{2}:\d{2})', block)
        answers = re.search(r'Ответов:\s*<a[^>]*>(\d+)</a>', block)
        created = None
        if date:
            created = datetime.datetime.strptime(f'{date.group(1)} {date.group(2)}', '%d.%m.%Y %H:%M') \
                .replace(tzinfo=MSK).isoformat()
        orders.append({
            'source': 'freelancejob.ru', 'id': link.group(2),
            'url': 'https://www.freelancejob.ru' + link.group(1),
            'title': _text(link.group(3)),
            'description': _text(divs[1]) if len(divs) > 1 else '',
            'category': '',
            'price': _int(price.group(1)) if price and 'руб' in price.group(1) else None,
            'responses': int(answers.group(1)) if answers else None,
            'created': created,
        })
    return orders


def fetch_telegram(channels):
    """Публичные Telegram-каналы через веб-превью t.me/s/<канал> (последние ~20 постов)."""
    client = HttpClient(retries=1)
    orders = []
    for ch in channels:
        try:
            page = client.text(f'https://t.me/s/{ch}')
        except HttpError as e:
            print(f'  telegram @{ch}: {e}', flush=True)
            continue
        for block in page.split('tgme_widget_message_wrap')[1:]:
            post = re.search(r'data-post="([^"]+)"', block)
            text = re.search(r'<div class="tgme_widget_message_text[^>]*>(.*?)</div>', block, re.S)
            when = re.search(r'<time datetime="([^"]+)"', block)
            if not (post and text):
                continue
            body = _text(text.group(1))
            lines = [ln.strip() for ln in body.split('\n') if ln.strip() and not ln.strip().startswith('#')]
            title = (lines[0] if lines else body)[:150]
            orders.append({
                'source': 'telegram', 'id': post.group(1),
                'url': f'https://t.me/{post.group(1)}',
                'title': title,
                'description': body,
                'category': f'@{ch}',
                'price': None, 'responses': None,
                'created': when.group(1) if when else None,
            })
    return orders
