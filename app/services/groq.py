import logging
import re

import httpx

from app.config import settings
from app.models import User


GROQ_API_BASE = "https://api.groq.com/openai/v1"
GROQ_MODELS_URL = f"{GROQ_API_BASE}/models"

# Groq issues keys like gsk_<52 alphanumeric characters>; the pattern is kept
# deliberately loose so a format change at Groq does not lock users out.
GROQ_KEY_RE = re.compile(r"^gsk_[A-Za-z0-9]{20,}$")


class GroqAuthError(Exception):
    """Groq rejected the API key (HTTP 401/403)."""


def is_groq_key_format(key: str) -> bool:
    return bool(GROQ_KEY_RE.match(key))


def mask_key(key: str) -> str:
    if len(key) <= 8:
        return "gsk_..."
    return f"{key[:4]}...{key[-4:]}"


def resolve_groq_key(user: User | None) -> str | None:
    """The Groq key used for API calls: the user's own key, or a server-side fallback."""
    if user is not None and user.groq_key:
        return user.groq_key
    return settings.groq_api_key


async def validate_key(key: str) -> str:
    """Check a Groq key against the models endpoint.

    Returns "valid", "invalid", "throttled" (key works but hit a rate limit)
    or "unreachable" (Groq is down or the answer was unexpected).
    """
    try:
        async with httpx.AsyncClient(timeout=settings.groq_validation_timeout) as client:
            response = await client.get(
                GROQ_MODELS_URL,
                headers={"Authorization": f"Bearer {key}"},
            )
    except httpx.HTTPError as exc:
        logging.warning("Groq key validation failed: %s", exc)
        return "unreachable"

    if response.status_code == 200:
        return "valid"
    if response.status_code in (401, 403):
        return "invalid"
    if response.status_code == 429:
        return "throttled"

    logging.warning("Groq key validation got HTTP %s: %s", response.status_code, response.text[:200])
    return "unreachable"
