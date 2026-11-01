#!/usr/bin/env python3
"""Interactive component choices; OpenClaw is core, extra coding harnesses are optional."""

import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

INSTALL = {
    "openclaw": 'npm install -g --prefix "$HOME/.local" openclaw@2026.9.4',
    "pi": "curl -fsSL https://pi.dev/install.sh | sh",
    "claude": "curl -fsSL https://claude.ai/install.sh | bash",
}


def locate(name):
    candidates = [Path.home() / ".local/bin" / name]
    if name == "pi":
        candidates.insert(0, Path.home() / ".pi/agent/bin/pi")
    return next((str(p) for p in candidates if os.access(p, os.X_OK)), shutil.which(name))


def write_json(path, value):
    if path.is_symlink() or not path.parent.resolve().is_relative_to(Path.home().resolve()):
        raise ValueError("Configuration must stay inside the instance home, without file symlinks")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as f:
        os.fchmod(f.fileno(), 0o600)
        json.dump(value, f, indent=2)
        f.write("\n")
        temp = f.name
    os.replace(temp, path)


def save_config(path, value):
    if path.is_symlink() or not path.parent.resolve().is_relative_to(Path.home().resolve()):
        raise ValueError("Configuration must stay inside the instance home")
    if path.exists():
        if json.loads(path.read_text()) == value:
            return True
        if input(f"Replace {path}, preserving a protected backup first? [y/N] ").lower() != "y":
            print("Kept the existing configuration; configuration remains pending.")
            return False
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=path.name + ".backup-", dir=path.parent, delete=False
        ) as backup:
            os.fchmod(backup.fileno(), 0o600)
            backup.write(path.read_bytes())
    write_json(path, value)
    return True


