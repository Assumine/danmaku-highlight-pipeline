"""Pure integration helpers for biliup 1.2.6 and multi-part submissions.

Callers own authentication, network requests, retries, and durable job state.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Sequence


EDIT_URL = "https://member.bilibili.com/x/vu/web/edit"
RAW_PREFIX = "[无弹幕版] "
DANMAKU_PREFIX = "[有弹幕版] "
HIGHLIGHT_TITLE = "下饭片段"
ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


@dataclass(frozen=True)
class BiliupCLI:
    executable: Path
    cookie_file: Path
    submit_api: str = "web"

    def __post_init__(self) -> None:
        if self.submit_api not in {"app", "web", "b-cut-android"}:
            raise ValueError(f"Unsupported submit API: {self.submit_api}")

    def _base(self, action: str) -> list[str]:
        return [str(self.executable), "--user-cookie", str(self.cookie_file), action]

    def list_submissions(self) -> list[str]:
        return [*self._base("list"), "--from-page", "1", "--max-pages", "1"]

    def show_submission(self, bvid: str) -> list[str]:
        if not bvid:
            raise ValueError("bvid is required")
        return [*self._base("show"), bvid]

    def upload(
        self,
        *,
        title: str,
        description: str,
        tags: str,
        category_id: int,
        video_paths: Iterable[str | Path],
    ) -> list[str]:
        paths = [str(path) for path in video_paths]
        if not title or not tags or not paths:
            raise ValueError("title, tags, and video_paths are required")
        return [
            *self._base("upload"),
            "--submit", self.submit_api,
            "--copyright", "1",
            "--tid", str(category_id),
            "--title", title,
            "--desc", description,
            "--tag", tags,
            "--hires", "1",
            "--no-reprint", "0",
            "--charging-pay", "0",
            *paths,
        ]

    def append(self, bvid: str, video_paths: Iterable[str | Path]) -> list[str]:
        paths = [str(path) for path in video_paths]
        if not bvid or not paths:
            raise ValueError("bvid and video_paths are required")
        return [*self._base("append"), "--submit", self.submit_api, "--vid", bvid, *paths]


def parse_list_output(stdout: str) -> list[tuple[str, str]]:
    """Read BV numbers and titles from biliup's human-readable list output."""
    entries = []
    for raw_line in stdout.splitlines():
        line = ANSI_ESCAPE.sub("", raw_line).strip()
        match = re.match(r"^(BV[0-9A-Za-z]+)\s+(.+)$", line)
        if match:
            title = re.split(r"\s{2,}|\t", match.group(2), maxsplit=1)[0].strip()
            entries.append((match.group(1), title))
    return entries


def parse_show_output(stdout: str) -> dict:
    """Read the JSON body following optional biliup diagnostic text."""
    start = stdout.find("{")
    if start < 0:
        raise ValueError("biliup show did not return JSON")
    payload = json.loads(stdout[start:])
    if not isinstance(payload, dict):
        raise ValueError("biliup show returned a non-object payload")
    return payload


def broadcast_day(item: dict) -> str:
    """A recording before 07:00 belongs to the previous broadcast day."""
    day = datetime.strptime(str(item["date_part"]), "%Y%m%d").date()
    match = re.match(r"^(\d{1,2})时场", str(item["base_name"]))
    if not match:
        raise ValueError(f"Recording start hour is missing: {item['base_name']}")
    hour = int(match.group(1))
    if hour > 23:
        raise ValueError(f"Invalid recording start hour: {hour}")
    if hour < 7:
        day -= timedelta(days=1)
    return day.strftime("%Y%m%d")


def ordered_source_inputs(inputs: Sequence[dict]) -> list[dict]:
    """Assign source P numbers from local recording date and time."""
    def sort_key(item: dict) -> tuple[str, int, int, str]:
        match = re.match(r"^(\d{1,2})时场(\d{1,2})", str(item["base_name"]))
        if not match:
            raise ValueError(f"Recording start time is missing: {item['base_name']}")
        hour, minute = map(int, match.groups())
        if hour > 23 or minute > 59:
            raise ValueError(f"Invalid recording start time: {item['base_name']}")
        return str(item["date_part"]), hour, minute, str(item["base_name"])

    days = {broadcast_day(item) for item in inputs}
    if len(days) > 1:
        raise ValueError("A daily submission cannot mix broadcast days")
    keys = [(str(item["date_part"]), str(item["base_name"])) for item in inputs]
    if len(keys) != len(set(keys)):
        raise ValueError("Duplicate local recording")
    return [{**item, "source_part": index} for index, item in enumerate(sorted(inputs, key=sort_key), 1)]


