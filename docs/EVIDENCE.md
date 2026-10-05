# Measured progress (2026-10-05)

- Pinned upstream `git ls-remote` returned
  `78d63846712178ded0b5f3c2552a80e952451c3c` for `refs/heads/master`.
- Baseline using isolated config/staging: `pytest packages/api/tests
  packages/yubal/tests -q` -> **700 passed in 6.29s**.
- Test-first intake check initially failed collection with
  `ModuleNotFoundError: yubal_api.db.intake_ledger`.
- `pytest packages/api/tests packages/yubal/tests -q` with the new migration/ledger
  tests -> **708 passed in 2.61s**. Targeted `ruff check`, `ruff format --check`
  and `ty check --python /tmp/opencode/yubal-venv` -> **All checks passed**.
- Scratch SQLite migration upgrade/downgrade and ledger unit checks passed.
  These tests use synthetic IDs and, where noted, synthetic file bytes;
  they do **not** establish playable audio or real download behavior.
- No live API, client, external tagger, Navidrome, device pairing, or real download was
  exercised. No production cutover is authorized.

Fresh-mode API work after that baseline: new `/v1` auth, submit/read-back, replay,
device ownership/revocation, exclusion of legacy routes/scheduler, and NFS
rejection tests. In-process TestClient checks exercise HTTP semantics with a
scratch SQLite database, **not** a running remote server or actual audio.

- Fresh-mode regression command: `pytest packages/api/tests packages/yubal/tests -q`
  using isolated local config/staging -> **712 passed, 1 Starlette/httpx
  deprecation warning in 5.42s**. `ruff check packages/api/src packages/api/tests
  scripts/intake_devices.py`, `ruff format --check` over the same paths, and
  targeted `ty check --python /tmp/opencode/yubal-venv` -> **All checks passed**.

## Isolated worker checks (2026-10-05)

- Tests generated a real 1-second AAC/m4a fixture using ffmpeg, copied it via
  a **fake network transfer**, and ran ffprobe/full decode, crash-window recovery,
  missing-output detection, explicit retry, and exclusive worker startup. Fake
  transfer results establish ledger/worker behavior, not YouTube availability.
- An additional test spawns a separate interpreter, claims a source, copies a
  decodable m4a into its planned path, then calls `os._exit(17)` before ledger
  completion. A new worker reconciles the file without invoking its fake
  transfer, leaving one downloaded attempt. This covers the output-to-ledger
  process-death window; other kill boundaries and real network interruption
  remain future work.
- Actual network check used pinned upstream `YTDLPDownloader` with no Google
  cookies and public source video `jNQXAC9IVRw`, local SQLite at
  `/tmp/opencode/yubal-e2e2/intake.db`, and isolated staging at
  `/tmp/opencode/yubal-e2e2/staging`. `IntakeWorker.run_once()` returned true;
  ledger state `downloaded`, attempts **1**, and a separate `verify_audio()`
  invocation found decodable AAC m4a: **309,136 bytes, 19.063583 seconds**.
  In a new Python process, a second intake of the same source yielded
  `run_once() == False`, state `downloaded`, attempts **1**, and independent
  file/probe confirmation. No new transfer was attempted in that process.
- Upstream's documented ATV test source `Vgpv5PtWsn4` also downloaded in
  isolated `/tmp/opencode/yubal-e2e-atv`: one attempt, state `downloaded`,
  independent AAC/m4a probe **2,909,905 bytes, 179.825011 seconds**.
- After adding a decoded-PCM fingerprint, another real isolated check of
  `jNQXAC9IVRw` produced one verified m4a (309,136 bytes, 19.063583 seconds,
  AAC; decoded PCM SHA-256
  `4caca51fa6067363b74ee4fe3ea2ceda572aceda7b3c764115c3b12ab4248f66`).
  A separate process resubmitted it and confirmed **one attempt, no transfer**,
  `downloaded` audio and `waiting_for_tagger` downstream.
- Tests simulate an external metadata-only edit using ffmpeg and verify the
  decoded-audio fingerprint remains the same; a different playable recording
  becomes `missing_output`. An owner-local tag acknowledgement checks real
  artist/title tags and does not call external tagger. No actual external tagger run occurred.
- A read-only filing preview test proposes `Music/Main Artist/Song (Live
  Version).m4a` with same-stem `.lrc` and `.jpg`, omits a `feat. Guest` credit
  from the folder, and detects a case-insensitive existing-title collision.
  It does not move audio or write into the target library.
- A synthetic evidence-backed alias test submitted an alternate source after
  the canonical source's verified transfer and read back `downloaded` with one
  attempt and no second fake transfer. This does **not** verify a real YouTube
  ATV/OMV resolution or cross-recording equivalence.
