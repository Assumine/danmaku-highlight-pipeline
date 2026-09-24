#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Upload-grade local media validation with content-based result caching."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from fractions import Fraction
from pathlib import Path
from typing import Optional


SCRIPT_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = Path(os.environ.get("DANMAKU_WORKSPACE_ROOT", Path.cwd())).resolve()
FFPROBE = Path(os.environ.get("FFPROBE_BIN") or shutil.which("ffprobe") or SCRIPT_DIR / "ffmpeg" / "bin" / "ffprobe.exe")
CACHE_DIR = WORKSPACE_ROOT / "outputs" / "media_validation"
CACHE_VERSION = 3
CACHE_WAIT_SECONDS = 15 * 60
CACHE_LOCK_STALE_SECONDS = 2 * 60 * 60
MAX_AV_BOUNDARY_DELTA_SECONDS = 0.75

STRUCTURAL_ERROR_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"missing picture in access unit",
        r"non-existing pps",
        r"sps_id .* out of range",
        r"invalid nal unit",
        r"illegal reordering_of_pic_nums_idc",
        r"different chroma and luma bit depth",
        r"invalid, non monotonically increasing dts",
        r"non[- ]monotonically increasing dts",
        r"non-monotonous dts",
        r"packet corrupt",
        r"corrupt decoded frame",
        r"error while decoding",
        r"error parsing",
        r"header damaged",
        r"invalid data found when processing input",
    )
)


class MediaIntegrityError(RuntimeError):
    pass


def _effective_duration(payload: dict) -> float:
    fmt = payload.get("format") or {}
    duration = float(fmt.get("duration") or 0)
    start_time = float(fmt.get("start_time") or 0)
    if start_time > 0 and duration > start_time:
        duration -= start_time
    return duration


def _rate(value: object) -> float:
    text = str(value or "0/0")
    try:
        return float(Fraction(text))
    except (ValueError, ZeroDivisionError):
        return 0.0


def _packet_count(stream: dict) -> int:
    try:
        return int(stream.get("nb_read_packets") or 0)
    except (TypeError, ValueError):
        return 0


def _packet_timestamp(stream: dict, key: str) -> Optional[float]:
    try:
        value = stream.get(key)
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _structural_errors(stderr: str) -> list[str]:
    matches: list[str] = []
    for raw_line in stderr.splitlines():
        line = raw_line.strip()
        if line and any(pattern.search(line) for pattern in STRUCTURAL_ERROR_PATTERNS):
            if line not in matches:
                matches.append(line)
            if len(matches) >= 12:
                break
    return matches


def _scan_packets(
    path: Path,
    stream_indexes: set[int],
) -> tuple[dict[int, dict], list[str], list[str], int]:
    command = [
        str(FFPROBE),
        "-v", "warning",
        "-show_packets",
        "-show_entries", "packet=stream_index,dts_time",
        "-of", "csv=p=0",
        str(path),
    ]
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    assert process.stdout is not None
    assert process.stderr is not None

    structural_errors: list[str] = []
    stderr_tail: list[str] = []

    def drain_stderr() -> None:
        for raw_line in process.stderr:
            line = raw_line.strip()
            if not line:
                continue
            stderr_tail.append(line)
            if len(stderr_tail) > 40:
                del stderr_tail[0]
            if len(structural_errors) < 12 and any(pattern.search(line) for pattern in STRUCTURAL_ERROR_PATTERNS):
                if line not in structural_errors:
                    structural_errors.append(line)

    stderr_thread = threading.Thread(target=drain_stderr, name="ffprobe-stderr", daemon=True)
    stderr_thread.start()

    packet_stats = {
        index: {"count": 0, "first_dts": None, "last_dts": None}
        for index in stream_indexes
    }
    last_dts: dict[int, float] = {}
    timestamp_errors: list[str] = []
    for raw_line in process.stdout:
        fields = raw_line.strip().split(",", 1)
        if not fields:
            continue
        try:
            stream_index = int(fields[0])
        except ValueError:
            continue
        if stream_index not in packet_stats:
            continue
        stats = packet_stats[stream_index]
        stats["count"] += 1
        if len(fields) < 2 or fields[1] in ("", "N/A"):
            continue
        try:
            dts = float(fields[1])
        except ValueError:
            continue
        if stats["first_dts"] is None:
            stats["first_dts"] = dts
        previous = last_dts.get(stream_index)
        if previous is not None and dts <= previous:
            if len(timestamp_errors) < 12:
                timestamp_errors.append(
                    f"stream {stream_index} non-monotonically increasing DTS: {previous:.6f} >= {dts:.6f}"
                )
        last_dts[stream_index] = dts
        stats["last_dts"] = dts

    return_code = process.wait()
    stderr_thread.join()
    if return_code != 0 and not structural_errors:
        structural_errors.append("ffprobe packet scan failed: " + " | ".join(stderr_tail[-8:]))
    return packet_stats, structural_errors, timestamp_errors, return_code


