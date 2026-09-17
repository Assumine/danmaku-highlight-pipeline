# Security and data handling

Do not commit platform cookies, tokens, account identifiers, raw production logs, private room URLs, or unreviewed danmaku/video files.

The default `.gitignore` excludes common credential and media paths, but it is not a substitute for reviewing `git diff --cached` before publishing. If a credential is committed, revoke it first; deleting it from the latest commit is not sufficient.

Raw danmaku may contain stable user identifiers and personal text. Anonymize datasets before sharing them publicly.
