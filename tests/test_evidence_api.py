import asyncio
import copy
import hashlib
import json
import socket
import sys

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
import uvicorn

from app.config import Settings, get_settings
from app.database import create_all_tables, create_async_engine_and_sessionmaker, get_db_session
from app.evidence_api import MAX_RECORD_BYTES, MAX_RESPONSE_BYTES, create_evidence_router
from app.models import Adapter, Message
from tests.test_evidence_service import archived, imported


TOKEN = "synthetic-evidence-service-token"
CHANNEL = {"namespace": "qq", "binding_id": "binding-a", "channel_conversation_id": "group:42", "thread_id": None}
PRIVATE = {**CHANNEL, "channel_conversation_id": "private:42"}
BOT_B = {**CHANNEL, "binding_id": "binding-b"}


def configuration():
    return {
        "mappings": [
            {"mapping_id": "group-a", "channel": CHANNEL, "robot_id": "account-a", "room_id": "42", "message_type": "group"},
            {"mapping_id": "private-a", "channel": PRIVATE, "robot_id": "account-a", "room_id": "42", "message_type": "private"},
            {"mapping_id": "group-b", "channel": BOT_B, "robot_id": "account-b", "room_id": "42", "message_type": "group"},
        ],
        "readers": [
            {"service_id": "group-reader", "token_sha256": hashlib.sha256(TOKEN.encode()).hexdigest(), "mapping_ids": ["group-a"]},
            {"service_id": "private-reader", "token_sha256": hashlib.sha256(b"synthetic-private-reader").hexdigest(), "mapping_ids": ["private-a"]},
        ],
    }


def make_settings(tmp_path, config):
    return Settings(
        _env_file=None, database_url="sqlite+aiosqlite:///:memory:",
        storage_root=tmp_path / "storage", backup_root=tmp_path / "backup",
        system_instance_id="isolated-ts-061", admin_api_token="synthetic-admin",
        evidence_read_config=json.dumps(config) if config is not None else "",
        auto_backup_cron="",
    )


def api_client(db, tmp_path, config=None):
    settings = make_settings(tmp_path, configuration() if config is None else config)
    # Product route and dependencies are real. This minimal host avoids the
    # legacy backup application's Unix-only fcntl import on Windows.
    app = FastAPI()
    app.include_router(create_evidence_router(settings))
    async def session():
        yield db
    app.dependency_overrides[get_db_session] = session
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": f"Bearer {TOKEN}"}), settings


def request(**kwargs):
    return {"channel": CHANNEL, "message_id": "7", **kwargs}


@pytest.mark.asyncio
async def test_read_observation_locator_and_no_side_effects(db_session, tmp_path):
    archived(db_session, "a")
    await db_session.commit()
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(db_session.bind.sync_engine, "before_cursor_execute", capture)
    client, _ = api_client(db_session, tmp_path)
    try:
        async with client:
            first = await client.post("/internal/evidence/read", json=request())
            assert first.status_code == 200, first.text
            data = first.json()
            second = await client.post("/internal/evidence/read", json=request(locator=data["target"]["locator"]))
            assert second.json() == data
    finally:
        event.remove(db_session.bind.sync_engine, "before_cursor_execute", capture)
    assert all(s.lstrip().upper().startswith("SELECT") for s in statements)
    assert data["status"] == "archive_observed"
    assert data["current_source_state"] == "not_checked"
    assert data["target"]["text"] == "raw a"
    assert data["target"]["stored_content_sha256"] == hashlib.sha256(b"raw a").hexdigest()
    assert data["target"]["media_state"] == "unavailable"
    assert data["context"] == {"before": [], "after": [], "complete": False}
    assert not any(word in first.text for word in ["receipt_id", "revision", "private-cache", "raw_message", "nickname"])
    assert first.headers["cache-control"] == "no-store"
    assert not db_session.dirty and not db_session.new


@pytest.mark.asyncio
async def test_unknown_mapping_denied_missing_and_conflicting_locator_are_identical(db_session, tmp_path):
    archived(db_session, "a")
    archived(db_session, "private", kind="private")
    archived(db_session, "b", robot="account-b")
    await db_session.commit()
    client, _ = api_client(db_session, tmp_path)
    async with client:
        data = (await client.post("/internal/evidence/read", json=request())).json()
        locator = data["target"]["locator"]
        attempts = [
            request(channel=PRIVATE), request(channel=BOT_B),
            request(channel={**CHANNEL, "binding_id": "unknown"}),
            request(message_id="missing"), request(message_id="missing", locator=locator),
            request(locator="made-up"), request(locator=locator[:-1] + ("0" if locator[-1] != "0" else "1")),
            request(channel=PRIVATE, locator=locator),
        ]
        responses = [await client.post("/internal/evidence/read", json=p) for p in attempts]
    assert all(r.status_code == 404 and r.json() == {"detail": "evidence_not_found"} for r in responses)
    assert all(r.headers["cache-control"] == "no-store" for r in responses)


