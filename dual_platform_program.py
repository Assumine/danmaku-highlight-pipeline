"""Build post-upload program points without changing recordings or uploading media."""

from __future__ import annotations

import bisect
import copy
import json
import math
import re
import statistics
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import program_rule_engine_v2 as engine
from danmaku_uid import filter_uid_spam, valid_uid


POLICY_VERSION = "dual-platform-v5-stable-tail"
MIN_ABSOLUTE_TIMESTAMP = 946684800.0
ENERGY_WINDOW_SECONDS = 12
ENERGY_MIN_MESSAGES = 5
ENERGY_MIN_USERS = 4
ENERGY_MIN_SCORE = 6.0
ENERGY_MIN_DENSITY_LIFT = 1.45
STABLE_RISE_SECONDS = 3
STABLE_FALL_SECONDS = 4
LAUGHTER_CLUSTER_SECONDS = 12
WATCHABLE_LINK_GAP_SECONDS = 30
WATCHABLE_MIN_USERS = 4
SHUANGJU_PRE_ROLL_SECONDS = 180
SHUANGJU_PATTERN = re.compile(r"爽局|爽了|太爽|爽死|舒服了|赢麻|起飞|碾压")


@dataclass(frozen=True)
class VideoClockSegment:
    xml_file: str
    video_start: float
    duration: float
    clock_origin: float
    clock_basis: str
    timestamp_samples: int

    @property
    def wall_end(self) -> float:
        return self.clock_origin + self.duration

    def map_wall_time(self, wall_time: float) -> float | None:
        if not self.clock_origin <= wall_time <= self.wall_end:
            return None
        return self.video_start + wall_time - self.clock_origin


@dataclass
class EnergyInterval:
    start: float
    end: float
    peak: dict


def timestamp(value) -> float | None:
    try:
        result = datetime.fromisoformat(str(value)).timestamp()
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None


def comments_from_root(root, platform: str, offset: float = 0.0):
    messages = []
    for node in root.findall("d"):
        fields = node.get("p", "").split(",")
        try:
            relative = float(fields[0])
            time_value = relative + offset
        except (ValueError, IndexError):
            continue
        if relative < 0 or not math.isfinite(time_value):
            continue
        uid = fields[6].strip() if len(fields) > 6 else ""
        text = (node.text or "").strip()
        if valid_uid(uid) and text:
            messages.append(engine.Message(time_value, f"{platform}:{uid}", text))
    return sorted(messages, key=lambda item: item.time)


def raw_times(root):
    for node in root.findall("d"):
        try:
            value = float(node.get("p", "").split(",")[0])
        except ValueError:
            continue
        if math.isfinite(value):
            yield value


def absolute_clock_origin(
    root,
    *,
    relative_offset: float = 0.0,
    relative_start: float | None = None,
    relative_end: float | None = None,
) -> tuple[float | None, int]:
    """Estimate wall-clock time at local media time zero from XML timestamps."""
    samples = []
    for node in root.findall("d"):
        fields = node.get("p", "").split(",")
        try:
            relative = float(fields[0])
            absolute = float(fields[4])
        except (ValueError, IndexError):
            continue
        if not math.isfinite(relative) or not math.isfinite(absolute):
            continue
        if absolute < MIN_ABSOLUTE_TIMESTAMP:
            continue
        if relative_start is not None and relative < relative_start:
            continue
        if relative_end is not None and relative > relative_end:
            continue
        samples.append(absolute - (relative - relative_offset))
    if not samples:
        return None, 0

    center = statistics.median(samples)
    deviations = [abs(value - center) for value in samples]
    deviation = statistics.median(deviations)
    limit = max(2.0, deviation * 6.0)
    inliers = [value for value in samples if abs(value - center) <= limit]
    return statistics.median(inliers), len(inliers)


def build_video_clock(root, segments, fallback_origin: float) -> tuple[VideoClockSegment, ...]:
    result = []
    for segment in segments:
        start = segment["offset"]
        end = start + segment["duration"]
        origin, sample_count = absolute_clock_origin(
            root,
            relative_offset=start,
            relative_start=start,
            relative_end=end,
        )
        if origin is not None:
            basis = "xml-comment-absolute-time"
        else:
            origin = segment["created_at"]
            basis = "recording-created-at"
        if origin is None and len(segments) == 1:
            origin = fallback_origin
            basis = "xml-file-created-at"
        if origin is None:
            continue
        result.append(VideoClockSegment(
            xml_file=segment["xml_file"],
            video_start=start,
            duration=segment["duration"],
            clock_origin=origin,
            clock_basis=basis,
            timestamp_samples=sample_count,
        ))
    return tuple(result)


