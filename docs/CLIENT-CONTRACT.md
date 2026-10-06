# Yubal intake API

Contract version: `2026-10-06.ui2`. Schema of record:
`docs/fixtures/openapi-v1.json`, exported from the running fresh-mode app and
asserted equal to it in the test suite. Bump this version whenever a route,
status enum or response field changes.

Authoritative owner: `/srv/dev/repos/yubal`. The generated FastAPI `/openapi.json`
is the schema source. These are custom `/v1` routes, not upstream Yubal routes.

## Current implementation (isolated `YUBAL_INTAKE_ONLY=true` only)

### Additive UI endpoints (`ui1`)

- `GET /v1/intakes/{intake_id}/details`: existing intake summary plus `request`,
  the original ordered `IntakeRequest`, including hints, mode and source context.
- `GET /v1/tracks/{video_id}/details`: existing track summary plus `attempts`.
  Each attempt exposes its UUID, status, sanitized error, audio byte count,
  duration, codec, byte SHA-256 and decoded-PCM SHA-256. Missing evidence is null;
  attempt order is unspecified, not a timestamped activity log.
- `POST /v1/intakes/preview-playlist`: `{ "playlist_id": "PL...", "limit": 100 }`.
  Authenticated public metadata lookup; returns `playlist_id`, `title` and ordered
  `tracks` compatible with `IntakeRequest`. Limit is 1–100 (default 100).
  Invalid IDs/limits return 422; upstream/unavailable/empty previews return 502
  with `{ "error": "request_failed", "message": "Public playlist preview unavailable" }`.
  Unavailable entries are omitted and positions renumbered. Preview creates no
  intake and downloads no audio. It consumes the existing per-device request
  budget (429 when exhausted); clients must separately submit reviewed tracks.

All three routes require the existing bearer authentication. Detail reads enforce
device ownership and return 404 to other devices. Existing response models and
all six manual/automatic modes remain compatible.
Frontend types in `web/src/api/intake-schema.d.ts` are generated from the schema
of record; regenerate with `bunx openapi-typescript ../docs/fixtures/openapi-v1.json
-o src/api/intake-schema.d.ts` from `web/`.

Client handoff for `../ytmusic`: these additions are optional. Existing capture
clients can continue to use their current payloads and endpoints unchanged.

### Job controls, explicit resets and schedules (`ui2`)

These features were explicitly authorized after the initial UI review. Migration
`91a30c1e0003` adds control records, replay-safe action receipts and schedules.

- `PATCH /v1/intakes/{id}/control`, body `{ "state": "active|cancelled|deleted" }`.
  `cancelled` suppresses future claims solely requested by that job. An in-flight
  track may finish and shared active jobs continue. `active` resumes eligibility.
  `deleted` cancels and hides the job from history, preserving request-ID replay,
  source identity and files. Details now include `state`. Delete is a history
  operation, not removal of the download-once ledger.
- `POST /v1/tracks/{id}/force-redownload`, body `{ "request_id": "uuid" }`.
  Returns 202 with `video_id`, `status: pending`, and a new `intake_id`.
  Explicitly resets verified/failed/missing sources and creates active manual
  intent. Previous audio files are preserved, previous source/downstream evidence
  is archived in the action receipt, and new audio gets a distinct attempt path.
- `DELETE /v1/tracks/{id}`, JSON body `{ "request_id": "uuid" }`.
  Returns 200 with `video_id`, `status: forgotten`, `intake_id: null`.
  Removes canonical/alias source identities, attempts, downstream state and their
  track memberships. Audio files are untouched. Original request payloads and
  action audit receipts remain; they are not deduplication gates. Earlier intakes
  can now contain fewer or zero items. Their original positions remain intact.
  Submit with a **new request UUID** to download the forgotten song again.
- Both track actions are transactional and replay-safe by request UUID; reusing
  an action UUID for changed intent gives 409. They return 404 for unowned sources,
  409 for active downloads/filing or another device's shared canonical/alias
  ownership. Force on an already-pending source gives 409. New actions consume
  the device rate budget (429); force also respects queue capacity (409).
