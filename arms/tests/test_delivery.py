from concurrent.futures import ThreadPoolExecutor

import pytest

from research_arms import ArmsRegistry, Unavailable
from test_registry import setup, shared, turn


def answered(arms):
    conv = shared(arms)
    ident = turn(arms, conv)
    assert arms.claim() == ident
    arms.save_answer(ident, "An answer @everyone must not cause mentions", "entry_1")
    return ident


def test_one_delivery_claim_and_idempotent_confirmation(setup):
    arms, _ = setup
    ident = answered(arms)
    with ThreadPoolExecutor(max_workers=3) as pool:
        claims = list(pool.map(lambda _: arms.begin_delivery(ident), range(3)))
    delivery, = [item for item in claims if item]
    assert delivery["channel_id"] == "21"
    assert delivery["nonce"].isdigit()
    arms.validate_delivery(delivery["delivery_id"])
    arms.confirm_delivery(delivery["delivery_id"], "900")
    arms.confirm_delivery(delivery["delivery_id"], "900")
    assert arms.begin_delivery(ident) is None
    with pytest.raises(ValueError, match="conflicts"):
        arms.confirm_delivery(delivery["delivery_id"], "901")


def test_uncertain_delivery_never_automatically_resends(setup):
    arms, spaces = setup
    ident = answered(arms)
    delivery = arms.begin_delivery(ident)
    reopened = ArmsRegistry(arms.root, spaces)
    with pytest.raises(ValueError):
        reopened.recover_stopped_workers()
    result = reopened.recover_stopped_workers(confirmed_stopped=True)
    assert result["uncertain_deliveries"] == 1
    assert reopened.begin_delivery(ident) is None
    with pytest.raises(ValueError):
        reopened.confirm_delivery(delivery["delivery_id"], "900")
    reopened.confirm_delivery(delivery["delivery_id"], "900", reconciled=True)


def test_revocation_blocks_pending_send_but_keeps_late_outcome(setup):
    arms, spaces = setup
    ident = answered(arms)
    delivery = arms.begin_delivery(ident)
    spaces.set_membership("project", "bob", None)
    with pytest.raises(Unavailable):
        arms.validate_delivery(delivery["delivery_id"])
    arms.revoke_stale()
    arms.confirm_delivery(delivery["delivery_id"], "900", reconciled=True)
    with arms.connect(readonly=True) as db:
        assert db.execute("SELECT answer FROM turns WHERE id=?", (ident,)).fetchone()[0]


def test_interrupted_prompt_quarantines_session_not_replayed(setup):
    arms, _ = setup
    conv = shared(arms)
    first = turn(arms, conv)
    turn(arms, conv, message="101")
    assert arms.claim() == first
    assert arms.recover_stopped_workers(confirmed_stopped=True)["conversations_quarantined"] == 1
    assert arms.claim() is None
    with pytest.raises(Unavailable):
        arms.save_answer(first, "late result", "entry_late")
