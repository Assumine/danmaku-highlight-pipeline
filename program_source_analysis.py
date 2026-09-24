"""Source-program evidence; independent of automatic clip selection."""

import json
import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import dual_platform_program as alignment
from danmaku_uid import filter_uid_spam
from program_rule_engine_v2 import Message, LAUGHTER_SIGNAL_PATTERN


BOSS_ROLE_NAMES = r"夜魔|夜行者|麦叔|水果刀|火箭筒|(?i:RPG)|追迹者|复仇之神|复仇女神|双子星|恶龙"
HERO_CONTEXT_ROLE_NAMES = r"地雷|机枪|加特林|大狙|激光"
HERO_EXCLUSIVE_ROLE_NAMES = r"狙击手|等离子"
HERO_ROLE_NAMES = rf"{HERO_CONTEXT_ROLE_NAMES}|{HERO_EXCLUSIVE_ROLE_NAMES}"
SHUANGJU_STREAMER_NAMES = r"坤(?:坤|哥)?|主播|鱼刺(?:坤)?"
SHUANGJU_COMPLETED_OUTCOMES = (
    r"(?:抓|吃|玩|打|刷)?爽(?:了|到了?|起来了?|死了?|翻了?|飞了?|爆了?)"
    r"|舒服了|赢麻了?"
)
SHUANGJU_ANCHOR_PATTERN = (
    rf"爽局|(?:让|给).{{0,4}}(?:{SHUANGJU_STREAMER_NAMES}).{{0,8}}(?:{SHUANGJU_COMPLETED_OUTCOMES})"
    rf"|(?:{SHUANGJU_STREAMER_NAMES}).{{0,8}}(?:{SHUANGJU_COMPLETED_OUTCOMES})"
)
SHUANGJU_SUPPORT_PATTERN = re.compile(r"爽|舒服|赢麻|碾压")
SHUANGJU_SUPPORT_SECONDS = 30

