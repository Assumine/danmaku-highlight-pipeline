from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
import re
import statistics
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

from danmaku_uid import filter_uid_spam


WINDOW_SECONDS = 24
BASELINE_SECONDS = 180
PRE_ROLL_SECONDS = 40
MERGE_SECONDS = 30
STEP_SECONDS = 2
PAYOFF_LOOKAHEAD_SECONDS = 60
PAYOFF_LINK_GAP_SECONDS = 12
PAYOFF_MIN_USERS = 4
PAYOFF_TAIL_SECONDS = 15
CLIP_GROUP_SECONDS = 100
LAUGHTER_KEY_MIN_USERS = 2
LAUGHTER_SIGNAL_PATTERN = re.compile(
    r"哈{2,}|笑|乐死|乐了|绷不住|蚌埠住|233|嘲笑|呲牙笑|"
    r"room_\d+_(?:79782|79508)|"
    r"\[(?:大笑|呲牙|坏笑|尬笑)\]|\b乐\b",
    re.I,
)

REACTION_PATTERNS = {
    "笑点": LAUGHTER_SIGNAL_PATTERN,
    "惊讶反转": re.compile(r"卧槽|我[操艹草]|哦豁|什么情况|真的假的|不会吧|完了|寄了|离谱|逆天|震惊|\?{2,}|？{2,}"),
    "吐槽冲突": re.compile(r"急了|红温|破防|嘴硬|自私|卑鄙|内鬼|害人精|小丑|出生|畜生|菜|怂"),
    "精彩操作": re.compile(r"牛|厉害|漂亮|好帅|天秀|神了|精彩|666|6{3,}|操作"),
    "翻车结果": re.compile(r"白给|翻车|送了|炸了|爆了|死了|没了|掉了|亏了|寄|失败"),
    "集体疑问": re.compile(r"怎么|为什么|为啥|啥|什么|哪来的|发生了|[?？]"),
}

ROUTINE_PATTERN = re.compile(
    r"开播|开了|来了|迟到|晚上好|早上好|晚安|拜拜|再见|下播|休息|睡觉|明天见|"
    r"最后一把|再来一把|加班|关注|福袋|早退|睡了|溜了|播多久|"
    r"卡了|掉线|断流|黑屏|没声音|画面|延迟|网络|测试|谢谢|礼物|舰长|上舰|充电|"
    r"老板糊涂|直播|录播|回归|回来|失踪|灯牌|牌子|取关"
)
PLACEHOLDER_PATTERN = re.compile(r"^表情【.*】$|^\[[^\]]+\]$")
LAUGHTER_PAYOFF_PATTERN = LAUGHTER_SIGNAL_PATTERN
BRACKET_PATTERN = re.compile(r"\[[^\]]+\]|【[^】]+】")
PUNCT_PATTERN = re.compile(r"[\s\W_]+", re.UNICODE)


@dataclass(frozen=True)
class Message:
    time: float
    user: str
    text: str


@dataclass
class Candidate:
    xml_file: str
    program_time_seconds: float
    trigger_time_seconds: float
    event_end_seconds: float
    score: float
    messages: int
    unique_users: int
    effective_messages: int
    density_lift: float
    new_user_ratio: float
    reaction_users: int
    reaction_ratio: float
    laughter_users: int
    laughter_messages: int
    laughter_ratio: float
    echo_users: int
    echo_text: str
    routine_ratio: float
    placeholder_ratio: float
    dominant_reaction: str
    reaction_breakdown: dict
    evidence: list
    payoff_start_seconds: float | None = None
    payoff_end_seconds: float | None = None
    payoff_laughter_users: int = 0
    delayed_laughter_payoff: bool = False
    review_status: str = "待复核"
    review_note: str = ""
    clip_group: int = 0
    clip_group_reason: str = ""
    bvid: str = ""
    episode: str = ""
    no_danmaku_p: int = 0
    danmaku_p: int = 0


def parse_xml(path):
    messages = []
    root = ET.parse(path).getroot()
    for node in root.findall("d"):
        parts = node.attrib.get("p", "").split(",")
        if not parts:
            continue
        try:
            timestamp = float(parts[0])
        except ValueError:
            continue
        user = parts[6] if len(parts) > 6 and parts[6] else "unknown"
        text = (node.text or "").strip()
        if text:
            messages.append(Message(timestamp, user, text))
    messages.sort(key=lambda item: item.time)
    return messages


def normalize_text(text):
    expression = re.fullmatch(r"表情【(.+)】", text)
    if expression:
        return f"表情:{expression.group(1).lower()}"
    text = BRACKET_PATTERN.sub("", text).lower()
    text = re.sub(r"哈{2,}", "哈哈", text)
    return PUNCT_PATTERN.sub("", text)


