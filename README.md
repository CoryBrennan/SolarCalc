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
implementation → App validation, three steps, two new backend endpoints.

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

Both are wired into the HMI's Inverter Design panel ("PVCase planning
brief" / "PVCase validation" cards), reusing its existing naming-convention
and per-switchboard controls rather than duplicating that state.

Two limitations, neither fixable from this side:

- **DWG scanning needs a local AutoCAD install.** `pvcase_dwg_scan.py`
  drives `accoreconsole.exe` headlessly and reads tag/coordinate pairs off
  PVCase's own "PVcase Device Numbering" layer — real, confirmed against a
  production CAD Release DWG, but only runs where AutoCAD is actually
  installed (the engineer's own machine), never on a cloud deployment like
  Render.
- **PVCase's cable lengths are routing-condition-blind.** The BOM export's
  circuit-length sheets (Transformer→Inverter, Inverter→DC Combiner, DC
  Combiner→String) measure module-connector-to-endpoint only, with no
  above-ground cable-tray/hanger vs. underground-conduit breakdown — real
  lengths now, but still not enough on their own for correct ampacity
  derating. See `memory/pvcase_integration_gaps.md`.

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
