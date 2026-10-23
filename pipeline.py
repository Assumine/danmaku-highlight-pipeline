"""Executable local analysis -> montage -> dual-version pipeline, no accounts."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import highlight_editor as editor
from dual_platform_program import build_program
from media_integrity import validate_media_file
from montage_danmaku import map_comments, write_ass
from pipeline_runtime import digest, job_lock, read_json, run, save_json


def execute(video, xml, workspace, *, douyin=None, room_url="", speech="off", preview_height=0):
    video, xml, workspace = Path(video).resolve(), Path(xml).resolve(), Path(workspace).resolve()
    # Content and algorithm identities prevent reuse after input/config/code changes.
    identity = {"video": digest(video), "xml": digest(xml), "speech": speech,
                "preview_height": preview_height, "room_url": room_url,
                "code": {p.name: digest(p) for p in sorted(Path(__file__).parent.glob("*.py"))},
                "douyin": {str(p.resolve()): digest(p) for p in sorted(Path(douyin).glob("*.xml"))} if douyin else {}}
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:20]
    directory = workspace / "records" / key
    directory.mkdir(parents=True, exist_ok=True)
    receipt = directory / "job.json"
    with job_lock(directory / "worker.lock"):
        job = read_json(receipt) if receipt.exists() else {"schema": 1, "job_id": key, "identity": identity, "attempts": []}
        if job.get("status") == "completed":
            for output in job["outputs"]:
                validate_media_file(Path(output), job["duration"])
                if digest(output) != job["output_hashes"][output]:
                    raise RuntimeError("Completed output changed; refusing silent reuse")
            return receipt
        job["status"] = "processing"
        save_json(receipt, job)
        try:
            source_report = validate_media_file(video)
            analysis = directory / "analysis"
            analysis.mkdir(exist_ok=True)
            timeline, merged_xml, alignment = build_program(xml, Path(douyin) if douyin else directory / "no-douyin",
                                                           analysis, source_report["duration"], room_url)
            job["alignment_errors"] = alignment["source_errors"]
            if not read_json(timeline):
                job.update(status="completed-no-highlights", outputs=[])
                save_json(receipt, job)
                return receipt
            ass = directory / Path(merged_xml).with_suffix(".ass").name
            write_ass(ET.parse(merged_xml).getroot(), ass)
            manifest_path = None
            for attempt in reversed(job["attempts"]):
                candidate = Path(attempt["manifest"])
                if candidate.exists() and read_json(candidate).get("automation_audit", {}).get("render_completed"):
                    manifest_path = candidate
                    break
            if manifest_path is None:
                name = f"highlight-{key}-{len(job['attempts']) + 1}"
                manifest_path = directory / name / "manifest.json"
                log = directory / f"{name}.log"
                job["attempts"].append({"manifest": str(manifest_path), "log": str(log)})
                save_json(receipt, job)
                command = [sys.executable, "-u", "-X", "faulthandler", str(Path(editor.__file__).resolve()),
                           "--video", video, "--ass", ass, "--timeline-json", timeline,
                           "--records-root", directory, "--media-root", workspace / "outputs" / key,
                           "--job-name", name, "--speech", speech, "--preview-height", str(preview_height), "--render"]
                result = run(command, log)
                if result.returncode:
                    raise RuntimeError(f"Editor exited {result.returncode}; see local log {log.name}")
            manifest = read_json(manifest_path)
            raw = Path(manifest["media_output"])
            raw_report = validate_media_file(raw)
            comments = map_comments(ET.parse(merged_xml).getroot(), manifest["source_reference_overlays"])
            clock = directory / "montage.xml"
            ET.ElementTree(comments).write(clock, encoding="utf-8", xml_declaration=True)
            burned_ass = directory / "montage.ass"
            job["danmaku"] = write_ass(comments, burned_ass)
            burned = raw.with_name(raw.stem + "-danmaku.mp4")
            if not job.get("burned_sha256") or not burned.exists() or digest(burned) != job["burned_sha256"]:
                temporary = burned.with_name(burned.stem + ".building.mp4")
                escaped = burned_ass.as_posix().replace(":", "\\:").replace("'", "'\\''")
                result = run([editor.FFMPEG, "-y", "-i", raw, "-vf", f"ass=filename='{escaped}'",
                              "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "copy",
                              "-movflags", "+faststart", temporary], directory / "danmaku-render.log")
                if result.returncode:
                    raise RuntimeError("Danmaku encode failed; see local danmaku-render.log")
                validate_media_file(temporary, raw_report["duration"], use_cache=False)
                temporary.replace(burned)
            validate_media_file(burned, raw_report["duration"])
            if digest(video) != identity["video"] or digest(xml) != identity["xml"]:
                raise RuntimeError("Source changed during processing; outputs not approved for upload")
            job.update(status="completed", duration=raw_report["duration"], manifest=str(manifest_path),
                       outputs=[str(raw), str(burned)], burned_sha256=digest(burned),
                       output_hashes={str(p): digest(p) for p in (raw, burned)})
            save_json(receipt, job)
        except Exception as exc:
            job.update(status="failed", error=str(exc))
            save_json(receipt, job)
            raise
    return receipt


def make_demo(directory):
    """Generate all sample material locally; no real audience or video data."""
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    video, xml = directory / "synthetic.mp4", directory / "synthetic.xml"
    if video.exists() or xml.exists():
        raise FileExistsError("Demo files exist; choose a fresh directory")
    result = run([editor.FFMPEG, "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=120",
                  "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=120",
                  "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", video], directory / "generate.log")
    if result.returncode:
        raise RuntimeError("Demo generation failed; FFmpeg needs lavfi, libx264 and AAC")
    root = ET.Element("i")
    for i in range(12):
        ET.SubElement(root, "d", p=f"{60 + i * .4},1,25,16777215,1700000000,0,synth{i:03d},0").text = "哈哈哈哈 synthetic reaction"
    ET.ElementTree(root).write(xml, encoding="utf-8", xml_declaration=True)
    return video, xml


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", type=Path)
    parser.add_argument("--xml", type=Path)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--douyin", type=Path)
    parser.add_argument("--room-url", default="")
    parser.add_argument("--speech", choices=("off", "whisper"), default="off")
    parser.add_argument("--preview-height", type=int, default=0)
    args = parser.parse_args(argv)
    if args.demo:
        args.video, args.xml = make_demo(args.workspace / "synthetic-input")
    if not args.video or not args.xml:
        parser.error("provide --video and --xml, or --demo")
    print(execute(args.video, args.xml, args.workspace, douyin=args.douyin, room_url=args.room_url,
                  speech=args.speech, preview_height=args.preview_height))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
