"""FL.ru — RSS-лента заказов (категория 5 = Программирование)."""
import re
import html as htmllib
import xml.etree.ElementTree as ET

from .base import HttpClient, HttpError

RSS_URL = 'https://www.fl.ru/rss/all.xml?category={category}'

CATEGORY_CODES = {
    '5': 'Программирование',
}

def fetch(config):
    """Возвращает список заказов (dict)."""
    client = HttpClient(retries=1)
    max_items = int(config.get('max_items', 50))
    categories = config.get('categories') or ['5']
    orders = []
    for cat in categories:
        code = CATEGORY_CODES.get(str(cat), str(cat))
        url = RSS_URL.format(category=cat)
        try:
            _, raw = client.request(url, headers={'Accept': 'application/rss+xml,*/*'})
        except HttpError as e:
            print(f'  [fl.ru] ошибка: {e}')
            continue
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as e:
            print(f'  [fl.ru] не удалось разобрать RSS: {e}')
            continue
        for item in root.iter('item'):
            title = htmllib.unescape((item.findtext('title') or '').strip())
            link = (item.findtext('link') or '').strip()
            desc = htmllib.unescape((item.findtext('description') or '').strip())
            desc = re.sub(r'\s+', ' ', desc)
            cat_name = htmllib.unescape((item.findtext('category') or '').strip())
            pub = (item.findtext('pubDate') or '').strip()
            orders.append({
                'source': 'fl.ru',
                'title': title,
                'url': link,
                'price': None,
                'date': pub,
                'category': cat_name or code,
                'description': desc[:1500],
                'responses': None,
            })
            if len(orders) >= max_items:
                break
        if len(orders) >= max_items:
            break
    return orders
