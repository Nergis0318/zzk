---
Language: Korean
---

# AGENTS.md

Compact guidance for OpenCode (and similar) agents working in this repo. Every item is something an agent would likely get wrong or miss without reading multiple files.

## Primary commands (from README + pyproject.toml)

- Install (only): `uv sync`
- Dev server (recommended): `uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload`
- Dev server (short): `uv run uvicorn app.main:app --reload`
- Via installed script: `uv run zzk`
- Direct module: `python -m app.main` (calls the same `run()` entry)

**Only use uv.** Never run pip, pipx, poetry, or conda commands in this repo. uv is the single source of truth for environments and scripts.

## Package boundaries and entrypoints

- Real package is `app/`, not the root `main.py`.
  - `app/main.py` — FastAPI app, lifespan, monitor loop (45s), all API routes, templates. Exports `run()` for uvicorn/script.
  - `app/chzzk.py` — Chzzk metadata client + **streamlink-only** live URL resolution (via `get_stream_url_via_streamlink` / `resolve_stream_url`).
  - `app/recorder.py` — Resilient HLS segment downloader. Writes `.ts` immediately; appends + flushes `.m3u8` for crash-safety. Re-resolves stream every 300s.
  - `app/db.py` — SQLite (channels, recordings, settings). Calls `init_db()` at import time; creates `data/zzk.db` and tables on first use.
  - `app/templates/index.html` — Single-file UI (Tailwind CDN + vanilla JS + hls.js).
- Root `main.py` is a stub (`print("Hello from zzk!")`). Never the app entry.
- Console script defined in `pyproject.toml`: `zzk = "app.main:run"`.
- No monorepo; single Python package under `app/`.

## Toolchain and verification

- Uses `uv` exclusively (uv.lock present; `.python-version` + pyproject.toml). README emphasizes `uv sync` / `uv run`.
- Python: `.python-version` pins 3.14 (pyproject only says `>=3.11`).
- No pytest, ruff, mypy, pyright, pre-commit, or CI workflows in the repo.
- No `[tool.*]` sections in `pyproject.toml` for linting/typechecking.
- Verification = start the server and use the UI or curl the API routes. There are no automated tests to run.
- orjson is a hard dependency (used for settings JSON in DB, API responses in chzzk, metadata in recorder).

## Architecture / runtime facts agents commonly miss

- **Stream resolution is streamlink-only** (chzzk plugin). Legacy custom master/variant playlist parsing exists in chzzk.py but is dead code for recording. All live URL work must go through `resolve_stream_url` / streamlink.
- Long recordings refresh live detail + stream URL every `REFRESH_INTERVAL = 300` seconds (token expiry handling).
- Media playlist poll interval: `POLL_INTERVAL = 2.0` s.
- Storage layout (enforced in recorder.py):
  ```
  recordings/{sanitized_channel}/{YYYY-MM-DD}/{title}.m3u8
  recordings/{sanitized_channel}/{YYYY-MM-DD}/chunk/segment_00000.ts ...
  ```
- `segment_minutes` is stored in channels DB and UI, but current recorder uses a single `chunk/` dir (rotation logic was simplified/removed; see recorder.py:228 comment).
- Crash safety contract: every `.ts` is `write_bytes` immediately; root playlist is opened in append mode with line buffering and flushed after every segment.
- FastAPI JSON: plain dict/list returns + Pydantic models now serialize directly (via Pydantic/Rust). `default_response_class=ORJSONResponse` was removed; do not reintroduce it.
- `app/db.py` has import side effects: `init_db()` + data dir creation. Importing db or main will create `data/` and the DB file.
- Background monitor (in app/main.py) runs on FastAPI lifespan startup and polls every 45s (`MONITOR_INTERVAL`).
- Output dirs are intentionally gitignored: `data/`, `recordings/`, `*.db`, `*.sqlite*`, `*.sqlite3`. Recordings can be many GB.
- `uv.lock` appears both committed in the tree and listed under the "uv" section of `.gitignore` — treat current repo state as source of truth.

## Editing gotchas

- Stream handling changes belong in `app/chzzk.py` (streamlink wrapper) or the recorder's use of `resolve_stream_url`.
- DB schema changes have no migrations. Agents typically delete `data/zzk.db` (or the whole `data/`) during dev.
- Recorder must preserve immediate segment write + playlist append+flush behavior.
- The single HTML file (`app/templates/index.html`) contains all UI/JS logic; changes there do not require a build step.
- When adding API routes, prefer return type annotations or `response_model` for serialization (matches current FastAPI guidance).

## Data / environment layout (from code + .gitignore)

- DB: `data/zzk.db` (created automatically).
- Recordings: `recordings/` (created on first successful record).
- Settings are simple key-value in the `settings` table (orjson-encoded for non-strings).
- No `.env` loading or special environment files are used by the app.

## What does not exist (do not invent commands for these)

- Test suite, lint config, type checker config, CI, pre-commit hooks, Dockerfiles, task runners (just/make/taskfile), workspace-level opencode.json or instruction files.

Sources of truth used: README.md, pyproject.toml, .python-version, .gitignore, app/main.py, app/db.py, app/chzzk.py, app/recorder.py (and their constants/comments).

코드수정후에 `uvx ruff format .` 실행
