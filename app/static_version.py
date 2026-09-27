"""Static asset cache busting.

`?v=<version>` is appended to /static/ URLs so browsers refetch assets after
every deploy. The version combines the current git commit (when available) with
the newest mtime among static files — either changing after a `git pull` is
enough to invalidate browser caches. No manual step is needed on deploy.
"""

import subprocess
from pathlib import Path

_STATIC_DIR = Path("static")


def _git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
        ).stdout.strip()
    except Exception:
        return ""


def _static_version() -> str:
    commit = _git_commit()
    try:
        newest = max(p.stat().st_mtime for p in _STATIC_DIR.rglob("*") if p.is_file())
        mtime = f"{int(newest):x}"
    except (OSError, ValueError):
        mtime = "0"
    return f"{commit or 'x'}-{mtime}"


# Computed once per process; a restart happens on every deploy (systemd restart).
STATIC_VERSION = _static_version()
