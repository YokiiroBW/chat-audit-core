"""Product-local candidate API; not a published Tianshu I06 wire contract."""

import hashlib
import json
import re
import secrets
import time
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database import get_db_session
from app.services.evidence_service import (
    ArchiveLimitExceeded, ArchiveScope, ArchivedMessage, EvidenceService,
    MAX_RECORD_CHARS, message_key,
)
from app.services.evidence_cursor import cursor_signing_key, decode_cursor, encode_cursor, query_binding
from app.services.interaction_service import InteractionService
from app.services.history_contract import history_contract_ready

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


Hash = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Locator = Annotated[str, Field(max_length=512, pattern=r"^audit-evidence:0:[a-f0-9]{64}:[A-Za-z0-9_-]{1,64}:[a-f0-9]{64}$")]
Timestamp = Annotated[int, Field(ge=-(2**63), le=2**63 - 1)]


class HistoryRequest(StrictModel):
    schema_version: Annotated[int, Field(ge=1, le=1)]
    channel: EvidenceChannel


class NativeAliasRecord(StrictModel):
    platform: Literal["qq"]
    account_id: Identifier
    room_id: Identifier
    message_type: Literal["group", "private"]
    id_kind: Literal["external", "qqnt_source", "qqnt_platform"]
    message_id: Identifier
    import_source_id: Identifier | None

    @model_validator(mode="after")
    def source_namespace(self):
        if (self.id_kind == "external") != (self.import_source_id is None):
            raise ValueError("Invalid native alias namespace")
        return self


class HistoryRecord(EvidenceRecord):
    locator: Locator
    stored_content_sha256: Hash
    text: Annotated[str, Field(max_length=MAX_RECORD_CHARS)]
    sender_id: Identifier
    timestamp: Timestamp
    source_sequence: Timestamp | None
    external_message_id: Annotated[str, Field(min_length=1, max_length=64)] | None
    message_id: Identifier | None
    native_aliases: Annotated[list[NativeAliasRecord], Field(max_length=256)]


class SearchFilters(StrictModel):
    keyword: Annotated[str, Field(max_length=256)] | None
    sender_id: Identifier | None
    start_timestamp: Timestamp | None
    end_timestamp: Timestamp | None

    @model_validator(mode="after")
    def interval(self):
        if self.start_timestamp is not None and self.end_timestamp is not None and self.start_timestamp > self.end_timestamp:
            raise ValueError("Invalid archive interval")
        return self


