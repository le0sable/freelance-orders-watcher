"""Freelance.ru — HTML-лента заданий https://freelance.ru/task.

Категории (параметр c[]):
    4   — Веб-разработка и IT
    724 — Искусственный интеллект
"""
import re
import html as htmllib

from .base import HttpClient, HttpError

TASK_URL = 'https://freelance.ru/task?' + '&'.join(f'c[]={c}' for c in ('{cats}'))

CATEGORY_CODES = {
    '4': 'Веб-разработка и IT',
    '724': 'Искусственный интеллект',
}

_RUB = '\u20bd'

def _clean(s):
    s = htmllib.unescape(s or '')
    return re.sub(r'\s+', ' ', s).strip()

def _parse_budget(block):
    m = re.search(r'<div class="task-card__budget[^"]*">\s*'
                  r'<span class="bold[^"]*">\s*(.*?)</span>', block, re.S)
    if not m:
        return None
    b = _clean(m.group(1)).replace(_RUB, '₽')
    if 'индивидуально' in b.lower() or 'договор' in b.lower():
        return b
    return b or None

def _parse_date(block):
    m = re.search(r'title="(\d{2}\.\d{2}\.\d{4} \d{2}:\d{2})"', block)
    return m.group(1) if m else None

def _parse_relative(block):
    m = re.search(r'<i class="fa fa-clock-o[^"]*"[^>]*></i>\s*([^<]+)</span>', block)
    return _clean(m.group(1)) if m else None

def fetch(config):
    client = HttpClient(retries=1)
    cats = config.get('categories') or ['4', '724']
    max_items = int(config.get('max_items', 60))
    url = 'https://freelance.ru/task?' + '&'.join(f'c%5B%5D={c}' for c in cats)
    try:
        page = client.text(url)
    except HttpError as e:
        print(f'  [freelance.ru] ошибка: {e}')
        return []
    orders = []
    marks = list(re.finditer(r'<a class="task-card__title-link" href="(/task/view/\d+)"'
                             r' title="([^"]*)"', page))
    for i, m in enumerate(marks):
        block = page[m.start(): marks[i + 1].start() if i + 1 < len(marks) else m.end() + 3000]
        href, title = m.group(1), htmllib.unescape(m.group(2))
        desc = ''
        md = re.search(r'<p class="task-card__desc">(.*?)</p>', block, re.S)
        if md:
            desc = _clean(md.group(1))
        mc = re.search(r'<span class="task-chip task-chip--cat">([^<]+)</span>', block)
        category = htmllib.unescape(mc.group(1)).strip() if mc else ''
        mr = re.search(r'<i class="fa fa-comments[^"]*"[^>]*></i>\s*<strong>(\d+)</strong>', block)
        orders.append({
            'source': 'freelance.ru',
            'title': _clean(title),
            'url': 'https://freelance.ru' + href,
            'price': _parse_budget(block),
            'date': _parse_date(block) or _parse_relative(block),
            'category': category or CATEGORY_CODES.get(str(cats[0]), ''),
            'description': desc[:1500],
            'responses': int(mr.group(1)) if mr else None,
        })
        if len(orders) >= max_items:
            break
    return orders
