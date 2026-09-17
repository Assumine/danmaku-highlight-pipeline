#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build highlight videos from the existing danmaku program points.

This tool is intentionally standalone. It reads a video and its ASS danmaku,
writes disposable media below ``outputs/media``, and keeps compact analysis
records below ``records``. It does not move, rename, delete, or upload source
files.
"""

from __future__ import annotations

import argparse
import array
import bisect
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import wave
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional, Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = Path(os.environ.get("DANMAKU_WORKSPACE_ROOT", Path.cwd())).resolve()
DEFAULT_MEDIA_ROOT = WORKSPACE_ROOT / "outputs" / "media"
DEFAULT_RECORDS_ROOT = WORKSPACE_ROOT / "records"
MODEL_HOME = Path(os.environ.get("DANMAKU_MODEL_HOME", WORKSPACE_ROOT / "models")).resolve()
FFMPEG = Path(os.environ.get("FFMPEG_BIN") or shutil.which("ffmpeg") or SCRIPT_DIR / "ffmpeg" / "bin" / "ffmpeg.exe")
FFPROBE = Path(os.environ.get("FFPROBE_BIN") or shutil.which("ffprobe") or SCRIPT_DIR / "ffmpeg" / "bin" / "ffprobe.exe")

PROGRAM_PRE_ROLL = 40.0
START_SPEECH_LOOKBACK = 10.0
START_SPEECH_LOOKAHEAD = 3.0
REACTION_WINDOW = 20.0
REACTION_MIN_HITS = 2
REACTION_LINK_GAP = 45.0
REACTION_WAVE_GAP = 10.0
DELAYED_REACTION_LOOKAHEAD = 35.0
DELAYED_REACTION_MIN_HITS = 4
SPEECH_REVIEW_BEFORE = 20.0
SPEECH_REVIEW_AFTER = 15.0
CUT_REVIEW_BEFORE = 12.0
SPEECH_TAIL_GUARD = 0.08
SHORT_TERMINAL_MAX_DURATION = 0.8
SHORT_TERMINAL_CONTINUATION_GAP = 0.85
TAIL_ADJUST_SEARCH_BACK = 2.0
TAIL_ADJUST_WINDOW = 0.080
TAIL_ADJUST_STEP = 0.010
TAIL_ADJUST_MIN_DROP_DB = 6.0
TAIL_ADJUST_MIN_SHIFT = 0.10
TAIL_ADJUST_MIN_ENDPOINT_DB = -16.0
MERGE_CLIP_GAP = 3.0
VIDEO_TRANSITION_DURATION = 0.6
AUDIO_TRANSITION_DURATION = 0.2
TIGHT_VIDEO_TRANSITION_DURATION = 0.4
TIGHT_AUDIO_TRANSITION_DURATION = 0.12
J_STYLE_AUDIO_TRANSITION_DURATION = 0.45
L_STYLE_AUDIO_TRANSITION_DURATION = 0.10
MONTAGE_END_EXTENSION = 0.6
MONTAGE_END_FADE_DURATION = 0.8
TRANSITION_ANALYSIS_WINDOWS = (2.0, 4.0, 6.0)
TRANSITION_ANALYSIS_DURATION = max(TRANSITION_ANALYSIS_WINDOWS)
TRANSITION_IMMEDIATE_WINDOW = 0.5
TRANSITION_AUDIO_SAMPLE_RATE = 16000
TRANSITION_AUDIO_BLOCK_SECONDS = 0.1
TRANSITION_USABLE_EDGE_RMS_DB = -16.0
TRANSITION_MIN_LEVEL_RANGE_DB = 4.0
TRANSITION_POLICY = "relative-audio-edge-v1"
ALGORITHM_VERSION = "highlight-editor-v14"
DECISION_MODE = "automatic-only"
SEEKABLE_CACHE_SCHEMA_VERSION = 1
SEEKABLE_CACHE_SAMPLE_BYTES = 1024 * 1024
SEEKABLE_CACHE_METADATA_SUFFIX = ".source.json"

PRODUCTION_RENDER_PROFILE = {
    "name": "production",
    "qsv_filter": "format=nv12",
    "cpu_filter": "format=yuv420p",
    "clip_qsv_quality": "18",
    "final_qsv_quality": "20",
    "clip_x264_crf": "16",
    "final_x264_crf": "18",
    "x264_preset": "medium",
    "audio_bitrate": "192k",
    "resolution": "source",
    "fps": "source",
}


def build_render_profile(preview_height: int = 0) -> dict[str, str]:
    profile = dict(PRODUCTION_RENDER_PROFILE)
    if preview_height <= 0:
        return profile
    if preview_height < 360 or preview_height % 2:
        raise ValueError("预览高度必须是大于等于 360 的偶数")
    profile.update(
        {
            "name": f"preview-{preview_height}p",
            "qsv_filter": (
                f"scale=-2:{preview_height}:flags=lanczos,format=nv12"
            ),
            "cpu_filter": (
                f"scale=-2:{preview_height}:flags=lanczos,format=yuv420p"
            ),
            "clip_qsv_quality": "20",
            "final_qsv_quality": "22",
            "clip_x264_crf": "19",
            "final_x264_crf": "21",
            "x264_preset": "fast",
            "audio_bitrate": "160k",
            "resolution": f"height={preview_height}",
        }
    )
    return profile

# Keep this list focused on immediate audience reactions. Ordinary discussion
# words are deliberately excluded: they should not make a highlight longer.
REACTION_TERMS = (
    "哈哈",
    "笑死",
    "笑不活",
    "笑麻",
    "笑晕",
    "绷不住",
    "蚌埠住",
    "乐死",
    "红温",
    "急了",
    "破防",
    "气死",
    "哦豁",
    "卧槽",
    "嘲笑",
    "呲牙笑",
    "菜就多练",
    "打一拳",
    "？？？",
)

INCOMPLETE_ENDINGS = (
    "但是",
    "然后",
    "因为",
    "所以",
    "结果",
    "而且",
    "不过",
    "我跟你说",
    "你知道吗",
    "你知道为什么吗",
)

@dataclass(frozen=True)
class Danmaku:
    time: float
    text: str


@dataclass(frozen=True)
class ProgramPoint:
    time: float
    trigger_time: float
    category: str
    rule_indexes: tuple[int, ...] = ()
    rule_keywords: tuple[str, ...] = ()
    end_hint: float = 0.0
    payoff_start: float = 0.0
    payoff_end: float = 0.0
    end_policy: str = "bilibili-reaction-wave"


@dataclass
class ClipTimelineGroup:
    index: int
    members: tuple[ProgramPoint, ...]
    max_gap: float
    selection_status: str = "pending"
    exclusion_reason: Optional[str] = None
    exclusion_evidence: list[str] = field(default_factory=list)

    @property
    def start(self) -> float:
        return min(point.time for point in self.members)

    @property
    def first_trigger(self) -> float:
        return self.members[0].trigger_time

    @property
    def last_trigger(self) -> float:
        return self.members[-1].trigger_time

    @property
    def end_hint(self) -> float:
        return max((point.end_hint for point in self.members), default=0.0)


@dataclass(frozen=True)
class ReactionBurst:
    start: float
    end: float
    hit_count: int


@dataclass
class Highlight:
    index: int
    start: float
    coarse_end: float
    final_end: float
    activity_start: float
    activity_end: float
    review_end: float
    categories: list[str] = field(default_factory=list)
    program_points: list[float] = field(default_factory=list)
    clip_group_ids: list[int] = field(default_factory=list)
    last_program_trigger: float = 0.0
    initial_start: float = 0.0
    start_basis: str = "fixed-40s-pre-roll"
    start_speech_text: str = ""
    tail_anchor_basis: str = "unassigned"
    end_basis: str = "unassigned"
    reaction_hits: int = 0
    speech_method: str = "not-run"
    speech_text: str = ""
    speech_confidence: str = "unreviewed"
    tail_adjustment_kind: str = "none"
    tail_adjustment_original_end: Optional[float] = None
    tail_adjustment_endpoint_db: Optional[float] = None
    tail_adjustment_valley_db: Optional[float] = None
    tail_adjustment_shift: Optional[float] = None
    speech_content_end: Optional[float] = None
    next_speech_start: Optional[float] = None

    @property
    def duration(self) -> float:
        return max(0.0, self.final_end - self.start)


@dataclass(frozen=True)
class SourceReferenceOverlay:
    clip_index: int
    source_part: int
    source_time: float
    output_start: float
    output_end: float
    text: str


@dataclass(frozen=True)
class MontageEnding:
    clip_index: int
    original_end: float
    final_end: float
    extension: float
    video_fade_duration: float
    audio_fade_duration: float
    speech_content_end: Optional[float]
    next_speech_start: Optional[float]
    reason: str


@dataclass
class AudioEdgeAnalysis:
    clip_index: int
    side: str
    immediate_rms_db: float
    score_2s: float
    score_4s: float
    score_6s: float
    level: str = "medium"
    intense_votes: int = 0

    @property
    def representative_score(self) -> float:
        return self.score_4s


@dataclass(frozen=True)
class TransitionPlan:
    boundary_index: int
    outgoing_clip_index: int
    incoming_clip_index: int
    kind: str
    video_duration: float
    audio_duration: float
    incoming_audio_trim: float
    outgoing_level: str
    incoming_level: str
    outgoing_immediate_rms_db: Optional[float]
    incoming_immediate_rms_db: Optional[float]
    reason: str


def format_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:05.2f}"


def format_whole_time(seconds: float) -> str:
    total = int(max(0.0, seconds))
    hours = total // 3600
    minutes = (total % 3600) // 60
    secs = total % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_identity(path: Path) -> dict[str, object]:
    """Build a cheap identity that catches same-name and same-duration replacements."""
    before = path.stat()
    with path.open("rb") as handle:
        head = handle.read(SEEKABLE_CACHE_SAMPLE_BYTES)
        tail_start = max(0, before.st_size - SEEKABLE_CACHE_SAMPLE_BYTES)
        handle.seek(tail_start)
        tail = handle.read(SEEKABLE_CACHE_SAMPLE_BYTES)
    after = path.stat()
    if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
        raise RuntimeError(f"生成缓存身份时源文件发生变化: {path}")
    return {
        "size": before.st_size,
        "mtime_ns": before.st_mtime_ns,
        "sample_bytes": SEEKABLE_CACHE_SAMPLE_BYTES,
        "head_sha256": hashlib.sha256(head).hexdigest(),
        "tail_sha256": hashlib.sha256(tail).hexdigest(),
    }


def cache_metadata_path(target: Path) -> Path:
    return target.with_name(f"{target.name}{SEEKABLE_CACHE_METADATA_SUFFIX}")


def read_cache_metadata(path: Path) -> Optional[dict]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def write_cache_metadata(
    target: Path,
    video: Path,
    identity: dict[str, object],
    duration: float,
) -> None:
    metadata = cache_metadata_path(target)
    target_stat = target.stat()
    payload = {
        "schema_version": SEEKABLE_CACHE_SCHEMA_VERSION,
        "source_path": str(video.resolve()),
        "source": identity,
        "cache": {
            "size": target_stat.st_size,
            "mtime_ns": target_stat.st_mtime_ns,
            "duration": duration,
        },
    }
    temporary = metadata.with_name(f".{metadata.name}.{os.getpid()}.building")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, metadata)
    finally:
        temporary.unlink(missing_ok=True)


def cache_identity_matches(
    target: Path,
    metadata: Optional[dict],
    identity: dict[str, object],
) -> bool:
    if not metadata or metadata.get("schema_version") != SEEKABLE_CACHE_SCHEMA_VERSION:
        return False
    if metadata.get("source") != identity:
        return False
    cache = metadata.get("cache")
    if not isinstance(cache, dict):
        return False
    target_stat = target.stat()
    return (
        cache.get("size") == target_stat.st_size
        and cache.get("mtime_ns") == target_stat.st_mtime_ns
    )


def run_command(cmd: Sequence[str], *, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(cmd),
        check=check,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def require_tools() -> None:
    missing = [str(path) for path in (FFMPEG, FFPROBE) if not path.exists()]
    if missing:
        raise FileNotFoundError(f"缺少 FFmpeg 工具: {', '.join(missing)}")


def probe_duration(video: Path) -> float:
    result = run_command(
        [
            str(FFPROBE),
            "-v",
            "error",
            "-show_entries",
            "format=start_time,duration",
            "-of",
            "json",
            str(video),
        ]
    )
    info = json.loads(result.stdout)
    fmt = info["format"]
    duration = float(fmt["duration"])
    start_time = float(fmt.get("start_time") or 0.0)
    if start_time > 0 and duration > start_time:
        duration -= start_time
    return duration


def ensure_seekable_source(video: Path, cache_dir: Path) -> Path:
    """Remux FLV once so deep random seeks do not scan from the beginning."""
    if video.suffix.lower() != ".flv":
        return video

    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / f"{video.stem}.seekable.mp4"
    metadata = cache_metadata_path(target)
    initial_identity = source_identity(video)
    if target.exists() and target.stat().st_size > 0:
        try:
            cached_metadata = read_cache_metadata(metadata)
            if cache_identity_matches(target, cached_metadata, initial_identity) and abs(
                probe_duration(target) - probe_duration(video)
            ) <= 2.0:
                return target
        except Exception:
            pass
        print(f"视频缓存身份不匹配，重新生成: {target}")

    temporary = target.with_name(f".{target.stem}.building{target.suffix}")
    cmd = [
        str(FFMPEG),
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y",
        "-i",
        str(video),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-c",
        "copy",
        str(temporary),
    ]
    result = run_command(cmd, check=False)
    if result.returncode != 0:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"建立可跳转视频缓存失败: {result.stderr[-4000:]}")

    final_identity = source_identity(video)
    if final_identity != initial_identity:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"建立视频缓存期间源文件发生变化: {video}")

    source_duration = probe_duration(video)
    target_duration = probe_duration(temporary)
    if abs(source_duration - target_duration) > 2.0:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(
            f"视频缓存时长异常: 原始 {source_duration:.3f}s, 缓存 {target_duration:.3f}s"
        )
    os.replace(temporary, target)
    try:
        write_cache_metadata(target, video, final_identity, target_duration)
    except Exception:
        target.unlink(missing_ok=True)
        metadata.unlink(missing_ok=True)
        raise
    return target


def parse_ass(ass_path: Path) -> list[Danmaku]:
    rows: list[Danmaku] = []
    with ass_path.open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            if not line.startswith("Dialogue"):
                continue
            parts = line.rstrip("\r\n").split(",", 9)
            if len(parts) < 10:
                continue
            try:
                hours, minutes, seconds = parts[1].split(":")
                timestamp = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
            except (ValueError, IndexError):
                continue
            text = re.sub(r"\{.*?\}", "", parts[9]).strip()
            rows.append(Danmaku(timestamp, text))
    rows.sort(key=lambda row: row.time)
    return rows


def build_clip_timeline(
    program_points: Sequence[ProgramPoint],
    *,
    max_gap: float,
) -> list[ClipTimelineGroup]:
    grouped: list[list[ProgramPoint]] = []
    for point in sorted(
        program_points,
        key=lambda item: (item.trigger_time, item.category, item.time),
    ):
        if (
            not grouped
            or point.trigger_time - grouped[-1][-1].trigger_time > max_gap
        ):
            grouped.append([point])
        else:
            grouped[-1].append(point)
    return [
        ClipTimelineGroup(index=index, members=tuple(members), max_gap=max_gap)
        for index, members in enumerate(grouped, 1)
    ]


def load_v2_clip_timeline(
    ass_path: Path,
    timeline_json: Path,
) -> tuple[list[ProgramPoint], list[ClipTimelineGroup]]:
    payload = json.loads(timeline_json.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"V2 时间线必须是 JSON 数组: {timeline_json}")

    target_xml = ass_path.with_suffix(".xml").name
    rows = [
        item
        for item in payload
        if str(item.get("xml_file") or "") == target_xml
        and str(item.get("review_status") or "待复核") != "无效"
    ]
    if not rows:
        raise ValueError(f"V2 时间线没有可用节目点: {target_xml}")

    points: list[ProgramPoint] = []
    grouped: dict[int, list[ProgramPoint]] = {}
    for item in sorted(rows, key=lambda value: float(value["program_time_seconds"])):
        point = ProgramPoint(
            time=float(item["program_time_seconds"]),
            trigger_time=float(item["trigger_time_seconds"]),
            category=str(item.get("dominant_reaction") or "弹幕事件"),
            end_hint=float(item.get("event_end_seconds") or 0.0),
            payoff_start=float(item.get("payoff_start_seconds") or 0.0),
            payoff_end=float(item.get("payoff_end_seconds") or 0.0),
            end_policy=str(item.get("end_policy") or "bilibili-reaction-wave"),
        )
        points.append(point)
        group_id = int(item.get("clip_group") or 0)
        grouped.setdefault(group_id, []).append(point)

    timeline = [
        ClipTimelineGroup(
            index=index,
            members=tuple(sorted(members, key=lambda point: point.trigger_time)),
            max_gap=100.0,
        )
        for index, (_, members) in enumerate(
            sorted(grouped.items(), key=lambda item: min(point.time for point in item[1])),
            1,
        )
    ]
    return points, timeline


def generate_clip_timeline(
    ass_path: Path,
    timeline_json: Optional[Path] = None,
) -> tuple[list[ProgramPoint], list[ClipTimelineGroup]]:
    if timeline_json is not None:
        return load_v2_clip_timeline(ass_path, timeline_json)

    sys.path.insert(0, str(SCRIPT_DIR))
    import program_extractor

    keyword_times, kun_times, all_danmaku = program_extractor.extract_times(str(ass_path))
    count_adjust = 0
    result = program_extractor.choose_levels(
        program_extractor.find_program_points(keyword_times, kun_times, all_danmaku)
    )
    total = sum(len(points) for points in result.values())
    if total < 10:
        count_adjust = -1
        result = program_extractor.choose_levels(
            program_extractor.find_program_points(
                keyword_times,
                kun_times,
                all_danmaku,
                count_adjust=-1,
            )
        )

    raw_result = program_extractor.find_clip_program_nodes(
        keyword_times,
        kun_times,
        all_danmaku,
        count_adjust=count_adjust,
    )
    allowed_categories = set(result)
    program_points = [
        ProgramPoint(
            time=float(node["time"]),
            trigger_time=float(node["trigger_time"]),
            category=str(node["category"]),
            rule_indexes=tuple(int(value) for value in node.get("rule_indexes") or []),
            rule_keywords=tuple(
                str(value) for value in node.get("rule_keywords") or []
            ),
        )
        for category, nodes in raw_result.items()
        if category in allowed_categories
        for node in nodes
    ]
    program_points.sort(
        key=lambda point: (point.trigger_time, point.category, point.time)
    )
    timeline = build_clip_timeline(
        program_points,
        max_gap=float(program_extractor.CLIP_GROUP_DISTANCE),
    )
    return program_points, timeline


def generate_program_points(
    ass_path: Path,
    timeline_json: Optional[Path] = None,
) -> list[ProgramPoint]:
    program_points, _ = generate_clip_timeline(ass_path, timeline_json)
    return program_points


def is_reaction(text: str, program_terms: Iterable[str] = ()) -> bool:
    compact = re.sub(r"\s+", "", text)
    if compact == "[哭]":
        return False
    if re.search(r"哈{2,}", compact):
        return True
    if any(term in compact for term in REACTION_TERMS):
        return True
    return any(term and term in compact for term in program_terms)


def collect_program_terms() -> tuple[str, ...]:
    sys.path.insert(0, str(SCRIPT_DIR))
    import program_extractor

    terms = {
        term
        for rules in program_extractor.RULES.values()
        for rule in rules
        for term in (rule.get("keyword") or [])
        if len(term) >= 2
    }
    return tuple(sorted(terms))


def build_reaction_bursts(
    hit_times: Sequence[float],
    *,
    window: float = REACTION_WINDOW,
    min_hits: int = REACTION_MIN_HITS,
    link_gap: float = REACTION_LINK_GAP,
) -> list[ReactionBurst]:
    times = sorted(hit_times)
    active_intervals: list[tuple[float, float, int]] = []
    for index, start in enumerate(times):
        end_index = bisect.bisect_right(times, start + window)
        if end_index - index >= min_hits:
            active_intervals.append((start, times[end_index - 1], end_index - index))

    merged: list[ReactionBurst] = []
    for start, end, count in active_intervals:
        if not merged or start - merged[-1].end > link_gap:
            merged.append(ReactionBurst(start, end, count))
            continue
        previous = merged[-1]
        merged[-1] = ReactionBurst(
            previous.start,
            max(previous.end, end),
            max(previous.hit_count, count),
        )
    return merged


def find_seed_burst(
    bursts: Sequence[ReactionBurst], trigger_time: float
) -> Optional[ReactionBurst]:
    candidates = [
        burst
        for burst in bursts
        if burst.end >= trigger_time - 5.0 and burst.start <= trigger_time + 30.0
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda burst: abs(burst.start - trigger_time))


def find_initial_reaction_wave(
    hit_times: Sequence[float],
    trigger_time: float,
    *,
    max_gap: float = REACTION_WAVE_GAP,
) -> Optional[ReactionBurst]:
    """Return the immediate wave, or a dense delayed payoff after a lead signal.

    Sparse immediate feedback does not block a collective laugh that starts up
    to ``DELAYED_REACTION_LOOKAHEAD`` seconds later. Requiring a dense delayed
    wave keeps ordinary later discussion from extending the clip.
    """
    times = sorted(hit_times)

    def wave_from_seed(seed: int) -> ReactionBurst:
        start_index = seed
        while (
            start_index > 0
            and times[start_index - 1] >= trigger_time - 5.0
            and times[start_index] - times[start_index - 1] <= max_gap
        ):
            start_index -= 1

        end_index = seed
        while (
            end_index + 1 < len(times)
            and times[end_index + 1] - times[end_index] <= max_gap
        ):
            end_index += 1
        return ReactionBurst(
            times[start_index],
            times[end_index],
            end_index - start_index + 1,
        )

    immediate_candidates = [
        index
        for index, timestamp in enumerate(times)
        if trigger_time - 5.0 <= timestamp <= trigger_time + 8.0
    ]
    immediate_wave = None
    if immediate_candidates:
        seed = min(
            immediate_candidates,
            key=lambda index: abs(times[index] - trigger_time),
        )
        immediate_wave = wave_from_seed(seed)
        if immediate_wave.hit_count >= DELAYED_REACTION_MIN_HITS:
            return immediate_wave

    delayed_candidates = [
        index
        for index, timestamp in enumerate(times)
        if trigger_time + 8.0 < timestamp <= trigger_time + DELAYED_REACTION_LOOKAHEAD
    ]
    for seed in delayed_candidates:
        window_end = bisect.bisect_right(times, times[seed] + REACTION_WINDOW)
        if window_end - seed < DELAYED_REACTION_MIN_HITS:
            continue
        delayed_wave = wave_from_seed(seed)
        if delayed_wave.hit_count >= DELAYED_REACTION_MIN_HITS:
            return delayed_wave

    return immediate_wave


def merge_highlights(highlights: Sequence[Highlight]) -> list[Highlight]:
    merged: list[Highlight] = []
    for item in sorted(highlights, key=lambda highlight: highlight.start):
        if not merged or item.start - merged[-1].coarse_end > MERGE_CLIP_GAP:
            merged.append(item)
            continue

        previous = merged[-1]
        previous.coarse_end = max(previous.coarse_end, item.coarse_end)
        previous.final_end = max(previous.final_end, item.final_end)
        previous.activity_start = min(previous.activity_start, item.activity_start)
        previous.activity_end = max(previous.activity_end, item.activity_end)
        previous.review_end = max(previous.review_end, item.review_end)
        previous.reaction_hits += item.reaction_hits
        previous.program_points.extend(item.program_points)
        previous.clip_group_ids.extend(item.clip_group_ids)
        previous.categories = list(dict.fromkeys(previous.categories + item.categories))
        if item.last_program_trigger >= previous.last_program_trigger:
            previous.last_program_trigger = item.last_program_trigger
            previous.tail_anchor_basis = item.tail_anchor_basis
            previous.end_basis = item.end_basis

    for index, item in enumerate(merged, 1):
        item.index = index
        item.program_points.sort()
        item.clip_group_ids = sorted(set(item.clip_group_ids))
    return merged


def analyze_highlights(
    video_duration: float,
    danmaku: Sequence[Danmaku],
    clip_timeline: Sequence[ClipTimelineGroup],
) -> list[Highlight]:
    program_terms = collect_program_terms()
    reaction_times = [
        row.time for row in danmaku if is_reaction(row.text, program_terms)
    ]
    highlights: list[Highlight] = []

    for group in clip_timeline:
        group.selection_status = "selected"
        group.exclusion_reason = None
        group.exclusion_evidence = []

        trigger = group.last_trigger
        terminal_policy = group.members[-1].end_policy
        authoritative_end_policies = {
            "dual-near-end", "dual-confirmed-bilibili-boundary",
            "absolute-time-bilibili", "absolute-time-douyin",
            "creation-time-fused", "watchable-shuangju",
        }
        if terminal_policy in authoritative_end_policies:
            terminal_end = group.members[-1].end_hint
            if not trigger <= terminal_end <= video_duration:
                raise ValueError(f"节目点结束参考越界: {group.index}, {terminal_end}")
            wave = ReactionBurst(
                group.first_trigger,
                terminal_end,
                sum(group.first_trigger <= t <= terminal_end for t in reaction_times),
            )
        else:
            wave = find_initial_reaction_wave(reaction_times, trigger)
        if wave:
            activity_start = wave.start
            activity_end = max(wave.end, trigger)
            hit_count = wave.hit_count
            tail_anchor_basis = (
                terminal_policy
                if terminal_policy in authoritative_end_policies
                else "last-node-delayed-reaction-wave"
                if wave.start > trigger + 8.0
                else "last-node-reaction-wave"
            )
        else:
            # A program point should normally have a matching burst. Keep a
            # conservative short range and mark it low-confidence later.
            activity_start = trigger
            activity_end = min(video_duration, trigger + 10.0)
            hit_count = 0
            tail_anchor_basis = "last-node-fallback-window"

        activity_end = min(video_duration, activity_end)
        coarse_end = min(video_duration, activity_end + AUDIO_TRANSITION_DURATION)
        review_end = min(video_duration, activity_end + SPEECH_REVIEW_AFTER)

        highlights.append(
            Highlight(
                index=len(highlights) + 1,
                start=max(0.0, group.start),
                coarse_end=coarse_end,
                final_end=coarse_end,
                activity_start=activity_start,
                activity_end=activity_end,
                review_end=review_end,
                categories=list(
                    dict.fromkeys(point.category for point in group.members)
                ),
                program_points=[point.time for point in group.members],
                clip_group_ids=[group.index],
                last_program_trigger=group.last_trigger,
                initial_start=max(0.0, group.start),
                tail_anchor_basis=tail_anchor_basis,
                end_basis=tail_anchor_basis,
                reaction_hits=hit_count,
                speech_confidence="pending" if wave else "low-no-reaction-burst",
            )
        )

    return merge_highlights(highlights)


def extract_audio_window(
    video: Path,
    output_wav: Path,
    start: float,
    end: float,
) -> None:
    output_wav.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(FFMPEG),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{start:.3f}",
        "-i",
        str(video),
        "-t",
        f"{max(0.1, end - start):.3f}",
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(output_wav),
    ]
    result = run_command(cmd, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr[-2000:])


def text_looks_incomplete(text: str) -> bool:
    compact = re.sub(r"[\s，。！？!?、,.]+$", "", text.strip())
    return any(compact.endswith(ending) for ending in INCOMPLETE_ENDINGS)


def build_speech_turns(words: Sequence[dict]) -> list[dict]:
    turns: list[dict] = []
    current: list[dict] = []
    for index, word in enumerate(words):
        current.append(word)
        next_start = (
            float(words[index + 1]["start"])
            if index + 1 < len(words)
            else float(word["end"]) + 1.0
        )
        pause = next_start - float(word["end"])
        punctuated = bool(re.search(r"[。！？!?]$", str(word["word"]).strip()))
        if punctuated or pause >= 0.55:
            turns.append(
                {
                    "start": float(current[0]["start"]),
                    "end": float(current[-1]["end"]),
                    "text": "".join(str(item["word"]) for item in current),
                }
            )
            current = []
    if current:
        turns.append(
            {
                "start": float(current[0]["start"]),
                "end": float(current[-1]["end"]),
                "text": "".join(str(item["word"]) for item in current),
            }
        )
    return turns


def choose_terminal_speech_turn(
    words: Sequence[dict],
    *,
    activity_start: float,
    activity_end: float,
) -> Optional[dict]:
    turns = build_speech_turns(words)
    candidates = [
        turn
        for turn in turns
        if turn["start"] <= activity_end
        and turn["end"] >= activity_start - 12.0
        and not text_looks_incomplete(turn["text"])
    ]
    if not candidates:
        return None
    selected = max(candidates, key=lambda turn: turn["end"])
    selected_index = turns.index(selected)
    # A short unpunctuated lead-in can straddle a brief pause. Only join its
    # immediate continuation; do not follow a chain of later conversation.
    if (
        selected["start"] <= activity_end <= selected["end"]
        and selected["end"] - selected["start"] <= SHORT_TERMINAL_MAX_DURATION
        and not re.search(r"[。！？!?]$", selected["text"].strip())
        and selected_index + 1 < len(turns)
    ):
        continuation = turns[selected_index + 1]
        if (
            0 <= continuation["start"] - selected["end"]
            <= SHORT_TERMINAL_CONTINUATION_GAP
        ):
            return {
                "start": selected["start"],
                "end": continuation["end"],
                "text": selected["text"] + continuation["text"],
                "continuation_basis": "short-lead-in-with-brief-pause",
            }
    return selected


def choose_speech_boundary(
    words: Sequence[dict],
    *,
    activity_start: float,
    activity_end: float,
    review_end: float,
) -> Optional[float]:
    if not words:
        return None

    selected = choose_terminal_speech_turn(
        words,
        activity_start=activity_start,
        activity_end=activity_end,
    )
    content_end = max(activity_end, float(selected["end"])) if selected else activity_end
    boundary = min(review_end, content_end + AUDIO_TRANSITION_DURATION)
    next_starts = [
        float(word["start"])
        for word in words
        if content_end < float(word["start"]) <= boundary + SPEECH_TAIL_GUARD
    ]
    if next_starts:
        protected_end = float(selected["end"]) if selected else -math.inf
        boundary = min(
            boundary,
            max(protected_end, min(next_starts) - SPEECH_TAIL_GUARD),
        )
    return boundary


def review_speech_boundary(
    words: Sequence[dict], *, boundary: float, review_end: float,
) -> Optional[dict]:
    """Repair only a turn already crossing the existing cut, not later talk."""
    turn = choose_terminal_speech_turn(
        words, activity_start=boundary, activity_end=boundary,
    )
    if turn is None or not (float(turn["start"]) <= boundary < float(turn["end"])):
        return None
    adjusted = choose_speech_boundary(
        words, activity_start=boundary, activity_end=boundary, review_end=review_end,
    )
    if adjusted is None or adjusted <= boundary:
        return None
    return {
        "original_end": boundary,
        "adjusted_end": adjusted,
        "terminal_turn": turn,
        "reason": "existing-cut-crosses-rechecked-speech-turn",
    }


def pcm_window_dbfs(samples: Sequence[int]) -> float:
    if not samples:
        return -120.0
    mean_square = sum(float(sample) * sample for sample in samples) / len(samples)
    if mean_square <= 0.0:
        return -120.0
    return max(-120.0, 20.0 * math.log10(math.sqrt(mean_square) / 32768.0))


def find_tail_weak_valley(
    wav_path: Path,
    *,
    window_start: float,
    original_end: float,
    lower_bound: float,
    protected_until: Optional[float] = None,
) -> Optional[dict]:
    with wave.open(str(wav_path), "rb") as wav_file:
        channels = wav_file.getnchannels()
        sample_width = wav_file.getsampwidth()
        sample_rate = wav_file.getframerate()
        frame_count = wav_file.getnframes()
        raw = wav_file.readframes(frame_count)

    if sample_width != 2 or channels < 1 or sample_rate <= 0:
        return None

    samples = array.array("h")
    samples.frombytes(raw)
    if sys.byteorder != "little":
        samples.byteswap()
    if channels > 1:
        samples = array.array(
            "h",
            (
                round(sum(samples[index : index + channels]) / channels)
                for index in range(0, len(samples), channels)
            ),
        )
    if not samples:
        return None

    duration = len(samples) / sample_rate
    window_frames = max(1, round(TAIL_ADJUST_WINDOW * sample_rate))
    half_window = TAIL_ADJUST_WINDOW / 2.0

    def centered_db(absolute_time: float) -> float:
        center_frame = round((absolute_time - window_start) * sample_rate)
        start_frame = max(0, center_frame - window_frames // 2)
        end_frame = min(len(samples), start_frame + window_frames)
        start_frame = max(0, end_frame - window_frames)
        return pcm_window_dbfs(samples[start_frame:end_frame])

    available_start = window_start + half_window
    available_end = window_start + duration - half_window
    endpoint_center = min(available_end, original_end - half_window)
    candidate_start = max(
        available_start,
        lower_bound,
        protected_until if protected_until is not None else lower_bound,
        original_end - TAIL_ADJUST_SEARCH_BACK,
    )
    candidate_end = min(
        available_end,
        original_end - TAIL_ADJUST_MIN_SHIFT,
    )
    if candidate_end <= candidate_start or endpoint_center < available_start:
        return None

    endpoint_db = centered_db(endpoint_center)
    if endpoint_db < TAIL_ADJUST_MIN_ENDPOINT_DB:
        return None
    step_count = int(math.floor((candidate_end - candidate_start) / TAIL_ADJUST_STEP))
    times = [
        candidate_start + index * TAIL_ADJUST_STEP
        for index in range(step_count + 1)
    ]
    levels = [centered_db(candidate_time) for candidate_time in times]
    valleys = [
        (times[index], levels[index])
        for index in range(1, len(times) - 1)
        if levels[index] <= levels[index - 1]
        and levels[index] < levels[index + 1]
        and levels[index] <= endpoint_db - TAIL_ADJUST_MIN_DROP_DB
    ]
    if not valleys:
        return None

    adjusted_end, valley_db = valleys[-1]
    return {
        "original_end": original_end,
        "adjusted_end": adjusted_end,
        "endpoint_db": endpoint_db,
        "valley_db": valley_db,
        "shift": original_end - adjusted_end,
    }


def choose_speech_start(
    words: Sequence[dict],
    *,
    anchor: float,
    lower_bound: float,
) -> tuple[float, Optional[dict]]:
    if not words:
        return anchor, None

    containing_turns = [
        turn
        for turn in build_speech_turns(words)
        if float(turn["start"]) <= anchor <= float(turn["end"])
    ]
    if not containing_turns:
        return anchor, None

    selected = min(containing_turns, key=lambda turn: float(turn["start"]))
    return max(lower_bound, float(selected["start"])), selected


class WhisperRefiner:
    def __init__(self, model_name: str, cpu_threads: int = 4):
        MODEL_HOME.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("HF_HOME", str(MODEL_HOME))
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError(
                "未安装 faster-whisper；请使用独立语音环境运行，或选择 --speech off"
            ) from exc

        self.model = WhisperModel(
            model_name,
            device="cpu",
            compute_type="int8",
            cpu_threads=max(1, cpu_threads),
        )

    def transcribe_window(
        self,
        video: Path,
        wav_path: Path,
        window_start: float,
        window_end: float,
        *,
        vad_filter: bool = True,
    ) -> tuple[list[dict], str]:
        extract_audio_window(video, wav_path, window_start, window_end)
        segments, _ = self.model.transcribe(
            str(wav_path),
            language="zh",
            beam_size=5,
            word_timestamps=True,
            vad_filter=vad_filter,
            condition_on_previous_text=False,
            initial_prompt="请按原话转写，包括嗯、啊、呃、诶、哦、哎、吧等语气词。",
        )
        words: list[dict] = []
        segment_texts: list[str] = []
        for segment in segments:
            segment_texts.append(segment.text.strip())
            for word in segment.words or []:
                words.append(
                    {
                        "start": window_start + float(word.start),
                        "end": window_start + float(word.end),
                        "word": word.word,
                        "probability": float(word.probability),
                    }
                )
        return words, "".join(segment_texts)

    def refine(
        self,
        video: Path,
        highlight: Highlight,
        media_dir: Path,
        records_dir: Path,
        job_name: str,
    ) -> None:
        initial_start = highlight.start
        head_window_start = max(0.0, initial_start - START_SPEECH_LOOKBACK)
        head_window_end = initial_start + START_SPEECH_LOOKAHEAD
        head_wav_path = (
            media_dir / f"{job_name}.highlight_{highlight.index:02d}_head.wav"
        )
        head_words, head_text = self.transcribe_window(
            video,
            head_wav_path,
            head_window_start,
            head_window_end,
        )
        selected_start, start_turn = choose_speech_start(
            head_words,
            anchor=initial_start,
            lower_bound=head_window_start,
        )
        highlight.initial_start = initial_start
        highlight.start_speech_text = head_text
        if selected_start < initial_start:
            highlight.start = selected_start
            highlight.start_basis = "speech-turn-containing-40s-anchor"
        else:
            highlight.start_basis = "fixed-40s-pre-roll"

        window_start = max(highlight.start, highlight.activity_end - SPEECH_REVIEW_BEFORE)
        window_end = highlight.review_end
        wav_path = media_dir / f"{job_name}.highlight_{highlight.index:02d}_tail.wav"
        words, tail_text = self.transcribe_window(
            video,
            wav_path,
            window_start,
            window_end,
        )

        terminal_turn = choose_terminal_speech_turn(
            words,
            activity_start=highlight.activity_start,
            activity_end=highlight.activity_end,
        )
        boundary = choose_speech_boundary(
            words,
            activity_start=highlight.activity_start,
            activity_end=highlight.activity_end,
            review_end=highlight.review_end,
        )
        highlight.speech_method = "faster-whisper-word-boundary"
        highlight.speech_text = tail_text
        if boundary is None:
            highlight.final_end = highlight.coarse_end
            highlight.speech_confidence = "low-no-sentence-boundary"
            highlight.end_basis = f"{highlight.tail_anchor_basis}+no-speech-boundary"
        else:
            highlight.final_end = max(highlight.start + 1.0, boundary)
            highlight.speech_confidence = "medium"
            highlight.end_basis = (
                f"{highlight.tail_anchor_basis}+speech-sentence-boundary"
            )

        # Recheck the actual cut with a different, local context and without VAD.
        # The first pass can omit a quiet lead-in and mistake it for a pause.
        cut_review_start = max(highlight.start, highlight.final_end - CUT_REVIEW_BEFORE)
        cut_review_wav = media_dir / f"{job_name}.highlight_{highlight.index:02d}_cut_review.wav"
        cut_words, cut_text = self.transcribe_window(
            video, cut_review_wav, cut_review_start, window_end, vad_filter=False,
        )
        cut_decision = review_speech_boundary(
            cut_words, boundary=highlight.final_end, review_end=window_end,
        )
        cut_review = {
            "window_start": cut_review_start,
            "window_end": window_end,
            "vad_filter": False,
            "text": cut_text,
            "words": cut_words,
            "decision": cut_decision,
        }
        if cut_decision is not None:
            highlight.final_end = float(cut_decision["adjusted_end"])
            terminal_turn = cut_decision["terminal_turn"]
            highlight.end_basis += "+local-cut-speech-review"

        tail_adjustment = find_tail_weak_valley(
            wav_path,
            window_start=window_start,
            original_end=highlight.final_end,
            lower_bound=max(
                highlight.activity_end,
                highlight.final_end - TAIL_ADJUST_SEARCH_BACK,
            ),
            protected_until=(
                float(terminal_turn["end"]) + SPEECH_TAIL_GUARD
                if terminal_turn is not None
                else None
            ),
        )
        if tail_adjustment is not None:
            highlight.tail_adjustment_kind = "audio-weak-valley"
            highlight.tail_adjustment_original_end = float(
                tail_adjustment["original_end"]
            )
            highlight.tail_adjustment_endpoint_db = float(
                tail_adjustment["endpoint_db"]
            )
            highlight.tail_adjustment_valley_db = float(
                tail_adjustment["valley_db"]
            )
            highlight.tail_adjustment_shift = float(tail_adjustment["shift"])
            highlight.final_end = max(
                highlight.start + 1.0,
                float(tail_adjustment["adjusted_end"]),
            )
            highlight.end_basis += "+tail-audio-weak-valley"

        highlight.speech_content_end = (
            float(terminal_turn["end"]) if terminal_turn is not None else None
        )
        highlight.next_speech_start = min(
            (float(word["start"]) for word in (cut_words or words)
             if float(word["start"]) >= highlight.final_end),
            default=None,
        )

        transcript_path = records_dir / f"highlight_{highlight.index:02d}_transcript.json"
        transcript_path.write_text(
            json.dumps(
                {
                    "window_start": window_start,
                    "window_end": window_end,
                    "initial_start": initial_start,
                    "head_window_start": head_window_start,
                    "head_window_end": head_window_end,
                    "selected_start": highlight.start,
                    "start_basis": highlight.start_basis,
                    "start_turn": start_turn,
                    "start_text": highlight.start_speech_text,
                    "start_words": head_words,
                    "activity_start": highlight.activity_start,
                    "activity_end": highlight.activity_end,
                    "clip_group_ids": highlight.clip_group_ids,
                    "last_program_trigger": highlight.last_program_trigger,
                    "selected_end": highlight.final_end,
                    "end_basis": highlight.end_basis,
                    "terminal_turn": terminal_turn,
                    "cut_review": cut_review,
                    "tail_adjustment": tail_adjustment,
                    "speech_content_end": highlight.speech_content_end,
                    "next_speech_start": highlight.next_speech_start,
                    "text": highlight.speech_text,
                    "words": words,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )


def selected_highlights(highlights: Sequence[Highlight], selection: str) -> list[Highlight]:
    if not selection:
        return list(highlights)
    indexes = {int(item.strip()) for item in selection.split(",") if item.strip()}
    invalid = sorted(indexes - {item.index for item in highlights})
    if invalid:
        raise ValueError(f"不存在的片段序号: {invalid}")
    return [item for item in highlights if item.index in indexes]


def prepare_montage_ending(
    highlights: Sequence[Highlight], video_duration: float,
) -> Optional[MontageEnding]:
    if not highlights:
        return None
    last = highlights[-1]
    original_end = last.final_end
    audio_fade_duration = 0.0
    reason = "speech-unavailable-video-fade-only"
    if last.speech_content_end is not None:
        available_end = min(video_duration, last.review_end)
        if last.next_speech_start is not None:
            available_end = min(available_end, last.next_speech_start - SPEECH_TAIL_GUARD)
        last.final_end = original_end + max(
            0.0, min(MONTAGE_END_EXTENSION, available_end - original_end),
        )
        audio_fade_duration = min(
            MONTAGE_END_FADE_DURATION,
            max(0.0, last.final_end - max(last.start, last.speech_content_end)),
        )
        reason = "after-final-sentence-before-next-speech"
    last.end_basis += "+whole-video-outro"
    return MontageEnding(
        clip_index=last.index,
        original_end=original_end,
        final_end=last.final_end,
        extension=last.final_end - original_end,
        video_fade_duration=min(MONTAGE_END_FADE_DURATION, last.duration),
        audio_fade_duration=audio_fade_duration,
        speech_content_end=last.speech_content_end,
        next_speech_start=last.next_speech_start,
        reason=reason,
    )


def render_normalized_clip(
    video: Path,
    highlight: Highlight,
    output: Path,
    profile: dict[str, str],
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    duration = highlight.duration
    if duration <= 0:
        raise ValueError(f"片段 {highlight.index} 时长无效")

    common = [
        str(FFMPEG),
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y",
        "-ss",
        f"{highlight.start:.3f}",
        "-i",
        str(video),
        "-t",
        f"{duration:.3f}",
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-vf",
        profile["qsv_filter"],
        "-c:v",
        "h264_qsv",
        "-global_quality",
        profile["clip_qsv_quality"],
        "-look_ahead",
        "0",
        "-c:a",
        "aac",
        "-b:a",
        profile["audio_bitrate"],
        "-movflags",
        "+faststart",
        str(output),
    ]
    result = run_command(common, check=False)
    if result.returncode == 0:
        return

    fallback = list(common)
    vf_index = fallback.index(profile["qsv_filter"])
    fallback[vf_index] = profile["cpu_filter"]
    codec_index = fallback.index("h264_qsv")
    fallback[codec_index] = "libx264"
    quality_index = fallback.index("-global_quality")
    del fallback[quality_index : quality_index + 4]
    fallback[codec_index + 1 : codec_index + 1] = [
        "-preset",
        profile["x264_preset"],
        "-crf",
        profile["clip_x264_crf"],
    ]
    retry = run_command(fallback, check=False)
    if retry.returncode != 0:
        raise RuntimeError(retry.stderr[-4000:])


def audio_dbfs(samples: Sequence[int]) -> float:
    if not samples:
        return -120.0
    mean_square = sum(float(sample) * sample for sample in samples) / len(samples)
    if mean_square <= 0:
        return -120.0
    return max(-120.0, 20.0 * math.log10(math.sqrt(mean_square) / 32768.0))


def audio_intensity_score(samples: Sequence[int]) -> float:
    if not samples:
        return -120.0
    block_size = max(
        1,
        int(TRANSITION_AUDIO_SAMPLE_RATE * TRANSITION_AUDIO_BLOCK_SECONDS),
    )
    block_levels = []
    for start in range(0, len(samples), block_size):
        block = samples[start : start + block_size]
        if len(block) >= block_size // 2:
            block_levels.append(audio_dbfs(block))
    if not block_levels:
        return audio_dbfs(samples)
    block_levels.sort()
    percentile_index = min(
        len(block_levels) - 1,
        max(0, math.ceil(len(block_levels) * 0.9) - 1),
    )
    return 0.4 * audio_dbfs(samples) + 0.6 * block_levels[percentile_index]


def decode_audio_window(video: Path, start: float, duration: float) -> array.array:
    cmd = [
        str(FFMPEG),
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{max(0.0, start):.3f}",
        "-i",
        str(video),
        "-t",
        f"{duration:.3f}",
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(TRANSITION_AUDIO_SAMPLE_RATE),
        "-f",
        "s16le",
        "pipe:1",
    ]
    result = subprocess.run(cmd, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode != 0:
        error = result.stderr.decode("utf-8", errors="replace")
        raise RuntimeError(error[-2000:] or f"无法读取片段音频: {video}")
    samples = array.array("h")
    samples.frombytes(result.stdout)
    if sys.byteorder != "little":
        samples.byteswap()
    if not samples:
        raise RuntimeError(f"片段没有可分析的音频: {video}")
    return samples


def edge_sample_slice(
    samples: Sequence[int],
    seconds: float,
    *,
    side: str,
) -> Sequence[int]:
    count = min(len(samples), max(1, int(seconds * TRANSITION_AUDIO_SAMPLE_RATE)))
    if side == "head":
        return samples[:count]
    return samples[len(samples) - count :]


def analyze_clip_audio_edges(
    clip_path: Path,
    clip_index: int,
    *,
    duration_limit: Optional[float] = None,
) -> tuple[AudioEdgeAnalysis, AudioEdgeAnalysis]:
    duration = probe_duration(clip_path)
    if duration_limit is not None:
        duration = min(duration, duration_limit)
    analysis_duration = min(TRANSITION_ANALYSIS_DURATION, duration / 2.0)
    if analysis_duration < TRANSITION_IMMEDIATE_WINDOW:
        raise RuntimeError(f"片段过短，无法分析转场音频: {clip_path.name}")
    head_samples = decode_audio_window(clip_path, 0.0, analysis_duration)
    tail_samples = decode_audio_window(
        clip_path,
        max(0.0, duration - analysis_duration),
        analysis_duration,
    )

    def build(side: str, samples: Sequence[int]) -> AudioEdgeAnalysis:
        scores = {
            window: audio_intensity_score(
                edge_sample_slice(samples, min(window, analysis_duration), side=side)
            )
            for window in TRANSITION_ANALYSIS_WINDOWS
        }
        immediate = edge_sample_slice(
            samples,
            TRANSITION_IMMEDIATE_WINDOW,
            side=side,
        )
        return AudioEdgeAnalysis(
            clip_index=clip_index,
            side=side,
            immediate_rms_db=round(audio_dbfs(immediate), 3),
            score_2s=round(scores[2.0], 3),
            score_4s=round(scores[4.0], 3),
            score_6s=round(scores[6.0], 3),
        )

    return build("head", head_samples), build("tail", tail_samples)


def classify_audio_edges(edges: Sequence[AudioEdgeAnalysis]) -> dict[str, object]:
    scores = [edge.representative_score for edge in edges]
    if not scores:
        return {"status": "no-audio-edges"}
    score_range = max(scores) - min(scores)
    if score_range < TRANSITION_MIN_LEVEL_RANGE_DB or len(set(scores)) < 3:
        for edge in edges:
            edge.level = "medium"
            edge.intense_votes = 0
        return {
            "status": "insufficient-level-range",
            "score_range_db": round(score_range, 3),
            "centers_db": [round(sum(scores) / len(scores), 3)],
            "calm_max_db": None,
            "intense_min_db": None,
        }

    centers = [min(scores), sum(scores) / len(scores), max(scores)]
    for _ in range(100):
        groups: list[list[float]] = [[], [], []]
        for score in scores:
            group_index = min(
                range(3),
                key=lambda index: abs(score - centers[index]),
            )
            groups[group_index].append(score)
        if any(not group for group in groups):
            break
        updated = [sum(group) / len(group) for group in groups]
        if max(abs(current - new) for current, new in zip(centers, updated)) < 0.001:
            centers = updated
            break
        centers = updated

    centers.sort()
    calm_max = (centers[0] + centers[1]) / 2.0
    intense_min = (centers[1] + centers[2]) / 2.0
    for edge in edges:
        if edge.representative_score < calm_max:
            edge.level = "calm"
        elif edge.representative_score < intense_min:
            edge.level = "medium"
        else:
            edge.level = "intense"
        edge.intense_votes = sum(
            score >= intense_min
            for score in (edge.score_2s, edge.score_4s, edge.score_6s)
        )
    return {
        "status": "classified",
        "score_range_db": round(score_range, 3),
        "centers_db": [round(center, 3) for center in centers],
        "calm_max_db": round(calm_max, 3),
        "intense_min_db": round(intense_min, 3),
    }


def standard_transition_plan(
    highlights: Sequence[Highlight],
    *,
    reason: str,
) -> list[TransitionPlan]:
    return [
        TransitionPlan(
            boundary_index=boundary,
            outgoing_clip_index=highlights[boundary - 1].index,
            incoming_clip_index=highlights[boundary].index,
            kind="standard",
            video_duration=VIDEO_TRANSITION_DURATION,
            audio_duration=AUDIO_TRANSITION_DURATION,
            incoming_audio_trim=(
                VIDEO_TRANSITION_DURATION - AUDIO_TRANSITION_DURATION
            ),
            outgoing_level="unknown",
            incoming_level="unknown",
            outgoing_immediate_rms_db=None,
            incoming_immediate_rms_db=None,
            reason=reason,
        )
        for boundary in range(1, len(highlights))
    ]


def plan_transitions_from_edges(
    highlights: Sequence[Highlight],
    edges: Sequence[AudioEdgeAnalysis],
) -> list[TransitionPlan]:
    edge_by_key = {(edge.clip_index, edge.side): edge for edge in edges}
    plans: list[TransitionPlan] = []
    for boundary in range(1, len(highlights)):
        outgoing_highlight = highlights[boundary - 1]
        outgoing_index = outgoing_highlight.index
        incoming_index = highlights[boundary].index
        outgoing = edge_by_key[(outgoing_index, "tail")]
        incoming = edge_by_key[(incoming_index, "head")]
        outgoing_intense = outgoing.level == "intense" and outgoing.intense_votes >= 2
        incoming_intense = incoming.level == "intense" and incoming.intense_votes >= 2
        outgoing_usable = outgoing.immediate_rms_db >= TRANSITION_USABLE_EDGE_RMS_DB
        incoming_usable = incoming.immediate_rms_db >= TRANSITION_USABLE_EDGE_RMS_DB

        kind = "standard"
        video_duration = VIDEO_TRANSITION_DURATION
        audio_duration = AUDIO_TRANSITION_DURATION
        reason = "no-directional-audio-transition"
        if outgoing_intense and incoming_intense:
            kind = "intense-to-intense"
            video_duration = TIGHT_VIDEO_TRANSITION_DURATION
            audio_duration = TIGHT_AUDIO_TRANSITION_DURATION
            reason = "both-edges-sustain-intense-audio"
        elif not outgoing_intense and incoming_intense and incoming_usable:
            kind = "j-style"
            audio_duration = J_STYLE_AUDIO_TRANSITION_DURATION
            reason = "incoming-edge-is-intense-and-immediately-audible"
        elif outgoing_intense and not incoming_intense and outgoing_usable:
            kind = "l-style"
            audio_duration = L_STYLE_AUDIO_TRANSITION_DURATION
            reason = "outgoing-edge-is-intense-and-has-usable-tail-audio"

        plans.append(
            TransitionPlan(
                boundary_index=boundary,
                outgoing_clip_index=outgoing_index,
                incoming_clip_index=incoming_index,
                kind=kind,
                video_duration=video_duration,
                audio_duration=audio_duration,
                incoming_audio_trim=video_duration - audio_duration,
                outgoing_level=outgoing.level,
                incoming_level=incoming.level,
                outgoing_immediate_rms_db=outgoing.immediate_rms_db,
                incoming_immediate_rms_db=incoming.immediate_rms_db,
                reason=reason,
            )
        )
    return plans


def build_transition_plan(
    clip_paths: Sequence[Path],
    highlights: Sequence[Highlight],
    *,
    ending: Optional[MontageEnding] = None,
) -> tuple[list[TransitionPlan], list[AudioEdgeAnalysis], dict[str, object]]:
    if len(clip_paths) != len(highlights):
        raise ValueError("片段文件与转场分析时间线数量不一致")
    if len(clip_paths) < 2:
        return [], [], {"status": "single-clip"}

    edges: list[AudioEdgeAnalysis] = []
    try:
        for clip_path, highlight in zip(clip_paths, highlights):
            limit = (
                ending.original_end - highlight.start
                if ending is not None and highlight.index == ending.clip_index
                else None
            )
            edges.extend(analyze_clip_audio_edges(
                clip_path, highlight.index, duration_limit=limit,
            ))
        analysis = classify_audio_edges(edges)
    except Exception as exc:
        analysis = {"status": "fallback", "error": str(exc)}
        return standard_transition_plan(highlights, reason="audio-analysis-failed"), [], analysis

    return plan_transitions_from_edges(highlights, edges), edges, analysis


def build_source_reference_overlays(
    highlights: Sequence[Highlight],
    source_part: int,
    transitions: Sequence[TransitionPlan] = (),
) -> list[SourceReferenceOverlay]:
    if source_part < 1:
        raise ValueError("源视频分P必须大于等于 1")

    transition_by_boundary = {
        transition.boundary_index: transition for transition in transitions
    }
    overlays: list[SourceReferenceOverlay] = []
    clip_output_start = 0.0
    for position, highlight in enumerate(highlights):
        output_start = clip_output_start
        output_end = clip_output_start + highlight.duration
        text = f"P{source_part} · {format_whole_time(highlight.start)}"
        overlays.append(
            SourceReferenceOverlay(
                clip_index=highlight.index,
                source_part=source_part,
                source_time=highlight.start,
                output_start=output_start,
                output_end=output_end,
                text=text,
            )
        )
        if position + 1 < len(highlights):
            transition = transition_by_boundary.get(position + 1)
            transition_duration = (
                transition.video_duration
                if transition is not None
                else VIDEO_TRANSITION_DURATION
            )
            clip_output_start += highlight.duration - transition_duration
    return overlays


def escape_drawtext_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")


def source_label_font() -> Path:
    windows_dir = Path(os.environ.get("WINDIR", r"C:\Windows"))
    preferred = windows_dir / "Fonts" / "msyh.ttc"
    if preferred.exists():
        return preferred
    return windows_dir / "Fonts" / "arial.ttf"


def append_source_reference_filter(
    filters: list[str],
    video_label: str,
    output_label: str,
    overlay: SourceReferenceOverlay,
) -> str:
    font = escape_drawtext_value(source_label_font().as_posix())
    text = escape_drawtext_value(overlay.text)
    filters.append(
        f"[{video_label}]drawtext=fontfile='{font}':text='{text}':"
        "fontcolor=white:fontsize=h*0.030:box=1:boxcolor=black@0.58:"
        "boxborderw=10:x=w-tw-w*0.018:y=h*0.028:"
        f"[{output_label}]"
    )
    return output_label


def render_montage(
    clip_paths: Sequence[Path],
    highlights: Sequence[Highlight],
    output: Path,
    *,
    source_part: int,
    profile: dict[str, str],
    transitions: Sequence[TransitionPlan],
    ending: Optional[MontageEnding] = None,
) -> None:
    if not clip_paths:
        raise ValueError("没有可拼接的片段")
    if len(clip_paths) != len(highlights):
        raise ValueError("片段文件与时间线数量不一致")
    if ending is not None and ending.clip_index != highlights[-1].index:
        raise ValueError("整片淡出只能作用于最后一个片段")

    if len(transitions) != max(0, len(clip_paths) - 1):
        raise ValueError("转场计划数量与片段数量不一致")
    transition_by_boundary = {
        transition.boundary_index: transition for transition in transitions
    }
    if set(transition_by_boundary) != set(range(1, len(clip_paths))):
        raise ValueError("转场计划边界不完整")
    clip_durations = [probe_duration(path) for path in clip_paths]
    overlays = build_source_reference_overlays(
        highlights,
        source_part,
        transitions,
    )
    cmd = [str(FFMPEG), "-hide_banner", "-loglevel", "warning", "-y"]
    for path in clip_paths:
        cmd.extend(["-i", str(path)])

    filters: list[str] = []
    for index in range(len(clip_paths)):
        final_clip = ending is not None and index == len(clip_paths) - 1
        prepared_video = f"vbase{index}"
        filters.append(
            f"[{index}:v]settb=AVTB,format=yuv420p[{prepared_video}]"
        )
        append_source_reference_filter(
            filters,
            prepared_video,
            f"vref{index}" if final_clip else f"v{index}",
            overlays[index],
        )
        if final_clip:
            fade_duration = min(ending.video_fade_duration, clip_durations[index])
            fade_start = max(0.0, clip_durations[index] - fade_duration)
            filters.append(
                f"[vref{index}]fade=t=out:st={fade_start:.3f}:"
                f"d={fade_duration:.3f}[v{index}]"
            )
        input_audio_trim = (
            0.0
            if index == 0
            else transition_by_boundary[index].incoming_audio_trim
        )
        audio_filters: list[str] = []
        if input_audio_trim > 0.0:
            audio_filters.extend(
                [
                    f"atrim=start={input_audio_trim:.3f}",
                    "asetpts=PTS-STARTPTS",
                ]
            )
        audio_filters.append("aresample=async=1:first_pts=0")
        if final_clip and ending.audio_fade_duration > 0.0:
            fade_duration = min(
                ending.audio_fade_duration,
                max(0.0, clip_durations[index] - input_audio_trim),
            )
            fade_start = max(0.0, clip_durations[index] - input_audio_trim - fade_duration)
            audio_filters.append(
                f"afade=t=out:st={fade_start:.3f}:d={fade_duration:.3f}:curve=hsin"
            )
        filters.append(f"[{index}:a]{','.join(audio_filters)}[a{index}]")

    video_label = "v0"
    audio_label = "a0"
    cumulative = clip_durations[0]
    for index in range(1, len(clip_paths)):
        transition = transition_by_boundary[index]
        offset = cumulative - transition.video_duration
        next_video = f"vx{index}"
        next_audio = f"ax{index}"
        filters.append(
            f"[{video_label}][v{index}]xfade=transition=fade:"
            f"duration={transition.video_duration:.3f}:"
            f"offset={offset:.3f}[{next_video}]"
        )
        filters.append(
            f"[{audio_label}][a{index}]acrossfade="
            f"d={transition.audio_duration:.3f}:"
            f"c1=tri:c2=tri[{next_audio}]"
        )
        video_label = next_video
        audio_label = next_audio
        cumulative += clip_durations[index] - transition.video_duration

    cmd.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            f"[{video_label}]",
            "-map",
            f"[{audio_label}]",
            "-c:v",
            "h264_qsv",
            "-global_quality",
            profile["final_qsv_quality"],
            "-look_ahead",
            "0",
            "-c:a",
            "aac",
            "-b:a",
            profile["audio_bitrate"],
            "-movflags",
            "+faststart",
            str(output),
        ]
    )
    result = run_command(cmd, check=False)
    if result.returncode == 0:
        return

    fallback = list(cmd)
    codec_index = fallback.index("h264_qsv")
    fallback[codec_index] = "libx264"
    quality_index = fallback.index("-global_quality")
    del fallback[quality_index : quality_index + 4]
    fallback[codec_index + 1 : codec_index + 1] = [
        "-preset",
        profile["x264_preset"],
        "-crf",
        profile["final_x264_crf"],
    ]
    retry = run_command(fallback, check=False)
    if retry.returncode != 0:
        raise RuntimeError(retry.stderr[-5000:])


def write_manifest(
    job_dir: Path,
    video: Path,
    ass_path: Path,
    video_duration: float,
    danmaku_count: int,
    program_points: Sequence[ProgramPoint],
    clip_timeline: Sequence[ClipTimelineGroup],
    highlights: Sequence[Highlight],
    *,
    selected_indexes: Sequence[int],
    speech_mode: str,
    whisper_model: str,
    render_enabled: bool,
    output_path: Optional[Path],
    media_dir: Path,
    source_part: int,
    timeline_json: Optional[Path],
    render_profile: dict[str, str],
    transitions: Sequence[TransitionPlan],
    audio_edges: Sequence[AudioEdgeAnalysis],
    transition_analysis: dict[str, object],
    render_completed: bool = False,
    ending: Optional[MontageEnding] = None,
) -> None:
    video_stat = video.stat()
    highlight_by_group = {
        group_id: highlight
        for highlight in highlights
        for group_id in highlight.clip_group_ids
    }
    clip_groups = []
    for group in clip_timeline:
        highlight = highlight_by_group.get(group.index)
        trigger_times = [point.trigger_time for point in group.members]
        clip_groups.append(
            {
                "group_id": group.index,
                "start": max(0.0, group.start),
                "first_trigger": group.first_trigger,
                "last_trigger": group.last_trigger,
                "member_count": len(group.members),
                "members": [asdict(point) for point in group.members],
                "member_gaps": [
                    current - previous
                    for previous, current in zip(trigger_times, trigger_times[1:])
                ],
                "categories": list(
                    dict.fromkeys(point.category for point in group.members)
                ),
                "grouping_rule": "adjacent-trigger-gap",
                "grouping_max_gap": group.max_gap,
                "selection_status": group.selection_status,
                "exclusion_reason": group.exclusion_reason,
                "exclusion_evidence": group.exclusion_evidence,
                "highlight_index": highlight.index if highlight else None,
                "final_end": highlight.final_end if highlight else None,
                "last_program_trigger": (
                    highlight.last_program_trigger if highlight else group.last_trigger
                ),
                "tail_anchor_basis": (
                    highlight.tail_anchor_basis if highlight else None
                ),
                "end_basis": highlight.end_basis if highlight else None,
                "is_final_end_anchor": bool(
                    highlight
                    and group.index == max(highlight.clip_group_ids)
                ),
            }
        )
    clip_timeline_payload = {
        "schema_version": 1,
        "algorithm_version": ALGORITHM_VERSION,
        "source_ass": str(ass_path.resolve()),
        "source_timeline_json": (
            str(timeline_json.resolve()) if timeline_json else None
        ),
        "node_count": len(program_points),
        "group_count": len(clip_timeline),
        "selected_group_count": sum(
            group.selection_status == "selected" for group in clip_timeline
        ),
        "excluded_group_count": sum(
            group.selection_status == "excluded" for group in clip_timeline
        ),
        "groups": clip_groups,
    }
    selected_index_set = set(selected_indexes)
    rendered_highlights = [
        highlight for highlight in highlights if highlight.index in selected_index_set
    ]
    source_reference_overlays = build_source_reference_overlays(
        rendered_highlights,
        source_part,
        transitions,
    )
    payload = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "video": str(video.resolve()),
        "ass": str(ass_path.resolve()),
        "video_duration": video_duration,
        "danmaku_count": danmaku_count,
        "media_output": str(output_path.resolve()) if output_path else None,
        "storage": {
            "media_policy": "temporary-auto-cleaned",
            "media_directory": str(media_dir.resolve()),
            "records_policy": "retained",
            "records_directory": str(job_dir.resolve()),
        },
        "program_points": [asdict(point) for point in program_points],
        "clip_timeline": clip_timeline_payload,
        "source_reference_overlays": [
            asdict(overlay) for overlay in source_reference_overlays
        ],
        "transition_plan": {
            "schema_version": 1,
            "policy": TRANSITION_POLICY,
            "analysis": transition_analysis,
            "audio_edges": [asdict(edge) for edge in audio_edges],
            "transitions": [asdict(transition) for transition in transitions],
        },
        "montage_ending": asdict(ending) if ending is not None else None,
        "highlights": [
            {
                **asdict(highlight),
                "duration": highlight.duration,
                "decision_source": "automatic-speech-boundary"
                if highlight.speech_method != "not-run"
                else "automatic-danmaku-boundary",
            }
            for highlight in highlights
        ],
        "automation_audit": {
            "algorithm_version": ALGORITHM_VERSION,
            "decision_mode": DECISION_MODE,
            "manual_overrides": [],
            "selected_indexes": list(selected_indexes),
            "speech_mode": speech_mode,
            "whisper_model": whisper_model if speech_mode == "whisper" else None,
            "render_enabled": render_enabled,
            "render_completed": render_completed,
            "quality_profile": render_profile["name"] if render_enabled else None,
            "output_resolution": render_profile["resolution"] if render_enabled else None,
            "output_fps": render_profile["fps"] if render_enabled else None,
            "script_sha256": sha256_file(Path(__file__).resolve()),
            "ass_sha256": sha256_file(ass_path),
            "timeline_sha256": sha256_file(timeline_json) if timeline_json else None,
            "video_fingerprint": {
                "size": video_stat.st_size,
                "mtime_ns": video_stat.st_mtime_ns,
            },
        },
        "settings": {
            "program_pre_roll": PROGRAM_PRE_ROLL,
            "start_speech_lookback": START_SPEECH_LOOKBACK,
            "start_speech_lookahead": START_SPEECH_LOOKAHEAD,
            "start_boundary_policy": "speech-turn-containing-40s-anchor",
            "clip_node_group_gap": (
                clip_timeline[0].max_gap if clip_timeline else None
            ),
            "reaction_window": REACTION_WINDOW,
            "reaction_min_hits": REACTION_MIN_HITS,
            "reaction_wave_gap": REACTION_WAVE_GAP,
            "candidate_weighting_source": "v2-timeline",
            "ending_signal": "per-node-end-policy",
            "program_end_policies": sorted({point.end_policy for point in program_points}),
            "excluded_high_energy_emoji": ["[哭]"],
            "speech_review_after": SPEECH_REVIEW_AFTER,
            "speech_boundary_policy": "first-version-overlapping-sentence",
            "local_cut_review": {
                "enabled": True,
                "lookback": CUT_REVIEW_BEFORE,
                "end_limit": "original-speech-review-window",
                "vad_filter": False,
                "action": "complete-only-speech-turn-crossing-existing-cut",
            },
            "speech_boundary_guards": {
                "tail_guard": SPEECH_TAIL_GUARD,
                "short_lead_in_max_duration": SHORT_TERMINAL_MAX_DURATION,
                "short_lead_in_continuation_gap": SHORT_TERMINAL_CONTINUATION_GAP,
                "continuation_limit": 1,
                "padding_must_not_enter_next_word": True,
            },
            "montage_ending": {
                "scope": "last-selected-clip-only",
                "extension": MONTAGE_END_EXTENSION,
                "fade_duration": MONTAGE_END_FADE_DURATION,
                "audio_curve": "hsin",
                "protect_speech": True,
                "fade_source_label": True,
            },
            "tail_audio_valley_adjustment": {
                "enabled": True,
                "semantic_classification": False,
                "protect_selected_speech_end": True,
                "search_back": TAIL_ADJUST_SEARCH_BACK,
                "window": TAIL_ADJUST_WINDOW,
                "step": TAIL_ADJUST_STEP,
                "minimum_drop_db": TAIL_ADJUST_MIN_DROP_DB,
                "minimum_endpoint_db": TAIL_ADJUST_MIN_ENDPOINT_DB,
                "minimum_shift": TAIL_ADJUST_MIN_SHIFT,
                "action": "move-cut-to-latest-qualified-audio-valley",
            },
            "video_transition": VIDEO_TRANSITION_DURATION,
            "audio_transition": AUDIO_TRANSITION_DURATION,
            "transition_policy": TRANSITION_POLICY,
            "transition_analysis_windows": list(TRANSITION_ANALYSIS_WINDOWS),
            "transition_immediate_window": TRANSITION_IMMEDIATE_WINDOW,
            "transition_usable_edge_rms_db": TRANSITION_USABLE_EDGE_RMS_DB,
            "intense_to_intense_transition": {
                "video": TIGHT_VIDEO_TRANSITION_DURATION,
                "audio": TIGHT_AUDIO_TRANSITION_DURATION,
            },
            "j_style_audio_transition": J_STYLE_AUDIO_TRANSITION_DURATION,
            "l_style_audio_transition": L_STYLE_AUDIO_TRANSITION_DURATION,
            "source_part": source_part,
            "source_label_position": "top-right",
            "source_label_duration": "full-clip",
            "source_label_starts_after_transition": False,
            "source_label_hidden_during_transition": False,
            "source_label_transition_behavior": "crossfade-with-clip",
        },
    }
    (job_dir / "manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (job_dir / "clip_timeline.json").write_text(
        json.dumps(clip_timeline_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    lines = [
        f"视频: {video.name}",
        f"ASS: {ass_path.name}",
        f"视频时长: {format_time(video_duration)}",
        f"弹幕数: {danmaku_count}",
        f"原始节目节点: {len(program_points)}",
        f"连续候选组: {len(clip_timeline)}",
        f"排除候选组: {sum(group.selection_status == 'excluded' for group in clip_timeline)}",
        f"合并后精彩事件: {len(highlights)}",
        f"算法版本: {ALGORITHM_VERSION}",
        "决策模式: 全自动（无人工切点）",
        "人工覆盖: 0",
        "",
    ]
    for item in highlights:
        lines.append(
            f"{item.index:02d}. {format_time(item.start)} -> {format_time(item.final_end)} "
            f"({item.duration:.1f}s) [{'/'.join(item.categories)}] "
            f"组={','.join(map(str, item.clip_group_ids))} "
            f"尾节点={format_time(item.last_program_trigger)} "
            f"反应={item.reaction_hits} 语音={item.speech_confidence} "
            f"结束依据={item.end_basis} "
            f"来源=P{source_part} · {format_whole_time(item.start)}"
        )
    excluded_groups = [
        group for group in clip_timeline if group.selection_status == "excluded"
    ]
    if excluded_groups:
        lines.extend(["", "排除候选:"])
        for group in excluded_groups:
            lines.append(
                f"组{group.index}: {format_time(group.first_trigger)} -> "
                f"{format_time(group.last_trigger)} 原因={group.exclusion_reason} "
                f"证据={' | '.join(group.exclusion_evidence)}"
            )
    if transitions:
        lines.extend(["", f"转场规则: {TRANSITION_POLICY}"])
        for transition in transitions:
            lines.append(
                f"P{transition.outgoing_clip_index}->P{transition.incoming_clip_index}: "
                f"{transition.kind} 画面={transition.video_duration:.2f}s "
                f"音频={transition.audio_duration:.2f}s "
                f"强度={transition.outgoing_level}->{transition.incoming_level} "
                f"依据={transition.reason}"
            )
    (job_dir / "timeline.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="根据节目单和弹幕生成精彩片段成片")
    parser.add_argument("--video", type=Path, required=True, help="已上传的视频文件")
    parser.add_argument("--ass", dest="ass_path", type=Path, required=True, help="对应 ASS 弹幕")
    parser.add_argument(
        "--media-root",
        type=Path,
        default=DEFAULT_MEDIA_ROOT,
        help="临时媒体和最终视频目录",
    )
    parser.add_argument(
        "--records-root",
        type=Path,
        default=DEFAULT_RECORDS_ROOT,
        help="长期保留的分析记录目录",
    )
    parser.add_argument("--job-name", default="", help="任务名称")
    parser.add_argument(
        "--select",
        default="",
        help="需要语音精修/渲染的片段序号，如 2,3,8；留空处理整场",
    )
    parser.add_argument("--speech", choices=("off", "whisper"), default="off")
    parser.add_argument("--whisper-model", default="small")
    parser.add_argument("--cpu-threads", type=int, default=4)
    parser.add_argument(
        "--source-part",
        type=int,
        default=1,
        help="角标显示的完整回放分P序号",
    )
    parser.add_argument(
        "--timeline-json",
        type=Path,
        default=None,
        help="V2 节目点 JSON；提供后不再调用旧关键词节目单",
    )
    parser.add_argument(
        "--preview-height",
        type=int,
        default=0,
        help="仅本次渲染使用的预览高度；0 表示正式原分辨率",
    )
    parser.add_argument("--render", action="store_true", help="渲染选中片段并生成成片")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    require_tools()
    render_profile = build_render_profile(args.preview_height)
    video = args.video.resolve()
    ass_path = args.ass_path.resolve()
    timeline_json = args.timeline_json.resolve() if args.timeline_json else None
    if not video.exists():
        raise FileNotFoundError(video)
    if not ass_path.exists():
        raise FileNotFoundError(ass_path)
    if timeline_json is not None and not timeline_json.exists():
        raise FileNotFoundError(timeline_json)

    job_name = args.job_name or f"{video.stem}_{datetime.now():%Y%m%d_%H%M%S}"
    media_dir = args.media_root.resolve()
    records_dir = args.records_root.resolve() / job_name
    media_dir.mkdir(parents=True, exist_ok=True)
    records_dir.mkdir(parents=True, exist_ok=False)
    output_path = media_dir / f"{job_name}.mp4" if args.render else None

    video_duration = probe_duration(video)
    danmaku = parse_ass(ass_path)
    program_points, clip_timeline = generate_clip_timeline(ass_path, timeline_json)
    highlights = analyze_highlights(video_duration, danmaku, clip_timeline)
    chosen = selected_highlights(highlights, args.select)

    print(f"视频时长: {format_time(video_duration)}")
    print(f"弹幕数量: {len(danmaku)}")
    print(
        f"原始节目节点: {len(program_points)}，连续组: {len(clip_timeline)}，"
        f"片尾排除: {sum(group.selection_status == 'excluded' for group in clip_timeline)}，"
        f"合并后事件: {len(highlights)}"
    )

    processing_source = video
    if args.render and args.speech == "whisper":
        print("准备可快速定位的视频缓存...")
        processing_source = ensure_seekable_source(video, media_dir)
        if processing_source != video:
            print(f"使用视频缓存: {processing_source}")

    if args.speech == "whisper":
        print(f"加载本地语音模型: {args.whisper_model}")
        refiner = WhisperRefiner(args.whisper_model, args.cpu_threads)
        for item in chosen:
            print(f"语音精修 {item.index:02d}: {format_time(item.start)}")
            refiner.refine(processing_source, item, media_dir, records_dir, job_name)

    ending = prepare_montage_ending(chosen, video_duration)
    transitions: list[TransitionPlan] = []
    audio_edges: list[AudioEdgeAnalysis] = []
    transition_analysis: dict[str, object] = {"status": "not-rendered"}
    if args.render:
        write_manifest(
            records_dir,
            video,
            ass_path,
            video_duration,
            len(danmaku),
            program_points,
            clip_timeline,
            highlights,
            selected_indexes=[item.index for item in chosen],
            speech_mode=args.speech,
            whisper_model=args.whisper_model,
            render_enabled=True,
            output_path=output_path,
            media_dir=media_dir,
            source_part=args.source_part,
            timeline_json=timeline_json,
            render_profile=render_profile,
            transitions=transitions,
            audio_edges=audio_edges,
            transition_analysis={"status": "pending-render"},
            ending=ending,
        )
    if args.render:
        render_source = processing_source
        if render_source == video:
            print("准备可快速定位的视频缓存...")
            render_source = ensure_seekable_source(video, media_dir)
        if render_source != video:
            print(f"使用视频缓存: {render_source}")
        clip_paths: list[Path] = []
        for item in chosen:
            categories = "_".join(item.categories)
            clip_path = media_dir / f"{job_name}.clip_{item.index:02d}_{categories}.mp4"
            print(
                f"渲染 {item.index:02d}: {format_time(item.start)} -> "
                f"{format_time(item.final_end)} ({item.duration:.1f}s)"
            )
            render_normalized_clip(render_source, item, clip_path, render_profile)
            clip_paths.append(clip_path)

        print("分析片段边缘响度并生成转场计划...")
        transitions, audio_edges, transition_analysis = build_transition_plan(
            clip_paths,
            chosen,
            ending=ending,
        )
        if transition_analysis.get("status") == "fallback":
            print(
                f"转场响度分析失败，全部使用标准转场: "
                f"{transition_analysis.get('error')}"
            )
        for transition in transitions:
            print(
                f"转场 P{transition.outgoing_clip_index}->"
                f"P{transition.incoming_clip_index}: {transition.kind} "
                f"(画面 {transition.video_duration:.2f}s / "
                f"音频 {transition.audio_duration:.2f}s)"
            )

        print(f"生成正式成片: {output_path}")
        render_montage(
            clip_paths,
            chosen,
            output_path,
            source_part=args.source_part,
            profile=render_profile,
            transitions=transitions,
            ending=ending,
        )

    write_manifest(
        records_dir,
        video,
        ass_path,
        video_duration,
        len(danmaku),
        program_points,
        clip_timeline,
        highlights,
        selected_indexes=[item.index for item in chosen],
        speech_mode=args.speech,
        whisper_model=args.whisper_model,
        render_enabled=args.render,
        render_completed=args.render,
        output_path=output_path,
        media_dir=media_dir,
        source_part=args.source_part,
        timeline_json=timeline_json,
        render_profile=render_profile,
        transitions=transitions,
        audio_edges=audio_edges,
        transition_analysis=transition_analysis,
        ending=ending,
    )

    print(f"分析记录: {records_dir}")
    if output_path:
        print(f"最终视频: {output_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("已取消", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"错误: {exc}", file=sys.stderr)
        raise SystemExit(1)