def map_wall_time(
    wall_time: float,
    video_clock: tuple[VideoClockSegment, ...],
    preferred_xml: str | None = None,
) -> tuple[float, VideoClockSegment] | None:
    candidates = []
    for segment in video_clock:
        mapped = segment.map_wall_time(wall_time)
        if mapped is not None:
            candidates.append((mapped, segment))
    if not candidates:
        return None
    return next(
        (candidate for candidate in candidates if candidate[1].xml_file == preferred_xml),
        candidates[0],
    )


def map_wall_coverage(start: float, end: float, video_clock) -> list[tuple[float, float]]:
    coverage = []
    for segment in video_clock:
        wall_start = max(start, segment.clock_origin)
        wall_end = min(end, segment.wall_end)
        if wall_start < wall_end:
            coverage.append((
                segment.video_start + wall_start - segment.clock_origin,
                segment.video_start + wall_end - segment.clock_origin,
            ))
    return coverage


def stable_intervals(messages, duration: float) -> list[EnergyInterval]:
    if not messages:
        return []
    times = [message.time for message in messages]
    half = ENERGY_WINDOW_SECONDS / 2
    end = min(math.ceil(duration), math.ceil(times[-1] + half))
    counts = [
        bisect.bisect_left(times, t + half) - bisect.bisect_left(times, t - half)
        for t in range(end + 1)
    ]
    busy = max(float(ENERGY_MIN_MESSAGES), engine.quantile(counts, 0.82))
    very_busy = max(busy, engine.quantile(counts, 0.94))
    intervals = []
    pending = []
    active_start = None
    peak = None
    last_high = 0.0
    falling = 0
    for second, count in enumerate(counts):
        left, right = second - half, second + half
        a, b = bisect.bisect_left(times, left), bisect.bisect_left(times, right)
        baseline_left = bisect.bisect_left(times, max(0, left - engine.BASELINE_SECONDS))
        span = max(ENERGY_WINDOW_SECONDS, min(engine.BASELINE_SECONDS, max(0, left)))
        metrics = engine.window_metrics(
            messages[a:b],
            {m.user for m in messages[baseline_left:a]},
            (a - baseline_left) / span,
            window_seconds=ENERGY_WINDOW_SECONDS,
        )
        high = bool(
            metrics
            and count >= busy
            and metrics["unique_users"] >= ENERGY_MIN_USERS
            and (metrics["density_lift"] >= ENERGY_MIN_DENSITY_LIFT or count >= very_busy)
            and metrics["score"] >= ENERGY_MIN_SCORE
        )
        if high:
            falling = 0
            last_high = float(second)
            if active_start is None:
                pending.append((float(second), metrics))
                if len(pending) >= STABLE_RISE_SECONDS:
                    active_start = pending[0][0]
                    peak = max((m for _t, m in pending), key=lambda m: m["score"])
            elif metrics["score"] > peak["score"]:
                peak = metrics
        else:
            pending = []
            if active_start is not None:
                falling += 1
                if falling >= STABLE_FALL_SECONDS:
                    intervals.append(EnergyInterval(active_start, min(duration, last_high + 1), peak))
                    active_start, peak = None, None
                    falling = 0
    if active_start is not None:
        intervals.append(EnergyInterval(active_start, min(duration, last_high + 1), peak))
    return intervals


def laughter_clusters(messages, start: float, end: float) -> list[dict]:
    hits = [
        m for m in messages
        if start <= m.time <= end and engine.LAUGHTER_SIGNAL_PATTERN.search(m.text)
    ]
    result = []
    for index, hit in enumerate(hits):
        group = []
        for following in hits[index:]:
            if following.time - hit.time >= LAUGHTER_CLUSTER_SECONDS:
                break
            group.append(following)
        users = {m.user for m in group}
        if len(users) < engine.LAUGHTER_KEY_MIN_USERS:
            continue
        if result and hit.time <= result[-1]["end"]:
            result[-1]["end"] = max(result[-1]["end"], group[-1].time)
            result[-1]["max_unique_users"] = max(result[-1]["max_unique_users"], len(users))
        else:
            result.append({"start": hit.time, "end": group[-1].time, "max_unique_users": len(users)})
    return result