def reaction_groups(text):
    return [name for name, pattern in REACTION_PATTERNS.items() if pattern.search(text)]


def quantile(values, fraction):
    if not values:
        return 0.0
    ordered = sorted(values)
    index = (len(ordered) - 1) * fraction
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return float(ordered[lower])
    weight = index - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def window_metrics(current, prior_users, baseline_rate, *, window_seconds=WINDOW_SECONDS):
    if not current:
        return None

    user_counts = Counter(message.user for message in current)
    unique_users = len(user_counts)
    effective_messages = len(current)
    current_users = set(user_counts)
    new_user_ratio = len(current_users - prior_users) / max(1, unique_users)

    category_users = defaultdict(set)
    reaction_user_set = set()
    laughter_user_set = set()
    laughter_messages = 0
    routine_users = set()
    placeholder_count = 0
    normalized_users = defaultdict(set)
    representative = Counter()

    for message in current:
        groups = reaction_groups(message.text)
        for group in groups:
            category_users[group].add(message.user)
            reaction_user_set.add(message.user)
        is_laughter = bool(LAUGHTER_SIGNAL_PATTERN.search(message.text))
        if is_laughter:
            laughter_user_set.add(message.user)
            laughter_messages += 1
        is_routine = bool(ROUTINE_PATTERN.search(message.text))
        if is_routine:
            routine_users.add(message.user)
        if PLACEHOLDER_PATTERN.match(message.text) and not is_laughter:
            placeholder_count += 1
        normalized = normalize_text(message.text)
        if normalized:
            representative[message.text] += 1
            if not is_routine:
                normalized_users[normalized].add(message.user)

    echo_text = ""
    echo_users = 0
    for normalized, users in normalized_users.items():
        if len(normalized) >= 1 and len(users) > echo_users:
            echo_users = len(users)
            echo_text = normalized

    reaction_breakdown = {
        name: len(category_users.get(name, set())) for name in REACTION_PATTERNS
    }
    dominant_reaction = max(reaction_breakdown, key=reaction_breakdown.get)
    if reaction_breakdown[dominant_reaction] == 0:
        dominant_reaction = "人群突增"

    count = len(current)
    density_lift = (count / window_seconds) / max(0.02, baseline_rate)
    reaction_users = len(reaction_user_set)
    reaction_ratio = reaction_users / max(1, unique_users)
    laughter_users = len(laughter_user_set)
    laughter_ratio = laughter_users / max(1, unique_users)
    routine_ratio = len(routine_users) / max(1, unique_users)
    placeholder_ratio = placeholder_count / max(1, count)

    score = (
        min(4.0, math.log2(max(1.0, density_lift)) * 1.8)
        + min(3.0, unique_users / 4.0)
        + min(3.0, reaction_users / 3.0)
        + min(2.5, laughter_users / 2.0)
        + min(2.0, echo_users / 2.0)
        + min(1.5, new_user_ratio * 1.5)
        - placeholder_ratio * 2.0
    )

    evidence = [text for text, _ in representative.most_common(5)]
    return {
        "score": round(score, 3),
        "messages": count,
        "unique_users": unique_users,
        "effective_messages": effective_messages,
        "density_lift": round(density_lift, 3),
        "new_user_ratio": round(new_user_ratio, 3),
        "reaction_users": reaction_users,
        "reaction_ratio": round(reaction_ratio, 3),
        "laughter_users": laughter_users,
        "laughter_messages": laughter_messages,
        "laughter_ratio": round(laughter_ratio, 3),
        "echo_users": echo_users,
        "echo_text": echo_text,
        "routine_ratio": round(routine_ratio, 3),
        "placeholder_ratio": round(placeholder_ratio, 3),
        "dominant_reaction": dominant_reaction,
        "reaction_breakdown": reaction_breakdown,
        "evidence": evidence,
    }


def find_laughter_payoff(messages, trigger_time):
    hits = [
        message
        for message in messages
        if trigger_time <= message.time <= trigger_time + PAYOFF_LOOKAHEAD_SECONDS
        and LAUGHTER_PAYOFF_PATTERN.search(message.text)
    ]
    if not hits:
        return None

    groups = [[hits[0]]]
    for message in hits[1:]:
        if message.time - groups[-1][-1].time <= PAYOFF_LINK_GAP_SECONDS:
            groups[-1].append(message)
        else:
            groups.append([message])

    for group in groups:
        users = {message.user for message in group}
        if len(users) >= PAYOFF_MIN_USERS:
            return group[0].time, group[-1].time, len(users)
    return None


