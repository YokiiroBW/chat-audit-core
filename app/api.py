import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.database import get_db_session
from app.schemas import (
    AdapterCreateRequest,
    AdapterResponse,
    AdapterUpdateRequest,
    BotProfileResponse,
    ImportResultResponse,
    ImportValidationResponse,
    MessageResponse,
    RoomResponse,
)
from app.services.adapter_service import AdapterService
from app.services.backup_service import BackupService
from app.services.onebot_rpc_service import OneBotRPCService
from app.services.query_service import QueryService


def _extract_bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def require_admin_api_token(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> None:
    configured_token = settings.admin_api_token.strip()
    if not configured_token:
        return

    header_token = request.headers.get("x-admin-token")
    bearer_token = _extract_bearer_token(request.headers.get("authorization"))
    if header_token == configured_token or bearer_token == configured_token:
        return

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid admin api token")


router = APIRouter(dependencies=[Depends(require_admin_api_token)])


@router.get("/adapters", response_model=list[AdapterResponse])
async def list_adapters(db: AsyncSession = Depends(get_db_session)) -> list[AdapterResponse]:
    adapters = await QueryService.list_adapters(db)
    return [AdapterResponse.model_validate(adapter) for adapter in adapters]


@router.get("/bots", response_model=list[BotProfileResponse])
async def list_bots(db: AsyncSession = Depends(get_db_session)) -> list[BotProfileResponse]:
    profiles = await QueryService.list_bot_profiles(db)
    return [BotProfileResponse.model_validate(profile) for profile in profiles]


@router.post("/adapters", response_model=AdapterResponse, status_code=status.HTTP_201_CREATED)
async def create_adapter(
    payload: AdapterCreateRequest,
    db: AsyncSession = Depends(get_db_session),
) -> AdapterResponse:
    try:
        adapter = await AdapterService.create_adapter(
            db,
            adapter_id=payload.id,
            platform=payload.platform,
            config_json=payload.config_json,
            status=payload.status,
            current_robot_id=payload.current_robot_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return AdapterResponse.model_validate(adapter)


@router.patch("/adapters/{adapter_id}", response_model=AdapterResponse)
async def update_adapter(
    adapter_id: str,
    payload: AdapterUpdateRequest,
    db: AsyncSession = Depends(get_db_session),
) -> AdapterResponse:
    adapter = await AdapterService.update_adapter(
        db,
        adapter_id=adapter_id,
        platform=payload.platform,
        config_json=payload.config_json,
        status=payload.status,
        current_robot_id=payload.current_robot_id,
        config_json_provided="config_json" in payload.model_fields_set,
        current_robot_id_provided="current_robot_id" in payload.model_fields_set,
    )
    if adapter is None:
        raise HTTPException(status_code=404, detail="adapter not found")
    return AdapterResponse.model_validate(adapter)


@router.delete("/adapters/{adapter_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_adapter(adapter_id: str, db: AsyncSession = Depends(get_db_session)) -> Response:
    deleted = await AdapterService.delete_adapter(db, adapter_id=adapter_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="adapter not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/rooms", response_model=list[RoomResponse])
async def list_rooms(
    robot_id: str = Query(..., min_length=1),
    db: AsyncSession = Depends(get_db_session),
) -> list[RoomResponse]:
    rooms = await QueryService.list_rooms(db, robot_id=robot_id)
    return [RoomResponse(**room) for room in rooms]


@router.get("/messages", response_model=list[MessageResponse])
async def list_messages(
    robot_id: str = Query(..., min_length=1),
    room_id: str = Query(..., min_length=1),
    before_timestamp: int | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db_session),
) -> list[MessageResponse]:
    messages = await QueryService.list_messages(
        db,
        robot_id=robot_id,
        room_id=room_id,
        before_timestamp=before_timestamp,
        limit=limit,
    )
    return [MessageResponse.model_validate(message) for message in messages]


@router.get("/search", response_model=list[MessageResponse])
async def search_messages(
    robot_id: str = Query(..., min_length=1),
    keyword: str | None = Query(default=None),
    room_id: str | None = Query(default=None),
    sender_id: str | None = Query(default=None),
    start_timestamp: int | None = Query(default=None),
    end_timestamp: int | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db_session),
) -> list[MessageResponse]:
    messages = await QueryService.search_messages(
        db,
        robot_id=robot_id,
        keyword=keyword,
        room_id=room_id,
        sender_id=sender_id,
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
        limit=limit,
    )
    return [MessageResponse.model_validate(message) for message in messages]


@router.get("/forward")
async def get_forward_message(
    robot_id: str = Query(..., min_length=1),
    forward_id: str = Query(..., min_length=1),
) -> dict:
    try:
        return await OneBotRPCService.call_action(robot_id, "get_forward_msg", {"id": forward_id})
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except asyncio.TimeoutError as exc:
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail="onebot action timed out") from exc


@router.get("/export")
async def export_data(
    robot_id: str | None = Query(default=None, min_length=1),
    room_id: str | None = Query(default=None, min_length=1),
    start_timestamp: int | None = Query(default=None),
    end_timestamp: int | None = Query(default=None),
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
):
    return await BackupService.export_package(
        db,
        robot_id=robot_id,
        room_id=room_id,
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
        storage_root=settings.storage_root,
        public_storage_prefix=settings.public_storage_prefix,
        max_media_bytes=settings.media_max_bytes,
    )


@router.post("/import/validate", response_model=ImportValidationResponse)
async def validate_import_data(
    package: dict,
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> ImportValidationResponse:
    report = await BackupService.preview_import_package(
        db,
        package,
        storage_root=settings.storage_root,
        public_storage_prefix=settings.public_storage_prefix,
    )
    return ImportValidationResponse(**report)


@router.post("/import", response_model=ImportResultResponse)
async def import_data(
    package: dict,
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> ImportResultResponse:
    try:
        result = await BackupService.import_package(
            db,
            package,
            storage_root=settings.storage_root,
            public_storage_prefix=settings.public_storage_prefix,
        )
    except ValueError as exc:
        BackupService.write_failure_log(settings.backup_root, event="import", error=str(exc), context={"schema": (package.get("manifest") or {}).get("schema")})
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ImportResultResponse(**result)
