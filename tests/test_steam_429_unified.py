"""Regression tests for app-wide Steam HTTP 429 coordination."""

from unittest.mock import Mock

import pytest
import requests

from app import sell_pipeline, steam_delist
from steam import request_policy, session as steam_session
from steam.request_policy import (
    MarketCooldown,
    SteamMarketCooldownActive,
    SteamRequestGate,
)


def _response(status_code=429, retry_after="120", body=b"null"):
    response = requests.Response()
    response.status_code = status_code
    response._content = body
    response.url = "https://steamcommunity.com/market/"
    if retry_after:
        response.headers["Retry-After"] = retry_after
    return response


@pytest.fixture
def shared_cooldown(monkeypatch):
    now = [1000.0]
    cooldown = MarketCooldown(clock=lambda: now[0])
    gate = SteamRequestGate(
        min_interval=0,
        clock=lambda: now[0],
        sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
    )
    monkeypatch.setattr(request_policy, "steam_market_cooldown", cooldown)
    monkeypatch.setattr(steam_session, "steam_request_gate", gate)
    monkeypatch.setattr(steam_session, "curl_requests", None)
    monkeypatch.setattr(sell_pipeline, "_listing_cooldown", cooldown)
    return cooldown, now


def _mock_http_adapter(monkeypatch, response):
    monkeypatch.setattr(
        steam_session.requests.adapters.HTTPAdapter,
        "send",
        lambda _self, _request, **_kwargs: response,
    )


def test_listing_cooldown_is_the_same_global_cooldown(shared_cooldown):
    cooldown, _now = shared_cooldown
    assert sell_pipeline._listing_cooldown is cooldown


def test_session_records_429_and_blocks_the_next_steam_request(
    monkeypatch, shared_cooldown
):
    cooldown, _now = shared_cooldown
    send = Mock(return_value=_response())
    monkeypatch.setattr(
        steam_session.requests.adapters.HTTPAdapter,
        "send",
        lambda _self, request, **kwargs: send(request, **kwargs),
    )

    with steam_session.create_steam_session() as session:
        first = session.post(
            "https://steamcommunity.com/market/sellitem/",
            data={"assetid": "123"},
            proxies={},
            timeout=1,
        )
        assert first.status_code == 429
        with pytest.raises(SteamMarketCooldownActive):
            session.post(
                "https://steamcommunity.com/market/removelisting/456",
                data={"sessionid": "test"},
                proxies={},
                timeout=1,
            )

    assert cooldown.remaining() == 120
    send.assert_called_once()


def test_same_429_response_is_not_counted_twice(shared_cooldown):
    cooldown, now = shared_cooldown
    response = _response(retry_after="120")

    request_policy.record_steam_market_response(response, cooldown=cooldown)
    now[0] += 10
    request_policy.record_steam_market_response(response, cooldown=cooldown)

    assert cooldown.remaining() == 110


def test_delist_uses_shared_session_and_propagates_429(
    monkeypatch, shared_cooldown
):
    cooldown, _now = shared_cooldown
    monkeypatch.setattr(
        steam_delist,
        "get_steam",
        lambda: {
            "cookies": "steamLoginSecure=token; sessionid=session",
            "steam_id": "76561198000000000",
        },
    )
    response = _response()
    _mock_http_adapter(monkeypatch, response)

    ok, new_assetid, error = steam_delist.delist_item("123", "Test Item")

    assert ok is False
    assert new_assetid is None
    assert "冷却中" in error or "HTTP 429" in error
    assert cooldown.remaining() == 120


def test_listing_429_updates_the_same_cooldown(
    monkeypatch, shared_cooldown
):
    cooldown, _now = shared_cooldown
    context = Mock()
    context.is_stop_requested.return_value = False

    limited = sell_pipeline._listing_rate_limited(
        context,
        {"status_code": 429, "text": "null", "retry_after": "90"},
    )

    assert limited is True
    assert cooldown.remaining() == 90
    assert any("HTTP 429" in call.args[0] for call in context.log.call_args_list)


def test_delist_does_not_send_any_request_during_an_existing_cooldown(
    monkeypatch, shared_cooldown
):
    cooldown, _now = shared_cooldown
    cooldown.defer("120")
    monkeypatch.setattr(
        steam_delist,
        "get_steam",
        lambda: {
            "cookies": "steamLoginSecure=token; sessionid=session",
            "steam_id": "76561198000000000",
        },
    )
    send = Mock()
    monkeypatch.setattr(
        steam_session.requests.adapters.HTTPAdapter,
        "send",
        lambda *_args, **_kwargs: send(),
    )

    ok, _new_assetid, error = steam_delist.delist_item("123", "Test Item")

    assert ok is False
    assert "冷却中" in error
    send.assert_not_called()


def test_friend_list_429_does_not_switch_proxy_and_retry(
    monkeypatch, shared_cooldown
):
    from app import gift_engine

    cooldown, _now = shared_cooldown
    send = Mock(return_value=_response())
    monkeypatch.setattr(
        steam_session.requests.adapters.HTTPAdapter,
        "send",
        lambda _self, request, **kwargs: send(request, **kwargs),
    )
    proxy_calls = []
    monkeypatch.setattr(
        "utils.proxy_manager.get_proxy_manager",
        lambda: type(
            "Manager",
            (),
            {
                "get_proxies_for_request": lambda self, failed=False: (
                    proxy_calls.append(failed)
                    or {"https": "http://proxy.example:8080"}
                )
            },
        )(),
    )

    with pytest.raises(SteamMarketCooldownActive):
        gift_engine.get_friend_list("steamLoginSecure=token", "76561198000000000")

    assert proxy_calls == [False]
    assert send.call_count == 1
    assert cooldown.remaining() == 120


def test_steam_guard_confirmation_uses_shared_cooldown(
    monkeypatch, shared_cooldown
):
    from app.steam_confirm import SteamConfirmer

    cooldown, _now = shared_cooldown
    cooldown.defer("120")
    send = Mock()
    monkeypatch.setattr(
        steam_session.requests.adapters.HTTPAdapter,
        "send",
        lambda *_args, **_kwargs: send(),
    )

    confirmer = SteamConfirmer(
        "YWFh",
        "device-id",
        "76561198000000000",
        "steamLoginSecure=token; sessionid=session",
    )
    try:
        ok, confirmations, error = confirmer.get_confirmations()
    finally:
        confirmer.session.close()

    assert ok is False
    assert confirmations == []
    assert "冷却中" in error
    send.assert_not_called()


def test_none_proxy_argument_still_uses_shared_proxy_policy(
    monkeypatch, shared_cooldown
):
    manager = type(
        "Manager",
        (),
        {
            "get_proxies_for_request": lambda self: {"https": "http://proxy.example:8080"},
            "should_always_use_proxy": lambda self: True,
            "should_use_proxy_on_failure": lambda self: False,
        },
    )()
    monkeypatch.setattr(
        "utils.proxy_manager.get_proxy_manager",
        lambda: manager,
    )
    sent = {}

    response = _response(status_code=200, retry_after=None, body=b"{}")

    def fake_send(_self, _request, **kwargs):
        sent.update(kwargs)
        return response

    monkeypatch.setattr(
        steam_session.requests.adapters.HTTPAdapter,
        "send",
        fake_send,
    )

    with steam_session.create_steam_session() as session:
        result = session.get(
            "https://steamcommunity.com/market/mylistings/",
            proxies=None,
            timeout=1,
        )

    assert result.status_code == 200
    assert sent["proxies"] == {"https": "http://proxy.example:8080"}
