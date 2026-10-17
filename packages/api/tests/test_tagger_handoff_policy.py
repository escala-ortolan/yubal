"""Isolated real-audio owner-filing tests; no production or downloads."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import test_filing
from mutagen.mp4 import MP4
from yubal_api.db.intake_ledger import IntakeLedger
from yubal_api.services.tag_stage import observe_manual_tags
from yubal_api.services.tagger_handoff import (
    canonical_duo,
    file_reviewed,
    preview_reviewed,
    quality_winner,
    split_artist_values,
    tag_credit,
)

VIDEO_ID = test_filing.VIDEO_ID


@pytest.fixture
def tagged_source(tmp_path: Path) -> tuple[IntakeLedger, Path]:
    return test_filing.tagged_source.__wrapped__(tmp_path)


def test_duo_alias_does_not_rewrite_unrelated_band_names() -> None:
    duos = ["João Carreiro & Capataz"]
    assert canonical_duo("Joao carreiro e capataz", duos) == duos[0]
    assert canonical_duo("João Carreiro and Capataz", duos) == duos[0]
    assert canonical_duo("Florence and the Machine", duos) == "Florence and the Machine"


@pytest.mark.parametrize(
    "values,expected",
    [
        (
            ["Bruno & Marrone / Henrique & Diego"],
            ["Bruno & Marrone", "Henrique & Diego"],
        ),
        (
            ["Bruno e Marrone; LEONARDO ; Cabaré"],
            ["Bruno & Marrone", "LEONARDO", "Cabaré"],
        ),
        (["AC/DC; Earth, Wind & Fire", "AC/DC"], ["AC/DC", "Earth, Wind & Fire"]),
        (
            ["Florence and the Machine", "Joao carreiro e capataz / Guest"],
            ["Florence and the Machine", "João Carreiro & Capataz", "Guest"],
        ),
    ],
)
def test_explicit_credit_separators_preserve_entities(
    values: list[str], expected: list[str]
) -> None:
    assert split_artist_values(values) == expected


@pytest.mark.parametrize(
    "credit", ["Bruno & Marrone / Henrique & Diego", "Bruno & Marrone;Henrique & Diego"]
)
def test_combined_tag_is_not_a_combined_main_artist_folder(
    tagged_source: tuple[IntakeLedger, Path], tmp_path: Path, credit: str
) -> None:
    ledger, source = tagged_source
    audio = MP4(source)
    audio["©ART"] = [credit]
    audio["aART"] = [credit]
    audio["----:com.apple.iTunes:ARTISTS"] = [credit.encode()]
    audio.save()
    assert observe_manual_tags(ledger, VIDEO_ID) == "tagged"
    proposal = preview_reviewed(ledger, VIDEO_ID, tmp_path / "Music")
    assert proposal["destination"].endswith(
        "Bruno & marrone/Bruno & marrone, henrique & diego - Reviewed title.m4a"
    )


def test_first_artist_stays_main_when_registered_duo_is_a_guest(
    tagged_source: tuple[IntakeLedger, Path], tmp_path: Path
) -> None:
    ledger, source = tagged_source
    audio = MP4(source)
    credit = "Gusttavo Lima / Bruno & Marrone"
    audio["©ART"] = [credit]
    audio["aART"] = [credit]
    audio["----:com.apple.iTunes:ARTISTS"] = [credit.encode()]
    audio["©nam"] = ["Medley One / Two"]
    audio.save()
    before = source.read_bytes()
    assert tag_credit(source) == (
        "Gusttavo Lima",
        ["Gusttavo Lima", "Bruno & Marrone"],
        "Medley One / Two",
    )
    proposal = preview_reviewed(ledger, VIDEO_ID, tmp_path / "Music")
    assert proposal["destination"].endswith(
        "Gusttavo lima/Gusttavo lima, bruno & marrone - Medley one - two.m4a"
    )
    assert source.read_bytes() == before


def test_album_group_identity_rejoins_member_credits_without_splitting_the_group(
    tagged_source: tuple[IntakeLedger, Path], tmp_path: Path
) -> None:
    ledger, source = tagged_source
    audio = MP4(source)
    audio["©ART"] = ["Mathias / Matogrosso / Daniel"]
    audio["aART"] = ["Matogrosso & Mathias"]
    audio["----:com.apple.iTunes:ARTISTS"] = [b"Mathias / Matogrosso / Daniel"]
    audio.save()
    proposal = preview_reviewed(ledger, VIDEO_ID, tmp_path / "Music")
    assert proposal["destination"].endswith(
        "Matogrosso & mathias/Matogrosso & mathias, daniel - Reviewed title.m4a"
    )


def test_id3_combined_frames_are_read_as_artist_entities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mocked file loader with real ID3 frames; not an MP3 integration test."""
    from types import SimpleNamespace

    from mutagen.id3 import ID3, TIT2, TPE1, TPE2, TXXX
    from yubal_api.services import tagger_handoff

    tags = ID3()
    tags.add(TPE1(encoding=3, text=["Bruno & Marrone; Leonardo"]))
    tags.add(TPE2(encoding=3, text=["Bruno & Marrone / Leonardo"]))
    tags.add(TXXX(encoding=3, desc="ARTISTS", text=["Bruno & Marrone;Leonardo"]))
    tags.add(TIT2(encoding=3, text=["Boate Azul"]))
    monkeypatch.setattr(
        tagger_handoff, "MutagenFile", lambda *a, **k: SimpleNamespace(tags=tags)
    )
    assert tag_credit(tmp_path / "fixture.mp3") == (
        "Bruno & Marrone",
        ["Bruno & Marrone", "Leonardo"],
        "Boate Azul",
    )


