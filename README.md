# solar-calc-backend

The NEC calc modules referenced throughout the HMI draft (`ampacity_calc.py`,
`ocpd_calc.py`, `switchboard_calc.py`, `bonding_calc.py`, `combiner_calc.py`,
`voltage_drop_calc.py`, `jurisdiction_lookup.py`, `placarding_calc.py`,
`etap_export.py`, `iv_curve_calc.py`, `document_header.py`), ported from the
client-side JS to real, tested Python — plus a numerical trench/duct-bank
thermal solver (`trench_calc.py` over `trench_thermal/`), a stateless `/calculate`
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

## Trench thermal design (direct-buried conduit ampacity)

`POST /trench/thermal-design` — `app/trench_calc.py` over the numerical solver
package in `app/trench_thermal/`.

Conduits sharing a trench heat each other through the soil. That is a
different mechanism from NEC 310.15(C)(1) conduit fill — fill adjustment is
conductors crowding each other *inside* one conduit; this is conduits
competing to reject heat into the same soil *outside* — and no NEC table
covers it. Rather than the closed-form Neher-McGrath/Kennelly expressions
(valid only for idealized homogeneous soil), this solves 2D steady-state heat
conduction across the trench section by finite volume, so an engineered
backfill envelope of one thermal resistivity inside native soil of another is
handled directly, in a rectangle or an arbitrary polygon.

What it does *not* do is re-enter or re-derive anything the schedule already
holds. Current, conductor size, and conduit trade size all come from the
project's own `raceway_runs` as sized by `app/raceway_calc.py` — and the NEC
derate factors are **not** reapplied to the `I²R` heat term, because they
change which conductor gets selected, not how much current physically flows.
The response reports the ambient-correction, conduit-fill, and trench factors
as three separate multiplying rows, never folded together.

Two modes:

- `conditions.fixed_layout` set → check one already-drawn arrangement. One
  solve, a fraction of a second.
- omitted → search every row/column split and 1.5"-on-centre spacing for the
  smallest trench that holds. Tens of seconds (each candidate is a fresh
  graded grid and sparse factorization), which is why the HMI drives it from a
  "run calc" button rather than recomputing live. Both the raw computed
  minimum spacing and the snapped buildable value are reported.

Soil **thermal** resistivity (°C·cm/W, ASTM D5334 / IEEE 442 needle probe) and
soil **electrical** resistivity (Ω·m, Wenner four-point) are different
measurements whose numeric ranges overlap almost completely, so a
grounding-study value used here produces a plausible-looking wrong answer that
no range check can catch. `conditions.soil_resistivity_source` must therefore
be declared, and a Wenner value is rejected with a 422 rather than warned
about.

Accuracy is pinned by `tests/test_trench_thermal.py`, which checks the solver
against the closed-form Kennelly result for an isolated buried conduit — the
one case where a closed form is exactly right — to under 2% across a
0.3–2.5 m burial range. **Run it after any change to grid construction or
solver assembly.** `solver.EQUIV_SOURCE_RADIUS_RATIO` is an empirically
calibrated correction for the logarithmic singularity of a point heat source
on a discretized grid (the heat-conduction analog of Peaceman's equivalent
well-block radius); without it the same case is ~42% wrong, not ~0.4%, and no
amount of grid refinement fixes it.

Scope for v1 is direct-buried conduit only — concrete-encased duct banks are
deliberately out, and tray/messenger runs are reported as skipped rather than
silently treated as buried. Every conduit is loaded at 100% simultaneously
with no diversity credit.

This module is why `numpy` and `scipy` are in `requirements.txt`; nothing else
in the codebase needs them.

## Bluebeam plan review: markup consolidation, approval, and design stamping

Rail 30 ("Plan Review & Markups") and `/bluebeam/*`. Two directions, both
file-based.

The premise the whole thing rests on: **Bluebeam markups are ordinary PDF
annotations.** Revu's Markups List is a view over each page's `/Annots` array,
not a proprietary sidecar. So `pypdf` reads and writes them directly, and none
of this needs a Studio Prime subscription, an OAuth app registered in the
Bluebeam Developer Portal, or a Revu install on the server. (Bluebeam does
publish a Studio API at `https://api.bluebeam.com/publicapi/v1/` covering
Sessions, Projects and Jobs — but every user of an integration built on it
must be a member of a Studio Prime space, which is the subscription this
deliberately avoids.)

