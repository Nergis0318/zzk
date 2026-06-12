"""Runtime path constants and directory bootstrap for zzk."""

from __future__ import annotations

from pathlib import Path

DATA_DIR = Path("data")
DEFAULT_OUTPUT_DIR = Path("recordings")
DB_PATH = DATA_DIR / "zzk.db"


def ensure_runtime_dirs(*extra: Path | str) -> None:
    """Create application directories when missing (idempotent)."""
    seen: set[str] = set()
    for raw in (DATA_DIR, DEFAULT_OUTPUT_DIR, *extra):
        if not raw:
            continue
        path = Path(raw)
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        path.mkdir(parents=True, exist_ok=True)
