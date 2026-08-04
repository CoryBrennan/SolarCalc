"""Datasheet extraction agent — calls the Claude API with the prompt/schema
from datasheet-extraction-agent-prompt.md, attaching the source PDF directly
as a document content block (Claude's own PDF support handles scanned/
rasterized pages internally, so this app never rasterizes pages itself).

Retry policy per the integration spec: one retry on a malformed/invalid
response, then give up — the caller (catalog_routes) marks the ingestion job
needs_attention rather than guessing at a repair.
"""

from __future__ import annotations

import base64
import json
import os

import anthropic
from pydantic import ValidationError

from app.extraction_schema import ExtractionAgentResponse

MODEL = "claude-opus-5"
MAX_TOKENS = 8000

# Verbatim from datasheet-extraction-agent-prompt.md's "Prompt text" section.
EXTRACTION_PROMPT = """You are extracting structured technical data from a solar equipment document for a solar electrical engineering design application's equipment catalog. The document may be a module datasheet, an inverter datasheet, an installation manual, or an O&M manual — determine which from the content.

**Rules — follow strictly:**

1. Extract only what is explicitly stated in the document. Never estimate, infer from typical industry values, or fill in a field from general knowledge. If a field is not present, set its `value` to `null` and `confidence` to `"not_found"`.
2. For every extracted field, report:
   - `value` — the extracted value, in the units the manufacturer states (record the unit separately; do not silently convert units)
   - `confidence` — one of `"high"` (clearly and unambiguously stated), `"medium"` (stated but requires interpretation, e.g. reading off a chart or a merged cell), `"low"` (present but ambiguous or conflicting elsewhere in the document), `"not_found"`
   - `source` — page number and a short label of where it was found (e.g. `"p.2, Electrical Characteristics table"`)
3. If the document lists multiple power-class/model variants in one table (common on module datasheets), return one entry per variant under `variants`, not a single averaged/first value.
4. If a value conflicts between two places in the same document (e.g. a summary table vs. detailed spec page), report the more detailed/specific one as `value` and note the discrepancy in a `conflict_note` field.
5. Output **only valid JSON** matching the schema below. No markdown fences, no commentary, no preamble.

**Output schema:**

```json
{
  "document_type": "module_datasheet | inverter_datasheet | install_manual | om_manual",
  "manufacturer": {"value": "", "confidence": "", "source": ""},
  "model": {"value": "", "confidence": "", "source": ""},
  "document_date_or_revision": {"value": "", "confidence": "", "source": ""},

  "module_fields": {
    "variants": [
      {
        "model_variant": "",
        "rated_power_w": {"value": null, "confidence": "", "source": ""},
        "voc_v": {"value": null, "confidence": "", "source": ""},
        "isc_a": {"value": null, "confidence": "", "source": ""},
        "vmp_v": {"value": null, "confidence": "", "source": ""},
        "imp_a": {"value": null, "confidence": "", "source": ""},
        "module_efficiency_pct": {"value": null, "confidence": "", "source": ""}
      }
    ],
    "temp_coeff_voc_pct_per_c": {"value": null, "confidence": "", "source": ""},
    "temp_coeff_isc_pct_per_c": {"value": null, "confidence": "", "source": ""},
    "temp_coeff_pmax_pct_per_c": {"value": null, "confidence": "", "source": ""},
    "noct_c": {"value": null, "confidence": "", "source": ""},
    "max_system_voltage_v": {"value": null, "confidence": "", "source": ""},
    "max_series_fuse_rating_a": {"value": null, "confidence": "", "source": ""},
    "application_class": {"value": null, "confidence": "", "source": ""},
    "cell_type": {"value": null, "confidence": "", "source": ""},
    "number_of_cells": {"value": null, "confidence": "", "source": ""},
    "dimensions_mm": {"length": null, "width": null, "depth": null, "confidence": "", "source": ""},
    "weight_kg": {"value": null, "confidence": "", "source": ""},
    "connector_type": {"value": null, "confidence": "", "source": ""},
    "frame_material": {"value": null, "confidence": "", "source": ""},
    "junction_box_ip_rating": {"value": null, "confidence": "", "source": ""},
    "certifications": {"value": [], "confidence": "", "source": ""}
  },

  "inverter_fields": {
    "nameplate_ac_power_kw": {"value": null, "confidence": "", "source": ""},
    "dc_fuse_rating_per_string_a": {"value": null, "confidence": "", "source": ""},
    "dc_fuse_rating_combined_a": {"value": null, "confidence": "", "source": ""},
    "max_dc_string_count_per_mppt": {"value": null, "confidence": "", "source": ""},
    "max_dc_string_count_per_combiner": {"value": null, "confidence": "", "source": ""},
    "terminal_torque_specs": {
      "value": [{"connection_point": "", "torque": "", "unit": ""}],
      "confidence": "", "source": ""
    },
    "termination_temp_rating_c": {"value": null, "confidence": "", "source": ""},
    "dc_connector_type": {"value": null, "confidence": "", "source": ""},
    "ac_termination_type": {"value": null, "confidence": "", "source": ""},
    "communications_protocols": {"value": [], "confidence": "", "source": ""},
    "enclosure_ip_rating": {"value": null, "confidence": "", "source": ""},
    "ambient_temp_range_c": {"min": null, "max": null, "confidence": "", "source": ""},
    "altitude_derating_notes": {"value": null, "confidence": "", "source": ""},
    "mounting_requirements": {"value": null, "confidence": "", "source": ""},
    "ground_fault_protection_type": {"value": null, "confidence": "", "source": ""},
    "warranty_years": {"value": null, "confidence": "", "source": ""}
  },

  "conflict_notes": [],
  "unparsed_sections_note": ""
}
```

Only populate `module_fields` or `inverter_fields` (whichever matches `document_type`) — leave the other as `null`.

Note: for inverters, several electrical/simulation fields (MPPT voltage window, max DC voltage/current, nameplate AC rating, MPPT count) are better sourced from the manufacturer's .OND file when available — this prompt's `inverter_fields` list deliberately focuses on fields .OND files typically omit (fuse ratings, torque specs, connector type, comms, mounting). If no .OND file exists for this model, this agent can still be re-run against the datasheet with an expanded field list on request."""


