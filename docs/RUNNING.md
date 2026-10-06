# How to run and test this

Everything below happens on **this machine only**, in `/tmp/opencode`. It does
not touch your real music library, your Google account, or any running service.

## Dashboard

Open `http://127.0.0.1:8011/` for the built-in dashboard. It submits a video ID and
shows its current state. It asks for a provisioned device UUID and token in the
browser; the pair remains in that tab's session storage across refreshes and is
cleared by Disconnect or closing the tab. Neither is stored by the dashboard
server. API documentation is at
`http://127.0.0.1:8011/docs`.

## 1. Is it a server?

Yes. One process, plain HTTP, listens on `127.0.0.1:8011`. It downloads and
deduplicates music. Another app (the browser extension) talks to it over that
port with a bearer token.

## 2. Start it

From anywhere:

```sh
/srv/dev/repos/yubal/scripts/run_intake_api.sh
```

It prints the paths it is using and stays in the foreground. Stop with Ctrl+C.

Check it is up:

```sh
curl -s http://127.0.0.1:8011/v1/health
# {"status":"healthy"}
```

## 3. Give it a device token (once per install)

In a second terminal:

```sh
export YUBAL_ROOT=/srv/dev/repos/yubal
export YUBAL_CONFIG=/tmp/opencode/yubal-run/config
export YUBAL_INTAKE_ONLY=true
/tmp/opencode/yubal-venv/bin/python /srv/dev/repos/yubal/scripts/intake_devices.py provision
```

It prints a `device_id` (UUID) and a `device_token`. The extension needs both,
in that pairing screen. Revoke one with
`scripts/intake_devices.py revoke <device_id>`. Never paste a token into a
public channel or a commit.

## 4. Submit a video

```sh
TOKEN=the-token-from-step-3

curl -s -X POST http://127.0.0.1:8011/v1/intakes \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "request_id": "b1f0c9d2-4e77-4a1e-9d3a-5c2b8e6f1a04",
    "device_id": "the-device-id-from-step-3",
    "mode": "manual_song",
    "source_context": {"kind": "song"},
    "tracks": [{"video_id": "jNQXAC9IVRw", "position": 0}]
  }'
```

You get HTTP 202 and the intake. **202 means accepted, not downloaded.**

## 5. Watch it work

```sh
curl -s http://127.0.0.1:8011/v1/tracks/jNQXAC9IVRw -H "Authorization: Bearer $TOKEN"
```

`status` goes `pending` → `downloading` → `downloaded` in a few seconds. The file
lands in `/tmp/opencode/yubal-run/staging/`. Submit the same `video_id` again and
you get a second intake with **no** second download.

## 6. File it into a music folder

Only after someone has written artist/title tags onto the file. Both commands
default to doing nothing.

```sh
# See where it would go, and what would collide.
scripts/file_intake_sources.py jNQXAC9IVRw /tmp/opencode/yubal-run/music

# Actually move it.
scripts/file_intake_sources.py jNQXAC9IVRw /tmp/opencode/yubal-run/music --apply
```

It never overwrites anything, refuses names that differ only by capitalisation,
and re-checks the audio after moving. `downstream_status` becomes `filed` and
`final_path` is populated.

If that process is killed mid-move, restart the server: it reconciles the file
and the database on startup and finishes the job instead of moving it twice.

## 7. Automated tests

Fast, no network:

```sh
export PATH=/tmp/opencode/yubal-ffmpeg-bin:$PATH
export YUBAL_ROOT=/srv/dev/repos/yubal
export YUBAL_CONFIG=/tmp/opencode/yubal-config-test
export YUBAL_DATA=/tmp/opencode/yubal-staging-test
export YUBAL_SCHEDULER_ENABLED=false

/tmp/opencode/yubal-venv/bin/pytest /srv/dev/repos/yubal/packages/api/tests /srv/dev/repos/yubal/packages/yubal/tests -q
```

## Where things are

The packaged React UI includes detailed history, ordered playlist previews,
scheduled playlist captures, cancel/resume/delete-from-history, and explicit
force-redownload/forget-song controls. See `docs/UI-REVIEW.md` for the build and
`docs/CLIENT-CONTRACT.md` for action semantics and migration/rollback requirements.

## Flat staging files and owner-only history reset

New verified downloads are stored directly in `YUBAL_INTAKE_STAGING` as
`Artist - Title.m4a`, with the uploader used when the artist is unavailable.
Names come from server-side yt-dlp metadata, not client hints. When a name
already exists, Yubal adds the video ID (and, if needed, a short attempt ID)
rather than overwriting another recording. Interrupted transfers may briefly
leave a hidden `.yubal-inflight-*` file in the same staging root.

An owner can inspect reset counts with `python scripts/reset_intake_history.py`
on the server. A reset requires stopping only the `yubal` service and running
`python scripts/reset_intake_history.py --apply --backup /state/yubal/<unique>.db`
inside the image before bringing that service back up. The command creates and
integrity-checks a 0600 SQLite backup and clears intakes, memberships, source
identities, aliases, attempts, downstream state, reset receipts and rate events.
It preserves device credentials, schedule configurations and *all audio files*.
Run it only when you intentionally want previously downloaded IDs to be
downloadable again; existing old nested audio files are not migrated or removed.

| Thing | Path |
| --- | --- |
| This guide | `docs/RUNNING.md` |
| API contract for client developers | `docs/CLIENT-CONTRACT.md` |
| Machine-readable schema | `docs/fixtures/openapi-v1.json` |
| Request/response examples | `docs/fixtures/intake-request.json`, `intake-accepted.json` |
| What was actually tested | `docs/EVIDENCE.md` |
| Isolated build/dev environment setup | `docs/DEVELOPMENT.md` |

## What is not built yet

- Tagging. Something else has to write artist/title onto the file first.
- Library indexing. `filed` is the last state the server knows about.
- Built-in TLS. The local development server uses HTTP; the deployed service
  uses the existing HTTPS reverse proxy.
