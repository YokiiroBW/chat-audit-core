"""Real scoped SQL and production routes; all archives and credentials synthetic."""
import hashlib
import json

import pytest
from sqlalchemy import delete, event

from app.models import ImportSource, Message, MessageSourceRecord, RobotMessage
from app.services.history_contract import history_contract_ready
from tests.test_evidence_api import CHANNEL, PRIVATE, TOKEN, api_client, configuration
from tests.test_evidence_service import archived, imported


def search_request(**changes):
    return {"schema_version": 1, "channel": CHANNEL,
            "filters": {"keyword": None, "sender_id": None, "start_timestamp": None, "end_timestamp": None},
            "cursor": None, "limit": 2, **changes}


def check_item(record):
    return {"locator": record["locator"], "expected_stored_content_sha256": record["stored_content_sha256"]}


@pytest.mark.asyncio
async def test_search_pages_same_second_null_sequences_without_cross_scope(db_session, tmp_path):
    for key, sequence in [("a", None), ("b", 0), ("c", 0), ("d", 1), ("e", 2)]:
        archived(db_session, key, message_id=key, sequence=sequence)
    for key, options in [("foreign", {"robot": "account-b"}), ("private", {"kind": "private"}),
                         ("room", {"room": "43"}), ("platform", {"platform": "tg"})]:
        archived(db_session, key, message_id=key, **options)
    # Several account views must not multiply LIMIT rows.
    db_session.add(RobotMessage(robot_id="account-b", msg_hash="e"))
    await db_session.commit()
    client, _ = api_client(db_session, tmp_path)
    seen, cursor = [], None
    async with client:
        for _ in range(4):
            response = await client.post("/internal/evidence/search", json=search_request(cursor=cursor))
            assert response.status_code == 200, response.text
            value = response.json()
            assert value["current_source_state"] == "not_checked"
            assert value["coverage"]["returned"] == len(value["records"])
            seen.extend(row["message_id"] for row in value["records"])
            assert response.headers["cache-control"] == "no-store"
            cursor = value["next_cursor"]
            assert bool(cursor) == value["coverage"]["has_more"]
            if not cursor:
                break
    assert seen == ["e", "d", "c", "b", "a"]


@pytest.mark.asyncio
async def test_literal_keyword_author_time_and_bound_cursor(db_session, tmp_path):
    for key, body in [("a", "literal 100%_done"), ("b", "100XXdone"), ("c", "100%_done later")]:
        row = archived(db_session, key, message_id=key, timestamp=100 if key != "c" else 101)
        row.raw_message = body
        row.sender_id = "author"
    await db_session.commit()
    client, _ = api_client(db_session, tmp_path)
    request = search_request(limit=1, filters={"keyword": "100%_done", "sender_id": "author",
                                              "start_timestamp": 100, "end_timestamp": 101})
    async with client:
        first = await client.post("/internal/evidence/search", json=request)
        assert first.status_code == 200, first.text
        assert first.json()["records"][0]["message_id"] == "c"
        cursor = first.json()["next_cursor"]
        second = await client.post("/internal/evidence/search", json={**request, "cursor": cursor})
        assert [r["message_id"] for r in second.json()["records"]] == ["a"]
        for changes in ({"limit": 2}, {"filters": {**request["filters"], "keyword": "other"}},
                        {"cursor": cursor[:-1] + ("A" if cursor[-1] != "A" else "B")}):
            bad = await client.post("/internal/evidence/search", json={**request, "cursor": cursor, **changes})
            assert bad.status_code == 422