def _check_expected_duration(actual: float, expected: Optional[float]) -> None:
    if expected is None:
        return
    tolerance = max(3.0, expected * 0.03)
    if abs(actual - expected) > tolerance:
        raise MediaIntegrityError(
            f"时长异常: 期望约 {expected:.3f}s，实际 {actual:.3f}s，容差 {tolerance:.3f}s"
        )


def validate_probe_payload(payload: dict, stderr: str = "", expected_duration: Optional[float] = None) -> dict:
    """Validate ffprobe's full packet-count result and return compact stats."""
    structural_errors = _structural_errors(stderr)
    if structural_errors:
        raise MediaIntegrityError("码流结构异常:\n" + "\n".join(structural_errors))

    duration = _effective_duration(payload)
    if duration <= 0:
        raise MediaIntegrityError(f"媒体时长无效: {duration}")
    _check_expected_duration(duration, expected_duration)

    streams = payload.get("streams") or []
    video_streams = [stream for stream in streams if stream.get("codec_type") == "video"]
    audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
    if not video_streams:
        raise MediaIntegrityError("没有可用视频流")

    video = video_streams[0]
    video_packets = _packet_count(video)
    fps = _rate(video.get("avg_frame_rate")) or _rate(video.get("r_frame_rate"))
    if video_packets <= 0:
        raise MediaIntegrityError("无法读出视频包")
    if fps > 0:
        minimum_video_packets = int(duration * fps * 0.70)
        if video_packets < minimum_video_packets:
            raise MediaIntegrityError(
                f"视频轨覆盖不足: {video_packets} 包，按 {fps:.3f}fps 和 {duration:.3f}s "
                f"至少应有约 {minimum_video_packets} 包"
            )

    audio_packets = 0
    av_start_delta = None
    av_end_delta = None
    if audio_streams:
        audio = audio_streams[0]
        audio_packets = _packet_count(audio)
        if audio_packets <= 0:
            raise MediaIntegrityError("存在音频流但无法读出音频包")
        sample_rate = int(audio.get("sample_rate") or 0)
        samples_per_packet = {
            "aac": 1024,
            "mp3": 1152,
            "opus": 960,
        }.get(str(audio.get("codec_name") or "").lower())
        if sample_rate > 0 and samples_per_packet:
            minimum_audio_packets = int(duration * sample_rate / samples_per_packet * 0.60)
            if audio_packets < minimum_audio_packets:
                raise MediaIntegrityError(
                    f"音频轨覆盖不足: {audio_packets} 包，按 {sample_rate}Hz 和 {duration:.3f}s "
                    f"至少应有约 {minimum_audio_packets} 包"
                )

        video_start = _packet_timestamp(video, "packet_first_dts")
        video_end = _packet_timestamp(video, "packet_last_dts")
        audio_start = _packet_timestamp(audio, "packet_first_dts")
        audio_end = _packet_timestamp(audio, "packet_last_dts")
        if None not in (video_start, video_end, audio_start, audio_end):
            av_start_delta = abs(video_start - audio_start)
            av_end_delta = abs(video_end - audio_end)
            if max(av_start_delta, av_end_delta) > MAX_AV_BOUNDARY_DELTA_SECONDS:
                raise MediaIntegrityError(
                    "音视频边界不同步: "
                    f"起点相差 {av_start_delta:.3f}s，终点相差 {av_end_delta:.3f}s，"
                    f"上限 {MAX_AV_BOUNDARY_DELTA_SECONDS:.3f}s"
                )

    return {
        "version": CACHE_VERSION,
        "duration": duration,
        "video_packets": video_packets,
        "audio_packets": audio_packets,
        "fps": fps,
        "av_start_delta": av_start_delta,
        "av_end_delta": av_end_delta,
    }


