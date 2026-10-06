"""Fresh device-scoped intake API. Acceptance is not a completed download."""

from typing import Annotated, Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from croniter import croniter
from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from yubal_api.api.exceptions import ErrorResponse
from yubal_api.db.intake_controls import IntakeControls
from yubal_api.db.intake_ledger import (
    DownstreamState,
    IntakeCapacityError,
    IntakeConflict,
    IntakeLedger,
    IntakeRateLimitError,
    SourceState,
)
from yubal_api.services.intake_preview import preview_playlist

FRESH_ERRORS: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorResponse} for code in (401, 403, 404, 409, 422, 429)
}
router = APIRouter(prefix="/intakes", tags=["intakes"], responses=FRESH_ERRORS)
tracks_router = APIRouter(prefix="/tracks", tags=["tracks"], responses=FRESH_ERRORS)
schedules_router = APIRouter(
    prefix="/schedules", tags=["schedules"], responses=FRESH_ERRORS
)


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
    mode: Literal[
        "manual_song",
        "manual_queue",
        "manual_playlist",
        "auto_song",
        "auto_queue",
        "auto_playlist",
    ]
    capture_session_id: UUID | None = None
    queue_revision: int | None = Field(default=None, ge=0)
    source_context: SourceContext | None = None
    tracks: list[IntakeTrack] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def ordered_positions(self) -> "IntakeRequest":
        if [item.position for item in self.tracks] != list(range(len(self.tracks))):
            raise ValueError("Track positions must start at zero and be contiguous")
        if self.mode in {"manual_song", "auto_song"} and len(self.tracks) != 1:
            raise ValueError(f"{self.mode} requires one track")
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


class IntakeDetailsResponse(IntakeResponse):
    request: IntakeRequest
    state: Literal["active", "cancelled", "deleted"] = "active"


class AttemptResponse(BaseModel):
    id: UUID
    status: str
    audio_bytes: int | None
    duration_seconds: float | None
    codec: str | None
    error: str | None
    audio_sha256: str | None
    pcm_sha256: str | None


class TrackDetailsResponse(TrackResponse):
    attempts: list[AttemptResponse]


class IntakeControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: Literal["active", "cancelled", "deleted"]


class TrackActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID


class TrackActionResponse(BaseModel):
    video_id: str
    status: Literal["pending", "forgotten"]
    intake_id: UUID | None = None


class ScheduleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)
    playlist_id: str = Field(pattern=r"^[A-Za-z0-9_-]{2,256}$")
    cron: str = Field(max_length=100)
    timezone: str = Field(default="UTC", max_length=100)
    limit: int = Field(default=100, ge=1, le=100)
    enabled: bool = True

    @model_validator(mode="after")
    def valid_schedule(self) -> "ScheduleRequest":
        try:
            if len(self.cron.split()) != 5 or not croniter.is_valid(self.cron):
                raise ValueError("Expected a five-field cron expression")
            ZoneInfo(self.timezone)
            croniter(self.cron).get_next(float)
        except (KeyError, ValueError) as exc:
            raise ValueError("Invalid cron expression or IANA timezone") from exc
        return self


class ScheduleResponse(ScheduleRequest):
    model_config = ConfigDict(extra="ignore")
    id: UUID
    next_run: float
    run_id: UUID | None
    last_run: float | None
    last_intake_id: UUID | None
    last_error: str | None


class ControlResult(BaseModel):
    status: str


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


def _control_error(
    exc: KeyError | IntakeConflict | IntakeRateLimitError,
) -> HTTPException:
    return HTTPException(
        status_code=404
        if isinstance(exc, KeyError)
        else 429
        if isinstance(exc, IntakeRateLimitError)
        else 409,
        detail=str(exc).strip("'"),
    )


@router.patch("/{intake_id}/control")
def control_intake(
    intake_id: UUID, body: IntakeControlRequest, ledger: LedgerDep, device_id: DeviceDep
) -> ControlResult:
    try:
        IntakeControls(ledger).set_intake_state(str(intake_id), device_id, body.state)
    except (KeyError, IntakeConflict) as exc:
        raise _control_error(exc) from exc
    return ControlResult(status=body.state)


@tracks_router.post("/{video_id}/force-redownload", status_code=202)
def force_redownload(
    video_id: Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{11}$")],
    body: TrackActionRequest,
    ledger: LedgerDep,
    device_id: DeviceDep,
) -> TrackActionResponse:
    try:
        return TrackActionResponse.model_validate(
            IntakeControls(ledger).track_action(
                video_id, device_id, str(body.request_id), "force_redownload"
            )
        )
    except (KeyError, IntakeConflict, IntakeRateLimitError) as exc:
        raise _control_error(exc) from exc


