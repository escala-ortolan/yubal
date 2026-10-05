# Upstream pin and integration map

- Source: https://github.com/guillevc/yubal, `master` at
  `78d63846712178ded0b5f3c2552a80e952451c3c` (verified with `git ls-remote`
  on 2026-10-05); workspace/package version `0.10.0`.
- The upstream checkout was copied into this pre-existing handoff directory without
  overwriting its `README.md`, `AGENTS.md`, or `docs/` handoff files. `README.md`
  appears modified relative to upstream by design. No commit or push was made.
- Python 3.12+, `uv.lock`, `web/bun.lock`; Python check commands in `justfile`
  and `.github/workflows/ci.yaml`.
- `packages/api/src/yubal_api/api/app.py`: `/api` router, automatic migrations,
  `create_services()` and scheduler lifecycle. `settings.py`: default DB at
  `YUBAL_CONFIG/yubal/yubal.db`; set config on **local disk**.
- `api/routes/jobs.py` -> `services/job_executor.py` -> `services/sync_service.py`
  -> `yubal.services.playlist_download_service.PlaylistDownloadService` ->
  `yubal.services.download_service.DownloadService`. `api/routes/subscriptions.py`
  and the scheduler also reach the job executor. These all require a common
  per-track ledger gate before they can safely run with custom intake enabled.
- `yubal/models/track.py`: original source, ATV and OMV IDs; selected ID prefers
  ATV. `download_service.py` currently skips if the expected path exists and
  considers tagging failures nonfatal. `api/services/job_store.py` is pruned,
  in-memory job state, not download history.
- `yubal/utils/filename.py`: matched download layout is album-oriented, not the
  desired final flat Main Artist/Title layout. Keep staging and filing separate.
- Upstream Alembic head before custom changes: `03132d5514f9`. Custom revisions:
  `51c7acb10a01` (source identity, intakes, items, aliases, attempts, devices,
  rate events) and `7c921b71e002` (downstream stages, including the pre-move
  filing plan columns). The downstream table is created from the same
  `MetaData` the ledger uses, so tests using `metadata.create_all` and a migrated
  DB stay identical.
- `api/app.py` `custom_openapi()` injects legacy SSE paths and event schemas.
  Fresh mode returns its own schema before that injection so the client contract
  contains no `/api` routes.

Current custom work: source-ID/intake ledger, migrations, device-scoped `/v1`
acceptance/read-back/retry and retry-tagging, and an opt-in single-owner staging
worker using upstream `YTDLPDownloader`. Legacy routes and scheduler are excluded
in fresh mode, per user direction. No pairing UI is connected. external tagger is a
separate folder-scanning job; the backend tracks its downstream status without
invoking its broken CLI or conflating tagging with download success.
Owner-local observation confirms tagged audio by artist/title and decoded
fingerprint; the filing service then moves verified tagged audio into an
owner-named music root, recording intent before the move so an interrupted
filing reconciles instead of repeating. Filing never overwrites and never
touches an existing library without reviewed approval.
