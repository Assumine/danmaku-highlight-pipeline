"""Read-only console audit of program points and clip boundaries."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path

DEFAULT_ROOT = Path.cwd()
REASONS = {
    "dual-near-end": "可信对齐，两边稳定区间取较早结尾",
    "dual-confirmed-bilibili-boundary": "双平台共同确认，使用B站事件边界",
    "absolute-time-bilibili": "绝对时间对齐，B站独立事件保留原边界",
    "absolute-time-douyin": "绝对时间对齐，抖音独立事件保留原边界",
    "creation-time-fused": "对齐不可信，按创建时间合并后分析",
    "bilibili-reaction-wave": "无双平台覆盖，使用 B 站反应波",
    "watchable-shuangju": "多人爽局共识，提前 180 秒并保留正常收尾",
    "no-crowd-laughter-cluster": "没有有效的多人笑词/笑表情反应簇",
    "no-overlapping-douyin-interval": "此阶段未找到重叠的抖音高能区间",
    "fixed-40s-pre-roll": "固定前置 40 秒",
    "speech-turn-containing-40s-anchor": "向前找到覆盖 40 秒锚点的完整语句开头",
    "speech-sentence-boundary": "保留当前完整语句",
    "absolute-timestamp-video-coverage": "绝对时间匹配录像覆盖",
    "absolute-time-aligned": "绝对时间对齐",
    "outside-video-coverage": "不在录像覆盖内",
    "bilibili_segment_finished": "B站该分段录制结束",
    "bilibili_segment_changed": "B站切换下一分段",
    "none": "未调整",
}


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def reason(value):
    if not value:
        return "无记录"
    return " + ".join(REASONS.get(part, part) for part in str(value).split("+"))


def clock(value):
    if value is None:
        return "—"
    seconds = max(0, round(float(value), 2))
    return f"{int(seconds // 3600):02}:{int(seconds // 60) % 60:02}:{seconds % 60:05.2f}"


def xml_stats(path):
    nodes = ET.parse(path).getroot().findall("d")
    times = []
    for node in nodes:
        try:
            value = float(node.get("p", "").split(",")[0])
            if math.isfinite(value) and value >= 0:
                times.append(value)
        except ValueError:
            pass
    return {"comments": len(nodes), "valid_times": len(times), "last_seconds": max(times, default=0)}


def choose_record(records, stem, warnings):
    candidates = []
    for folder in records.glob(stem + "_下饭片段*"):
        path = folder / "manifest.json"
        if not path.is_file():
            continue
        try:
            data = read_json(path)
            audit = data.get("automation_audit", {})
            if Path(data.get("ass", "")).stem != stem:
                continue
            if audit.get("quality_profile") != "production" or audit.get("decision_mode") != "automatic-only" or audit.get("manual_overrides"):
                continue
            timeline = Path(data.get("clip_timeline", {}).get("source_timeline_json") or "")
            if timeline.is_file() and audit.get("timeline_sha256") != digest(timeline):
                warnings.append(f"跳过时间线校验不一致的记录: {path}")
                continue
            candidates.append((audit.get("render_completed") is True, path.stat().st_mtime_ns, path, data))
        except (OSError, ValueError, TypeError, AttributeError) as error:
            warnings.append(f"记录读取失败: {path}: {error}")
    if not candidates:
        return None, {}
    _, _, path, data = max(candidates, key=lambda item: item[:2])
    return path, data


def load_analysis(records, stem, manifest, warnings):
    explicit = manifest.get("clip_timeline", {}).get("source_timeline_json")
    if explicit:
        timeline = Path(explicit)
        if not timeline.is_file():
            warnings.append(f"剪辑引用时间线已不存在: {timeline}，不以其他批次替代")
            return None, [], {}
    else:
        candidates = list((records / "节目单自动生成").glob(f"*/{stem}/{stem}.json"))
        candidates += list((records / "完整回放弹幕" / stem).glob(stem + ".json"))
        if not candidates:
            return None, [], {}
        timeline = max(candidates, key=lambda path: path.stat().st_mtime_ns)
    try:
        rows = read_json(timeline)
        if not isinstance(rows, list):
            raise ValueError("时间线不是数组")
        report_path = timeline.parent / "alignment_report.json"
        report = read_json(report_path) if report_path.is_file() else {}
        return timeline, rows, report
    except (OSError, ValueError) as error:
        warnings.append(f"分析读取失败: {timeline}: {error}")
        return timeline, [], {}


def platform_status(xml, report, warnings):
    stats = xml_stats(xml)
    sources = report.get("sources", [])
    details = []
    for source in sources:
        path = Path(source["xml"])
        meta_path = path.parent / "时间轴" / (path.stem + ".timeline.json")
        metadata = {}
        if meta_path.is_file():
            try:
                metadata = read_json(meta_path)
            except (OSError, ValueError) as error:
                warnings.append(f"录制状态读取失败: {meta_path}: {error}")
        diagnostics = metadata.get("protocol_diagnostics", {})
        alignment = source.get("alignment", {})
        details.append({
            "文件": str(path), "弹幕原数": source.get("raw_comments"),
            "合入数量": source.get("added_comments"), "状态": reason(source.get("mode")),
            "依据": reason(alignment.get("reason")),
            "绝对映射偏移秒": alignment.get("absolute_offset_seconds"),
            "来源时钟依据": alignment.get("source_clock_basis"),
            "绝对时间样本数": alignment.get("source_timestamp_samples"),
            "录像时钟依据": alignment.get("preferred_video_clock_basis"),
            "覆盖区间": source.get("video_coverage"),
            "无视频丢弃数": source.get("outside_video_coverage"),
            "分析时刷屏过滤数": source.get("ignored_spam"),
            "分析时缺失UID数": source.get("ignored_missing_uid"),
            "WebSocket曾连接": metadata.get("websocket_connected", "无记录"),
            "录制结束原因": metadata.get("close_reason", "无记录"),
            "协议累计统计": diagnostics,
            "状态证据": str(meta_path) if meta_path.is_file() else "元数据缺失",
        })
    modes = Counter(source.get("mode") for source in sources)
    summary = (
        f"B站 {stats['comments']} 条；抖音 {sum(s.get('added_comments', 0) for s in sources)} 条合入 / {len(sources)} 个来源；"
        f"绝对时间对齐 {modes['absolute-time-aligned']}，录像覆盖外 {modes['outside-video-coverage']}"
    )
    if not report:
        summary += "；无双平台分析记录，不能据此认定没有录制抖音"
    return summary, {"bilibili": stats, "douyin": details, "source_errors": report.get("source_errors", [])}


def clip_rows(candidates, manifest, warnings):
    groups = manifest.get("clip_timeline", {}).get("groups", [])
    highlights = {h["index"]: h for h in manifest.get("highlights", [])}
    selected = set(manifest.get("automation_audit", {}).get("selected_indexes", []))
    completed = manifest.get("automation_audit", {}).get("render_completed") is True
    overlays = {o["clip_index"]: o for o in manifest.get("source_reference_overlays", [])}
    transitions = {t["incoming_clip_index"]: t for t in manifest.get("transition_plan", {}).get("transitions", [])}
    cumulative, totals = 0.0, {}
    for index, h in sorted(highlights.items()):
        if index in selected:
            cumulative += max(0.0, h["final_end"] - h["start"])
            totals[index] = cumulative
    rows = []
    for number, candidate in enumerate(candidates, 1):
        trigger = candidate["trigger_time_seconds"]
        matches = [g for g in groups if any(abs(m["trigger_time"] - trigger) < .001 for m in g.get("members", []))]
        group = matches[0] if len(matches) == 1 else {}
        h = highlights.get(group.get("highlight_index"), {})
        index = h.get("index")
        status = "候选生效，未剪辑"
        explanation = {
            "dual-near-end": "两边高能区间重叠且存在群体笑声簇",
            "dual-confirmed-bilibili-boundary": "双平台事件重叠确认；边界采用B站事件",
            "absolute-time-bilibili": "B站事件命中；未与抖音重叠，保留原起止边界",
            "absolute-time-douyin": "抖音事件命中；未与B站重叠，保留原起止边界",
            "creation-time-fused": "群体笑词/笑表情反应簇",
            "bilibili-reaction-wave": "B站反应规则命中",
            "watchable-shuangju": "至少 4 位观众形成爽局共识；片头提前 180 秒，片尾按主题事件正常收束",
        }.get(candidate.get("end_policy"), "候选规则命中")
        if manifest:
            if len(matches) != 1:
                status = "待核对"
                explanation += "；节点与剪辑组无法唯一匹配"
            elif group.get("selection_status") == "excluded":
                status = "失效"
                explanation = reason(group.get("exclusion_reason"))
            elif index in selected:
                status = "生效/已成片" if completed else "生效/未完成压制"
            else:
                status = "未入本次成片"
        initial, start = h.get("initial_start"), h.get("start")
        coarse, end = h.get("coarse_end"), h.get("final_end")
        platform_evidence = candidate.get("platform_evidence") or {}
        bili_interval = platform_evidence.get("bilibili_interval")
        douyin_interval = platform_evidence.get("douyin_interval")
        platform_head_shift = None
        platform_tail_shift = None
        if (
            isinstance(bili_interval, list) and len(bili_interval) == 2
            and isinstance(douyin_interval, list) and len(douyin_interval) == 2
        ):
            platform_head_shift = round(
                float(bili_interval[0]) - min(float(bili_interval[0]), float(douyin_interval[0])),
                3,
            )
            platform_tail_shift = round(
                min(float(bili_interval[1]), float(douyin_interval[1])) - float(bili_interval[1]),
                3,
            )
        rows.append({
            "序号": number, "节目时间": clock(candidate.get("program_time_seconds")),
            "触发时间": clock(trigger), "状态": status, "生效失效原因": explanation,
            "组": group.get("group_id", candidate.get("clip_group")), "片段": index,
            "组成员数": group.get("member_count"),
            "组末触发": clock(h.get("last_program_trigger")),
            "连续组说明": "连续节点组成同一片段，结尾基于组末事件再精修" if len(h.get("program_points", [])) > 1 else "单节点，无连续组延长",
            "弹幕事件结束": clock(candidate.get("event_end_seconds")),
            "粗剪开头": clock(initial), "最终开头": clock(start),
            "开头前移秒": round(initial - start, 3) if start is not None else None,
            "开头原因": reason(h.get("start_basis")),
            "平台开头调整秒": platform_head_shift,
            "平台结尾调整秒": platform_tail_shift,
            "粗剪结尾": clock(coarse), "最终结尾": clock(end),
            "结尾调整秒": round(end - coarse, 3) if end is not None else None,
            "结尾原因": reason(h.get("end_basis")),
            "声谷处理": h.get("tail_adjustment_kind", "无记录"),
            "声谷位移秒": h.get("tail_adjustment_shift"),
            "组片长秒": round(end - start, 3) if end is not None else None,
            "组累计秒不重复": round(totals[index], 3) if index in totals else None,
            "成片开始": clock(overlays.get(index, {}).get("output_start")),
            "成片累计含转场": clock(overlays.get(index, {}).get("output_end")),
            "转场": transitions.get(index, {}).get("kind", "首段或无记录"),
            "转场重叠秒": transitions.get(index, {}).get("video_duration"),
            "笑声用户数": candidate.get("laughter_users"),
            "复读用户数": candidate.get("echo_users"),
            "复读文本": candidate.get("echo_text"), "分数": candidate.get("score"),
            "开头识别文本": h.get("start_speech_text", ""),
            "结尾识别文本": h.get("speech_text", ""),
            "原始证据": candidate.get("platform_evidence", candidate.get("evidence")),
        })
    return rows


def rejection_rows(report, candidates):
    rows = []
    coverage = [
        interval
        for source in report.get("sources", [])
        for interval in source.get("video_coverage", [])
        if source.get("mode") == "absolute-time-aligned"
    ]
    for item in report.get("rejected", []):
        interval = item.get("bilibili_interval") or item.get("fused_interval")
        if not interval:
            continue
        begin, end = interval
        retained = [n for n, c in enumerate(candidates, 1) if max(begin, c["trigger_time_seconds"]) < min(end, c["event_end_seconds"])]
        in_trusted = any(len(c) == 2 and max(begin, c[0]) < min(end, c[1]) for c in coverage)
        state = "阶段未通过"
        if retained:
            state = "后续有重叠生效候选，不能记为最终剔除"
        elif "bilibili_interval" in item and not in_trusted:
            state = "非可信覆盖区的中间记录，不单独作为剔除依据"
        rows.append({"峰开始": clock(begin), "峰结束": clock(end), "状态": state,
                     "原因": reason(item.get("reason")), "重叠生效候选": retained, "原始证据": item})
    return rows


def original_rows(ass, extractor):
    if not ass.is_file():
        return [], "原始 ASS 缺失，未额外生成"
    analysis = extractor.analyze_source_program(str(ass))
    display = extractor.source_program_points(analysis)
    result = {category: {"P1": points.copy()} for category, points in display.items()}
    with redirect_stdout(io.StringIO()):
        extractor.refine_program_list_starts(result, [str(ass)])
    rows = []
    for node in analysis["nodes"]:
        category, time = node["category"], node["time"]
        active = time in display.get(category, [])
        refined = None
        if active:
            refined = result[category]["P1"][display[category].index(time)]
        explanation = {
            "sustained-theme": "持续主题关键词及多用户覆盖",
            "multiuser-theme": "多人主题关键词反馈",
            "bilibili-laughter-seed": "B站笑词/笑表情触发，两平台反应辅助",
        }.get(node.get("reason"), "ASS关键词窗口命中")
        rows.append({
            "分类": category, "节目时间": clock(time),
            "触发时间": clock(node["trigger_time"]),
            "状态": "生效" if active else "展示合并/未单列",
            "原因": explanation if active else "同类别90秒展示去重",
            "阈值调整": 0, "命中关键词": node.get("rule_keywords"),
            "展示开头": clock(refined),
            "缓存开头调整秒": round(time - refined, 3) if refined is not None else None,
            "平台证据": node.get("evidence", {}),
        })
    note = "双平台XML原节目单复算；B站触发笑点，主题接受两边证据；不代表剪辑入选"
    if analysis["sources"]["mode"] == "ass-only":
        note = "仅ASS分析，缺少XML平台及UID证据"
    if analysis["sources"].get("warnings"):
        note += "；" + "；".join(analysis["sources"]["warnings"])
    return sorted(rows, key=lambda row: row["触发时间"]), note


def audit_source(xml, records, extractor):
    warnings = []
    manifest_path, manifest = choose_record(records, xml.stem, warnings)
    timeline, candidates, report = load_analysis(records, xml.stem, manifest, warnings)
    summary, platforms = platform_status(xml, report, warnings)
    original, original_note = original_rows(xml.with_suffix(".ass"), extractor)
    rows = clip_rows(candidates, manifest, warnings)
    if not manifest:
        warnings.append("没有正式剪辑记录：最终开头、结尾、累计时长均不可判定")
    return {
        "source": xml.stem, "platform_summary": summary, "platforms": platforms,
        "source_part": manifest.get("settings", {}).get("source_part"),
        "counts": {"双平台候选节点": len(candidates), "成片片段": len(manifest.get("automation_audit", {}).get("selected_indexes", [])),
                   "阶段未通过记录": len(report.get("rejected", [])), "原节目单原始命中": len(original),
                   "原节目单展示生效": sum(r["状态"] == "生效" for r in original)},
        "rows": rows, "stage_rejections": rejection_rows(report, candidates),
        "original_program": original, "original_note": original_note,
        "ending": manifest.get("montage_ending"), "warnings": warnings,
        "evidence": {"xml": str(xml), "manifest": str(manifest_path) if manifest_path else None,
                     "timeline": str(timeline) if timeline else None, "audit": manifest.get("automation_audit")},
    }


def display(value):
    if value is None:
        return "—"
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, float):
        return str(round(value, 3))
    return str(value)


def enable_color():
    if not sys.stdout.isatty():
        return False
    if os.name != "nt":
        return True
    # Enable ANSI on this output handle only; never change fonts or registry.
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetStdHandle.restype = wintypes.HANDLE
    kernel.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    handle = kernel.GetStdHandle(-11)
    mode = wintypes.DWORD()
    return bool(kernel.GetConsoleMode(handle, ctypes.byref(mode)) and kernel.SetConsoleMode(handle, mode.value | 4))


def print_report(sources, *, color=True, verbose=False):
    codes = {"green": "32", "red": "31", "yellow": "33", "cyan": "36", "bold": "1"}

    def paint(text, shade):
        return f"\x1b[{codes[shade]}m{text}\x1b[0m" if color else text

    def status(text):
        shade = "red" if text == "失效" else "green" if text.startswith("生效") else "yellow"
        return paint(text, shade)

    for source in sources:
        print("\n" + "=" * 100)
        print(paint(f"{source['source']} | 源P{source['source_part'] or '未知'}", "bold"))
        print(source["platform_summary"])
        counts = source["counts"]
        print("数量: " + " | ".join(f"{key}={value}" for key, value in counts.items()
                                   if verbose or key != "原节目单原始命中"))
        for warning in source["warnings"]:
            print(paint("注意: " + warning, "yellow"))
        if not verbose:
            seen = set()
            for row in source["rows"]:
                key = ("clip", row["片段"]) if row["片段"] is not None else ("node", row["序号"])
                if key in seen:
                    continue
                seen.add(key)
                start = row["最终开头"] if row["最终开头"] != "—" else row["节目时间"]
                head_shift = row["开头前移秒"]
                tail_shift = row["结尾调整秒"]
                head = f"{head_shift:+.1f}s" if head_shift else "0.0s" if head_shift is not None else "未知"
                tail = f"{tail_shift:+.1f}s" if tail_shift else "0.0s" if tail_shift is not None else "未知"
                why = []
                if row["状态"] == "失效":
                    why.append(row["生效失效原因"])
                else:
                    why.append(row["生效失效原因"])
                    if head_shift:
                        why.append("开头补全语句")
                    if tail_shift:
                        why.append("结尾声谷微调" if row["声谷位移秒"] else "结尾断句微调")
                    if row["连续组说明"].startswith("连续节点"):
                        why.append("连续节点合组")
                platform_head = (
                    f"{row['平台开头调整秒']:+.1f}s"
                    if row["平台开头调整秒"] is not None else "—"
                )
                platform_tail = (
                    f"{row['平台结尾调整秒']:+.1f}s"
                    if row["平台结尾调整秒"] is not None else "—"
                )
                duration = f"{row['组片长秒']:.1f}s" if row['组片长秒'] is not None else "未知"
                cumulative = row["成片累计含转场"]
                print(f"{len(seen):02} {start} | {status(row['状态'])}"
                      f" | 片长{duration} 累计{cumulative}"
                      f" | 开头延长{paint(platform_head, 'cyan')} 开头微调{paint(head, 'cyan')}"
                      f" | 结尾纠正{paint(platform_tail, 'cyan')} 结尾微调{paint(tail, 'cyan')}"
                      f" | {'；'.join(why)}")
            if not source["rows"]:
                for row in source["original_program"]:
                    if row["状态"] == "生效":
                        print(f"  {row['展示开头']} | {row['分类']} | 生效 | {row['原因']}（未剪辑）")
            rejected = Counter(r["原因"] for r in source["stage_rejections"]
                               if r["状态"] == "阶段未通过")
            if rejected:
                print("阶段筛除汇总（非最终片段数）: " + "；".join(f"{k} {v}处" for k, v in rejected.items()))
            ending = source.get("ending")
            if ending:
                print(f"整条片尾: 延长{ending.get('extension', 0):.1f}s，淡出{ending.get('video_fade_duration', 0):.1f}s")
            continue
        for item in source["platforms"]["douyin"]:
            print(f"  抖音 {Path(item['文件']).name} | {item['弹幕原数']}条 -> 合入{item['合入数量']}条"
                  f" | {paint(item['状态'], 'green' if item['状态']=='绝对时间对齐' else 'yellow')}"
                  f" | {item['依据']} | 绝对时间样本{item['绝对时间样本数']}"
                  f" | 映射偏移{display(item['绝对映射偏移秒'])}秒"
                  f" | 无视频丢弃{display(item['无视频丢弃数'])}条")
            errors = {k: v for k, v in item["协议累计统计"].items() if k.endswith("_errors") and v}
            print(f"    WS曾连接={item['WebSocket曾连接']} | 结束={reason(item['录制结束原因'])}"
                  f" | 协议错误={errors or '未记录非零错误'} | 分析刷屏过滤={item['分析时刷屏过滤数']}"
                  f" | 缺UID={item['分析时缺失UID数']}"
                  f" | 来源时钟={item['来源时钟依据']} | 录像时钟={item['录像时钟依据']}")
        for error in source["platforms"]["source_errors"]:
            print(paint("  异常来源: " + display(error), "red"))
        print("\n[剪辑候选与最终处理]")
        print("结尾调整=最终结尾-粗剪结尾；负数表示收紧。同一片段多节点只累计一次。")
        if not source["rows"]:
            print("无剪辑候选记录。")
        for row in source["rows"]:
            print(f"{row['序号']:03} | {row['节目时间']} | {status(row['状态'])}"
                  f" | 组{display(row['组'])}/片段{display(row['片段'])}"
                  f" | 触发{row['触发时间']} | {row['生效失效原因']}")
            print(f"    开头 {row['粗剪开头']} -> {row['最终开头']}"
                  f" | {paint('前移' + display(row['开头前移秒']) + '秒', 'cyan')} | {row['开头原因']}")
            print(f"    结尾 {row['粗剪结尾']} -> {row['最终结尾']}"
                  f" | {paint('调整' + display(row['结尾调整秒']) + '秒', 'cyan')}"
                  f" | {row['结尾原因']} | 声谷={reason(row['声谷处理'])} ({display(row['声谷位移秒'])}秒)")
            print(f"    片长={display(row['组片长秒'])}秒 | 组累计={display(row['组累计秒不重复'])}秒"
                  f" | 成片位置={row['成片开始']}~{row['成片累计含转场']}"
                  f" | 转场={row['转场']} | 笑声用户={display(row['笑声用户数'])}"
                  f" | 复读用户={display(row['复读用户数'])} | 复读={display(row['复读文本'])}")
            print(f"    合组: {row['连续组说明']} | 组末触发={row['组末触发']}")
            if row["开头识别文本"]:
                print("    开头语音: " + row["开头识别文本"])
            if row["结尾识别文本"]:
                print("    结尾语音: " + row["结尾识别文本"])
        print("\n[各阶段未通过的高能峰]")
        print("这些是阶段判断，不直接等于最终剔除。")
        for row in source["stage_rejections"]:
            print(f"  {row['峰开始']}~{row['峰结束']} | {status(row['状态'])}"
                  f" | {row['原因']} | 重叠生效候选={row['重叠生效候选']}")
        print("\n[program_extractor 原节目单全部命中]")
        print(source["original_note"])
        for row in source["original_program"]:
            print(f"  {row['节目时间']} | {row['分类']} | {status(row['状态'])}"
                  f" | {row['原因']} | 触发={row['触发时间']} | 展示开头={row['展示开头']}"
                  f" | 缓存调整={display(row['缓存开头调整秒'])}秒"
                  f" | count调整={row['阈值调整']} | 关键词={display(row['命中关键词'])}")
        print("\n整条片尾淡出: " + display(source["ending"]))
        print("依据清单: " + display(source["evidence"].get("manifest")))
        print("依据时间线: " + display(source["evidence"].get("timeline")))


def main():
    parser = argparse.ArgumentParser(description="只读审查节目单、双平台状态和剪辑切点")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--date", action="append", help="指定源文件日期 YYYYMMDD，可重复；覆盖最近24小时范围")
    parser.add_argument("--hours", type=float, default=24, help="最近多少小时内录制更新的场次，默认24")
    parser.add_argument("--all", action="store_true", help="审查所有历史场次")
    parser.add_argument("--source", help="源 XML 文件名包含的文字")
    parser.add_argument("--no-color", action="store_true", help="关闭 ANSI 颜色")
    parser.add_argument("--verbose", action="store_true", help="展开语音全文、平台诊断及全部规则命中")
    args = parser.parse_args()
    root = args.root.resolve()
    sys.path.insert(0, str(root))
    import program_extractor
    backup = root / "backup"
    if not math.isfinite(args.hours) or args.hours <= 0:
        parser.error("--hours 必须是正数")
    cutoff = datetime.now() - timedelta(hours=args.hours)
    sources = sorted(p for p in backup.glob("*.xml") if re.search(r"_\d{8}$", p.stem)
                     and (p.stem[-8:] in args.date if args.date else args.all or p.stat().st_mtime >= cutoff.timestamp())
                     and (not args.source or args.source in p.name))
    scope = ', '.join(args.date) if args.date else '全部历史' if args.all else f"{cutoff:%Y-%m-%d %H:%M:%S} 至今（按源XML最后录制/合并写入时间）"
    print(f"节目单审查范围: {scope} | {len(sources)} 场")
    results = []
    for xml in sources:
        print(f"审查: {xml.name}", flush=True)
        try:
            results.append(audit_source(xml, root / "records", program_extractor))
        except (OSError, ValueError, TypeError, KeyError, AttributeError, ET.ParseError) as error:
            results.append({"source": xml.stem, "source_part": None, "platform_summary": "分析失败，状态未知", "platforms": {"douyin": [], "source_errors": []}, "counts": {}, "rows": [], "stage_rejections": [], "original_program": [], "original_note": "未完成", "ending": None, "warnings": [str(error)], "evidence": {"xml": str(xml)}})
    if not sources:
        print("此范围未找到源 XML。")
        return
    print_report(results, color=not args.no_color and enable_color(), verbose=args.verbose)
    print(f"\n完成: {len(results)} 场。")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        if sys.stdin.isatty() and sys.stdout.isatty():
            try:
                input("\n审查结束，按回车键退出...")
            except (EOFError, KeyboardInterrupt):
                pass