def find_candidates(path, messages):
    messages, _ignored = filter_uid_spam(messages)
    if not messages:
        return []

    duration = messages[-1].time
    times = [message.time for message in messages]

    def message_slice(start, end):
        left = bisect.bisect_left(times, start)
        right = bisect.bisect_left(times, end)
        return messages[left:right]

    session_bins = []
    for start in range(0, int(duration) + STEP_SECONDS, STEP_SECONDS):
        left = bisect.bisect_left(times, start)
        right = bisect.bisect_left(times, start + WINDOW_SECONDS)
        count = right - left
        session_bins.append(count)
    busy_count = max(8.0, quantile(session_bins, 0.82))

    windows = []
    for start in range(0, int(duration) + STEP_SECONDS, STEP_SECONDS):
        end = start + WINDOW_SECONDS
        before_start = max(0, start - BASELINE_SECONDS)
        baseline_messages = message_slice(before_start, start)
        baseline_span = max(WINDOW_SECONDS, start - before_start)
        baseline_rate = len(baseline_messages) / baseline_span
        prior_users = {message.user for message in message_slice(max(0, start - 120), start)}
        metrics = window_metrics(message_slice(start, end), prior_users, baseline_rate)
        if not metrics:
            continue

        has_laughter_key = (
            metrics["laughter_users"] >= LAUGHTER_KEY_MIN_USERS
            and metrics["laughter_messages"] >= LAUGHTER_KEY_MIN_USERS
        )
        is_collective = (
            metrics["messages"] >= busy_count
            and metrics["unique_users"] >= 6
            and metrics["effective_messages"] >= 8
        )
        is_above_baseline = (
            metrics["density_lift"] >= 1.45
            or metrics["messages"] >= quantile(session_bins, 0.94)
        )
        is_not_placeholder_only = metrics["placeholder_ratio"] < 0.65

        if (
            is_collective
            and is_above_baseline
            and has_laughter_key
            and is_not_placeholder_only
            and metrics["score"] >= 6.0
        ):
            windows.append((start, end, metrics))

    if not windows:
        return []

    events = []
    group = [windows[0]]
    for window in windows[1:]:
        if window[0] - group[-1][0] <= MERGE_SECONDS:
            group.append(window)
        else:
            events.append(group)
            group = [window]
    events.append(group)

    candidates = []
    for group in events:
        peak = max(group, key=lambda item: item[2]["score"])
        event_start = group[0][0]
        event_end = max(item[1] for item in group)
        metrics = peak[2]
        strong_reaction = (
            metrics["score"] >= 9.0
            and metrics["laughter_users"] >= LAUGHTER_KEY_MIN_USERS
        )
        collective_echo = (
            metrics["score"] >= 8.4
            and metrics["laughter_users"] >= LAUGHTER_KEY_MIN_USERS
            and metrics["echo_users"] >= 5
            and metrics["unique_users"] >= 12
            and metrics["density_lift"] >= 3.0
            and metrics["new_user_ratio"] >= 0.7
        )
        if not (strong_reaction or collective_echo):
            continue
        candidate = Candidate(
                xml_file=path.name,
                program_time_seconds=max(0.0, event_start - PRE_ROLL_SECONDS),
                trigger_time_seconds=float(event_start),
                event_end_seconds=float(event_end),
                **metrics,
            )
        payoff = find_laughter_payoff(messages, candidate.trigger_time_seconds)
        if payoff:
            payoff_start, payoff_end, payoff_users = payoff
            candidate.payoff_start_seconds = payoff_start
            candidate.payoff_end_seconds = payoff_end
            candidate.payoff_laughter_users = payoff_users
            candidate.delayed_laughter_payoff = (
                payoff_start - candidate.trigger_time_seconds > 8.0
            )
            candidate.event_end_seconds = max(
                candidate.event_end_seconds,
                payoff_end + PAYOFF_TAIL_SECONDS,
            )
            if candidate.delayed_laughter_payoff:
                candidate.dominant_reaction = "笑点铺垫"
        candidates.append(candidate)
    return candidates


def format_time(seconds):
    total = max(0, int(seconds))
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def load_video_map(path):
    if not path or not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    mapping = {}
    for item in data:
        name = Path(item.get("ass_file", "")).with_suffix(".xml").name
        if name and name not in mapping:
            mapping[name] = {
                "bvid": item.get("bvid", ""),
                "episode": item.get("episode", ""),
                "no_danmaku_p": item.get("no_danmaku_p", 0),
                "danmaku_p": item.get("danmaku_p", 0),
            }
    return mapping