class ExtractionAgentError(Exception):
    """Raised when the agent's response isn't usable after one retry —
    the caller marks the ingestion job needs_attention rather than
    guessing at a repair."""


def _client() -> anthropic.Anthropic:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    return anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()


def _call_once(pdf_bytes: bytes) -> ExtractionAgentResponse:
    # Anything that can go wrong reaching the API (missing/invalid
    # credentials, network failure, rate limit, server error) is a "failed
    # extraction agent call" under the pipeline's retry policy just as much
    # as a malformed response is — none of it should escape as an unhandled
    # 500, only as ExtractionAgentError so the caller can retry-then-flag.
    try:
        client = _client()
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": base64.standard_b64encode(pdf_bytes).decode("ascii"),
                            },
                        },
                        {"type": "text", "text": EXTRACTION_PROMPT},
                    ],
                }
            ],
        )
    except ExtractionAgentError:
        raise
    except Exception as exc:
        raise ExtractionAgentError(f"extraction agent call failed: {exc}") from exc

    text = "".join(block.text for block in response.content if block.type == "text")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ExtractionAgentError(f"agent response was not valid JSON: {exc}") from exc

    try:
        return ExtractionAgentResponse.model_validate(payload)
    except ValidationError as exc:
        raise ExtractionAgentError(f"agent response did not match the extraction schema: {exc}") from exc


def call_extraction_agent(pdf_bytes: bytes) -> ExtractionAgentResponse:
    """Retry-once-then-fail, per the ingestion pipeline's confirmed retry
    design: auto-retry a failed call once, then surface needs_attention
    rather than repairing or guessing at malformed output."""
    try:
        return _call_once(pdf_bytes)
    except ExtractionAgentError:
        return _call_once(pdf_bytes)
