from datetime import UTC, datetime, timedelta

import pytest

from agent_commerce.core.clock import FixedClock
from agent_commerce.policy.approvals import ApprovalStore


class _MutableClock:
    """Test-only clock whose 'now' can be advanced, to exercise timeout expiry without
    sleeping in tests.
    """

    def __init__(self, start: datetime) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: int) -> None:
        self._now += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> _MutableClock:
    return _MutableClock(datetime(2026, 1, 1, tzinfo=UTC))


@pytest.fixture
def store(tmp_path, clock: _MutableClock) -> ApprovalStore:
    return ApprovalStore(tmp_path / "approvals.db", clock=clock)


def _create(store: ApprovalStore, timeout_seconds: int = 300):
    return store.create(
        transaction_id="txn_1",
        ledger_entry_id="entry_abc",
        tool_name="checkout.confirm",
        arguments={"cart": {"total_paise": 600_000}},
        timeout_seconds=timeout_seconds,
    )


def test_create_and_get(store: ApprovalStore) -> None:
    created = _create(store)
    fetched = store.get(created.approval_id)
    assert fetched is not None
    assert fetched.status == "pending"
    assert fetched.tool_name == "checkout.confirm"
    assert fetched.arguments == {"cart": {"total_paise": 600_000}}


def test_get_unknown_returns_none(store: ApprovalStore) -> None:
    assert store.get("appr_does_not_exist") is None


def test_list_pending(store: ApprovalStore) -> None:
    a = _create(store)
    b = _create(store)
    pending = store.list_pending()
    assert {p.approval_id for p in pending} == {a.approval_id, b.approval_id}


def test_approve_resolves_and_removes_from_pending(store: ApprovalStore) -> None:
    created = _create(store)
    resolved = store.approve(created.approval_id)
    assert resolved.status == "approved"
    assert resolved.decided_at is not None
    assert store.list_pending() == []


def test_deny_resolves(store: ApprovalStore) -> None:
    created = _create(store)
    resolved = store.deny(created.approval_id)
    assert resolved.status == "denied"


def test_cannot_decide_twice(store: ApprovalStore) -> None:
    created = _create(store)
    store.approve(created.approval_id)
    with pytest.raises(ValueError, match="already resolved"):
        store.approve(created.approval_id)
    with pytest.raises(ValueError, match="already resolved"):
        store.deny(created.approval_id)


def test_decide_unknown_approval_raises(store: ApprovalStore) -> None:
    with pytest.raises(ValueError, match="unknown approval_id"):
        store.approve("appr_does_not_exist")


def test_expire_overdue_fails_closed(store: ApprovalStore, clock: _MutableClock) -> None:
    created = _create(store, timeout_seconds=60)
    clock.advance(61)
    expired = store.expire_overdue()
    assert [e.approval_id for e in expired] == [created.approval_id]
    assert store.get(created.approval_id).status == "timed_out"


def test_expire_overdue_leaves_fresh_approvals_pending(store: ApprovalStore, clock: _MutableClock) -> None:
    created = _create(store, timeout_seconds=300)
    clock.advance(60)  # well under the timeout
    expired = store.expire_overdue()
    assert expired == []
    assert store.get(created.approval_id).status == "pending"


def test_expire_overdue_does_not_touch_already_decided(store: ApprovalStore, clock: _MutableClock) -> None:
    created = _create(store, timeout_seconds=60)
    store.approve(created.approval_id)
    clock.advance(61)
    expired = store.expire_overdue()
    assert expired == []
    assert store.get(created.approval_id).status == "approved"


def test_fixed_clock_is_respected_for_created_at(tmp_path) -> None:
    fixed = FixedClock(datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC))
    store = ApprovalStore(tmp_path / "approvals2.db", clock=fixed)
    created = _create(store)
    assert created.created_at == "2026-06-01T12:00:00+00:00"