def attach_video_map(candidates, mapping):
    for candidate in candidates:
        item = mapping.get(candidate.xml_file, {})
        candidate.bvid = item.get("bvid", "")
        candidate.episode = item.get("episode", "")
        candidate.no_danmaku_p = int(item.get("no_danmaku_p", 0) or 0)
        candidate.danmaku_p = int(item.get("danmaku_p", 0) or 0)


def parse_clock(value):
    parts = [int(part) for part in value.split(":")]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    raise ValueError(f"Unsupported time value: {value}")


def apply_review_labels(candidates, path):
    if not path or not path.exists():
        return
    labels = json.loads(path.read_text(encoding="utf-8"))
    for label in labels:
        program_time = label.get("program_time")
        if not program_time:
            continue
        xml_file = Path(label.get("ass_file", "")).with_suffix(".xml").name
        target = parse_clock(program_time)
        nearby = [
            candidate
            for candidate in candidates
            if candidate.xml_file == xml_file
            and abs(candidate.program_time_seconds - target) <= 30
        ]
        if nearby:
            nearest = min(
                nearby,
                key=lambda candidate: abs(candidate.program_time_seconds - target),
            )
            nearest.review_status = label.get("status", "待复核")
            note = label.get("note", "")
            if note and note not in nearest.review_note:
                nearest.review_note = "；".join(
                    part for part in (nearest.review_note, note) if part
                )


def assign_clip_groups(candidates):
    current_file = None
    group_id = 0
    previous = None
    for candidate in candidates:
        if candidate.xml_file != current_file:
            current_file = candidate.xml_file
            group_id = 1
            candidate.clip_group = group_id
            candidate.clip_group_reason = "single"
            previous = candidate
            continue

        gap = candidate.program_time_seconds - previous.program_time_seconds
        if gap <= CLIP_GROUP_SECONDS:
            candidate.clip_group = group_id
            candidate.clip_group_reason = "within-100-seconds"
        else:
            group_id += 1
            candidate.clip_group = group_id
            candidate.clip_group_reason = "single"
        previous = candidate


