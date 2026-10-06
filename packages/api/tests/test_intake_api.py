"""Fresh intake API: authentication, replay and no legacy submission paths."""

import json
import shutil
import subprocess
from collections.abc import Generator
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Starlette's in-process event loop needs a local socketpair; no HTTP server is used.
pytestmark = pytest.mark.enable_socket


@pytest.fixture
def intake_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[tuple[FastAPI, TestClient], None, None]:
    from yubal_api.settings import get_settings

    monkeypatch.setenv("YUBAL_ROOT", str(tmp_path))
    monkeypatch.setenv("YUBAL_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("YUBAL_DATA", str(tmp_path / "staging"))
    monkeypatch.setenv("YUBAL_INTAKE_ONLY", "true")
    get_settings.cache_clear()
    from yubal_api.api.app import create_app

    app = create_app()
    try:
        with TestClient(app) as client:
            yield app, client
    finally:
        get_settings.cache_clear()


def test_fresh_mode_omits_legacy_and_requires_device_token(
    intake_app: tuple[FastAPI, TestClient],
) -> None:
    app, client = intake_app
    assert "/api/jobs" not in app.openapi()["paths"]
    assert "/api/subscriptions" not in app.openapi()["paths"]
    assert client.get("/v1/health").json() == {"status": "healthy"}
    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert "Yubal intake" in dashboard.text
    assert "/v1/intakes" in dashboard.text
    assert (
        client.post("/api/jobs", json={"url": "https://example.com"}).status_code == 404
    )
    assert client.post("/v1/intakes", json={}).status_code == 401


def test_react_assets_use_fresh_runtime_and_do_not_mask_api_errors(
    intake_app: tuple[FastAPI, TestClient],
) -> None:
    from yubal_api.api.app import create_app
    from yubal_api.settings import get_settings

    build = get_settings().root / "web" / "dist"
    build.mkdir(parents=True)
    (build / "index.html").write_text(
        '<html><head><base href="/"></head><body>React shell</body></html>'
    )
    (build / "app.js").write_text("console.log('fixture')")
    client = TestClient(create_app())
    assert 'name="yubal-intake-only" content="true"' in client.get("/").text
    assert (
        client.get("/app.js")
        .headers["content-type"]
        .startswith("application/javascript")
    )
    assert client.get("/api/jobs").status_code == 404
    assert client.get("/v1/nonexistent").status_code == 404
    assert client.get("/assets/missing.js").status_code == 404


def test_submit_replay_conflict_readback_and_revoke(
    intake_app: tuple[FastAPI, TestClient],
) -> None:
    _, client = intake_app
    ledger = client.app.state.intake_ledger
    device_id, token = ledger.provision_device()
    headers = {"Authorization": f"Bearer {token}"}
    payload = {
        "request_id": str(uuid4()),
        "device_id": device_id,
        "mode": "manual_queue",
        "capture_session_id": str(uuid4()),
        "queue_revision": 1,
        "source_context": {"kind": "queue"},
        "tracks": [
            {"video_id": "dQw4w9WgXcQ", "position": 0},
            {"video_id": "dQw4w9WgXcQ", "position": 1},
        ],
    }
    first = client.post("/v1/intakes", json=payload, headers=headers)
    assert first.status_code == 202
    assert first.json()["items"][0]["status"] == "pending"
    assert (
        client.post("/v1/intakes", json=payload, headers=headers).json() == first.json()
    )
    intake_id = first.json()["intake_id"]
    assert (
        client.get(f"/v1/intakes/{intake_id}", headers=headers).json() == first.json()
    )
    assert ledger.source_count() == 1
    changed = {**payload, "queue_revision": 2}
    assert client.post("/v1/intakes", json=changed, headers=headers).status_code == 409
    assert (
        client.post(
            "/v1/intakes", json={**payload, "device_id": str(uuid4())}, headers=headers
        ).status_code
        == 403
    )
    ledger.revoke_device(device_id)
    assert client.get(f"/v1/intakes/{intake_id}", headers=headers).status_code == 401


def test_ownership_and_invalid_video_id(
    intake_app: tuple[FastAPI, TestClient],
) -> None:
    _, client = intake_app
    ledger = client.app.state.intake_ledger
    a, token_a = ledger.provision_device()
    _, token_b = ledger.provision_device()
    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}
    payload = {
        "request_id": str(uuid4()),
        "device_id": a,
        "mode": "manual_song",
        "tracks": [{"video_id": "dQw4w9WgXcQ", "position": 0}],
    }
    intake_id = client.post("/v1/intakes", json=payload, headers=headers_a).json()[
        "intake_id"
    ]
    assert client.get(f"/v1/intakes/{intake_id}", headers=headers_b).status_code == 404
    bad = {
        **payload,
        "request_id": str(uuid4()),
        "tracks": [{"video_id": "http://127.0.0.1", "position": 0}],
    }
    assert client.post("/v1/intakes", json=bad, headers=headers_a).status_code == 422


