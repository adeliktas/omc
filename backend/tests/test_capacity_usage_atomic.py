"""Regression tests for concurrent sidecar metadata updates (T-capacity-atomic).

Defects under test (all exercised ONLY against disposable scratch sidecar
paths — production metadata is never read or written):

(a) Lost usage increments: model_ranking.record_usage reads observed_usage
    BEFORE mc_sidecar's metadata update lock, so concurrent callers race on
    a read-modify-write of the whole sidecar and drop increments. New /
    cache-creation input must be summed separately from cache reads.
(b) Concurrent temp-write collisions: mc_sidecar.save_sidecar uses a fixed
    `<sidecar>.json.tmp` filename, so concurrent writers (threads AND
    processes) can clobber each other's temp file and publish corrupt JSON.
(c) Manual `disabled` flags and unknown (non-schema) metadata keys must
    survive concurrent metadata updates.
(d) Replacement must be atomic (unique temp name, os.replace) — after heavy
    concurrency the sidecar must always be valid JSON, and a success event
    must never clear a sibling's still-live quota window.

Baseline expectation: the lost-increment, temp-collision, and
cache-creation tests FAIL against the current implementation. That is the
point of the regression suite.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import sys
import threading
from pathlib import Path

import pytest

from app.services import mc_sidecar
from app.services import model_ranking

PROVIDER = "scratch-provider"
MODEL = "scratch-model"


@pytest.fixture(autouse=True)
def scratch_sidecar(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the sidecar at a disposable path for the duration of a test."""
    scratch = tmp_path / "openclaw.mc.json"
    monkeypatch.setattr(mc_sidecar, "_SIDECAR_PATH", scratch)
    yield scratch
    # Safety: never leave production pointing at a scratch file.


def _seed_meta(**fields: object) -> None:
    mc_sidecar.set_model_meta(PROVIDER, MODEL, **fields)  # type: ignore[arg-type]


# ── (a) lost usage increments under concurrent record_usage ────────────────


def test_concurrent_record_usage_no_lost_increments(scratch_sidecar: Path) -> None:
    threads = 16
    per_thread = 25
    barrier = threading.Barrier(threads)

    def worker() -> None:
        barrier.wait()
        for _ in range(per_thread):
            model_ranking.record_usage(
                PROVIDER,
                MODEL,
                {
                    "input_tokens": 100,
                    "output_tokens": 40,
                    "cache_read_tokens": 10,
                    "cache_creation_input_tokens": 5,
                    "total_tokens": 155,
                    "cost_usd": 0.001,
                    "source": "cli_report",
                },
            )

    threads_list = [threading.Thread(target=worker) for _ in range(threads)]
    for t in threads_list:
        t.start()
    for t in threads_list:
        t.join()

    observed = mc_sidecar.get_model_meta(PROVIDER, MODEL).get("observed_usage") or {}
    calls = threads * per_thread
    assert observed.get("calls") == calls, (
        f"lost record_usage events: calls={observed.get('calls')} != {calls}"
    )
    assert observed.get("input_tokens") == calls * 100, observed
    assert observed.get("output_tokens") == calls * 40, observed
    assert observed.get("total_tokens") == calls * 155, observed


def test_cache_creation_input_summed_separately_from_cache_reads(
    scratch_sidecar: Path,
) -> None:
    model_ranking.record_usage(
        PROVIDER,
        MODEL,
        {
            "input_tokens": 1000,
            "cache_read_tokens": 500,
            "cache_creation_input_tokens": 300,
            "total_tokens": 1800,
        },
    )
    model_ranking.record_usage(
        PROVIDER,
        MODEL,
        {
            "input_tokens": 1,
            "cache_read_tokens": 2,
            "cache_creation_input_tokens": 3,
            "total_tokens": 6,
        },
    )
    observed = mc_sidecar.get_model_meta(PROVIDER, MODEL).get("observed_usage") or {}
    # Cache-read tokens must be tracked, and new/cache-creation input must be
    # accumulated under its own key — not folded into cache reads or input.
    assert observed.get("cache_read_tokens") == 502, observed
    assert observed.get("cache_creation_input_tokens") == 303, observed
    assert observed.get("input_tokens") == 1001, observed