THEMES = {
    "猎人模式": (r"猎人", 210, 120, 6, 4, 40),
    "Boss英雄模式": (
        rf"(?i:boss)|英雄|(?:{BOSS_ROLE_NAMES})|(?:{HERO_EXCLUSIVE_ROLE_NAMES})",
        120, 150, 6, 4, 40,
    ),
    "爽局": (SHUANGJU_ANCHOR_PATTERN, 30, 0, 4, 4, 10),
    "自私坤": (r"自私|卑鄙|红温", 30, 0, 4, 4, 40),
    "内鬼坤": (r"内鬼|害人精", 30, 0, 4, 4, 40),
    "理财坤": (r"理财|血亏|亏了|亏麻|赚麻|赚了", 30, 0, 4, 4, 40),
}
MODE_NEIGHBOR_SECONDS = 60
MODE_ISLAND_MIN_USERS = 3
MODE_BRIDGE_MIN_USERS = 2
MODE_CATEGORIES = {"猎人模式", "Boss英雄模式"}
HUNTER_SHORT_MIN_DURATION = 45
HUNTER_SHORT_MIN_MESSAGES = 4
HUNTER_SHORT_MIN_USERS = 4
HUNTER_SHORT_MECHANICS_USERS = 3
HUNTER_SHORT_PRE_ROLL = 90
HUNTER_MECHANICS = re.compile(
    r"猎人.{0,12}(?:模式是什么|模式是啥|机制|怎么玩|咋玩|能买|买啥|技能|小弟|看得到|看不到|能看到|能看见)"
    r"|(?:什么|啥).{0,8}猎人模式"
)
HUNTER_ROLE_PATTERN = re.compile(
    r"(?:小鬼|打手|毒液|信息|地震|迅捷|迅速|幽暗|攀爬)\s*猎人"
)
HUNTER_STANDALONE_ROLE_PATTERN = re.compile(r"小鬼|毒液|地震|迅捷|迅速|幽暗|攀爬")
HUNTER_ROLE_SUPPORT_SECONDS = 120
BOSS_ROLE_PATTERN = re.compile(rf"(?:{BOSS_ROLE_NAMES})(?:\s*(?i:boss))?")
HERO_ROLE_PATTERN = re.compile(rf"(?:{HERO_ROLE_NAMES})")
BOSS_HERO_ROLE_PATTERN = re.compile(
    rf"(?:{HERO_ROLE_NAMES})\s*英雄"
    rf"|英雄\s*(?:{HERO_ROLE_NAMES})"
    rf"|(?:{HERO_EXCLUSIVE_ROLE_NAMES})"
    r"|人类\s*英雄|英雄\s*人类"
    r"|僵尸\s*(?i:boss)|(?i:boss)\s*僵尸"
    rf"|{BOSS_ROLE_PATTERN.pattern}"
)
BOSS_HERO_CURRENT_PATTERN = re.compile(
    r"(?:boss|英雄).{0,8}(?:来了|警告|技能|特性|死了|没了|在|模式|单位|是什么|是啥|怎么|能|会)"
    r"|(?:这|那个|新|最强|综合最强).{0,6}(?:boss|英雄)"
    r"|(?:变成|当上|成为|是).{0,4}(?:boss|英雄)"
    r"|(?:变|当).{0,4}(?:boss|英雄)(?:了|啦|咯)",
    re.IGNORECASE,
)
SPECIAL_MODE_CONTEXT_PATTERN = re.compile(r"特殊模式|(?:多重|特殊)?感染(?:模式)?|(?:军团)?对抗(?:模式)?")
BOSS_ROLE_SUPPORT_SECONDS = 90
MODE_EXCLUSIONS = {
    "Boss英雄模式": re.compile(r"英雄联盟|反恐精英OL"),
}
MODE_NONCURRENT_CONTEXT = re.compile(
    r"下(?:一)?把|下一局|下次|最后一把|(?:等|待|一)会(?:儿)?|"
    r"想(?:看|玩|打|当|变|要)|我要看|(?:快|速)来|"
    r"(?:再)?来(?:一)?(?:把|个|点)|整(?:个|一把)|安排(?:一把|一下)?|"
    r"(?:用|让).{0,8}(?:权限|管理员).{0,8}(?:开|调)|"
    r"(?:什么|啥)时候|何时|(?:变|玩|打)一把|"
    r"(?:能不能|可不可以|能否).{0,10}(?:玩|打|变|来|开|安排)|"
    r"盲猜|预测|猜(?:这|下)?把|(?:感觉|估计|应该|可能|大概).{0,6}(?:这|下)?把|"
    r"(?:不会|该不会|不可能).{0,10}(?:猎人|boss|英雄)|"
    r"要是|如果|假如|昨天|前天|昨晚|前晚|"
    r"(?:今天|今晚).{0,8}(?:玩|打|变|当)(?:了|过).{0,8}(?:吗|么)|"
    r"(?:没见|好久没|还没).{0,12}(?:变|玩|打|出|开)|"
    r"怎么(?:还)?没有.{0,12}(?:模式|boss|英雄|猎人)|"
    r"(?:猎人|boss|英雄).{0,8}比.{0,8}(?:猎人|boss|英雄)|"
    r"(?:视频|录播).{0,10}(?:看完|看了)|(?:看完|看了).{0,10}(?:视频|录播)",
    re.IGNORECASE,
)
REACTION = re.compile(r"哈{2,}|笑|绷不住|乐死|233|666|卧槽|离谱|急了|哦豁|自私|内鬼|害人精|理财|爽")


def clusters(messages, gap):
    result = []
    for message in sorted(messages, key=lambda m: m.time):
        if not result or message.time - result[-1][-1].time > gap:
            result.append([message])
        else:
            result[-1].append(message)
    return result


