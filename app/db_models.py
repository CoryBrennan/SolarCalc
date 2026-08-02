"""Persisted tables — the first real data layer this backend has had.
Everything before this phase was stateless (request in, computed response
out); the changeset system needs actual persistence to decouple "the
project data changed" from "AutoCAD picked up the change."
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_changeset_id() -> str:
    return f"cs-{uuid.uuid4().hex[:12]}"


class Project(SQLModel, table=True):
    """One stored project. Single implicit "default" row for now — no
    multi-project/multi-user support yet, same scope limit as the rest of
    this phase."""

    id: str = Field(default="default", primary_key=True)
    data: str  # JSON-encoded ProjectInput
    updated_at: datetime = Field(default_factory=_now)


class Changeset(SQLModel, table=True):
    """A pending (or resolved) unit of CAD-generator work, matching the
    changeset shape described in the block generator specs:
    {changeset_id, operation, target_tag, block_type, config}.

    status: "pending" -> "applied", or "pending" -> "needs_attention" after
    5 failed attempts (retry_policy). "needs_attention" requires an explicit
    /retry to go back to "pending".
    """

    id: str = Field(default_factory=_new_changeset_id, primary_key=True)
    target_tag: str = Field(index=True)
    block_type: str = Field(index=True)
    operation: str  # "regenerate" | "attribute_update"
    config: str  # JSON-encoded generator input contract
    status: str = Field(default="pending", index=True)  # pending | applied | needs_attention
    retry_count: int = Field(default=0)
    last_error: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