@pytest.mark.asyncio
async def test_locator_disambiguates_only_inside_authorized_candidate_set(db_session, tmp_path):
    archived(db_session, "a")
    await db_session.commit()
    client, settings = api_client(db_session, tmp_path)
    async with client:
        locator = (await client.post("/internal/evidence/read", json=request())).json()["target"]["locator"]
        archived(db_session, "other")
        await db_session.commit()
        assert (await client.post("/internal/evidence/read", json=request())).status_code == 404
        assert (await client.post("/internal/evidence/read", json=request(locator=locator))).json()["target"]["text"] == "raw a"
        (await db_session.get(Message, "a")).raw_message = "changed archive content"
        await db_session.commit()
        assert (await client.post("/internal/evidence/read", json=request(locator=locator))).status_code == 404
        # Even a legitimately registered new account binding cannot reuse the old locator.
        config = configuration()
        config["mappings"][0]["robot_id"] = "account-b"
        settings.evidence_read_config = json.dumps(config)
        assert (await client.post("/internal/evidence/read", json=request(locator=locator))).status_code == 404


@pytest.mark.asyncio
async def test_fixed_account_survives_adapter_switch_and_private_reader_cannot_read_group(db_session, tmp_path):
    archived(db_session, "a")
    archived(db_session, "b", robot="account-b")
    archived(db_session, "private", kind="private")
    adapter = Adapter(id="binding-a", platform="qq", current_robot_id="account-a")
    db_session.add(adapter)
    await db_session.commit()
    client, _ = api_client(db_session, tmp_path)
    async with client:
        before = (await client.post("/internal/evidence/read", json=request())).json()
        adapter.current_robot_id = "account-b"
        await db_session.commit()
        after = (await client.post("/internal/evidence/read", json=request())).json()
        assert before == after
        client.headers["authorization"] = "Bearer synthetic-private-reader"
        assert (await client.post("/internal/evidence/read", json=request())).status_code == 404
        private = await client.post("/internal/evidence/read", json=request(channel=PRIVATE))
        assert private.json()["target"]["text"] == "raw private"


@pytest.mark.asyncio
async def test_import_alias_lookup_uses_registered_source_and_id_kind(db_session, tmp_path):
    archived(db_session, "imported", robot=None, message_id=None)
    imported(db_session, "imported")
    imported(db_session, "imported", source="source-b", account="account-b", source_alias="b-alias")
    await db_session.commit()
    config = configuration()
    config["mappings"][0].update(id_kind="qqnt_source", import_source_id="source-a")
    client, _ = api_client(db_session, tmp_path, config)
    async with client:
        valid = await client.post("/internal/evidence/read", json=request(message_id="native-7"))
        assert valid.status_code == 200
        assert valid.json()["target"]["external_message_id"] is None
        for message_id in ["b-alias", "platform-7"]:
            assert (await client.post("/internal/evidence/read", json=request(message_id=message_id))).status_code == 404


