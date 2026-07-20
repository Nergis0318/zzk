"""
Resilient segmented HLS recorder for Chzzk live streams.

Stream resolution is streamlink-only (via its chzzk plugin).
The custom segment downloader + always-playable .m3u8 logic remains for resilience.

  Storage layout (per-broadcast session, supports multiple lives per day):
  {output_root}/{channel}/{YYYY-MM-DD}/{sanitized_title}/
      {title}.m3u8
      chunk/init.mp4 + segment_00000.m4s ...
      recording.json
  Multiple sessions on same day get unique subdirs (title/ or title_HHMMSS/ etc.)
  Legacy flat recordings (pre-2026-06) may have title.m3u8 + date-level chunk/ — still playable.

Key guarantees:
- Every downloaded segment is immediately written to disk.
- A .m3u8 playlist (named after the broadcast title) is kept up-to-date.
- If the process is killed at any moment, the .m3u8 + written segments are playable.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import httpx
import orjson

from .chzzk import DEFAULT_HEADERS, ChzzkClient, resolve_stream_url
from .cookies import get_cookie_string

# How often we poll the media playlist while live
POLL_INTERVAL = 2.0
# Re-resolve live detail (fresh tokens) every N seconds during long streams
REFRESH_INTERVAL = 300  # 5 minutes

# Reasonable UA + referer for segment downloads
SEGMENT_HEADERS = {
    **DEFAULT_HEADERS,
    "Accept": "*/*",
}


def get_segment_headers() -> dict[str, str]:
    headers = {**SEGMENT_HEADERS}
    if cookie_str := get_cookie_string():
        headers["Cookie"] = cookie_str
    return headers


@dataclass
class RecordingState:
    channel_id: str
    channel_name: str
    quality: str
    base_dir: Path
    started_at: datetime = field(default_factory=datetime.now)
    is_recording: bool = True
    live_title: str = ""
    live_id: Optional[int] = None
    open_date: Optional[str] = None
    playlist_filename: str = ""
    segment_count: int = 0
    total_duration: float = 0.0  # seconds
    last_error: Optional[str] = None
    ended_naturally: bool = False
    current_playlist: Optional[str] = (
        None  # filename of the main playable m3u8 (relative to base_dir)
    )
    resumed: bool = False


class ChzzkRecorder:
    """
    Downloads a live HLS stream segment-by-segment.

    Usage:
        rec = ChzzkRecorder(client, channel_id, quality="best", segment_minutes=30, output_root=Path("recordings"))
        await rec.start()
        ...
        await rec.stop()
    """

    def __init__(
        self,
        client: ChzzkClient,
        channel_id: str,
        channel_name: str = "",
        quality: str = "best",
        segment_minutes: int = 0,
        output_root: Path = Path("recordings"),
        on_progress=None,  # async callable(state)
    ):
        self.client = client
        self.channel_id = channel_id
        self.channel_name = channel_name or channel_id
        self.quality = quality
        self.segment_minutes = max(0, int(segment_minutes))
        self.output_root = Path(output_root)
        self.on_progress = on_progress

        self.state = RecordingState(
            channel_id=channel_id,
            channel_name=self.channel_name,
            quality=quality,
            base_dir=self.output_root,  # will be overridden by prepare_paths / _recording_loop
        )

        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._http = httpx.AsyncClient(
            headers=get_segment_headers(),
            timeout=httpx.Timeout(20.0, connect=10),
            follow_redirects=True,
        )

        # segment / playlist handles
        self._chunk_dir: Optional[Path] = None
        self._root_pl_path: Optional[Path] = None
        self._root_pl_file = None
        self._segment_counter = 0
        self._seen_segment_urls: set[str] = set()
        self._playlist_filename: str = ""
        self._has_map: bool = False
        self._map_local_name: str = "init.mp4"
        self._segment_ext: str = ".m4s"
        self._paths_locked: bool = False
        self._resuming: bool = False

    # ---------------- public API ----------------

    async def start(self):
        if self._task and not self._task.done():
            return
        self._stop_event.clear()
        self.state.is_recording = True
        # Directory + playlist setup is performed inside _recording_loop after
        # we successfully fetch live detail (to use proper channel/date/title layout).
        self._task = asyncio.create_task(
            self._run(), name=f"recorder-{self.channel_id}"
        )
        await self._emit()

    async def stop(self):
        self._stop_event.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=15)
            except asyncio.TimeoutError:
                self._task.cancel()
        await self._finalize_all()
        self.state.is_recording = False
        await self._emit()

    async def wait(self):
        if self._task:
            await self._task

    # ---------------- internals ----------------

    def prepare_paths(self, live_title: Optional[str] = None):
        """Compute the final base_dir and playlist filename using the requested layout:
        {channel}/{YYYY-MM-DD}/{sanitized_title}.m3u8
        Segments live under base_dir/chunk/init.mp4 + segment_XXXXX.m4s (fMP4/CMAF via #EXT-X-MAP)
        Call this as soon as channel_name + title are known (before or inside recording).
        """
        title = (
            (live_title or "").strip() or getattr(self.state, "live_title", "") or ""
        )
        self._compute_paths(title)

    def load_resume_session(
        self,
        base_dir: Path,
        playlist_filename: str,
        *,
        live_title: str = "",
        live_id: Optional[int] = None,
        open_date: Optional[str] = None,
        segment_count: int = 0,
        total_duration: float = 0.0,
        started_at: Optional[datetime] = None,
    ):
        """Continue an existing session (same broadcast reconnect / re-start)."""
        base_dir = Path(base_dir)
        playlist_filename = (playlist_filename or "").strip()
        if not base_dir.is_dir() or not playlist_filename:
            return False

        pl_path = base_dir / playlist_filename
        if not pl_path.is_file():
            return False

        self._resuming = True
        self.state.resumed = True
        self.state.base_dir = base_dir
        self.state.live_title = live_title or ""
        self.state.live_id = live_id
        self.state.open_date = open_date
        self.state.playlist_filename = playlist_filename
        self.state.current_playlist = playlist_filename
        self._playlist_filename = playlist_filename
        self._root_pl_path = pl_path
        if started_at:
            self.state.started_at = started_at
        self.state.segment_count = max(0, int(segment_count))
        self.state.total_duration = max(0.0, float(total_duration))
        self._paths_locked = True

        self._chunk_dir = base_dir / "chunk"
        if self._chunk_dir.is_dir():
            init_path = self._chunk_dir / self._map_local_name
            if init_path.is_file():
                self._has_map = True
            seg_nums: list[int] = []
            for p in self._chunk_dir.glob(f"segment_*{self._segment_ext}"):
                try:
                    seg_nums.append(int(p.stem.split("_", 1)[1]))
                except (IndexError, ValueError):
                    pass
            if seg_nums:
                self._segment_counter = max(seg_nums) + 1
            self.state.segment_count = max(self.state.segment_count, len(seg_nums))

        self._strip_endlist()
        return True

    def _sanitize_name(self, name: str) -> str:
        if not name:
            return ""
        safe = "".join(
            c if c.isalnum() or c in ("-", "_", ".") else "_" for c in name
        ).strip()
        while "__" in safe:
            safe = safe.replace("__", "_")
        return safe.strip("._-") or ""

    def _make_unique_playlist_name(self, base: str, directory: Path) -> str:
        candidate = f"{base}.m3u8"
        if not (directory / candidate).exists():
            return candidate
        ts = self.state.started_at.strftime("%H%M%S")
        candidate = f"{base}_{ts}.m3u8"
        if not (directory / candidate).exists():
            return candidate
        i = 1
        while True:
            candidate = f"{base}_{ts}_{i}.m3u8"
            if not (directory / candidate).exists():
                return candidate
            i += 1
            if i > 50:
                return f"{base}_{int(time.time())}.m3u8"

    def _compute_paths(self, live_title: str):
        # prepare_paths may run twice (early in main.py, again in _recording_loop).
        # Re-running would see our own empty session dir and pick a different _1 path.
        if self._paths_locked:
            return

        safe_channel = self._sanitize_name(self.channel_name or self.channel_id)
        if not safe_channel:
            safe_channel = self.channel_id[:8]
        date_str = self.state.started_at.strftime("%Y-%m-%d")
        date_dir = self.output_root / safe_channel / date_str
        date_dir.mkdir(parents=True, exist_ok=True)

        # Per-broadcast subdir so same-day re-lives / reconnects get their own session
        # e.g. recordings/채널/2026-06-12/방송제목/  or  방송제목_201530/
        safe_title = self._sanitize_name(live_title) or self.state.started_at.strftime(
            "%H%M%S"
        )
        sess_dir = date_dir / safe_title
        if sess_dir.exists():
            # make unique within the day (keep original for the first one)
            ts = self.state.started_at.strftime("%H%M%S")
            candidate = f"{safe_title}_{ts}"
            sess_dir = date_dir / candidate
            i = 1
            while sess_dir.exists() and i < 50:
                sess_dir = date_dir / f"{safe_title}_{ts}_{i}"
                i += 1
        sess_dir.mkdir(parents=True, exist_ok=True)

        self.state.base_dir = sess_dir

        m3u8_name = self._make_unique_playlist_name(safe_title, sess_dir)

        self.state.live_title = live_title or ""
        self.state.playlist_filename = m3u8_name
        self._playlist_filename = m3u8_name
        self._root_pl_path = sess_dir / m3u8_name
        self.state.current_playlist = m3u8_name
        self._paths_locked = True

    def _strip_endlist(self):
        if not self._root_pl_path or not self._root_pl_path.is_file():
            return
        try:
            text = self._root_pl_path.read_text(encoding="utf-8")
        except Exception:
            return
        if "#EXT-X-ENDLIST" not in text:
            return
        lines = [ln for ln in text.splitlines() if ln.strip() != "#EXT-X-ENDLIST"]
        self._root_pl_path.write_text(
            "\n".join(lines).rstrip() + "\n", encoding="utf-8"
        )

    def _open_root_playlist(self):
        if self._root_pl_file:
            return
        if not self._root_pl_path:
            return
        first = not self._root_pl_path.exists()
        self._root_pl_file = self._root_pl_path.open("a", encoding="utf-8", buffering=1)
        if first and not self._resuming:
            self._root_pl_file.write("#EXTM3U\n")
            self._root_pl_file.write("#EXT-X-VERSION:7\n")
            self._root_pl_file.write("#EXT-X-TARGETDURATION:10\n\n")
            title_part = f" - {self.state.live_title}" if self.state.live_title else ""
            self._root_pl_file.write(
                f"# {self.channel_name}{title_part} - started {self.state.started_at.isoformat()}\n\n"
            )
            self._root_pl_file.flush()

    # (chunk rotation removed for the new flat layout: all segments under a single chunk/ folder)

    def _append_to_playlists(self, duration: float, seg_relative: str):
        """Append segment entry to the main title-named playlist."""
        if self._root_pl_file:
            self._root_pl_file.write(f"#EXTINF:{duration:.3f},\n{seg_relative}\n")
            self._root_pl_file.flush()

    async def _run(self):
        try:
            await self._recording_loop()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.state.last_error = str(e)
            await self._emit()
        finally:
            await self._finalize_all()
            self.state.is_recording = False
            await self._emit()

    async def _recording_loop(self):
        # 1. Resolve initial live detail + variant
        detail = await self.client.get_live_detail(self.channel_id)
        if not detail or detail.status != "OPEN":
            self.state.last_error = "방송이 시작되지 않았습니다."
            return

        self.channel_name = detail.channel.channel_name or self.channel_name
        self.state.channel_name = self.channel_name

        # Use live title for the m3u8 filename per requested structure
        live_title = getattr(detail, "live_title", "") or ""
        self.state.live_id = getattr(detail, "live_id", None)
        self.state.open_date = getattr(detail, "open_date", None)
        if not self._paths_locked:
            self.prepare_paths(live_title)

        self._open_root_playlist()

        # segments dir (flat under session folder): init.mp4 + segment_XXXXX.m4s (fMP4/CMAF)
        self._chunk_dir = self.state.base_dir / "chunk"
        self._chunk_dir.mkdir(exist_ok=True)

        # Streamlink only: resolve the HLS media playlist URL via streamlink's chzzk plugin.
        variant_url = await resolve_stream_url(self.channel_id, self.quality)
        if not variant_url:
            self.state.last_error = "streamlink으로 재생 URL을 가져올 수 없습니다."
            return

        # 2. Emit so UI/DB sees the resolved playlist path early
        await self._emit()

        last_refresh = time.time()
        consecutive_errors = 0

        while not self._stop_event.is_set():
            # Refresh master/variant periodically (tokens can expire)
            now = time.time()
            if now - last_refresh > REFRESH_INTERVAL:
                try:
                    fresh = await self.client.get_live_detail(self.channel_id)
                    if fresh and fresh.status == "OPEN":
                        # Streamlink only re-resolve (tokens/URLs can expire)
                        new_variant = await resolve_stream_url(
                            self.channel_id, self.quality
                        )
                        if new_variant:
                            variant_url = new_variant
                    last_refresh = now
                except Exception:
                    pass

            try:
                # Fetch current media playlist
                pl_resp = await self._http.get(
                    variant_url, headers=get_segment_headers()
                )
                if pl_resp.status_code in (403, 401, 410):
                    # token probably expired -> force refresh next loop
                    await asyncio.sleep(1)
                    continue

                pl_resp.raise_for_status()
                pl_text = pl_resp.text

                segments, map_uri = self._parse_media_playlist(pl_text, variant_url)

                if not self._has_map:
                    init_url = map_uri or self._guess_init_url(segments, variant_url)
                    if init_url:
                        await self._ensure_init_segment(init_url)

                new_added = 0
                for dur, seg_full_url, seg_raw_name in segments:
                    if seg_full_url in self._seen_segment_urls:
                        continue

                    if not self._has_map:
                        # fMP4/CMAF requires init.mp4 before media segments are playable
                        break

                    # Download segment
                    try:
                        seg_resp = await self._http.get(
                            seg_full_url, headers=get_segment_headers()
                        )
                        seg_resp.raise_for_status()
                        seg_bytes = seg_resp.content
                    except Exception as se:
                        # One bad segment is not fatal for the whole recording
                        self.state.last_error = f"세그먼트 다운로드 실패: {se}"
                        await asyncio.sleep(0.5)
                        continue

                    seg_name = f"segment_{self._segment_counter:05d}{self._segment_ext}"
                    seg_path = self._chunk_dir / seg_name
                    seg_path.write_bytes(seg_bytes)

                    # Relative to the main playlist (which lives next to the chunk/ folder)
                    seg_rel = f"chunk/{seg_name}"

                    self._append_to_playlists(dur, seg_rel)

                    self._seen_segment_urls.add(seg_full_url)
                    self._segment_counter += 1
                    self.state.segment_count += 1
                    self.state.total_duration += dur
                    new_added += 1

                if new_added == 0:
                    # No new segments this poll (normal for live)
                    pass

                consecutive_errors = 0
                if new_added > 0:
                    self.state.last_error = None
                await self._emit()

            except httpx.HTTPStatusError as he:
                consecutive_errors += 1
                self.state.last_error = f"HTTP {he.response.status_code}"
                if consecutive_errors > 8:
                    # Give up this recording
                    break
            except Exception as e:
                consecutive_errors += 1
                self.state.last_error = str(e)[:200]
                if consecutive_errors > 12:
                    break

            # polite poll
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=POLL_INTERVAL)
            except asyncio.TimeoutError:
                pass

            # If the live has ended, the playlist will stop giving new segments.
            # We also do an explicit status check every ~30s.
            if int(time.time()) % 30 < 2:
                try:
                    st = await self.client.get_live_status(self.channel_id)
                    if (
                        st.get("status") == "ENDED"
                        or st.get("playableStatus") == "NONE"
                    ):
                        self.state.ended_naturally = True
                        break
                except Exception:
                    pass

        # loop ended
        await self._finalize_all()

    def _parse_media_playlist(self, text: str, base_url: str):
        segments = []
        map_uri = None
        lines = [l.strip() for l in text.splitlines()]
        i = 0
        target_dur = 10.0
        while i < len(lines):
            line = lines[i]
            if line.startswith("#EXT-X-TARGETDURATION:"):
                try:
                    target_dur = float(line.split(":")[1])
                except Exception:
                    pass
            elif line.startswith("#EXT-X-MAP:"):
                try:
                    if 'URI="' in line:
                        u = line.split('URI="', 1)[1].split('"', 1)[0]
                    elif "URI=" in line:
                        u = line.split("URI=", 1)[1].split(",", 1)[0].strip().strip('"')
                    else:
                        u = None
                    if u:
                        map_uri = urljoin(base_url, u)
                except Exception:
                    pass
            elif line.startswith("#EXTINF:"):
                try:
                    dur_str = line.split(":")[1].split(",")[0]
                    dur = float(dur_str)
                except Exception:
                    dur = target_dur
                if i + 1 < len(lines):
                    next_line = lines[i + 1]
                    if next_line and not next_line.startswith("#"):
                        full = urljoin(base_url, next_line)
                        segments.append((dur, full, next_line))
                        i += 1
            i += 1
        return segments, map_uri

    def _guess_init_url(
        self, segments: list[tuple[float, str, str]], variant_url: str
    ) -> Optional[str]:
        """Best-effort init.mp4 URL when the playlist omits #EXT-X-MAP."""
        candidates: list[str] = []
        variant_base = variant_url.rsplit("/", 1)[0] + "/"
        candidates.append(urljoin(variant_base, self._map_local_name))
        if segments:
            seg_base = segments[0][1].rsplit("/", 1)[0] + "/"
            candidates.append(urljoin(seg_base, self._map_local_name))
        seen: set[str] = set()
        for url in candidates:
            if url and url not in seen:
                seen.add(url)
                return url
        return None

    async def _ensure_init_segment(self, init_url: str) -> bool:
        if self._has_map or not self._chunk_dir:
            return self._has_map
        try:
            mr = await self._http.get(init_url, headers=get_segment_headers())
            mr.raise_for_status()
            map_path = self._chunk_dir / self._map_local_name
            map_path.write_bytes(mr.content)
            map_rel = f"chunk/{self._map_local_name}"
            if self._root_pl_file:
                self._root_pl_file.write(f'#EXT-X-MAP:URI="{map_rel}"\n')
                self._root_pl_file.flush()
            self._has_map = True
            self.state.last_error = None
            return True
        except Exception as me:
            self.state.last_error = f"MAP(init) 다운로드 실패: {me}"
            return False

    async def _finalize_all(self):
        # write ENDLIST to the main playlist
        if self._root_pl_file:
            try:
                self._root_pl_file.write("#EXT-X-ENDLIST\n")
                self._root_pl_file.flush()
            except Exception:
                pass
            try:
                self._root_pl_file.close()
            except Exception:
                pass
            self._root_pl_file = None

        # also write a small metadata file (only if we have a proper recording dir)
        try:
            if self.state.base_dir and self.state.base_dir != self.output_root:
                meta = self.state.base_dir / "recording.json"
                meta.write_text(
                    json_dumps_safe(
                        {
                            "channel_id": self.state.channel_id,
                            "channel_name": self.state.channel_name,
                            "live_title": self.state.live_title,
                            "live_id": self.state.live_id,
                            "open_date": self.state.open_date,
                            "started_at": self.state.started_at.isoformat(),
                            "ended_at": datetime.now().isoformat(),
                            "quality": self.state.quality,
                            "segment_count": self.state.segment_count,
                            "total_duration_sec": round(self.state.total_duration, 1),
                            "playlist": self.state.playlist_filename,
                            "segment_minutes": self.segment_minutes,
                            "resumed": self.state.resumed,
                        }
                    ),
                    encoding="utf-8",
                )
        except Exception:
            pass

        try:
            await self._http.aclose()
        except Exception:
            pass

    async def _emit(self):
        if self.on_progress:
            try:
                await self.on_progress(self.state)
            except Exception:
                pass


def json_dumps_safe(obj):
    return orjson.dumps(obj, option=orjson.OPT_INDENT_2).decode("utf-8")


def read_recording_meta(base_dir: Path) -> Optional[dict]:
    """Load recording.json from a session directory, if present."""
    meta_path = Path(base_dir) / "recording.json"
    if not meta_path.is_file():
        return None
    try:
        data = orjson.loads(meta_path.read_bytes())
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def broadcast_key_from_meta(meta: dict) -> tuple[Optional[int], Optional[str]]:
    live_id = meta.get("live_id")
    open_date = meta.get("open_date")
    try:
        live_id = int(live_id) if live_id is not None else None
    except (TypeError, ValueError):
        live_id = None
    open_date = str(open_date) if open_date else None
    return (live_id, open_date)