- `GET/POST /v1/schedules` list/create device-owned schedules; creation returns
  201. `PUT/DELETE /v1/schedules/{id}` edit/delete. `POST
  /v1/schedules/{id}/run` schedules immediate execution (202), **enabling a paused
  schedule**. Schedule edits/deletion during a durable active run return 409.
  Other devices receive 404 and cannot see the schedule in their list.
- Schedule configuration: `title`, `playlist_id`, five-field `cron`, IANA
  `timezone` (default UTC), `limit` (1–100, default 100), `enabled` (default true).
  At most 100 schedules per device. Public playlist snapshots use `auto_playlist`
  and the ordinary ledger submission path, auth ownership, rate/queue limits and
  source deduplication. Responses also expose `next_run`, `run_id`, `last_run`,
  `last_intake_id`, and sanitized `last_error`; times are Unix seconds.
  The scheduler runs only with `YUBAL_INTAKE_WORKER_ENABLED=true`, under its
  exclusive owner lock, polls every ten seconds, and skips revoked devices.
  A due snapshot and its UUID are persisted before submission. Restart after
  submission but before acknowledgement replays the same intent. Missed intervals
  coalesce into one run; failed runs report an error and await the next cron time
  or an explicit Run now. The legacy upstream scheduler remains unmounted.

Fresh mode exposes `GET /v1/health`, `POST /v1/intakes`, `GET /v1/intakes/{intake_id}`, `GET /v1/intakes` (device history), `GET /v1/tracks/{video_id}`, `POST /v1/tracks/{video_id}/retry`, and `POST /v1/tracks/{video_id}/retry-tagging`. It does not mount legacy `/api` routes or start the upstream scheduler. Device tokens are provisioned and revoked via a local owner command, not an open network endpoint. The server stores token hashes; `Authorization: Bearer <device_token>` is required on intake routes. HTTPS is required before access from other machines. Sanitized examples for the client are in `docs/fixtures/`; the generated OpenAPI remains the schema authority.

`POST /v1/intakes` accepts UUID `request_id` and `device_id`, mode `manual_song`, `manual_queue`, `manual_playlist`, `auto_song`, `auto_queue`, or `auto_playlist`, optional UUID `capture_session_id`, optional nonnegative `queue_revision`, optional `source_context` (`kind`: `song`, `queue`, `playlist`, `radio`; optional `playlist_id`), and 1–100 tracks. `manual_song` and `auto_song` each require exactly one track. Automatic modes are labels supplied by the client; they do not change worker behavior or add payload fields. Each track has an 11-character YouTube `video_id`, contiguous zero-based `position`, and optional 200-character display hints. Unknown fields are rejected. Repeated video IDs preserve order while sharing source identity. Identical request-ID replay returns the same intake; a changed body gives 409. A token/device mismatch gives 403; missing/revoked credentials give 401. The response is 202 with `{ "intake_id": "uuid", "items": [{ "position": 0, "video_id": "...", "status": "pending", "downstream_status": "not_started" }] }`. GET returns the same shape with current source state and returns 404 for another device's intake. **202 means accepted only**, whether or not the opt-in worker is running. Worker-side states currently include `pending`, `downloading`, `downloaded`, `failed`, and `missing_output`. `downloaded` is verified staging audio, not library-ready; the separate downstream state becomes `waiting_for_tagger` on verified completion and `tagged` only after owner-local evidence checks.

`POST /v1/tracks/{video_id}/retry` is device-scoped: the device must have submitted the source video ID. It only resets `failed` to `pending` (202); an unauthorized source gives 404 and a source in any other state gives 409. It does not force-redownload verified or missing output. `POST /v1/tracks/{video_id}/retry-tagging` is also device-scoped and re-opens tagging only: it resets `tagged` to `waiting_for_tagger` and returns the same shape as `GET /v1/tracks/{video_id}`; a source in any other state gives 409. Neither route changes the download state or attempt count. Fresh API errors use `{ "error": "conflict", "message": "..." }` (codes: `unauthorized`, `forbidden`, `not_found`, `conflict`, `rate_limited`, `invalid_request`); malformed 422 requests receive a generic message without echoing their payload. Error schemas are generated in OpenAPI.