@pytest.mark.asyncio
async def test_context_and_cq_metadata_do_not_expand_other_records(db_session, tmp_path):
    archived(db_session, "a", message_id="6", sequence=0)
    center = archived(db_session, "b", sequence=1)
    center.raw_message = "hello [CQ:reply,id=foreign] [CQ:image,file=/secret/cache.jpg,url=https://secret.invalid/?token=hidden]"
    archived(db_session, "c", message_id="8", sequence=1)
    archived(db_session, "foreign", kind="private", sequence=1)
    archived(db_session, "other-room", room="43", sequence=1)
    archived(db_session, "other-bot", robot="account-b", sequence=1)
    await db_session.commit()
    client, _ = api_client(db_session, tmp_path)
    async with client:
        response = await client.post("/internal/evidence/read", json=request(before=10, after=10))
    assert response.status_code == 200
    data = response.json()
    assert [m["text"] for m in data["context"]["before"]] == ["raw a"]
    assert [m["text"] for m in data["context"]["after"]] == ["raw c"]
    assert data["target"]["text"] == "hello [非文字内容不可用] [非文字内容不可用]"
    assert data["target"]["text_representation"] == "text_with_unavailable_segments"
    assert not any(s in response.text for s in ["foreign", "secret", "hidden", "other-room", "other-bot"])


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["{\"url\":\"secret\"}", "{malformed-secret", '[CQ:json,data={"array":["secret"]}]', '[CQ:unknown,data={"array":["secret"]}]', "[CQ:forward,id=secret]", "[CQ:image,file=unclosed", "prefix\x00hidden-suffix"])
async def test_structured_and_malformed_payloads_are_unavailable(db_session, tmp_path, raw):
    archived(db_session, "a").raw_message = raw
    await db_session.commit()
    client, _ = api_client(db_session, tmp_path)
    async with client:
        response = await client.post("/internal/evidence/read", json=request())
    assert response.status_code == 422
    assert response.json() == {"detail": "evidence_text_unavailable"}


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["x" * 16385, "字" * (MAX_RECORD_BYTES // 3 + 1)], ids=["character-limit", "utf8-byte-limit"])
async def test_record_size_limit_does_not_silently_truncate(db_session, tmp_path, raw):
    archived(db_session, "a").raw_message = raw
    await db_session.commit()
    client, _ = api_client(db_session, tmp_path)
    async with client:
        response = await client.post("/internal/evidence/read", json=request())
    assert response.status_code == 413
    assert response.json() == {"detail": "evidence_too_large"}


@pytest.mark.asyncio
async def test_exact_size_boundary_preserves_unicode_text(db_session, tmp_path):
    raw = "字" * (MAX_RECORD_BYTES // 3) + "xy"
    assert len(raw.encode()) == MAX_RECORD_BYTES
    archived(db_session, "a").raw_message = raw
    await db_session.commit()
    client, _ = api_client(db_session, tmp_path)
    async with client:
        response = await client.post("/internal/evidence/read", json=request())
    assert response.status_code == 200
    assert response.json()["target"]["text"] == raw


@pytest.mark.asyncio
async def test_revoked_scope_and_rotated_token_invalidate_cached_locator(db_session, tmp_path):
    archived(db_session, "a")
    await db_session.commit()
    client, settings = api_client(db_session, tmp_path)
    async with client:
        locator = (await client.post("/internal/evidence/read", json=request())).json()["target"]["locator"]
        config = configuration()
        config["readers"][0]["mapping_ids"] = ["private-a"]
        settings.evidence_read_config = json.dumps(config)
        assert (await client.post("/internal/evidence/read", json=request(locator=locator))).status_code == 404
        config = configuration()
        config["readers"][0]["token_sha256"] = hashlib.sha256(b"rotated-token").hexdigest()
        settings.evidence_read_config = json.dumps(config)
        assert (await client.post("/internal/evidence/read", json=request(locator=locator))).status_code == 401


@pytest.mark.asyncio
async def test_response_total_bytes_and_oversized_neighbor_fail_atomically(db_session, tmp_path):
    for n in range(11):
        archived(db_session, str(n), message_id=str(n), sequence=n).raw_message = "字" * 6000
    await db_session.commit()
    client, _ = api_client(db_session, tmp_path)
    async with client:
        one = await client.post("/internal/evidence/read", json=request(message_id="0"))
        assert one.status_code == 200 and len(one.content) < MAX_RESPONSE_BYTES
        all_rows = await client.post("/internal/evidence/read", json=request(message_id="0", after=10))
        assert all_rows.status_code == 413
        (await db_session.get(Message, "1")).raw_message = "x" * 20000
        await db_session.commit()
        neighbor = await client.post("/internal/evidence/read", json=request(message_id="0", after=1))
        assert neighbor.status_code == 413 and "target" not in neighbor.json()


@pytest.mark.asyncio
async def test_unconfigured_and_invalid_credentials_do_not_fall_back_to_admin(db_session, tmp_path):
    client, settings = api_client(db_session, tmp_path)
    async with client:
        for headers in [{}, {"authorization": "Bearer synthetic-admin"}, {"x-admin-token": "synthetic-admin"}, {"cookie": "chat_audit_session=synthetic-admin"}]:
            client.headers.clear()
            response = await client.post("/internal/evidence/read", json=request(), headers=headers)
            assert response.status_code == 401
        settings.evidence_read_config = ""
        assert (await client.post("/internal/evidence/read", json=request())).status_code == 503


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["token", "channel", "mapping-id", "unknown-scope", "reader-id", "bad-json", "unsupported", "missing-source"])
async def test_invalid_registration_fails_closed_without_echoing_config(db_session, tmp_path, case, caplog):
    config = copy.deepcopy(configuration())
    if case == "token":
        config["readers"][1]["token_sha256"] = config["readers"][0]["token_sha256"]
    elif case == "channel":
        config["mappings"][1]["channel"] = CHANNEL
    elif case == "mapping-id":
        config["mappings"][1]["mapping_id"] = "group-a"
    elif case == "unknown-scope":
        config["readers"][0]["mapping_ids"] = ["not-registered"]
    elif case == "reader-id":
        config["readers"][1]["service_id"] = config["readers"][0]["service_id"]
    elif case == "unsupported":
        config["mappings"][0]["channel"] = {**CHANNEL, "namespace": "tg"}
    elif case == "missing-source":
        config["mappings"][0]["id_kind"] = "qqnt_source"
    client, settings = api_client(db_session, tmp_path, config)
    if case == "bad-json":
        settings.evidence_read_config = "{sensitive-invalid-config"
    async with client:
        response = await client.post("/internal/evidence/read", json=request())
    assert response.status_code == 503
    assert response.json() == {"detail": "evidence_unavailable"}
    assert TOKEN not in caplog.text and "sensitive-invalid-config" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"before": -1}, {"after": 11}, {"before": True}, {"after": 1.5}, {"actor_id": "admin"}, {"person_id": "owner"}, {"receipt_id": "core-receipt"}, {"revision": 1}])