@pytest.mark.asyncio
async def test_import_search_locator_only_and_aliases_exclude_other_account_source(db_session, tmp_path):
    archived(db_session, "only", robot=None, message_id=None)
    imported(db_session, "only")
    imported(db_session, "only", account="account-b", source="source-b", source_alias="secret-b")
    archived(db_session, "other-device", robot=None, message_id=None)
    imported(db_session, "other-device", source="device-c", source_alias="not-this-source")
    await db_session.commit()
    config = configuration()
    config["mappings"][0].update(id_kind="qqnt_source", import_source_id="source-a")
    client, _ = api_client(db_session, tmp_path, config)
    async with client:
        response = await client.post("/internal/evidence/search", json=search_request())
        assert response.status_code == 200, response.text
        rows = response.json()["records"]
        assert len(rows) == 1
        record = rows[0]
        assert record["external_message_id"] is None and record["message_id"] == "native-7"
        assert {(a["id_kind"], a["message_id"], a["import_source_id"]) for a in record["native_aliases"]} == {
            ("qqnt_source", "native-7", "source-a"), ("qqnt_platform", "platform-7", "source-a")}
        assert all(a["account_id"] == "account-a" and a["room_id"] == "42" for a in record["native_aliases"])
        read = await client.post("/internal/evidence/read-locator", json={
            "schema_version": 1, "channel": CHANNEL, **check_item(record), "before": 10, "after": 10})
        assert read.status_code == 200, read.text
        assert read.json()["status"] == "archive_current" and read.json()["target"] == record
        assert read.json()["context"] == {"before": [], "after": [], "complete": False}
        assert read.json()["core_source_state"] == "not_observed"


@pytest.mark.asyncio
async def test_check_changed_missing_and_revocation_are_fresh(db_session, tmp_path):
    row = archived(db_session, "a")
    await db_session.commit()
    client, settings = api_client(db_session, tmp_path)
    async with client:
        record = (await client.post("/internal/evidence/search", json=search_request())).json()["records"][0]
        request = {"schema_version": 1, "channel": CHANNEL, "items": [check_item(record)]}
        current = await client.post("/internal/evidence/check", json=request)
        assert current.json()["results"][0]["status"] == "archive_current"
        row.raw_message = "changed after search"
        await db_session.commit()
        changed = await client.post("/internal/evidence/check", json=request)
        assert changed.json()["results"][0] == {"locator": record["locator"], "status": "changed",
            "current_stored_content_sha256": hashlib.sha256(row.raw_message.encode()).hexdigest()}
        read = await client.post("/internal/evidence/read-locator", json={
            "schema_version": 1, "channel": CHANNEL, **check_item(record), "before": 0, "after": 0})
        assert read.json()["status"] == "changed" and read.json()["target"] is None
        await db_session.execute(delete(Message).where(Message.msg_hash == "a"))
        await db_session.commit()
        missing = await client.post("/internal/evidence/check", json=request)
        assert missing.json()["results"][0]["status"] == "missing"
        config = configuration()
        config["readers"][0]["mapping_ids"] = ["private-a"]
        settings.evidence_read_config = json.dumps(config)
        denied = await client.post("/internal/evidence/check", json=request)
        assert denied.status_code == 404


@pytest.mark.asyncio
async def test_changed_to_oversized_text_never_hashes_a_prefix(db_session, tmp_path):
    row = archived(db_session, "a")
    await db_session.commit()
    client, _ = api_client(db_session, tmp_path)
    async with client:
        record = (await client.post("/internal/evidence/search", json=search_request())).json()["records"][0]
        row.raw_message = "x" * 20000
        await db_session.commit()
        response = await client.post("/internal/evidence/check", json={
            "schema_version": 1, "channel": CHANNEL, "items": [check_item(record)]})
        assert response.status_code == 200
        assert response.json()["results"][0] == {"locator": record["locator"], "status": "changed",
                                               "current_stored_content_sha256": None}


@pytest.mark.asyncio
async def test_byte_budget_pages_complete_unicode_records_without_skips(db_session, tmp_path):
    for index in range(8):
        row = archived(db_session, str(index), message_id=str(index), timestamp=100 + index)
        row.raw_message = "汉" * 9000
    await db_session.commit()
    client, _ = api_client(db_session, tmp_path)
    records, cursor = [], None
    async with client:
        for _ in range(4):
            response = await client.post("/internal/evidence/search", json=search_request(limit=100, cursor=cursor))
            assert response.status_code == 200, response.text
            assert len(response.content) <= 131072
            value = response.json()
            records.extend(value["records"])
            cursor = value["next_cursor"]
            if not cursor:
                break
    assert [r["message_id"] for r in records] == list(map(str, reversed(range(8))))
    assert all(r["text"] == "汉" * 9000 for r in records)