def configure_pi(repo):
    executable = locate("pi")
    if not executable or not Path(executable).resolve().is_relative_to(Path.home().resolve()):
        print("Install Pi for this instance user first; another user's installation is not used.")
        return False
    env = dict(
        line.split("=", 1)
        for line in (repo / "data/.env").read_text().splitlines()
        if "=" in line and not line.startswith("#")
    )
    base = "http://localhost:" + env["OMNIROUTE_PORT"] + "/v1"
    key = env.get("OMNIROUTE_RUNTIME_KEY", "")
    if not key:
        import getpass

        key = getpass.getpass("This instance's OmniRoute API key (hidden): ")
    if not key:
        raise ValueError("Configure this instance's OmniRoute API key in setup step 3 first")
    req = urllib.request.Request(base + "/models", headers={"Authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=30) as response:
        ids = [m["id"] for m in json.load(response).get("data", [])]
    if not ids:
        raise ValueError("This instance's OmniRoute lists no models")
    print("Available instance models: " + ", ".join(ids))
    default = "omniroute-auto" if "omniroute-auto" in ids else ids[0]
    model = input(f"Model/combo [{default}]: ").strip() or default
    if model not in ids:
        raise ValueError("Choose a model/combo listed by this instance")
    context = int(input("Model context window (required; verify against your provider): ").strip())
    output = int(input("Maximum output tokens (required; verify against your provider): ").strip())
    if not 0 < output <= context:
        raise ValueError("Maximum output must be positive and no larger than the context window")
    agent = Path.home() / ".pi/agent"
    if not agent.resolve().is_relative_to(Path.home().resolve()):
        raise ValueError("Pi configuration must stay inside the instance home")
    models_path, settings_path = agent / "models.json", agent / "settings.json"
    for path in (models_path, settings_path):
        if path.is_symlink():
            raise ValueError("Refusing a symlinked instance configuration file")
    models = json.loads(models_path.read_text()) if models_path.exists() else {"providers": {}}
    provider = json.loads((repo / "harness/pi/models.json.example").read_text())["providers"][
        "omniroute-api"
    ]
    provider.update(baseUrl=base, apiKey=key)
    provider["models"][0].update(
        id=model, name=model + " via OmniRoute", contextWindow=context, maxTokens=output
    )
    models.setdefault("providers", {})["omniroute-api"] = provider
    if not save_config(models_path, models):
        return False
    settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    settings.update(defaultProvider="omniroute-api", defaultModel=model, defaultThinkingLevel="off")
    if not save_config(settings_path, settings):
        return False
    return check_pi(repo, "omniroute-api", model)


def check_pi(repo, provider, model):
    executable = locate("pi")
    if not executable or not Path(executable).resolve().is_relative_to(Path.home().resolve()):
        return False
    canonical = Path.home() / ".pi/agent/bin/pi"
    if not canonical.exists():
        if canonical.is_symlink():
            raise ValueError("Repair the existing broken Pi executable link explicitly")
        canonical.parent.mkdir(parents=True, exist_ok=True)
        canonical.symlink_to(executable)
    # An in-memory, tool-free request verifies the actual CLI/config without reading project/session data.
    result = subprocess.run(
        [
            str(canonical),
            "-p",
            "--provider",
            provider,
            "--model",
            model,
            "--no-session",
            "--no-tools",
            "--no-extensions",
            "--no-mcp",
            "--no-skills",
            "--no-context-files",
            "--no-prompt-templates",
            "--no-themes",
            "Reply with the word OK.",
        ],
        cwd=repo,
        capture_output=True,
        timeout=180,
    )
    if result.returncode:
        print(
            "Pi model check failed; inspect the instance settings locally. No response/secret was logged."
        )
        return False
    print(
        "Pi CLI model request passed through this instance's OmniRoute; protected models/settings saved."
    )
    return True


def fleet_ready(repo):
    path = repo / "data/state/components.json"
    states = json.loads(path.read_text()) if path.exists() else {}
    agent = Path.home() / ".pi/agent"
    pi = (
        states.get("pi") == "configured"
        and os.access(agent / "bin/pi", os.X_OK)
        and all((agent / n).is_file() for n in ("models.json", "settings.json"))
    )
    return bool(pi or (states.get("claude") == "configured" and locate("claude")))


def components(repo):
    if not sys.stdin.isatty():
        print(
            "OpenClaw is core; Pi and Claude Code are optional. Run deploy/setup.sh --components in a terminal for install/configure/skip choices."
        )
        return
    state_path = repo / "data/state/components.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    for name in INSTALL:
        print(
            f"\n{name}: "
            + (
                "CORE gateway for boards"
                if name == "openclaw"
                else "OPTIONAL coding harness; base OpenClaw/PEV works without it"
            )
        )
        print(
            "Executable: "
            + (locate(name) or "not installed")
            + "; previous choice: "
            + state.get(name, "not chosen")
        )
        choice = (
            input(
                "[u]se existing / [i]nstall / [c]onfigure / "
                + ("" if name == "openclaw" else "[s]kip / ")
                + "[l]ater: "
            )
            .strip()
            .lower()
        )
        if choice in ("s", "l", ""):
            if choice == "s" and name != "openclaw":
                state[name] = "skipped"
            else:
                state.setdefault(name, "deferred")
        elif choice in ("u", "i", "c"):
            if choice == "i":
                print("Installing as this instance user only: " + INSTALL[name])
                result = subprocess.run(["bash", "-o", "pipefail", "-c", INSTALL[name]])
                if result.returncode:
                    state[name] = "install-failed"
                    write_json(state_path, state)
                    print(
                        "Installer failed; preserved existing configuration. Re-run --components to retry."
                    )
                    continue
            executable = locate(name)
            if not executable:
                print(
                    "Not installed; choose install or return later. Base setup needs only OpenClaw."
                )
                state[name] = "not-installed"
            else:
                result = subprocess.run([executable, "--version"])
                state[name] = "installed" if result.returncode == 0 else "check-failed"
                if not result.returncode and name == "pi":
                    state[name] = "configuration-pending"
                    write_json(state_path, state)
                    if choice in ("i", "c"):
                        ready = configure_pi(repo)
                    else:
                        settings_path = Path.home() / ".pi/agent/settings.json"
                        settings = (
                            json.loads(settings_path.read_text()) if settings_path.exists() else {}
                        )
                        ready = bool(
                            settings.get("defaultProvider") and settings.get("defaultModel")
                        ) and check_pi(repo, settings["defaultProvider"], settings["defaultModel"])
                    state[name] = "configured" if ready else "configuration-pending"
                elif choice in ("i", "c"):
                    print(
                        "Follow "
                        + str(repo / "harness/HARNESS-SETUP.md")
                        + " for "
                        + name
                        + " configuration; use only this instance's OmniRoute and credentials."
                    )
                    if name == "claude":
                        print(
                            "Configure/login in your own Claude CLI; no hooks or credentials are copied automatically."
                        )
                        subprocess.run([executable], cwd=repo)
                        if (
                            input(
                                "Completed configuration and verified your instance model request? [y/N] "
                            ).lower()
                            == "y"
                        ):
                            state[name] = "configured"
        else:
            print("No valid choice selected; leaving this component for later.")
            state[name] = "deferred"
        write_json(state_path, state)
    if not fleet_ready(repo):
        print(
            "Optional coding fleet is unavailable/disarmed until a coding harness is configured. OpenClaw and PEV remain available; run setup step 7 to apply fleet availability."
        )


if __name__ == "__main__":
    os.umask(0o077)
    os.environ["PATH"] = (
        str(Path.home() / ".local/bin")
        + ":"
        + str(Path.home() / ".local/node/bin")
        + ":"
        + str(Path.home() / ".pi/agent/bin")
        + ":"
        + os.environ.get("PATH", "")
    )
    repo = Path(__file__).resolve().parents[1]
    if sys.argv[1:] == ["--fleet-ready"]:
        sys.exit(0 if fleet_ready(repo) else 1)
    if os.getuid() == 0 or not pwd.getpwuid(os.getuid()).pw_name.startswith("omc-"):
        sys.exit("Run component setup as the unprivileged instance user omc-<name>.")
    try:
        components(repo)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        sys.exit(
            "Component setup did not finish; configuration was preserved where not explicitly replaced. Check this instance locally and re-run --components. No secret or response body was logged."
        )
