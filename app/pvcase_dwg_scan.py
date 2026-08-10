"""DWG scanner for PVCase's CAD Release output. Wraps AutoCAD's headless
console (accoreconsole.exe) plus a generated AutoLISP script to read
equipment tag + site-plan coordinate pairs directly off PVCase's own
"PVcase Device Numbering" layer -- a layer PVCase itself writes into every
CAD Release it produces, confirmed against a real export (Encore Brighton 1,
WIP90 CHINT 2025-04-18): text entities reading "INV-2-18", "DCC-2-18",
"XFMR-2", etc. sitting at real insertion-point coordinates, in the same tag
format the BOM export and this app's own naming convention use.

This is the one thing PVCase's BOM .xlsx export can't provide -- it has no
coordinates at all for non-racking equipment (inverters, DC combiners,
transformers) -- but the CAD Release DWG does, because PVCase authors that
layer itself rather than leaving it to manual drafting. See
memory/pvcase_integration_gaps.md.

The equipment *blocks* themselves (e.g. "PVcase Inverter (2nd tier)") carry
no attributes, so tag identity comes only from this text layer, not from the
block reference -- confirmed by inspecting a real INSERT dump where every
block's group-66 "has attributes" flag was unset. Reading the text layer
directly is simpler and more robust than a block+nearest-text spatial join,
and doesn't depend on manufacturer-specific block names (which do vary
project to project, e.g. "Chint 125").

Windows + a local AutoCAD install only -- accoreconsole.exe ships with
AutoCAD/AutoCAD LT, not something a cloud deployment (e.g. Render) would
have. See find_accoreconsole()'s error message when it's missing.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

DEVICE_NUMBERING_LAYER = "PVcase Device Numbering"

# strcat-built inside AutoLISP, so tag text is written as-is; the "|"
# delimiter is what the Python side splits on, not something re-escaped
# in LISP -- fine in practice since PVCase's device tags never contain "|".
_LISP_TEMPLATE = """(vl-load-com)
(defun safe-str (x)
  (cond ((null x) "") ((= (type x) 'STR) x) (t (vl-princ-to-string x)))
)
(defun c:SCANDEVICES ()
  (setq f (open "{out_path}" "w"))
  (setq ss (ssget "X" (list (cons 8 "{layer}") (cons -4 "<OR") (cons 0 "TEXT") (cons 0 "MTEXT") (cons -4 "OR>"))))
  (if ss
    (progn
      (setq n (sslength ss))
      (setq i 0)
      (while (< i n)
        (vl-catch-all-apply (function (lambda ()
          (setq ent (ssname ss i))
          (setq edata (entget ent))
          (setq pt (cdr (assoc 10 edata)))
          (setq txt (safe-str (cdr (assoc 1 edata))))
          (write-line (strcat "TAG|" txt "|" (rtos (car pt) 2 6) "|" (rtos (cadr pt) 2 6)) f)
        )))
        (setq i (1+ i))
      )
    )
  )
  (write-line "DONE" f)
  (close f)
  (princ)
)
(c:SCANDEVICES)
"""

_SCR_TEMPLATE = '(load "{lisp_path}")\nQUIT\nY\n'


class PvcaseDwgError(RuntimeError):
    pass


@dataclass
class DwgDeviceTag:
    tag: str
    x: float
    y: float


def find_accoreconsole(search_base: Path = Path("C:/Program Files/Autodesk")) -> Path:
    override = os.environ.get("ACCORECONSOLE_PATH")
    if override and Path(override).exists():
        return Path(override)
    if search_base.exists():
        # Prefer the newest AutoCAD install if more than one is present.
        candidates = sorted(search_base.glob("AutoCAD */accoreconsole.exe"), reverse=True)
        if candidates:
            return candidates[0]
    raise PvcaseDwgError(
        "accoreconsole.exe not found. Scanning a DWG needs a local AutoCAD install "
        "on this machine -- it will not work on a headless cloud deployment (e.g. "
        "Render). Set the ACCORECONSOLE_PATH environment variable if AutoCAD is "
        "installed somewhere non-standard."
    )


def _parse_dump(text: str) -> list[DwgDeviceTag]:
    tags: list[DwgDeviceTag] = []
    for line in text.splitlines():
        if not line.startswith("TAG|"):
            continue
        parts = line.split("|")
        if len(parts) != 4:
            continue
        _, tag, x, y = parts
        try:
            tags.append(DwgDeviceTag(tag=tag, x=float(x), y=float(y)))
        except ValueError:
            continue
    return tags


def scan_device_tags(
    dwg_path: str | Path,
    layer: str = DEVICE_NUMBERING_LAYER,
    timeout_s: int = 240,
) -> list[DwgDeviceTag]:
    """Opens `dwg_path` headlessly in AutoCAD and returns every text label on
    `layer` as a (tag, x, y) record. Read-only -- the script always ends in
    QUIT/Y (discard changes), never SAVE."""
    dwg_path = Path(dwg_path)
    if not dwg_path.exists():
        raise PvcaseDwgError(f"DWG file not found: {dwg_path}")

    accoreconsole = find_accoreconsole()

    with tempfile.TemporaryDirectory(prefix="pvcase_dwg_scan_") as tmp:
        tmp_path = Path(tmp)
        out_path = tmp_path / "device_tags.txt"
        lisp_path = tmp_path / "scan.lsp"
        scr_path = tmp_path / "run.scr"

        lisp_path.write_text(
            _LISP_TEMPLATE.format(out_path=out_path.as_posix(), layer=layer),
            encoding="utf-8",
        )
        scr_path.write_text(_SCR_TEMPLATE.format(lisp_path=lisp_path.as_posix()), encoding="utf-8")

        try:
            result = subprocess.run(
                [str(accoreconsole), "/i", str(dwg_path), "/s", str(scr_path)],
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            raise PvcaseDwgError(f"accoreconsole timed out after {timeout_s}s scanning {dwg_path.name}") from exc

        if not out_path.exists():
            raise PvcaseDwgError(
                f"accoreconsole produced no output scanning {dwg_path.name} (exit code "
                f"{result.returncode}). stdout tail: {result.stdout[-500:]!r}"
            )
        return _parse_dump(out_path.read_text(encoding="utf-8", errors="replace"))
