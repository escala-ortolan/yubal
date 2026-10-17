# External tagger ↔ Yubal ledger handoff

## Deployed manual-arrival extension

`yubal:manual-intake-overlay-20261017` supports owner registration of
stable manually copied MP3/M4A files in the configured intake root:

```bash
python /app/scripts/tagger_handoff.py register-manual --source /music/_intake --paths-json '["/music/_intake/local.mp3"]' --settle 300
```

The owner verifies readable audio and records baseline PCM/bytes, assigns
`manual-<uuid32>`, and stores immutable original provenance in `manual_sources`.
The same durable source ledger holds waiting/tagged/filed state. This is not a
YouTube download: no video ID is guessed, no attempt or transfer is created, and
public submissions still accept only genuine 11-character video IDs. Work items
use `source_id/source_kind`; only YouTube items include `video_id`.

Repeated registration of an active waiting path verifies PCM and reuses its ID;
changed audio or wrong states fail. Reused filenames after successful filing get
new local identities. Equal recordings at different intake paths are not silently
merged. Missing title/artist may be registered for tagging, but owner observation
still requires usable tags and unchanged original PCM before filing.

Registration is bounded to regular non-hidden files inside the configured root,
with symlinks/young/invalid audio rejected. Partial registrations are durable and
idempotent; no tagging starts until the complete batch has waiting identities and
cross-mount bytes match. Source tags/paths remain unchanged during registration.
Whole-folder scan safety means a decode failure holds the folder, not permission
to delete/repair/quarantine files or ignore strict verification.

Migration `e51a9b2c0005` adds provenance only. SQLite VARCHAR lengths are not
enforced; existing text-affinity source key columns need no destructive copy.
Public clients/routes/schema are unchanged. Downgrade refuses to discard manual
records; old-image rollback uses a verified pre-migration DB/media checkpoint,
after reconciliation of any later writes. This migration and Yubal-only recreation
were explicitly approved and completed. Future production migration/recreation
still requires approval. The narrow overlay preserves the deployed downloader/UI.

## Responsibility boundary

The external tagger is a separate folder-scanning process. Yubal does **not**
start it, call its API/CLI, or infer success from a tagger job being queued. The
tagger owns writing metadata tags to audio; Yubal owns download identity, audio
verification, downstream state, and any move into the library.

The tagger should not connect to Yubal's SQLite database, write ledger tables,
or claim that an item is `tagged` or `filed`. There is currently no network
callback/API for tagger completion. The supported communication is an owner-run
Yubal command against the same local ledger and the actual media path.

## State transitions

| Ledger state | Meaning | Who changes it |
| --- | --- | --- |
| `downloaded` / `waiting_for_tagger` | Yubal verified playable audio and its decoded-audio fingerprint; it is waiting for downstream work. | Yubal worker |
| `tagged` | Yubal read back non-empty title and artist tags from the expected audio, and verified that its decoded audio is still the downloaded recording. | `scripts/observe_intake_tags.py` |
| `filed` | Yubal moved the verified, tagged audio (and supported sidecars) to the reviewed final path and verified it there. | `scripts/file_intake_sources.py --apply` or its crash-recovery path |
| `missing_output` | Expected audio is absent or no longer matches the verified recording. This is not a successful download and does not trigger an automatic redownload. | Yubal reconciliation / explicit owner action |

`downloaded` is about verified audio in its recorded location; it does not mean
that the tagger ran or that the file is in the library. `tagged` is not `filed`.
The final path is written only after Yubal's filing checks complete.

## Normal handoff

1. **Select work from Yubal history.** Use the source video ID and display
   metadata from the Yubal dashboard/API. Only process entries whose downstream
   state is `waiting_for_tagger`. The current operator script accepts one source
   ID at a time; do not guess an ID from a filename. Source IDs are identity,
   while title/artist are descriptive metadata.
2. **Operate on Yubal's recorded audio, in place.** The tagger should scan the
   configured intake staging folder and update tags on that file. Preserve the
   audio stream/recording and the file path/name. Metadata-only edits are
   expected. Do not rename, move, replace, transcode, or delete the audio as a
   side effect of tagging. Leave lyrics/artwork sidecars alongside it for the
   filing step. If the tagger cannot guarantee an in-place metadata-only update,
   stop and use a reviewed/manual process rather than silently replacing audio.
3. **Read tags back through Yubal.** With the deployment's normal Yubal
   environment (so the command points to the same local SQLite DB and can see
   the same audio mount), run:

   ```sh
   python scripts/observe_intake_tags.py <video_id>
   ```

   For the isolated development environment, see `docs/DEVELOPMENT.md` for the
   Python path and required `YUBAL_*` variables. The command applies migrations,
   reads Yubal's recorded audio path, re-decodes it and compares its PCM
   fingerprint with the download, then checks readable title and artist tags.
   Artist is taken from `albumartist`, falling back to `artist`. It prints the
   resulting state (`tagged`) on success. A missing path, changed audio, absent
   tags, or wrong state is an error; fix the cause and rerun, do not edit the DB.
4. **Review the proposed library destination.** After successful tag
   observation, preview the move:

   ```sh
   python scripts/preview_intake_filing.py <video_id> <music_root>
   ```

   The preview is read-only. Review the Main Artist/Title path, sidecars, and
   collision warnings. Do not use this workflow to overwrite or bulk-edit
   existing library files.
