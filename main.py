"""
지직 (zzk) root entry (shim).

Prefer:
  uv run zzk
  uv run uvicorn app.main:app --reload

This file simply forwards to the real package CLI so that
`python main.py` also works as expected.
"""

from app.main import run

if __name__ == "__main__":
    run()