@pytest.mark.asyncio
async def test_read_only_routes_never_flush_or_hydrate(db_session, tmp_path):
    archived(db_session, "a")
    await db_session.commit()
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(db_session.bind.sync_engine, "before_cursor_execute", capture)
    client, _ = api_client(db_session, tmp_path)
    try:
        async with client:
            value = (await client.post("/internal/evidence/search", json=search_request())).json()
            db_session.add(Message(msg_hash="unflushed"))
            response = await client.post("/internal/evidence/check", json={
                "schema_version": 1, "channel": CHANNEL, "items": [check_item(value["records"][0])]})
            assert response.status_code == 200
        assert statements and all(s.lstrip().upper().startswith("SELECT") for s in statements)
        assert any(m.msg_hash == "unflushed" for m in db_session.new)
        assert not db_session.dirty
    finally:
        event.remove(db_session.bind.sync_engine, "before_cursor_execute", capture)
        await db_session.rollback()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"robot_id": "account-b"}, {"channel": PRIVATE}, {"schema_version": True},
                                    {"limit": 0}, {"limit": 101}, {"cursor": ""}])
async def test_search_rejects_identity_claims_and_invalid_bounds(db_session, tmp_path, change):
    client, _ = api_client(db_session, tmp_path)
    async with client:
        response = await client.post("/internal/evidence/search", json=search_request(**change))
    assert response.status_code in {404, 422}


@pytest.mark.asyncio
async def test_token_rotation_rejects_old_cursor_under_new_token(db_session, tmp_path):
    for key in ("a", "b", "c"):
        archived(db_session, key, message_id=key)
    await db_session.commit()
    client, settings = api_client(db_session, tmp_path)
    async with client:
        cursor = (await client.post("/internal/evidence/search", json=search_request())).json()["next_cursor"]
        config = configuration()
        config["readers"][0]["token_sha256"] = hashlib.sha256(b"new-synthetic-token").hexdigest()
        settings.evidence_read_config = json.dumps(config)
        assert (await client.post("/internal/evidence/search", json=search_request(cursor=cursor))).status_code == 401
        client.headers["Authorization"] = "Bearer new-synthetic-token"
        assert (await client.post("/internal/evidence/search", json=search_request(cursor=cursor))).status_code == 422


@pytest.mark.asyncio
async def test_complete_alias_set_is_not_silently_truncated(db_session, tmp_path):
    archived(db_session, "a")
    db_session.add(ImportSource(id="source-a", source_type="qqnt", platform="qq", account_id="account-a", device_id="a"))
    for index in range(129):
        db_session.add(MessageSourceRecord(msg_hash="a", source_id="source-a", source_table="messages",
            source_primary_key=str(index), source_external_message_id="source-" + str(index),
            platform_message_id="platform-" + str(index)))
    await db_session.commit()
    client, _ = api_client(db_session, tmp_path)
    async with client:
        response = await client.post("/internal/evidence/search", json=search_request())
    assert response.status_code == 413 and "records" not in response.json()


@pytest.mark.asyncio
async def test_new_contract_failure_does_not_disable_legacy_read(db_session, tmp_path, monkeypatch):
    assert history_contract_ready()
    archived(db_session, "a")
    await db_session.commit()
    monkeypatch.setattr("app.evidence_api.history_contract_ready", lambda: False)
    client, _ = api_client(db_session, tmp_path)
    async with client:
        new = await client.post("/internal/evidence/search", json=search_request())
        old = await client.post("/internal/evidence/read", json={"channel": CHANNEL, "message_id": "7"})
    assert new.status_code == 503 and old.status_code == 200


@pytest.mark.asyncio
async def test_rebinding_scope_invalidates_locator_and_cursor_without_leaking_new_body(db_session, tmp_path):
    for key in ("a", "b", "c"):
        archived(db_session, key, message_id=key)
    archived(db_session, "secret", robot="account-b", message_id="secret")
    await db_session.commit()
    client, settings = api_client(db_session, tmp_path)
    async with client:
        page = (await client.post("/internal/evidence/search", json=search_request())).json()
        config = configuration()
        config["mappings"][0]["robot_id"] = "account-b"
        settings.evidence_read_config = json.dumps(config)
        read = await client.post("/internal/evidence/read-locator", json={"schema_version": 1,
            "channel": CHANNEL, **check_item(page["records"][0]), "before": 10, "after": 10})
        assert read.status_code == 200 and read.json()["status"] == "missing" and read.json()["target"] is None
        cursor = await client.post("/internal/evidence/search", json=search_request(cursor=page["next_cursor"]))
        assert cursor.status_code == 422