def write_outputs(output_dir, candidates, file_stats):
    output_dir.mkdir(parents=True, exist_ok=True)
    data = []
    for index, candidate in enumerate(candidates, 1):
        item = asdict(candidate)
        item["id"] = index
        item["program_time"] = format_time(candidate.program_time_seconds)
        item["trigger_time"] = format_time(candidate.trigger_time_seconds)
        item["event_end"] = format_time(candidate.event_end_seconds)
        item["payoff_start"] = (
            format_time(candidate.payoff_start_seconds)
            if candidate.payoff_start_seconds is not None
            else ""
        )
        item["payoff_end"] = (
            format_time(candidate.payoff_end_seconds)
            if candidate.payoff_end_seconds is not None
            else ""
        )
        if candidate.bvid and candidate.danmaku_p:
            item["review_url"] = (
                f"https://www.bilibili.com/video/{candidate.bvid}"
                f"?p={candidate.danmaku_p}&t={int(candidate.program_time_seconds)}"
            )
        else:
            item["review_url"] = ""
        data.append(item)

    (output_dir / "新规则节目单.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    fieldnames = [
        "id", "review_status", "review_note", "clip_group", "clip_group_reason",
        "episode", "bvid", "danmaku_p", "no_danmaku_p",
        "xml_file", "program_time", "trigger_time", "event_end", "score",
        "dominant_reaction", "messages", "unique_users", "effective_messages",
        "density_lift", "new_user_ratio", "reaction_users", "reaction_ratio",
        "laughter_users", "laughter_messages", "laughter_ratio",
        "echo_users", "echo_text", "routine_ratio", "placeholder_ratio",
        "payoff_start", "payoff_end", "payoff_laughter_users",
        "delayed_laughter_payoff",
        "evidence", "review_url",
    ]
    with (output_dir / "新规则节目单.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for item in data:
            row = dict(item)
            row["evidence"] = " / ".join(row["evidence"])
            writer.writerow(row)

    lines = [
        "# 新规则节目单复核",
        "",
        "这份清单由观众行为的多维信号生成，未接入正式流程。`有效/无效`由人工回看填写。",
        "",
        f"- XML 文件：{len(file_stats)}",
        f"- 弹幕总数：{sum(item['messages'] for item in file_stats):,}",
        f"- 候选节目点：{len(data)}",
        "",
        "| # | 状态 | 期次/文件 | 分P | 剪辑组 | 节目点 | 类型 | 分数 | 观众证据 | 代表弹幕 | 回看 |",
        "|---:|---|---|---|---|---|---|---:|---|---|---|",
    ]
    for item in data:
        episode = item["episode"] or item["xml_file"]
        part = f"P{item['danmaku_p']}" if item["danmaku_p"] else "待映射"
        evidence = (
            f"{item['messages']}条/{item['unique_users']}人，"
            f"基线×{item['density_lift']:.2f}，反应{item['reaction_users']}人，"
            f"复读{item['echo_users']}人"
        )
        if item["delayed_laughter_payoff"]:
            delay = item["payoff_start_seconds"] - item["trigger_time_seconds"]
            evidence += (
                f"，铺垫后{delay:.0f}秒出现{item['payoff_laughter_users']}人笑点"
            )
        examples = " / ".join(text.replace("|", "\\|") for text in item["evidence"][:3])
        link = f"[打开]({item['review_url']})" if item["review_url"] else "-"
        lines.append(
            f"| {item['id']} | {item['review_status']} | {episode} | {part} | G{item['clip_group']} | "
            f"**{item['program_time']}** | {item['dominant_reaction']} | "
            f"{item['score']:.2f} | {evidence} | {examples} | {link} |"
        )
    (output_dir / "新规则节目单.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    per_file_dir = output_dir / "按场次复核"
    per_file_dir.mkdir(exist_ok=True)
    for xml_file, items in _group_by(data, "xml_file").items():
        title = items[0]["episode"] or xml_file
        file_lines = [
            f"# {title} - 新规则节目单",
            "",
            "请将状态改为 `有效` 或 `无效`；时间已经提前 40 秒，可直接打开有弹幕版回看。",
            "",
            "| # | 状态 | 剪辑组 | 节目点 | 类型 | 观众证据 | 代表弹幕 | 回看 |",
            "|---:|---|---|---|---|---|---|---|",
        ]
        for index, item in enumerate(items, 1):
            evidence = (
                f"{item['messages']}条/{item['unique_users']}人，"
                f"基线×{item['density_lift']:.2f}，反应{item['reaction_users']}人，"
                f"复读{item['echo_users']}人"
            )
            if item["delayed_laughter_payoff"]:
                delay = item["payoff_start_seconds"] - item["trigger_time_seconds"]
                evidence += (
                    f"，铺垫后{delay:.0f}秒出现{item['payoff_laughter_users']}人笑点"
                )
            examples = " / ".join(
                text.replace("|", "\\|") for text in item["evidence"][:3]
            )
            link = f"[打开]({item['review_url']})" if item["review_url"] else "-"
            file_lines.append(
                f"| {index} | {item['review_status']} | G{item['clip_group']} | **{item['program_time']}** | "
                f"{item['dominant_reaction']} | {evidence} | {examples} | {link} |"
            )
        (per_file_dir / f"{Path(xml_file).stem}.md").write_text(
            "\n".join(file_lines) + "\n", encoding="utf-8"
        )

    summary = {
        "xml_files": len(file_stats),
        "messages": sum(item["messages"] for item in file_stats),
        "candidates": len(data),
        "candidates_by_type": dict(Counter(item["dominant_reaction"] for item in data)),
        "score": {
            "min": min((item["score"] for item in data), default=0),
            "median": statistics.median((item["score"] for item in data)) if data else 0,
            "max": max((item["score"] for item in data), default=0),
        },
        "files": file_stats,
    }
    (output_dir / "统计.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _group_by(items, key):
    groups = defaultdict(list)
    for item in items:
        groups[item[key]].append(item)
    return groups


def main():
    parser = argparse.ArgumentParser(description="Generate V2 program candidates from danmaku XML files.")
    parser.add_argument("input_dir", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--video-map", type=Path)
    parser.add_argument("--review-labels", type=Path, action="append", default=[])
    args = parser.parse_args()

    candidates = []
    file_stats = []
    for path in sorted(args.input_dir.glob("*.xml")):
        messages = parse_xml(path)
        found = find_candidates(path, messages)
        candidates.extend(found)
        file_stats.append({"xml_file": path.name, "messages": len(messages), "candidates": len(found)})

    candidates.sort(key=lambda item: (item.xml_file, item.program_time_seconds))
    attach_video_map(candidates, load_video_map(args.video_map))
    for review_path in args.review_labels:
        apply_review_labels(candidates, review_path)
    assign_clip_groups(candidates)
    write_outputs(args.output_dir, candidates, file_stats)
    print(f"Parsed {len(file_stats)} XML files and generated {len(candidates)} candidates.")


if __name__ == "__main__":
    main()
