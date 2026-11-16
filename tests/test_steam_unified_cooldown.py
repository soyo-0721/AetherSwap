"""Regression coverage for the unified Steam market 429 cooldown."""

from types import SimpleNamespace
from unittest.mock import Mock

import requests
import pytest

from app import sell_pipeline, steam_delist
from steam import request_policy
from steam.session import create_steam_session


@pytest.fixture
def isolated_cooldown(monkeypatch):
    now = [1000.0]
    cooldown = request_policy.MarketCooldown(lambda: now[0])
    monkeypatch.setattr(request_policy, "steam_market_cooldown", cooldown)
    return SimpleNamespace(now=now, cooldown=cooldown)


def _response(status=429, retry_after="90"):
    response = requests.Response()
    response.status_code = status
    response._content = b"null"
    if retry_after is not None:
        response.headers["Retry-After"] = retry_after
    return response


def test_session_records_market_post_429_once(monkeypatch, isolated_cooldown):
    sent = _response()
    send = Mock(return_value=sent)
    monkeypatch.setattr(requests.Session, "request", send)

    with create_steam_session() as session:
        response = session.post(
            "https://steamcommunity.com/market/sellitem/",
            data={"assetid": "asset-1"},
            proxies={},
        )

    assert response.status_code == 429
    assert isolated_cooldown.cooldown.remaining() == 90
    assert isolated_cooldown.cooldown._failures == 1

    remaining = request_policy.record_steam_market_rate_limit(response)
    assert remaining == 90
    assert isolated_cooldown.cooldown._failures == 1


def test_listing_and_delist_share_the_same_cooldown():
    assert sell_pipeline._listing_cooldown is request_policy.steam_market_cooldown


def test_delist_is_blocked_before_opening_any_steam_session(monkeypatch, isolated_cooldown):
    isolated_cooldown.cooldown.defer("120")
    monkeypatch.setattr(
        steam_delist,
        "get_steam",
        lambda: {"cookies": "steamLoginSecure=fake; sessionid=test", "steam_id": "76561198000000000"},
    )
    create_session = Mock()
    monkeypatch.setattr(steam_delist, "create_market_session", create_session)

    ok, assetid, error = steam_delist.delist_item("asset-1", "Test Item")

    assert not ok
    assert assetid is None
    assert "冷却中" in error
    create_session.assert_not_called()


def test_delist_post_429_starts_shared_cooldown_without_retry(monkeypatch, isolated_cooldown):
    class FakeSession:
        def __init__(self):
            self.headers = {}
            self.cookies = requests.cookies.RequestsCookieJar()
            self.cookies.set("sessionid", "test")
            self.post_calls = 0
            self.closed = False

        def get(self, *args, **kwargs):
            return requests.Response()

        def post(self, *args, **kwargs):
            self.post_calls += 1
            return _response(429, "75")

        def close(self):
            self.closed = True

    session = FakeSession()
    monkeypatch.setattr(
        steam_delist,
        "get_steam",
        lambda: {"cookies": "steamLoginSecure=fake; sessionid=test", "steam_id": "76561198000000000"},
    )
    monkeypatch.setattr(steam_delist, "create_market_session", lambda *_args, **_kwargs: session)
    monkeypatch.setattr(
        steam_delist,
        "_get_mylistings_api",
        lambda _session: {
            "asset-1": {
                "listingid": "listing-1",
                "classid": "class-1",
                "instanceid": "0",
                "appid": "730",
                "contextid": "2",
            }
        },
    )
    monkeypatch.setattr(steam_delist, "_get_assetids_by_class_instance", lambda *_args: {"asset-before"})
    monkeypatch.setattr(steam_delist, "jittered_sleep", lambda *_args, **_kwargs: None)

    ok, assetid, error = steam_delist.delist_item("asset-1", "Test Item")

    assert not ok
    assert assetid is None
    assert "HTTP 429" in error
    assert "75s" in error
    assert session.post_calls == 1
    assert session.closed
    assert isolated_cooldown.cooldown.remaining() == 75
    assert isolated_cooldown.cooldown._failures == 1


def test_listing_429_blocks_followup_delist_without_opening_a_second_session(monkeypatch, isolated_cooldown):
    response = _response(429, "120")
    send = Mock(return_value=response)
    monkeypatch.setattr(requests.Session, "request", send)

    with create_steam_session() as session:
        result = session.post(
            "https://steamcommunity.com/market/sellitem/",
            data={"assetid": "asset-1"},
            proxies={},
        )

    assert result.status_code == 429
    assert isolated_cooldown.cooldown.remaining() == 120
    assert isolated_cooldown.cooldown._failures == 1

    monkeypatch.setattr(
        steam_delist,
        "get_steam",
        lambda: {"cookies": "steamLoginSecure=fake; sessionid=test", "steam_id": "76561198000000000"},
    )
    create_session = Mock()
    monkeypatch.setattr(steam_delist, "create_market_session", create_session)

    ok, assetid, error = steam_delist.delist_item("asset-1", "Test Item")

    assert not ok
    assert assetid is None
    assert "冷却中" in error
    assert isolated_cooldown.cooldown._failures == 1
    create_session.assert_not_called()