async def test_request_bounds_and_identity_claims_rejected(db_session, tmp_path, change):
    client, _ = api_client(db_session, tmp_path)
    async with client:
        response = await client.post("/internal/evidence/read", json=request(**change))
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_real_loopback_http_with_temporary_sqlite(tmp_path):
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'evidence.sqlite3').as_posix()}"
    engine, sessionmaker = create_async_engine_and_sessionmaker(database_url)
    await create_all_tables(engine)
    async with sessionmaker() as db:
        archived(db, "actual-http")
        archived(db, "private-http", kind="private")
        await db.commit()
    settings = make_settings(tmp_path, configuration()).model_copy(update={"database_url": database_url})
    app = FastAPI()
    app.include_router(create_evidence_router(settings))
    async def session():
        async with sessionmaker() as db:
            yield db
    app.dependency_overrides[get_db_session] = session
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=sock.getsockname()[1], access_log=False, lifespan="on"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if task.done():
                    await task
                    pytest.fail("HTTP server exited before startup")
                await asyncio.sleep(0.01)
        async with AsyncClient(base_url=f"http://127.0.0.1:{sock.getsockname()[1]}", headers={"Authorization": f"Bearer {TOKEN}"}) as client:
            result = await client.post("/internal/evidence/read", json=request())
            assert result.status_code == 200 and result.json()["target"]["text"] == "raw actual-http"
            locator = result.json()["target"]["locator"]
            assert (await client.post("/internal/evidence/read", json=request(locator=locator))).json() == result.json()
            assert (await client.post("/internal/evidence/read", json=request(channel=PRIVATE))).status_code == 404
            assert (await client.post("/internal/evidence/read", json=request(), headers={"Authorization": "Bearer synthetic-admin"})).status_code == 401
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=10)
        finally:
            sock.close()
            await engine.dispose()


@pytest.mark.skipif(sys.platform == "win32", reason="Existing full app imports Unix-only fcntl for backup jobs")
@pytest.mark.asyncio
async def test_product_factory_wires_candidate_without_admin_privilege_crossover(db_session, tmp_path):
    from app.main import create_app

    archived(db_session, "product")
    await db_session.commit()
    settings = make_settings(tmp_path, configuration())
    app = create_app(settings, engine=db_session.bind)
    async def session():
        yield db_session
    app.dependency_overrides[get_db_session] = session
    app.dependency_overrides[get_settings] = lambda: settings
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        reader = {"Authorization": f"Bearer {TOKEN}"}
        admin = {"Authorization": "Bearer synthetic-admin"}
        assert (await client.post("/internal/evidence/read", json=request(), headers=reader)).status_code == 200
        assert (await client.post("/internal/evidence/read", json=request(), headers=admin)).status_code == 401
        assert (await client.get("/api/adapters", headers=reader)).status_code == 401
        assert (await client.get("/api/adapters", headers=admin)).status_code == 200