def test_quality_is_codec_aware_and_ties_keep_existing() -> None:
    assert (
        quality_winner(
            {"codec": "aac", "bitrate": 256000}, {"codec": "aac", "bitrate": 128000}
        )
        == "incoming"
    )
    assert (
        quality_winner(
            {"codec": "aac", "bitrate": 128000}, {"codec": "aac", "bitrate": 256000}
        )
        == "existing"
    )
    assert (
        quality_winner(
            {"codec": "aac", "bitrate": 128000}, {"codec": "aac", "bitrate": 128000}
        )
        == "existing"
    )
    assert (
        quality_winner(
            {"codec": "aac", "bitrate": 256000}, {"codec": "mp3", "bitrate": 320000}
        )
        == "review"
    )
    assert (
        quality_winner(
            {"codec": "alac", "sample_rate": 44100, "bits": 16},
            {"codec": "aac", "bitrate": 256000},
        )
        == "incoming"
    )


def test_owner_files_new_layout_without_guessing_source_id(
    tagged_source: tuple[IntakeLedger, Path], tmp_path: Path
) -> None:
    ledger, source = tagged_source
    work = ledger.tagger_work(source.parent)
    assert work == []  # Fixture already observed; only waiting_for_tagger is selected.
    proposal = preview_reviewed(ledger, VIDEO_ID, tmp_path / "Music")
    assert proposal["destination"].endswith(
        "Main artist/Main artist - Reviewed title.m4a"
    )
    filed = file_reviewed(
        ledger, VIDEO_ID, tmp_path / "Music", proposal["token"], allow_replace=True
    )
    assert filed["status"] == "filed"
    assert ledger.final_path(VIDEO_ID) == filed["path"]
    assert not source.exists()


def test_changed_review_token_never_moves_audio(
    tagged_source: tuple[IntakeLedger, Path], tmp_path: Path
) -> None:
    ledger, source = tagged_source
    with pytest.raises(ValueError, match="review"):
        file_reviewed(ledger, VIDEO_ID, tmp_path / "Music", "stale", allow_replace=True)
    assert source.exists()
    assert ledger.downstream_state(VIDEO_ID) == "tagged"


