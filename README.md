# solar-calc-backend

The NEC calc modules referenced throughout the HMI draft (`ampacity_calc.py`,
`ocpd_calc.py`, `switchboard_calc.py`, `bonding_calc.py`, `combiner_calc.py`,
`voltage_drop_calc.py`, `jurisdiction_lookup.py`, `placarding_calc.py`,
`etap_export.py`, `iv_curve_calc.py`, `document_header.py`), ported from the
client-side JS to real, tested Python — plus a stateless `/calculate`
endpoint, and a changeset system (SQLite-backed) that the AutoCAD add-in
(`ac-switchboard-addin`) polls to keep switchboard, aux panelboard, inverter
DC/combiner, transformer, and MV device blocks in sync with project data.

## Setup

```bash
cd solar-calc-backend
python -m venv .venv
.venv/Scripts/activate   # Windows
pip install -r requirements.txt
```

## Run the tests

```bash
pytest
```

## Run the API locally

```bash
uvicorn app.main:app --reload --port 8000
```

`POST /calculate` with a project JSON (same shape as the HMI's "Save
project" export) returns computed ampacity, OCPD, switchboard, bonding,
combiner, voltage-drop, jurisdiction, placarding, ETAP, and IV-curve results
in one response. `GET /health` for a liveness check.

The changeset endpoints (`PUT /projects/{id}`, `POST /changesets/*/refresh`,
`GET /changesets(/pending)`, `POST /changesets/{id}/applied|failed|retry`)
are what the AutoCAD add-in and the HMI's "Backend Verification" panel
actually talk to.

## PVCase planning + validation

Closes the loop the rest of this codebase used to treat as a permanent
placeholder (`voltage_drop_calc.py`'s "length is expected to come from the
PVCase export" docstring, `run_reference_design.py`'s hardcoded
`pvcase_combiner_to_inverter_length_ft`): App Planning → PVCase/AutoCAD
implementation → App validation, three steps, three new backend endpoints.

- **`POST /pvcase/plan`** (`app/pvcase_plan.py`) — the App Planning step.
  Given a project + a switchboard/naming-convention layout, returns what to
  key into PVCase before building the site: modules-per-string (from
  `string_design_calc`) and the exact equipment tags (`INV-*`, `DCC-*`,
  `XFMR-*`) PVCase should end up producing.
- **`POST /pvcase/validate`** (`app/pvcase_validate.py`) — the App
  Validation step, run after the PVCase/AutoCAD design is built. Parses
  whichever of a BOM export (`app/pvcase_bom_import.py`) and/or CAD Release
  DWG (`app/pvcase_dwg_scan.py`) are supplied — by local filesystem path,
  since both live in a Dropbox-synced project folder rather than needing a
  browser upload — and diffs their equipment tags against the plan and
  against each other.
- **`POST /pvcase/routing-report`** (`app/pvcase_routing.py`, built on
  `app/cable_routing_calc.py`) — closes the routing-condition gap below:
  applies a per-circuit-type routing template (an optional fixed lead-in
  leg, e.g. "10 ft in EMT conduit exiting the equipment," then a remainder
  leg for the rest, e.g. "buried PVC for whatever's left") across every real
  segment parsed from the BOM, and reports the governing (worst-case)
  segment and conductor per circuit type. A single continuous conductor is
  sized to whichever leg is most restrictive — not averaged across legs —
  matching how NEC actually requires a run to be sized; voltage-drop's
  length-driven upsize can push the real answer (`final_conductor`) past
  what ampacity alone would pick (`selected_conductor`), so both are
  reported. Integrated with `app/raceway_calc.py` (built separately, not
  part of this PVCase work): once the governing leg and final conductor are
  known, each conduit leg gets a real trade size via
  `raceway_calc.size_conduit()` and a free-air leg explicitly marked
  `size_as_tray=True` gets a real tray width via
  `raceway_calc.size_cable_tray()` — sized against the *final* conductor,
  since every leg carries the same continuous wire. Voltage drop is computed
  leg-by-leg and summed (not one flat length calc) so an AC run through
  steel conduit correctly picks up `raceway_calc`'s reactance multiplier on
  just that leg. Verified end-to-end against a real 504-segment BOM (36 AC +
  36 DC combiner + 432 DC string runs), including a case where a bundled
  312 A DC circuit legitimately has no conductor that clears its derated
  ampacity (`selected_conductor: null`) — surfaced, not silently guessed at.

All three are wired into the HMI's Inverter Design panel ("PVCase planning
brief" / "PVCase validation" / "PVCase routing-aware ampacity" cards),
reusing its existing naming-convention and per-switchboard controls rather
than duplicating that state. The routing card exposes per-leg conductor
count, conduit material, and cable-tray sizing (`size_as_tray`/`tray_type`/
`tray_width_in`) — check "Size as tray" on a free-air leg to get a real
392.22 tray-width spec instead of true free air (no raceway to size).

One limitation left, not fixable from this side:

- **DWG scanning needs a local AutoCAD install.** `pvcase_dwg_scan.py`
  drives `accoreconsole.exe` headlessly and reads tag/coordinate pairs off
  PVCase's own "PVcase Device Numbering" layer — real, confirmed against a
  production CAD Release DWG, but only runs where AutoCAD is actually
  installed (the engineer's own machine), never on a cloud deployment like
  Render.

See `memory/pvcase_integration_gaps.md` for the fuller history of both gaps.

## Fluke IV-curve field-export validation + PVA project generation

Closes the two gaps `IV_Curve_Panel_Handoff_Spec.md` and the HMI's I-V
Curve panel used to carry as "not yet built"/"deferred" — see that spec's
2026-08-10 update section for the full before/after. Two new endpoints,
verified against a real 432-string Encore Brighton 1 export:

- **`POST /fluke/validate`** (`app/fluke_export_import.py` +
  `app/fluke_validate.py`) — parses a real Solmetric PVA field export's
  `Table` sheet (not a CSV — that workbook is a 40+-sheet macro-driven
  report; verified column map in `fluke_export_import.py`'s module
  docstring), then: per-string pass/fail preferring Solmetric's own
  Modeled/Deviation columns over recomputing a translation; a
  design-intent divergence check against this project's catalog module
  (asymmetric tolerance — 3% current / 8% voltage — after verification
  found a real ~4-5% Voc/Vmp translation-methodology gap vs. Solmetric's
  own "Blended" temperature source, even with the correct module); and, if
  a BOM path is also supplied, a coverage check against
  `pvcase_bom_import.py`'s parsed string list for strings the BOM expected
  but the export never tested.
- **`POST /fluke/pvapx`** (`app/pvapx_generator.py`) — generates a real
  Solmetric `.pvapx` project by cloning a real, working template file and
  rewriting its module data and switchboard/inverter/combiner/string tree
  from a parsed PVCase BOM. Reverses the handoff spec's earlier "no
  documented import path exists" deferral with actual evidence: a
  generated file (2 switchboards, 36 inverters, 432 strings, from the real
  Encore Brighton 1 BOM) was opened in real Solmetric PVA software and
  loaded correctly. **Every newly generated `.pvapx` still needs the same
  manual check before trusting it on a job** — the response includes a
  `validation_gate` field as a standing reminder, since the blank
  `data/`/`meas/` structure this assumes is inferred, not confirmed
  against a Solmetric-authored blank project.
- **`module_catalog.py`** gained a second module family (Znshine PV-Tech
  ZXM7-UHLDD144, real values from the Encore Brighton project's own
  `.pvapx` files) alongside the original ReneSola RS9 family — which also
  meant promoting `TEMP_COEFF_VOC_PCT_PER_C`/`TEMP_COEFF_ISC_PCT_PER_C`
  from two bare module-level constants to per-SKU fields on
  `ModuleElectricalSpec` (RS9's existing behavior is unchanged; it was the
  second family needing different real coefficients that broke the old
  assumption).

Like DWG scanning above, `/fluke/pvapx` only makes sense running on the
engineer's own machine — both the BOM/export/template inputs and the
generated output are local Dropbox-synced file paths, not uploads.

Not built in this pass, still open: the .docx/PDF field-prep packet (Part
1+2) `IV_Curve_Panel_Handoff_Spec.md` describes, and the SolarPro
root-cause troubleshooting flowchart (shading, shorted bypass diode,
series/shunt resistance, PID) for Phase 2's deviation engine — both
separate, larger efforts from what shipped here.

## Deploy (Render)

A `Dockerfile` and `render.yaml` blueprint are included.

1. Push this directory to a GitHub repo.
2. On [render.com](https://render.com): New → Blueprint → connect the repo.
   Render reads `render.yaml` and deploys automatically.
3. Note the resulting `https://<name>.onrender.com` URL.

Known free-tier limits: the service spins down after ~15 minutes idle (first
request after that wakes it up, takes ~30–50s), and there's no persistent
disk — the SQLite file resets on every redeploy/restart, so changesets and
saved projects don't survive a cold restart. Fine for testing the sync flow;
upgrade to a paid plan (and add a Render disk + `DATABASE_URL` env var
pointing at its mount path — already read from the environment in `db.py`)
before relying on it for real project data.
