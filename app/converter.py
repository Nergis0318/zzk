"""Convert completed HLS recordings (.m3u8 + segments) to MP4/MKV via ffmpeg."""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

SUPPORTED_FORMATS = frozenset({"mp4", "mkv"})


class ConversionStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class PlaylistSegment:
    duration: float
    uri: str
    start_sec: float = 0.0


@dataclass
class ConversionJob:
    recording_id: int
    format: str
    status: ConversionStatus = ConversionStatus.PENDING
    output_path: Optional[Path] = None
    error: Optional[str] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    task: Optional[asyncio.Task] = None
    kind: str = "full"  # "full" | "clip"
    start_sec: Optional[float] = None
    end_sec: Optional[float] = None


_jobs: dict[tuple[int, str], ConversionJob] = {}
_clip_jobs: dict[str, ClipJob] = {}


@dataclass
class ClipJob:
    recording_id: int
    format: str
    start_sec: float
    end_sec: float
    status: ConversionStatus = ConversionStatus.PENDING
    output_path: Optional[Path] = None
    error: Optional[str] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    task: Optional[asyncio.Task] = None

    @property
    def key(self) -> str:
        return clip_job_key(self.recording_id, self.start_sec, self.end_sec, self.format)


def clip_job_key(recording_id: int, start_sec: float, end_sec: float, fmt: str) -> str:
    return f"{recording_id}:{int(start_sec)}:{int(end_sec)}:{fmt.lower()}"


def find_ffmpeg(custom_path: Optional[str] = None) -> Optional[str]:
    if custom_path:
        p = Path(custom_path)
        if p.is_file():
            return str(p)
    found = shutil.which("ffmpeg")
    if found:
        return found
    for candidate in (
        Path("C:/ffmpeg/bin/ffmpeg.exe"),
        Path("C:/Program Files/ffmpeg/bin/ffmpeg.exe"),
    ):
        if candidate.is_file():
            return str(candidate)
    return None


def output_path_for(playlist_path: Path, fmt: str) -> Path:
    return playlist_path.with_suffix(f".{fmt}")


def clip_output_path(
    playlist_path: Path, start_sec: float, end_sec: float, fmt: str
) -> Path:
    stem = playlist_path.stem
    return playlist_path.parent / f"{stem}_clip_{int(start_sec)}_{int(end_sec)}.{fmt}"


def existing_output(playlist_path: Path, fmt: str) -> Optional[Path]:
    out = output_path_for(playlist_path, fmt)
    if out.is_file() and out.stat().st_size > 0:
        return out
    return None


def existing_clip_output(
    playlist_path: Path, start_sec: float, end_sec: float, fmt: str
) -> Optional[Path]:
    out = clip_output_path(playlist_path, start_sec, end_sec, fmt)
    if out.is_file() and out.stat().st_size > 0:
        return out
    return None


def get_job(recording_id: int, fmt: str) -> Optional[ConversionJob]:
    return _jobs.get((recording_id, fmt))


def get_clip_job(
    recording_id: int, start_sec: float, end_sec: float, fmt: str
) -> Optional[ClipJob]:
    return _clip_jobs.get(clip_job_key(recording_id, start_sec, end_sec, fmt))


def job_to_dict(job: ConversionJob) -> dict:
    return {
        "recording_id": job.recording_id,
        "format": job.format,
        "status": job.status.value,
        "output_path": str(job.output_path) if job.output_path else None,
        "error": job.error,
        "started_at": job.started_at,
        "ended_at": job.ended_at,
        "kind": job.kind,
        "start_sec": job.start_sec,
        "end_sec": job.end_sec,
    }


def clip_job_to_dict(job: ClipJob) -> dict:
    return {
        "recording_id": job.recording_id,
        "format": job.format,
        "status": job.status.value,
        "output_path": str(job.output_path) if job.output_path else None,
        "error": job.error,
        "started_at": job.started_at,
        "ended_at": job.ended_at,
        "kind": "clip",
        "start_sec": job.start_sec,
        "end_sec": job.end_sec,
    }


