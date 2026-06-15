"""
지직 (zzk) - 치지직 방송 자동 녹화기 (웹 버전)

FastAPI + 자가호스팅 웹 UI
- 채널 등록 → 방송 시작 대기 → 자동 녹화
- 중단되더라도 init.mp4 + .m4s + .m3u8 로 해당 시점까지 재생 가능
- 저장 구조: {channel}/{YYYY-MM-DD}/{title}.m3u8 + chunk/init.mp4 + chunk/segment_XXXXX.m4s
"""

from __future__ import annotations

import asyncio
import mimetypes
import shutil
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Optional

try:
    __version__ = version("zzk")
except PackageNotFoundError:
    __version__ = "0.0.0-dev"

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from .chzzk import ChzzkClient, LiveDetail, extract_channel_id_from_url
from .converter import (
    SUPPORTED_FORMATS,
    ConversionStatus,
    cancel_jobs_for_recording,
    clip_job_to_dict,
    existing_clip_output,
    existing_output,
    get_clip_job,
    get_job,
    job_to_dict,
    playlist_info,
    start_clip,
    start_conversion,
)
from .cookies import delete_cookies, get_cookie_status, save_cookie_file
from .db import (
    add_or_update_channel,
    create_recording,
    delete_channel,
    delete_recording,
    get_channel,
    get_latest_finished_recording,
    get_recording,
    get_setting,
    list_active_recordings,
    list_channels,
    list_recordings,
    reopen_recording,
    set_setting,
    update_channel_settings,
    update_recording,
)
from .paths import DEFAULT_OUTPUT_DIR, ensure_runtime_dirs
from .recorder import (
    ChzzkRecorder,
    RecordingState,
    broadcast_key_from_meta,
    read_recording_meta,
)

# ---------------- Global state ----------------

# One shared HTTP client for Chzzk API (set in lifespan before any requests)
chzzk_client: Optional[ChzzkClient] = None


def _chzzk() -> ChzzkClient:
    if chzzk_client is None:
        raise RuntimeError("Chzzk client not initialized")
    return chzzk_client


# Active recorders: channel_id -> {recorder, task, recording_id, state}
active: dict[str, dict] = {}

# After manual stop: skip auto-record while the same broadcast is still live.
# channel_id -> (live_id, open_date)
dismissed_live: dict[str, tuple[Optional[int], Optional[str]]] = {}

# Simple in-memory recent log ring (for UI)
LOG_BUFFER: list[dict] = []
LOG_MAX = 300


def log_event(channel_id: str, message: str, level: str = "info"):
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "channel_id": channel_id,
        "message": message,
        "level": level,
    }
    LOG_BUFFER.append(entry)
    if len(LOG_BUFFER) > LOG_MAX:
        del LOG_BUFFER[: len(LOG_BUFFER) - LOG_MAX]
    # also print for server console
    print(f"[zzk] [{channel_id[:8]}] {message}")


def _resolve_playlist_paths(base_path: str, playlist_path: str) -> tuple[str, str]:
    """Return base_path/playlist_path that exist on disk.

    Handles stale DB rows where an early prepare_paths claimed an empty session dir
    but the recorder later wrote files to a sibling *_1 directory.
    """
    bp = Path(base_path)
    if (bp / playlist_path).is_file():
        return base_path, playlist_path
    parent = bp.parent
    if not parent.is_dir():
        return base_path, playlist_path
    stem = bp.name
    candidates = sorted(
        (
            d
            for d in parent.iterdir()
            if d.is_dir()
            and (d.name == stem or d.name.startswith(f"{stem}_"))
            and (d / playlist_path).is_file()
        ),
        key=lambda d: d.name,
    )
    if candidates:
        return str(candidates[0]), playlist_path
    return base_path, playlist_path


def _resolve_recording_playlist(
    base_path: str, playlist_path: Optional[str]
) -> Optional[Path]:
    if not playlist_path:
        return None
    base_path, playlist_path = _resolve_playlist_paths(base_path, playlist_path)
    full = Path(base_path) / playlist_path
    return full if full.is_file() else None


def _recording_outputs(
    base_path: str,
    playlist_path: Optional[str],
    recording_id: int,
    output_dir: str,
) -> dict:
    out: dict[str, dict] = {}
    full_playlist = _resolve_recording_playlist(base_path, playlist_path)
    if not full_playlist:
        return out
    for fmt in sorted(SUPPORTED_FORMATS):
        existing = existing_output(full_playlist, fmt)
        job = get_job(recording_id, fmt)
        entry: dict = {
            "exists": existing is not None,
            "url": None,
            "conversion": None,
        }
        if existing:
            entry["url"] = _build_playlist_url(base_path, existing.name, output_dir)
        if job:
            entry["conversion"] = job_to_dict(job)
        out[fmt] = entry
    return out


