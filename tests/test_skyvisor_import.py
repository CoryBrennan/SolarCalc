"""Unit-level tests for the CSV parser and nearest-neighbor matcher, using
synthetic data since no real SkyVisor export exists yet (see
app/skyvisor_import.py module docstring)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.db_models import ArrayTable, ModuleAsset, StringAsset
from app.skyvisor_import import (
    DEFAULT_MATCH_TOLERANCE_M,
    _haversine_m,
    import_batch,
    parse_skyvisor_csv,
)

CSV_HEADER = "anomaly_type,severity,latitude,longitude,delta_t_c,image_url"


def _make_session() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _seed_one_module(session: Session, latitude: float = 40.0, longitude: float = -89.0) -> ModuleAsset:
    table = ArrayTable(block_tag="TBL-1", row_label="Row 1", latitude=latitude, longitude=longitude)
    string = StringAsset(inverter_tag="INV-1", combiner_index=1, module_count=28, module_sku="720")
    session.add(table)
    session.add(string)
    session.commit()
    session.refresh(table)
    session.refresh(string)

    module = ModuleAsset(
        array_table_id=table.id,
        string_asset_id=string.id,
        position_in_string=1,
        module_sku="720",
        latitude=latitude,
        longitude=longitude,
    )
    session.add(module)
    session.commit()
    session.refresh(module)
    return module


def test_parse_skyvisor_csv_reads_required_and_optional_columns():
    csv_text = "\n".join([CSV_HEADER, "hotspot,high,40.0,-89.0,18.5,https://example.com/img.jpg"])

    parsed = parse_skyvisor_csv(csv_text)

    assert len(parsed) == 1
    assert parsed[0].anomaly_type == "hotspot"
    assert parsed[0].severity == "high"
    assert parsed[0].delta_t_c == 18.5
    assert parsed[0].image_url == "https://example.com/img.jpg"


def test_parse_skyvisor_csv_handles_missing_optional_fields():
    csv_text = "\n".join([CSV_HEADER, "soiling,low,40.0,-89.0,,"])

    parsed = parse_skyvisor_csv(csv_text)

    assert parsed[0].delta_t_c is None
    assert parsed[0].image_url is None


def test_haversine_zero_distance_for_identical_points():
    assert _haversine_m(40.0, -89.0, 40.0, -89.0) == 0.0


def test_haversine_matches_known_short_distance():
    # ~0.0001 deg latitude at this latitude is roughly 11 m -- sanity check
    # the formula against a hand-computable ballpark, not an exact fixture.
    dist = _haversine_m(40.0, -89.0, 40.0001, -89.0)
    assert 9 < dist < 13


def test_import_batch_matches_anomaly_within_tolerance():
    session = _make_session()
    module = _seed_one_module(session)
    csv_text = "\n".join([CSV_HEADER, "hotspot,high,40.0,-89.0,20.0,"])

    batch = import_batch(session, csv_text, "flight1.csv", datetime(2026, 8, 1, tzinfo=timezone.utc))

    assert batch.status == "matched"
    from sqlmodel import select

    from app.db_models import SkyvisorAnomaly

    anomalies = session.exec(select(SkyvisorAnomaly).where(SkyvisorAnomaly.import_batch_id == batch.id)).all()
    assert len(anomalies) == 1
    assert anomalies[0].module_asset_id == module.id
    assert anomalies[0].string_asset_id == module.string_asset_id


def test_import_batch_flags_needs_attention_when_anomaly_too_far_from_any_module():
    session = _make_session()
    _seed_one_module(session)
    # ~1 degree away -- roughly 111 km, way outside DEFAULT_MATCH_TOLERANCE_M.
    csv_text = "\n".join([CSV_HEADER, "hotspot,high,41.0,-89.0,20.0,"])

    batch = import_batch(session, csv_text, "flight2.csv", datetime(2026, 8, 1, tzinfo=timezone.utc))

    assert batch.status == "needs_attention"
    from sqlmodel import select

    from app.db_models import SkyvisorAnomaly

    anomalies = session.exec(select(SkyvisorAnomaly).where(SkyvisorAnomaly.import_batch_id == batch.id)).all()
    assert anomalies[0].module_asset_id is None
    assert anomalies[0].string_asset_id is None


def test_import_batch_respects_custom_tolerance():
    session = _make_session()
    _seed_one_module(session)
    # ~11 m away (0.0001 deg lat) -- outside a tight 1 m tolerance.
    csv_text = "\n".join([CSV_HEADER, "hotspot,medium,40.0001,-89.0,10.0,"])

    batch = import_batch(
        session, csv_text, "flight3.csv", datetime(2026, 8, 1, tzinfo=timezone.utc), tolerance_m=1.0
    )

    assert batch.status == "needs_attention"


def test_default_tolerance_is_a_few_meters():
    assert 1.0 <= DEFAULT_MATCH_TOLERANCE_M <= 10.0
