# Backend review and React UI restoration

Review date: 2026-10-05. Baseline: custom `5f722da` on upstream
`78d63846712178ded0b5f3c2552a80e952451c3c`, including the pending auto-mode work.

## Cause of the reduced interface

`api/app.py` explicitly excluded `web/dist` in fresh mode and mounted the inline
`api/routes/dashboard.py` form. The upstream React app was still in `web/`, but
its JobsProvider, subscription screens, settings and log stream called `/api`.
Those routes belong to the legacy job system, not the durable intake ledger.

Fresh mode now serves the React bundle, with a server-injected runtime mode marker.
The same source still supports the legacy UI when running the legacy backend.
The inline form remains the fallback when no frontend bundle is installed.
Unknown API and asset paths return 404 rather than a misleading SPA HTML response.

## Delivered fresh-mode UI

- Upstream React/HeroUI/Tailwind, theme toggle, branding and attribution.
- Single track links/IDs, ordered queue and playlist snapshots with deduplication.
- Public playlist URL/ID preview, title/artist hints, up to 100 entries; explicit
  submission after review. Preview itself never downloads.
- Device-scoped paginated history, per-page counts, search and state filters.
- Downloaded tracks display server-resolved artist/title even when capture clients
  submitted only video IDs; history can be sorted by title or artist.
- Ordered track tables, verified-track completion bars, download and downstream
  states. Automatic captures are shown with their original mode/context.
- Detail views for sanitized failures, attempts, codec, size, duration, audio
  fingerprints and final path; explicit failed-download retry and tagging retry.
- Ledger-backed cron/timezone schedules with create/edit, pause/enable, run now,
  last-run results, and deletion. Restart recovery reuses the saved snapshot/UUID.
- Cancel/resume jobs and delete from history without discarding source identity.
- Explicit force-redownload and forget-song actions with replay-safe receipts,
  canonical/alias ownership checks, and preserved audio files.
- Four-second polling with cancellation on view changes/disconnect. The dashboard
  keeps the device pair in this tab's `sessionStorage` across refreshes; it clears
  the pair on Disconnect or tab close and never puts it in a URL or localStorage.
  An uncertain POST is retried with the same request UUID/body.
- LAN HTTP-compatible request UUID generation using `crypto.getRandomValues`.

After this review the user explicitly authorized ledger-backed schedules and job
controls, including forget/force-redownload. Those equivalents are implemented;
the upstream scheduler and downloader paths remain excluded in fresh mode.
Global log streaming and administrative cookie upload are not exposed to device
tokens. Byte-level live speed/ETA is not recorded by the intake worker. The UI
reports verified-track completion, not invented transfer percentages. Private
playlists still require client-side capture. These gaps need separate API and
ownership designs before their upstream controls can be enabled in fresh mode.

## Review findings

| Priority | Finding and location | Disposition |
| --- | --- | --- |
| High | Fresh UI was disconnected from upstream frontend (`api/app.py`, `web/src/main.tsx`) | Fixed with a runtime-selected React intake UI and generated API types. |
| High | Startup checked missing staging output before reconciling an interrupted filing move (`api/app.py` lifespan) | Fixed. Filing recovery runs first; a shared local filing lock excludes concurrent CLI moves throughout startup recovery. A real-audio lifespan test covers the ordering. |
| High | Filing's `_move_no_clobber` used existence-check + `os.replace`, and cross-filesystem `shutil.move` could overwrite a concurrently created destination (`services/filing.py`) | Fixed after red tests reproduced concurrent overwrite and dangling-symlink replacement. Publication now uses atomic no-replace hard links; cross-filesystem copies are completed/fsynced privately before publication. Actual EXDEV/process-death recovery and concurrent retry tests pass. |
| Medium | History lacked original intent and attempt evidence (`api/routes/intakes.py`) | Added ownership-checked detail endpoints, without changing existing response models. |
| Medium | Browser retry after lost response could create new requests; LAN HTTP lacks `crypto.randomUUID` | React UI retains uncertain intent and uses secure random bytes directly. |
| Medium | Missing timestamps/progress events in ledger (`db/intake_ledger.py`) | Outstanding. Add timestamped durable lifecycle events before offering chronological logs or speed/ETA. No fabricated timestamps or schema migration in this update. |
| Medium | A startup media-verification error in filing reconciliation can prevent service startup; worker task failures have no degraded-health indicator | Outstanding. Separate recoverable per-source failures from worker-fatal failures and expose health explicitly. |
| Medium | `verify_audio` detects size changes but not same-size concurrent rewrites | Outstanding. Stronger file-version checks/ownership would improve independent verification. |
| Low | History detail loading is one bounded list plus up to 20 detail calls per refresh | Functional at this scale; an additive expanded-history response would reduce requests for large/multi-device deployments. |
| Low | README claimed the already-deployed backend was planning-only | Corrected; historical evidence remains date-scoped. |

Reviewed the custom intake routes/auth, ledger transitions, worker verification,
filing/recovery, migration shape, app lifecycle/static serving, React routing/API
clients and container packaging. The upstream downloader remains pinned. This is
not a claim that every upstream module or every failure window has been audited.

## Build and run

`docker build -f Dockerfile.intake -t yubal:intake-ui-staging .` builds both the
React bundle and backend, with pinned base images and frozen dependency locks.
Use isolated local SQLite/staging directories for evaluation; production cutover
requires approval. Migration `91a30c1e0003` adds schedules, controls and action
receipts. Take a local SQLite backup before upgrading. For production rollback,
restore that pre-upgrade snapshot with the previous image; a simple image swap
cannot resolve an Alembic revision unknown to the old image. The isolated suite
tests upgrade/downgrade/re-upgrade and backup restore integrity. Downgrading alone
drops the new controls/schedules, so old workers would not honor cancellations.

Filing uses a local `.filing.lock` beside the database to serialize plan/recovery
operations. Destination filesystems must support hard links; unsupported operations
fail rather than falling back to an overwriting rename. A hard kill during a
cross-filesystem copy can leave a private `.yubal-filing-*` temporary file, while
retaining the source. Such leftovers are not treated as completed audio or
automatically deleted. Production NFS behavior has not been exercised by these tests.

For frontend development: `bun install --frozen-lockfile` then
`VITE_INTAKE_ONLY=true bun run dev` in `web/`. Vite proxies `/v1` to localhost:8000;
run the backend there with isolated config and the worker disabled for UI tests.
Export OpenAPI and regenerate the frontend types after backend schema changes.

## Verification

- Python API/ledger regressions include ownership, original ordered hints,
  attempts, playlist authentication/validation, and schema export equality.
- Controls tests cover shared-source cancellation, replay-safe explicit resets,
  ownership refusal, file preservation and a subprocess killed after scheduled
  submission but before acknowledging its run (one intake after restart).
- Playlist API test substitutes metadata lookup explicitly. Browser preview uses
  mocked metadata; its submission and history use real staging HTTP and SQLite.
- Browser smoke test on LAN HTTP exercised device connect, one-song acceptance,
  polling/read-back, details, ordered duplicate playlist submission, playlist tab,
  disconnect, desktop and 390px mobile. No page errors or mobile document overflow.
- These UI tests do not claim a new real YouTube download or private-playlist access.
- A separate real, unauthenticated public playlist metadata lookup returned three
  ordered entries successfully; no audio was downloaded by that check.
- A real cross-filesystem test used local-disk staging and `/dev/shm` audio output,
  killed the filing subprocess after publication but before source removal, and
  recovered audio/sidecars to one completed filing with one original download attempt.
- Production deployment and Git publication are separate from this working-tree update.
