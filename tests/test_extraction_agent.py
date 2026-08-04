"""Extraction agent retry policy: one retry on a malformed response, then
raise so the caller marks the ingestion job needs_attention (mocks the
Anthropic client entirely -- no network access in tests)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.extraction_agent import ExtractionAgentError, call_extraction_agent


@dataclass
class _FakeTextBlock:
    text: str
    type: str = "text"


@dataclass
class _FakeResponse:
    content: list


class _FakeMessages:
    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.call_count = 0

    def create(self, **kwargs):
        self.call_count += 1
        text = self._responses.pop(0)
        return _FakeResponse(content=[_FakeTextBlock(text=text)])


class _FakeClient:
    def __init__(self, responses: list[str]):
        self.messages = _FakeMessages(responses)


_VALID_MODULE_JSON = """{
  "document_type": "module_datasheet",
  "manufacturer": {"value": "ReneSola", "confidence": "high", "source": "p.1"},
  "model": {"value": "RS9-700", "confidence": "high", "source": "p.1"},
  "document_date_or_revision": {"value": "2025-01", "confidence": "high", "source": "p.1"},
  "module_fields": {
    "variants": [
      {"model_variant": "RS9-700", "rated_power_w": {"value": 700, "confidence": "high", "source": "p.2"},
       "voc_v": {"value": 48.6, "confidence": "high", "source": "p.2"},
       "isc_a": {"value": 18.32, "confidence": "high", "source": "p.2"},
       "vmp_v": {"value": 40.5, "confidence": "high", "source": "p.2"},
       "imp_a": {"value": 17.29, "confidence": "high", "source": "p.2"},
       "module_efficiency_pct": {"value": 22.5, "confidence": "high", "source": "p.2"}}
    ],
    "temp_coeff_voc_pct_per_c": {"value": -0.24, "confidence": "high", "source": "p.2"},
    "temp_coeff_isc_pct_per_c": {"value": 0.04, "confidence": "high", "source": "p.2"},
    "temp_coeff_pmax_pct_per_c": {"value": -0.34, "confidence": "high", "source": "p.2"},
    "noct_c": {"value": 45, "confidence": "high", "source": "p.2"},
    "max_system_voltage_v": {"value": 1500, "confidence": "high", "source": "p.2"},
    "max_series_fuse_rating_a": {"value": 35, "confidence": "high", "source": "p.2"},
    "application_class": {"value": "A", "confidence": "high", "source": "p.2"},
    "cell_type": {"value": "monocrystalline", "confidence": "high", "source": "p.2"},
    "number_of_cells": {"value": 132, "confidence": "high", "source": "p.2"},
    "dimensions_mm": {"length": 2384, "width": 1303, "depth": 33, "confidence": "high", "source": "p.2"},
    "weight_kg": {"value": 33.5, "confidence": "high", "source": "p.2"},
    "connector_type": {"value": "MC4", "confidence": "high", "source": "p.2"},
    "frame_material": {"value": "aluminum", "confidence": "high", "source": "p.2"},
    "junction_box_ip_rating": {"value": "IP68", "confidence": "high", "source": "p.2"},
    "certifications": {"value": ["UL 61730"], "confidence": "high", "source": "p.2"}
  },
  "inverter_fields": null,
  "conflict_notes": [],
  "unparsed_sections_note": ""
}"""


def test_valid_response_parses_on_first_call(monkeypatch):
    fake = _FakeClient([_VALID_MODULE_JSON])
    monkeypatch.setattr("app.extraction_agent._client", lambda: fake)

    result = call_extraction_agent(b"%PDF-1.4 fake bytes")

    assert result.document_type == "module_datasheet"
    assert result.module_fields.variants[0].rated_power_w.value == 700
    assert fake.messages.call_count == 1


def test_malformed_json_retries_once_then_succeeds(monkeypatch):
    fake = _FakeClient(["not json at all", _VALID_MODULE_JSON])
    monkeypatch.setattr("app.extraction_agent._client", lambda: fake)

    result = call_extraction_agent(b"%PDF-1.4 fake bytes")

    assert result.document_type == "module_datasheet"
    assert fake.messages.call_count == 2


def test_malformed_json_twice_raises_after_one_retry(monkeypatch):
    fake = _FakeClient(["not json at all", "still not json"])
    monkeypatch.setattr("app.extraction_agent._client", lambda: fake)

    with pytest.raises(ExtractionAgentError):
        call_extraction_agent(b"%PDF-1.4 fake bytes")

    assert fake.messages.call_count == 2


def test_response_missing_required_top_level_key_retries_then_raises(monkeypatch):
    # Missing the required "document_type" key entirely.
    missing_key_json = '{"manufacturer": {"value": "X", "confidence": "high", "source": ""}}'
    fake = _FakeClient([missing_key_json, missing_key_json])
    monkeypatch.setattr("app.extraction_agent._client", lambda: fake)

    with pytest.raises(ExtractionAgentError):
        call_extraction_agent(b"%PDF-1.4 fake bytes")

    assert fake.messages.call_count == 2


def test_api_call_failure_is_wrapped_not_left_to_crash(monkeypatch):
    """A network/auth/rate-limit failure reaching the API is a "failed
    extraction agent call" under the retry policy too, not just a malformed
    response -- it must surface as ExtractionAgentError, never an unhandled
    exception that would 500 the request."""

    class _RaisingMessages:
        def __init__(self):
            self.call_count = 0

        def create(self, **kwargs):
            self.call_count += 1
            raise TypeError("Could not resolve authentication method")

    class _RaisingClient:
        def __init__(self):
            self.messages = _RaisingMessages()

    fake = _RaisingClient()
    monkeypatch.setattr("app.extraction_agent._client", lambda: fake)

    with pytest.raises(ExtractionAgentError):
        call_extraction_agent(b"%PDF-1.4 fake bytes")

    assert fake.messages.call_count == 2  # auto-retry once, then give up
