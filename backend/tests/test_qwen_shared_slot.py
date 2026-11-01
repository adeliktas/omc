"""Synthetic managed-native/local-reader concurrency; no inference or API work."""

import importlib.util
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

import mc_pi_runtime as shared


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    obj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj


def test_slot_exclusion_deadline_and_cancel(tmp_path, monkeypatch):
    monkeypatch.setattr(shared, "QWEN_SLOT", tmp_path / "qwen.lock", raising=False)
    with shared.qwen_slot(1) as remaining:
        assert 0 < remaining <= 1
        with pytest.raises(TimeoutError):
            with shared.qwen_slot(0.05):
                pytest.fail("two holders")
    with shared.qwen_slot(1):
        pass
    with pytest.raises(TimeoutError):
        with shared.qwen_slot(1, lambda: True):
            pytest.fail("cancelled acquisition")


def test_native_wrapper_and_reader_use_same_slot(tmp_path, monkeypatch):
    root = Path(__file__).parents[2]
    runner = module("qwen_native_slot_test", root / "scripts/mc-team-runner.py")
    local = module("qwen_local_slot_test", root / "scripts/private-local-review.py")
    monkeypatch.setattr(shared, "QWEN_SLOT", tmp_path / "qwen.lock", raising=False)
    local.OUTPUT_ROOT = tmp_path / "private"
    local.OUTPUT_ROOT.mkdir(mode=0o700)
    read = Mock(side_effect=AssertionError("no input while slot is busy"))
    monkeypatch.setattr(local, "read_document", read)
    monkeypatch.setattr(
        local, "qwen_slot", lambda timeout: shared.qwen_slot(0.05), raising=False
    )
    run = Mock(side_effect=AssertionError("no inference while slot is busy"))
    monkeypatch.setattr(runner, "_run_harness", run)
    monkeypatch.setattr(runner, "api", Mock(return_value={}))
    monkeypatch.setattr(runner, "log", Mock())
    claim = {
        "role_kind": "pm",
        "attempt": {"provider": "vllm", "model": "Qwen3.8-27B-FP8"},
    }
    with shared.qwen_slot(1):
        assert local.review(tmp_path / "synthetic")["status"] == "refused"
        assert (
            runner.run_harness(claim, "synthetic", tmp_path, tmp_path, 0.05)["success"]
            is False
        )
    read.assert_not_called()
    run.assert_not_called()


def test_process_exclusion_and_unsafe_lock_refusal(tmp_path, monkeypatch):
    path = tmp_path / "qwen.lock"
    monkeypatch.setattr(shared, "QWEN_SLOT", path)
    code = (
        "from pathlib import Path; import mc_pi_runtime as s; "
        f"s.QWEN_SLOT=Path({str(path)!r}); "
        "\ntry:\n with s.qwen_slot(.05): raise AssertionError('second holder')"
        "\nexcept TimeoutError: print('excluded')"
    )
    with shared.qwen_slot(1):
        result = subprocess.run(
            [sys.executable, "-P", "-c", code],
            env={
                "PYTHONPATH": str(Path(shared.__file__).parent),
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            capture_output=True,
            text=True,
            timeout=3,
        )
        assert result.returncode == 0 and result.stdout.strip() == "excluded"
    path.chmod(0o666)
    with pytest.raises(RuntimeError):
        with shared.qwen_slot(1):
            pytest.fail("unsafe lock")
    path.unlink()
    target = tmp_path / "target"
    target.write_text("synthetic")
    path.symlink_to(target)
    with pytest.raises(OSError):
        with shared.qwen_slot(1):
            pytest.fail("symlink lock")


def test_native_holds_slot_and_cloud_is_not_globally_serialized(tmp_path, monkeypatch):
    root = Path(__file__).parents[2]
    runner = module("qwen_native_slot_positive", root / "scripts/mc-team-runner.py")
    monkeypatch.setattr(shared, "QWEN_SLOT", tmp_path / "qwen.lock")
    monkeypatch.setattr(runner, "api", Mock(side_effect=AssertionError("no API work")))

    def inference(*args):
        with pytest.raises(TimeoutError):
            with shared.qwen_slot(0.05):
                pytest.fail("native released slot before inference")
        return {"success": True}

    monkeypatch.setattr(runner, "_run_harness", inference)
    claim = {
        "role_kind": "pm",
        "attempt": {"provider": "vllm", "model": "Qwen3.8-27B-FP8"},
    }
    assert runner.run_harness(claim, "synthetic", tmp_path, tmp_path, 3)["success"]
    cloud = Mock(return_value={"success": True})
    monkeypatch.setattr(runner, "_run_harness", cloud)
    claim["attempt"] = {"provider": "claude-code", "model": "opus-5-5"}
    with shared.qwen_slot(1):
        assert runner.run_harness(claim, "synthetic", tmp_path, tmp_path, 3)["success"]
    cloud.assert_called_once()