def test_same_exact_path_quality_replacement_keeps_recovery_copy(
    tagged_source: tuple[IntakeLedger, Path], tmp_path: Path
) -> None:
    ledger, source = tagged_source
    root = tmp_path / "Music"
    first = preview_reviewed(ledger, VIDEO_ID, root)
    destination = Path(first["destination"])
    destination.parent.mkdir(parents=True)
    subprocess.run(
        [
            shutil.which("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=400:duration=1",
            "-c:a",
            "aac",
            "-b:a",
            "24k",
            str(destination),
        ],
        check=True,
    )
    original = destination.read_bytes()
    proposal = preview_reviewed(ledger, VIDEO_ID, root)
    assert proposal["quality_decision"] == "incoming"
    filed = file_reviewed(ledger, VIDEO_ID, root, proposal["token"], allow_replace=True)
    assert filed["status"] == "filed"
    assert Path(filed["backup"]).read_bytes() == original
    assert ledger.downstream_state(VIDEO_ID) == "filed"
    assert not source.exists()
    receipt = json.loads(Path(filed["receipt"]).read_text())
    assert receipt["state"] == "complete"


def test_quality_archive_crash_is_resumable_without_losing_either_recording(
    tagged_source: tuple[IntakeLedger, Path], tmp_path: Path
) -> None:
    ledger, source = tagged_source
    root = tmp_path / "Music"
    destination = Path(preview_reviewed(ledger, VIDEO_ID, root)["destination"])
    destination.parent.mkdir(parents=True)
    subprocess.run(
        [
            shutil.which("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=400:duration=1",
            "-c:a",
            "aac",
            "-b:a",
            "24k",
            str(destination),
        ],
        check=True,
    )
    original = destination.read_bytes()
    proposal = preview_reviewed(ledger, VIDEO_ID, root)

    def crash(_path: Path) -> None:
        raise RuntimeError("simulated crash after archive")

    with pytest.raises(RuntimeError, match="simulated crash"):
        file_reviewed(
            ledger,
            VIDEO_ID,
            root,
            proposal["token"],
            allow_replace=True,
            after_archive=crash,
        )
    assert source.exists() and not destination.exists()
    assert ledger.downstream_state(VIDEO_ID) == "tagged"
    result = file_reviewed(
        ledger, VIDEO_ID, root, proposal["token"], allow_replace=True
    )
    assert result["status"] == "filed"
    assert Path(result["backup"]).read_bytes() == original


def test_existing_quality_tie_keeps_both_files_and_does_not_claim_filed(
    tagged_source: tuple[IntakeLedger, Path], tmp_path: Path
) -> None:
    ledger, source = tagged_source
    root = tmp_path / "Music"
    destination = Path(preview_reviewed(ledger, VIDEO_ID, root)["destination"])
    destination.parent.mkdir(parents=True)
    shutil.copyfile(source, destination)
    proposal = preview_reviewed(ledger, VIDEO_ID, root)
    result = file_reviewed(
        ledger, VIDEO_ID, root, proposal["token"], allow_replace=True
    )
    assert result["status"] == "held_quality"
    assert source.is_file() and destination.is_file()
    assert ledger.downstream_state(VIDEO_ID) == "tagged"
    assert ledger.final_path(VIDEO_ID) is None


def test_sidecar_collision_is_not_overwritten(
    tagged_source: tuple[IntakeLedger, Path], tmp_path: Path
) -> None:
    ledger, source = tagged_source
    root = tmp_path / "Music"
    destination = Path(preview_reviewed(ledger, VIDEO_ID, root)["destination"])
    destination.parent.mkdir(parents=True)
    sidecar = destination.with_suffix(".lrc")
    sidecar.write_text("Existing unrelated lyrics")
    proposal = preview_reviewed(ledger, VIDEO_ID, root)
    with pytest.raises(ValueError, match="conflict"):
        file_reviewed(ledger, VIDEO_ID, root, proposal["token"], allow_replace=True)
    assert sidecar.read_text() == "Existing unrelated lyrics"
    assert source.exists()


def test_owner_work_exports_only_waiting_ids_at_recorded_intake_path(
    tagged_source: tuple[IntakeLedger, Path], tmp_path: Path
) -> None:
    ledger, source = tagged_source
    assert ledger.retry_tagging(VIDEO_ID)
    rows = ledger.tagger_work(source.parent)
    assert [(r["video_id"], r["path"]) for r in rows] == [(VIDEO_ID, str(source))]
    assert ledger.tagger_work(tmp_path / "other") == []


def test_registered_duo_stays_one_entity_even_with_member_credit_tags(
    tagged_source: tuple[IntakeLedger, Path], tmp_path: Path
) -> None:
    ledger, source = tagged_source
    audio = MP4(source)
    audio["©ART"] = ["Joao carreiro e capataz"]
    audio["aART"] = ["Joao carreiro and capataz"]
    audio["----:com.apple.iTunes:ARTISTS"] = [b"Joao Carreiro", b"Capataz"]
    audio.save()
    assert observe_manual_tags(ledger, VIDEO_ID) == "tagged"
    proposal = preview_reviewed(ledger, VIDEO_ID, tmp_path / "Music")
    assert proposal["destination"].endswith(
        "Joao carreiro & capataz/Joao carreiro & capataz - Reviewed title.m4a"
    )


def test_real_process_death_after_archive_recovers_once(
    tagged_source: tuple[IntakeLedger, Path], tmp_path: Path
) -> None:
    ledger, source = tagged_source
    root = tmp_path / "Music"
    destination = Path(preview_reviewed(ledger, VIDEO_ID, root)["destination"])
    destination.parent.mkdir(parents=True)
    subprocess.run(
        [
            shutil.which("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=400:duration=1",
            "-c:a",
            "aac",
            "-b:a",
            "24k",
            str(destination),
        ],
        check=True,
    )
    original = destination.read_bytes()
    proposal = preview_reviewed(ledger, VIDEO_ID, root)
    script = """
import sys,os
from pathlib import Path
from sqlalchemy import create_engine
from yubal_api.db.intake_ledger import IntakeLedger
from yubal_api.services.tagger_handoff import file_reviewed
ledger=IntakeLedger(create_engine('sqlite:///'+sys.argv[1]))
file_reviewed(ledger,sys.argv[2],Path(sys.argv[3]),sys.argv[4],
    allow_replace=True,after_archive=lambda p:os._exit(29))
"""
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(tmp_path / "ledger.db"),
            VIDEO_ID,
            str(root),
            proposal["token"],
        ],
        check=False,
    )
    assert child.returncode == 29
    assert source.exists() and not destination.exists()
    result = file_reviewed(
        ledger, VIDEO_ID, root, proposal["token"], allow_replace=True
    )
    assert result["status"] == "filed"
    assert Path(result["backup"]).read_bytes() == original
    assert ledger.attempt_count(VIDEO_ID) == 1
    again = file_reviewed(ledger, VIDEO_ID, root, proposal["token"], allow_replace=True)
    assert again["path"] == result["path"]
    assert ledger.attempt_count(VIDEO_ID) == 1
