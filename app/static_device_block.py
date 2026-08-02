"""Generator input contract for static (non-regenerating) AutoCAD Electrical
blocks — the transformer and the three MV devices (recloser, GOAB switch,
meter). All four are "Tier 1" per spec: fixed geometry, catalog-driven
attributes, no layout algorithm, and use "attribute_update" changesets
rather than "regenerate" — the block gets placed once, then only its
attribute values get refreshed.

The transformer's SDS_FLAG/GROUNDING_TYPE are wired to the same
bonding_calc logic already used for Bonding & Grounding sizing — that's the
"SDS/winding validation already implemented in the app's data layer" the
transformer block spec assumes exists.
"""

from __future__ import annotations

from app.bonding_calc import is_separately_derived_system
from app.models import MvGoabConfig, MvMeterConfig, MvRecloserConfig, TransformerConfig

_WINDING_LABEL = {"delta": "Delta", "wye": "Wye (ungrounded)", "grounded_wye": "Grounded Wye"}
_GROUNDING_LABEL = {"delta": "N/A (delta, no neutral)", "wye": "Ungrounded", "grounded_wye": "Solidly grounded"}


def build_transformer_config(tag: str, transformer: TransformerConfig) -> dict:
    sds = is_separately_derived_system(transformer.primary_winding, transformer.secondary_winding)
    return {
        "tag": tag,
        "device_type": "transformer",
        "attributes": {
            "TAG1": tag,
            "KVA_RATING": f"{transformer.kva:g}",
            "PRIMARY_VOLTAGE": f"{transformer.primary_v:g}V",
            "SECONDARY_VOLTAGE": f"{transformer.secondary_v:g}V",
            "WINDING_CONFIG": f"{_WINDING_LABEL[transformer.primary_winding]}-{_WINDING_LABEL[transformer.secondary_winding]}",
            "IMPEDANCE": "TBD — requires catalog lookup",
            "SDS_FLAG": "Yes" if sds else "No",
            "GROUNDING_TYPE": _GROUNDING_LABEL[transformer.secondary_winding],
        },
    }


def build_mv_recloser_config(config: MvRecloserConfig) -> dict:
    return {
        "tag": config.tag,
        "device_type": "mv_recloser",
        "attributes": {
            "TAG1": config.tag,
            "VOLTAGE_CLASS": f"{config.voltage_class_kv:g}kV",
            "INTERRUPTING_RATING": f"{config.interrupting_rating_ka:g}kA",
            "CONTROL_TYPE": config.control_type,
        },
    }


def build_mv_goab_config(config: MvGoabConfig) -> dict:
    return {
        "tag": config.tag,
        "device_type": "mv_goab",
        "attributes": {
            "TAG1": config.tag,
            "VOLTAGE_CLASS": f"{config.voltage_class_kv:g}kV",
            "LOAD_BREAK_RATING": f"{config.load_break_rating_a:g}A",
        },
    }


def build_mv_meter_config(config: MvMeterConfig) -> dict:
    return {
        "tag": config.tag,
        "device_type": "mv_meter",
        "attributes": {
            "TAG1": config.tag,
            "METERING_CLASS": config.metering_class,
            "CT_RATIO": config.ct_ratio,
            "PT_RATIO": config.pt_ratio,
        },
    }
