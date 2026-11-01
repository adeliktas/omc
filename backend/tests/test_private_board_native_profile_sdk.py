"""Actual MC guest, SDK, guarded tool and result protocol; synthetic HTTP, no AI calls."""
import importlib.util
import json
import shlex
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
spec = importlib.util.spec_from_file_location("private_sdk_runner", SCRIPTS / "mc-team-runner.py")
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


@pytest.mark.parametrize("fault", [None, "missing_tool", "broken_sandbox", "invalid_model", "unsafe_evidence", "invalid_version", "missing_version"])
def test_actual_private_sdk_protocol_or_startup_refusal(monkeypatch, tmp_path, fault):
    source = tmp_path / "source"
    scratch = tmp_path / "private"
    source.mkdir()
    scratch.mkdir(mode=0o700)
    r.evidence_dir(scratch).mkdir(mode=0o700)
    runtime = scratch / "api-runtime"
    runtime.mkdir(mode=0o700)
    (source / "sample.txt").write_text("synthetic source")
    (source / "AGENTS.md").write_text("SYNTHETIC_PROJECT_CONTEXT_MUST_NOT_AUTOLOAD")
    (source / ".pi").mkdir()
    (source / ".pi" / "settings.json").write_text('{"defaultTools":["read","write","bash"],"shellCommandPrefix":"exit 99;"}')
    requests = []
    probe = (
        "import http.client,os;from pathlib import Path;"
        "assert 'FIXTURE_LOG' not in os.environ;"
        f"assert not Path({str(runtime / 'models.json')!r}).exists();"
        "assert not Path('/proc/self/status').exists()\n"
        f"assert Path({str(scratch / 'qa-evidence.json')!r}).read_text() == 'synthetic metadata'\n"
        f"assert Path({str(scratch / 'qa-changes.diff')!r}).read_text() == 'synthetic patch'\n"
        f"assert Path({str(scratch / 'acceptance-evidence.json')!r}).read_text() == 'synthetic bundle'\n"
        f"try:\n Path({str(scratch / 'qa-changes.diff')!r}).write_text('denied')\nexcept OSError: pass\nelse: raise AssertionError('review evidence became writable')\n"
        "try:\n Path('sample.txt').write_text('denied')\nexcept OSError: pass\nelse: raise AssertionError('readonly source became writable')\n"
        "client=http.client.HTTPConnection('127.0.0.1',PORT,timeout=1)\n"
        "try:\n client.request('GET','/must-be-denied'); client.getresponse(); reachable=True\n"
        "except OSError: reachable=False\nfinally: client.close()\n"
        "assert not reachable\n"
    )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append({"leak": self.path})
            self.send_response(200)
            self.end_headers()

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            index = len(requests)
            delta = {"role": "assistant"}
            if index == 1:
                delta["tool_calls"] = [{"index": 0, "id": "synthetic-bash-1", "type": "function",
                                        "function": {"name": "bash", "arguments": json.dumps({"command": command})}}]
            else:
                delta["content"] = "Synthetic SDK protocol complete"
            chunks = [
                {"id": "synthetic", "object": "chat.completion.chunk", "created": 1, "model": "fixture",
                 "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                {"id": "synthetic", "object": "chat.completion.chunk", "created": 1, "model": "fixture",
                 "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if index == 1 else "stop"}],
                 "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}},
            ]
            data = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data.encode())))
            self.end_headers()
            self.wfile.write(data.encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    command = "/usr/bin/python3 -P -c " + shlex.quote(probe.replace('PORT', str(server.server_port)))
    command += " && cat sample.txt && printf synthetic > /tmp/guarded-artifact"
    try:
        (runtime / "models.json").write_text(json.dumps({"providers": {"fixture": {
            "baseUrl": f"http://127.0.0.1:{server.server_port}/v1", "api": "openai-completions",
            "apiKey": "synthetic-not-a-real-credential", "models": [{"id": "fixture", "name": "fixture",
                "reasoning": False, "input": ["text"], "contextWindow": 57344, "maxTokens": 16384,
                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}}]}}}))
        (scratch / "qa-evidence.json").write_text("synthetic metadata")
        (scratch / "acceptance-evidence.json").write_text("synthetic bundle")
        if fault == "unsafe_evidence":
            (scratch / "qa-changes.diff").symlink_to(runtime / "models.json")
        else:
            (scratch / "qa-changes.diff").write_text("synthetic patch")
        (runtime / "auth.json").write_text("{}")
        (runtime / "settings.json").write_text('{"compaction":{"enabled":true,"reserveTokens":16384,"keepRecentTokens":12000}}')
        helper = tmp_path / "trusted-helper"
        helper.mkdir()
        worker = (SCRIPTS / "private-pi-worker.mjs").read_text()
        if fault == "missing_tool":
            worker = worker.replace("customTools: [bash]", "customTools: []")
        (helper / "private-pi-worker.mjs").write_text(worker)
        # All source/protocol code is the real candidate. Only these cause-specific
        # faults are injected into the owned helper copy or the existing guest.
        (helper / "mc-agent-result.py").write_bytes((SCRIPTS / "mc-agent-result.py").read_bytes())
        monkeypatch.setattr(r, "HERE", helper)
        monkeypatch.setattr(r, "BOARD", next(iter(r.PRIVATE_BOARDS)))
        monkeypatch.setattr(r, "STOP", threading.Event())
        monkeypatch.setenv("RUNNER_FIXTURES", "1")
        monkeypatch.setenv("FIXTURE_LOG", str(tmp_path / "fixture.log"))
        monkeypatch.setattr(r, "harness_cmd", lambda *a: (["pi", "--provider", "fixture", "--model", "missing-model" if fault == "invalid_model" else "fixture"],
                                                        {"PI_CODING_AGENT_DIR": str(runtime)}))
        posts = []
        monkeypatch.setattr(r, "api", lambda *a, **kw: posts.append((a, kw)))
        original = r.sandbox_command
        if fault in ("invalid_version", "missing_version"):
            version_file = scratch / "synthetic-current-version"
            version_file.write_text("../outside-managed-releases")
            empty_install = scratch / "empty-install"
            empty_install.mkdir()
            def sandbox(*a, **kw):
                argv = original(*a, **kw)
                pos = argv.index("--chdir")
                mount = (["--ro-bind", str(version_file), "/root/.pi/agent/install/current-version"]
                         if fault == "invalid_version" else
                         ["--ro-bind", str(empty_install), "/root/.pi/agent/install"])
                return argv[:pos] + mount + argv[pos:]
            monkeypatch.setattr(r, "sandbox_command", sandbox)
        if fault == "broken_sandbox":
            broken = scratch / "broken-bwrap"
            broken.write_text("#!/bin/sh\nexit 127\n")
            broken.chmod(0o700)
            def sandbox(*a, **kw):
                argv = original(*a, **kw)
                pos = argv.index("--chdir")
                return argv[:pos] + ["--ro-bind", str(broken), "/usr/bin/bwrap"] + argv[pos:]
            monkeypatch.setattr(r, "sandbox_command", sandbox)
        claim = {"attempt": {"id": "synthetic-protocol-only", "provider": "omniroute-codex",
                             "model": "gpt-6.1-sol", "thinking_level": "off"}, "role_kind": "qa"}
        result = r._run_harness(claim, "Synthetic fixture: use the offered bash, then finish", source, scratch, 25)
        if fault:
            assert result["success"] is False and result["retryable"] is False
            assert result["usage_observed"] is False
            assert requests == posts == []  # failure BEFORE even a synthetic inference request
        else:
            assert result["success"], result
            assert result["tool_counts"] == {"bash": 1}
            assert result["tool_activity"] == [{"tool": "bash", "is_error": False}]
            assert result["usage_observed"] is True  # fixture-reported data, NOT measured AI/billing usage
            assert result["usage"]["total_tokens"] == 26
            assert len(requests) == 2 and len(posts) == 1
            assert "SYNTHETIC_PROJECT_CONTEXT_MUST_NOT_AUTOLOAD" not in json.dumps(requests)
            assert all(req.get("max_tokens", req.get("max_completion_tokens")) == 16384 for req in requests)
            assert (scratch / "out.raw").stat().st_mode & 0o777 == 0o600
            assert [[tool["function"]["name"] for tool in req["tools"]] for req in requests] == [["bash"], ["bash"]]
            assert "synthetic source" in json.dumps(requests[1]["messages"])
            assert (r.evidence_dir(scratch) / "guarded-artifact").read_text() == "synthetic"
            assert json.loads((scratch / "out.raw").read_text().split("\n")[0])["id"] == "mc-team-synthetic-protocol-only"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
