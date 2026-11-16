from unittest.mock import Mock

import requests

from steam.market import SELL_ITEM_URL
from steam.market import list_item
from steam.session import create_market_session


def test_market_session_sends_language_header_on_listing_request():
    with create_market_session("sessionid=test-session", "test-account") as session:
        request = session.prepare_request(
            requests.Request("POST", SELL_ITEM_URL, data={"sessionid": "test-session"})
        )

    assert request.headers["Accept-Language"] == "en-US,en;q=0.9"
    assert request.headers["X-Requested-With"] == "XMLHttpRequest"
    assert request.headers["Referer"].endswith("/test-account/inventory/")
    assert request.headers["Content-Type"] == "application/x-www-form-urlencoded"


def test_market_session_preserves_explicit_language_override():
    with create_market_session(
        "sessionid=test-session", "test-account", headers={"Accept-Language": "zh-CN"}
    ) as session:
        assert session.headers["Accept-Language"] == "zh-CN"


def test_listing_retains_retry_after_metadata():
    session = Mock()
    session.post.return_value = Mock(status_code=429, text="null", headers={"Retry-After": "120"})
    result = list_item(session, "test-session", 730, "2", "fake-asset", 100)
    assert result["status_code"] == 429
    assert result["retry_after"] == "120"
    assert session.post.call_args.kwargs["data"]["assetid"] == "fake-asset"

def test_listing_post_uses_origin_and_inventory_referer_without_xhr_marker():
    session = Mock()
    session.headers = {"Referer": "https://steamcommunity.com/profiles/test-account/inventory/"}
    session.post.return_value = Mock(status_code=200, text='{"success": true}', headers={})

    list_item(session, "test-session", 730, "2", "fake-asset", 100)

    headers = session.post.call_args.kwargs["headers"]
    assert headers["Origin"] == "https://steamcommunity.com"
    assert headers["Referer"].endswith("/test-account/inventory/")
    assert headers["X-Requested-With"] is None