def test_concurrent_record_usage_sums_cache_creation_across_threads(
    scratch_sidecar: Path,
) -> None:
    threads = 8
    per_thread = 20
    barrier = threading.Barrier(threads)

    def worker() -> None:
        barrier.wait()
        for _ in range(per_thread):
            model_ranking.record_usage(
                PROVIDER,
                MODEL,
                {
                    "cache_read_tokens": 7,
                    "cache_creation_input_tokens": 11,
                    "total_tokens": 18,
                },
            )

    threads_list = [threading.Thread(target=worker) for _ in range(threads)]
    for t in threads_list:
        t.start()
    for t in threads_list:
        t.join()

    observed = mc_sidecar.get_model_meta(PROVIDER, MODEL).get("observed_usage") or {}
    calls = threads * per_thread
    assert observed.get("cache_read_tokens") == calls * 7, observed
    assert observed.get("cache_creation_input_tokens") == calls * 11, observed


# ── (b) concurrent temp-write collisions / corrupt JSON ────────────────────


def test_concurrent_set_model_meta_no_lost_entries_or_corruption(
    scratch_sidecar: Path,
) -> None:
    """Many threads each create their own model entry concurrently. The
    final sidecar must be valid JSON and contain EVERY entry (no lost
    read-modify-write update) with no leftover temp-file debris."""
    threads = 16

    def worker(i: int) -> None:
        mc_sidecar.set_model_meta(PROVIDER, f"model-{i}", iq_score=i, note=f"n{i}")

    threads_list = [
        threading.Thread(target=worker, args=(i,)) for i in range(threads)
    ]
    for t in threads_list:
        t.start()
    for t in threads_list:
        t.join()

    data = mc_sidecar.load_sidecar()
    models = (data.get("models") or {}).get(PROVIDER) or {}
    assert set(models) == {f"model-{i}" for i in range(threads)}, (
        f"lost concurrent metadata entries: {sorted(models)}"
    )
    for i in range(threads):
        assert models[f"model-{i}"]["iq_score"] == i
    # Atomic replace must not leave temp files behind.
    leftovers = list(scratch_sidecar.parent.glob("*.json.tmp*"))
    assert leftovers == [], f"temp file debris left behind: {leftovers}"


def test_concurrent_updates_always_leave_valid_json(scratch_sidecar: Path) -> None:
    """Hammer the same entry from many threads; every intermediate on-disk
    snapshot must be parseable JSON (a fixed shared .json.tmp lets a reader
    racing os.replace observe a torn/partial file, and two writers can
    corrupt each other's temp payload)."""
    stop = threading.Event()
    errors: list[str] = []

    def writer(base: int) -> None:
        i = 0
        while not stop.is_set():
            mc_sidecar.set_model_meta(PROVIDER, MODEL, counter=base + i)
            i += 1

    def reader() -> None:
        while not stop.is_set():
            try:
                raw = scratch_sidecar.read_text()
            except FileNotFoundError:
                continue
            if not raw:
                continue
            try:
                json.loads(raw)
            except Exception as exc:
                errors.append(f"corrupt JSON observed: {exc}")
                stop.set()

    writers = [threading.Thread(target=writer, args=(k * 1_000_000,)) for k in range(8)]
    readers = [threading.Thread(target=reader) for _ in range(2)]
    for t in writers + readers:
        t.start()
    for t in writers:
        t.join(timeout=10)
    stop.set()
    for t in writers + readers:
        t.join(timeout=10)
    assert errors == [], errors


# ── (c) manual disabled flags and unknown metadata survive concurrency ─────


def test_manual_disabled_flag_survives_concurrent_updates(
    scratch_sidecar: Path,
) -> None:
    _seed_meta(disabled=True, disabled_reason="manual: owner parked this")
    barrier = threading.Barrier(8)

    def worker(i: int) -> None:
        barrier.wait()
        if i % 2 == 0:
            model_ranking.record_usage(
                PROVIDER, MODEL, {"input_tokens": 10, "total_tokens": 10}
            )
        else:
            mc_sidecar.set_model_meta(PROVIDER, MODEL, iq_score=i)

    threads_list = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads_list:
        t.start()
    for t in threads_list:
        t.join()

    meta = mc_sidecar.get_model_meta(PROVIDER, MODEL)
    assert meta.get("disabled") is True, meta
    assert meta.get("disabled_reason") == "manual: owner parked this", meta
    # Usage recorded concurrently must not be lost either.
    assert (meta.get("observed_usage") or {}).get("calls") == 4, meta


def test_unknown_metadata_keys_survive_concurrent_updates(
    scratch_sidecar: Path,
) -> None:
    custom = {"deep": {"nested": [1, 2, 3]}, "owner_note": "keep me"}
    _seed_meta(custom_field=custom, another_unknown="x")

    def worker() -> None:
        model_ranking.record_usage(
            PROVIDER, MODEL, {"input_tokens": 5, "total_tokens": 5}
        )
        mc_sidecar.set_model_meta(PROVIDER, MODEL, iq_score=1)

    threads_list = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads_list:
        t.start()
    for t in threads_list:
        t.join()

    meta = mc_sidecar.get_model_meta(PROVIDER, MODEL)
    assert meta.get("custom_field") == custom, meta
    assert meta.get("another_unknown") == "x", meta
    assert (meta.get("observed_usage") or {}).get("calls") == 8, meta