@tracks_router.delete("/{video_id}")
def forget_track(
    video_id: Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{11}$")],
    body: TrackActionRequest,
    ledger: LedgerDep,
    device_id: DeviceDep,
) -> TrackActionResponse:
    try:
        return TrackActionResponse.model_validate(
            IntakeControls(ledger).track_action(
                video_id, device_id, str(body.request_id), "forget"
            )
        )
    except (KeyError, IntakeConflict, IntakeRateLimitError) as exc:
        raise _control_error(exc) from exc


@schedules_router.get("")
def list_schedules(ledger: LedgerDep, device_id: DeviceDep) -> list[ScheduleResponse]:
    return [
        ScheduleResponse.model_validate(row)
        for row in IntakeControls(ledger).list_schedules(device_id)
    ]


@schedules_router.post("", status_code=201)
def create_schedule(
    body: ScheduleRequest, ledger: LedgerDep, device_id: DeviceDep
) -> ScheduleResponse:
    try:
        return ScheduleResponse.model_validate(
            IntakeControls(ledger).save_schedule(device_id, None, body.model_dump())
        )
    except (KeyError, IntakeConflict) as exc:
        raise _control_error(exc) from exc


@schedules_router.put("/{schedule_id}")
def update_schedule(
    schedule_id: UUID, body: ScheduleRequest, ledger: LedgerDep, device_id: DeviceDep
) -> ScheduleResponse:
    try:
        return ScheduleResponse.model_validate(
            IntakeControls(ledger).save_schedule(
                device_id, str(schedule_id), body.model_dump()
            )
        )
    except (KeyError, IntakeConflict) as exc:
        raise _control_error(exc) from exc


@schedules_router.post("/{schedule_id}/run", status_code=202)
def run_schedule(
    schedule_id: UUID, ledger: LedgerDep, device_id: DeviceDep
) -> ControlResult:
    try:
        IntakeControls(ledger).run_schedule_now(str(schedule_id), device_id)
    except (KeyError, IntakeConflict) as exc:
        raise _control_error(exc) from exc
    return ControlResult(status="scheduled")


@schedules_router.delete("/{schedule_id}")
def delete_schedule(
    schedule_id: UUID, ledger: LedgerDep, device_id: DeviceDep
) -> ControlResult:
    try:
        IntakeControls(ledger).delete_schedule(str(schedule_id), device_id)
    except (KeyError, IntakeConflict) as exc:
        raise _control_error(exc) from exc
    return ControlResult(status="deleted")


class PlaylistPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    playlist_id: str = Field(pattern=r"^[A-Za-z0-9_-]{2,256}$")
    limit: int = Field(default=100, ge=1, le=100)


class PlaylistPreviewResponse(BaseModel):
    playlist_id: str
    title: str
    tracks: list[IntakeTrack]


@router.post("/preview-playlist", responses={502: {"model": ErrorResponse}})
def playlist_preview(
    body: PlaylistPreviewRequest, ledger: LedgerDep, device_id: DeviceDep
) -> PlaylistPreviewResponse:
    # Reuse the durable per-device request budget for expensive metadata reads.
    try:
        ledger.reserve_preview(device_id)
    except IntakeRateLimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    try:
        return PlaylistPreviewResponse.model_validate(
            preview_playlist(body.playlist_id, body.limit)
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail="Public playlist preview unavailable"
        ) from exc


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


@router.get("/{intake_id}/details")
def intake_details(
    intake_id: UUID, ledger: LedgerDep, device_id: DeviceDep
) -> IntakeDetailsResponse:
    summary = read_intake(intake_id, ledger, device_id)
    return IntakeDetailsResponse(
        **summary.model_dump(),
        request=IntakeRequest.model_validate(ledger.intake_request(str(intake_id))),
        state=IntakeControls(ledger).intake_state(str(intake_id)),
    )


@tracks_router.get("/{video_id}/details")
def track_details(
    video_id: Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{11}$")],
    ledger: LedgerDep,
    device_id: DeviceDep,
) -> TrackDetailsResponse:
    summary = read_track(video_id, ledger, device_id)
    return TrackDetailsResponse(
        **summary.model_dump(),
        attempts=[
            AttemptResponse.model_validate(row)
            for row in ledger.attempt_details(video_id)
        ],
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