def intersect(a, b) -> bool:
    return max(a[0], b[0]) < min(a[1], b[1])


def merge_platform_candidates(bili_candidates, dy_candidates, duration):
    """Keep all single-platform events and fuse only intersecting event spans."""
    pairs = []
    for b_index, b in enumerate(bili_candidates):
        b_span = (b.trigger_time_seconds, min(duration, b.event_end_seconds))
        for d_index, d in enumerate(dy_candidates):
            d_span = (d.trigger_time_seconds, min(duration, d.event_end_seconds))
            if intersect(b_span, d_span):
                overlap = min(b_span[1], d_span[1]) - max(b_span[0], d_span[0])
                pairs.append((overlap, max(b.score, d.score), b_index, d_index))

    matched_b, matched_d, matches = set(), set(), {}
    for _overlap, _score, b_index, d_index in sorted(pairs, reverse=True):
        if b_index in matched_b or d_index in matched_d:
            continue
        matched_b.add(b_index)
        matched_d.add(d_index)
        matches[b_index] = d_index

    result = []
    for b_index, b in enumerate(bili_candidates):
        if b_index in matches:
            d = dy_candidates[matches[b_index]]
            row = asdict(b)
            row.update(
                xml_file=b.xml_file,
                event_end_seconds=min(duration, b.event_end_seconds),
                end_policy="dual-confirmed-bilibili-boundary",
                platform_evidence={
                    "boundary_correction": "disabled-use-bilibili-event",
                    "bilibili_interval": [b.trigger_time_seconds, min(duration, b.event_end_seconds)],
                    "douyin_interval": [d.trigger_time_seconds, min(duration, d.event_end_seconds)],
                    "confidence": "high",
                },
                policy_version=POLICY_VERSION,
            )
        else:
            row = asdict(b)
            row.update(
                event_end_seconds=min(duration, b.event_end_seconds),
                end_policy="absolute-time-bilibili",
                platform_evidence={"boundary_correction": "none-no-dual-overlap"},
                policy_version=POLICY_VERSION,
            )
        result.append(row)

    for d_index, d in enumerate(dy_candidates):
        if d_index in matched_d:
            continue
        row = asdict(d)
        row.update(
            event_end_seconds=min(duration, d.event_end_seconds),
            end_policy="absolute-time-douyin",
            platform_evidence={"boundary_correction": "none-no-dual-overlap"},
            policy_version=POLICY_VERSION,
        )
        result.append(row)
    return result


def restore_stable_tails(candidates, bili_intervals, dy_intervals, combined_intervals):
    """Keep selection intact; stop at the first stable fall in the source event."""
    for candidate in candidates:
        policy = candidate["end_policy"]
        intervals = (
            dy_intervals if policy == "absolute-time-douyin"
            else combined_intervals if policy == "watchable-shuangju"
            else bili_intervals
        )
        trigger = float(candidate["trigger_time_seconds"])
        original_end = float(candidate["event_end_seconds"])
        matching = sorted(
            (event for event in intervals
             if event.end > trigger and event.start <= original_end),
            key=lambda event: event.start,
        )
        # Do not chase a second wave after the first stable fall.
        selected = matching[0] if matching else None
        end = min(original_end, selected.end) if selected else original_end
        candidate["event_end_seconds"] = end
        candidate.setdefault("platform_evidence", {})["tail_boundary"] = {
            "policy": "first-stable-fall" if selected else "unchanged-no-stable-interval",
            "original_end": original_end,
            "selected_interval": [selected.start, selected.end] if selected else None,
            "end": end,
        }


