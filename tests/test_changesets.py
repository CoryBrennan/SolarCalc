"""Changeset lifecycle logic, tested directly against an in-memory DB
session (no HTTP layer) — create, dedupe-on-refresh, and the retry policy
(5 failures -> needs_attention -> manual retry resets the counter).
"""

from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app import changesets


def make_session() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def test_create_changeset_defaults_to_pending():
    with make_session() as session:
        cs = changesets.create_changeset(session, "SWBD-1", "SWITCHBOARD", "regenerate", {"a": 1})
        assert cs.status == "pending"
        assert cs.retry_count == 0
        assert cs.id.startswith("cs-")


def test_refresh_creates_new_changeset_when_config_differs():
    with make_session() as session:
        first, created1 = changesets.refresh_changeset(session, "SWBD-1", "SWITCHBOARD", "regenerate", {"bus": 1200})
        assert created1 is True

        second, created2 = changesets.refresh_changeset(session, "SWBD-1", "SWITCHBOARD", "regenerate", {"bus": 1600})
        assert created2 is True
        assert second.id != first.id


def test_refresh_is_a_noop_when_config_is_unchanged():
    with make_session() as session:
        first, created1 = changesets.refresh_changeset(session, "SWBD-1", "SWITCHBOARD", "regenerate", {"bus": 1200})
        assert created1 is True

        same, created2 = changesets.refresh_changeset(session, "SWBD-1", "SWITCHBOARD", "regenerate", {"bus": 1200})
        assert created2 is False
        assert same.id == first.id


def test_refresh_ignores_key_order_when_comparing_config():
    with make_session() as session:
        first, _ = changesets.refresh_changeset(session, "SWBD-1", "SWITCHBOARD", "regenerate", {"a": 1, "b": 2})
        _same, created = changesets.refresh_changeset(session, "SWBD-1", "SWITCHBOARD", "regenerate", {"b": 2, "a": 1})
        assert created is False


def test_mark_applied_resets_retry_state():
    with make_session() as session:
        cs = changesets.create_changeset(session, "SWBD-1", "SWITCHBOARD", "regenerate", {})
        cs = changesets.mark_failed(session, cs, "boom")
        assert cs.retry_count == 1

        cs = changesets.mark_applied(session, cs)
        assert cs.status == "applied"
        assert cs.retry_count == 0
        assert cs.last_error is None


def test_needs_attention_after_five_failures():
    with make_session() as session:
        cs = changesets.create_changeset(session, "SWBD-1", "SWITCHBOARD", "regenerate", {})
        for i in range(1, 5):
            cs = changesets.mark_failed(session, cs, f"attempt {i}")
            assert cs.status == "pending"
        cs = changesets.mark_failed(session, cs, "attempt 5")
        assert cs.status == "needs_attention"
        assert cs.retry_count == 5
        assert cs.last_error == "attempt 5"


def test_retry_resets_needs_attention_back_to_pending():
    with make_session() as session:
        cs = changesets.create_changeset(session, "SWBD-1", "SWITCHBOARD", "regenerate", {})
        for i in range(5):
            cs = changesets.mark_failed(session, cs, "boom")
        assert cs.status == "needs_attention"

        cs = changesets.retry(session, cs)
        assert cs.status == "pending"
        assert cs.retry_count == 0
        assert cs.last_error is None


def test_list_changesets_filters_by_status_and_tag():
    with make_session() as session:
        changesets.create_changeset(session, "SWBD-1", "SWITCHBOARD", "regenerate", {})
        cs2 = changesets.create_changeset(session, "SWBD-2", "SWITCHBOARD", "regenerate", {})
        changesets.mark_applied(session, cs2)

        pending = changesets.list_changesets(session, status="pending")
        assert [c.target_tag for c in pending] == ["SWBD-1"]

        for_swbd2 = changesets.list_changesets(session, target_tag="SWBD-2")
        assert len(for_swbd2) == 1
        assert for_swbd2[0].status == "applied"
