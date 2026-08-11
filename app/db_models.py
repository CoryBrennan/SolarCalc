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


class ArrayTable(SQLModel, table=True):
    """One physical mounting structure (fixed-tilt table or tracker row) —
    the spatial unit SkyVisor's flight plan and imagery are organized
    around. Anchor point only; individual modules hang off this via
    ModuleAsset."""

    id: str = Field(default_factory=lambda: f"tbl-{uuid.uuid4().hex[:12]}", primary_key=True)
    block_tag: str = Field(index=True)  # matches Changeset.target_tag conventions
    row_label: str  # e.g. "Row 12", site-drawing designation
    latitude: float
    longitude: float
    azimuth_deg: float | None = Field(default=None)
    created_at: datetime = Field(default_factory=_now)


class StringAsset(SQLModel, table=True):
    """One PV string — the electrical grouping (combiner/inverter/MPPT
    membership) already carried in ProjectInput. Individual modules
    (ModuleAsset) belong to exactly one string; some SkyVisor anomaly types
    (e.g. a full string outage) resolve at this level rather than to a
    single module."""

    id: str = Field(default_factory=lambda: f"str-{uuid.uuid4().hex[:12]}", primary_key=True)
    inverter_tag: str = Field(index=True)
    combiner_index: int | None = Field(default=None)
    mppt_index: int | None = Field(default=None)
    module_count: int
    module_sku: str
    created_at: datetime = Field(default_factory=_now)


class ModuleAsset(SQLModel, table=True):
    """One physical module — the granularity SkyVisor thermal anomalies
    resolve to. Carries its own lat/long (rather than inheriting the
    table's) because SkyVisor's geo-tagged detections need a per-module
    coordinate to nearest-neighbor match against, and modules span physical
    space across a table. position_in_string is the ordinal a technician
    would use to locate it on a site walk-down (e.g. "module 14 of 28,
    counting from the combiner end")."""

    id: str = Field(default_factory=lambda: f"mod-{uuid.uuid4().hex[:12]}", primary_key=True)
    array_table_id: str = Field(foreign_key="arraytable.id", index=True)
    string_asset_id: str = Field(foreign_key="stringasset.id", index=True)
    position_in_string: int
    module_sku: str
    latitude: float
    longitude: float
    created_at: datetime = Field(default_factory=_now)


class SkyvisorImportBatch(SQLModel, table=True):
    """One ingested SkyVisor inspection export. Mirrors the Changeset
    pattern: queryable status columns + raw payload as JSON, since we don't
    know SkyVisor's exact export schema yet and don't want to model it
    field-by-field until it's confirmed."""

    id: str = Field(default_factory=lambda: f"sky-{uuid.uuid4().hex[:12]}", primary_key=True)
    flight_date: datetime
    source_filename: str
    status: str = Field(default="pending", index=True)  # pending | matched | needs_attention
    raw_payload: str  # JSON-encoded, as-received from SkyVisor export
    imported_at: datetime = Field(default_factory=_now)


class SkyvisorAnomaly(SQLModel, table=True):
    """One flagged defect from a SkyvisorImportBatch, reconciled against a
    ModuleAsset where a coordinate match was found. module_asset_id is
    nullable both because matching can fail (drift, unmapped table —
    surfaces for manual review rather than silently dropping the anomaly)
    and because some anomaly types are string-level, not module-level, in
    which case only string_asset_id is set."""

    id: str = Field(default_factory=lambda: f"anom-{uuid.uuid4().hex[:12]}", primary_key=True)
    import_batch_id: str = Field(foreign_key="skyvisorimportbatch.id", index=True)
    module_asset_id: str | None = Field(default=None, foreign_key="moduleasset.id", index=True)
    string_asset_id: str | None = Field(default=None, foreign_key="stringasset.id", index=True)
    anomaly_type: str = Field(index=True)  # hotspot | string_outage | soiling | wiring_fault
    severity: str  # low | medium | high, as reported by SkyVisor
    delta_t_c: float | None = Field(default=None)
    latitude: float
    longitude: float
    image_url: str | None = Field(default=None)
    resolution_status: str = Field(default="open", index=True)  # open | resolved | false_positive
    created_at: datetime = Field(default_factory=_now)