`GET /v1/intakes?limit=20&before=<cursor>` returns `{ "items": [<intake responses>], "next_before": null | <integer> }` ordered newest first. `limit` ranges 1–100. `GET /v1/tracks/{video_id}` returns `{ "video_id": "...", "status": "pending", "downstream_status": "not_started", "final_path": null }` for a source the device submitted; another device sees 404. Every intake item carries the same four fields. `status` is one of `pending`, `downloading`, `downloaded`, `failed`, `missing_output`; `downstream_status` is one of `not_started`, `waiting_for_tagger`, `tagged`, `filed`, `missing_output`; `final_path` is a nullable absolute library path and is non-null only when `downstream_status` is `filed`. The two status fields are independent: `downloaded` audio can be `waiting_for_tagger`, `tagged`, `filed`, or `missing_output`, and a `missing_output` downstream state never implies a new download. New intake creation is limited to 20 requests/minute and 500 distinct pending/active sources per device; exact replays bypass the rate/queue checks.

Exact schemas are generated by FastAPI; the request below remains an illustration. `docs/fixtures/openapi-v1.json` is the checked-in export of the fresh-mode schema, generated with `python scripts/export_intake_openapi.py docs/fixtures/openapi-v1.json` under `YUBAL_INTAKE_ONLY=true` and asserted equal to the live app schema in the test suite. A library-index reconciliation endpoint is not implemented.

## Boundary
Operational note: A deliberately requested reset of all download ledger history
retains device credentials and schedule configuration, but removes prior intakes,
attempts and source identity. The old intake IDs and their request-ID replay
history no longer resolve. Clients must use a new request UUID when resubmitting.
This is an owner-local maintenance operation and changes no `/v1` schema.

Frontend observes real playback/queue and applies opt-in capture policy; backend accepts normalized IDs, persists intent and downloads deduplicated music. Google website login, blocker and playback stay on client. Server Yubal access tokens are separate, device-specific and revocable. No browser cookies transferred by default. Backend decides success using verified output, not client hints.

## Example request
POST /v1/intakes:
{"request_id":"uuid","device_id":"paired-device","capture_session_id":"uuid","mode":"manual_queue","queue_revision":1,"source_context":{"kind":"radio","playlist_id":null},"tracks":[{"video_id":"dQw4w9WgXcQ","position":0,"title_hint":"Optional","artist_hint":"Optional"}]}

Validate ID syntax, mode enum, sizes/count/rate and device ownership. Hints never determine paths/identity. Backend derives fixed YouTube URLs, rejects arbitrary destinations (SSRF). request_id is idempotent; conflicting body 409, invalid request 422, auth 401/403, throttled 429. Accepted response returns intake_id and per-track ledger refs. GET /v1/intakes/{id}, GET /v1/tracks/{id}, paginated history, stage-specific retry and separately confirmed force-redownload. Decide exact fields/enums in OpenAPI before coding both clients.

## Credentials

The backend owns credentials. An owner provisions a device locally with
`scripts/intake_devices.py provision`; it returns one UUID and one bearer token.
The backend stores only a hash of the token. The owner transfers that pair to an
app through a secure local setup path; the app sends the UUID in `device_id` and
the token in `Authorization: Bearer ...`. The owner can revoke the UUID with
`scripts/intake_devices.py revoke <device_id>`. There is deliberately no open
network endpoint that issues credentials. Network access beyond loopback requires
HTTPS before any app uses it.

Stages distinguish pending, downloading, downloaded, waiting_for_tagger, tagged, filed, ready, failed/cancelled/missing_output. Client polling/read-back must show accepted vs done honestly. Bounded offline frontend outbox replays UUIDs safely; replay may not create duplicate downloads. Backend errors never interrupt YouTube playback. `ready` (library indexing) is not implemented and clients must not infer it from `filed`.

When fresh mode is enabled, legacy `/api/jobs` and subscriptions are excluded rather than integrated. Do not run both submission systems together. Future gateway can consume this backend but does not own another downloader.
