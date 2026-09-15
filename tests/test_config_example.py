import re
from pathlib import Path

from app.config import Settings

ENV_EXAMPLE = Path(__file__).resolve().parent.parent / ".env.example"

# Active and commented-out assignments both count: `# GROQ_API_KEY=gsk_...`
# documents a setting that is optional.
_ASSIGNMENT_RE = re.compile(r"^#?\s*([A-Z][A-Z0-9_]*)=", re.MULTILINE)


def _documented_settings() -> set[str]:
    return set(_ASSIGNMENT_RE.findall(ENV_EXAMPLE.read_text(encoding="utf-8")))


def _declared_settings() -> set[str]:
    return {name.upper() for name in Settings.model_fields}


def test_env_example_documents_every_setting():
    missing = _declared_settings() - _documented_settings()
    assert not missing, f"Settings missing from .env.example: {sorted(missing)}"


def test_env_example_has_no_stale_settings():
    stale = _documented_settings() - _declared_settings()
    assert not stale, f".env.example lists settings that no longer exist: {sorted(stale)}"
