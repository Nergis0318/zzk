"""
Chzzk (치지직) API client for zzk.

- Metadata / live status / channel info via official-ish CHZZK APIs.
- Stream URL resolution is **streamlink only** (chzzk plugin).
"""

from __future__ import annotations

import orjson
import re
from dataclasses import dataclass
from typing import Any, Optional

import httpx

# Common headers to mimic browser (important for CDN)
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://chzzk.naver.com/",
    "Origin": "https://chzzk.naver.com",
}


@dataclass
class ChannelInfo:
    channel_id: str
    channel_name: str
    channel_image_url: Optional[str] = None
    verified: bool = False
    follower_count: int = 0
    open_live: bool = False


@dataclass
class LiveDetail:
    live_id: Optional[int]
    live_title: str
    status: str  # "OPEN" | "CLOSE" | ...
    chat_channel_id: Optional[str]
    channel: ChannelInfo
    open_date: Optional[str] = None


class ChzzkClient:
    def __init__(self, client: Optional[httpx.AsyncClient] = None):
        self._client = client or httpx.AsyncClient(
            headers=DEFAULT_HEADERS.copy(),
            timeout=httpx.Timeout(15.0, connect=10.0),
            follow_redirects=True,
        )
        self.apply_cookies()

    def apply_cookies(self) -> None:
        """Reload Cookie header from the uploaded cookie file (if any)."""
        from .cookies import get_cookie_string

        cookie_str = get_cookie_string()
        if cookie_str:
            self._client.headers["Cookie"] = cookie_str
        elif "Cookie" in self._client.headers:
            del self._client.headers["Cookie"]

    async def close(self):
        await self._client.aclose()

    async def _get_json(self, url: str, **kwargs) -> dict[str, Any]:
        r = await self._client.get(url, **kwargs)
        r.raise_for_status()
        data = orjson.loads(r.content)
        if isinstance(data, dict):
            return data
        return {}

    async def get_channel(self, channel_id: str) -> Optional[ChannelInfo]:
        url = f"https://api.chzzk.naver.com/service/v1/channels/{channel_id}"
        try:
            data = await self._get_json(url)
            if data.get("code") != 200:
                return None
            c = data.get("content") or {}
            return ChannelInfo(
                channel_id=c.get("channelId", channel_id),
                channel_name=c.get("channelName", ""),
                channel_image_url=c.get("channelImageUrl"),
                verified=bool(c.get("verifiedMark")),
                follower_count=int(c.get("followerCount") or 0),
                open_live=bool(c.get("openLive")),
            )
        except Exception:
            return None

    async def get_live_detail(self, channel_id: str) -> Optional[LiveDetail]:
        """Primary endpoint for live info."""
        url = f"https://api.chzzk.naver.com/service/v3.2/channels/{channel_id}/live-detail"
        try:
            data = await self._get_json(url)
            if data.get("code") != 200:
                return None
            c = data.get("content") or {}
            if not c:
                return None

            ch_raw = c.get("channel") or {}
            channel = ChannelInfo(
                channel_id=ch_raw.get("channelId", channel_id),
                channel_name=ch_raw.get("channelName", ""),
                channel_image_url=ch_raw.get("channelImageUrl"),
                verified=bool(ch_raw.get("verifiedMark")),
            )

            return LiveDetail(
                live_id=c.get("liveId"),
                live_title=c.get("liveTitle") or "",
                status=c.get("status") or "CLOSE",
                chat_channel_id=c.get("chatChannelId"),
                channel=channel,
                open_date=c.get("openDate"),
            )
        except Exception:
            return None

    async def get_live_status(self, channel_id: str) -> dict:
        """Polling status endpoint (used by official client too)."""
        url = (
            f"https://api.chzzk.naver.com/polling/v2/channels/{channel_id}/live-status"
        )
        try:
            data = await self._get_json(url)
            return data.get("content") or {}
        except Exception:
            return {}


def extract_channel_id_from_url(url: str) -> Optional[str]:
    """Accepts full chzzk url or raw channel id."""
    if not url:
        return None
    url = url.strip()
    # raw id
    if re.fullmatch(r"[0-9a-f]{32}", url):
        return url
    # https://chzzk.naver.com/live/affa78deac0b23d2046b8ed4856c1e62
    m = re.search(r"chzzk\.naver\.com/(?:live|channels?)/([0-9a-f]{32})", url)
    if m:
        return m.group(1)
    # fallback: any 32 hex
    m = re.search(r"([0-9a-f]{32})", url)
    if m:
        return m.group(1)
    return None


# ---------------- Streamlink integration (the only way we resolve streams now) ----------------


def get_stream_url_via_streamlink(
    channel_id: str, quality: str = "best"
) -> Optional[str]:
    """
    Resolve a CHZZK live stream using streamlink's official chzzk plugin.

    Returns a direct media playlist URL for the requested quality.
    The returned URL is a ready-to-consume HLS variant playlist that the
    resilient recorder can segment-download from.

    Benefits over custom scraping:
    - Maintained upstream against CHZZK site changes
    - Proper quality labels (including 1080p60 / 720p60)
    - Handles auth tokens, restricted (age/login) streams when cookies provided via streamlink config
    - Less code to maintain in zzk
    """
    if not channel_id:
        return None
    live_url = f"https://chzzk.naver.com/live/{channel_id}"
    try:
        from streamlink import Streamlink
        from streamlink.exceptions import NoPluginError, PluginError

        session = Streamlink()
        from .cookies import get_cookie_string

        cookie_str = get_cookie_string()
        if cookie_str:
            session.set_option("http-cookies", cookie_str)

        streams = session.streams(live_url)
        if not streams:
            return None

        q = (quality or "best").lower().strip()

        chosen = None
        if q in streams:
            chosen = streams[q]
        elif q in ("best", "highest", ""):
            chosen = streams.get("best")
        elif q in ("worst", "lowest"):
            chosen = streams.get("worst")
        else:
            # fuzzy match: "1080p" should hit "1080p60" etc.
            for name, strm in streams.items():
                nl = name.lower()
                if (
                    q in nl
                    or nl.startswith(q)
                    or q.replace("p", "") in nl.replace("p", "")
                ):
                    chosen = strm
                    break
            if chosen is None:
                chosen = streams.get("best")

        if chosen is None:
            # pick first "real" quality
            for k in streams:
                if k not in ("best", "worst"):
                    chosen = streams[k]
                    break
            if chosen is None and streams:
                chosen = next(iter(streams.values()))

        if chosen is None:
            return None

        url = getattr(chosen, "url", None)
        if isinstance(url, str) and url:
            return url
        return None
    except (NoPluginError, PluginError):
        return None
    except Exception:
        # Never let streamlink errors kill a recording attempt
        return None


async def resolve_stream_url(channel_id: str, quality: str = "best") -> Optional[str]:
    """
    Async-friendly wrapper around get_stream_url_via_streamlink.

    Runs the (blocking) streamlink resolution in a thread so it does not
    block the asyncio event loop.
    """
    import asyncio

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, lambda: get_stream_url_via_streamlink(channel_id, quality)
    )
