# Deferred recommendation: custom Yubal music intake

Status: tabled; research/design only. Do not implement or deploy until Gordo explicitly requests it. Full desired feature set is being discussed first.

## Recommendation and inspected sources
Fork https://github.com/guillevc/yubal rather than rebuild its downloader. Public master inspected: workspace version 0.10.0; Python >=3.12 download library (yt-dlp/ytmusicapi), FastAPI backend, React/TypeScript UI, browser extension, migration tooling, tests, and multistage Dockerfile. Keep download core largely intact; add a separate library/intake layer to limit upstream merge conflicts.

Source anchors:
- packages/yubal/src/yubal/services/download_service.py
- packages/yubal/src/yubal/models/track.py
- packages/yubal/src/yubal/utils/filename.py
- packages/api/src/yubal_api/services/job_store.py
- packages/api/src/yubal_api/migrations/versions/b36ae7fb398c_initial_schema.py
- Dockerfile and .github/workflows/ci.yaml

## Verified limitations
DownloadService.download_track skips by expected filename existence; it is not persistent successful-download history. Normal matched-track upstream layout is Artist/Year - Album/Track - Title, NOT the flat Artist/Title previously claimed. Metadata correction and filing can invalidate the existence check even with output rooted in the final library. JobStore is in memory and prunes completed jobs; it cannot be used as durable download history. Extraction cache is metadata, not proof of successful downloads.

## Implementation outline for later
1. Add durable source/download records through database migrations, on local disk (not NFS SQLite).
2. Preserve original source_video_id, selected download ID, atv_video_id and omv_video_id relationships. Check source identities before download. Track proven aliases; do not assume all uploads with matching names are the same recording.
3. Record verified completed download separately from downstream stages: pending/downloading/downloaded/waiting-for-tagger/tagged/filed/failed. Only mark success after audio exists and validates. Tagger outages retry tagging, never downloading.
4. Download to isolated staging; identify/tag with external tagger; file into Music/Main Artist/Title with sidecars and collision protections. Existing custom filing script may own moves rather than external tagger's rename task. Update final path in the record.
5. Add explicit retry, force-redownload, status and history UI/API; transactional claims/uniqueness and restart recovery prevent simultaneous duplicate requests and stranded work.
6. Reconcile M3U paths after moves; preserve playlist order and membership independently from physical paths.
7. Existing-library reconciliation must use evidence; do not mark metadata-cache entries as completed downloads. Same recording across unrelated source IDs needs an additional recording-level check (MusicBrainz/fingerprint), not a video-ID ledger alone.
8. Keep API compatibility where practical for rec-engine/browser extension. Build own pinned Docker image, test with separate config/staging and port before cutover. Retain current service/config for rollback; fold in upstream downloader fixes deliberately.

## Required acceptance tests
Download, correct artist/title, move, restart app, resubmit same source: skip without network download. Alias submissions skip when proven same resolved track. Failed/cancelled/partial downloads remain retryable. external tagger failure does not trigger redownload. Concurrent repeated submissions create one download. Force-redownload is explicit. Lyrics/artwork and playlists survive moves. Verify final state independently on storage and through Navidrome API.

## Scope assessment
Persistent history is a contained backend feature; reliable end-to-end intake is a moderate database/worker/API/UI/integration-test project. No measured time estimate or build verification exists yet.

## Desired feature set under discussion (not implemented)
Manual: YouTube link/playlist -> custom Yubal staging -> external tagger identification/tags -> final filing -> Navidrome/AudioMuse/Nextcloud processing.
Listening: select local seed in Symfonium, Radio Mix requests YouTube Music radio via a server app; submit ordered results for deduplicated download; play seed while subsequent items arrive. Critical open design question: how to expose not-yet-downloaded tracks as playable items to an unmodified Subsonic client. Consider a Subsonic gateway with stable virtual track IDs versus a ready-only playlist with client-refresh limitations. Do not assume Navidrome supports virtual files or Symfonium live-refreshes its current queue. No implementation authorized.
