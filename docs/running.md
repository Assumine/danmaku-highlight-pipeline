# Run the actual pipeline

This is an executable local workflow, not a daemon or a recording service.
It operates on one sealed video/XML pair per job. Existing per-session algorithms
remain responsible for candidate selection and montage boundaries.

## Synthetic end-to-end example

Install the package with `pip install -e .`. Install FFmpeg/ffprobe separately;
FFmpeg needs libx264, AAC, lavfi and libass. No speech model is required for this demo.

```powershell
danmaku-pipeline --demo --workspace ./outputs/demo --preview-height 360
```

The command creates a 120-second moving test pattern with a sine-wave soundtrack,
and XML containing twelve invented users and a synthetic laughter burst. All
fixtures are generated locally. There are no actual viewer messages or IDs.
The resulting montage is shorter than the input. This proves the processing chain,
not whether a real moment is funny.

The workspace contains:

```text
synthetic-input/                 generated demo media/XML only
records/<content-key>/job.json  durable processing receipt
records/<content-key>/analysis/ candidate timeline and alignment report
records/<content-key>/highlight-*/manifest.json
records/<content-key>/montage.xml
records/<content-key>/montage.ass
outputs/<content-key>/*.mp4     raw and burned montages
```

The per-clip manifest includes selection evidence, source-reference overlays,
transition lengths and final boundaries. Danmaku is mapped through those recorded
overlays: `output_start + comment_time - source_time`. Intervals are half-open;
crossfades can have simultaneous comments from both sides. The basic ASS renderer
uses five top-quarter scrolling lanes and drops overflow rather than overlapping
comments. `job.json` reports visible/dropped counts. It does not reproduce a live
player's exact layout or custom platform emoji images.

## Own footage

```powershell
danmaku-pipeline --video ./input/session.mp4 --xml ./input/session.xml --workspace ./outputs/my-job
```

Optional: `--douyin ./input/douyin --room-url <your-local-room-url>` for timestamped
cross-platform evidence; `--speech whisper` for the optional faster-whisper model.
Use local configuration for room URLs. Never commit generated receipts, comments,
models, clips or account files from a real run.

To resume, run the **same input command** again (without `--demo`). The generated
demo pair can likewise be passed with `--video` and `--xml`. Input content, options,
and code hashes select the job. Completed media is validated and checked against
saved SHA-256 hashes. Changed inputs or code create a new job. Failed editor attempts
receive fresh record directories; their logs remain available. A worker lock is
released by the OS after termination, so a stale lock file does not require deletion.

There is no automatic deletion, repair of corrupt source media, multi-day merge,
background polling service, or automatic discovery of unfinished jobs here.
Validation failures stop this job; run other independent jobs normally.

## Explicit upload and reconciliation

The submission entry point appends to an **explicit BV**, not the latest account
submission. It never creates, deletes, reorders or replaces remote parts. Two titles
are used: `[无弹幕版]下饭片段` and `[有弹幕版]下饭片段`.

```powershell
danmaku-submit ./outputs/my-job/records/<content-key>/job.json --target <BV> --biliup ./tools/biliup.exe --cookies ./private/cookies.json
```

Without `--execute` this is a plan only: no account access or network requests.
Review the BV and local receipt first, then explicitly opt in:

```powershell
danmaku-submit ./outputs/my-job/records/<content-key>/job.json --target <BV> --biliup ./tools/biliup.exe --cookies ./private/cookies.json --execute --wait-seconds 120
```

The uploader validates both local files and checks their hashes. Before every
append it records existing CIDs, title and attempt status in `submission.json`.
On return or restart, it queries the remote archive. A newly observed matching CID
is confirmation even if the upload CLI returned an error. An unknown result blocks
duplicate uploading; it is **not** retried blindly. A title already present without
this job's receipt also blocks submission. Resolve ambiguous attempts manually.
Confirmed and platform-ready are separate states; rerun to refresh readiness.

Credentials stay in the caller's external cookie file. The uploader does not copy
cookies, persist CLI stdout/stderr or print commands containing account arguments.
Submission receipts still contain private BV/CID information and must remain local.
The CLI is built for the documented biliup 1.2.6 interface; remote platform changes
may require updates. Network behavior is mocked in the public tests, not tested
against a production account by CI.
