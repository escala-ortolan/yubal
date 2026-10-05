# Custom Yubal Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.
> **For OpenCode:** Execute these test-first milestones; no automatic commits or live deployment.

**Goal:** Make Yubal download-once by proven source identity, restart-safe and usable by independent YouTube Music clients.
**Architecture:** Reuse pinned guillevc/yubal downloader/API/UI. Add persistent intake records and workers, preserving ordered membership separately from files and downstream stages. One authoritative backend serves existing/manual clients and ../ytmusic.
**Tech Stack:** Existing upstream Python/FastAPI/React/TypeScript, database migrations and yt-dlp/ytmusicapi. Local-disk SQLite initially, upstream tooling and dependency versions verified during discovery.

**Scope update (user direction):** Start fresh for intake. The new deployment enables `YUBAL_INTAKE_ONLY=true`, excludes legacy jobs/subscriptions and leaves their scheduler off. Do not wire those paths into the new ledger. external tagger is a separate folder-scanning job, not a step invoked by the Yubal downloader.

## 0. Discovery and development surface
1. Inspect upstream manifests, commit SHA, migrations, job schemas, worker/download completion, track identity, filename rules and UI. Record exact source paths and version in docs/UPSTREAM.md. Starting anchors: packages/yubal/src/yubal/services/download_service.py; models/track.py; utils/filename.py; packages/api/src/yubal_api/services/job_store.py; migrations/versions/b36ae7fb398c_initial_schema.py; Dockerfile and CI.
2. Observe deployment read-only if authorized: VM1 192.168.3.11, historical /opt/youtube, Yubal :8011 -> container :8000. Inspect /openapi.json, actual image/version, volumes and nonsecret settings. Never dump .env/cookies. Historical format m4a, scheduler OFF; reverify.
3. Establish dev/test commands from manifests, lockfiles and CI. Do not assume pip installed. Create docs/DEVELOPMENT.md with working commands and real outputs. Define exact new module/test paths after inspecting package conventions; paths in the old frontend handoff are proposals, not upstream symbols.
4. Isolated config, staging outside served Music, test port after collision check, resource bounds. Verify local DB location (workspace may be NFS). No live image/container changes.

## 1. Versioned intake contract
Publish precise OpenAPI + fixtures from CLIENT-CONTRACT.md before clients diverge. Backend owns schemas/auth/pairing/rate limits; frontend owns content observation/capture thresholds. Fresh intake mode excludes legacy `/api` endpoints and subscription scheduling. Unknown endpoints or job fields require discovery, not guessing.

For each code task: write focused failing tests, run/record failure, minimal implementation, passing focused tests plus regressions. Record real command in DEVELOPMENT.md; do not invent test counts.

## 2. Persistent schema and identity
Add migrations for devices, intakes, ordered intake_items, source_tracks, alias evidence, download_attempts, stage records and worker leases. Use request UUID idempotency; same UUID+different payload is conflict. Unique canonical source identity and transactional claim prohibit concurrent duplicate downloads.

Preserve original source_video_id, selected download ID, atv_video_id and omv_video_id. Alias only with resolution evidence; don't collapse same-title live/studio/remix or unrelated uploads. Existing-library adoption uses verified tags/fingerprints/recording evidence, never extraction cache as proof.

Tests: schema upgrade/rollback on scratch copy; UUID replay/conflict; two simultaneous source submissions; alias resolution; ordered duplicate occurrences remain in playlist while download dedupes; legacy submission routes are absent in fresh mode.

## 3. Restart-safe worker and completion evidence
States pending/downloading/downloaded/waiting-for-tagger/tagged/filed/ready plus stage-specific failed/cancelled/missing-output. Durable attempts capture original/resolved IDs, verified audio path/format/duration/checksum, error/retry timing and final path. Mark downloaded only after playable output is verified. Bound concurrency, retries/backoff and request size; cancel explicitly.

