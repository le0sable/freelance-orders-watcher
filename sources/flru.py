"""FL.ru — RSS-ленты заказов. Откликов в RSS нет, бюджет есть только у части заказов, в заголовке:
«Дизайн сайта (Бюджет: 100 000 ₽, для всех)»."""
import html
import re
import xml.etree.ElementTree as ET

from .base import HttpClient, HttpError

RSS_URL = 'https://www.fl.ru/rss/all.xml'


def title_budget(title):
    """Бюджет в рублях из заголовка или None."""
    m = re.search(r'Бюджет: (?:Более )?(\d[\d\s]*)', title or '')
    return int(re.sub(r'\D', '', m.group(1))) if m else None


def _item(item):
    text = lambda tag: html.unescape(item.findtext(tag) or '').strip()  # noqa: E731
    url, title = text('link'), text('title')
    m = re.search(r'/projects/(\d+)', url)
    return {
        'source': 'fl.ru', 'id': m and m.group(1), 'url': url, 'title': title,
        'description': re.sub(r'\s+', ' ', text('description'))[:1500],
        'category': text('category'), 'price': title_budget(title), 'responses': None,
        'date': text('pubDate'),
    }


def fetch(config):
    """config: categories — коды рубрик ('' — вся лента), max_items — предел на одну ленту."""
    client = HttpClient(retries=1)
    orders = []
    for cat in config.get('categories') or ['']:
        url = RSS_URL + (f'?category={cat}' if cat else '')
        try:
            root = ET.fromstring(client.request(url, headers={'Accept': 'application/rss+xml,*/*'})[1])
        except (HttpError, ET.ParseError) as e:
            print(f'  fl.ru {url}: {e}', flush=True)
            continue
        orders += [_item(i) for i in root.iter('item')][:int(config.get('max_items', 500))]
    return [o for o in orders if o['id']]
