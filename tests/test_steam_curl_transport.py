"""Regression coverage for the shared curl_cffi Steam Community transport."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from steam import request_policy, session as steam_session
from steam.request_policy import MarketCooldown, SteamMarketCooldownActive, SteamRequestGate
from steam.session import create_market_session, create_steam_session

STEAM_ID = "76561198000000000"
COOKIES = f"steamLoginSecure={STEAM_ID}%7C%7Ctest; sessionid=old-session"


class FakeCurlResponse:
    def __init__(self, status_code=200, body=b'{"success": true}', headers=None):
        self.status_code = status_code
        self.url = "https://steamcommunity.com/"
        self.headers = {"Content-Type": "application/json", **(headers or {})}
        self.encoding = "utf-8"
        self.content = body
        self.reason = "OK"
        self.elapsed = None
        self.http_version = "v1"
        self.primary_ip = "203.0.113.10"
        self.cookies = requests.cookies.RequestsCookieJar()


@pytest.fixture
def policy(monkeypatch):
    cooldown = MarketCooldown()
    gate = SteamRequestGate(min_interval=0)
    monkeypatch.setattr(request_policy, "steam_market_cooldown", cooldown)
    monkeypatch.setattr(steam_session, "steam_request_gate", gate)
    return cooldown


def fake_curl_transport(monkeypatch, replies):
    calls = []
    instances = []

    class FakeCurlSession:
        def __init__(self, **options):
            self.options = options
            self.cookies = requests.cookies.RequestsCookieJar()
            self.closed = False
            instances.append(self)

        def request(self, **kwargs):
            calls.append({"options": self.options, **kwargs})
            response = replies.pop(0)
            response.url = kwargs["url"]
            return response

        def close(self):
            self.closed = True

    monkeypatch.setattr(
        steam_session,
        "curl_requests",
        SimpleNamespace(Session=FakeCurlSession),
    )
    return calls, instances


def test_get_and_post_share_one_curl_session_and_same_browser_profile(monkeypatch, policy):
    calls, instances = fake_curl_transport(
        monkeypatch,
        [FakeCurlResponse(), FakeCurlResponse()],
    )
    with create_market_session(COOKIES, STEAM_ID) as session:
        get_result = session.get(
            "https://steamcommunity.com/inventory/76561198000000000/730/2",
            proxies={},
            timeout=5,
        )
        post_result = session.post(
            "https://steamcommunity.com/market/sellitem/",
            data={"sessionid": "old-session", "assetid": "123"},
            headers={"Origin": "https://steamcommunity.com"},
            proxies={},
            timeout=5,
        )

    assert get_result.status_code == 200
    assert post_result.status_code == 200
    assert len(instances) == 1
    assert instances[0].options["impersonate"] == "chrome120"
    assert instances[0].options["trust_env"] is False
    assert instances[0].options["default_headers"] is False
    assert [call["method"] for call in calls] == ["GET", "POST"]
    assert calls[1]["url"].endswith("/market/sellitem/")
    assert "assetid=123" in calls[1]["data"].decode()
    assert calls[1]["headers"]["Origin"] == "https://steamcommunity.com"
    assert "Chrome/156." in calls[1]["headers"]["User-Agent"]


def test_response_set_cookie_is_synced_back_to_requests_session_and_next_request(monkeypatch, policy):
    first = FakeCurlResponse()
    first.cookies.set_cookie(
        requests.cookies.create_cookie(
            name="sessionid",
            value="rotated-session",
            domain="steamcommunity.com",
            path="/",
        )
    )
    calls, _instances = fake_curl_transport(monkeypatch, [first, FakeCurlResponse()])

    with create_market_session(COOKIES, STEAM_ID) as session:
        session.get("https://steamcommunity.com/market/", proxies={}, timeout=5)
        assert session.cookies.get("sessionid") == "rotated-session"
        session.get("https://steamcommunity.com/market/mylistings/", proxies={}, timeout=5)

    assert "sessionid=rotated-session" in calls[1]["headers"]["Cookie"]
    assert calls[0]["headers"]["Cookie"]
    assert calls[1]["headers"]["Cookie"]


def test_429_from_curl_post_starts_shared_cooldown_and_blocks_followup(monkeypatch, policy):
    limited = FakeCurlResponse(status_code=429, body=b"null", headers={"Retry-After": "90"})
    calls, _instances = fake_curl_transport(monkeypatch, [limited])

    with create_steam_session() as session:
        first = session.post(
            "https://steamcommunity.com/market/sellitem/",
            data={"assetid": "123"},
            proxies={},
            timeout=5,
        )
        assert first.status_code == 429
        with pytest.raises(SteamMarketCooldownActive):
            session.post(
                "https://steamcommunity.com/market/removelisting/456",
                data={"sessionid": "test"},
                proxies={},
                timeout=5,
            )

    assert policy.remaining() == 90
    assert len(calls) == 1


def test_non_steam_hosts_continue_using_requests_transport(monkeypatch, policy):
    calls, _instances = fake_curl_transport(monkeypatch, [])
    sent = Mock()
    reply = requests.Response()
    reply.status_code = 200
    reply._content = b"ok"

    def fake_requests_send(*_args, **_kwargs):
        sent()
        return reply

    monkeypatch.setattr(
        steam_session.requests.adapters.HTTPAdapter,
        "send",
        fake_requests_send,
    )
    with create_steam_session() as session:
        result = session.get("https://example.com/", proxies={}, timeout=5)

    assert result.status_code == 200
    sent.assert_called_once()
    assert calls == []
