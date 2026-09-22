"""
Базовый HTTP-клиент для всех источников (только stdlib).
Умеет: cookie-сессии, User-Agent, таймауты, повторные попытки.
"""
import time
import http.cookiejar
import urllib.request
import urllib.error
import urllib.parse

DEFAULT_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36')

class HttpError(Exception):
    """Ошибка HTTP/сети при запросе к источнику."""

class HttpClient:
    """Простая сессия с куками и ретраями."""

    def __init__(self, timeout=30, retries=2, pause=2.0, user_agent=DEFAULT_UA,
                 extra_headers=None, raw_cookie=None):
        self.timeout = timeout
        self.retries = retries
        self.pause = pause
        self.last_url = None
        self.headers = {
            'User-Agent': user_agent,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'ru-RU,ru;q=0.9,en;q=0.8',
            'Connection': 'close',
        }
        if extra_headers:
            self.headers.update(extra_headers)
        self.cookie_jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookie_jar))
        self.raw_cookie = (raw_cookie or '').strip()

    def request(self, url, method='GET', data=None, headers=None,
                as_multipart=None, form=None):
        """Возвращает (status, bytes ответа). Бросает HttpError после ретраев."""
        hdrs = dict(self.headers)
        if headers:
            hdrs.update(headers)
        body = None
        if as_multipart is not None:
            boundary, fields = as_multipart
            body = b''.join(
                (f'--{boundary}\r\n'
                 f'Content-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n').encode('utf-8')
                for k, v in fields)
            body += f'--{boundary}--\r\n'.encode('utf-8')
            hdrs['Content-Type'] = f'multipart/form-data; boundary={boundary}'
        elif form is not None:
            body = urllib.parse.urlencode(form).encode('utf-8')
            hdrs['Content-Type'] = 'application/x-www-form-urlencoded'
        elif data is not None:
            body = data

        if self.raw_cookie:
            prev = hdrs.get('Cookie', '')
            hdrs['Cookie'] = (self.raw_cookie + '; ' + prev).strip('; ')

        last_err = None
        for attempt in range(self.retries + 1):
            try:
                req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
                with self.opener.open(req, timeout=self.timeout) as resp:
                    self.last_url = resp.geturl()
                    return resp.status, resp.read()
            except urllib.error.HTTPError as e:
                last_err = HttpError(f'HTTP {e.code} для {url}')
                try:
                    e.read()
                except Exception:
                    pass
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last_err = HttpError(f'Сетевая ошибка для {url}: {e}')
            if attempt < self.retries:
                time.sleep(self.pause * (attempt + 1))
        raise last_err

    def text(self, url, **kw):
        _, raw = self.request(url, **kw)
        return raw.decode('utf-8', 'ignore')

    def json(self, url, **kw):
        import json
        _, raw = self.request(url, **kw)
        return json.loads(raw.decode('utf-8', 'ignore'))