5. **File only after review/approval.** To let Yubal perform the protected move
   and ledger update for this one source:

   ```sh
   python scripts/file_intake_sources.py <video_id> <music_root> --apply
   ```

   Yubal records a filing plan before moving, refuses collisions rather than
   overwriting, verifies the decoded audio after the move, and then sets
   `downstream_status=filed` and the final path. An interrupted operation is
   recovered at startup or by the owner-only `--reconcile` command. `--reconcile`
   does not transfer audio; it finishes/reports previously planned filings.

## Errors, retries, and recovery

- **Tags are absent/wrong:** The read-back command fails and leaves the source
  unfiled. Correct tags on the same audio, then rerun it. `retry-tagging` only
  reopens a tagged downstream stage; it does not redownload audio.
- **PCM fingerprint changed:** Treat this as a replaced/different recording,
  not a tagger success. Do not force it into the ledger. Restore the verified
  original or follow an explicitly reviewed new-source/download workflow.
- **Recorded path is missing:** Do not substitute a similarly named library
  file. Yubal must verify the same decoded recording before it can accept the
  file. Run the documented reconciliation/read-back workflow and investigate
  the missing source. A title/artist match, filename match, duration, or larger
  file size is not proof of audio identity.
- **Filing was interrupted:** Use `python scripts/file_intake_sources.py
  --reconcile` with the normal deployment environment. Do not manually repair
  ledger rows or move the remaining files around the pending plan.
- **The library already contains a possible match:** Keep it untouched. Compare
  recordings through an explicitly reviewed process; this handoff does not
  import/adopt existing files into Yubal's download-once ledger.

Never mark a source `tagged` or `filed` based only on tagger logs, a successful
scan, a filename, or file existence. Never directly update SQLite to clear a
missing state or to set a final path. The verification commands are the ledger
write boundary.

## Operational constraints

- The ledger SQLite database must remain on local disk, not NFS. The tagger may
  read/write the audio on the configured audio mount; it must not open SQLite
  itself.
- Run owner scripts with the same configuration, database, and mount paths as
  the live Yubal service. A command pointed at a scratch DB verifies only that
  scratch DB.
- Tagging failures are downstream failures. Do not resubmit or force-redownload
  merely to retry tagging; successful audio remains the download-once record.
- Current SongKong CLI integration is intentionally off. Manual UI tagging or a
  separate approved tagger job remains the tagging mechanism.

## Deployed owner-command extension — 2026-10-16

The user subsequently authorized exact-path quality replacement. The opt-in
`scripts/tagger_handoff.py` implements that policy **inside Yubal**, not in the
tagger. It is deployed with owner approval. The legacy
commands above keep their no-overwrite behavior; there is no new HTTP callback,
API schema change, or database migration.

The command emits JSON and supports:

```sh
python scripts/tagger_handoff.py capabilities
python scripts/tagger_handoff.py work --source /music/_intake
python scripts/tagger_handoff.py verify VIDEO_ID
python scripts/tagger_handoff.py observe VIDEO_ID
python scripts/tagger_handoff.py preview VIDEO_ID /music
python scripts/tagger_handoff.py file VIDEO_ID /music --expected-token TOKEN --allow-quality-replace
python scripts/tagger_handoff.py reconcile
```

`work` exports only recorded source IDs in `downloaded/waiting_for_tagger` under
the requested staging root. `verify` checks the downloaded PCM before tagging.
`verify`/`observe` also return the current full-file `audio_sha256`; the tagger must
compare this with its own pre/post-tag bytes, so matching path strings across
containers cannot conceal different media mounts. Capabilities advertise
`tagger_byte_identity=true` for this protocol.
`observe` performs the existing tag/PCM read-back. `preview` proposes accent-free
sentence-case `Main artist/All artists - Title.ext`, without requiring an online
metadata match. It returns a review token binding the source, destination,
sidecars and existing file evidence. Both preview/file accept `--duos-json` with
the same canonical duo list used by the external tagger.

The deployed `yubal:intake-credit-separators-v2-20261016` parser splits semicolons
and whitespace-surrounded `/` within combined/native multi-value artist credits.
Bare `AC/DC` slashes, ampersands, and commas remain inside entity names. MP4, ID3,
ASF and Vorbis artist fields are supported. A whole album-artist entity can rejoin
an exact leading member set (even in reverse order), preserving groups such as
Matogrosso & Mathias instead of filing under an individual. Parser reads do not
write metadata; unknown duo identities still need review.

Only an **exact pathname including extension** can use quality replacement.
Case/accent twins and sidecar conflicts remain review items. Lossless is preferred
over lossy; otherwise like-codec quality is compared by audio-stream properties,
not file size. Ties retain the existing library file; incomparable encodings or
mix/channel differences require review. If the existing file wins, the incoming
source remains staged and `tagged`, never falsely marked `filed` or adopted from
another recording.

When the arrival wins, Yubal stores a local receipt beside its DB, copies the old
audio to `tagger-replacements/VIDEO_ID/`, fsyncs and verifies the full backup before
removing the old library pathname, and then uses its verified no-clobber filer for
the arrival. Both recordings survive. The ledger records the final path only after
PCM verification. Displaced ledger entries with different PCM become
`missing_output`; they are not silently reassigned to the new recording.

An interruption after archival can temporarily leave the library pathname empty.
Repeat the same owner `file` command with its original token to resume; do not
retag/redownload or manufacture another token. `reconcile` retains its existing
non-transfer behavior and does not restore/move archived audio. An uncertain
receipt or leftover private receipt temporary file requires inspection. Backups
and receipts are never automatically deleted.

Files lacking readable artist/title stay in `_intake` for review: this handoff
cannot truthfully mark them tagged or move them to `_unknown`. Untracked manual
arrivals likewise need an explicit owner workflow; never guess a source ID.
