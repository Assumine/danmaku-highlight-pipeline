"""Map source comments through recorded montage transitions and render ASS."""
from __future__ import annotations

import copy
import math
import xml.etree.ElementTree as ET
from pathlib import Path


def map_comments(root, overlays):
    result = ET.Element("i")
    for overlay in overlays:
        start = float(overlay["source_time"])
        out_start, out_end = float(overlay["output_start"]), float(overlay["output_end"])
        if not all(math.isfinite(v) for v in (start, out_start, out_end)) or start < 0 or out_end <= out_start:
            raise ValueError("Invalid montage clock interval")
        for node in root.findall("d"):
            fields = node.get("p", "").split(",")
            try:
                time = float(fields[0])
            except (ValueError, IndexError):
                continue
            if start <= time < start + out_end - out_start:
                mapped = copy.deepcopy(node)
                fields[0] = f"{out_start + time - start:.3f}"
                mapped.set("p", ",".join(fields))
                result.append(mapped)
    result[:] = sorted(result, key=lambda node: float(node.get("p").split(",")[0]))
    return result


def ass_time(seconds):
    centiseconds = max(0, round(seconds * 100))
    minutes, centiseconds = divmod(centiseconds, 6000)
    hours, minutes = divmod(minutes, 60)
    seconds, fraction = divmod(centiseconds, 100)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{fraction:02d}"


def write_ass(root, path):
    """Five scrolling lanes in the top quarter; suppress overflow, not overlap."""
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 2
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,38,&H00FFFFFF,&H00FFFFFF,&H00101010,&H80000000,0,0,0,0,100,100,0,0,1,2,0,7,0,0,0,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lanes = [0.0] * 5
    lines = []
    dropped = 0
    for node in sorted(root.findall("d"), key=lambda n: float(n.get("p", "0").split(",")[0])):
        start = float(node.get("p").split(",")[0])
        if not math.isfinite(start) or start < 0:
            continue
        text = "".join(node.itertext()).strip()
        if not text:
            continue
        lane = min(range(len(lanes)), key=lanes.__getitem__)
        if lanes[lane] > start:
            dropped += 1
            continue
        # A lane stays occupied until the whole comment leaves the screen.
        lanes[lane] = start + 6
        text = text[:120].replace("\\", "＼").replace("{", "｛").replace("}", "｝").replace("\n", " ").replace("\r", " ")
        width = max(80, len(text) * 40)
        movement = "{\\move(1920,%d,%d,%d)}" % (12 + lane * 48, -width, 12 + lane * 48)
        lines.append(f"Dialogue: 0,{ass_time(start)},{ass_time(start + 6)},Default,,0,0,0,,{movement}{text}\n")
    Path(path).write_text(header + "".join(lines), encoding="utf-8-sig")
    return {"rendered": len(lines), "overflow_dropped": dropped}
