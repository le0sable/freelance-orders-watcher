"""Freelance.ru — HTML-лента заданий https://freelance.ru/task, одна страница.

Рубрики (параметр c[]): 4 — «Веб-разработка и IT», 724 — «Искусственный интеллект».
"""
import html
import re

from .base import HttpClient, HttpError


def _clean(s):
    return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', s or ''))).strip()


def _number(s):
    """Первое число в строке: «10 000 ₽» -> 10000, «Обсуждается индивидуально» -> None."""
    m = re.search(r'\d[\d\s]*', s or '')
    return int(re.sub(r'\D', '', m.group())) if m else None


def _card(block):
    link = re.search(r'class="task-card__title-link" href="/task/view/(\d+)" title="([^"]*)"', block)
    if not link:
        return None
    find = lambda rx: (m.group(1) if (m := re.search(rx, block, re.S)) else '')  # noqa: E731
    return {
        'source': 'freelance.ru', 'id': link.group(1),
        'url': f'https://freelance.ru/task/view/{link.group(1)}',
        'title': _clean(link.group(2)),
        'description': _clean(find(r'<p class="task-card__desc">(.*?)</p>'))[:1500],
        'category': _clean(find(r'class="task-chip task-chip--cat">([^<]+)<')),
        'price': _number(_clean(find(r'class="task-card__budget[^"]*">\s*<span[^>]*>(.*?)</span>'))),
        'responses': _number(find(r'fa-comments[^>]*></i>\s*<strong>([\d\s]+)</strong>')),
        'views': _number(find(r'fa-eye[^>]*></i>\s*([\d\s]+)<')),
        'date': find(r'title="(\d\d\.\d\d\.\d{4} \d\d:\d\d)"') or None,
    }


def fetch(config):
    """config: categories — коды рубрик, max_items — предел."""
    cats = config.get('categories') or ['4', '724']
    url = 'https://freelance.ru/task?' + '&'.join(f'c%5B%5D={c}' for c in cats)
    try:
        page = HttpClient(retries=1).text(url)
    except HttpError as e:
        print(f'  freelance.ru: {e}', flush=True)
        return []
    orders = [o for o in map(_card, page.split('<article class="task-card')[1:]) if o]
    return orders[:int(config.get('max_items', 60))]