def _build_playlist_url(
    base_path: str, playlist_path: Optional[str], output_dir: str = "recordings"
) -> Optional[str]:
    """Build a /recordings/... URL that works for both old (flat ts_chan dirs) and
    new ({channel}/{date}/{title}.m3u8) layouts. The static mount serves from the recordings root.
    """
    if not playlist_path:
        return None
    try:
        base_path, playlist_path = _resolve_playlist_paths(base_path, playlist_path)
        bp = Path(base_path)
        # Robust relative computation: locate the recordings root component in the path
        parts = list(bp.parts)
        out_name = Path(output_dir).name.lower()
        idx = -1
        for i, part in enumerate(parts):
            if part.lower() in (out_name, "recordings"):
                idx = i
                break
        if idx >= 0 and idx + 1 < len(parts):
            rel = "/".join(parts[idx + 1 :])
            return f"/recordings/{rel}/{playlist_path}"
        # Fallbacks
        if bp.name:
            return f"/recordings/{bp.name}/{playlist_path}"
        return f"/recordings/{playlist_path}"
    except Exception:
        return (
            f"/recordings/{Path(base_path).name}/{playlist_path}"
            if playlist_path
            else None
        )


def _output_roots() -> list[Path]:
    configured = get_setting("output_dir", str(DEFAULT_OUTPUT_DIR))
    roots: list[Path] = []
    seen: set[str] = set()
    for raw in (configured, str(DEFAULT_OUTPUT_DIR), "recordings"):
        try:
            resolved = Path(raw).resolve()
        except OSError:
            continue
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        roots.append(resolved)
    return roots


def _dir_size(path: Path) -> int:
    """Total bytes of all files under path (0 if missing)."""
    try:
        if not path.exists():
            return 0
        if path.is_file():
            return path.stat().st_size
        total = 0
        for entry in path.rglob("*"):
            if entry.is_file():
                try:
                    total += entry.stat().st_size
                except OSError:
                    pass
        return total
    except OSError:
        return 0


def _recording_size_bytes(
    base_path: str,
    playlist_path: Optional[str],
    recording_id: Optional[int] = None,
) -> int:
    """Disk usage for one recording session (live recorder dir preferred when active)."""
    if recording_id:
        for entry in active.values():
            if entry.get("recording_id") == recording_id:
                live_dir = entry["recorder"].state.base_dir
                if live_dir and Path(live_dir).exists():
                    return _dir_size(Path(live_dir))
                break
    if not base_path:
        return 0
    resolved_base = base_path
    if playlist_path:
        resolved_base, _ = _resolve_playlist_paths(base_path, playlist_path)
    return _dir_size(Path(resolved_base))


