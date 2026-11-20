import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("balancer", Path(__file__).resolve().parents[2] / "scripts/omniroute-quota-balancer.py")
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)


def test_most_weekly_headroom_first_and_exhausted_windows_last():
    accounts = {
        "acct-d": {"weekly": 1, "session": 7},
        "acct-a": {"weekly": 1, "session": 51},
        "prozocker": {"weekly": 49, "session": 100},
        "fresh_week_empty_session": {"weekly": 90, "session": 2},
    }
    assert b.order(accounts) == ["prozocker", "fresh_week_empty_session", "acct-a", "acct-d"]


def test_restricted_free_accounts_go_first_so_the_full_plan_keeps_its_quota():
    # 2026-11-04: two accounts fell back to free plans (no sol); luna drained the Teams plan's quota.
    accounts = {"teams": {"weekly": 90, "session": 90}, "free-a": {"weekly": 40, "session": 50, "restricted": True},
                "free-b": {"weekly": 0, "session": 80, "restricted": True}}
    assert b.order(accounts) == ["free-a", "teams", "free-b"]  # an exhausted account still goes last


def test_a_held_codex_pool_that_answers_probes_is_released():
    from datetime import datetime, timezone
    now = datetime(2026, 10, 27, 2, 15, tzinfo=timezone.utc)
    hold = {"unavailable_until": "2026-10-27T06:37:00+00:00", "availability_reason": "quota_exhausted"}
    models = {"gpt-6.1-sol": dict(hold), "gpt-6-luna": dict(hold)}
    released = []
    assert b.release_codex_hold(models, lambda m: True, lambda m, t: released.append(m), now) == ["gpt-6.1-sol", "gpt-6-luna"]
    assert released == ["gpt-6.1-sol"]
    # One model still at its threshold keeps the hold.
    released.clear()
    assert b.release_codex_hold(models, lambda m: m == "gpt-6-luna", lambda m, t: released.append(m), now) == []
    assert released == []
    # A hold ending within the next run, or a non-quota hold, is left alone without probing.
    models = {"gpt-6.1-sol": {"unavailable_until": "2026-10-27T02:20:00+00:00", "availability_reason": "quota_exhausted"},
              "gpt-6-luna": {"unavailable_until": "9999-01-01T00:00:00+00:00", "availability_reason": "credential_repair_required"}}
    assert b.release_codex_hold(models, lambda m: 1 / 0, released.append, now) == []


def test_single_window_free_plans_rank_by_their_primary_window():
    # OmniRoute 3.8.54 snapshot: free plans have one window and null weekly/session.
    accounts = {"teams": {"weekly": 100, "session": 99},
                "free-ok": {"unknown_primary": 60, "weekly": None, "session": None, "restricted": True},
                "free-empty": {"unknown_primary": 0, "weekly": None, "session": None, "restricted": True},
                "unobserved": {"weekly": None, "session": None}}
    assert b.order(accounts) == ["free-ok", "teams", "free-empty", "unobserved"]
