import os

import pytest

from app.pvcase_dwg_scan import DwgDeviceTag, PvcaseDwgError, _parse_dump, find_accoreconsole


def test_parse_dump_reads_tag_lines():
    text = "TAG|INV-1-1|-100.5|200.25\nTAG|DCC-1-1|-90.0|180.0\nDONE\n"
    tags = _parse_dump(text)
    assert tags == [
        DwgDeviceTag(tag="INV-1-1", x=-100.5, y=200.25),
        DwgDeviceTag(tag="DCC-1-1", x=-90.0, y=180.0),
    ]


def test_parse_dump_skips_malformed_lines():
    text = "TAG|ONLY_THREE|1.0\nTAG|BAD_NUM|abc|def\nTAG|OK|1.0|2.0\n"
    tags = _parse_dump(text)
    assert tags == [DwgDeviceTag(tag="OK", x=1.0, y=2.0)]


def test_parse_dump_ignores_non_tag_lines():
    text = "some accoreconsole banner text\nCommand:\nTAG|XFMR-1|0.0|0.0\nDONE\n"
    tags = _parse_dump(text)
    assert tags == [DwgDeviceTag(tag="XFMR-1", x=0.0, y=0.0)]


def test_find_accoreconsole_raises_clear_error_when_missing(monkeypatch, tmp_path):
    monkeypatch.delenv("ACCORECONSOLE_PATH", raising=False)
    empty_base = tmp_path / "Autodesk"
    empty_base.mkdir()
    with pytest.raises(PvcaseDwgError, match="accoreconsole.exe not found"):
        find_accoreconsole(search_base=empty_base)


def test_find_accoreconsole_uses_env_override(monkeypatch, tmp_path):
    fake_exe = tmp_path / "accoreconsole.exe"
    fake_exe.write_text("")
    monkeypatch.setenv("ACCORECONSOLE_PATH", str(fake_exe))
    assert find_accoreconsole() == fake_exe
