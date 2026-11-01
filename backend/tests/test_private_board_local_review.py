"""Synthetic-only checks: no real Jira, project history, credentials or models."""

import fcntl
import importlib.util
import json
import subprocess
import threading
from pathlib import Path
from unittest.mock import Mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import mc_pi_runtime as shared


@pytest.fixture
def local(tmp_path, monkeypatch):
    path = Path(__file__).parents[2] / "scripts/private-local-review.py"
    spec = importlib.util.spec_from_file_location("private_local_review_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(shared, "QWEN_SLOT", tmp_path / "qwen.lock")
    monkeypatch.setenv("OPENCLAW_HOME", str(tmp_path))
    metadata = tmp_path / "openclaw.mc.json"
    metadata.write_text(json.dumps({"models": {module.PROVIDER: {
        module.MODEL: {"fully_trusted": True},
    }}}))
    metadata.chmod(0o600)
    module.INPUT_ROOTS = (tmp_path,)
    module.OUTPUT_ROOT = tmp_path / "restricted-output"
    module.OUTPUT_ROOT.mkdir(mode=0o700)
    return module


@pytest.mark.parametrize(
    "provider,model",
    [("omniroute-api", "vllm/Qwen3.8-27B-FP8"), ("local-qwen", "gpt-6.1-sol")],
)
def test_wrong_route_refuses_before_content_read(local, monkeypatch, provider, model):
    read = Mock(side_effect=AssertionError("must not read"))
    monkeypatch.setattr(local, "read_document", read)
    assert local.review(Path("/never-read"), provider, model)["status"] == "refused"
    read.assert_not_called()


def test_bad_storage_refuses_before_content_or_inference(local, monkeypatch):
    local.OUTPUT_ROOT.chmod(0o755)
    read = Mock(return_value="synthetic")
    call = Mock(
        return_value={
            "model": local.MODEL,
            "choices": [{"message": {"content": "synthetic"}}],
        }
    )
    monkeypatch.setattr(local, "read_document", read)
    monkeypatch.setattr(local, "call_model", call)
    assert local.review(Path("/synthetic"))["status"] == "refused"
    read.assert_not_called()
    call.assert_not_called()


def test_local_unavailable_has_no_fallback_or_raw_error(local, monkeypatch):
    monkeypatch.setattr(local, "read_document", lambda path: "synthetic")
    call = Mock(side_effect=OSError("SYNTHETIC_PRIVATE_ERROR"))
    monkeypatch.setattr(local, "call_model", call)
    result = local.review(Path("/synthetic"))
    assert result == {"status": "refused", "reason": "local_review_failed_closed"}
    assert call.call_count == 1 and not list(local.OUTPUT_ROOT.glob("*.json"))


def test_concurrent_local_reader_refuses_before_input_or_inference(local, monkeypatch):
    read = Mock(side_effect=AssertionError("must not read while busy"))
    monkeypatch.setattr(local, "read_document", read)
    with (local.OUTPUT_ROOT / "review.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert local.review(Path("/synthetic"))["status"] == "refused"
    read.assert_not_called()


def test_response_stays_private_and_requires_approval(local, monkeypatch):
    marker = "SYNTHETIC_PRIVATE_RESULT"
    monkeypatch.setattr(local, "read_document", lambda path: "synthetic")
    monkeypatch.setattr(
        local,
        "call_model",
        lambda document: {
            "model": local.MODEL,
            "choices": [{"message": {"content": marker}}],
        },
    )
    result = local.review(Path("/synthetic"))
    assert result["status"] == "completed_awaiting_disclosure_approval"
    assert marker not in json.dumps(result)
    path = local.OUTPUT_ROOT / (result["job_id"] + ".json")
    assert marker in path.read_text() and path.stat().st_mode & 0o777 == 0o600
    assert (
        json.loads(path.read_text())["classification"]
        == "restricted_local_only_unapproved"
    )


@pytest.mark.parametrize(
    "status,model",
    [(302, "Qwen3.8-27B-FP8"), (200, "cloud-other-model"), (503, "Qwen3.8-27B-FP8")],
)
def test_redirect_error_or_wrong_model_never_falls_back(
    local, monkeypatch, status, model
):
    response = Mock(status=status)
    response.read.return_value = json.dumps(
        {"model": model, "choices": [{"message": {"content": "SYNTHETIC_PRIVATE"}}]}
    ).encode()
    connection = Mock()
    connection.getresponse.return_value = response
    factory = Mock(return_value=connection)
    monkeypatch.setattr(local.http.client, "HTTPConnection", factory)
    monkeypatch.setenv("HTTP_PROXY", "http://cloud.invalid:8080")
    with pytest.raises(RuntimeError):
        local.call_model("synthetic")
    factory.assert_called_once_with("192.0.2.10", 8000, timeout=90)
    assert connection.request.call_count == 1
    connection.close.assert_called_once()


def test_reader_rejects_symlink_outside_scope_oversize_and_missing_isolation(
    local, tmp_path, monkeypatch
):
    source = tmp_path / "synthetic"
    source.write_text("synthetic")
    link = tmp_path / "link"
    link.symlink_to(source)
    with pytest.raises(ValueError):
        local.read_document(link)
    with pytest.raises(ValueError):
        local.read_document(Path("/etc/hosts"))
    source.write_bytes(b"x" * (local.MAX_INPUT + 1))
    with pytest.raises(ValueError):
        local.read_document(source)
    source.write_text("synthetic")
    monkeypatch.setattr(local.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError):
        local.read_document(source)


def test_reader_pins_source_before_projection(local, tmp_path, monkeypatch):
    source = tmp_path / "synthetic-source"
    source.write_text("original synthetic")
    outside = tmp_path / "synthetic-outside"
    outside.write_text("SYNTHETIC_FORBIDDEN_SWAP")
    real_run = subprocess.run

    def swap(command, **kwargs):
        source.rename(tmp_path / "original-renamed")
        source.symlink_to(outside)
        return real_run(command, **kwargs)

    monkeypatch.setattr(local.subprocess, "run", swap)
    assert local.read_document(source) == "original synthetic"


def test_real_reader_network_namespace_and_readonly_file(local, tmp_path, monkeypatch):
    source = tmp_path / "synthetic-document"
    source.write_text("synthetic")
    assert local.read_document(source) == "synthetic"
    commands = []
    real_run = subprocess.run

    def recording(command, **kwargs):
        commands.append((command, local.os.dup(kwargs["pass_fds"][0])))
        return real_run(command, **kwargs)

    monkeypatch.setattr(local.subprocess, "run", recording)
    assert local.read_document(source) == "synthetic"
    command, pinned = commands[-1]
    command[command.index("/input/document") - 1] = f"/proc/self/fd/{pinned}"
    assert (
        "--unshare-all" in command
        and "--clearenv" in command
        and "--cap-drop" in command
        and command[command.index("/proc") - 1] == "--tmpfs"
    )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        probe = (
            "import pathlib,socket,json; s=socket.socket(); s.settimeout(1); "
            f'network=s.connect_ex(("127.0.0.1",{server.server_port}))==0; '
            "readonly=False; "
            '\ntry: pathlib.Path("/input/document").write_text("tamper")'
            "\nexcept OSError: readonly=True"
            '\nprint(json.dumps({"network_denied":not network,"readonly":readonly,"host_root_hidden":not pathlib.Path("/root/.pi/agent/models.json").exists()}))'
        )
        command[-1] = probe
        result = real_run(
            command,
            pass_fds=(pinned,),
            env={"PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.returncode == 0
        assert json.loads(result.stdout) == {
            "network_denied": True,
            "readonly": True,
            "host_root_hidden": True,
        }
        assert source.read_text() == "synthetic"
    finally:
        local.os.close(pinned)
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
