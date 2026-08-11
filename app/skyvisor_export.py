"""Builds the module-level asset map SkyVisor needs to geolocate thermal
anomalies against a specific physical module. Column shape below is this
app's own choice, not a confirmed SkyVisor import format -- no public API
docs exist yet, so swap column names once SkyVisor's actual import spec is
confirmed (same caveat as etap_export's "not what ETAP actually accepts").
"""

from __future__ import annotations

import csv
import io

from sqlmodel import Session, select

from app.db_models import ArrayTable, ModuleAsset, StringAsset


def build_asset_map_rows(session: Session) -> list[dict]:
    tables_by_id = {t.id: t for t in session.exec(select(ArrayTable)).all()}
    strings_by_id = {s.id: s for s in session.exec(select(StringAsset)).all()}

    rows = []
    for module in session.exec(select(ModuleAsset)).all():
        table = tables_by_id.get(module.array_table_id)
        string = strings_by_id.get(module.string_asset_id)
        rows.append(
            {
                "module_id": module.id,
                "latitude": module.latitude,
                "longitude": module.longitude,
                "module_sku": module.module_sku,
                "position_in_string": module.position_in_string,
                "string_id": module.string_asset_id,
                "inverter_tag": string.inverter_tag if string else None,
                "combiner_index": string.combiner_index if string else None,
                "table_id": module.array_table_id,
                "block_tag": table.block_tag if table else None,
                "row_label": table.row_label if table else None,
            }
        )
    return rows


def build_asset_map_csv(session: Session) -> str:
    rows = build_asset_map_rows(session)
    if not rows:
        return ""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()