def _path_is_under_output_root(path: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    for root in _output_roots():
        if resolved == root:
            return False
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _delete_recording_files(base_path: str, playlist_path: Optional[str]) -> list[str]:
    deleted: list[str] = []
    if not base_path:
        return deleted

    resolved_base = base_path
    if playlist_path:
        resolved_base, _ = _resolve_playlist_paths(base_path, playlist_path)

    target = Path(resolved_base)
    if not _path_is_under_output_root(target):
        log_event("", f"녹화 파일 삭제 건너뜀 (경로 검증 실패): {target}", level="warn")
        return deleted

    target = target.resolve()
    if target.is_dir():
        shutil.rmtree(target)
        deleted.append(str(target))
    elif target.is_file():
        target.unlink()
        deleted.append(str(target))
    return deleted


def _broadcast_key(detail: LiveDetail) -> tuple[Optional[int], Optional[str]]:
    return (detail.live_id, detail.open_date)


def _is_dismissed_broadcast(channel_id: str, detail: LiveDetail) -> bool:
    """True if this live session was manually stopped and broadcast info is unchanged."""
    key = dismissed_live.get(channel_id)
    if key is None:
        return False
    return _broadcast_key(detail) == key


def _find_resumable_recording(
    channel_id: str, detail: LiveDetail
) -> Optional[tuple[int, Path, str, dict]]:
    """Return (recording_id, base_dir, playlist_path, meta) if the last session matches this live."""
    prev = get_latest_finished_recording(channel_id)
    if not prev or not prev.base_path or not prev.playlist_path:
        return None

    base_path, playlist_path = _resolve_playlist_paths(
        prev.base_path, prev.playlist_path
    )
    base_dir = Path(base_path)
    pl_file = base_dir / playlist_path
    if not pl_file.is_file():
        return None

    meta = read_recording_meta(base_dir) or {}
    prev_key = broadcast_key_from_meta(meta)
    cur_key = _broadcast_key(detail)
    if prev_key != (None, None) and prev_key == cur_key:
        return prev.id, base_dir, playlist_path, meta

    # Legacy sessions without live_id: fall back to title when both are non-empty
    if prev_key == (None, None) and meta.get("live_title") and detail.live_title:
        if meta.get("live_title") == detail.live_title:
            return prev.id, base_dir, playlist_path, meta

    return None


# ---------------- Active recording cleanup (handles same-day re-lives / reconnects) ----------------


async def _trigger_auto_convert(recording_id: int):
    """Start background conversion when auto_convert setting is enabled."""
    if not get_setting("auto_convert", False):
        return
    rec = get_recording(recording_id)
    if not rec or rec.status == "recording":
        return
    fmt = str(get_setting("auto_convert_format", "mp4")).lower().strip()
    if fmt not in SUPPORTED_FORMATS:
        fmt = "mp4"
    delete_segs = bool(get_setting("auto_convert_delete_segments", False))
    playlist = _resolve_recording_playlist(rec.base_path, rec.playlist_path)
    if not playlist:
        log_event(
            rec.channel_id,
            f"자동 변환 건너뜀 — 재생 목록 없음 (#{recording_id})",
            "warn",
        )
        return
    try:
        await start_conversion(
            recording_id,
            fmt,
            playlist,
            ffmpeg_path=get_setting("ffmpeg_path"),
            delete_segments=delete_segs,
        )
        log_event(
            rec.channel_id,
            f"녹화 #{recording_id} 자동 {fmt.upper()} 변환 시작"
            + (" (완료 후 세그먼트 삭제)" if delete_segs else ""),
        )
    except Exception as e:
        log_event(rec.channel_id, f"자동 변환 실패 (#{recording_id}): {e}", "error")


async def _cleanup_active_if_finished(channel_id: str):
    """If the recorder task for this channel has completed (natural end, error, or live ended),
    finalize its DB row and remove from active so monitor can start a fresh session later.
    Safe to call repeatedly.
    """
    entry = active.get(channel_id)
    if not entry:
        return
    task = entry.get("task")
    if task and task.done():
        recorder: ChzzkRecorder = entry["recorder"]
        rec_id = entry.get("recording_id")
        try:
            if rec_id:
                status = (
                    "completed"
                    if recorder.state.ended_naturally
                    else "error"
                    if recorder.state.last_error
                    else "completed"
                )

                update_recording(
                    rec_id,
                    status=status,
                    ended_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    segment_count=recorder.state.segment_count,
                    total_duration=round(recorder.state.total_duration, 1),
                    error=recorder.state.last_error,
                )
                if recorder.state.ended_naturally:
                    asyncio.create_task(
                        _trigger_auto_convert(rec_id),
                        name=f"zzk-auto-convert-{rec_id}",
                    )
        except Exception:
            pass
        active.pop(channel_id, None)
        log_event(channel_id, "녹화 세션 종료됨 (자연 종료 / 오류 / 방송 종료)")


async def _watch_recorder(channel_id: str):
    """Background watcher: await the recorder's task, then cleanup.
    This makes natural completions (live ended, errors, etc.) promptly remove the active entry
    without waiting for the next monitor tick.
    """
    try:
        entry = active.get(channel_id)
        if not entry:
            return
        task = entry.get("task")
        if task:
            try:
                await task
            except Exception:
                # Recorder already stores last_error and finalizes its own files.
                pass
        await _cleanup_active_if_finished(channel_id)
    except Exception:
        pass


# ---------------- Background monitor ----------------

MONITOR_INTERVAL = 60  # seconds


async def monitor_loop():
    """Periodically checks registered channels and starts auto-recording when live."""
    while True:
        try:
            # First, reap any finished recorders so we can start fresh sessions for re-lives / reconnects
            for cid in list(active.keys()):
                await _cleanup_active_if_finished(cid)

            channels = list_channels()
            for ch in channels:
                if not ch.auto_record:
                    continue
                if ch.channel_id in active:
                    continue  # already recording

                try:
                    detail = await _chzzk().get_live_detail(ch.channel_id)
                except Exception:
                    detail = None

                if not detail or detail.status != "OPEN":
                    continue

                if _is_dismissed_broadcast(ch.channel_id, detail):
                    continue

                log_event(
                    ch.channel_id, f"방송 감지 → 녹화 시작 (quality={ch.quality})"
                )
                await start_recording_for_channel(ch.channel_id, triggered_by="monitor")
        except Exception as e:
            log_event("system", f"monitor error: {e}", "error")

        await asyncio.sleep(MONITOR_INTERVAL)


async def start_recording_for_channel(
    channel_id: str, triggered_by: str = "manual"
) -> dict:
    ch = get_channel(channel_id)
    if not ch:
        raise ValueError("등록되지 않은 채널입니다.")

    if triggered_by == "monitor":
        try:
            detail = await _chzzk().get_live_detail(channel_id)
            if detail and _is_dismissed_broadcast(channel_id, detail):
                return {"status": "dismissed"}
        except Exception:
            pass

    if channel_id in active:
        return {"status": "already_recording"}

    # Opportunistic cleanup in case a previous recorder for this channel finished
    # between the last monitor tick and this manual/force start.
    await _cleanup_active_if_finished(channel_id)
    if channel_id in active:
        return {"status": "already_recording"}

    # Prepare recorder
    output_root = Path(get_setting("output_dir", "recordings"))
    output_root.mkdir(parents=True, exist_ok=True)

    async def on_progress(state: RecordingState):
        # keep DB in sync periodically
        rec_id = active.get(channel_id, {}).get("recording_id")
        if rec_id:
            update_recording(
                rec_id,
                segment_count=state.segment_count,
                total_duration=round(state.total_duration, 1),
                playlist_path=state.current_playlist,
                base_path=str(state.base_dir) if state.base_dir else None,
            )

    recorder = ChzzkRecorder(
        client=_chzzk(),
        channel_id=ch.channel_id,
        channel_name=ch.channel_name,
        quality=ch.quality,
        segment_minutes=ch.segment_minutes,
        output_root=output_root,
        on_progress=on_progress,
    )

    rec_id: int
    resumed = False
    detail: Optional[LiveDetail] = None
    try:
        detail = await _chzzk().get_live_detail(ch.channel_id)
    except Exception:
        detail = None

    resume_info = (
        _find_resumable_recording(ch.channel_id, detail)
        if detail and detail.status == "OPEN"
        else None
    )

    if resume_info and detail:
        prev_id, base_dir, playlist_path, meta = resume_info
        started_raw = meta.get("started_at") or ""
        try:
            started_at = datetime.fromisoformat(started_raw)
        except ValueError:
            started_at = None
        if recorder.load_resume_session(
            base_dir,
            playlist_path,
            live_title=detail.live_title or meta.get("live_title") or "",
            live_id=detail.live_id,
            open_date=detail.open_date,
            segment_count=int(meta.get("segment_count") or 0),
            total_duration=float(meta.get("total_duration_sec") or 0.0),
            started_at=started_at,
        ):
            recorder.channel_name = detail.channel.channel_name or recorder.channel_name
            recorder.state.channel_name = recorder.channel_name
            rec_id = prev_id
            resumed = True
            reopen_recording(rec_id)
            log_event(
                channel_id,
                f"같은 방송 이어서 녹화 (#{rec_id}, {playlist_path})",
            )
        else:
            resume_info = None

    if not resumed:
        if detail and detail.status == "OPEN":
            recorder.channel_name = detail.channel.channel_name or recorder.channel_name
            recorder.state.channel_name = recorder.channel_name
            recorder.state.live_id = detail.live_id
            recorder.state.open_date = detail.open_date
            recorder.prepare_paths(detail.live_title or "")

        base_path = str(recorder.state.base_dir)
        rec_id = create_recording(
            channel_id=ch.channel_id,
            channel_name=ch.channel_name,
            base_path=base_path,
            quality=ch.quality,
            playlist_path=recorder.state.current_playlist or None,
        )

    await recorder.start()

    # store
    active[channel_id] = {
        "recorder": recorder,
        "task": recorder._task,
        "recording_id": rec_id,
        "started_at": datetime.now(timezone.utc),
    }

    # update (in case the async loop set a slightly different title-based name)
    update_recording(rec_id, playlist_path=recorder.state.current_playlist)

    # Launch a detached watcher so the active entry is promptly cleaned when the recorder finishes naturally.
    # This enables immediate re-start for same-day re-lives / technical reconnects.
    asyncio.create_task(_watch_recorder(channel_id), name=f"zzk-watch-{channel_id}")

    log_event(
        channel_id,
        f"녹화 {'재개' if resumed else '시작'}됨 (trigger={triggered_by}, segment_minutes={ch.segment_minutes})",
    )
    return {
        "status": "resumed" if resumed else "started",
        "recording_id": rec_id,
    }


async def stop_recording_for_channel(channel_id: str, reason: str = "manual"):
    entry = active.get(channel_id)
    if not entry:
        return {"status": "not_recording"}

    recorder: ChzzkRecorder = entry["recorder"]
    rec_id = entry["recording_id"]

    await recorder.stop()

    if reason == "manual":
        try:
            detail = await _chzzk().get_live_detail(channel_id)
            if detail and detail.status == "OPEN":
                dismissed_live[channel_id] = _broadcast_key(detail)
        except Exception:
            pass

    # finalize DB
    update_recording(
        rec_id,
        status="stopped" if reason == "manual" else "completed",
        ended_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        segment_count=recorder.state.segment_count,
        total_duration=round(recorder.state.total_duration, 1),
        error=recorder.state.last_error,
    )

    active.pop(channel_id, None)
    log_event(channel_id, f"녹화 중지 ({reason})")
    return {"status": "stopped"}


# ---------------- FastAPI app ----------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    global chzzk_client
    ensure_runtime_dirs(get_setting("output_dir", str(DEFAULT_OUTPUT_DIR)))
    chzzk_client = ChzzkClient()

    # restore any previous "recording" rows as "stopped" on restart (they are not truly running)
    for r in list_active_recordings():
        update_recording(
            r.id,
            status="stopped",
            ended_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

    # start monitor
    monitor_task = asyncio.create_task(monitor_loop(), name="zzk-monitor")

    log_event("system", "지직 서버 시작")

    yield

    # shutdown
    monitor_task.cancel()
    for cid in list(active.keys()):
        try:
            await stop_recording_for_channel(cid, "shutdown")
        except Exception:
            pass
    if chzzk_client:
        await chzzk_client.close()
    log_event("system", "지직 서버 종료")


app = FastAPI(
    title="지직 (zzk) - 치지직 방송 다운로더",
    lifespan=lifespan,
)

# HLS segment types are not registered on all platforms; needed for browser/MSE playback.
mimetypes.add_type("video/mp4", ".m4s")
mimetypes.add_type("video/mp4", ".mp4")

# Static recordings (supports both old and new layouts: /recordings/{chan}/{date}/{title}.m3u8 etc.)
ensure_runtime_dirs()
recordings_dir = Path(get_setting("output_dir", str(DEFAULT_OUTPUT_DIR)))
ensure_runtime_dirs(recordings_dir)
app.mount(
    "/recordings",
    StaticFiles(directory=str(recordings_dir), html=False),
    name="recordings",
)

# Static assets (logo etc. placed in templates/ for convenience)
app.mount(
    "/static",
    StaticFiles(directory=str(Path(__file__).parent / "templates"), html=False),
    name="static",
)

# Templates (we will serve a single nice HTML page)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


# ---------------- Models ----------------


class ChannelCreate(BaseModel):
    url_or_id: str
    auto_record: bool = True
    quality: str = "best"
    segment_minutes: int = 0


class ChannelUpdate(BaseModel):
    auto_record: Optional[bool] = None
    quality: Optional[str] = None
    segment_minutes: Optional[int] = None


class SettingsUpdate(BaseModel):
    output_dir: Optional[str] = None
    ffmpeg_path: Optional[str] = None
    auto_convert: Optional[bool] = None
    auto_convert_format: Optional[str] = None
    auto_convert_delete_segments: Optional[bool] = None


class ConvertRequest(BaseModel):
    format: str  # mp4 | mkv
    overwrite: bool = False
    delete_segments: bool = False


class ClipRequest(BaseModel):
    format: str = "mp4"
    start_sec: float
    end_sec: float
    overwrite: bool = False


# ---------------- Routes: API ----------------


@app.get("/api/status")
async def api_status():
    return {
        "active_recordings": len(active),
        "registered_channels": len(list_channels()),
        "time": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/channels")
async def api_list_channels():
    chans = list_channels()
    result = []
    for c in chans:
        is_rec = c.channel_id in active
        rec_info = None
        if is_rec:
            st = active[c.channel_id]["recorder"].state
            rec_info = {
                "segment_count": st.segment_count,
                "total_duration": round(st.total_duration, 1),
                "size_bytes": _dir_size(st.base_dir),
                "current_playlist": st.current_playlist,
                "base_dir": str(st.base_dir),
                "started_at": active[c.channel_id]["started_at"].isoformat(),
            }
        result.append(
            {
                "id": c.id,
                "channel_id": c.channel_id,
                "channel_name": c.channel_name,
                "channel_image_url": c.channel_image_url,
                "auto_record": c.auto_record,
                "quality": c.quality,
                "segment_minutes": c.segment_minutes,
                "is_recording": is_rec,
                "recording": rec_info,
            }
        )
    return {"channels": result}


@app.post("/api/channels")
async def api_add_channel(payload: ChannelCreate):
    cid = extract_channel_id_from_url(payload.url_or_id)
    if not cid:
        raise HTTPException(400, "올바른 치지직 채널 ID 또는 URL을 입력하세요.")

    # fetch info for nice name
    info = await _chzzk().get_channel(cid)
    name = info.channel_name if info else cid
    img = info.channel_image_url if info else None

    ch_id = add_or_update_channel(
        channel_id=cid,
        channel_name=name,
        channel_image_url=img,
        auto_record=payload.auto_record,
        quality=payload.quality,
        segment_minutes=payload.segment_minutes,
    )

    # If currently live and auto, start immediately (best effort)
    if payload.auto_record:
        try:
            detail = await _chzzk().get_live_detail(cid)
            if detail and detail.status == "OPEN":
                await start_recording_for_channel(cid, triggered_by="add")
        except Exception:
            pass

    return {"ok": True, "channel_id": cid, "db_id": ch_id}


@app.patch("/api/channels/{channel_id}")
async def api_update_channel(channel_id: str, payload: ChannelUpdate):
    ch = get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "채널을 찾을 수 없습니다.")

    update_channel_settings(
        channel_id,
        auto_record=payload.auto_record,
        quality=payload.quality,
        segment_minutes=payload.segment_minutes,
    )

    # If currently recording and quality/segment changed, we don't hot-swap (user can stop+start)
    return {"ok": True}


@app.delete("/api/channels/{channel_id}")
async def api_delete_channel(channel_id: str):
    # stop if running
    if channel_id in active:
        await stop_recording_for_channel(channel_id, "channel_deleted")
    dismissed_live.pop(channel_id, None)
    delete_channel(channel_id)
    return {"ok": True}


@app.post("/api/channels/{channel_id}/record")
async def api_force_record(channel_id: str):
    ch = get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "채널을 찾을 수 없습니다.")
    dismissed_live.pop(channel_id, None)
    res = await start_recording_for_channel(channel_id, triggered_by="manual")
    return res


@app.post("/api/channels/{channel_id}/stop")
async def api_stop_record(channel_id: str):
    res = await stop_recording_for_channel(channel_id, "manual")
    return res


@app.get("/api/recordings")
async def api_list_recordings():
    recs = list_recordings(limit=300)
    out = []
    total_size = 0
    output_dir = get_setting("output_dir", "recordings")
    for r in recs:
        rel_playlist = _build_playlist_url(r.base_path, r.playlist_path, output_dir)
        size_bytes = _recording_size_bytes(r.base_path, r.playlist_path, r.id)
        total_size += size_bytes
        out.append(
            {
                "id": r.id,
                "channel_id": r.channel_id,
                "channel_name": r.channel_name,
                "started_at": r.started_at,
                "ended_at": r.ended_at,
                "status": r.status,
                "base_path": r.base_path,
                "playlist_url": rel_playlist,
                "segment_count": r.segment_count,
                "total_duration": round(r.total_duration, 1),
                "size_bytes": size_bytes,
                "quality": r.quality,
                "error": r.error,
                "is_active": r.id in [a.get("recording_id") for a in active.values()],
                "outputs": _recording_outputs(
                    r.base_path, r.playlist_path, r.id, output_dir
                ),
            }
        )
    return {"recordings": out, "total_size_bytes": total_size}


@app.delete("/api/recordings/{recording_id}")
async def api_delete_recording(recording_id: int):
    rec = get_recording(recording_id)
    if not rec:
        raise HTTPException(404, "녹화 기록을 찾을 수 없습니다.")
    if rec.status == "recording" or rec.id in [
        a.get("recording_id") for a in active.values()
    ]:
        raise HTTPException(400, "녹화 중인 항목은 삭제할 수 없습니다.")

    cancel_jobs_for_recording(recording_id)
    deleted_files = _delete_recording_files(rec.base_path, rec.playlist_path)
    if not delete_recording(recording_id):
        raise HTTPException(404, "녹화 기록을 찾을 수 없습니다.")

    log_event(rec.channel_id, f"녹화 #{recording_id} 삭제됨")
    return {"ok": True, "deleted_files": deleted_files}


@app.post("/api/recordings/{recording_id}/convert")
async def api_convert_recording(recording_id: int, payload: ConvertRequest):
    rec = get_recording(recording_id)
    if not rec:
        raise HTTPException(404, "녹화 기록을 찾을 수 없습니다.")
    if rec.status == "recording" or rec.id in [
        a.get("recording_id") for a in active.values()
    ]:
        raise HTTPException(400, "녹화 중인 항목은 변환할 수 없습니다.")

    fmt = payload.format.lower().strip()
    if fmt not in SUPPORTED_FORMATS:
        raise HTTPException(400, f"지원 형식: {', '.join(sorted(SUPPORTED_FORMATS))}")

    playlist = _resolve_recording_playlist(rec.base_path, rec.playlist_path)
    if not playlist:
        raise HTTPException(404, "재생 목록(.m3u8) 파일을 찾을 수 없습니다.")

    try:
        job = await start_conversion(
            recording_id,
            fmt,
            playlist,
            ffmpeg_path=get_setting("ffmpeg_path"),
            overwrite=payload.overwrite,
            delete_segments=payload.delete_segments,
        )
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except RuntimeError as e:
        raise HTTPException(503, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    output_dir = get_setting("output_dir", "recordings")
    result = job_to_dict(job)
    if job.output_path and job.status == ConversionStatus.COMPLETED:
        result["url"] = _build_playlist_url(
            rec.base_path, job.output_path.name, output_dir
        )
    log_event(rec.channel_id, f"녹화 #{recording_id} → {fmt.upper()} 변환 시작")
    return result


@app.get("/api/recordings/{recording_id}/convert")
async def api_convert_status(recording_id: int, format: str = "mp4"):
    rec = get_recording(recording_id)
    if not rec:
        raise HTTPException(404, "녹화 기록을 찾을 수 없습니다.")

    fmt = format.lower().strip()
    if fmt not in SUPPORTED_FORMATS:
        raise HTTPException(400, f"지원 형식: {', '.join(sorted(SUPPORTED_FORMATS))}")

    job = get_job(recording_id, fmt)
    playlist = _resolve_recording_playlist(rec.base_path, rec.playlist_path)
    output_dir = get_setting("output_dir", "recordings")

    if job:
        result = job_to_dict(job)
        if job.output_path and job.status == ConversionStatus.COMPLETED:
            result["url"] = _build_playlist_url(
                rec.base_path, job.output_path.name, output_dir
            )
        return result

    if playlist:
        existing = existing_output(playlist, fmt)
        if existing:
            return {
                "recording_id": recording_id,
                "format": fmt,
                "status": "completed",
                "output_path": str(existing),
                "url": _build_playlist_url(rec.base_path, existing.name, output_dir),
                "error": None,
                "started_at": None,
                "ended_at": None,
            }

    return {
        "recording_id": recording_id,
        "format": fmt,
        "status": "idle",
        "output_path": None,
        "url": None,
        "error": None,
        "started_at": None,
        "ended_at": None,
    }


@app.get("/api/recordings/{recording_id}/playlist")
async def api_recording_playlist(recording_id: int):
    rec = get_recording(recording_id)
    if not rec:
        raise HTTPException(404, "녹화 기록을 찾을 수 없습니다.")
    playlist = _resolve_recording_playlist(rec.base_path, rec.playlist_path)
    if not playlist:
        raise HTTPException(404, "재생 목록(.m3u8) 파일을 찾을 수 없습니다.")
    try:
        info = playlist_info(playlist)
    except Exception as e:
        raise HTTPException(500, f"재생 목록 파싱 실패: {e}") from e
    return {
        "recording_id": recording_id,
        "playlist_path": str(playlist),
        **info,
    }


@app.post("/api/recordings/{recording_id}/clip")
async def api_create_clip(recording_id: int, payload: ClipRequest):
    rec = get_recording(recording_id)
    if not rec:
        raise HTTPException(404, "녹화 기록을 찾을 수 없습니다.")
    if rec.status == "recording" or rec.id in [
        a.get("recording_id") for a in active.values()
    ]:
        raise HTTPException(400, "녹화 중인 항목은 클립을 만들 수 없습니다.")

    fmt = payload.format.lower().strip()
    if fmt not in SUPPORTED_FORMATS:
        raise HTTPException(400, f"지원 형식: {', '.join(sorted(SUPPORTED_FORMATS))}")

    playlist = _resolve_recording_playlist(rec.base_path, rec.playlist_path)
    if not playlist:
        raise HTTPException(404, "재생 목록(.m3u8) 파일을 찾을 수 없습니다.")

    try:
        job = await start_clip(
            recording_id,
            fmt,
            playlist,
            payload.start_sec,
            payload.end_sec,
            ffmpeg_path=get_setting("ffmpeg_path"),
            overwrite=payload.overwrite,
        )
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except RuntimeError as e:
        raise HTTPException(503, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    output_dir = get_setting("output_dir", "recordings")
    result = clip_job_to_dict(job)
    if job.output_path and job.status == ConversionStatus.COMPLETED:
        result["url"] = _build_playlist_url(
            rec.base_path, job.output_path.name, output_dir
        )
    log_event(
        rec.channel_id,
        f"클립 #{recording_id} {payload.start_sec:.0f}–{payload.end_sec:.0f}s → {fmt.upper()}",
    )
    return result


@app.get("/api/recordings/{recording_id}/clip")
async def api_clip_status(
    recording_id: int,
    start_sec: float,
    end_sec: float,
    format: str = "mp4",
):
    rec = get_recording(recording_id)
    if not rec:
        raise HTTPException(404, "녹화 기록을 찾을 수 없습니다.")

    fmt = format.lower().strip()
    if fmt not in SUPPORTED_FORMATS:
        raise HTTPException(400, f"지원 형식: {', '.join(sorted(SUPPORTED_FORMATS))}")

    job = get_clip_job(recording_id, start_sec, end_sec, fmt)
    playlist = _resolve_recording_playlist(rec.base_path, rec.playlist_path)
    output_dir = get_setting("output_dir", "recordings")

    if job:
        result = clip_job_to_dict(job)
        if job.output_path and job.status == ConversionStatus.COMPLETED:
            result["url"] = _build_playlist_url(
                rec.base_path, job.output_path.name, output_dir
            )
        return result

    if playlist:
        existing = existing_clip_output(playlist, start_sec, end_sec, fmt)
        if existing:
            return {
                "recording_id": recording_id,
                "format": fmt,
                "status": "completed",
                "output_path": str(existing),
                "url": _build_playlist_url(rec.base_path, existing.name, output_dir),
                "error": None,
                "started_at": None,
                "ended_at": None,
                "kind": "clip",
                "start_sec": start_sec,
                "end_sec": end_sec,
            }

    return {
        "recording_id": recording_id,
        "format": fmt,
        "status": "idle",
        "output_path": None,
        "url": None,
        "error": None,
        "started_at": None,
        "ended_at": None,
        "kind": "clip",
        "start_sec": start_sec,
        "end_sec": end_sec,
    }


@app.get("/api/logs")
async def api_logs(limit: int = 100):
    return {"logs": LOG_BUFFER[-limit:][::-1]}


@app.get("/api/settings")
async def api_get_settings():
    fmt = str(get_setting("auto_convert_format", "mp4")).lower().strip()
    if fmt not in SUPPORTED_FORMATS:
        fmt = "mp4"
    return {
        "output_dir": get_setting("output_dir", "recordings"),
        "ffmpeg_path": get_setting("ffmpeg_path", ""),
        "auto_convert": bool(get_setting("auto_convert", False)),
        "auto_convert_format": fmt,
        "auto_convert_delete_segments": bool(
            get_setting("auto_convert_delete_segments", False)
        ),
        "poll_interval": MONITOR_INTERVAL,
        "cookies": get_cookie_status(),
    }


@app.post("/api/settings")
async def api_update_settings(payload: SettingsUpdate):
    if payload.output_dir:
        p = Path(payload.output_dir)
        p.mkdir(parents=True, exist_ok=True)
        set_setting("output_dir", str(p))
    if payload.ffmpeg_path is not None:
        path = payload.ffmpeg_path.strip()
        if path:
            if not Path(path).is_file():
                raise HTTPException(400, "ffmpeg 실행 파일을 찾을 수 없습니다.")
            set_setting("ffmpeg_path", path)
        else:
            set_setting("ffmpeg_path", "")
    if payload.auto_convert is not None:
        set_setting("auto_convert", payload.auto_convert)
    if payload.auto_convert_format is not None:
        fmt = payload.auto_convert_format.lower().strip()
        if fmt not in SUPPORTED_FORMATS:
            raise HTTPException(
                400, f"지원 형식: {', '.join(sorted(SUPPORTED_FORMATS))}"
            )
        set_setting("auto_convert_format", fmt)
    if payload.auto_convert_delete_segments is not None:
        set_setting(
            "auto_convert_delete_segments", payload.auto_convert_delete_segments
        )
    return {"ok": True}


@app.post("/api/settings/cookies")
async def api_upload_cookies(file: UploadFile = File(...)):
    if not file.filename:
        raise HTTPException(400, "파일을 선택하세요.")
    content = await file.read()
    try:
        meta = save_cookie_file(content, file.filename)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if chzzk_client:
        chzzk_client.apply_cookies()
    log_event("system", f"쿠키 업로드됨 ({meta.get('count', 0)}개)")
    return {"ok": True, **meta}


@app.delete("/api/settings/cookies")
async def api_delete_cookies():
    delete_cookies()
    if chzzk_client:
        chzzk_client.apply_cookies()
    log_event("system", "쿠키 삭제됨")
    return {"ok": True}


# ---------------- UI ----------------


@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


# ---------------- CLI / runner (improved 제작성: prod-friendly defaults + flags) ----------------


def run():
    """Entry point for the `zzk` console script and `python -m app.main`.

    Provides a minimal CLI so that after `uv run zzk`, `uv tool install .`,
    or `pip install .` the command "zzk" starts the server with sane defaults.
    """
    import argparse
    import sys

    import uvicorn

    parser = argparse.ArgumentParser(
        prog="zzk",
        description="지직 (zzk) - 치지직 방송 자동 녹화기 (웹 UI)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""예시:
  zzk                  # 127.0.0.1:8000 (프로덕션 기본)
  zzk --port 9000      # 다른 포트
  zzk --reload         # 개발 모드 (자동 리로드)
  zzk --host 0.0.0.0 --port 8000
""",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="바인드 호스트 (기본: 127.0.0.1, Docker는 0.0.0.0 권장)",
    )
    parser.add_argument("--port", type=int, default=8000, help="포트 (기본: 8000)")
    parser.add_argument(
        "--reload", action="store_true", help="개발용 자동 리로드 활성화"
    )
    parser.add_argument(
        "--no-reload",
        action="store_true",
        help="리로드 비활성화 (기본값과 동일)",
    )

    args = parser.parse_args()

    reload = bool(args.reload) and not bool(args.no_reload)

    # When installed as package, users usually want stable run.
    # Reload=True is only useful during `uv run uvicorn ... --reload` from source.
    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=reload,
    )


if __name__ == "__main__":
    run()
