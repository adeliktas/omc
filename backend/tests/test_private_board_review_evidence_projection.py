"""Independent synthetic actual-caller projection regression; no real provider or API."""
import importlib.util
import json
import shlex
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_private_board_acceptance_caller_preserves_required_evidence(monkeypatch, tmp_path):
    review = load("independent_review", ROOT / "scripts/mc-review-agent.py")
    r = review.team
    source, scratch = tmp_path / "source", tmp_path / "private"
    source.mkdir()
    scratch.mkdir(mode=0o700)
    r.evidence_dir(scratch).mkdir(mode=0o700)
    (source / "sample.txt").write_text("SYNTHETIC_CURRENT_SOURCE")
    runtime = scratch / "api-runtime"
    runtime.mkdir(mode=0o700)
    marker = "SYNTHETIC_REQUIRED_WHOLE_ACCEPTANCE_EVIDENCE"
    public_marker = "SYNTHETIC_PUBLIC_EVIDENCE_VISIBLE"
    qa_marker = "SYNTHETIC_QA_PATCH_BYTES"
    # These are the exact existing Cycle QA handoff paths (not real Git/history).
    (scratch / "qa-evidence.json").write_text('{"synthetic":true}')
    (scratch / "qa-changes.diff").write_text(qa_marker)
    candidate = {"id": "synthetic-task", "kind": "task", "status": "review",
                 "fingerprint": "synthetic-fingerprint", "board_id": next(iter(r.PRIVATE_BOARDS)),
                 "evidence": {"whole": marker + "x" * (review.MAX_EVIDENCE_CHARS + 1),
                              "evidence_files": {"public-proof.txt": public_marker}}}
    requests, posts = [], []
    command = "cat /tmp/public-proof.txt; cat " + shlex.quote(str(scratch / "qa-changes.diff"))
    command += "; cat " + shlex.quote(str(scratch / "acceptance-evidence.json"))

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            first = len(requests) == 1
            delta = {"role": "assistant"}
            if first:
                delta["tool_calls"] = [{"index": 0, "id": "synthetic-evidence-read", "type": "function",
                    "function": {"name": "bash", "arguments": json.dumps({"command": command})}}]
            else:
                delta["content"] = json.dumps({"action": "revise", "impact": "routine",
                    "evidence": "Synthetic evidence could not be read", "feedback": "Restore whole proof"})
            chunks = [
                {"id": "synthetic", "object": "chat.completion.chunk", "created": 1, "model": "fixture",
                 "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                {"id": "synthetic", "object": "chat.completion.chunk", "created": 1, "model": "fixture",
                 "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if first else "stop"}],
                 "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}}]
            data = ("".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
                    + "data: [DONE]\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        (runtime / "models.json").write_text(json.dumps({"providers": {"fixture": {
            "baseUrl": f"http://127.0.0.1:{server.server_port}/v1", "api": "openai-completions",
            "apiKey": "synthetic-not-real", "models": [{"id": "fixture", "name": "fixture",
                "reasoning": False, "input": ["text"], "contextWindow": 131072, "maxTokens": 16384,
                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}}]}}}))
        (runtime / "auth.json").write_text("{}")
        (runtime / "settings.json").write_text('{"compaction":{"enabled":true,"reserveTokens":16384,"keepRecentTokens":12000}}')
        monkeypatch.setattr(r, "BOARD", "")  # only authenticated candidate.board_id selects profile
        monkeypatch.setattr(r, "STOP", threading.Event())
        monkeypatch.setattr(review, "STOP", threading.Event())
        monkeypatch.setenv("RUNNER_FIXTURES", "1")
        monkeypatch.setenv("FIXTURE_LOG", str(tmp_path / "fixture.log"))
        monkeypatch.setattr(r, "harness_cmd", lambda *a: (["pi", "--provider", "fixture", "--model", "fixture"],
                                                        {"PI_CODING_AGENT_DIR": str(runtime)}))
        original_mkdtemp = review.tempfile.mkdtemp
        monkeypatch.setattr(review.tempfile, "mkdtemp", lambda *a, **kw: str(scratch)
                            if kw.get("prefix") == "mc-review-attempt." else original_mkdtemp(*a, **kw))
        monkeypatch.setattr(review, "_review_workspace", lambda *a: (source, []))
        monkeypatch.setattr(review, "REVIEW_TIMEOUT", 25)
        def api(method, path, body=None):
            posts.append({"method": method, "path": path, "body": body})
            if path == "/api/v1/review/automation":
                return {"enabled": True, "models": [{"eligible": True, "provider": "omniroute-codex", "model": "gpt-6.1-sol"}]}
            if path.endswith("/review-lease"):
                return {"lease_id": "synthetic-lease"}
            return {"ok": True, "action": "revise"}
        monkeypatch.setattr(r, "api", api)
        # No production publication lock is acquired.
        import contextlib
        monkeypatch.setattr(r, "global_lock", lambda *a: contextlib.nullcontext())
        outcome = review.review_candidate(candidate, [{"provider": "omniroute-codex", "model": "gpt-6.1-sol"}], set(), {})
        assert outcome == "revise"
        assert len(requests) == 2
        assert marker not in json.dumps(requests[0])  # caller really used the file-backed branch
        assert public_marker in json.dumps(requests[1])  # positive owned-/tmp projection control
        tools = [m for m in requests[1]["messages"] if m.get("role") == "tool"]
        evidence = {"outcome": outcome, "tool_messages": tools, "requests": len(requests),
                    "intercepted_api_paths": [p["path"] for p in posts],
                    "private_bundle_marker_present": marker in json.dumps(requests[1]),
                    "qa_patch_marker_present": qa_marker in json.dumps(requests[1]),
                    "public_marker_present": public_marker in json.dumps(requests[1])}
        output = tmp_path / "independent-evidence-projection.json"
        output.write_text(json.dumps(evidence, indent=2))
        output.chmod(0o600)
        # Expected contract: complete authorized proof is readable through the sole offered tool.
        assert marker in json.dumps(requests[1]), "actual acceptance caller's required whole bundle is masked from guarded bash"
        assert qa_marker in json.dumps(requests[1]), "Cycle QA handoff patch is masked too"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
