# API quick reference

Base URL while running locally: `http://127.0.0.1:8011`.

Dashboard: `http://127.0.0.1:8011/`
Interactive schema: `http://127.0.0.1:8011/docs`
Raw schema: `http://127.0.0.1:8011/openapi.json`
Checked-in schema: `docs/fixtures/openapi-v1.json`

All routes except `/v1/health` require:

```http
Authorization: Bearer <device-token>
```

| Route | Purpose |
| --- | --- |
| `GET /v1/health` | Service health |
| `POST /v1/intakes` | Submit 1–100 video IDs; returns 202 when accepted |
| `GET /v1/intakes/{intake_id}` | Read one intake |
| `GET /v1/intakes/{intake_id}/details` | Ordered request, display hints and current state |
| `POST /v1/intakes/preview-playlist` | Preview up to 100 public playlist entries before submission |
| `GET /v1/intakes?limit=20&before=<cursor>` | Read device-owned history |
| `GET /v1/tracks/{video_id}` | Read current state for one device-owned source |
| `GET /v1/tracks/{video_id}/details` | Download attempts, sanitized errors and audio evidence |
| `PATCH /v1/intakes/{intake_id}/control` | Cancel, resume, or delete from history |
| `POST /v1/tracks/{video_id}/force-redownload` | Explicit new download; preserves old files; UUID body required |
| `DELETE /v1/tracks/{video_id}` | Forget deduplication identity; keeps files; UUID body required |
| `GET /v1/schedules` | Device-owned playlist schedules |
| `POST /v1/schedules` | Create a cron/timezone playlist schedule |
| `PUT /v1/schedules/{id}` | Edit or pause/enable a schedule |
| `DELETE /v1/schedules/{id}` | Delete a schedule |
| `POST /v1/schedules/{id}/run` | Enable and queue an immediate schedule run |
| `POST /v1/tracks/{video_id}/retry` | Retry a failed download only |
| `POST /v1/tracks/{video_id}/retry-tagging` | Re-open tagging only; never downloads again |

Intake modes: `manual_song`, `manual_queue`, `manual_playlist`, `auto_song`,
`auto_queue`, `auto_playlist`. `auto_*` values are capture-source labels only;
the request shape and backend download behavior are unchanged. `manual_song`
and `auto_song` require exactly one track.

## Submit one video

```http
POST /v1/intakes
Content-Type: application/json
Authorization: Bearer <device-token>

{
  "request_id": "b1f0c9d2-4e77-4a1e-9d3a-5c2b8e6f1a04",
  "device_id": "<provisioned-device-uuid>",
  "mode": "manual_song",
  "source_context": {"kind": "song"},
  "tracks": [{"video_id": "jNQXAC9IVRw", "position": 0}]
}
```

Response (202):

```json
{
  "intake_id": "942399c9-1f53-4f8f-b0b3-9efa8f63e689",
  "items": [{
    "position": 0,
    "video_id": "jNQXAC9IVRw",
    "status": "pending",
    "downstream_status": "not_started",
    "final_path": null
  }]
}
```

202 means only that the server saved the request. Poll `GET /v1/tracks/{video_id}`
for the actual state.

## States

`status`: `pending`, `downloading`, `downloaded`, `failed`, `missing_output`.

`downstream_status`: `not_started`, `waiting_for_tagger`, `tagged`, `filed`,
`missing_output`.

`final_path` is null unless the source is `filed`.

## Credentials

Run this on the server host, once per app/device:

```sh
docker exec -it yubal python /app/scripts/intake_devices.py provision
```

It prints the `device_id` and one-time `device_token`. Keep the token private;
the server only keeps its hash. Revoke a lost device:

```sh
docker exec -it yubal python /app/scripts/intake_devices.py revoke <device_id>
```

Complete details and security boundary: `docs/CLIENT-CONTRACT.md`.
