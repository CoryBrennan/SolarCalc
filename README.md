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
