# ruff: noqa: S101
"""Patch 1 (2026-07-16): structural + canonical session-key validators.

The openclaw gateway (>= v2026.6.11) refuses agent session keys with fewer
than 3 colon-separated parts ("Malformed agent session key"); MC stores the
delivery address in `agents.openclaw_session_id`. These tests pin the
validator contract: structural shape for ALL keys, canonical-builder
equality ONLY for keys claiming the MC/lead forms, and hands-off behavior
for bridge/gateway keys.
"""

from __future__ import annotations

from uuid import uuid4

from app.services.openclaw.internal.session_keys import (
    board_agent_session_key,
    board_lead_session_key,
    canonical_session_key_mismatch,
    is_gateway_parseable_session_key,
)


# ── structural shape ────────────────────────────────────────────────────────


def test_canonical_worker_key_is_parseable() -> None:
    assert is_gateway_parseable_session_key(board_agent_session_key(uuid4()))


def test_canonical_lead_key_is_parseable() -> None:
    assert is_gateway_parseable_session_key(board_lead_session_key(uuid4()))


def test_bridge_style_key_is_parseable() -> None:
    # Non-canonical but valid 3-part keys (gateway bridges) must pass.
    assert is_gateway_parseable_session_key("agent:example-bridge:main")


def test_bare_non_agent_keys_accepted() -> None:
    # Smallest safe validator: only keys CLAIMING the agent form are held
    # to the >=3-part contract. Plain/legacy session keys are not provably
    # malformed and must remain accepted (bridge/noncanonical forms).
    assert is_gateway_parseable_session_key("lead-session")
    assert is_gateway_parseable_session_key("session-worker")


def test_agent_form_two_part_key_rejected() -> None:
    # The exact 2026-07-06 incident shape: `agent:mc-<uuid>` (no `:main`).
    assert not is_gateway_parseable_session_key(f"agent:mc-{uuid4()}")


def test_empty_and_none_rejected() -> None:
    assert not is_gateway_parseable_session_key("")
    assert not is_gateway_parseable_session_key(None)


def test_agent_form_empty_part_rejected() -> None:
    assert not is_gateway_parseable_session_key("agent::main")
    assert not is_gateway_parseable_session_key("agent:mc-x:  ")


# ── canonical drift ─────────────────────────────────────────────────────────


def test_matching_worker_key_has_no_mismatch() -> None:
    aid = uuid4()
    key = board_agent_session_key(aid)
    assert (
        canonical_session_key_mismatch(
            key, agent_id=aid, board_id=uuid4(), is_board_lead=False
        )
        is None
    )


def test_matching_lead_key_has_no_mismatch() -> None:
    bid = uuid4()
    key = board_lead_session_key(bid)
    assert (
        canonical_session_key_mismatch(
            key, agent_id=uuid4(), board_id=bid, is_board_lead=True
        )
        is None
    )


def test_wrong_uuid_worker_key_reports_expected() -> None:
    aid = uuid4()
    stale = board_agent_session_key(uuid4())  # someone else's key
    expected = canonical_session_key_mismatch(
        stale, agent_id=aid, board_id=None, is_board_lead=False
    )
    assert expected == board_agent_session_key(aid)


def test_lead_agent_with_worker_key_reports_lead_expected() -> None:
    bid = uuid4()
    stale = board_agent_session_key(uuid4())
    expected = canonical_session_key_mismatch(
        stale, agent_id=uuid4(), board_id=bid, is_board_lead=True
    )
    assert expected == board_lead_session_key(bid)


def test_lead_form_without_board_id_is_unverifiable() -> None:
    # No board_id → cannot compute the expected lead key → no false positive.
    assert (
        canonical_session_key_mismatch(
            f"agent:lead-{uuid4()}:main",
            agent_id=uuid4(),
            board_id=None,
            is_board_lead=True,
        )
        is None
    )


def test_gateway_main_key_never_flagged() -> None:
    assert (
        canonical_session_key_mismatch(
            f"agent:mc-gateway-{uuid4()}:main",
            agent_id=uuid4(),
            board_id=uuid4(),
            is_board_lead=False,
        )
        is None
    )


def test_bridge_key_never_flagged() -> None:
    assert (
        canonical_session_key_mismatch(
            "agent:example-bridge:main",
            agent_id=uuid4(),
            board_id=uuid4(),
            is_board_lead=False,
        )
        is None
    )
