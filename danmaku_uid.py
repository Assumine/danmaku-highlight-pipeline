"""Shared UID filtering for alignment, reaction analysis and program points."""

from collections import defaultdict

UID_WINDOW_SECONDS = 15.0
INVALID_UIDS = {"", "0", "unknown", "dy_unknown", "none", "null"}


def valid_uid(uid: str) -> bool:
    return str(uid).rsplit(":", 1)[-1].strip().lower() not in INVALID_UIDS


def filter_uid_spam(messages):
    by_user = defaultdict(list)
    for message in sorted(messages, key=lambda item: item.time):
        if valid_uid(message.user):
            by_user[message.user].append(message)
    accepted = []
    for rows in by_user.values():
        left = 0
        while left < len(rows):
            right = left + 1
            while right < len(rows) and rows[right].time < rows[left].time + UID_WINDOW_SECONDS:
                right += 1
            group = rows[left:right]
            accepted.extend(group if len(group) <= 2 else (group[0], group[len(group) // 2]))
            left = right
    accepted.sort(key=lambda item: item.time)
    return accepted, len(messages) - len(accepted)
