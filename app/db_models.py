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


def _new_inspection_item_id() -> str:
    return f"insp-{uuid.uuid4().hex[:12]}"


def _new_electrical_reading_id() -> str:
    return f"elec-{uuid.uuid4().hex[:12]}"


def _new_review_set_id() -> str:
    return f"rev-{uuid.uuid4().hex[:12]}"


def _new_submission_id() -> str:
    return f"sub-{uuid.uuid4().hex[:12]}"


def _new_markup_item_id() -> str:
    return f"mki-{uuid.uuid4().hex[:12]}"


def _new_markup_audit_id() -> str:
    return f"mka-{uuid.uuid4().hex[:12]}"


class CommissioningUnit(SQLModel, table=True):
    """One physical piece of equipment going through field commissioning --
    an inverter, switchboard, or load center. Ties torque checks, wire
    inspections, and photos to the equipment tag the rest of the app already
    uses (Changeset.target_tag, PVCase BOM tags), so a field crew's QC record
    for "INV-04" lines up with the same tag the design and CAD side use.

    status is derived, not hand-set -- app/commissioning_calc.summarize_unit
    recomputes it from the unit's own torque points, visual/mechanical
    InspectionItems, wire items, and ElectricalReadings every time one of
    them changes (see commissioning_routes), the same "status reflects child
    state" approach Changeset/IngestionJob use for their own status columns.
    The four child types split into the two groups the HMI panel shows:
    torque + InspectionItem = Visual & Mechanical Inspection, WireInspectionItem
    + ElectricalReading = Electrical Inspection.
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


class InspectionItem(SQLModel, table=True):
    """One visual/mechanical checklist item on a CommissioningUnit --
    enclosure condition, nameplate legibility, conduit entries sealed,
    mounting hardware secure, required labels/placards present, etc.
    Unlike TorquePoint this has no numeric design band; a technician just
    marks it pass/fail directly (see commissioning_routes.InspectionItemUpdate),
    since "is the enclosure dented" isn't a measurement to grade against a
    spec the way torque or a voltage reading is.
    """

    id: str = Field(default_factory=_new_inspection_item_id, primary_key=True)
    unit_id: str = Field(foreign_key="commissioningunit.id", index=True)
    label: str  # "Enclosure condition / no physical damage", etc.
    notes: str | None = Field(default=None)
    result: str = Field(default="pending", index=True)  # pending | pass | fail
    checked_at: datetime | None = Field(default=None)
    created_at: datetime = Field(default_factory=_now)


class ElectricalReading(SQLModel, table=True):
    """One electrical measurement on a CommissioningUnit -- AC line-to-line/
    line-to-neutral voltage, DC string/combiner voltage, etc. Same
    design-band-vs-measured shape as TorquePoint (see
    commissioning_calc.score_measurement_band, which both call), but
    design_min/max here can also be derived automatically from the project's
    own design data (app/commissioning_routes.auto_populate_electrical_readings
    uses ProjectInput.inverter.nominal_ac_voltage_v +/- a tolerance for
    inverter/switchboard units) rather than always requiring manual entry
    the way a torque spec does -- unlike a lug's torque rating, nominal
    system voltage already lives in this app's own design data.
    """

    id: str = Field(default_factory=_new_electrical_reading_id, primary_key=True)
    unit_id: str = Field(foreign_key="commissioningunit.id", index=True)
    label: str  # "AC output L1-L2", "DC combiner 1 Voc", etc.
    reading_type: str = Field(default="ac_voltage")  # ac_voltage | dc_voltage | other
    design_min: float | None = Field(default=None)
    design_max: float | None = Field(default=None)
    unit: str = Field(default="VAC")  # "VAC" | "VDC"
    measured_value: float | None = Field(default=None)
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
    category: str = Field(default="visual_mechanical", index=True)  # visual_mechanical | electrical
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


class PlanReviewSet(SQLModel, table=True):
    """One drawing set out for review — the thing reviewers mark up and the
    thing that eventually gets approved.

    Holds the *clean* master set the copies were all issued from. Consolidation
    clones every reviewer's markups onto this, so it has to be the unmarked
    original; sending a marked-up copy as the master would leave those markups
    unattributed and unlayered (see app/bluebeam_consolidate.consolidate).

    Two ways in, matching how the rest of this codebase handles big files:
    `file_data` for an HMI upload, or `source_path` for a file already sitting
    in the Dropbox-synced project folder (the same local-path approach
    pvcase_bom_import and fluke_export_import take, and the only practical one
    for a full plan set, which routinely runs past what belongs in a DB row).
    Exactly one of the two is set.

    status is a real gate, not a label: it can only reach "approved" through
    bluebeam_review.approval_gate, which refuses while any markup is still
    open, accepted-but-not-drawn, or deferred.
    """

    id: str = Field(default_factory=_new_review_set_id, primary_key=True)
    name: str = Field(index=True)
    revision_label: str | None = Field(default=None)  # "IFC Rev C", "90% CD", ...
    discipline: str | None = Field(default=None)  # "Electrical", "Civil", ...
    master_filename: str | None = Field(default=None)
    master_source_path: str | None = Field(default=None)
    master_data: bytes | None = Field(default=None)
    page_count: int | None = Field(default=None)
    current_round: int = Field(default=0)
    status: str = Field(default="open", index=True)  # open | consolidated | approved
    consolidated_data: bytes | None = Field(default=None)
    consolidated_at: datetime | None = Field(default=None)
    approved_by: str | None = Field(default=None)
    approved_at: datetime | None = Field(default=None)
    approval_note: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class MarkupSubmission(SQLModel, table=True):
    """One reviewer's marked-up copy of a PlanReviewSet, as handed back.

    `label` is what the reviewer is called throughout — it becomes the PDF
    layer name in the consolidated set and the source column in the review
    table, so it wants to be a person or firm, not a filename.

    `round_number` is what makes "trackable changes" work: uploading the same
    reviewer's set again at round N+1 does not overwrite round N, it sits
    beside it, and bluebeam_review.diff_rounds reports what moved between the
    two. Nothing is ever replaced in place.
    """

    id: str = Field(default_factory=_new_submission_id, primary_key=True)
    review_set_id: str = Field(foreign_key="planreviewset.id", index=True)
    label: str = Field(index=True)
    round_number: int = Field(default=1, index=True)
    filename: str | None = Field(default=None)
    source_path: str | None = Field(default=None)
    file_data: bytes | None = Field(default=None)
    page_count: int | None = Field(default=None)
    markup_count: int = Field(default=0)
    uploaded_by: str | None = Field(default=None)
    uploaded_at: datetime = Field(default_factory=_now)


class MarkupItem(SQLModel, table=True):
    """One markup, lifted out of a submission and given a disposition.

    The PDF-side columns (page/subtype/author/contents/rect/revu_status) are a
    snapshot of what the annotation said when it was imported — deliberately
    copied rather than re-read on demand, so the review log still reads
    correctly after someone edits or deletes the markup in their own copy.

    `disposition` is the app's authoritative decision and is distinct from
    `revu_status`, which is whatever the Markups List status column happened
    to say. See app/bluebeam_review's module docstring for why those are two
    columns and not one.
    """

    id: str = Field(default_factory=_new_markup_item_id, primary_key=True)
    review_set_id: str = Field(foreign_key="planreviewset.id", index=True)
    submission_id: str = Field(foreign_key="markupsubmission.id", index=True)
    round_number: int = Field(default=1, index=True)
    markup_key: str = Field(index=True)  # /NM GUID, or a content-hash fallback
    fingerprint: str = Field(default="")  # change detection across rounds
    source_label: str | None = Field(default=None, index=True)
    # Other reviewers whose copy carried this same markup. A comment from a
    # prior round is inherited by every copy issued from that set, so the same
    # /NM arrives several times in one round; it is stored once (mirroring how
    # bluebeam_consolidate merges it once) with the extra sources recorded here,
    # rather than as duplicate rows that would each need dispositioning.
    also_from_json: str | None = Field(default=None)
    page: int = Field(default=0, index=True)
    subtype: str | None = Field(default=None)
    author: str | None = Field(default=None)
    subject: str | None = Field(default=None)
    contents: str | None = Field(default=None)
    colour: str | None = Field(default=None)
    rect_json: str | None = Field(default=None)
    custom_json: str | None = Field(default=None)
    revu_status: str | None = Field(default=None)  # advisory, from the /IRT reply chain
    markup_created_at: datetime | None = Field(default=None)
    markup_modified_at: datetime | None = Field(default=None)
    change: str | None = Field(default=None, index=True)  # added|modified|unchanged|withdrawn
    equipment_tag: str | None = Field(default=None, index=True)  # from /SolarCalcTag when present
    disposition: str = Field(default="open", index=True)
    response: str | None = Field(default=None)
    assigned_to: str | None = Field(default=None, index=True)
    resolved_by: str | None = Field(default=None)
    resolved_at: datetime | None = Field(default=None)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class MarkupAudit(SQLModel, table=True):
    """Append-only record of every disposition change on a MarkupItem.

    This is the thing a PDF cannot give you, and the reason disposition is
    tracked in the app at all: Revu's status column holds only the current
    value, overwritable by anyone with the file and with no record of who
    changed it or when. A review that gates a drawing revision has to be able
    to answer "who accepted this, and when" months later.
    """

    id: str = Field(default_factory=_new_markup_audit_id, primary_key=True)
    markup_item_id: str = Field(foreign_key="markupitem.id", index=True)
    from_disposition: str | None = Field(default=None)
    to_disposition: str = Field(index=True)
    actor: str | None = Field(default=None)
    note: str | None = Field(default=None)
    at: datetime = Field(default_factory=_now)
