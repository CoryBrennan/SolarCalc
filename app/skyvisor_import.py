"""Ingests a SkyVisor inspection anomaly export and reconciles each flagged
defect against the ModuleAsset asset map (app/skyvisor_export.py). Column
names in ParsedAnomaly/parse_skyvisor_csv are a placeholder shape -- no
public SkyVisor API/export docs exist yet, so update parse_skyvisor_csv to
match field-for-field once a real SkyVisor export sample is available.
"""

from __future__ import annotations

import csv
import io
import math
from datetime import datetime

from pydantic import BaseModel
from sqlmodel import Session, select

from app.db_models import ModuleAsset, SkyvisorAnomaly, SkyvisorImportBatch

EARTH_RADIUS_M = 6_371_000

# Drone geotags typically run 1-3 m accurate (better with RTK-equipped
# drones); module pitch within a table can be under 2 m, so this is a
# starting point, not a validated constant -- tighten it once real SkyVisor
# exports show how precise their coordinates actually are, and watch for
# ambiguous matches in densely packed tables.
DEFAULT_MATCH_TOLERANCE_M = 3.0


class ParsedAnomaly(BaseModel):
    anomaly_type: str
    severity: str
    latitude: float
    longitude: float
    delta_t_c: float | None = None
    image_url: str | None = None


def parse_skyvisor_csv(csv_text: str) -> list[ParsedAnomaly]:
    reader = csv.DictReader(io.StringIO(csv_text))
    return [
        ParsedAnomaly(
            anomaly_type=row["anomaly_type"],
            severity=row["severity"],
            latitude=float(row["latitude"]),
            longitude=float(row["longitude"]),
            delta_t_c=float(row["delta_t_c"]) if row.get("delta_t_c") else None,
            image_url=row.get("image_url") or None,
        )
        for row in reader
    ]


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dlambda = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def _nearest_module(modules: list[ModuleAsset], lat: float, lon: float) -> tuple[ModuleAsset | None, float]:
    best, best_dist = None, math.inf
    for m in modules:
        d = _haversine_m(lat, lon, m.latitude, m.longitude)
        if d < best_dist:
            best, best_dist = m, d
    return best, best_dist


def import_batch(
    session: Session,
    csv_text: str,
    source_filename: str,
    flight_date: datetime,
    tolerance_m: float = DEFAULT_MATCH_TOLERANCE_M,
) -> SkyvisorImportBatch:
    parsed = parse_skyvisor_csv(csv_text)
    batch = SkyvisorImportBatch(flight_date=flight_date, source_filename=source_filename, raw_payload=csv_text)
    session.add(batch)
    session.commit()
    session.refresh(batch)

    modules = list(session.exec(select(ModuleAsset)).all())
    any_unmatched = False

    for anomaly in parsed:
        module, dist_m = _nearest_module(modules, anomaly.latitude, anomaly.longitude)
        matched = module if module is not None and dist_m <= tolerance_m else None
        any_unmatched = any_unmatched or matched is None

        session.add(
            SkyvisorAnomaly(
                import_batch_id=batch.id,
                module_asset_id=matched.id if matched else None,
                string_asset_id=matched.string_asset_id if matched else None,
                anomaly_type=anomaly.anomaly_type,
                severity=anomaly.severity,
                delta_t_c=anomaly.delta_t_c,
                latitude=anomaly.latitude,
                longitude=anomaly.longitude,
                image_url=anomaly.image_url,
            )
        )

    batch.status = "needs_attention" if any_unmatched else "matched"
    session.add(batch)
    session.commit()
    session.refresh(batch)
    return batch