def test_siblings_and_unknown_provider_metadata_survive_usage_recording(
    scratch_sidecar: Path,
) -> None:
    """record_usage's quota path touches every sibling in the provider; the
    siblings' unrelated (unknown) metadata must survive, and other
    providers must be untouched entirely."""
    _seed_meta(disabled=False, custom_field={"a": 1})
    mc_sidecar.set_model_meta(PROVIDER, "sibling", custom_field={"b": 2})
    mc_sidecar.set_model_meta("other-provider", "m", custom_field={"c": 3})

    model_ranking.record_usage(
        PROVIDER,
        MODEL,
        {"input_tokens": 1, "total_tokens": 1, "unavailable_until": "2030-01-01T00:00:00Z"},
    )

    meta = mc_sidecar.get_model_meta(PROVIDER, MODEL)
    assert meta.get("custom_field") == {"a": 1}, meta
    assert meta.get("disabled") is False, meta
    assert meta.get("unavailable_until") == "2030-01-01T00:00:00Z", meta
    sibling = mc_sidecar.get_model_meta(PROVIDER, "sibling")
    assert sibling.get("custom_field") == {"b": 2}, sibling
    assert sibling.get("unavailable_until") == "2030-01-01T00:00:00Z", sibling
    other = mc_sidecar.get_model_meta("other-provider", "m")
    assert other.get("custom_field") == {"c": 3}, other


# ── (d) quota windows: a later success must not clear a live sibling window ─


def test_success_does_not_clear_live_sibling_quota_window(
    scratch_sidecar: Path,
) -> None:
    mc_sidecar.set_model_meta(PROVIDER, "a", unavailable_until="2030-01-01T00:00:00Z")
    model_ranking.record_usage(
        PROVIDER, "b", {"success": True, "input_tokens": 1, "total_tokens": 1}
    )
    # b's success clears only b (b had no window); a's window must survive.
    assert mc_sidecar.get_model_meta(PROVIDER, "a").get("unavailable_until") == (
        "2030-01-01T00:00:00Z"
    )


def test_success_does_not_clear_own_live_quota_window(scratch_sidecar: Path) -> None:
    mc_sidecar.set_model_meta(
        PROVIDER, MODEL, unavailable_until="2030-01-01T00:00:00Z"
    )
    model_ranking.record_usage(
        PROVIDER, MODEL, {"success": True, "input_tokens": 1, "total_tokens": 1}
    )
    assert mc_sidecar.get_model_meta(PROVIDER, MODEL).get("unavailable_until") == (
        "2030-01-01T00:00:00Z"
    )


# ── cross-process concurrency (native/stdlib file locking) ─────────────────


def _proc_worker(path: str, i: int) -> None:
    """Runs in a forked child: inherits the patched _SIDECAR_PATH via fork."""
    for _ in range(20):
        mc_sidecar.set_model_meta(PROVIDER, f"proc-{i}", counter=i)
        model_ranking.record_usage(
            PROVIDER, MODEL, {"input_tokens": 3, "total_tokens": 3}
        )


@pytest.mark.skipif(
    not hasattr(os, "fork"), reason="fork-based process pool not available"
)
def test_cross_process_writers_no_lost_updates_or_corruption(
    scratch_sidecar: Path,
) -> None:
    procs = 8
    events_per_proc = 20
    ctx = multiprocessing.get_context("fork")
    with ctx.Pool(procs) as pool:
        pool.starmap(_proc_worker, [(str(scratch_sidecar), i) for i in range(procs)])

    data = json.loads(scratch_sidecar.read_text())
    models = (data.get("models") or {}).get(PROVIDER) or {}
    # Workers legitimately create MODEL via record_usage in addition to each
    # proc's own proc-i entry.
    expected_models = {f"proc-{i}" for i in range(procs)} | {MODEL}
    assert set(models) == expected_models, (
        f"lost cross-process entries: {sorted(models)}"
    )
    observed = models[MODEL].get("observed_usage") or {}
    expected_calls = procs * events_per_proc
    assert observed.get("calls") == expected_calls, (
        f"lost cross-process record_usage events: "
        f"calls={observed.get('calls')} != {expected_calls}"
    )
    assert observed.get("input_tokens") == expected_calls * 3, observed
