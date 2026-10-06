"""Bounded public playlist metadata lookup; never submits or downloads audio."""

import re
from typing import Any

import requests
from ytmusicapi import YTMusic


class _TimeoutSession(requests.Session):
    def send(
        self, request: requests.PreparedRequest, **kwargs: Any
    ) -> requests.Response:
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = (5, 20)
        return super().send(request, **kwargs)


def preview_playlist(playlist_id: str, limit: int) -> dict[str, object]:
    with _TimeoutSession() as session:
        playlist = YTMusic(requests_session=session).get_playlist(
            playlist_id, limit=limit
        )
    tracks = []
    for track in playlist.get("tracks", [])[:limit]:
        video_id = track.get("videoId")
        if not isinstance(video_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{11}", video_id
        ):
            continue
        tracks.append(
            {
                "video_id": video_id,
                "position": len(tracks),
                "title_hint": str(track.get("title") or "")[:200],
                "artist_hint": ", ".join(
                    str(a.get("name") or "") for a in track.get("artists", [])
                )[:200],
            }
        )
    if not tracks:
        raise ValueError("No playable tracks")
    return {
        "playlist_id": playlist_id,
        "title": str(playlist.get("title") or playlist_id)[:200],
        "tracks": tracks,
    }
