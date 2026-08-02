"""Changeset lifecycle: create, list, mark applied/failed, and the retry
policy described in the block generator specs — auto-retry, needs_attention
after 5 failures, manual retry resets the counter for the next natural
pickup.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlmodel import Session, select

from app.db_models import Changeset

MAX_RETRIES_BEFORE_NEEDS_ATTENTION = 5


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_changeset(session: Session, target_tag: str, block_type: str, operation: str, config: dict) -> Changeset:
    changeset = Changeset(
        target_tag=target_tag,
        block_type=block_type,
        operation=operation,
        config=json.dumps(config, sort_keys=True),
    )
    session.add(changeset)
    session.commit()
    session.refresh(changeset)
    return changeset


def latest_changeset_for_tag(session: Session, target_tag: str) -> Changeset | None:
    statement = (
        select(Changeset)
        .where(Changeset.target_tag == target_tag)
        .order_by(Changeset.created_at.desc())
    )
    return session.exec(statement).first()


def refresh_changeset(
    session: Session, target_tag: str, block_type: str, operation: str, config: dict
) -> tuple[Changeset, bool]:
    """Creates a new pending changeset only if config differs from the most
    recent changeset for this tag — the "did anything actually change"
    check a real sync system needs, not just "always enqueue on every
    call." Returns (changeset, created)."""
    latest = latest_changeset_for_tag(session, target_tag)
    new_config_normalized = json.loads(json.dumps(config, sort_keys=True))
    if latest is not None and json.loads(latest.config) == new_config_normalized:
        return latest, False

    changeset = create_changeset(session, target_tag, block_type, operation, config)
    return changeset, True


def list_changesets(
    session: Session,
    status: str | None = None,
    block_type: str | None = None,
    target_tag: str | None = None,
) -> list[Changeset]:
    statement = select(Changeset)
    if status:
        statement = statement.where(Changeset.status == status)
    if block_type:
        statement = statement.where(Changeset.block_type == block_type)
    if target_tag:
        statement = statement.where(Changeset.target_tag == target_tag)
    statement = statement.order_by(Changeset.created_at)
    return list(session.exec(statement).all())


def get_changeset(session: Session, changeset_id: str) -> Changeset | None:
    return session.get(Changeset, changeset_id)


def mark_applied(session: Session, changeset: Changeset) -> Changeset:
    changeset.status = "applied"
    changeset.retry_count = 0
    changeset.last_error = None
    changeset.updated_at = _now()
    session.add(changeset)
    session.commit()
    session.refresh(changeset)
    return changeset


def mark_failed(session: Session, changeset: Changeset, error: str) -> Changeset:
    changeset.retry_count += 1
    changeset.last_error = error
    changeset.updated_at = _now()
    if changeset.retry_count >= MAX_RETRIES_BEFORE_NEEDS_ATTENTION:
        changeset.status = "needs_attention"
    session.add(changeset)
    session.commit()
    session.refresh(changeset)
    return changeset


def retry(session: Session, changeset: Changeset) -> Changeset:
    """Manual acknowledgment of a needs_attention changeset — the next
    natural pickup starts the retry count fresh."""
    changeset.status = "pending"
    changeset.retry_count = 0
    changeset.last_error = None
    changeset.updated_at = _now()
    session.add(changeset)
    session.commit()
    session.refresh(changeset)
    return changeset
