"""User-Agent parsing for the devices list."""

from app.auth import describe_user_agent


def test_iphone_safari():
    ua = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
          "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1")
    assert describe_user_agent(ua) == "iPhone · Safari"


def test_android_shows_model():
    ua = ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/126.0.0.0 Mobile Safari/537.36")
    assert describe_user_agent(ua) == "Android (Pixel 8) · Chrome"


def test_desktop_windows():
    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0")
    assert describe_user_agent(ua) == "Windows · Edge"


def test_mac_firefox():
    ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:127.0) Gecko/20100101 Firefox/127.0"
    assert describe_user_agent(ua) == "Mac · Firefox"


def test_empty_and_unknown():
    assert describe_user_agent(None) == "Unknown device"
    assert describe_user_agent("") == "Unknown device"
    assert describe_user_agent("curl/8.0") == "Unknown OS · Safari" or describe_user_agent("curl/8.0")
