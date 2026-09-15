from app.config import settings
from app.models import User
from app.services.groq import is_groq_key_format, mask_key, resolve_groq_key

KEY = "gsk_" + "a" * 52


def test_accepts_groq_keys():
    assert is_groq_key_format(KEY)
    assert is_groq_key_format("gsk_" + "Ab1" * 10)


def test_rejects_anything_else():
    for value in ["", "sk-abc", "gsk_short", "invitation-key", KEY + "!", " " + KEY]:
        assert not is_groq_key_format(value)


def test_mask_hides_the_middle_of_the_key():
    assert mask_key(KEY) == f"gsk_...{KEY[-4:]}"
    assert mask_key("gsk_1") == "gsk_..."
    assert KEY not in mask_key(KEY)


def test_user_key_wins_over_the_server_fallback(monkeypatch):
    monkeypatch.setattr(settings, "groq_api_key", "gsk_" + "b" * 52)
    assert resolve_groq_key(User(groq_key=KEY)) == KEY


def test_falls_back_to_the_server_key_when_the_user_has_none(monkeypatch):
    monkeypatch.setattr(settings, "groq_api_key", "gsk_" + "b" * 52)
    assert resolve_groq_key(None) == "gsk_" + "b" * 52
    assert resolve_groq_key(User(groq_key="")) == "gsk_" + "b" * 52
