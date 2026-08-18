"""Approval management and round-over-round change tracking for plan review.

Everything here is pure logic over `Markup` objects and plain dicts — no
database, no PDF. bluebeam_routes owns persistence; this owns the rules. Same
split the rest of the codebase uses (`commissioning_calc` vs
`commissioning_routes`), and for the same reason: the rules are the part
worth testing without a session fixture in the way.

**Why disposition lives here and not in the PDF.** Revu does have a per-markup
status column, and bluebeam_markup_io reads it — but it is a free-text state
on a reply annotation that any reviewer with the file can overwrite, with no
record of who changed what when, and no notion of "accepted, but the drawing
hasn't been revised yet." A review that gates a drawing revision needs an
audit trail the file itself cannot provide. So Revu's status is imported as
*advisory* (`Markup.status`) and the authoritative disposition is tracked
separately, in the app.

**The disposition lifecycle**, and why `accepted` is not the end of it:

    open ─┬─> accepted ──> incorporated
          ├─> rejected
          └─> deferred

`accepted` means "the reviewer is right." `incorporated` means "and the
drawing has been changed accordingly." Collapsing those two into one status
is the single most common way a review log lies: a set gets signed off with a
dozen accepted comments nobody actually drew. Only `rejected` and
`incorporated` are terminal, so `approval_gate` refuses to clear a set that
still has accepted-but-not-drawn comments sitting in it.

`deferred` is deliberately non-terminal too. It is a real answer during a
round ("valid, but next revision"), and it blocks final approval rather than
quietly disappearing — deferred items are reported as their own blocker
category so they get an explicit decision instead of aging out.

**Round-over-round diffing.** Reviewers re-issue marked-up sets, and the
question that matters on round two is "what actually changed since round
one." Matching is by `/NM` — Revu's own per-annotation GUID, stable across
save and reopen — falling back to a content fingerprint for markups from
tools that don't write one. The honest limitation, carried over from
bluebeam_markup_io: for a fallback-keyed markup, *moving* it reads as
withdraw-plus-add rather than as a modification, because without a GUID there
is nothing that survives the move to match on. Flagged in the response
(`unmatchable_count`) rather than smoothed over with a proximity heuristic
that would merge genuinely different comments.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.bluebeam_markup_io import Markup

OPEN = "open"
ACCEPTED = "accepted"
REJECTED = "rejected"
DEFERRED = "deferred"
INCORPORATED = "incorporated"

DISPOSITIONS = (OPEN, ACCEPTED, REJECTED, DEFERRED, INCORPORATED)

#: Dispositions that end a markup's life in the review. Everything else keeps
#: the set from being approved.
TERMINAL_DISPOSITIONS = frozenset({REJECTED, INCORPORATED})

#: Allowed moves. Reopening from a terminal state is permitted on purpose --
#: an "incorporated" comment that turns out not to have been drawn has to be
#: reopenable, and a back-door edit straight to the column would leave no
#: audit trail.
_TRANSITIONS: dict[str, frozenset[str]] = {
    OPEN: frozenset({ACCEPTED, REJECTED, DEFERRED}),
    ACCEPTED: frozenset({INCORPORATED, REJECTED, DEFERRED, OPEN}),
    DEFERRED: frozenset({ACCEPTED, REJECTED, OPEN}),
    REJECTED: frozenset({OPEN}),
    INCORPORATED: frozenset({OPEN}),
}


class ReviewStateError(Exception):
    """Raised on an illegal disposition transition."""


def validate_transition(current: str, requested: str) -> None:
    """Guard a disposition change, naming the legal moves when refusing."""
    if requested not in DISPOSITIONS:
        raise ReviewStateError(
            f"unknown disposition {requested!r}; expected one of {', '.join(DISPOSITIONS)}"
        )
    current = current or OPEN
    if current == requested:
        return
    allowed = _TRANSITIONS.get(current, frozenset())
    if requested not in allowed:
        raise ReviewStateError(
            f"cannot move a markup from {current!r} to {requested!r}; "
            f"from {current!r} the legal moves are: {', '.join(sorted(allowed)) or 'none'}"
        )


def requires_response(disposition: str) -> bool:
    """Rejecting or deferring a reviewer's comment needs a written reason.

    Accepting doesn't -- agreement is self-explanatory, and forcing a note
    there just produces a column full of "ok".
    """
    return disposition in (REJECTED, DEFERRED)


@dataclass
class RoundDiff:
    added: list[dict]
    modified: list[dict]
    unchanged: list[dict]
    withdrawn: list[dict]
    unmatchable_count: int

    def to_dict(self) -> dict:
        return {
            "added": self.added,
            "modified": self.modified,
            "unchanged": self.unchanged,
            "withdrawn": self.withdrawn,
            "counts": {
                "added": len(self.added),
                "modified": len(self.modified),
                "unchanged": len(self.unchanged),
                "withdrawn": len(self.withdrawn),
            },
            "unmatchable_count": self.unmatchable_count,
            "unmatchable_note": (
                f"{self.unmatchable_count} markup(s) carry no Revu /NM GUID, so a "
                "moved or edited copy reads as withdrawn plus added rather than "
                "as a modification."
            )
            if self.unmatchable_count
            else None,
        }


def _diff_entry(m: Markup, **extra) -> dict:
    row = m.to_dict()
    row.update(extra)
    return row


def diff_rounds(previous: list[Markup], current: list[Markup]) -> RoundDiff:
    """Classify `current` against `previous` as added/modified/unchanged/withdrawn."""
    prev_by_key = {m.key: m for m in previous}
    cur_by_key = {m.key: m for m in current}

    added: list[dict] = []
    modified: list[dict] = []
    unchanged: list[dict] = []

    for m in current:
        prior = prev_by_key.get(m.key)
        if prior is None:
            added.append(_diff_entry(m, change="added"))
        elif prior.content_fingerprint() != m.content_fingerprint():
            modified.append(
                _diff_entry(
                    m,
                    change="modified",
                    previous={
                        "contents": prior.contents,
                        "page_label": prior.page_label,
                        "rect": prior.rect,
                        "subject": prior.subject,
                    },
                )
            )
        else:
            unchanged.append(_diff_entry(m, change="unchanged"))

    withdrawn = [
        _diff_entry(m, change="withdrawn")
        for m in previous
        if m.key not in cur_by_key
    ]

    unmatchable = sum(1 for m in current if not m.nm) + sum(
        1 for m in previous if not m.nm
    )

    return RoundDiff(added, modified, unchanged, withdrawn, unmatchable)


def carry_forward_dispositions(
    diff: RoundDiff, previous_dispositions: dict[str, dict]
) -> dict[str, dict]:
    """Decide each markup's starting disposition in a newly uploaded round.

    An unchanged markup keeps whatever it was dispositioned as — re-deciding
    a comment nobody touched is busywork. A *modified* one is deliberately
    reset to `open`: the reviewer changed what they were asking for, so a
    prior "accepted" no longer refers to the same request. The previous
    decision is preserved in `reopened_from` so the reset is visible rather
    than looking like data loss.
    """
    carried: dict[str, dict] = {}

    for row in diff.unchanged:
        prior = previous_dispositions.get(row["key"])
        if prior:
            carried[row["key"]] = {**prior, "carried_from_previous_round": True}

    for row in diff.modified:
        prior = previous_dispositions.get(row["key"])
        if prior and prior.get("disposition") not in (None, OPEN):
            carried[row["key"]] = {
                "disposition": OPEN,
                "reopened_from": prior.get("disposition"),
                "response": prior.get("response"),
                "assigned_to": prior.get("assigned_to"),
                "carried_from_previous_round": True,
                "reset_reason": "reviewer changed this markup after it was dispositioned",
            }

    return carried


def rollup(items: list[dict]) -> dict:
    """Counts by disposition, plus the derived progress numbers the panel shows."""
    counts = {d: 0 for d in DISPOSITIONS}
    for item in items:
        counts[item.get("disposition") or OPEN] = (
            counts.get(item.get("disposition") or OPEN, 0) + 1
        )
    total = len(items)
    resolved = sum(counts[d] for d in TERMINAL_DISPOSITIONS)
    return {
        "total": total,
        "by_disposition": counts,
        "resolved": resolved,
        "outstanding": total - resolved,
        "percent_resolved": round(100.0 * resolved / total, 1) if total else 0.0,
    }


def approval_gate(items: list[dict]) -> dict:
    """Can this review set be signed off?

    Returns the decision plus every reason it can't be, grouped by cause, so
    the panel can show "3 accepted but not yet drawn" rather than a bare
    "not ready".
    """
    blockers: list[dict] = []

    def _collect(disposition: str, reason: str) -> None:
        rows = [i for i in items if (i.get("disposition") or OPEN) == disposition]
        if rows:
            blockers.append({
                "disposition": disposition,
                "count": len(rows),
                "reason": reason,
                "markups": [
                    {
                        "key": r.get("key"),
                        "page_label": r.get("page_label"),
                        "author": r.get("author"),
                        "contents": r.get("contents"),
                    }
                    for r in rows
                ],
            })

    _collect(OPEN, "no decision recorded yet")
    _collect(
        ACCEPTED,
        "accepted but not yet marked incorporated -- the drawing change hasn't been confirmed",
    )
    _collect(DEFERRED, "deferred items need an explicit decision before sign-off")

    summary = rollup(items)
    return {
        "ready_to_approve": not blockers and summary["total"] > 0,
        "blockers": blockers,
        "blocker_count": sum(b["count"] for b in blockers),
        "summary": summary,
        "empty": summary["total"] == 0,
    }
