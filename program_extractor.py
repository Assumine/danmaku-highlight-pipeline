import hashlib
import json
import os
import re
from datetime import timedelta
from pathlib import Path

WORKSPACE_ROOT = Path(os.environ.get("DANMAKU_WORKSPACE_ROOT", Path.cwd())).resolve()

# ==========================
# 配置区（你可以自由修改）
# ==========================

RULES = {
    "下饭坤": [
        {
            "keyword": ["笑"],
            "count": 6,
            "window": 20,
            "need_kun": False
        },
        {
            "keyword": ["哈"],
            "count": 6,
            "window": 20,
            "need_kun": False
        },
        {
            "keyword": ["哈", "笑", "急了"],
            "count": 10,
            "window": 20,
            "need_kun": False
        },
        {
            "keyword": ["哦豁"],
            "count": 6,
            "window": 20,
            "need_kun": False
        },
        {
            "keyword": ["急了"],
            "count": 6,
            "window": 20,
            "need_kun": False
        }
    ],
    "自私坤": [
        {
            "keyword": ["自私"],
            "count": 4,
            "window": 30,
            "need_kun": False
        },
        {
            "keyword": ["卑鄙"],
            "count": 4,
            "window": 30,
            "need_kun": False
        },
        {
            "keyword": ["自私","卑鄙"],
            "count": 5,
            "window": 30,
            "need_kun": False
        },
        {
            "keyword": ["红温"],
            "count": 5,
            "window": 30,
            "need_kun": False
        }
    ],
    "理财坤": [
        {
            "keyword": ["理财"],
            "count": 5,
            "window": 30,
            "need_kun": True
        },
        {
            "keyword": ["亏"],
            "count": 3,
            "window": 20,
            "need_kun": True
        },
        {
            "keyword": ["理财","亏"],
            "count": 5,
            "window": 30,
            "need_kun": True
        }
    ],
    "内鬼坤": [
        {
            "keyword": ["内鬼"],
            "count": 5,
            "window": 30,
            "need_kun": True
        },
        {
            "keyword": ["害人精"],
            "count": 4,
            "window": 30,
            "need_kun": True
        },
        {
            "keyword": ["内鬼","害人精"],
            "count": 4,
            "window": 30,
            "need_kun": True
        }
    ]
}

# These categories are published in the source program list but are never
# returned by find_clip_program_nodes(), so they cannot enter auto-editing.
DISPLAY_ONLY_CATEGORIES = ("猎人模式", "爽局", "Boss英雄模式")
PROGRAM_CATEGORIES = (*RULES, *DISPLAY_ONLY_CATEGORIES)
HUNTER_KEYWORD = "猎人"
HUNTER_LINK_GAP_SECONDS = 210
HUNTER_MIN_DURATION_SECONDS = 150
HUNTER_MIN_MESSAGES = 6
HUNTER_SECTION_COUNT = 2
HUNTER_MIN_MESSAGES_PER_SECTION = 2

KUN_KEYWORD = "坤"
PRE_ROLL = 40          # 节之前移秒数
MERGE_DISTANCE = 90   # 展示节目单中，同等级节目点 90 秒内合并
CLIP_GROUP_DISTANCE = 100  # 剪辑时间线中，100秒内的相邻原始触发归为连续组
REFINED_START_MATCH_TOLERANCE = 30.0
DEFAULT_HIGHLIGHT_RECORDS_ROOT = (
    WORKSPACE_ROOT / "records"
)


# ==========================
# 工具函数
# ==========================

def parse_time(t):
    """将 ASS 时间格式 转为秒"""
    h, m, s = t.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def format_time(sec):
    """格式化输出时间"""
    if sec < 0:
        sec = 0
    td = timedelta(seconds=sec)
    total = int(td.total_seconds())
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60

    if h == 0:
        return f"{m:02d}:{s:02d}"
    return f"{h:02d}:{m:02d}:{s:02d}"


def clean_text(text):
    """去除 ASS 控制符"""
    return re.sub(r"\{.*?\}", "", text)


