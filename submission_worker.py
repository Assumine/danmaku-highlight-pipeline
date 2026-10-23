"""Opt-in append worker: reconcile CIDs before retrying any interrupted upload."""
from __future__ import annotations

import argparse
import os
import re
import time
from pathlib import Path

from biliup_integration import BiliupCLI, parse_show_output
from media_integrity import validate_media_file
from pipeline_runtime import digest, job_lock, read_json, run, save_json

TITLES = ("[无弹幕版]下饭片段", "[有弹幕版]下饭片段")


def reconcile(entry, videos):
    matches = [v for v in videos if v.get("title") == entry["title"] and int(v["cid"]) not in entry["before_cids"]]
    if len(matches) > 1:
        raise RuntimeError("Multiple candidate CIDs; manual review required")
    if matches:
        entry.update(cid=int(matches[0]["cid"]), status="confirmed")
        return True
    return False


def submit(job_path, target, cli, *, wait_seconds=0):
    job_path = Path(job_path).resolve()
    if not re.fullmatch(r"BV[0-9A-Za-z]+", target):
        raise ValueError("Explicit BV target required")
    job = read_json(job_path)
    if job.get("status") != "completed" or len(job.get("outputs", [])) != 2:
        raise ValueError("A completed dual-version local job is required")
    receipt_path = job_path.with_name("submission.json")

    def videos():
        result = run(cli.show_submission(target))
        if result.returncode:
            raise RuntimeError("Submission query failed; no upload attempted")
        return parse_show_output(result.stdout).get("videos", [])

    with job_lock(job_path.parent.parent / f"submit-{target}.lock"):
        receipt = read_json(receipt_path) if receipt_path.exists() else {"target": target, "entries": {}}
        if receipt["target"] != target:
            raise RuntimeError("Receipt belongs to another BV; do not change targets during recovery")
        for path, title in zip(map(Path, job["outputs"]), TITLES):
            validate_media_file(path, job["duration"])
            if digest(path) != job["output_hashes"][str(path)]:
                raise RuntimeError("Output changed since processing completed")
            entry = receipt["entries"].get(title)
            current = videos()
            if entry:
                if entry.get("cid"):
                    if not any(int(v["cid"]) == entry["cid"] and v["title"] == title for v in current):
                        raise RuntimeError("Confirmed CID missing or renamed; duplicate blocked")
                elif not reconcile(entry, current):
                    raise RuntimeError("Interrupted upload outcome unknown; duplicate blocked")
            else:
                if any(v.get("title") == title for v in current):
                    raise RuntimeError("Part title already exists without this job's receipt")
                entry = {"title": title, "before_cids": [int(v["cid"]) for v in current], "status": "attempting"}
                receipt["entries"][title] = entry
                save_json(receipt_path, receipt)
                alias = job_path.parent / (title + ".mp4")
                if alias.exists():
                    if not os.path.samefile(alias, path):
                        raise RuntimeError("Upload alias collision")
                else:
                    os.link(path, alias)
                # Never persist raw CLI stdout/stderr: it may contain account data.
                result = run(cli.append(target, [alias]))
                entry["returncode"] = result.returncode
                save_json(receipt_path, receipt)
                for attempt in range(6):
                    if reconcile(entry, videos()):
                        break
                    if attempt < 5:
                        time.sleep(5)
                save_json(receipt_path, receipt)
                if not entry.get("cid"):
                    raise RuntimeError("Upload result not confirmed; retained locally for reconciliation")
            save_json(receipt_path, receipt)
            deadline = time.monotonic() + wait_seconds
            while True:
                part = next(v for v in videos() if int(v["cid"]) == entry["cid"])
                if part.get("status") == 0 and part.get("xcode_state") == 6:
                    entry["status"] = "ready"
                    break
                if time.monotonic() >= deadline:
                    break
                time.sleep(min(15, max(0, deadline - time.monotonic())))
            save_json(receipt_path, receipt)
        receipt["status"] = "ready" if all(e["status"] == "ready" for e in receipt["entries"].values()) else "uploaded-waiting-platform"
        save_json(receipt_path, receipt)
    return receipt_path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job", type=Path)
    parser.add_argument("--target", required=True)
    parser.add_argument("--biliup", type=Path, required=True)
    parser.add_argument("--cookies", type=Path, required=True)
    parser.add_argument("--wait-seconds", type=int, default=0)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    if not args.execute:
        print("Plan only: append two local variants to the explicitly selected BV; no network requests.")
        return 0
    if args.wait_seconds < 0:
        parser.error("--wait-seconds must be nonnegative")
    print(submit(args.job, args.target, BiliupCLI(args.biliup.resolve(), args.cookies.resolve()), wait_seconds=args.wait_seconds))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
