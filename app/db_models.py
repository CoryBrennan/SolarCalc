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


def _new_commissioning_unit_id() -> str:
    return f"cxu-{uuid.uuid4().hex[:12]}"


def _new_torque_point_id() -> str:
    return f"trq-{uuid.uuid4().hex[:12]}"


def _new_wire_item_id() -> str:
    return f"wir-{uuid.uuid4().hex[:12]}"


def _new_commissioning_photo_id() -> str:
    return f"cxp-{uuid.uuid4().hex[:12]}"


class CommissioningUnit(SQLModel, table=True):
    """One physical piece of equipment going through field commissioning --
    an inverter, switchboard, or load center. Ties torque checks, wire
    inspections, and photos to the equipment tag the rest of the app already
    uses (Changeset.target_tag, PVCase BOM tags), so a field crew's QC record
    for "INV-04" lines up with the same tag the design and CAD side use.

    status is derived, not hand-set -- app/commissioning_calc.summarize_unit
    recomputes it from the unit's own torque points + wire items every time
    one of them changes (see commissioning_routes), the same "status reflects
    child state" approach Changeset/IngestionJob use for their own status
    columns.
    """

    id: str = Field(default_factory=_new_commissioning_unit_id, primary_key=True)
    equipment_type: str = Field(index=True)  # "inverter" | "switchboard" | "load_center"
    tag: str = Field(index=True)  # e.g. "INV-04", "SWBD-1", "LC-2"
    manufacturer: str | None = Field(default=None)
    model: str | None = Field(default=None)
    status: str = Field(default="not_started", index=True)  # not_started | in_progress | complete | needs_attention
    commissioned_by: str | None = Field(default=None)
    commissioned_at: datetime | None = Field(default=None)
    notes: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class TorquePoint(SQLModel, table=True):
    """One torque-critical connection on a CommissioningUnit (e.g. "AC output
    lugs", "DC input terminals", "busbar splice", "ground lug").

    design_torque_min/max are entered by the engineer off the equipment's own
    install manual/torque chart -- this app deliberately does not ship
    invented manufacturer torque values (UL 486A/B specs vary by lug and
    equipment, and a wrong hardcoded number is worse than none, the same
    reasoning wire_cost_calc.py uses for pricing being an explicit
    placeholder rather than a fabricated "real" figure). Both are nullable so
    a connection point can be logged before its design spec is known; a
    result can only be pass/fail once both the design band and a measured
    reading exist (see commissioning_calc.score_torque_point).
    """

    id: str = Field(default_factory=_new_torque_point_id, primary_key=True)
    unit_id: str = Field(foreign_key="commissioningunit.id", index=True)
    connection_label: str  # "AC output lugs (L1)", "Neutral bar", etc.
    design_torque_min: float | None = Field(default=None)
    design_torque_max: float | None = Field(default=None)
    torque_unit: str = Field(default="ft-lb")  # "ft-lb" | "in-lb" | "Nm"
    measured_torque_value: float | None = Field(default=None)
    wrench_id: str | None = Field(default=None)  # cal-tracked tool ID
    tech_initials: str | None = Field(default=None)
    result: str = Field(default="pending", index=True)  # pending | pass | fail
    checked_at: datetime | None = Field(default=None)
    created_at: datetime = Field(default_factory=_now)


class WireInspectionItem(SQLModel, table=True):
    """One inspected conductor/circuit on a CommissioningUnit.
    design_conductor is the as-designed size/material (copied in by the
    engineer from whatever the Ampacity (13) or Raceway (24) panel sized
    that circuit to); as_built_conductor is what the field crew actually
    finds landed. A mismatch here is the same kind of design-vs-as-built
    divergence the GPS/pile (app/gps_validation_calc.py) and Fluke IV-curve
    (app/fluke_validate.py) panels already catch for other equipment
    classes -- see commissioning_calc.score_wire_item for the pass/fail
    rule.
    """

    id: str = Field(default_factory=_new_wire_item_id, primary_key=True)
    unit_id: str = Field(foreign_key="commissioningunit.id", index=True)
    circuit_label: str  # "DC input MPPT1", "AC output", "EGC", etc.
    design_conductor: str  # e.g. "500 kcmil CU"
    as_built_conductor: str | None = Field(default=None)
    termination_ok: bool | None = Field(default=None)
    labeling_ok: bool | None = Field(default=None)
    continuity_ok: bool | None = Field(default=None)
    insulation_resistance_megohm: float | None = Field(default=None)
    notes: str | None = Field(default=None)
    result: str = Field(default="pending", index=True)  # pending | pass | fail
    checked_at: datetime | None = Field(default=None)
    created_at: datetime = Field(default_factory=_now)


class CommissioningPhoto(SQLModel, table=True):
    """A field photo attached to a CommissioningUnit -- nameplate, a torque
    stripe/paint-pen mark, a wiring close-up, etc. Stored as a DB blob
    (matching IngestionJob.file_data's approach in app/catalog_models.py)
    rather than on local disk, since the hosted instance's filesystem is
    ephemeral -- see app/db.py's DATABASE_URL docstring for the same
    persistent-vs-ephemeral concern that motivates that pattern.
    """

    id: str = Field(default_factory=_new_commissioning_photo_id, primary_key=True)
    unit_id: str = Field(foreign_key="commissioningunit.id", index=True)
    category: str = Field(default="general", index=True)  # torque | wiring | nameplate | general
    torque_point_id: str | None = Field(default=None, foreign_key="torquepoint.id")
    wire_item_id: str | None = Field(default=None, foreign_key="wireinspectionitem.id")
    caption: str | None = Field(default=None)
    filename: str
    content_type: str
    file_data: bytes
    uploaded_at: datetime = Field(default_factory=_now)


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