def _fingerprint(path: Path) -> str:
    stat = path.stat()
    digest = hashlib.sha256()
    digest.update(f"{stat.st_size}:{stat.st_mtime_ns}:".encode("ascii"))
    with path.open("rb") as stream:
        digest.update(stream.read(64 * 1024))
        if stat.st_size > 64 * 1024:
            stream.seek(max(0, stat.st_size - 64 * 1024))
            digest.update(stream.read(64 * 1024))
    return digest.hexdigest()


def _cache_path(fingerprint: str) -> Path:
    return CACHE_DIR / f"{fingerprint}.json"


def _read_cache(path: Path) -> Optional[dict]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("version") == CACHE_VERSION:
            return payload
    except (OSError, ValueError, TypeError):
        pass
    return None


def _write_cache(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def _acquire_scan_lock(cache_path: Path) -> tuple[Path, bool]:
    lock_path = cache_path.with_suffix(".lock")
    deadline = time.monotonic() + CACHE_WAIT_SECONDS
    while True:
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(descriptor, "w", encoding="ascii") as stream:
                stream.write(f"{os.getpid()}\n")
            return lock_path, True
        except FileExistsError:
            cached = _read_cache(cache_path)
            if cached:
                return lock_path, False
            try:
                if time.time() - lock_path.stat().st_mtime > CACHE_LOCK_STALE_SECONDS:
                    lock_path.unlink()
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() >= deadline:
                raise MediaIntegrityError(f"等待其他进程完成媒体校验超时: {lock_path}")
            time.sleep(2)


def validate_media_file(
    path: Path,
    expected_duration: Optional[float] = None,
    *,
    use_cache: bool = True,
) -> dict:
    """Scan every packet without decoding; cached results survive file renames."""
    media = Path(path).resolve()
    if not media.is_file() or media.stat().st_size < 1024 * 1024:
        raise MediaIntegrityError(f"媒体文件不存在或过小: {media}")
    if not FFPROBE.is_file():
        raise FileNotFoundError(f"找不到 ffprobe: {FFPROBE}")

    fingerprint = _fingerprint(media)
    cache_path = _cache_path(fingerprint)
    if use_cache:
        cached = _read_cache(cache_path)
        if cached:
            _check_expected_duration(float(cached["duration"]), expected_duration)
            return {**cached, "cached": True}

    lock_path: Optional[Path] = None
    owns_lock = False
    if use_cache:
        lock_path, owns_lock = _acquire_scan_lock(cache_path)
        if not owns_lock:
            cached = _read_cache(cache_path)
            if not cached:
                raise MediaIntegrityError(f"媒体校验锁已释放但缓存不存在: {cache_path}")
            _check_expected_duration(float(cached["duration"]), expected_duration)
            return {**cached, "cached": True}

    try:
        if use_cache:
            cached = _read_cache(cache_path)
            if cached:
                _check_expected_duration(float(cached["duration"]), expected_duration)
                return {**cached, "cached": True}

        command = [
            str(FFPROBE),
            "-v", "error",
            "-show_entries",
            "format=start_time,duration:stream=index,codec_type,codec_name,avg_frame_rate,r_frame_rate,sample_rate",
            "-of", "json",
            str(media),
        ]
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            raise MediaIntegrityError(f"ffprobe 全包扫描失败:\n{result.stderr.strip()[-3000:]}")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise MediaIntegrityError(f"ffprobe 未返回有效 JSON: {exc}") from exc

        stream_indexes = {
            int(stream["index"])
            for stream in payload.get("streams") or []
            if stream.get("codec_type") in ("video", "audio") and stream.get("index") is not None
        }
        packet_stats, structural_errors, timestamp_errors, packet_return_code = _scan_packets(media, stream_indexes)
        if packet_return_code != 0 and not structural_errors:
            structural_errors.append(f"ffprobe packet scan exited with {packet_return_code}")
        for stream in payload.get("streams") or []:
            index = stream.get("index")
            if index is not None:
                stats = packet_stats.get(int(index), {})
                stream["nb_read_packets"] = str(stats.get("count", 0))
                stream["packet_first_dts"] = stats.get("first_dts")
                stream["packet_last_dts"] = stats.get("last_dts")
        scan_errors = "\n".join(structural_errors + timestamp_errors)
        report = validate_probe_payload(payload, scan_errors, expected_duration)
        report["fingerprint"] = fingerprint
        _write_cache(cache_path, report)
        return {**report, "cached": False}
    finally:
        if owns_lock and lock_path is not None:
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
