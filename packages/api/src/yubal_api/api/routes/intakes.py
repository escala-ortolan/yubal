"""Fresh device-scoped intake API. Acceptance is not a completed download."""

from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from yubal_api.api.exceptions import ErrorResponse
from yubal_api.db.intake_ledger import (
    DownstreamState,
    IntakeCapacityError,
    IntakeConflict,
    IntakeLedger,
    IntakeRateLimitError,
    SourceState,
)

FRESH_ERRORS: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorResponse} for code in (401, 403, 404, 409, 422, 429)
}
router = APIRouter(prefix="/intakes", tags=["intakes"], responses=FRESH_ERRORS)
tracks_router = APIRouter(prefix="/tracks", tags=["tracks"], responses=FRESH_ERRORS)


class IntakeTrack(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_id: str = Field(pattern=r"^[A-Za-z0-9_-]{11}$")
    position: int = Field(ge=0, le=99)
    title_hint: str | None = Field(default=None, max_length=200)
    artist_hint: str | None = Field(default=None, max_length=200)


class SourceContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["song", "queue", "playlist", "radio"]
    playlist_id: str | None = Field(default=None, max_length=256)


class IntakeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    device_id: UUID
    mode: Literal["manual_song", "manual_queue", "manual_playlist"]
    capture_session_id: UUID | None = None
    queue_revision: int | None = Field(default=None, ge=0)
    source_context: SourceContext | None = None
    tracks: list[IntakeTrack] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def ordered_positions(self) -> "IntakeRequest":
        if [item.position for item in self.tracks] != list(range(len(self.tracks))):
            raise ValueError("Track positions must start at zero and be contiguous")
        if self.mode == "manual_song" and len(self.tracks) != 1:
            raise ValueError("manual_song requires one track")
        return self


class IntakeItemResponse(BaseModel):
    position: int
    video_id: str
    status: SourceState
    downstream_status: DownstreamState
    final_path: str | None = None


class IntakeResponse(BaseModel):
    intake_id: UUID
    items: list[IntakeItemResponse]


class IntakeHistoryResponse(BaseModel):
    items: list[IntakeResponse]
    next_before: int | None


class TrackResponse(BaseModel):
    video_id: str
    status: SourceState
    downstream_status: DownstreamState
    final_path: str | None = None


def _ledger(request: Request) -> IntakeLedger:
    return request.app.state.intake_ledger  # type: ignore[no-any-return]


LedgerDep = Annotated[IntakeLedger, Depends(_ledger)]


def _device(
    ledger: LedgerDep, authorization: Annotated[str | None, Header()] = None
) -> str:
    prefix = "Bearer "
    token = (
        authorization[len(prefix) :]
        if authorization and authorization.startswith(prefix)
        else ""
    )
    device_id = ledger.authenticate(token)
    if device_id is None:
        raise HTTPException(status_code=401, detail="Invalid device token")
    return device_id


DeviceDep = Annotated[str, Depends(_device)]


def _response(ledger: IntakeLedger, intake_id: str) -> IntakeResponse:
    return IntakeResponse(
        intake_id=UUID(intake_id),
        items=[
            IntakeItemResponse.model_validate(item) for item in ledger.items(intake_id)
        ],
    )


@router.post("", status_code=202)
def submit_intake(
    body: IntakeRequest, ledger: LedgerDep, device_id: DeviceDep
) -> IntakeResponse:
    if str(body.device_id) != device_id:
        raise HTTPException(status_code=403, detail="Device ID does not match token")
    try:
        intake_id = ledger.submit(
            str(body.request_id),
            device_id,
            body.mode,
            [item.video_id for item in body.tracks],
            details=body.model_dump(mode="json", exclude={"request_id"}),
        )
    except IntakeConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except IntakeCapacityError as error:
        raise HTTPException(status_code=429, detail=str(error)) from error
    except IntakeRateLimitError as error:
        raise HTTPException(status_code=429, detail=str(error)) from error
    return _response(ledger, intake_id)


@router.get("")
def list_intakes(
    ledger: LedgerDep,
    device_id: DeviceDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    before: Annotated[int | None, Query(ge=1)] = None,
) -> IntakeHistoryResponse:
    ids, next_before = ledger.history(device_id, limit=limit, before=before)
    return IntakeHistoryResponse(
        items=[_response(ledger, intake_id) for intake_id in ids],
        next_before=next_before,
    )


@router.get("/{intake_id}")
def read_intake(
    intake_id: UUID, ledger: LedgerDep, device_id: DeviceDep
) -> IntakeResponse:
    if not ledger.intake_for_device(str(intake_id), device_id):
        raise HTTPException(status_code=404, detail="Intake not found")
    return _response(ledger, str(intake_id))


@tracks_router.get("/{video_id}")
def read_track(
    video_id: Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{11}$")],
    ledger: LedgerDep,
    device_id: DeviceDep,
) -> TrackResponse:
    if not ledger.source_for_device(video_id, device_id):
        raise HTTPException(status_code=404, detail="Source not found")
    state = ledger.state(video_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Source not found")
    return TrackResponse(
        video_id=video_id,
        status=state,
        downstream_status=ledger.downstream_state(video_id),
        final_path=ledger.final_path(video_id),
    )


@tracks_router.post("/{video_id}/retry", status_code=202)
def retry_failed_track(
    video_id: Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{11}$")],
    ledger: LedgerDep,
    device_id: DeviceDep,
) -> dict[str, str]:
    if not ledger.source_for_device(video_id, device_id):
        raise HTTPException(status_code=404, detail="Source not found")
    if not ledger.retry_failed(video_id):
        raise HTTPException(status_code=409, detail="Source is not failed")
    return {"video_id": video_id, "status": "pending"}


@tracks_router.post("/{video_id}/retry-tagging", status_code=202)
def retry_tagging_track(
    video_id: Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{11}$")],
    ledger: LedgerDep,
    device_id: DeviceDep,
) -> TrackResponse:
    """Re-open tagging only; verified audio is never downloaded again."""
    if not ledger.source_for_device(video_id, device_id):
        raise HTTPException(status_code=404, detail="Source not found")
    if not ledger.retry_tagging(video_id):
        raise HTTPException(status_code=409, detail="Source is not tagged")
    return TrackResponse(
        video_id=video_id,
        status=ledger.state(video_id) or "missing_output",
        downstream_status=ledger.downstream_state(video_id),
        final_path=ledger.final_path(video_id),
    )
