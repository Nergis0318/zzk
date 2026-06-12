"""
Cookie file management for Chzzk authentication.

Supports Netscape cookies.txt, JSON (browser extensions), and header-style exports.
Stored under data/ and applied to API calls, streamlink, and segment downloads.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import orjson

from .db import get_setting, set_setting

COOKIE_DIR = Path("data")
COOKIE_PATH_KEY = "cookie_file"
COOKIE_META_KEY = "cookie_meta"
DEFAULT_COOKIE_FILE = COOKIE_DIR / "cookies.txt"
MAX_COOKIE_FILE_BYTES = 1_000_000


def _parse_json_cookies(data: Any) -> dict[str, str]:
    cookies: dict[str, str] = {}
    items: list[Any]
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        nested = data.get("cookies") or data.get("data")
        if isinstance(nested, list):
            items = nested
        elif "name" in data:
            items = [data]
        else:
            items = []
    else:
        return {}

    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("Name")
        if not name:
            continue
        value = item.get("value") or item.get("Value") or ""
        cookies[str(name)] = str(value)
    return cookies


def _parse_header_pairs(text: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for pair in text.split(";"):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        name, value = pair.split("=", 1)
        name = name.strip()
        if name:
            cookies[name] = value.strip()
    return cookies


def parse_cookies_from_text(text: str) -> dict[str, str]:
    """Parse cookies from Netscape, JSON, or header-style text."""
    text = text.strip()
    if not text:
        return {}

    if text.startswith("[") or text.startswith("{"):
        try:
            data = orjson.loads(text)
            parsed = _parse_json_cookies(data)
            if parsed:
                return parsed
        except Exception:
            pass

    cookies: dict[str, str] = {}
    netscape_found = False
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            netscape_found = True
            name, value = parts[5], parts[6]
            if name:
                cookies[name] = value
        elif "=" in line:
            cookies.update(_parse_header_pairs(line))

    if cookies:
        return cookies

    if not netscape_found and "=" in text:
        return _parse_header_pairs(text)

    return {}


def cookies_to_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def get_cookie_file_path() -> Optional[Path]:
    stored = get_setting(COOKIE_PATH_KEY)
    if stored:
        path = Path(stored)
        if path.is_file():
            return path
    if DEFAULT_COOKIE_FILE.is_file():
        return DEFAULT_COOKIE_FILE
    return None


def load_cookies() -> dict[str, str]:
    path = get_cookie_file_path()
    if not path:
        return {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        return parse_cookies_from_text(text)
    except Exception:
        return {}


def get_cookie_string() -> str:
    cookies = load_cookies()
    return cookies_to_header(cookies) if cookies else ""


def get_cookie_status() -> dict[str, Any]:
    path = get_cookie_file_path()
    meta = get_setting(COOKIE_META_KEY) or {}
    cookies = load_cookies() if path else {}
    return {
        "configured": bool(cookies),
        "count": len(cookies),
        "filename": meta.get("filename") or (path.name if path else None),
        "uploaded_at": meta.get("uploaded_at"),
        "has_nid": any(k.startswith("NID_") for k in cookies),
    }


def save_cookie_file(content: bytes, filename: str) -> dict[str, Any]:
    if len(content) > MAX_COOKIE_FILE_BYTES:
        raise ValueError("쿠키 파일이 너무 큽니다 (최대 1MB).")

    text = content.decode("utf-8", errors="replace")
    cookies = parse_cookies_from_text(text)
    if not cookies:
        raise ValueError(
            "쿠키를 인식할 수 없습니다. Netscape cookies.txt 또는 JSON 형식을 사용하세요."
        )

    COOKIE_DIR.mkdir(parents=True, exist_ok=True)
    ext = Path(filename).suffix if filename else ".txt"
    if ext.lower() not in (".txt", ".json"):
        ext = ".txt"
    dest = COOKIE_DIR / f"cookies{ext}"

    old = get_cookie_file_path()
    if old and old.resolve() != dest.resolve():
        try:
            old.unlink(missing_ok=True)
        except Exception:
            pass

    dest.write_bytes(content)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    meta = {
        "filename": filename or dest.name,
        "uploaded_at": now,
        "count": len(cookies),
    }
    set_setting(COOKIE_PATH_KEY, str(dest))
    set_setting(COOKIE_META_KEY, meta)
    return meta


def delete_cookies() -> None:
    path = get_cookie_file_path()
    if path:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass
    set_setting(COOKIE_PATH_KEY, "")
    set_setting(COOKIE_META_KEY, None)
