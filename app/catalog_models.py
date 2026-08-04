"""Equipment catalog persistence — the server-side counterpart to the HMI's
former client-only MODULE_SKUS/inverter objects. A "version" is a full
catalog record (not a diff): every field the model has, each carrying its
own confidence/source/flag, so approving one is promoting a complete,
traceable snapshot rather than patching a shared row in place.

Multiple `active` versions can exist for the same (equipment_type,
manufacturer, model) — this is a version history, not a single slot.
CatalogDefault is the separate, explicit record of which version new
projects actually use, so promoting a new version never silently changes
what's already been assumed elsewhere (see catalog_routes.approve_version).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_catalog_version_id() -> str:
    return f"catv-{uuid.uuid4().hex[:12]}"


def _new_ingestion_job_id() -> str:
    return f"ing-{uuid.uuid4().hex[:12]}"


class CatalogVersion(SQLModel, table=True):
    """One version of one equipment model's catalog record.

    status: "pending_review" -> "active" (approve) or "rejected" (reject).
    Rejected rows are kept, not deleted, for audit — same spirit as
    Changeset never hard-deleting a failed row.

    fields: JSON-encoded dict[str, FieldValue] (see extraction_schema.py),
    keyed by the canonical field names from datasheet-extraction-agent-prompt.md
    (module_fields/inverter_fields, flattened).
    """

    id: str = Field(default_factory=_new_catalog_version_id, primary_key=True)
    equipment_type: str = Field(index=True)  # "module" | "inverter"
    manufacturer: str = Field(index=True)
    model: str = Field(index=True)
    status: str = Field(default="pending_review", index=True)  # pending_review | active | rejected
    fields: str  # JSON-encoded dict[str, FieldValue]
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    approved_at: datetime | None = Field(default=None)


class CatalogDefault(SQLModel, table=True):
    """Which CatalogVersion new projects use for a given
    (equipment_type, manufacturer, model). Only ever changed by an explicit
    engineer action (catalog_routes.set_default) — approving a version does
    not repoint this unless no default exists yet for that model."""

    key: str = Field(primary_key=True)  # f"{equipment_type}|{manufacturer}|{model}"
    equipment_type: str = Field(index=True)
    manufacturer: str = Field(index=True)
    model: str = Field(index=True)
    version_id: str
    updated_at: datetime = Field(default_factory=_now)


class IngestionJob(SQLModel, table=True):
    """One upload's journey through the pipeline — the changeset-style
    retry/status record for the extraction-agent call. See
    app/extraction_agent.py for the retry-once-then-needs_attention policy
    this status field reflects.
    """

    id: str = Field(default_factory=_new_ingestion_job_id, primary_key=True)
    filename: str
    file_type: str  # "pdf" | "pan" | "ond"
    equipment_type: str  # "module" | "inverter"
    manufacturer_hint: str | None = Field(default=None)
    model_hint: str | None = Field(default=None)
    status: str = Field(default="pending", index=True)  # pending | processing | needs_attention | merged
    retry_count: int = Field(default=0)
    last_error: str | None = Field(default=None)
    catalog_version_id: str | None = Field(default=None)
    # Retained so a manual /retry can re-run the same upload without asking
    # the engineer to re-attach the file.
    file_data: bytes | None = Field(default=None)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