def parse_playlist(playlist_path: Path) -> tuple[list[PlaylistSegment], float, bool]:
    """Parse a VOD m3u8 playlist. Returns (segments, total_duration, has_endlist)."""
    text = playlist_path.read_text(encoding="utf-8", errors="replace")
    lines = [l.strip() for l in text.splitlines()]
    segments: list[PlaylistSegment] = []
    cursor = 0.0
    target_dur = 10.0
    has_endlist = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("#EXT-X-TARGETDURATION:"):
            try:
                target_dur = float(line.split(":", 1)[1])
            except ValueError:
                pass
        elif line == "#EXT-X-ENDLIST":
            has_endlist = True
        elif line.startswith("#EXTINF:"):
            try:
                dur = float(line.split(":", 1)[1].split(",")[0])
            except ValueError:
                dur = target_dur
            if i + 1 < len(lines):
                uri = lines[i + 1]
                if uri and not uri.startswith("#"):
                    segments.append(
                        PlaylistSegment(duration=dur, uri=uri, start_sec=cursor)
                    )
                    cursor += dur
                    i += 1
        i += 1
    return segments, cursor, has_endlist


def playlist_info(playlist_path: Path) -> dict:
    segments, total, has_endlist = parse_playlist(playlist_path)
    return {
        "segment_count": len(segments),
        "total_duration": round(total, 3),
        "has_endlist": has_endlist,
        "segments": [
            {
                "index": idx,
                "uri": s.uri,
                "duration": round(s.duration, 3),
                "start_sec": round(s.start_sec, 3),
                "end_sec": round(s.start_sec + s.duration, 3),
            }
            for idx, s in enumerate(segments)
        ],
    }


def delete_hls_source(playlist_path: Path) -> list[str]:
    """Delete m3u8, chunk/ segments, and recording.json. Returns deleted paths."""
    deleted: list[str] = []
    base = playlist_path.parent
    chunk_dir = base / "chunk"
    if chunk_dir.is_dir():
        shutil.rmtree(chunk_dir)
        deleted.append(str(chunk_dir))
    meta = base / "recording.json"
    if meta.is_file():
        meta.unlink()
        deleted.append(str(meta))
    if playlist_path.is_file():
        playlist_path.unlink()
        deleted.append(str(playlist_path))
    return deleted


def _ffmpeg_args(
    ffmpeg: str,
    playlist: Path,
    output: Path,
    fmt: str,
    *,
    start_sec: Optional[float] = None,
    end_sec: Optional[float] = None,
) -> list[str]:
    args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
    if start_sec is not None and start_sec > 0:
        args.extend(["-ss", f"{start_sec:.3f}"])
    args.extend(["-i", str(playlist)])
    if end_sec is not None:
        if start_sec is not None and start_sec > 0:
            duration = end_sec - start_sec
            if duration > 0:
                args.extend(["-t", f"{duration:.3f}"])
        else:
            args.extend(["-to", f"{end_sec:.3f}"])
    args.extend(["-c", "copy"])
    if fmt == "mp4":
        args.extend(["-movflags", "+faststart"])
    args.append(str(output))
    return args


async def _run_ffmpeg(
    job: ConversionJob | ClipJob,
    ffmpeg: str,
    playlist: Path,
    output: Path,
    *,
    start_sec: Optional[float] = None,
    end_sec: Optional[float] = None,
    delete_segments: bool = False,
):
    job.status = ConversionStatus.RUNNING
    job.started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    job.error = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *_ffmpeg_args(
                ffmpeg,
                playlist,
                output,
                job.format,
                start_sec=start_sec,
                end_sec=end_sec,
            ),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            err = (stderr or b"").decode("utf-8", errors="replace").strip()
            job.status = ConversionStatus.FAILED
            job.error = err or f"ffmpeg exited with code {proc.returncode}"
            return
        if not output.is_file() or output.stat().st_size == 0:
            job.status = ConversionStatus.FAILED
            job.error = "변환 파일이 생성되지 않았습니다."
            return
        job.status = ConversionStatus.COMPLETED
        job.output_path = output
        if delete_segments and isinstance(job, ConversionJob):
            delete_hls_source(playlist)
    except FileNotFoundError:
        job.status = ConversionStatus.FAILED
        job.error = "ffmpeg를 찾을 수 없습니다. PATH에 추가하거나 설정에서 경로를 지정하세요."
    except Exception as e:
        job.status = ConversionStatus.FAILED
        job.error = str(e)
    finally:
        job.ended_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        job.task = None


