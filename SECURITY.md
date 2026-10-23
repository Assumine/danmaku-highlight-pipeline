# Security and data handling

Do not commit platform cookies, tokens, account identifiers, raw production logs, private room URLs, or unreviewed danmaku/video files.

The default `.gitignore` excludes common credential and media paths, but it is not a substitute for reviewing `git diff --cached` before publishing. If a credential is committed, revoke it first; deleting it from the latest commit is not sufficient.

Raw danmaku may contain stable user identifiers and personal text. Anonymize datasets before sharing them publicly.

## Generated artifacts are private

`pipeline.py` writes local file paths, content identities and processing evidence.
`submission_worker.py` writes BV/CID receipts. Keep the entire run workspace outside
tracked source directories, or under ignored `outputs/` and `records/` directories.
Do not attach unreviewed receipts or logs to public issues. The synthetic demo is
the preferred way to report a reproducible processing problem without audience data.

The upload worker reads a caller-owned Cookie file, does not copy it, and does not
persist upload CLI stdout/stderr. This does not make third-party tools or arbitrary
local logs safe to publish. Keep credentials under an external private directory.

## Before publishing

- Review tracked and untracked files, not only the diff of existing source files.
- Inspect history before the first public push; current ignore rules do not remove old secrets.
- Check for account cookies/tokens, signed URLs, private room IDs, BV/CID records,
  local production paths and original viewer messages/identifiers.
- Use fabricated IDs and generated media for public examples and tests.
- Stage explicit paths, inspect the staged diff, run tests, then publish.

Keyword scans reduce risk but cannot prove that every possible secret has been detected.
