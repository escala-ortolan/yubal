# Custom Yubal — OpenCode handoff

Custom backend fork of guillevc/yubal, pinned to upstream
`78d63846712178ded0b5f3c2552a80e952451c3c`.

Read AGENTS.md, docs/IMPLEMENTATION-PLAN.md, docs/CLIENT-CONTRACT.md and docs/PRIOR-RECOMMENDATION.md.

## Status and how to use it

The durable intake backend and React UI are deployed on VM1. See
`docs/EVIDENCE.md` for the deployment checks and backup/rollback references.

The React frontend retains upstream styling/components and provides fresh-mode
download history, playlist previews, ordered track status and attempt details.
Build with `Dockerfile.intake`, or see `docs/UI-REVIEW.md` for local development,
feature coverage and the backend review.

- **Start it and use it:** `docs/RUNNING.md`
- **API endpoints and credentials:** `docs/API.md`
- **API for client developers:** `docs/CLIENT-CONTRACT.md`, schema of record `docs/fixtures/openapi-v1.json`
- **What was actually tested:** `docs/EVIDENCE.md`
- **Dev/test environment:** `docs/DEVELOPMENT.md`

## Project ownership
`/srv/dev/repos/yubal` owns the custom downloader/intake backend: durable source-ID history, aliases, retries, restart recovery, API/device pairing, staging, tagging/filing, playlist reconciliation and backend UI.
`/srv/dev/repos/ytmusic` owns desktop browser extension and Android browser app: actual YouTube playback, site/queue observation, capture policies, controls, local Google session and ad blocking. It calls this backend; it must not create another intake backend or ledger.

Reuse/fork guillevc/yubal's downloader, FastAPI and React UI rather than rebuilding them. Pin upstream and minimize invasive changes. Whether the intake API lives inside the fork or as a sidecar is an implementation decision; one durable ledger and one downloader ownership path are mandatory.

The earlier Symfonium/Subsonic gateway concept is a separate optional track, not required for the YouTube frontend. Latest recalled context says gateway + skip-to-ready-track policy was discussed, but exact protocol behavior was not verified. Do not implement or silently discard it based on older documents; retrieve that discussion/confirm detailed scope before gateway work.

## First OpenCode request
Read this handoff, inspect and pin upstream Yubal, then implement durable intake in isolated configuration/staging. Follow tests-first milestones and publish actual test output. Coordinate the versioned client contract with ../ytmusic. Do not alter the live Yubal service, export Google credentials, write to the live library or build the optional Subsonic gateway without separate approval.