### Inbound: several marked-up copies → one consolidated set

A drawing set goes out to four reviewers, each marks up their own copy, and
four PDFs come back. Revu can't combine those without a Studio Session, so the
merge happens here.

- **`POST /bluebeam/review-sets`**, **`/master`**, **`/submissions`**
  (`app/bluebeam_routes.py`) — create the review set from the *clean* master
  set, then upload each reviewer's returned copy under their name. Markups are
  extracted on the way in (`app/bluebeam_markup_io.py`) and stored as
  dispositionable rows.
- **`POST /bluebeam/review-sets/{id}/consolidate`**
  (`app/bluebeam_consolidate.py`) — clones every reviewer's annotation objects
  onto the master, appearance streams and all. Nothing is flattened,
  rasterised, or redrawn: the merged markups stay live, still selectable and
  editable in Revu, still individually listed with their original author and
  timestamp. Each reviewer also becomes an **optional content group**, so they
  show up as toggleable layers in Revu's Layers panel — the single most useful
  property of a consolidated set, and it falls out of a PDF primitive rather
  than anything Bluebeam-specific.
- **`GET /consolidated.pdf`** and **`GET /markups.csv`** — the merged set, and
  a markup summary shaped like Revu's own export plus the disposition,
  assignee and response columns Revu has no concept of.

Three behaviours worth knowing, all of them chosen rather than fallen into:

- **Page grids must match.** Same page count, same sheet sizes, checked before
  anything merges (`POST /check-compatibility` dry-runs it). Markup coordinates
  are page coordinates, so a comment from an 11x17 sheet lands somewhere
  meaningless on a 30x42 one. This is the same constraint Revu enforces on its
  own markup import. A mismatch is refused with the offending pages named — the
  usual cause is one reviewer having been issued a stale revision.
- **The same comment in two copies is merged once.** Matched on `/NM`, Revu's
  own per-annotation GUID: a comment from an earlier round is inherited by
  every copy issued from that set, so it legitimately arrives several times.
  It is stored once with the extra reviewers recorded in `also_from`, which
  keeps the review table's count equal to the consolidated set's and means
  nobody dispositions the same comment twice.
- **Overlapping markups are flagged, never auto-merged.** Two reviewers
  circling the same conduit are two opinions; silently dropping one would lose
  a real review comment. Both are kept and the pair is reported in
  `overlap_clusters` for a human.

### Approval and round-over-round tracking

`app/bluebeam_review.py` holds the rules; the routes hold the persistence
(same split as `commissioning_calc` vs `commissioning_routes`).

Revu *does* have a per-markup status column, and it is imported — but only as
advisory (`revu_status`). It is free text on a reply annotation that anyone
with the file can overwrite, with no record of who changed it or when, and a
review that gates a drawing revision needs an audit trail the file cannot
provide. The authoritative disposition is tracked in the app, and every change
lands in `MarkupAudit`.

    open ─┬─> accepted ──> incorporated
          ├─> rejected
          └─> deferred

`accepted` means the reviewer is right. `incorporated` means the drawing has
actually been changed. Collapsing those two is the most common way a review log
lies — a set signed off with a dozen accepted comments nobody drew — so only
`rejected` and `incorporated` are terminal, and `approval_gate` refuses
sign-off while anything sits in `open`, `accepted`, or `deferred`, reporting
each blocker category separately. Rejecting or deferring requires a written
reason. Approving is not a one-way door: reopening a markup, or a late
submission arriving, unwinds the sign-off automatically.

**`GET /rounds/{n}/diff`** answers "what actually changed since last round",
classifying added / modified / unchanged / withdrawn. Dispositions carry
forward for markups nobody touched; a *modified* markup is deliberately reset
to `open` (the reviewer changed what they were asking for, so a prior
"accepted" no longer refers to the same request), with the previous decision
preserved in `reopened_from` so the reset is visible rather than looking like
data loss.

