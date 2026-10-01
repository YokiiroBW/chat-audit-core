"""Product-local candidate API; not a published Tianshu I06 wire contract."""

import hashlib
import json
import re
import secrets
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database import get_db_session
from app.services.evidence_service import ArchiveScope, ArchivedMessage, EvidenceService, MAX_RECORD_CHARS

MAX_RECORD_BYTES = 32768
MAX_RESPONSE_BYTES = 131072
NO_STORE = {"Cache-Control": "no-store"}
Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^\S+$")]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class EvidenceChannel(StrictModel):
    namespace: Literal["qq"]
    binding_id: Identifier
    channel_conversation_id: Identifier
    thread_id: None


class EvidenceReadRequest(StrictModel):
    channel: EvidenceChannel
    message_id: Annotated[str, Field(min_length=1, max_length=64)]
    locator: Annotated[str, Field(min_length=1, max_length=512)] | None = None
    before: Annotated[int, Field(ge=0, le=10)] = 0
    after: Annotated[int, Field(ge=0, le=10)] = 0


class ChannelMapping(StrictModel):
    mapping_id: Identifier
    channel: EvidenceChannel
    robot_id: Identifier
    room_id: Identifier
    message_type: Literal["group", "private"]
    id_kind: Literal["external", "qqnt_source", "qqnt_platform"] = "external"
    import_source_id: Identifier | None = None

    def archive_scope(self) -> ArchiveScope:
        return ArchiveScope(
            self.robot_id, self.room_id, self.message_type, self.id_kind, self.import_source_id,
        )

    @model_validator(mode="after")
    def exact_source(self):
        self.archive_scope()
        return self


class EvidenceReader(StrictModel):
    service_id: Identifier
    token_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    mapping_ids: Annotated[list[Identifier], Field(min_length=1, max_length=256)]


class EvidenceReadConfig(StrictModel):
    mappings: Annotated[list[ChannelMapping], Field(min_length=1, max_length=256)]
    readers: Annotated[list[EvidenceReader], Field(min_length=1, max_length=64)]

    @model_validator(mode="after")
    def unambiguous(self):
        for values in (
            [m.mapping_id for m in self.mappings],
            [m.channel.model_dump_json() for m in self.mappings],
            [r.service_id for r in self.readers],
            [r.token_sha256 for r in self.readers],
        ):
            if len(values) != len(set(values)):
                raise ValueError("Duplicate evidence registration")
        mapping_ids = {m.mapping_id for m in self.mappings}
        for reader in self.readers:
            if len(reader.mapping_ids) != len(set(reader.mapping_ids)) or not set(reader.mapping_ids) <= mapping_ids:
                raise ValueError("Invalid evidence reader scope")
        return self


class EvidenceRecord(StrictModel):
    locator: str
    archive_observation_id: str
    stored_content_sha256: str
    text: str
    text_representation: Literal["stored_text", "text_with_unavailable_segments"]
    sender_id: str
    timestamp: int
    source_sequence: int | None
    # Archive-native id; never a Core receipt or asserted Core revision.
    external_message_id: str | None
    media_state: Literal["unavailable"] = "unavailable"


class EvidenceContext(StrictModel):
    before: list[EvidenceRecord]
    after: list[EvidenceRecord]
    complete: Literal[False] = False


class EvidenceReadResponse(StrictModel):
    status: Literal["archive_observed"] = "archive_observed"
    current_source_state: Literal["not_checked"] = "not_checked"
    channel: EvidenceChannel
    id_kind: Literal["external", "qqnt_source", "qqnt_platform"]
    target: EvidenceRecord
    context: EvidenceContext


def _error(status: int, detail: str):
    return HTTPException(status_code=status, detail=detail, headers=NO_STORE)


def _scope_digest(settings: Settings, mapping: ChannelMapping) -> str:
    # Mapping identity and storage scope are part of the locator. Rebinding a
    # channel/account or changing id namespace invalidates the old locator.
    data = {"instance": settings.system_instance_id, "mapping": mapping.model_dump()}
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _stored_text(message: ArchivedMessage) -> tuple[str, str]:
    if message.raw_chars > MAX_RECORD_CHARS or message.raw_bytes > MAX_RECORD_BYTES:
        raise _error(413, "evidence_too_large")
    raw = message.raw_message
    if len(raw.encode("utf-8")) != message.raw_bytes:
        # SQLite text length/substr stop at NUL; never mislabel such a prefix
        # as the original complete text or mint a locator for only its prefix.
        raise _error(422, "evidence_text_unavailable")
    # A serialized payload is not a text message. Do not echo its attachment
    # URLs, credentials, source paths or arbitrary embedded forward contents.
    if raw.lstrip().startswith(("{", "[")) and not raw.lstrip().startswith("[CQ:"):
        raise _error(422, "evidence_text_unavailable")
    if re.search(r"\[CQ:(json|xml|node|forward)(?:,|\])", raw):
        # Embedded/forwarded payloads can contain their own delimiters. Without
        # a trusted text-only representation, refusing is safer than slicing
        # the first closing bracket and echoing the remaining payload.
        raise _error(422, "evidence_text_unavailable")
    text = re.sub(r"\[CQ:[^\[\]]*\]", "[非文字内容不可用]", raw)
    if "[CQ:" in text:
        raise _error(422, "evidence_text_unavailable")
    return text, "stored_text" if text == raw else "text_with_unavailable_segments"


