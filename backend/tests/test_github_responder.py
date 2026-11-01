import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/omc-github-responder.py"


def load(monkeypatch, tmp_path):
    monkeypatch.setenv("OMC_GH_STATE", str(tmp_path / "state.json"))
    monkeypatch.setenv("OMC_GH_LOG", str(tmp_path / "log"))
    monkeypatch.setenv("OMC_GH_REPO", "example/repo")
    monkeypatch.setenv("OMC_GH_BOARD", "b")
    spec = importlib.util.spec_from_file_location("gh_responder", SCRIPT)
    r = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(r)
    return r


def test_first_run_answers_nothing_then_replies_once_and_never_to_itself(monkeypatch, tmp_path):
    r = load(monkeypatch, tmp_path)
    posted = []
    comments = [
        {"id": 1, "body": "Why is the sky blue?", "user": {"login": "me", "type": "User"}, "created_at": "2026-10-22T05:00:00Z",
         "issue_url": "https://api.github.com/repos/o/r/issues/7", "html_url": "u1"},
        {"id": 2, "body": f"old answer {r.MARKER}", "user": {"login": "me", "type": "User"}, "created_at": "2026-10-22T05:01:00Z",
         "issue_url": "https://api.github.com/repos/o/r/issues/7", "html_url": "u2"},
    ]

    def gh(path, *args, data=None):
        if path.endswith("/comments") and "-X" in args:
            posted.append(json.loads(data)["body"])
            return {}
        if "issues/comments?" in path:
            return comments
        if "pulls/comments?" in path:
            return []
        if path.endswith("/issues/7"):
            return {"title": "Question", "body": ""}
        return []

    monkeypatch.setattr(r, "gh", gh)
    monkeypatch.setattr(r, "ask", lambda messages: {"reply": "Rayleigh scattering.", "create_task": False})
    monkeypatch.setattr(r.runpy, "run_path", lambda path: {"request": None})
    monkeypatch.setattr(r.time, "sleep", lambda s: None)
    r.main()
    assert posted == []  # first run only records the start time
    r.main()
    assert len(posted) == 1 and posted[0].startswith("Rayleigh scattering.") and r.MARKER in posted[0]
    r.main()
    assert len(posted) == 1  # already seen; marked replies are never answered


def test_replies_are_written_under_ponytail(monkeypatch, tmp_path):
    import io
    import sys

    r = load(monkeypatch, tmp_path)
    sys.path.insert(0, str(SCRIPT.parent))
    import mc_skills

    monkeypatch.setattr(mc_skills, "ponytail", lambda: ("ultra", "PONYTAIL"))
    sent = []
    monkeypatch.setattr(r, "open", lambda path: io.StringIO(json.dumps(
        {"providers": {"omniroute-api": {"baseUrl": "http://x/", "apiKey": "k"}}})), raising=False)

    def urlopen(req, timeout):
        sent.append(json.loads(req.data))
        return io.StringIO(json.dumps({"choices": [{"message": {"content": '{"reply": "ok"}'}}]}))

    monkeypatch.setattr(r.urllib.request, "urlopen", urlopen)
    assert r.ask([{"role": "system", "content": "answer"}, {"role": "user", "content": "q"}]) == {"reply": "ok"}
    assert sent[0]["messages"][0]["content"] == "PONYTAIL\n\n---\n\nanswer"