### Outbound: stamping computed design data onto a plan set

**`POST /bluebeam/stamp`** and **`/stamp-schedule`** (`app/bluebeam_stamp.py`)
write the engine's own numbers onto a drawing PDF as live Revu markups —
equipment tags, conductor and conduit callouts — each carrying
`/SolarCalcTag`, so a set that comes back marked up ties comment-by-comment to
the equipment it concerns without matching on text. They render everywhere,
not just after Revu regenerates them, because each stamp is written with its
own appearance stream.

Placement is the hard part and is not guessed at. The app knows equipment by
tag and, via `pvcase_dwg_scan.DwgDeviceTag`, by *model-space* coordinate —
which is not a page coordinate, since a site plan is model geometry plotted at
some scale, rotation and origin that lives in the drawing's layout. So
`PlanTransform` takes **two control points**: name two pieces of equipment far
apart, give their PDF coordinates once, and every other tag follows. It is a
*similarity* transform (uniform scale, rotation, translation), not a general
affine one, because a plotted plan preserves shape — fitting an affine over
more points would quietly absorb control-point error into a fake anisotropic
scale and place everything slightly wrong. A similarity transform can't, so
the error surfaces honestly in `residual_error_pt`. For anything with no
coordinate at all, `stamp_schedule` drops a titled table in a chosen corner,
no calibration involved.

Like `/fluke/pvapx` and DWG scanning, these take local file paths in and out
rather than uploads — a plan set is too big to round-trip through a browser
and already lives in the Dropbox-synced project folder.

### Limits

- **The Revu round trip is not proven by the tests.**
  `tests/test_bluebeam_markups.py` and `tests/test_bluebeam_api.py` (94 tests)
  check real annotation structure — markups survive the cross-document clone as
  live annotations, appearance streams intact, one OCG per reviewer, status
  replies re-linked to the right parent — but they read the merged file with
  the same library that wrote it. **Open a consolidated set in Revu before
  issuing it.** Same standing validation gate the generated `.pvapx` files
  carry, for the same reason.
- **Markups with no `/NM` GUID** (written by some non-Revu PDF tools) fall back
  to a content-hash identity, so *moving* one across rounds reads as
  withdraw-plus-add rather than as a modification. Reported as
  `unmatchable_count` rather than smoothed over with a proximity heuristic that
  would be wrong in the other direction.
- **Uploads live in the database row.** With the database on Supabase those
  rows survive a redeploy (see the deploy notes below), but a real plan set can
  still exceed the 150 MB upload cap and the free tier's 500 MB of database.
  For an actual review cycle, run locally or use the `submissions-by-path` /
  `master_source_path` routes against the project folder.
- **Not built:** the Studio API path (needs Studio Prime), FDF/BAX export for
  pushing consolidated markups onto a *different* revision of the set, and
  Tool Chest (`.btx`) generation.

## Deploy (Render web service + Supabase Postgres)

The app runs on Render; the database is Supabase Postgres, not Render Postgres.
`app/db.py` builds the engine from `DATABASE_URL` alone, so the only thing that
makes it Supabase rather than anything else is the value of that variable.