def theme_rejection_reason(group, min_duration, min_messages, min_users):
    if not group:
        return "insufficient-duration-messages-or-users"
    start, end = group[0].time, group[-1].time
    users = {message.user for message in group}
    if end - start < min_duration or len(group) < min_messages or len(users) < min_users:
        return "insufficient-duration-messages-or-users"
    if min_duration and end > start:
        middle = (start + end) / 2
        side_users = (
            {message.user for message in group if message.time < middle},
            {message.user for message in group if message.time >= middle},
        )
        if min(map(len, side_users)) < 2:
            return "one-sided-discussion"
    return None


def island_distance(left, right):
    if left[-1].time <= right[0].time:
        return right[0].time - left[-1].time
    return left[0].time - right[-1].time


def evidence(messages):
    return {
        platform: {
            "messages": len(rows), "users": len({m.user for m in rows}),
            "examples": list(dict.fromkeys(m.text for m in rows))[:6],
        }
        for platform in ("bilibili", "douyin")
        for rows in [[m for m in messages if m.user.startswith(platform + ":")]]
    }


def audience_user(message):
    return message.user.partition(":")[2] or message.user


def load_messages(ass_path, *, root, config=None):
    """Read fresh XML and align only linked, same-room Douyin recordings."""
    root = Path(root)
    ass_path = Path(ass_path)
    if not ass_path.is_absolute():
        ass_path = root / "backup" / ass_path
    xml = ass_path.with_suffix(".xml")
    if not xml.is_file():
        return None, {"mode": "ass-only", "warnings": ["No source XML; UID evidence unavailable"]}
    tree = ET.parse(xml)
    raw = alignment.comments_from_root(tree.getroot(), "bilibili")
    duration = max((m.time for m in raw), default=0.0)
    duration_basis = "last-source-comment-conservative-coverage"
    report_paths = list((root / "records" / "完整回放弹幕" / xml.stem).glob("alignment_report.json"))
    report_paths += list((root / "records" / "节目单自动生成").glob(f"*/{xml.stem}/alignment_report.json"))
    warnings = []
    for path in sorted(report_paths, key=lambda p: p.stat().st_mtime_ns, reverse=True):
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
            measured = float(report["duration_seconds"])
            if Path(report["source_xml"]).resolve() == xml.resolve() and math.isfinite(measured) and measured >= duration:
                duration, duration_basis = measured, "recorded-video-duration"
                break
        except (OSError, ValueError, KeyError, TypeError):
            continue
    if config is None:
        config_path = root / "config" / "douyin.json"
        try:
            config = json.loads(config_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            config = {}
    if not isinstance(config, dict):
        config = {}
    room = config.get("room_url")
    room = room.strip() if isinstance(room, str) else ""
    directory = config.get("output_directory") or "backup/抖音弹幕"
    segments = alignment.recording_segments(tree.getroot(), xml, duration)
    clock = alignment.build_video_clock(tree.getroot(), segments, xml.stat().st_ctime)
    sources, errors = alignment.discover_sources(root / directory, segments, room)
    warnings.extend(str(error) for error in errors)
    seen = set()
    source_rows = []
    for path, source, metadata, segment in sources:
        origin, _ = alignment.absolute_clock_origin(source.getroot())
        if origin is None:
            origin = alignment.timestamp(metadata.get("segment_started_at"))
        if origin is None:
            warnings.append(f"No reliable source clock: {path}")
            continue
        added = 0
        for message in alignment.comments_from_root(source.getroot(), "douyin"):
            mapped = alignment.map_wall_time(origin + message.time, clock, preferred_xml=segment["xml_file"])
            if mapped is None:
                continue
            key = (round(mapped[0], 3), message.user, message.text)
            if key in seen:
                continue
            seen.add(key)
            raw.append(Message(mapped[0], message.user, message.text))
            added += 1
        source_rows.append({"xml": str(path), "mapped_messages": added})
    filtered, ignored = filter_uid_spam(raw)
    return filtered, {
        "mode": "absolute-time-xml", "source_xml": str(xml),
        "duration_basis": duration_basis, "sources": source_rows,
        "ignored_uid_or_spam": ignored, "warnings": warnings,
    }


def analyze(messages):
    messages, _ = filter_uid_spam(messages)
    nodes, rejected = [], []
    for category, (pattern, gap, min_duration, min_messages, min_users, pre_roll) in THEMES.items():
        matches = [m for m in messages if re.search(pattern, m.text)]
        if category in MODE_EXCLUSIONS:
            exclusion = MODE_EXCLUSIONS[category]
            unrelated = [m for m in matches if exclusion.search(m.text)]
            matches = [m for m in matches if not exclusion.search(m.text)]
            if unrelated:
                rejected.append({"category": category, "reason": "unrelated-title", "evidence": evidence(unrelated)})
        if category == "爽局" and matches:
            support = [
                message for message in messages
                if SHUANGJU_SUPPORT_PATTERN.search(message.text)
                and any(abs(message.time - anchor.time) <= SHUANGJU_SUPPORT_SECONDS for anchor in matches)
            ]
            matches = sorted(
                {id(message): message for message in matches + support}.values(),
                key=lambda message: message.time,
            )
        if category in MODE_CATEGORIES:
            noncurrent = [m for m in matches if MODE_NONCURRENT_CONTEXT.search(m.text)]
            matches = [m for m in matches if not MODE_NONCURRENT_CONTEXT.search(m.text)]
            if noncurrent:
                rejected.append({
                    "category": category,
                    "reason": "non-current-request-prediction-or-history",
                    "evidence": evidence(noncurrent),
                })
            if category == "猎人模式" and matches:
                role_support = [
                    message for message in messages
                    if HUNTER_STANDALONE_ROLE_PATTERN.search(message.text)
                    and not MODE_NONCURRENT_CONTEXT.search(message.text)
                    and any(abs(message.time - seed.time) <= HUNTER_ROLE_SUPPORT_SECONDS for seed in matches)
                ]
                matches = sorted({id(message): message for message in matches + role_support}.values(), key=lambda m: m.time)
            if category == "Boss英雄模式" and matches:
                current_seeds = [
                    message for message in matches
                    if BOSS_HERO_CURRENT_PATTERN.search(message.text) or BOSS_HERO_ROLE_PATTERN.search(message.text)
                ]
                hero_role_support = [
                    message for message in messages
                    if HERO_ROLE_PATTERN.search(message.text)
                    and not MODE_NONCURRENT_CONTEXT.search(message.text)
                    and any(abs(message.time - seed.time) <= BOSS_ROLE_SUPPORT_SECONDS for seed in current_seeds)
                ]
                mode_support = [
                    message for message in messages
                    if SPECIAL_MODE_CONTEXT_PATTERN.search(message.text)
                    and not MODE_NONCURRENT_CONTEXT.search(message.text)
                    and any(abs(message.time - seed.time) <= BOSS_ROLE_SUPPORT_SECONDS for seed in current_seeds)
                ]
                matches = sorted(
                    {id(message): message for message in matches + hero_role_support + mode_support}.values(),
                    key=lambda m: m.time,
                )
            islands = clusters(matches, MODE_NEIGHBOR_SECONDS)
            strong_indexes = {
                index for index, island in enumerate(islands)
                if len({item.user for item in island}) >= MODE_ISLAND_MIN_USERS
            }
            bridge_indexes = set()
            if category == "猎人模式":
                for index, island in enumerate(islands):
                    if index in strong_indexes or len({item.user for item in island}) < MODE_BRIDGE_MIN_USERS:
                        continue
                    if any(island_distance(island, islands[strong]) <= gap for strong in strong_indexes):
                        bridge_indexes.add(index)
            included_indexes = strong_indexes | bridge_indexes
            supported = [message for index, island in enumerate(islands)
                         if index in included_indexes for message in island]
            bridge_messages = {
                id(message) for index, island in enumerate(islands)
                if index in bridge_indexes for message in island
            }
            isolated = [message for index, island in enumerate(islands)
                        if index not in included_indexes for message in island]
            if isolated:
                rejected.append({"category": category, "reason": "temporally-isolated-small-cluster",
                                 "evidence": evidence(isolated)})
            matches = supported
        else:
            bridge_messages = set()
        for group in clusters(matches, gap):
            start, end = group[0].time, group[-1].time
            reason = theme_rejection_reason(group, min_duration, min_messages, min_users)
            mechanics_users = {
                message.user for message in group
                if HUNTER_MECHANICS.search(message.text) or HUNTER_ROLE_PATTERN.search(message.text)
            }
            short_hunter_mechanics = (
                category == "猎人模式"
                and end - start >= HUNTER_SHORT_MIN_DURATION
                and len(group) >= HUNTER_SHORT_MIN_MESSAGES
                and len({message.user for message in group}) >= HUNTER_SHORT_MIN_USERS
                and len(mechanics_users) >= HUNTER_SHORT_MECHANICS_USERS
            )
            boss_current_users = {
                audience_user(message) for message in group
                if BOSS_HERO_CURRENT_PATTERN.search(message.text) or BOSS_HERO_ROLE_PATTERN.search(message.text)
            }
            short_boss_role_event = (
                category == "Boss英雄模式"
                and end - start >= 5
                and len(group) >= 3
                and len({audience_user(message) for message in group}) >= 3
                and len(boss_current_users) >= 2
            )
            if reason and not short_hunter_mechanics and not short_boss_role_event:
                rejected.append({"category": category, "start": start, "end": end, "reason": reason, "evidence": evidence(group)})
                continue
            base_group = [message for message in group if id(message) not in bridge_messages]
            base_reason = theme_rejection_reason(base_group, min_duration, min_messages, min_users)
            trigger = base_group[0].time if base_group and base_reason is None else start
            selected_pre_roll = HUNTER_SHORT_PRE_ROLL if short_hunter_mechanics and reason else pre_roll
            program_time = max(0.0, trigger - selected_pre_roll)
            node_reason = (
                "short-mechanics-theme"
                if short_hunter_mechanics and reason
                else "short-current-role-event" if short_boss_role_event and reason
                else "sustained-theme" if min_duration else "multiuser-theme"
            )
            if category == "爽局":
                seen = set()
                for message in group:
                    seen.add(message.user)
                    if len(seen) >= min_users:
                        trigger = message.time
                        program_time = max(0.0, group[0].time - pre_roll)
                        break
            nodes.append({
                "category": category, "time": program_time,
                "trigger_time": trigger, "event_end": end,
                "display_only": True, "reason": node_reason,
                "evidence": evidence(group),
            })

    # Douyin can corroborate a Bilibili laughter seed, never create one.
    laugh = [m for m in messages if m.user.startswith("bilibili:") and LAUGHTER_SIGNAL_PATTERN.search(m.text)]
    seeds = []
    for index, first in enumerate(laugh):
        window = [m for m in laugh[index:] if m.time <= first.time + 20]
        if len(window) >= 6 and len({m.user for m in window}) >= 4:
            seeds.append(first)
    for group in clusters(seeds, 30):
        start, last = group[0].time, group[-1].time
        response = [m for m in messages if start <= m.time <= last + 30 and (
            LAUGHTER_SIGNAL_PATTERN.search(m.text) if m.user.startswith("bilibili:") else REACTION.search(m.text)
        )]
        nodes.append({
            "category": "下饭坤", "time": max(0.0, start - 40),
            "trigger_time": start, "event_end": max((m.time for m in response), default=last),
            "display_only": True, "reason": "bilibili-laughter-seed",
            "evidence": evidence(response),
        })

    return {"nodes": sorted(nodes, key=lambda n: n["trigger_time"]), "rejected": rejected}