def desired_remote_parts(videos: Sequence[dict], inputs: Sequence[dict]) -> list[dict]:
    """Group all raw parts, then danmaku parts, then highlights."""
    ordered = ordered_source_inputs(inputs)
    titles = [
        *(RAW_PREFIX + item["base_name"] for item in ordered),
        *(DANMAKU_PREFIX + item["base_name"] for item in ordered),
    ]
    by_title: dict[str, list[dict]] = {}
    for video in videos:
        by_title.setdefault(str(video.get("title") or ""), []).append(video)
    expected = []
    for index, title in enumerate(titles):
        matches = by_title.get(title, [])
        if len(matches) > 1 or (index < len(ordered) and len(matches) != 1):
            raise ValueError(f"Source part is missing or ambiguous: {title}")
        expected.extend(matches)
    known = {int(video["cid"]) for video in expected}
    remaining = [video for video in videos if int(video["cid"]) not in known]
    if any(str(video.get("title") or "").startswith((RAW_PREFIX.rstrip(), DANMAKU_PREFIX.rstrip())) for video in remaining):
        raise ValueError("Submission has a source part absent from local inputs")
    result = [
        *expected,
        *(video for video in remaining if not str(video.get("title") or "").startswith(HIGHLIGHT_TITLE)),
        *(video for video in remaining if str(video.get("title") or "").startswith(HIGHLIGHT_TITLE)),
    ]
    if len(result) != len(videos) or len({int(video["cid"]) for video in result}) != len(videos):
        raise ValueError("Submission contains duplicate CIDs")
    return result


def archive_edit_payload(submission: dict, ordered_videos: Sequence[dict]) -> dict:
    """Build the archive body used by the Bilibili edit endpoint."""
    original = submission.get("videos") or []
    if len({int(video["cid"]) for video in original}) != len(original):
        raise ValueError("Submission contains duplicate CIDs")
    if {int(video["cid"]) for video in ordered_videos} != {int(video["cid"]) for video in original}:
        raise ValueError("Edit must include the same submission parts")
    if len(ordered_videos) != len(original):
        raise ValueError("Edit contains duplicate or missing parts")
    archive = dict(submission.get("archive") or {})
    archive.pop("limited_free", None)
    archive["videos"] = [
        {"title": video.get("title"), "filename": video["filename"], "desc": video.get("desc") or ""}
        for video in ordered_videos
    ]
    return archive


def replacement_payload(
    submission: dict, *, old_cids: Sequence[int], replacement_cid: int,
    title: str = HIGHLIGHT_TITLE,
) -> dict:
    """Replace old highlights only after the new part is fully transcoded."""
    old = {int(cid) for cid in old_cids}
    if not old or replacement_cid in old:
        raise ValueError("old_cids must be non-empty and exclude the replacement")
    videos = list(submission.get("videos") or [])
    by_cid = {int(video["cid"]): video for video in videos}
    if len(by_cid) != len(videos) or not old.issubset(by_cid) or replacement_cid not in by_cid:
        raise ValueError("Part CIDs are missing or ambiguous")
    if any(not str(by_cid[cid].get("title") or "").startswith(HIGHLIGHT_TITLE) for cid in old):
        raise ValueError("Only highlight parts may be replaced")
    replacement = by_cid[replacement_cid]
    if int(replacement.get("status") or 0) != 0 or int(replacement.get("xcode_state") or 0) != 6:
        raise ValueError("Replacement part is not ready")
    first_old_index = min(i for i, video in enumerate(videos) if int(video["cid"]) in old)
    ordered = []
    for index, video in enumerate(videos):
        cid = int(video["cid"])
        if index == first_old_index:
            ordered.append({**replacement, "title": title})
        if cid not in old and cid != replacement_cid:
            ordered.append(video)
    archive = dict(submission.get("archive") or {})
    archive.pop("limited_free", None)
    archive["videos"] = [
        {"title": video.get("title"), "filename": video["filename"], "desc": video.get("desc") or ""}
        for video in ordered
    ]
    return archive
