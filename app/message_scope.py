"""Single source of truth for "which messages may this account see".

A message row is global: ``msg_hash`` is derived from platform, room, sender and
event identity, deliberately *not* from the account that captured it (see
``app.services.message_service.message_hash``). Per-account visibility lives in
two places instead, and a query that consults only one of them is wrong:

* ``robot_messages`` -- written by the realtime NapCat path, one row per
  (account, message);
* ``message_source_records -> import_sources.account_id`` -- written by the
  QQNT import path, which has no ``robot_messages`` row at all.

Checking only ``robot_messages`` hides every imported-only conversation from the
account (the defect fixed in ``0f1b2dd``). Checking neither leaks other
accounts' content, because OneBot message ids are small per-connection integers
that collide across accounts as a matter of course.

The join and the WHERE clause must always be applied together, which is what
:func:`apply_robot_message_scope` exists to guarantee -- applying only the WHERE
multiplies a message by its ``robot_messages`` row count and silently eats the
caller's ``limit``.
"""

from __future__ import annotations

from sqlalchemy import and_, or_
from sqlalchemy.orm import aliased
from sqlalchemy.sql import Select, select

from app.models import ImportSource, Message, MessageSourceRecord, RobotMessage


def robot_message_join(robot_id: str):
    """ON clause for the ``RobotMessage`` outer join.

    The robot filter has to live in the JOIN, not only in the WHERE: a message
    visible to several accounts has one ``RobotMessage`` row per account, so an
    unrestricted join multiplies the message by that count. Rows that also
    satisfy the imported-source branch of :func:`robot_message_scope` then pass
    the WHERE more than once and the message is returned repeatedly.
    """
    return and_(RobotMessage.msg_hash == Message.msg_hash, RobotMessage.robot_id == robot_id)


def robot_message_scope(robot_id: str):
    """WHERE clause selecting the messages ``robot_id`` is allowed to see.

    Correlated on ``Message``, so the statement's FROM must already contain it.
    Pair with :func:`robot_message_join`, or use
    :func:`apply_robot_message_scope` which applies both.
    """
    source_record = aliased(MessageSourceRecord)
    import_source = aliased(ImportSource)
    imported_message_scope = (
        select(source_record.id)
        .join(import_source, import_source.id == source_record.source_id)
        .where(
            source_record.msg_hash == Message.msg_hash,
            import_source.account_id == robot_id,
            import_source.platform == Message.platform,
        )
        .exists()
    )
    return or_(RobotMessage.robot_id == robot_id, imported_message_scope)


def apply_robot_message_scope(stmt: Select, robot_id: str) -> Select:
    """Restrict ``stmt`` to the messages ``robot_id`` may see.

    Applies the outer join and the WHERE clause together; prefer this over
    calling the two helpers separately, since applying only one of them is the
    failure mode both of them exist to prevent.
    """
    return stmt.outerjoin(RobotMessage, robot_message_join(robot_id)).where(robot_message_scope(robot_id))