async def start_conversion(
    recording_id: int,
    fmt: str,
    playlist_path: Path,
    *,
    ffmpeg_path: Optional[str] = None,
    overwrite: bool = False,
    delete_segments: bool = False,
) -> ConversionJob:
    fmt = fmt.lower()
    if fmt not in SUPPORTED_FORMATS:
        raise ValueError(f"지원하지 않는 형식입니다: {fmt}")

    if not playlist_path.is_file():
        raise FileNotFoundError("재생 목록(.m3u8) 파일을 찾을 수 없습니다.")

    key = (recording_id, fmt)
    existing = _jobs.get(key)
    if existing and existing.task and not existing.task.done():
        return existing

    output = output_path_for(playlist_path, fmt)
    if output.is_file() and not overwrite:
        job = ConversionJob(
            recording_id=recording_id,
            format=fmt,
            status=ConversionStatus.COMPLETED,
            output_path=output,
            started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ended_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        _jobs[key] = job
        if delete_segments:
            delete_hls_source(playlist_path)
        return job

    ffmpeg = find_ffmpeg(ffmpeg_path)
    if not ffmpeg:
        raise RuntimeError(
            "ffmpeg를 찾을 수 없습니다. https://ffmpeg.org 에서 설치 후 PATH에 추가하세요."
        )

    job = ConversionJob(recording_id=recording_id, format=fmt, output_path=output)
    _jobs[key] = job
    job.task = asyncio.create_task(
        _run_ffmpeg(
            job,
            ffmpeg,
            playlist_path,
            output,
            delete_segments=delete_segments,
        ),
        name=f"zzk-convert-{recording_id}-{fmt}",
    )
    return job


async def start_clip(
    recording_id: int,
    fmt: str,
    playlist_path: Path,
    start_sec: float,
    end_sec: float,
    *,
    ffmpeg_path: Optional[str] = None,
    overwrite: bool = False,
) -> ClipJob:
    fmt = fmt.lower()
    if fmt not in SUPPORTED_FORMATS:
        raise ValueError(f"지원하지 않는 형식입니다: {fmt}")

    if not playlist_path.is_file():
        raise FileNotFoundError("재생 목록(.m3u8) 파일을 찾을 수 없습니다.")

    if start_sec < 0:
        raise ValueError("시작 시각은 0 이상이어야 합니다.")
    if end_sec <= start_sec:
        raise ValueError("종료 시각은 시작 시각보다 커야 합니다.")

    _segments, total, _has_endlist = parse_playlist(playlist_path)
    if total > 0 and start_sec >= total:
        raise ValueError(f"시작 시각이 녹화 길이({total:.1f}초)를 초과합니다.")
    if total > 0:
        end_sec = min(end_sec, total)

    key = clip_job_key(recording_id, start_sec, end_sec, fmt)
    existing = _clip_jobs.get(key)
    if existing and existing.task and not existing.task.done():
        return existing

    output = clip_output_path(playlist_path, start_sec, end_sec, fmt)
    if output.is_file() and not overwrite:
        job = ClipJob(
            recording_id=recording_id,
            format=fmt,
            start_sec=start_sec,
            end_sec=end_sec,
            status=ConversionStatus.COMPLETED,
            output_path=output,
            started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ended_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        _clip_jobs[key] = job
        return job

    ffmpeg = find_ffmpeg(ffmpeg_path)
    if not ffmpeg:
        raise RuntimeError(
            "ffmpeg를 찾을 수 없습니다. https://ffmpeg.org 에서 설치 후 PATH에 추가하세요."
        )

    job = ClipJob(
        recording_id=recording_id,
        format=fmt,
        start_sec=start_sec,
        end_sec=end_sec,
        output_path=output,
    )
    _clip_jobs[key] = job
    job.task = asyncio.create_task(
        _run_ffmpeg(
            job,
            ffmpeg,
            playlist_path,
            output,
            start_sec=start_sec,
            end_sec=end_sec,
        ),
        name=f"zzk-clip-{recording_id}-{int(start_sec)}-{int(end_sec)}",
    )
    return job