JobStore is memory-resident/prunes jobs. Filename existence and metadata extraction cache cannot close the completion crash window. Add completion hooks or deterministic reconciliation in the downloader, not merely an optimistic sidecar log. An upstream timeout can mean accepted: persist submission IDs and reconcile before retry.

Tests: partial/corrupt audio; HTTP timeout after accepted job; process death before write, after output, before ledger update; stale lease and resume; two workers; cancellation; unavailable/age-restricted content; successful restart/resubmission causes no new network download. Store failures visibly, never spin infinitely.

## 4. Downstream stages and filing
Native m4a output avoids unsupported Opus for external tagger; don't transcode merely to fix ledger behavior. Download to staging, never directly into served library during development. external tagger identifies/tags; current CLI license path fails, so waiting-for-manual-UI-tagging is a supported state. Do not re-enable broken CLI automatically.

File Music/Main Artist/Title with guest credits removed from folder (not legitimate & in artist names), preserve lyrics/artwork, normalize case conflicts for Windows compatibility. Protect curated genres and higher-quality copies; same-title is not proof of same recording. Collision review rather than silent replacement. Existing filing script is candidate after source inspection.

Persist final path after moves. Retry tagging/filing without downloading again. A missing final file is missing_output, not already_saved; deliberate force-redownload creates an audited new attempt.

Tests: metadata correction changes expected upstream path but ledger still skips; same source after move/restart skips; tagger failure does not redownload; sidecars move with stem; recording collisions/higher quality retained for review; root-squash/write permissions surfaced clearly.

## 5. Playlist reconciliation and indexing
Ordered membership independent of physical paths. Snapshot queue is not infinite radio. Reconcile M3U after final moves, retaining unavailable/pending entries in backend state without emitting broken ready-playlist paths. Subscription syncing OFF until explicitly enabled; all enabled sync paths must use ledger.

NFS has no inotify; verify Navidrome scan explicitly/schedule. Navidrome tags/indexing, AudioMuse analysis and Nextcloud scans are separate downstream tasks, not evidence of download completion. First exercise on scratch library/test server; production integration requires approval.

Tests: order/removal/reorder/repeated occurrence; pending to ready updates; moved files; cancelled downloads; no stale playlist paths. Compare generated files and test Navidrome API independently.

## 6. API/UI operations
Reuse React UI for history/status/retry/force-redownload and failed-stage visibility. Separate request accepted, download complete and library ready. Show resolved aliases and final-path evidence, not 'already in library' based only on file_exists. Retry downstream only; force-redownload explicit confirmation. Add paginated history, device revocation and authenticated pairing. No Google cookies sent by default from frontend.

Tests: authorization/ownership, revoked token, limits, SSRF/malformed IDs, secret redaction, idempotent API retries, UI reflecting read-back state, stage-specific retry and no accidental force operation.

## 7. Acceptance and release
- Real isolated track -> verify audio -> change tags/move -> restart -> resubmit source and proven alias -> no new network download.
- Concurrent duplicates yield one successful download. Partial/failed/cancelled jobs remain retriable.
- Kill/restart at all completion boundaries; output and database reconcile without silent duplicate work.
- Tagging outage only blocks downstream. Lyrics/artwork and ordered playlists survive moves.
- Restore DB backup on local disk and reconcile storage; ledger migrations and retention documented.
- Test generated OpenAPI against ../ytmusic client fixtures. No frontend app build belongs here.
- Build pinned image, preserve upstream patch list, provide isolated deployment/rollback runbook, then request production approval. Actual test/build outputs and independent storage/API checks go in docs/EVIDENCE.md.

## Optional future track: Symfonium/Subsonic gateway
Separate from core and current YouTube browser clients. Latest recalled discussion: Symfonium -> gateway -> Navidrome for existing tracks + YouTube radio/Yubal for missing; skip-to-ready-track policy. Do not assume Navidrome supports virtual files or Symfonium updates an existing queue dynamically. Retrieve exact latest session design and test protocol/client behavior before writing a gateway plan. Core ledger/API should be reusable without preemptively implementing gateway endpoints.
