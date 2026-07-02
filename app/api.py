from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db_session
from app.schemas import AdapterResponse, ImportResultResponse, MessageResponse, RoomResponse
from app.services.backup_service import BackupService
from app.services.query_service import QueryService

router = APIRouter()


@router.get("/adapters", response_model=list[AdapterResponse])
async def list_adapters(db: AsyncSession = Depends(get_db_session)) -> list[AdapterResponse]:
    adapters = await QueryService.list_adapters(db)
    return [AdapterResponse.model_validate(adapter) for adapter in adapters]


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


@router.get("/export")
async def export_data(
    robot_id: str | None = Query(default=None, min_length=1),
    room_id: str | None = Query(default=None, min_length=1),
    start_timestamp: int | None = Query(default=None),
    end_timestamp: int | None = Query(default=None),
    db: AsyncSession = Depends(get_db_session),
):
    return await BackupService.export_package(
        db,
        robot_id=robot_id,
        room_id=room_id,
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
    )


@router.post("/import", response_model=ImportResultResponse)
async def import_data(package: dict, db: AsyncSession = Depends(get_db_session)) -> ImportResultResponse:
    try:
        result = await BackupService.import_package(db, package)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ImportResultResponse(**result)