- Initial real attempt with `BaW_jenozKc` failed as video unavailable; ledger
  correctly recorded `failed`, one attempt and no m4a. It is not evidence of
  a successful download.

This verifies one real staging download and source-ID resubmission, **not** an
alias, final-file move, external tagger scan, Navidrome indexing, or a production path.

- Subsequent isolated regression run (including history, bounded queue,
  device rate-limit and client fixture tests): `pytest packages/api/tests
  packages/yubal/tests -q` -> **725 passed, 1 Starlette/httpx deprecation
  warning in 7.94s**. `ruff check` on API/tests/provisioning script and targeted
  `ty check --python /tmp/opencode/yubal-venv` -> **All checks passed**.
- With external-tag checks, decoded-audio fingerprint reconciliation and the
  read-only filing preview: `pytest packages/api/tests packages/yubal/tests -q`
  on isolated paths -> **730 passed, 1 Starlette/httpx deprecation warning
  in 9.74s**. Targeted `ty check` -> **All checks passed**. The additional
  `mutagen>=1.48.1` direct API dependency changed only its workspace edges in
  `uv.lock`; the resolved package version remained 1.48.1.

- A scratch-only SQLite online backup/restore test preserved a submitted
  source and refused to overwrite an existing destination. No production DB
  or audio was restored.
- `docker build -f Dockerfile.intake -t yubal:intake-test-78d638 .` succeeded
  using digest-pinned Python/uv bases and SHA-256-checked ffmpeg. Build-time
  smoke checked `/v1/intakes` OpenAPI and ffmpeg/ffprobe. A subsequent build
  ran all migrations against a disposable local DB as uid1000/gid2000 and
  verified source/device/downstream tables. No runtime container was started
  or production image replaced. Build-time Debian apt packages are not
  snapshot-pinned.
- Latest isolated suite with backup test: **731 passed, 1 Starlette/httpx
  deprecation warning in 13.67s**. Formatting was then applied to the new
  backup code before the next suite run.
- Updated isolated suite covering explicit alias reuse and uniform fresh-API
  errors: **733 passed, 1 Starlette/httpx deprecation warning in 10.62s**.
  `ruff check` and targeted `ty check` then passed after a type annotation and
  formatting-only fix.

Outstanding release gates: an actual separate external tagger scan; library indexing,
playlist and final-path reconciliation against a real player; owner-approved
pairing/HTTPS and client fixtures on real clients; rollback of an isolated
deployed container and independent downstream indexing checks.
Legacy jobs and subscriptions are out of scope and disabled in fresh mode.

## Filing and downstream stage recovery (2026-10-05)

Full isolated suite after the filing stage, `/v1/tracks/{video_id}/retry-tagging`
and the client schema fixture: **742 passed, 1 Starlette/httpx deprecation
warning in 12.98s**. `ruff check`, `ruff format --check` and targeted
`ty check --python /tmp/opencode/yubal-venv` over the intake modules, routes,
tests and scripts -> **All checks passed**. The earlier lint/type fixes were the
ledger `state`/`downstream_state` return types now being `Literal`
`SourceState`/`DownstreamState`, so the route models and the ledger cannot drift
silently.

Unit coverage in `packages/api/tests/test_filing.py` (real ffmpeg-generated AAC,
**fake** transfer): filing moves audio plus `.lrc` and `.jpg` sidecars and records
the final path; a case-only conflicting target is refused and nothing is written;
a separate interpreter that moves the audio and calls `os._exit(17)` before the
ledger write is recovered by `reconcile_filing` to `filed` with one attempt and
no second move; a filing plan whose move never started is cleared and remains
filable; a deleted or replaced filed file becomes downstream `missing_output`
with the download state still `downloaded` and the attempt count unchanged; and
retrying tagging never touches the download. A fresh-API test drives a real
transfer and tag observation, then confirms `POST
/v1/tracks/{video_id}/retry-tagging` is 404 for another device, 409 until
`tagged`, then returns the reopened track with the download state, attempt
count and verified staging file unchanged.

Real-download evidence, isolated under `/tmp/opencode/yubal-e2e-filing`,
`/tmp/opencode/yubal-e2e-probe` and `/tmp/opencode/yubal-e2e-crash`, no Google
cookies, pinned upstream `YTDLPDownloader`, public source `jNQXAC9IVRw`:

- One `IntakeWorker.run_once()` -> `downloaded`, attempts **1**, staged m4a
  **309,136 bytes, 19.063583 seconds**. After owner-local tagging, filing moved
  it to `Music/Main Artist/Filed From A Real Download.m4a` (**309,283 bytes**)
  with its `.lrc`, recorded downstream `filed` and the final path, removed the
  staging copy, left attempts at **1**, and `reconcile_filing` then returned 0
  changes. A second filing attempt was refused as already filed. Deleting the
  filed file and reconciling returned 1 change, downstream `missing_output`,
  download state still `downloaded`, attempts still **1**, and
  `IntakeWorker.run_once()` returned `False` — no re-download.