# ==========================
# 主逻辑
# ==========================

def extract_times(ass_path):
    """解析 ASS 文件，返回所有弹幕时间点"""
    keyword_times = {k: [] for k in RULES}
    kun_times = []
    all_danmaku = []  # 存储所有弹幕时间点和文本，用于高能时刻检测和多关键词匹配

    try:
        # Relative paths are resolved below the configured workspace backup directory.
        if os.path.isabs(ass_path):
            full_ass_path = ass_path
        else:
            full_ass_path = os.path.join(WORKSPACE_ROOT, "backup", ass_path)

        with open(full_ass_path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.startswith("Dialogue"):
                    continue

                parts = line.split(",", 9)
                if len(parts) < 10:
                    continue

                start = parse_time(parts[1])
                text = clean_text(parts[9]).strip()

                # 新增：记录所有弹幕时间点和文本
                all_danmaku.append((start, text))

                # 匹配坤
                if KUN_KEYWORD in text:
                    kun_times.append(start)

                # 匹配各等级关键词
                for level, rules in RULES.items():
                    for rule in rules:
                        # 高能时刻不需要关键词匹配，跳过
                        if rule["keyword"] is None:
                            continue

                        # 处理多个关键词匹配
                        if isinstance(rule["keyword"], list):
                            # 匹配任意一个关键词
                            for keyword in rule["keyword"]:
                                if keyword in text:
                                    keyword_times[level].append(start)
                                    break
                        else:
                            # 匹配单个关键词
                            if rule["keyword"] in text:
                                keyword_times[level].append(start)
    except FileNotFoundError:
        # 文件不存在时，返回空的时间点
        print(f"警告: 找不到文件 {ass_path}")
        keyword_times = {k: [] for k in RULES}
        kun_times = []
        all_danmaku = []
    except Exception as e:
        # 其他错误时，返回空的时间点
        print(f"警告: 处理文件 {ass_path} 时发生错误: {e}")
        keyword_times = {k: [] for k in RULES}
        kun_times = []
        all_danmaku = []

    return keyword_times, kun_times, all_danmaku


def find_clip_program_nodes(keyword_times, kun_times, all_danmaku, count_adjust=0):
    """根据规则扫描窗口，返回剪辑专用的未合并节目节点。

    展示节目单会在 ``find_program_points`` 中继续执行原有的 90 秒合并；
    这里保留每个规则命中的原始触发时间，供剪辑器构建连续事件组。

    参数:
        keyword_times: 关键词时间点字典
        kun_times: 坤出现的时间点列表
        all_danmaku: 所有弹幕时间点和文本列表
        count_adjust: count调整值，默认为0
    """
    results = {}

    for level, rules in RULES.items():
        level_nodes = {}

        # 遍历该等级的所有规则
        for rule_index, rule in enumerate(rules, 1):
            # 处理规则
            if rule["keyword"] is None:
                # 高能时刻：检测5秒内有10条弹幕
                times = [t for t, _ in all_danmaku]
            else:
                # 收集当前规则下所有关键词的命中时间点
                times = []
                for t, text in all_danmaku:
                    matched = False
                    if isinstance(rule["keyword"], list):
                        # 匹配任意一个关键词
                        for keyword in rule["keyword"]:
                            if keyword in text:
                                matched = True
                                break
                    else:
                        # 匹配单个关键词
                        if rule["keyword"] in text:
                            matched = True
                    if matched:
                        times.append(t)

            if not times:
                continue

            window = rule["window"]
            need_kun = rule["need_kun"]
            count_need = max(1, rule["count"] + count_adjust)  # 确保count_need至少为1

            n = len(times)

            for i in range(n):
                start_t = times[i]
                # 找窗口内所有出现
                window_times = [t for t in times if start_t <= t <= start_t + window]

                if len(window_times) >= count_need:
                    # 若需要坤，则检查窗口内是否有坤
                    if need_kun:
                        if not any(start_t <= kt <= start_t + window for kt in kun_times):
                            continue

                    # 取第一个命中关键词
                    first_time = window_times[0]

                    # 往前推 40 秒
                    program_point = first_time - PRE_ROLL
                    node = level_nodes.setdefault(
                        first_time,
                        {
                            "time": program_point,
                            "trigger_time": first_time,
                            "category": level,
                            "rule_indexes": [],
                            "rule_keywords": [],
                        },
                    )
                    if rule_index not in node["rule_indexes"]:
                        node["rule_indexes"].append(rule_index)
                    rule_keywords = rule.get("keyword") or []
                    if not isinstance(rule_keywords, list):
                        rule_keywords = [rule_keywords]
                    for keyword in rule_keywords:
                        if keyword not in node["rule_keywords"]:
                            node["rule_keywords"].append(keyword)

        if level_nodes:
            results[level] = [level_nodes[key] for key in sorted(level_nodes)]

    return results


def find_display_only_program_nodes(all_danmaku):
    """生成只用于原节目单展示、不进入剪辑的长主题节点。"""
    hits = sorted(
        (float(time), text)
        for time, text in all_danmaku
        if HUNTER_KEYWORD in text
    )
    clusters = []
    for hit in hits:
        if not clusters or hit[0] - clusters[-1][-1][0] > HUNTER_LINK_GAP_SECONDS:
            clusters.append([hit])
        else:
            clusters[-1].append(hit)

    nodes = []
    for cluster in clusters:
        start, end = cluster[0][0], cluster[-1][0]
        duration = end - start
        if duration < HUNTER_MIN_DURATION_SECONDS or len(cluster) < HUNTER_MIN_MESSAGES:
            continue

        section_counts = [0] * HUNTER_SECTION_COUNT
        for time, _text in cluster:
            relative = (time - start) / duration
            section = min(
                HUNTER_SECTION_COUNT - 1,
                int(relative * HUNTER_SECTION_COUNT),
            )
            section_counts[section] += 1
        if min(section_counts) < HUNTER_MIN_MESSAGES_PER_SECTION:
            continue

        nodes.append({
            "time": max(0.0, start - PRE_ROLL),
            "trigger_time": start,
            "category": "猎人模式",
            "rule_indexes": [],
            "rule_keywords": [HUNTER_KEYWORD],
            "display_only": True,
            "duration_seconds": duration,
            "message_count": len(cluster),
            "section_message_counts": section_counts,
        })
    return {"猎人模式": nodes} if nodes else {}


def find_program_points(keyword_times, kun_times, all_danmaku, count_adjust=0):
    """根据规则扫描窗口，生成展示节目点。"""
    raw_results = find_clip_program_nodes(
        keyword_times,
        kun_times,
        all_danmaku,
        count_adjust=count_adjust,
    )
    raw_results.update(find_display_only_program_nodes(all_danmaku))
    results = {}

    for level, nodes in raw_results.items():
        level_pts = sorted(node["time"] for node in nodes)

        # 展示层去重（90 秒内合并）
        merged = []
        for p in level_pts:
            if not merged or p - merged[-1] > MERGE_DISTANCE:
                merged.append(p)

        if merged:
            results[level] = merged

    return results


def choose_levels(results):
    """按优先级选择等级

    规则：
    - 按RULES字典中的顺序，优先级从高到低
    - 高能时刻等级最低，如果触发了其他等级，则忽略高能时刻
    """
    ordered = list(PROGRAM_CATEGORIES)
    final = {}

    # 检查是否有其他等级被触发
    has_other_levels = any(level in results for level in ordered if level != "高能时刻")

    for level in ordered:
        if level in results:
            # 如果有其他等级被触发，且当前是高能时刻，则忽略
            if has_other_levels and level == "高能时刻":
                continue
            final[level] = results[level]

    return final


def _source_identity(path, fallback_date=None):
    """从视频、ASS 或记录目录名中提取场次和录制日期。"""
    name = Path(str(path)).stem
    match = re.search(r"(\d{1,2})时场(\d{2})(?:_(\d{8}))?", name)
    if not match:
        return None, None
    base_name = f"{int(match.group(1)):02d}时场{match.group(2)}"
    return base_name, match.group(3) or fallback_date


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _trusted_refined_starts(ass_file, records_root, recording_date=None):
    """读取同一场次正式剪辑留下的原始开头和语音精修开头。"""
    expected_base, expected_date = _source_identity(ass_file, recording_date)
    root = Path(records_root)
    if not expected_base or not root.is_dir():
        return []

    candidates = []
    for manifest_path in root.glob("*/manifest.json"):
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            audit = payload.get("automation_audit") or {}
            settings = payload.get("settings") or {}
            source_ass = Path(payload.get("ass") or "")
            source_base, source_date = _source_identity(source_ass)
            if source_base != expected_base:
                continue
            if expected_date and source_date != expected_date:
                continue
            if not expected_date and not source_date:
                continue
            if not (
                audit.get("decision_mode") == "automatic-only"
                and audit.get("manual_overrides") == []
                and audit.get("speech_mode") == "whisper"
                and audit.get("render_completed") is True
                and audit.get("quality_profile") == "production"
                and audit.get("output_resolution") == "source"
                and audit.get("output_fps") == "source"
                and settings.get("start_boundary_policy")
                == "speech-turn-containing-40s-anchor"
            ):
                continue
            expected_hash = audit.get("ass_sha256")
            if (
                not source_ass.is_file()
                or not expected_hash
                or _sha256_file(source_ass) != expected_hash
            ):
                continue

            selected_indexes = set(audit.get("selected_indexes") or [])
            anchors = []
            for highlight in payload.get("highlights") or []:
                if highlight.get("index") not in selected_indexes:
                    continue
                if highlight.get("start_basis") != "speech-turn-containing-40s-anchor":
                    continue
                initial_start = float(highlight["initial_start"])
                refined_start = float(highlight["start"])
                if not (0 <= refined_start < initial_start):
                    continue
                anchors.append((initial_start, refined_start))
            if anchors:
                candidates.append((manifest_path.stat().st_mtime_ns, anchors))
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            continue

    if not candidates:
        return []
    return max(candidates, key=lambda item: item[0])[1]


def refine_program_list_starts(
    program_list,
    ass_files,
    *,
    records_root=DEFAULT_HIGHLIGHT_RECORDS_ROOT,
    recording_date=None,
):
    """仅复用正式剪辑的语音开头，不改变节目点选择、分类或结尾。"""
    refined_count = 0
    for index, ass_file in enumerate(ass_files, 1):
        p_tag = f"P{index}"
        anchors = _trusted_refined_starts(
            ass_file,
            records_root,
            recording_date=recording_date,
        )
        if not anchors:
            continue

        for level, p_dict in program_list.items():
            if level in DISPLAY_ONLY_CATEGORIES:
                continue
            points = p_dict.get(p_tag)
            if not points:
                continue
            refined_points = []
            for point in points:
                initial_start, refined_start = min(
                    anchors,
                    key=lambda anchor: abs(anchor[0] - point),
                )
                if abs(initial_start - point) <= REFINED_START_MATCH_TOLERANCE:
                    refined_points.append(refined_start)
                    if abs(refined_start - point) > 0.001:
                        refined_count += 1
                else:
                    refined_points.append(point)
            p_dict[p_tag] = sorted(refined_points)

    if refined_count:
        print(f"复用正式剪辑缓存精修节目单开头: {refined_count} 个")
    return program_list


def analyze_source_program(ass_file):
    """Return source-program nodes and platform evidence on the video clock."""
    from program_source_analysis import load_messages, analyze

    messages, sources = load_messages(
        ass_file, root=Path(__file__).resolve().parent.parent,
    )
    if messages is not None:
        result = analyze(messages)
        result["sources"] = sources
        return result
    kw, kun, comments = extract_times(ass_file)
    raw = find_clip_program_nodes(kw, kun, comments)
    raw.update(find_display_only_program_nodes(comments))
    return {
        "nodes": [node for members in raw.values() for node in members],
        "rejected": [], "sources": sources,
    }


def source_program_points(analysis):
    results = {}
    for node in sorted(analysis["nodes"], key=lambda n: n["time"]):
        category = node["category"]
        points = results.setdefault(category, [])
        if not points or node["time"] - points[-1] > MERGE_DISTANCE:
            points.append(node["time"])
    return choose_levels(results)


def generate_program_list(
    ass_files,
    *,
    highlight_records_root=DEFAULT_HIGHLIGHT_RECORDS_ROOT,
    recording_date=None,
):
    """Generate the published source program independently of clip selection."""
    result = {category: {} for category in PROGRAM_CATEGORIES}
    for index, ass_file in enumerate(ass_files, 1):
        analysis = analyze_source_program(ass_file)
        for warning in analysis["sources"].get("warnings", []):
            print(f"节目单数据提示: {warning}")
        for category, points in source_program_points(analysis).items():
            result[category][f"P{index}"] = points
    result = {category: parts for category, parts in result.items() if parts}
    result = refine_program_list_starts(
        result, ass_files, records_root=highlight_records_root,
        recording_date=recording_date,
    )
    return {
        category + "(测试)" if category in ("猎人模式", "Boss英雄模式") else category: parts
        for category, parts in result.items()
    }


def main(argv=None):
    import argparse
    import shutil
    import subprocess
    from datetime import datetime

    parser = argparse.ArgumentParser(description="生成原始节目单")
    parser.add_argument("files", nargs="*", help="指定 XML 或 ASS；默认最近24小时录像")
    parser.add_argument("--no-clipboard", action="store_true")
    parser.add_argument("--no-pause", action="store_true")
    args = parser.parse_args(argv)
    backup = WORKSPACE_ROOT / "backup"
    files = [Path(name).resolve() for name in args.files]
    if not files:
        threshold = datetime.now().timestamp() - 86400
        files = [path for path in backup.glob("*.xml") if path.stat().st_ctime > threshold]
        def recording_order(path):
            match = re.search(r"(\d{1,2})时场(\d{2})_(\d{8})", path.stem)
            return (match[3], int(match[1]), int(match[2])) if match else (path.stem, 0, 0)
        files.sort(key=recording_order)
    if not files:
        print("未找到24小时内的XML文件，可指定XML或ASS路径运行。")
        return 0
    for index, path in enumerate(files, 1):
        if not path.is_file():
            raise FileNotFoundError(path)
        print(f"P{index}: {path.name}")
    # XML analysis does not require regenerating a display ASS file.
    program = generate_program_list([path.with_suffix(".ass") for path in files])
    lines = ["本期节目单：", ""]
    for category, parts in program.items():
        lines.append(category + ":")
        for part, points in parts.items():
            lines.append(part + ":")
            lines.extend(f"{index}. {format_time(time)}" for index, time in enumerate(points, 1))
        lines.append("")
    text = "\n".join(lines) if program else "本期节目单：无节目"
    print(text)
    if not args.no_clipboard:
        pwsh = shutil.which("pwsh")
        if pwsh:
            subprocess.run(
                [pwsh, "-NoProfile", "-Command", "$ErrorActionPreference='Stop'; [Console]::InputEncoding=[System.Text.UTF8Encoding]::new($false); [Console]::In.ReadToEnd() | Set-Clipboard"],
                input=text, text=True, encoding="utf-8", check=True,
            )
            print("节目单已复制到剪贴板。")
        else:
            print("未找到PowerShell 7，节目单已显示，未复制到剪贴板。")
    return 0


if __name__ == "__main__":
    import sys
    import time
    import traceback

    exit_code = 0
    try:
        exit_code = main()
    except Exception:
        traceback.print_exc()
        exit_code = 1
    finally:
        if sys.stdin.isatty() and "--no-pause" not in sys.argv:
            print("1秒后自动关闭窗口...", flush=True)
            time.sleep(1)
    sys.exit(exit_code)