def supplemental_shuangju_candidates(xml_path, messages, existing_candidates, duration):
    """Add sustained positive-payoff moments not already covered by an event."""
    hits = sorted(
        (message for message in messages if SHUANGJU_PATTERN.search(message.text)),
        key=lambda item: item.time,
    )
    clusters = []
    for hit in hits:
        if not clusters or hit.time - clusters[-1][-1].time > WATCHABLE_LINK_GAP_SECONDS:
            clusters.append([hit])
        else:
            clusters[-1].append(hit)

    ordered = sorted(messages, key=lambda item: item.time)
    times = [message.time for message in ordered]
    result = []
    for cluster in clusters:
        users = set()
        consensus = None
        for hit in cluster:
            users.add(hit.user)
            if len(users) >= WATCHABLE_MIN_USERS:
                consensus = hit.time
                break
        if consensus is None:
            continue

        event_start, event_end = cluster[0].time, cluster[-1].time
        if any(
            max(event_start, float(candidate["trigger_time_seconds"]))
            <= min(event_end, float(candidate["event_end_seconds"]))
            for candidate in existing_candidates
        ):
            continue

        window_start = max(0.0, event_start - ENERGY_WINDOW_SECONDS)
        window_end = min(duration, event_end + ENERGY_WINDOW_SECONDS)
        left = bisect.bisect_left(times, window_start)
        right = bisect.bisect_right(times, window_end)
        baseline_start = max(0.0, window_start - engine.BASELINE_SECONDS)
        baseline_left = bisect.bisect_left(times, baseline_start)
        baseline_span = max(1.0, window_start - baseline_start)
        metrics = engine.window_metrics(
            ordered[left:right],
            {message.user for message in ordered[baseline_left:left]},
            (left - baseline_left) / baseline_span,
            window_seconds=max(1.0, window_end - window_start),
        )
        if metrics is None:
            continue
        metrics["dominant_reaction"] = "爽局体验"
        row = asdict(engine.Candidate(
            xml_file=Path(xml_path).name,
            program_time_seconds=max(0.0, consensus - SHUANGJU_PRE_ROLL_SECONDS),
            trigger_time_seconds=consensus,
            event_end_seconds=min(duration, event_end),
            **metrics,
        ))
        platform_users = {}
        for hit in cluster:
            platform = hit.user.partition(":")[0]
            platform_users.setdefault(platform, set()).add(hit.user)
        row.update(
            end_policy="watchable-shuangju",
            platform_evidence={
                "theme": "shuangju",
                "event_interval": [event_start, min(duration, event_end)],
                "consensus_time_seconds": consensus,
                "consensus_rule": f"{WATCHABLE_MIN_USERS}-unique-users",
                "keyword_messages": len(cluster),
                "unique_users": len({hit.user for hit in cluster}),
                "platform_unique_users": {
                    platform: len(platform_set)
                    for platform, platform_set in sorted(platform_users.items())
                },
                "boundary_correction": "180s-pre-roll-normal-theme-end",
            },
            policy_version=POLICY_VERSION,
        )
        result.append(row)
    return result


def recording_segments(root, xml_path, duration):
    elements = root.findall("./recording_segments/segment")
    if not elements:
        return [{"xml_file": xml_path.name, "offset": 0.0, "duration": duration, "created_at": None}]
    segments = []
    for element in elements:
        segment = {
            "xml_file": element.attrib["xml_file"],
            "offset": float(element.attrib["offset"]),
            "duration": float(element.attrib["duration"]),
            "created_at": timestamp(element.get("created_at")),
        }
        if not all(math.isfinite(segment[key]) for key in ("offset", "duration")) or segment["duration"] <= 0:
            raise ValueError("Invalid merged recording segment")
        segments.append(segment)
    return segments


def discover_sources(douyin_root, segments, room_url):
    sources, errors = [], []
    if not room_url:
        return sources, errors
    seen = set()
    for metadata_path in sorted(douyin_root.glob("*/时间轴/*.timeline.json")):
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if not isinstance(metadata, dict):
                raise ValueError("Recording metadata must be an object")
            source_room = metadata.get("room_url")
            if not isinstance(source_room, str):
                raise ValueError("Recording metadata has no valid room URL")
            if source_room.rstrip("/") != room_url.rstrip("/"):
                continue
            linked_video = metadata.get("bilibili_video")
            linked_xml = Path(linked_video).with_suffix(".xml").name if linked_video else None
            matched = next((
                segment for segment in segments
                if linked_xml == segment["xml_file"]
                or metadata.get("xml_file") == segment["xml_file"]
            ), None)
            if matched is None:
                continue
            name = metadata["xml_file"]
            if Path(name).name != name:
                raise ValueError("Invalid source XML basename")
            path = (metadata_path.parent.parent / name).resolve()
            if path in seen:
                continue
            seen.add(path)
            tree = ET.parse(path)
            if tree.getroot().tag != "i":
                raise ValueError("Invalid danmaku XML root")
            sources.append((path, tree, metadata, matched))
        except (OSError, ValueError, TypeError, KeyError, ET.ParseError) as error:
            errors.append({"metadata": str(metadata_path), "error": str(error)})
    return sources, errors