def test_sqlite_path_rejects_nfs_before_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from yubal_api.db.local_disk import require_local_sqlite

    original = Path.read_text

    def mountinfo(
        path: Path, encoding: str | None = None, errors: str | None = None
    ) -> str:
        if str(path) == "/proc/self/mountinfo":
            return (
                "1 0 8:1 / / rw - ext4 /dev/sda rw\n"
                "2 1 0:1 / /srv/dev rw - nfs4 storage:/dev rw\n"
            )
        return original(path, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", mountinfo)
    require_local_sqlite(Path("/tmp/opencode/test.db"))
    with pytest.raises(RuntimeError, match="local disk"):
        require_local_sqlite(Path("/srv/dev/repos/yubal/test.db"))


def test_retry_tagging_is_device_scoped_and_never_re_downloads(
    intake_app: tuple[FastAPI, TestClient], tmp_path: Path
) -> None:
    _, client = intake_app
    ledger = client.app.state.intake_ledger
    device_id, token = ledger.provision_device()
    _, other_token = ledger.provision_device()
    headers = {"Authorization": f"Bearer {token}"}
    other = {"Authorization": f"Bearer {other_token}"}
    video_id = "dQw4w9WgXcQ"
    client.post(
        "/v1/intakes",
        json={
            "request_id": str(uuid4()),
            "device_id": device_id,
            "mode": "manual_song",
            "tracks": [{"video_id": video_id, "position": 0}],
        },
        headers=headers,
    )
    endpoint = f"/v1/tracks/{video_id}/retry-tagging"
    # Not tagged yet, and not this device's source.
    assert client.post(endpoint, headers=other).status_code == 404
    assert client.post(endpoint, headers=headers).status_code == 409

    # Drive the source to tagged through the real worker and tag observation.
    sample = tmp_path / "sample.m4a"
    subprocess.run(
        [
            shutil.which("ffmpeg") or "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=duration=1",
            "-c:a",
            "aac",
            "-metadata",
            "album_artist=Main Artist",
            "-metadata",
            "title=Reviewed",
            str(sample),
        ],
        check=True,
    )

    def fake_transfer(_video_id: str, output: Path) -> Path:
        result = Path(f"{output}.m4a")
        result.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(sample, result)
        return result

    from yubal_api.services.intake_worker import IntakeWorker
    from yubal_api.services.tag_stage import observe_manual_tags

    staging = tmp_path / "staging"
    assert IntakeWorker(ledger, staging, fake_transfer).run_once()
    assert observe_manual_tags(ledger, video_id) == "tagged"
    attempts = ledger.attempt_count(video_id)

    response = client.post(endpoint, headers=headers)
    assert response.status_code == 202
    assert response.json() == {
        "video_id": video_id,
        "status": "downloaded",
        "downstream_status": "waiting_for_tagger",
        "final_path": None,
    }
    assert client.post(endpoint, headers=headers).status_code == 409
    # The verified audio is untouched; only the downstream stage reopened.
    assert ledger.attempt_count(video_id) == attempts
    assert ledger.state(video_id) == "downloaded"
    assert ledger.tagging_candidate(video_id)[0].is_file()


@pytest.mark.parametrize("mode", ["auto_song", "auto_queue", "auto_playlist"])
def test_auto_intake_modes_are_accepted(
    intake_app: tuple[FastAPI, TestClient], mode: str
) -> None:
    _, client = intake_app
    device_id, token = client.app.state.intake_ledger.provision_device()
    response = client.post(
        "/v1/intakes",
        json={
            "request_id": str(uuid4()),
            "device_id": device_id,
            "mode": mode,
            "source_context": {"kind": "song"},
            "tracks": [{"video_id": "dQw4w9WgXcQ", "position": 0}],
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 202
    assert response.json()["items"][0]["status"] == "pending"


def test_unknown_intake_mode_is_rejected(
    intake_app: tuple[FastAPI, TestClient],
) -> None:
    _, client = intake_app
    device_id, token = client.app.state.intake_ledger.provision_device()
    response = client.post(
        "/v1/intakes",
        json={
            "request_id": str(uuid4()),
            "device_id": device_id,
            "mode": "automatic_whatever",
            "tracks": [{"video_id": "dQw4w9WgXcQ", "position": 0}],
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 422
    assert response.json() == {
        "error": "invalid_request",
        "message": "Invalid request",
    }


def test_playlist_preview_is_authenticated_and_does_not_submit(
    intake_app: tuple[FastAPI, TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mock metadata lookup; no live playlist or download is claimed."""
    _, client = intake_app
    ledger = client.app.state.intake_ledger
    _, token = ledger.provision_device()
    from yubal_api.api.routes import intakes

    monkeypatch.setattr(
        intakes,
        "preview_playlist",
        lambda playlist_id, limit: {
            "playlist_id": playlist_id,
            "title": "Test playlist",
            "tracks": [
                {"video_id": "dQw4w9WgXcQ", "position": 0, "title_hint": "Test"}
            ],
        },
    )
    path = "/v1/intakes/preview-playlist"
    body = {"playlist_id": "PLtest", "limit": 10}
    assert client.post(path, json=body).status_code == 401
    headers = {"Authorization": f"Bearer {token}"}
    result = client.post(path, json=body, headers=headers)
    assert result.status_code == 200
    assert result.json()["title"] == "Test playlist"
    assert ledger.next_pending() is None
    ledger.MAX_NEW_INTAKES_PER_MINUTE = 1
    assert client.post(path, json=body, headers=headers).status_code == 429
    assert (
        client.post(
            path, json={**body, "playlist_id": "https://evil.test"}, headers=headers
        ).status_code
        == 422
    )
    assert (
        client.post(path, json={**body, "limit": 101}, headers=headers).status_code
        == 422
    )


def test_dashboard_details_preserve_order_and_are_device_scoped(
    intake_app: tuple[FastAPI, TestClient],
) -> None:
    _, client = intake_app
    ledger = client.app.state.intake_ledger
    device, token = ledger.provision_device()
    _, other_token = ledger.provision_device()
    headers = {"Authorization": f"Bearer {token}"}
    body = {
        "request_id": str(uuid4()),
        "device_id": device,
        "mode": "manual_playlist",
        "source_context": {"kind": "playlist"},
        "tracks": [
            {"video_id": "dQw4w9WgXcQ", "position": 0, "title_hint": "First"},
            {"video_id": "dQw4w9WgXcQ", "position": 1, "title_hint": "Repeat"},
        ],
    }
    intake = client.post("/v1/intakes", json=body, headers=headers).json()["intake_id"]
    claim = ledger.claim("dQw4w9WgXcQ")
    ledger.fail(claim, "Download/verification failed: ValueError")
    detail = client.get(f"/v1/intakes/{intake}/details", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["request"]["mode"] == "manual_playlist"
    assert [t["title_hint"] for t in detail.json()["request"]["tracks"]] == [
        "First",
        "Repeat",
    ]
    track = client.get("/v1/tracks/dQw4w9WgXcQ/details", headers=headers)
    assert track.status_code == 200
    assert track.json()["attempts"][0]["status"] == "failed"
    assert (
        track.json()["attempts"][0]["error"]
        == "Download/verification failed: ValueError"
    )
    for path in [f"/v1/intakes/{intake}/details", "/v1/tracks/dQw4w9WgXcQ/details"]:
        assert client.get(path).status_code == 401
        assert (
            client.get(
                path, headers={"Authorization": f"Bearer {other_token}"}
            ).status_code
            == 404
        )


def test_schedule_and_job_controls_are_authenticated_and_device_scoped(
    intake_app: tuple[FastAPI, TestClient],
) -> None:
    _, client = intake_app
    ledger = client.app.state.intake_ledger
    device, token = ledger.provision_device()
    _, other_token = ledger.provision_device()
    headers = {"Authorization": f"Bearer {token}"}
    other = {"Authorization": f"Bearer {other_token}"}
    spec = {"title": "Daily playlist", "playlist_id": "PLtest", "cron": "0 6 * * *"}
    assert client.post("/v1/schedules", json=spec).status_code == 401
    created = client.post("/v1/schedules", json=spec, headers=headers)
    assert created.status_code == 201
    schedule_id = created.json()["id"]
    assert client.get("/v1/schedules", headers=other).json() == []
    assert (
        client.delete(f"/v1/schedules/{schedule_id}", headers=other).status_code == 404
    )
    assert (
        client.post(f"/v1/schedules/{schedule_id}/run", headers=headers).status_code
        == 202
    )
    assert (
        client.put(
            f"/v1/schedules/{schedule_id}",
            json={**spec, "enabled": False},
            headers=headers,
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/v1/schedules", json={**spec, "cron": "invalid"}, headers=headers
        ).status_code
        == 422
    )
    assert (
        client.delete(f"/v1/schedules/{schedule_id}", headers=headers).status_code
        == 200
    )
    intake = ledger.submit(str(uuid4()), device, "manual_song", ["dQw4w9WgXcQ"])
    endpoint = f"/v1/intakes/{intake}/control"
    assert (
        client.patch(endpoint, json={"state": "cancelled"}, headers=other).status_code
        == 404
    )
    assert (
        client.patch(endpoint, json={"state": "cancelled"}, headers=headers).status_code
        == 200
    )
    assert ledger.next_pending() is None
    assert (
        client.get(f"/v1/intakes/{intake}/details", headers=headers).json()["state"]
        == "cancelled"
    )
    action = {"request_id": str(uuid4())}
    assert (
        client.request(
            "DELETE", "/v1/tracks/dQw4w9WgXcQ", json=action, headers=other
        ).status_code
        == 404
    )
    assert (
        client.request(
            "DELETE", "/v1/tracks/dQw4w9WgXcQ", json=action, headers=headers
        ).status_code
        == 200
    )
    assert (
        client.request(
            "DELETE", "/v1/tracks/dQw4w9WgXcQ", json=action, headers=headers
        ).status_code
        == 200
    )


def test_retry_is_explicit_and_device_scoped(
    intake_app: tuple[FastAPI, TestClient],
) -> None:
    _, client = intake_app
    ledger = client.app.state.intake_ledger
    device_id, token = ledger.provision_device()
    _, other_token = ledger.provision_device()
    headers = {"Authorization": f"Bearer {token}"}
    other = {"Authorization": f"Bearer {other_token}"}
    video_id = "dQw4w9WgXcQ"
    payload = {
        "request_id": str(uuid4()),
        "device_id": device_id,
        "mode": "manual_song",
        "tracks": [{"video_id": video_id, "position": 0}],
    }
    intake_id = client.post("/v1/intakes", json=payload, headers=headers).json()[
        "intake_id"
    ]
    claim = ledger.claim(video_id)
    assert claim is not None
    ledger.fail(claim, "Invalid media")
    assert (
        client.get(f"/v1/intakes/{intake_id}", headers=headers).json()["items"][0][
            "status"
        ]
        == "failed"
    )
    endpoint = f"/v1/tracks/{video_id}/retry"
    assert client.post(endpoint, headers=other).status_code == 404
    assert client.post(endpoint, headers=headers).status_code == 202
    assert client.post(endpoint, headers=headers).status_code == 409
    assert (
        client.get(f"/v1/intakes/{intake_id}", headers=headers).json()["items"][0][
            "status"
        ]
        == "pending"
    )


def test_paginated_history_and_track_status_are_device_scoped(
    intake_app: tuple[FastAPI, TestClient],
) -> None:
    _, client = intake_app
    ledger = client.app.state.intake_ledger
    device_id, token = ledger.provision_device()
    _, other_token = ledger.provision_device()
    headers = {"Authorization": f"Bearer {token}"}
    other = {"Authorization": f"Bearer {other_token}"}
    video_id = "dQw4w9WgXcQ"
    created = []
    for _ in range(3):
        body = {
            "request_id": str(uuid4()),
            "device_id": device_id,
            "mode": "manual_song",
            "tracks": [{"video_id": video_id, "position": 0}],
        }
        created.append(
            client.post("/v1/intakes", json=body, headers=headers).json()["intake_id"]
        )
    first = client.get("/v1/intakes?limit=2", headers=headers).json()
    assert [item["intake_id"] for item in first["items"]] == created[2:0:-1]
    assert first["next_before"] is not None
    second = client.get(
        f"/v1/intakes?limit=2&before={first['next_before']}", headers=headers
    ).json()
    assert [item["intake_id"] for item in second["items"]] == [created[0]]
    assert second["next_before"] is None
    assert client.get("/v1/intakes", headers=other).json()["items"] == []
    assert client.get(f"/v1/tracks/{video_id}", headers=other).status_code == 404
    track = client.get(f"/v1/tracks/{video_id}", headers=headers)
    assert track.json() == {
        "video_id": video_id,
        "status": "pending",
        "downstream_status": "not_started",
        "final_path": None,
    }


def test_client_request_and_accepted_fixtures_match_generated_api(
    intake_app: tuple[FastAPI, TestClient],
) -> None:
    app, client = intake_app
    fixtures = Path(__file__).parents[3] / "docs/fixtures"
    request = json.loads((fixtures / "intake-request.json").read_text())
    accepted = json.loads((fixtures / "intake-accepted.json").read_text())
    device_id, token = client.app.state.intake_ledger.provision_device()
    request["device_id"] = device_id
    response = client.post(
        "/v1/intakes", json=request, headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 202
    assert response.json()["items"] == accepted["items"]
    assert "/v1/intakes" in app.openapi()["paths"]
    assert "/v1/tracks/{video_id}/retry" in app.openapi()["paths"]
    assert "/v1/tracks/{video_id}/retry-tagging" in app.openapi()["paths"]


def test_checked_in_client_schema_matches_fresh_generated_openapi(
    intake_app: tuple[FastAPI, TestClient],
) -> None:
    app, _ = intake_app
    schema = json.loads(
        (Path(__file__).parents[3] / "docs/fixtures/openapi-v1.json").read_text()
    )
    assert schema == app.openapi()
    assert not any(path.startswith("/api/") for path in schema["paths"])
    properties = schema["components"]["schemas"]["IntakeItemResponse"]["properties"]
    assert set(properties["status"]["enum"]) == {
        "pending",
        "downloading",
        "downloaded",
        "failed",
        "missing_output",
    }
    assert set(properties["downstream_status"]["enum"]) == {
        "not_started",
        "waiting_for_tagger",
        "tagged",
        "filed",
        "missing_output",
    }
    intake_mode = schema["components"]["schemas"]["IntakeRequest"]["properties"]["mode"]
    assert set(intake_mode["enum"]) == {
        "manual_song",
        "manual_queue",
        "manual_playlist",
        "auto_song",
        "auto_queue",
        "auto_playlist",
    }
    final_path = properties["final_path"]
    assert {option["type"] for option in final_path["anyOf"]} == {"string", "null"}


def test_fresh_mode_errors_have_stable_secret_free_shapes(
    intake_app: tuple[FastAPI, TestClient],
) -> None:
    _, client = intake_app
    assert client.post("/v1/intakes", json={}).json() == {
        "error": "unauthorized",
        "message": "Invalid device token",
    }
    device_id, token = client.app.state.intake_ledger.provision_device()
    headers = {"Authorization": f"Bearer {token}"}
    invalid = client.post("/v1/intakes", json={"device_id": device_id}, headers=headers)
    assert invalid.status_code == 422
    assert invalid.json() == {"error": "invalid_request", "message": "Invalid request"}
    payload = {
        "request_id": str(uuid4()),
        "device_id": device_id,
        "mode": "manual_song",
        "tracks": [{"video_id": "dQw4w9WgXcQ", "position": 0}],
    }
    assert client.post("/v1/intakes", json=payload, headers=headers).status_code == 202
    changed = {**payload, "tracks": [{"video_id": "aBcdEf123_0", "position": 0}]}
    conflict = client.post("/v1/intakes", json=changed, headers=headers)
    assert conflict.status_code == 409
    assert conflict.json()["error"] == "conflict"
    assert token not in conflict.json()["message"]
