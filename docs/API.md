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
| `GET /v1/intakes?limit=20&before=<cursor>` | Read device-owned history |
| `GET /v1/tracks/{video_id}` | Read current state for one device-owned source |
| `POST /v1/tracks/{video_id}/retry` | Retry a failed download only |
| `POST /v1/tracks/{video_id}/retry-tagging` | Re-open tagging only; never downloads again |

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