class SearchRequest(HistoryRequest):
    filters: SearchFilters
    cursor: Annotated[str, Field(max_length=2048, pattern=r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")] | None
    limit: Annotated[int, Field(ge=1, le=100)]


class LocatorRequest(HistoryRequest):
    locator: Locator
    expected_stored_content_sha256: Hash
    before: Annotated[int, Field(ge=0, le=10)]
    after: Annotated[int, Field(ge=0, le=10)]


class LocatorCheck(StrictModel):
    locator: Locator
    expected_stored_content_sha256: Hash


class CheckRequest(HistoryRequest):
    items: Annotated[list[LocatorCheck], Field(min_length=1, max_length=100)]

    @model_validator(mode="after")
    def unique(self):
        if len({item.locator for item in self.items}) != len(self.items):
            raise ValueError("Duplicate archive locator")
        return self


class GroupStatsRequest(HistoryRequest):
    start_timestamp: Timestamp
    end_timestamp: Timestamp
    bucket_seconds: int
    timezone_offset_minutes: Annotated[int, Field(ge=-840, le=840)]
    message_limit: Annotated[int, Field(ge=1, le=10000)]

    @model_validator(mode="after")
    def interval(self):
        if self.start_timestamp > self.end_timestamp or self.bucket_seconds not in {3600, 86400}:
            raise ValueError("Invalid archive interval")
        return self


def _response(value):
    response = JSONResponse(value, headers=NO_STORE)
    if len(response.body) > MAX_RESPONSE_BYTES:
        raise _error(413, "evidence_too_large")
    return response


def _locator_key(locator, expected, scope_digest):
    # Malformed shape is rejected by the request model. A contradictory expected
    # hash is a bad request, not an observed source change.
    _, _, digest, msg_hash, stored = locator.split(":")
    if stored != expected:
        raise _error(422, "contradictory_evidence_hash")
    return msg_hash if digest == scope_digest else None


def _current_status(message, expected):
    if message is None:
        return "missing", None
    if (message.raw_chars > MAX_RECORD_CHARS or
            len(message.raw_message.encode("utf-8")) != message.raw_bytes):
        # The bounded SQL projection is not the full new body. It cannot prove
        # the previous hash current and must not mint a digest for its prefix.
        return "changed", None
    digest = hashlib.sha256(message.raw_message.encode("utf-8")).hexdigest()
    return ("archive_current" if digest == expected else "changed"), digest


async def _history_records(db, mapping, messages, digest):
    scope = mapping.archive_scope()
    try:
        aliases = await EvidenceService.native_aliases(db, scope=scope, messages=messages)
    except ArchiveLimitExceeded:
        raise _error(413, "evidence_too_large") from None
    records = {}
    for message in messages:
        native = sorted(aliases[message.msg_hash], key=lambda value: (value.id_kind, value.import_source_id or "", value.message_id))
        identifiers = {value.message_id for value in native if value.id_kind == mapping.id_kind and
                       value.import_source_id == mapping.import_source_id}
        record = _record(message, digest).model_dump()
        record.update(message_id=next(iter(identifiers)) if len(identifiers) == 1 else None,
                      native_aliases=[{"platform": scope.platform, "account_id": scope.robot_id,
                          "room_id": scope.room_id, "message_type": scope.message_type,
                          "id_kind": value.id_kind, "message_id": value.message_id,
                          "import_source_id": value.import_source_id} for value in native])
        try:
            records[message.msg_hash] = HistoryRecord.model_validate(record).model_dump()
        except ValidationError:
            raise _error(422, "evidence_metadata_unavailable") from None
    return records


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
    history_ready = history_contract_ready()

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

    async def authorize_history(authorization=Depends(authorize)):
        if not history_ready:
            raise _error(503, "history_contract_unavailable")
        return authorization

    def authorized_mapping(payload, authorization):
        config, _ = authorization
        mapping = next((m for m in config.mappings if m.channel == payload.channel and
                        m.mapping_id in authorization[1].mapping_ids), None)
        if mapping is None:
            raise _error(404, "evidence_not_found")
        return mapping

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
        mapping = authorized_mapping(payload, authorization)
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

    @router.post("/internal/evidence/search", tags=["Evidence history"])
    async def search_history(
        payload: SearchRequest, authorization=Depends(authorize_history), db: AsyncSession = Depends(get_db_session),
    ):
        mapping = authorized_mapping(payload, authorization)
        digest = _scope_digest(settings, mapping)
        reader = authorization[1]
        binding = query_binding(digest, reader.service_id, payload.filters.model_dump(), payload.limit)
        signing_key = cursor_signing_key(settings.app_secret_key, reader.token_sha256)
        try:
            before = decode_cursor(payload.cursor, binding, signing_key) if payload.cursor else None
        except ValueError:
            raise _error(422, "invalid_evidence_cursor") from None
        page = await EvidenceService.search(db, scope=mapping.archive_scope(),
                                           **payload.filters.model_dump(), limit=payload.limit, before=before)
        records = await _history_records(db, mapping, page.messages, digest)
        # Fit complete records; never split text or advance past an unreturned row.
        for count in range(len(page.messages), -1, -1):
            more = page.has_more or count < len(page.messages)
            cursor = (encode_cursor(message_key(page.messages[count - 1]), binding, signing_key)
                      if more and count else None)
            value = {"schema_version": 1, "channel": payload.channel.model_dump(),
                "status": "archive_observed", "current_source_state": "not_checked", "id_kind": mapping.id_kind,
                "records": [records[m.msg_hash] for m in page.messages[:count]], "next_cursor": cursor,
                "coverage": {"consistency": "live_keyset", "returned": count, "has_more": more}}
            response = JSONResponse(value, headers=NO_STORE)
            if len(response.body) <= MAX_RESPONSE_BYTES and (count or not page.messages):
                return response
        raise _error(413, "evidence_too_large")

    @router.post("/internal/evidence/read-locator", tags=["Evidence history"])
    async def read_locator(
        payload: LocatorRequest, authorization=Depends(authorize_history), db: AsyncSession = Depends(get_db_session),
    ):
        mapping = authorized_mapping(payload, authorization)
        digest = _scope_digest(settings, mapping)
        key = _locator_key(payload.locator, payload.expected_stored_content_sha256, digest)
        window = (await EvidenceService.read(db, scope=mapping.archive_scope(), message_id=None,
                  msg_hash=key, before=payload.before, after=payload.after)) if key else None
        status, _ = _current_status(window.target if window else None, payload.expected_stored_content_sha256)
        target = context = None
        if status == "archive_current":
            records = await _history_records(db, mapping, [*window.before, window.target, *window.after], digest)
            target = records[window.target.msg_hash]
            context = {"before": [records[m.msg_hash] for m in window.before],
                       "after": [records[m.msg_hash] for m in window.after], "complete": False}
        return _response({"schema_version": 1, "channel": payload.channel.model_dump(),
            "status": status, "core_source_state": "not_observed", "id_kind": mapping.id_kind,
            "target": target, "context": context})

    @router.post("/internal/evidence/check", tags=["Evidence history"])
    async def check_locators(
        payload: CheckRequest, authorization=Depends(authorize_history), db: AsyncSession = Depends(get_db_session),
    ):
        mapping = authorized_mapping(payload, authorization)
        digest = _scope_digest(settings, mapping)
        keys = [_locator_key(item.locator, item.expected_stored_content_sha256, digest) for item in payload.items]
        messages = await EvidenceService.current_many(db, scope=mapping.archive_scope(), msg_hashes=[k for k in keys if k])
        results = []
        for item, key in zip(payload.items, keys):
            status, current = _current_status(messages.get(key), item.expected_stored_content_sha256)
            results.append({"locator": item.locator, "status": status, "current_stored_content_sha256": current})
        return _response({"schema_version": 1, "channel": payload.channel.model_dump(),
            "core_source_state": "not_observed", "observed_at": int(time.time()), "results": results})

    @router.post("/internal/evidence/group-stats", tags=["Evidence history"])
    async def group_stats(
        payload: GroupStatsRequest, authorization=Depends(authorize_history), db: AsyncSession = Depends(get_db_session),
    ):
        mapping = authorized_mapping(payload, authorization)
        if mapping.message_type != "group":
            raise _error(404, "evidence_not_found")
        try:
            value = await InteractionService.observe(db, scope=mapping.archive_scope(),
                                                     **payload.model_dump(exclude={"channel", "schema_version"}))
        except ArchiveLimitExceeded:
            raise _error(413, "evidence_too_large") from None
        hashes = {key for edge in value["edges"] for key in edge["evidence_hashes"]}
        messages = await EvidenceService.current_many(db, scope=mapping.archive_scope(), msg_hashes=hashes)
        if set(messages) != hashes:
            raise _error(409, "evidence_changed_during_observation")
        digest = _scope_digest(settings, mapping)
        for edge in value["edges"]:
            edge["evidence_locators"] = [_record(messages[key], digest).locator for key in edge.pop("evidence_hashes")]
        return _response({"schema_version": 1, "channel": payload.channel.model_dump(),
            "status": "archive_observed", "current_source_state": "not_checked",
            "window": payload.model_dump(exclude={"schema_version", "channel", "message_limit"}), **value})

    return router