def _record(message: ArchivedMessage, scope_digest: str) -> EvidenceRecord:
    text, representation = _stored_text(message)
    stored_digest = hashlib.sha256(message.raw_message.encode("utf-8")).hexdigest()
    locator = f"audit-evidence:0:{scope_digest}:{message.msg_hash}:{stored_digest}"
    metadata = [locator, message.sender_id, message.timestamp, message.source_sequence, message.external_message_id]
    observation = hashlib.sha256(json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    return EvidenceRecord(
        locator=locator, archive_observation_id=f"audit-observation:{observation}",
        stored_content_sha256=stored_digest, text=text, text_representation=representation,
        sender_id=message.sender_id, timestamp=message.timestamp,
        source_sequence=message.source_sequence, external_message_id=message.external_message_id,
    )


def create_evidence_router(settings: Settings) -> APIRouter:
    router = APIRouter()
    bearer = HTTPBearer(auto_error=False, scheme_name="EvidenceServiceBearer")

    async def authorize(
        credential: HTTPAuthorizationCredentials | None = Depends(bearer),
    ) -> tuple[EvidenceReadConfig, EvidenceReader]:
        try:
            config = EvidenceReadConfig.model_validate_json(settings.evidence_read_config)
        except (ValidationError, ValueError):
            # Deliberately omit validation input from both response and logs.
            raise _error(503, "evidence_unavailable") from None
        token = credential.credentials if credential is not None else ""
        if not token or len(token) > 4096:
            raise _error(401, "invalid_evidence_credentials")
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        reader = next((r for r in config.readers if secrets.compare_digest(digest, r.token_sha256)), None)
        if reader is None:
            raise _error(401, "invalid_evidence_credentials")
        return config, reader

    @router.post(
        "/internal/evidence/read", response_model=EvidenceReadResponse, tags=["Evidence candidate"],
        responses={
            401: {"description": "Invalid independent service credential"},
            404: {"description": "Unauthorized scope, absent, ambiguous or contradictory reference"},
            413: {"description": "Evidence cannot be completely provided within size limits"},
            422: {"description": "Invalid request or no safe complete text representation"},
            503: {"description": "Evidence bridge is unconfigured or configuration is invalid"},
        },
    )
    async def read_evidence(
        payload: EvidenceReadRequest,
        authorization: tuple[EvidenceReadConfig, EvidenceReader] = Depends(authorize),
        db: AsyncSession = Depends(get_db_session),
    ):
        config, reader = authorization
        mapping = next((m for m in config.mappings if m.channel == payload.channel and m.mapping_id in reader.mapping_ids), None)
        if mapping is None:
            raise _error(404, "evidence_not_found")
        scope_digest = _scope_digest(settings, mapping)
        msg_hash = None
        if payload.locator is not None:
            match = re.fullmatch(r"audit-evidence:0:([a-f0-9]{64}):([A-Za-z0-9_-]{1,64}):([a-f0-9]{64})", payload.locator)
            if match is None or match[1] != scope_digest:
                raise _error(404, "evidence_not_found")
            msg_hash = match[2]
        window = await EvidenceService.read(
            db, scope=mapping.archive_scope(), message_id=payload.message_id,
            msg_hash=msg_hash, before=payload.before, after=payload.after,
        )
        if window is None:
            raise _error(404, "evidence_not_found")
        # Check a supplied locator before formatting even an oversized record.
        # A stale/contradictory locator must have the same result as no match.
        if payload.locator is not None:
            if window.target.raw_chars > MAX_RECORD_CHARS or len(window.target.raw_message.encode("utf-8")) != window.target.raw_bytes:
                raise _error(404, "evidence_not_found")
            stored_digest = hashlib.sha256(window.target.raw_message.encode("utf-8")).hexdigest()
            if not secrets.compare_digest(payload.locator, f"audit-evidence:0:{scope_digest}:{window.target.msg_hash}:{stored_digest}"):
                raise _error(404, "evidence_not_found")
        result = EvidenceReadResponse(
            channel=payload.channel, id_kind=mapping.id_kind,
            target=_record(window.target, scope_digest),
            context=EvidenceContext(
                before=[_record(m, scope_digest) for m in window.before],
                after=[_record(m, scope_digest) for m in window.after],
            ),
        )
        response = JSONResponse(result.model_dump(), headers=NO_STORE)
        if len(response.body) > MAX_RESPONSE_BYTES:
            raise _error(413, "evidence_too_large")
        return response

    return router