def assign_groups(candidates):
    candidates.sort(key=lambda item: item["trigger_time_seconds"])
    group, previous = 0, -math.inf
    for item in candidates:
        trigger = item["trigger_time_seconds"]
        if trigger - previous > engine.CLIP_GROUP_SECONDS:
            group += 1
        item["clip_group"] = group
        item["clip_group_reason"] = "consecutive-trigger-gap"
        previous = trigger


def build_program(xml_path: Path, douyin_root: Path, output_dir: Path, duration: float, room_url: str):
    """Write isolated fused XML, timeline and evidence; never touch source files."""
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("A positive media duration is required")
    tree = ET.parse(xml_path)
    if tree.getroot().tag != "i":
        raise ValueError("Invalid Bilibili danmaku XML root")
    segments = recording_segments(tree.getroot(), xml_path, duration)
    sources, errors = discover_sources(douyin_root, segments, room_url)
    if sources and (output_dir / xml_path.name).resolve() in {xml_path.resolve(), *(path for path, *_ in sources)}:
        raise ValueError("Analysis output must be isolated from source XML files")
    bili_raw = [m for m in comments_from_root(tree.getroot(), "bilibili") if 0 <= m.time <= duration]
    bili, bili_ignored = filter_uid_spam(bili_raw)
    video_clock = build_video_clock(tree.getroot(), segments, xml_path.stat().st_ctime)
    missing_clock = {segment["xml_file"] for segment in segments} - {item.xml_file for item in video_clock}
    for name in sorted(missing_clock):
        errors.append({"recording_segment": name, "error": "No absolute time for video segment"})
    root = copy.deepcopy(tree.getroot())
    all_nodes = []
    for node in list(root.findall("d")):
        root.remove(node)
        try:
            relative = float(node.get("p", "").split(",")[0])
        except ValueError:
            continue
        if math.isfinite(relative) and 0 <= relative <= duration:
            node.set("platform", "bilibili")
            all_nodes.append((relative, node))
    trusted_raw, source_reports = [], []
    trusted_coverage = []
    for path, source_tree, metadata, segment in sources:
        source_root = source_tree.getroot()
        source_origin, source_samples = absolute_clock_origin(source_root)
        source_basis = "xml-comment-absolute-time"
        if source_origin is None:
            source_origin = timestamp(metadata.get("segment_started_at"))
            source_basis = "recording-started-at"
        if source_origin is None:
            source_origin = path.stat().st_ctime
            source_basis = "xml-file-created-at"

        preferred_clock = next(
            (item for item in video_clock if item.xml_file == segment["xml_file"]),
            None,
        )
        raw = comments_from_root(source_root, "douyin")
        mapped = []
        for message in raw:
            result = map_wall_time(
                source_origin + message.time,
                video_clock,
                preferred_xml=segment["xml_file"],
            )
            if result is not None:
                mapped.append(engine.Message(result[0], message.user, message.text))
        identified, ignored = filter_uid_spam(mapped)
        trusted_raw.extend(mapped)

        d_start = timestamp(metadata.get("segment_started_at"))
        d_end = timestamp(metadata.get("segment_ended_at"))
        raw_span = max(raw_times(source_root), default=0)
        segment_span = max(0.0, d_end - d_start) if d_start is not None and d_end is not None else raw_span
        coverage = map_wall_coverage(source_origin, source_origin + segment_span, video_clock)
        trusted_coverage.extend(coverage)

        added = 0
        outside_video = 0
        for original in source_root.findall("d"):
            fields = original.get("p", "").split(",")
            try:
                relative = float(fields[0])
            except (ValueError, IndexError):
                continue
            result = map_wall_time(
                source_origin + relative,
                video_clock,
                preferred_xml=segment["xml_file"],
            )
            if result is None:
                outside_video += 1
                continue
            mapped_time, mapped_segment = result
            node = copy.deepcopy(original)
            fields[0] = f"{mapped_time:.3f}"
            node.set("p", ",".join(fields))
            node.set("platform", "douyin")
            node.set("alignment", "absolute-time")
            node.set("source", path.parent.name)
            node.set("video_segment", mapped_segment.xml_file)
            all_nodes.append((mapped_time, node))
            added += 1

        nominal_offset = None
        if preferred_clock is not None:
            nominal_offset = segment["offset"] + source_origin - preferred_clock.clock_origin
        source_reports.append({
            "xml": str(path),
            "mode": "absolute-time-aligned" if coverage else "outside-video-coverage",
            "alignment": {
                "trusted": bool(coverage),
                "reason": "absolute-timestamp-video-coverage",
                "source_clock_origin": source_origin,
                "source_clock_basis": source_basis,
                "source_timestamp_samples": source_samples,
                "preferred_video_segment": segment["xml_file"],
                "preferred_video_clock_basis": preferred_clock.clock_basis if preferred_clock else None,
                "absolute_offset_seconds": nominal_offset,
            },
            "video_coverage": coverage,
            "raw_comments": len(source_root.findall("d")),
            "identified_comments": len(raw), "filtered_comments": len(identified),
            "ignored_missing_uid": len(source_root.findall("d")) - len(raw),
            "outside_video_coverage": outside_video,
            "ignored_spam": ignored, "added_comments": added,
        })

    # All timing is frozen above. Program detection cannot move the absolute timeline.
    trusted, _ = filter_uid_spam(trusted_raw)
    bili_intervals = stable_intervals(bili, duration)
    dy_intervals = stable_intervals(trusted, duration)
    bili_candidates = engine.find_candidates(xml_path, bili_raw)
    dy_candidates = engine.find_candidates(xml_path, trusted_raw)
    candidates = merge_platform_candidates(bili_candidates, dy_candidates, duration)
    combined, combined_ignored = filter_uid_spam(bili_raw + trusted_raw)
    candidates.extend(supplemental_shuangju_candidates(
        xml_path, combined, candidates, duration,
    ))
    restore_stable_tails(
        candidates, bili_intervals, dy_intervals, stable_intervals(combined, duration),
    )
    rejected = []
    assign_groups(candidates)

    output_dir.mkdir(parents=True, exist_ok=True)
    analysis_xml = xml_path
    if sources:
        analysis_xml = output_dir / xml_path.name
        for _time_value, node in sorted(all_nodes, key=lambda item: item[0]):
            root.append(node)
        ET.ElementTree(root).write(analysis_xml, encoding="utf-8", xml_declaration=True)
    timeline_path = output_dir / f"{xml_path.stem}.json"
    timeline_path.write_text(json.dumps(candidates, ensure_ascii=False, indent=2), encoding="utf-8")
    report = {
        "policy_version": POLICY_VERSION, "source_xml": str(xml_path), "analysis_xml": str(analysis_xml),
        "duration_seconds": duration, "candidate_count": len(candidates),
        "alignment_policy": "absolute-comment-time-with-recorded-video-coverage",
        "video_clock_segments": [
            {
                **asdict(item),
                "wall_start": item.clock_origin,
                "wall_end": item.wall_end,
            }
            for item in video_clock
        ],
        "settings": {
            "energy_window_seconds": ENERGY_WINDOW_SECONDS,
            "stable_rise_seconds": STABLE_RISE_SECONDS, "stable_fall_seconds": STABLE_FALL_SECONDS,
            "energy_min_messages": ENERGY_MIN_MESSAGES, "energy_min_users": ENERGY_MIN_USERS,
            "energy_min_score": ENERGY_MIN_SCORE, "energy_min_density_lift": ENERGY_MIN_DENSITY_LIFT,
            "laughter_cluster_seconds": LAUGHTER_CLUSTER_SECONDS,
            "laughter_min_users": engine.LAUGHTER_KEY_MIN_USERS,
            "watchable_link_gap_seconds": WATCHABLE_LINK_GAP_SECONDS,
            "watchable_min_users": WATCHABLE_MIN_USERS,
            "shuangju_pre_roll_seconds": SHUANGJU_PRE_ROLL_SECONDS,
            "pre_roll_seconds": engine.PRE_ROLL_SECONDS, "clip_group_seconds": engine.CLIP_GROUP_SECONDS,
        },
        "video_gap_policy": "drop-only-outside-recorded-video-coverage",
        "silent_video_policy": "keep-douyin-comments-throughout-recorded-segment",
        "routine_policy": "exclude-from-echo-only-not-laughter-or-whole-events",
        "uid_policy": "15s-first-and-chronological-middle; missing UIDs never count as users",
        "combined_ignored_uid_or_spam": combined_ignored,
        "bilibili_ignored_uid_or_spam": bili_ignored, "sources": source_reports, "source_errors": errors,
        "bilibili_intervals": [asdict(item) for item in bili_intervals],
        "douyin_intervals": [asdict(item) for item in dy_intervals],
        "rejected": rejected,
    }
    (output_dir / "alignment_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return timeline_path.resolve(), analysis_xml.resolve(), report
