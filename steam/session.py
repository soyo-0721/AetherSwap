import base64
import copy
import json
import math
import time
from typing import Optional
from urllib.parse import unquote, urlparse
from steam.request_policy import record_steam_market_response, steam_request_gate
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

try:
    from curl_cffi import requests as curl_requests
except ImportError:  # pragma: no cover - dependency is declared in requirements
    curl_requests = None

MARKET_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
}


STEAM_CURL_IMPERSONATE = "chrome120"


class _SteamCommunityAdapter(requests.adapters.HTTPAdapter):
    """Use one curl_cffi browser-profile session for all Steam Community HTTP verbs.

    Requests still prepares URLs, form bodies and the Cookie header. curl_cffi
    performs the network exchange; response cookies are copied back into both
    the owning Session jar and the PreparedRequest jar so normal requests
    redirect handling continues to work.
    """

    _TRANSPORT_MANAGED_HEADERS = {
        "host",
        "connection",
        "content-length",
        "transfer-encoding",
        "accept-encoding",
        "proxy-connection",
    }

    def __init__(self, cookie_jar):
        super().__init__()
        self._cookie_jar = cookie_jar
        self._curl_session = None
        if curl_requests is not None:
            self._curl_session = curl_requests.Session(
                impersonate=STEAM_CURL_IMPERSONATE,
                trust_env=False,
                default_headers=False,
            )

    @staticmethod
    def _curl_timeout(timeout):
        """Convert requests' urllib3 Timeout object to curl_cffi's timeout form."""
        if timeout is None or isinstance(timeout, (int, float, tuple)):
            return timeout
        connect_timeout = getattr(timeout, "connect_timeout", None)
        read_timeout = getattr(timeout, "read_timeout", None)
        if connect_timeout is not None or read_timeout is not None:
            if connect_timeout == read_timeout:
                return connect_timeout
            return (
                connect_timeout if connect_timeout is not None else read_timeout,
                read_timeout if read_timeout is not None else connect_timeout,
            )
        return timeout

    @staticmethod
    def _cookie_applies_to_request(cookie, parsed_url) -> bool:
        host = (parsed_url.hostname or "").lower()
        domain = (getattr(cookie, "domain", "") or "").lstrip(".").lower()
        if domain and host != domain and not host.endswith("." + domain):
            return False

        cookie_path = getattr(cookie, "path", "/") or "/"
        request_path = parsed_url.path or "/"
        if not request_path.startswith(cookie_path):
            return False
        if not (
            cookie_path.endswith("/")
            or request_path == cookie_path
            or request_path[len(cookie_path):].startswith("/")
        ):
            return False
        if getattr(cookie, "secure", False) and parsed_url.scheme != "https":
            return False
        return True

    @classmethod
    def _store_cookie(cls, jar, cookie, parsed_url) -> None:
        if jar is None or not hasattr(jar, "set_cookie"):
            return
        # Replace only a cookie with the same name that would have been sent
        # to this request. Keep other domain/path-scoped cookies untouched.
        for old_cookie in list(jar):
            if (
                old_cookie.name == cookie.name
                and cls._cookie_applies_to_request(old_cookie, parsed_url)
            ):
                try:
                    jar.clear(old_cookie.domain, old_cookie.path, old_cookie.name)
                except KeyError:
                    pass
        jar.set_cookie(copy.copy(cookie))
        clear_expired = getattr(jar, "clear_expired_cookies", None)
        if callable(clear_expired):
            clear_expired()

    def _copy_response_cookies(self, request, raw_response, response) -> None:
        raw_cookies = getattr(raw_response, "cookies", None)
        raw_jar = getattr(raw_cookies, "jar", raw_cookies)
        if raw_jar is None:
            return
        try:
            cookies = list(raw_jar)
        except TypeError:
            return

        parsed_url = urlparse(request.url)
        response.cookies = requests.cookies.RequestsCookieJar()
        target_jars = [self._cookie_jar, getattr(request, "_cookies", None)]
        seen_jars = set()
        for cookie in cookies:
            response.cookies.set_cookie(copy.copy(cookie))
            for jar in target_jars:
                if jar is None or id(jar) in seen_jars:
                    continue
                seen_jars.add(id(jar))
                self._store_cookie(jar, cookie, parsed_url)

    @staticmethod
    def _raise_transport_error(exc):
        if isinstance(exc, requests.RequestException):
            raise exc
        error_name = type(exc).__name__.lower()
        if "timeout" in error_name:
            raise requests.Timeout(str(exc)) from exc
        if "proxy" in error_name:
            raise requests.ProxyError(str(exc)) from exc
        if "connection" in error_name or "curl" in error_name:
            raise requests.ConnectionError(str(exc)) from exc
        raise requests.RequestException(
            f"Steam curl_cffi transport failed: {type(exc).__name__}"
        ) from exc

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        # curl_cffi is a declared runtime dependency. Keep a requests fallback
        # for deliberately minimal installations and isolated transport tests.
        if self._curl_session is None:
            return super().send(
                request, stream=stream, timeout=timeout, verify=verify,
                cert=cert, proxies=proxies,
            )

        headers = {
            name: value
            for name, value in request.headers.items()
            if name.lower() not in self._TRANSPORT_MANAGED_HEADERS
        }
        try:
            # requests has already merged its cookie jar into this prepared
            # Cookie header. Clear curl_cffi's internal jar before each call to
            # avoid duplicate/stale cookies; Set-Cookie is synchronized below.
            self._curl_session.cookies.clear()
            request_kwargs = {
                "method": request.method,
                "url": request.url,
                "headers": headers,
                "data": request.body,
                "proxies": proxies,
                "timeout": self._curl_timeout(timeout),
                "verify": verify,
                "allow_redirects": False,
                "stream": False,
                "impersonate": STEAM_CURL_IMPERSONATE,
            }
            if cert is not None:
                request_kwargs["cert"] = cert
            raw = self._curl_session.request(**request_kwargs)
        except Exception as exc:
            self._raise_transport_error(exc)

        response = requests.Response()
        response.status_code = raw.status_code
        response.url = getattr(raw, "url", request.url)
        response.headers.update(getattr(raw, "headers", {}) or {})
        response.encoding = getattr(raw, "encoding", None)
        response.reason = getattr(raw, "reason", None)
        response.elapsed = getattr(raw, "elapsed", None)
        response._content = getattr(raw, "content", b"") or b""
        response._content_consumed = True
        response.request = request
        response.cookies = requests.cookies.RequestsCookieJar()
        response.steam_transport = f"curl_cffi/{STEAM_CURL_IMPERSONATE}"
        response.steam_http_version = getattr(raw, "http_version", None)
        response.steam_remote_ip = getattr(raw, "primary_ip", None)
        self._copy_response_cookies(request, raw, response)
        return response

    def close(self):
        if self._curl_session is not None:
            self._curl_session.close()
            self._curl_session = None
        super().close()


