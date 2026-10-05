# Isolated development commands

To actually start the server and drive it with curl, see `docs/RUNNING.md`. This
file covers the build/test environment itself.

The source checkout is on `/srv/dev` (NFS); `/tmp/opencode` is local ext4.
No SQLite DB or active staging audio should live under this checkout.

The host lacked `uv`, Bun, `just`, Python 3.12 and ffmpeg at discovery. An
isolated `uv==0.9.23` was installed at `/tmp/opencode/yubal-tools`, Python
3.12.12 at `/tmp/opencode/yubal-python`, and the frozen-lockfile environment at
`/tmp/opencode/yubal-venv`. These are development paths, not a deployment.

```sh
export YUBAL_ROOT=/srv/dev/repos/yubal
export YUBAL_CONFIG=/tmp/opencode/yubal-config-test
export YUBAL_DATA=/tmp/opencode/yubal-staging-test
export YUBAL_SCHEDULER_ENABLED=false
export YUBAL_AUDIO_FORMAT=m4a
export YUBAL_INTAKE_ONLY=true
export YUBAL_INTAKE_WORKER_ENABLED=false
export YUBAL_INTAKE_STAGING=/tmp/opencode/yubal-intake-staging
export UV_PYTHON_INSTALL_DIR=/tmp/opencode/yubal-python
export UV_PROJECT_ENVIRONMENT=/tmp/opencode/yubal-venv
export UV_CACHE_DIR=/tmp/opencode/yubal-cache
/tmp/opencode/yubal-tools/bin/uv sync --frozen --all-packages --python 3.12
/tmp/opencode/yubal-venv/bin/pytest packages/api/tests packages/yubal/tests -q
/tmp/opencode/yubal-venv/bin/ruff check packages/api/src packages/api/tests
/tmp/opencode/yubal-venv/bin/ty check --python /tmp/opencode/yubal-venv packages/api/src
```

No backend or frontend server was started. Do not use the upstream default
`YUBAL_CONFIG` here: it would put SQLite on NFS. Fresh mode checks the mount and
refuses to start on NFS. For a local owner-provisioned test device, after setting
the variables above, run
`/tmp/opencode/yubal-venv/bin/python scripts/intake_devices.py provision`.
It prints a revocable device token once; protect it and never paste it into logs.
`scripts/intake_devices.py revoke <device_id>` revokes that device.

The fresh API accepts intent. To exercise automatic downloads in isolation,
explicitly set `YUBAL_INTAKE_WORKER_ENABLED=true` and the staging path above;
the worker uses pinned upstream yt-dlp and requires `ffprobe`/`ffmpeg` in PATH.
It holds an exclusive local lock, serially processes pending sources and verifies
real m4a decode/checksum before marking `downloaded`. It does not call external tagger,
file into Music, or serve audio. Failed downloads require explicit device-scoped
retry; missing completed audio never auto-redownloads. Keep the API on an
isolated loopback test port; use HTTPS before remote device access.

Regenerate the client schema fixture after any response-model or route change:
`/tmp/opencode/yubal-venv/bin/python scripts/export_intake_openapi.py docs/fixtures/openapi-v1.json`
under `YUBAL_INTAKE_ONLY=true`. The test suite asserts the checked-in file equals
the generated fresh-mode schema.

After a separate external tagger folder scan or manual UI tagging, an owner can run
`/tmp/opencode/yubal-venv/bin/python scripts/observe_intake_tags.py <video_id>`
locally. It checks artist/title tags and the decoded-audio fingerprint against
the verified download before recording `tagged`. It does not invoke external tagger or
move audio. Metadata-only edits keep the download complete; replacing the audio
with a different recording becomes `missing_output` upon reconciliation.

After `tagged`, run
`/tmp/opencode/yubal-venv/bin/python scripts/preview_intake_filing.py <video_id> /path/to/scratch/Music`.
It prints a
**read-only** Main Artist/Title and sidecar proposal, plus case-insensitive
collisions. This command never creates folders or moves files. Existing library
edits and final filing require a separate reviewed approval.

`scripts/run_intake_api.sh` starts the fresh-mode server on loopback port 8011
with the worker enabled, using an isolated `/tmp/opencode/yubal-run` tree.
`docs/RUNNING.md` is the operator-facing guide; this is only the environment
those paths assume.

`scripts/file_intake_sources.py <video_id> /path/to/scratch/Music` is the filing
command, and is also read-only without `--apply`. With `--apply` it moves the
verified audio and its `.lrc`/`.jpg`/`.png`/`.webp` sidecars into the named
music root, re-verifies the decoded audio after the move, and records `filed`
with the final path. It refuses any collision, including a folder or filename
that differs only by case, and never overwrites. The destination is recorded in
the ledger **before** the first move, so an interrupted filing is either
completed on the next run or on API startup (`reconcile_filing`) rather than
repeated; audio that moved but whose ledger write was lost finishes the sidecar
moves and records the final path without moving audio again. A filed file that
disappears or is replaced by a different recording becomes downstream
`missing_output` and never triggers a download. `--reconcile` runs only the
recovery pass. Filing still writes into a real music root, so it needs the same
reviewed approval as any library edit.

Run `/tmp/opencode/yubal-venv/bin/python scripts/backup_intake_db.py /tmp/opencode/new-backup.db`
for a consistent local SQLite backup. Its destination must not exist. The
scratch restore test copies that backup to a **new** DB path; it does not
overwrite a running database.

`Dockerfile.intake` builds an isolated amd64 backend image from digest-pinned
Python/uv bases, frozen `uv.lock`, and a SHA-256-checked ffmpeg/ffprobe archive.
Its build checks generated `/v1` OpenAPI and both media binaries. The runtime
user is uid1000/gid2000. An eventual container needs a writable local-disk
`/state` and separate `/staging`; enabling the worker is explicit. No runtime
container was started for this build. Debian apt packages are not
snapshot-pinned yet.

For the local test suite, ffmpeg/ffprobe 7.0.2 static binaries were installed in
`/tmp/opencode/yubal-ffmpeg-bin` from johnvansickle.com's release archive;
prepend that directory to PATH for worker tests. Their network transfer is a
**fake copy of a locally generated, playable AAC m4a**; it is not evidence of a
real YouTube download. Separate real-download results are in `docs/EVIDENCE.md`.