**1. Create the Supabase project.** [supabase.com](https://supabase.com) → New
project. Save the database password it generates — it is shown once, and the
connection string is the only place it appears afterwards.

**2. Copy the *Session pooler* connection string.** Click **Connect** at the
top of the project dashboard and pick **Session pooler** under the shared
pooler:

```
postgres://postgres.<project-ref>:<password>@aws-<n>-<region>.pooler.supabase.com:5432/postgres
```

Two things to get right:

- **It must be a `pooler.supabase.com` host, not `db.<project-ref>.supabase.co`.**
  The shared pooler is IPv4 on every tier; the project host resolves to IPv6
  unless you buy the IPv4 add-on. Render has no IPv6 egress, so the direct
  string fails with an opaque `Network is unreachable` at connect time.
- **Paste it unedited**, including the legacy `postgres://` scheme the
  dashboard still emits. `_normalize_database_url` rewrites that to
  `postgresql+psycopg://`; hand-editing it invites a typo in the one string
  nothing else can work without.

Transaction mode (port `6543`) also works — `db.py` reads the *port* to detect
it and switches to `prepare_threshold=None` plus `NullPool`, because PgBouncer's
transaction mode can't route psycopg3's prepared statements and the pooler is
already the connection pool. Port is the right signal rather than hostname,
since transaction mode is served both by the shared pooler and by the paid
tiers' dedicated pooler on `db.<project-ref>.supabase.co:6543`. Session mode is
still the closer match to how this app holds one session per request.

**3. Seed it from your local database**, so the catalog and any saved projects
don't start empty. Put the connection string in `.supabase-url` (gitignored)
rather than passing it on the command line, which would leave the password in
your shell history:

```
Read-Host "Paste Supabase session pooler URL" | Set-Content -Path .supabase-url -NoNewline
DATABASE_URL="$(cat .supabase-url)" python -m scripts.migrate_sqlite_to_postgres
```

`--target` defaults to `$DATABASE_URL`, and the script prints only the host half
of the URL. It walks `SQLModel.metadata` in foreign-key order rather than naming
tables, so it stays correct as models are added; tables that already hold rows
are skipped unless `--force`, which makes a re-run after a partial failure safe.
Columns present in the models but missing from an older SQLite file (SQLModel's
`create_all` only ever CREATEs, never ALTERs) are reported and left to the
target's default instead of aborting the copy.

Note that the SQLite path follows the working directory uvicorn was launched
from, so there may be more than one local `solar_calc.db` holding different
data. Check both before assuming one is authoritative; `--source` points at the
other, with `--force` to add into tables the first pass already filled.

Do this step **before** touching Render. It doubles as the connectivity test: a
wrong password, or the IPv6 host mistake from step 2, surfaces here as a
traceback on your own machine instead of in a deploy log.

**4. Point Render at it.** On an existing service: dashboard → the service →
**Environment** in the *left pane* → **+ Add Environment Variable**, key
`DATABASE_URL`, value the same string.

Saving does not deploy on its own — Render offers *Save only*, *Save and
deploy*, and *Save, rebuild, and deploy*. Choose **Save only** if step 5 is
still ahead of you: the push then triggers one build that already has the
variable, instead of building twice. The variable takes effect on the next
deploy either way.

On a fresh Blueprint deploy, Render reads `render.yaml` and prompts for the
value instead (that is what `sync: false` does).

**5. Deploy the code.** Setting the env var alone works against any revision
whose `db.py` reads `DATABASE_URL`, but without the `pool_pre_ping` added here
the first request after Supabase's pooler drops an idle connection fails
instead of reconnecting — which on a free tier that idles constantly is often.

**6. Verify.** `/health` only proves the service booted. To prove it is reading
Supabase, request a row that only exists in seeded data:

```
curl https://<name>.onrender.com/ingest/<a job id from step 3>
```

A JSON body means the app is on Postgres; a 404 means `DATABASE_URL` did not
take and it is still on container SQLite. Then **redeploy and request it again**
— the same 200 is the entire point of this exercise.

### What this does and doesn't fix

Uploads — commissioning photos, plan-set PDFs, datasheet ingests — are `bytea`
columns, not files on a disk (see the notes above). So there is no disk to move:
pointing `DATABASE_URL` at Supabase is what makes them survive a redeploy, and
it's the whole fix for the "wiped on every redeploy" problem. What it doesn't
change is the size ceiling. Supabase's free tier is 500 MB of database, and the
upload cap here is 150 MB per file, so a full plan set still belongs on the
`submissions-by-path` / `master_source_path` routes against a project folder
rather than in a row. Supabase Storage (S3-compatible, separate 1 GB free
allowance) is where those blobs would go if they need to leave Postgres — that
is a code change to the upload routes, not a config change, and isn't built.

Two idle-timeout behaviours stack on the free tiers: the Render service spins
down after ~15 minutes idle (first request after wakes it, ~30–50 s), and a free
Supabase project **pauses after 7 days of no activity** and needs a manual
restore from the dashboard. `db.py` sets `pool_pre_ping` and a 300 s
`pool_recycle` so a connection dropped by the pooler reconnects instead of
surfacing as a failed request, but a paused project is a dashboard action.