- Independent post-move probe of the filed file: `ffprobe` reports an `aac`
  audio stream and **19.063583 seconds**; `ffmpeg -f hash -hash sha256` gave
  `4caca51fa6067363b74ee4fe3ea2ceda572aceda7b3c764115c3b12ab4248f66`, equal to
  the pre-move fingerprint, so the move preserved the decoded audio.
- Real process-death window: a child interpreter recorded the plan, moved the
  audio, and exited **17** before the ledger write; the audio was present at
  **309,269 bytes** and both sidecars were still in staging. A fresh process and
  ledger handle reconciled **1** change to `filed`, finished the `.lrc` and `.jpg`
  moves without moving the audio again, left **1** attempt, and an independent
  `ffmpeg` hash of the recovered file still equalled
  `4caca51fa6067363b74ee4fe3ea2ceda572aceda7b3c764115c3b12ab4248f66`.

## Live server loop over HTTP (2026-10-05)

The previous runs exercised library code directly. This one started the actual
server with the new `scripts/run_intake_api.sh` (loopback `127.0.0.1:8011`,
isolated `/tmp/opencode/yubal-run`, one owner-provisioned device, worker
enabled, no Google cookies) and drove it with `curl`.

- `GET /v1/health` -> `{"status":"healthy"}`.
- `scripts/intake_devices.py provision` printed one `device_id` UUID and token.
- `POST /v1/intakes` for `jNQXAC9IVRw` -> **HTTP 202** with
  `status: "pending"`. A first startup attempt failed correctly: pointing
  `YUBAL_INTAKE_STAGING` at `YUBAL_DATA` was rejected with "Intake staging must
  be outside YUBAL_DATA", so the launcher uses a separate music root.
- `GET /v1/tracks/jNQXAC9IVRw` a few seconds later ->
  `status: "downloaded"`, `downstream_status: "waiting_for_tagger"`,
  `final_path: null`, with a **309,136-byte** file in staging.
- The same `video_id` was resubmitted under a **new** `request_id`. The second
  intake came back `status: "downloaded"` immediately; staging still held
  **one** file; the database held **one** source row, **one** download attempt,
  and **two** intakes. Duplicate suppression confirmed over HTTP.
- Tags were written, `observe_intake_tags.py` returned `tagged`, and
  `file_intake_sources.py` printed the proposal in dry-run mode before `--apply`
  moved the file.
- Final `GET` -> `downstream_status: "filed"`,
  `final_path: "/tmp/opencode/yubal-run/music/Main Artist/Meitnerium.m4a"`
  (**309,269 bytes**), staging empty. An independent `ffprobe` reported
  `codec_name=aac`, `duration=19.063583`, and `ffmpeg -f hash -hash sha256`
  returned `4caca51fa6067363b74ee4fe3ea2ceda572aceda7b3c764115c3b12ab4248f66`,
  equal to the fingerprint captured before filing.

This is the backend alone. No browser extension was loaded and no real YouTube
Music page was involved, so the consumer-side round trip is still untested. The
server is loopback-only with no HTTPS, and it was not left running as a service.

These runs exercised real YouTube transfers with an isolated local SQLite DB and
scratch music roots. They still do **not** cover an existing library,
indexing/playlist reconciliation, device pairing over HTTPS, or any production
path.

## Client contract coordination (2026-10-05)

`docs/CLIENT-CONTRACT.md` is now versioned as `2026-10-05.filing1`, naming
`docs/fixtures/openapi-v1.json` as the schema of record and `scripts/
export_intake_openapi.py` as the generator. The counterpart repo
`/srv/dev/repos/ytmusic` is **not** a git repository, so coordination was done by
editing its docs and shared client types in place; no commit or push was made on
either side.

Consumer-side drift found and corrected there, driven by the exported schema:
source kinds were `track|playlist|radio|album` but the backend accepts
`song|queue|playlist|radio`; `manual_playlist` mode, a 1-100 track bound,
200-character hints, contiguous zero-based positions, `manual_song` arity, a
UUID `device_id`, the nullable `final_path` read-back and both
`POST /v1/tracks/{video_id}/retry*` routes were missing; and transport parsed a
`tracks` array with `id` refs where the contract returns `items` with
`position`/`video_id`/`status`/`downstream_status`/`final_path`. The consumer's
own suite reported **41 passed** across 7 files after the change, with `tsc
--noEmit` and `eslint .` exiting 0. These are consumer-side unit tests only:
no client binary was built, installed or run against this backend, so the
round trip remains **NOT TESTED** on both sides.
