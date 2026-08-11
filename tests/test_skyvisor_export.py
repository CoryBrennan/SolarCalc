"""Unit tests for the asset-map CSV builder, using synthetic rows since no
real SkyVisor export/import format is confirmed yet."""

from __future__ import annotations

from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.db_models import ArrayTable, ModuleAsset, StringAsset
from app.skyvisor_export import build_asset_map_csv, build_asset_map_rows


def _make_session() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def test_build_asset_map_rows_empty_when_no_modules():
    session = _make_session()
    assert build_asset_map_rows(session) == []
    assert build_asset_map_csv(session) == ""


def test_build_asset_map_rows_joins_table_and_string_fields():
    session = _make_session()
    table = ArrayTable(block_tag="TBL-1", row_label="Row 1", latitude=40.0, longitude=-89.0)
    string = StringAsset(inverter_tag="INV-1", combiner_index=2, module_count=28, module_sku="720")
    session.add(table)
    session.add(string)
    session.commit()
    session.refresh(table)
    session.refresh(string)

    module = ModuleAsset(
        array_table_id=table.id,
        string_asset_id=string.id,
        position_in_string=5,
        module_sku="720",
        latitude=40.0001,
        longitude=-89.0001,
    )
    session.add(module)
    session.commit()

    rows = build_asset_map_rows(session)

    assert len(rows) == 1
    row = rows[0]
    assert row["block_tag"] == "TBL-1"
    assert row["row_label"] == "Row 1"
    assert row["inverter_tag"] == "INV-1"
    assert row["combiner_index"] == 2
    assert row["position_in_string"] == 5
    assert row["latitude"] == 40.0001


def test_build_asset_map_csv_round_trips_through_csv_module():
    import csv
    import io

    session = _make_session()
    table = ArrayTable(block_tag="TBL-1", row_label="Row 1", latitude=40.0, longitude=-89.0)
    string = StringAsset(inverter_tag="INV-1", module_count=28, module_sku="720")
    session.add(table)
    session.add(string)
    session.commit()
    session.refresh(table)
    session.refresh(string)
    session.add(
        ModuleAsset(
            array_table_id=table.id, string_asset_id=string.id, position_in_string=1,
            module_sku="720", latitude=40.0, longitude=-89.0,
        )
    )
    session.commit()

    csv_text = build_asset_map_csv(session)
    reader = csv.DictReader(io.StringIO(csv_text))
    parsed_rows = list(reader)

    assert len(parsed_rows) == 1
    assert parsed_rows[0]["block_tag"] == "TBL-1"