# Retain the old private class name for internal imports from older extensions.
_MarketReadAdapter = _SteamCommunityAdapter

class _SteamSession(requests.Session):
    """Route Steam traffic and coordinate every Steam Community request."""

    def __init__(self, *, allow_auth_during_cooldown=False):
        super().__init__()
        self._allow_auth_during_cooldown = allow_auth_during_cooldown

    def _request_with_proxy(self, method, url, **kwargs):
        # A non-None proxy mapping means the caller has explicitly selected
        # its route. None is equivalent to omitting the argument and must still
        # use the shared proxy policy.
        if kwargs.get("proxies") is not None:
            return super().request(method, url, **kwargs)
        kwargs.pop("proxies", None)

        from utils.proxy_manager import get_proxy_manager

        manager = get_proxy_manager()
        proxies = manager.get_proxies_for_request()
        if manager.should_always_use_proxy() and not proxies:
            raise requests.exceptions.ProxyError("Steam 代理已启用，但没有可用节点")
        if proxies:
            # Pass per request so environment proxies cannot override this choice.
            kwargs["proxies"] = dict(proxies)
        try:
            return super().request(method, url, **kwargs)
        except (requests.ConnectionError, requests.Timeout):
            # A failed write may have reached Steam. Never replay it implicitly.
            if method.upper() not in ("GET", "HEAD") or not manager.should_use_proxy_on_failure():
                raise
            fallback = manager.get_proxies_for_request(failed=True)
            if not fallback or fallback == proxies:
                raise
            kwargs["proxies"] = dict(fallback)
            return super().request(method, url, **kwargs)

    def request(self, method, url, **kwargs):
        parsed = urlparse(url)
        if parsed.hostname != "steamcommunity.com":
            return self._request_with_proxy(method, url, **kwargs)

        is_auth_check = (
            self._allow_auth_during_cooldown
            and method.upper() == "GET"
            and parsed.path.rstrip("/") == "/my/profile"
        )
        # Explicit auth verification may run during cooldown, but it still uses
        # the same request gate and any 429 response is recorded globally.
        with steam_request_gate.slot(check_cooldown=not is_auth_check):
            response = self._request_with_proxy(method, url, **kwargs)
            record_steam_market_response(response)
            return response

def create_steam_session(*, verify: bool = False, allow_auth_during_cooldown: bool = False) -> requests.Session:
    session = _SteamSession(allow_auth_during_cooldown=allow_auth_during_cooldown)
    session.verify = verify
    # Mount at the host root so market writes, reads, inventory, friends and
    # Steam Guard community requests all use the same curl_cffi transport.
    session.mount("https://steamcommunity.com/", _SteamCommunityAdapter(session.cookies))
    return session

def get_market(url, *, headers=None, cookies=None, proxies=None, timeout=15, verify=False):
    with create_steam_session(verify=verify) as session:
        if cookies:
            session.cookies.update(cookies)
        kwargs = {"headers": headers, "timeout": timeout}
        if proxies is not None:
            kwargs["proxies"] = proxies
        return session.get(url, **kwargs)


def parse_cookies(cookie_str: str) -> dict:
    out = {}
    for item in cookie_str.split(";"):
        s = item.strip()
        if "=" in s:
            k, _, v = s.partition("=")
            out[k.strip()] = v.strip()
    return out
def steam_login_cookie_expired(cookie_str: str) -> bool:
    """Reject locally expired JWTs; unverified claims never establish validity."""
    secure = parse_cookies(cookie_str or "").get("steamLoginSecure", "")
    token = unquote(secure).partition("||")[2]
    parts = token.split(".")
    if len(parts) != 3:
        return False
    try:
        payload = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        claims = json.loads(payload)
        expiry = claims.get("exp") if isinstance(claims, dict) else None
        return (
            type(expiry) in (int, float)
            and math.isfinite(expiry)
            and expiry <= time.time()
        )
    except (ValueError, TypeError, OverflowError):
        return False


def create_market_session(
    cookies_raw: str,
    steam_id: str,
    *,
    headers: Optional[dict] = None,
    verify: bool = False,
) -> requests.Session:
    session = create_steam_session(verify=verify)
    session.cookies.update(parse_cookies(cookies_raw))
    h = {**MARKET_HEADERS, "Referer": f"https://steamcommunity.com/profiles/{steam_id}/inventory/"}
    if headers:
        h.update(headers)
    session.headers.update(h)
    return session
