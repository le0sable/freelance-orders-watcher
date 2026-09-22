"""HTTP-клиент для источников на стандартной библиотеке: куки, заголовки браузера, повторы."""
import http.cookiejar
import json as jsonlib
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36')


class HttpError(Exception):
    """Площадка не ответила или ответила ошибкой."""


def _multipart(boundary, fields):
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
             for name, value in fields]
    return (''.join(parts) + f'--{boundary}--\r\n').encode('utf-8')


class HttpClient:
    """Сессия с куками. Сетевые сбои, 429 и 5xx повторяет с растущей паузой;
    остальные 4xx (например, 403 от Kwork) сразу отдаёт наверх: повтор там только злит сайт."""

    def __init__(self, timeout=30, retries=2, pause=2.0, user_agent=DEFAULT_UA,
                 extra_headers=None, raw_cookie=None):
        self.timeout, self.retries, self.pause = timeout, retries, pause
        self.headers = {
            'User-Agent': user_agent,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'ru-RU,ru;q=0.9,en;q=0.8',
            'Connection': 'close',
            **(extra_headers or {}),
        }
        self.raw_cookie = (raw_cookie or '').strip()
        self.cookie_jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookie_jar))
        self.last_url = None

    def request(self, url, method='GET', data=None, headers=None, as_multipart=None, form=None):
        """Возвращает (status, bytes). as_multipart = (boundary, [(поле, значение)]), form = dict."""
        hdrs = {**self.headers, **(headers or {})}
        body = data
        if as_multipart is not None:
            boundary, fields = as_multipart
            body = _multipart(boundary, fields)
            hdrs['Content-Type'] = f'multipart/form-data; boundary={boundary}'
        elif form is not None:
            body = urllib.parse.urlencode(form).encode('utf-8')
            hdrs['Content-Type'] = 'application/x-www-form-urlencoded'
        if self.raw_cookie:
            hdrs['Cookie'] = '; '.join(c for c in (self.raw_cookie, hdrs.get('Cookie')) if c)

        for attempt in range(self.retries + 1):
            try:
                req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
                with self.opener.open(req, timeout=self.timeout) as resp:
                    self.last_url = resp.geturl()
                    return resp.status, resp.read()
            except urllib.error.HTTPError as e:
                e.close()
                error = HttpError(f'HTTP {e.code} для {url}')
                if 400 <= e.code < 500 and e.code != 429:
                    raise error from None
            except (urllib.error.URLError, OSError) as e:  # TimeoutError — подкласс OSError
                error = HttpError(f'Сетевая ошибка для {url}: {e}')
            if attempt < self.retries:
                time.sleep(self.pause * (attempt + 1))
        raise error

    def text(self, url, **kw):
        return self.request(url, **kw)[1].decode('utf-8', 'ignore')

    def json(self, url, **kw):
        return jsonlib.loads(self.text(url, **kw))
