#!/usr/bin/env python3
"""Wizard tests replace only terminal, network and external-process boundaries."""

import importlib.util
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("components", ROOT / "deploy/components.py")
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)


class ComponentsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.repo = self.home / "omc"
        (self.repo / "data/state").mkdir(parents=True)
        (self.repo / "data/.env").write_text(
            "OMNIROUTE_PORT=13004\nOMNIROUTE_RUNTIME_KEY=synthetic-test-credential\n"
        )
        shutil.copytree(ROOT / "harness/pi", self.repo / "harness/pi")
        self.env = patch.dict(os.environ, {"HOME": str(self.home)})
        self.env.start()
        self.addCleanup(self.env.stop)
        tty = patch.object(c.sys.stdin, "isatty", return_value=True)
        tty.start()
        self.addCleanup(tty.stop)
        self.executable = self.home / ".local/bin/pi"
        self.executable.parent.mkdir(parents=True)
        self.executable.write_text("#!/bin/sh\nexit 0\n")
        self.executable.chmod(0o755)

    def test_skip_does_not_install_or_configure_optional_harnesses(self):
        with patch.object(c, "locate", return_value=str(self.executable)), patch(
            "builtins.input", side_effect=["u", "s", "s"]
        ), patch.object(c.subprocess, "run") as run:
            run.return_value.returncode = 0
            c.components(self.repo)
        self.assertFalse((self.home / ".pi/agent/models.json").exists())
        self.assertEqual(
            json.loads((self.repo / "data/state/components.json").read_text())["pi"], "skipped"
        )
        self.assertFalse(any("curl" in str(call.args) for call in run.call_args_list))
        self.assertFalse(c.fleet_ready(self.repo))

    def test_install_requires_explicit_choice_and_failed_installer_is_not_ready(self):
        with patch.object(c, "locate", return_value=None), patch(
            "builtins.input", side_effect=["l", "i", "s"]
        ), patch.object(c.subprocess, "run") as run:
            run.return_value.returncode = 1
            c.components(self.repo)
        self.assertEqual(run.call_count, 1)
        self.assertIn("curl -fsSL https://pi.dev/install.sh | sh", run.call_args.args[0][-1])
        self.assertIn("pipefail", run.call_args.args[0])
        self.assertNotEqual(
            json.loads((self.repo / "data/state/components.json").read_text()).get("pi"),
            "configured",
        )

    def test_selected_pi_configuration_preserves_custom_settings_and_checks_real_cli(self):
        agent = self.home / ".pi/agent"
        agent.mkdir(parents=True)
        settings = agent / "settings.json"
        settings.write_text(json.dumps({"theme": "custom", "defaultModel": "old"}))
        with patch.object(c, "locate", return_value=str(self.executable)), patch(
            "builtins.input", side_effect=["", "65536", "16384", "y"]
        ), patch.object(
            c.urllib.request,
            "urlopen",
            return_value=io.BytesIO(b'{"data":[{"id":"omniroute-auto"}]}'),
        ), patch.object(
            c.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)
        ) as run:
            self.assertTrue(c.configure_pi(self.repo))
        model = json.loads((agent / "models.json").read_text())["providers"]["omniroute-api"]
        self.assertEqual(model["baseUrl"], "http://localhost:13004/v1")
        self.assertEqual(model["apiKey"], "synthetic-test-credential")
        self.assertEqual(json.loads(settings.read_text())["theme"], "custom")
        self.assertEqual(json.loads(settings.read_text())["defaultModel"], "omniroute-auto")
        self.assertEqual(settings.stat().st_mode & 0o777, 0o600)
        backups = list(agent.glob("settings.json.backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
        self.assertTrue((agent / "bin/pi").samefile(self.executable))
        args = run.call_args.args[0]
        self.assertIn("--no-session", args)
        self.assertIn("--no-tools", args)
        self.assertNotIn("synthetic-test-credential", str(args))

    def test_declining_replacement_keeps_existing_config_bytes(self):
        agent = self.home / ".pi/agent"
        agent.mkdir(parents=True)
        models = agent / "models.json"
        models.write_text('{"providers":{"custom":{"apiKey":"keep"}}}')
        before = models.read_bytes()
        with patch.object(c, "locate", return_value=str(self.executable)), patch(
            "builtins.input", side_effect=["", "65536", "16384", "n"]
        ), patch.object(
            c.urllib.request,
            "urlopen",
            return_value=io.BytesIO(b'{"data":[{"id":"omniroute-auto"}]}'),
        ), patch.object(
            c.subprocess, "run"
        ) as run:
            self.assertFalse(c.configure_pi(self.repo))
        self.assertEqual(models.read_bytes(), before)
        self.assertFalse((agent / "settings.json").exists())
        run.assert_not_called()

    def test_explicit_successful_install_configures_pi_and_records_readiness(self):
        self.executable.unlink()
        claw = self.executable.with_name("openclaw")
        claw.write_text("#!/bin/sh\n")
        claw.chmod(0o755)

        def process(args, **kwargs):
            if "curl -fsSL https://pi.dev/install.sh | sh" in args:
                self.executable.write_text("#!/bin/sh\nexit 0\n")
                self.executable.chmod(0o755)
            return subprocess.CompletedProcess(args, 0)

        with patch.object(c.shutil, "which", return_value=None), patch(
            "builtins.input", side_effect=["u", "i", "", "65536", "16384", "s"]
        ), patch.object(
            c.urllib.request,
            "urlopen",
            return_value=io.BytesIO(b'{"data":[{"id":"omniroute-auto"}]}'),
        ), patch.object(
            c.subprocess, "run", side_effect=process
        ):
            c.components(self.repo)
        self.assertEqual(
            json.loads((self.repo / "data/state/components.json").read_text())["pi"], "configured"
        )
        self.assertTrue(c.fleet_ready(self.repo))

    def test_pending_configuration_does_not_arm_fleet_even_when_files_exist(self):
        agent = self.home / ".pi/agent"
        (agent / "bin").mkdir(parents=True)
        (agent / "bin/pi").symlink_to(self.executable)
        for name in ("models.json", "settings.json"):
            (agent / name).write_text("{}")
        (self.repo / "data/state/components.json").write_text('{"pi":"configuration-pending"}')
        self.assertFalse(c.fleet_ready(self.repo))

    def test_failed_reconfiguration_cannot_reuse_old_ready_state(self):
        state = self.repo / "data/state/components.json"
        state.write_text('{"pi":"configured"}')
        with patch.object(c, "locate", return_value=str(self.executable)), patch(
            "builtins.input", side_effect=["u", "c"]
        ), patch.object(
            c.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)
        ), patch.object(
            c.urllib.request, "urlopen", side_effect=OSError("offline")
        ):
            with self.assertRaises(OSError):
                c.components(self.repo)
        self.assertEqual(json.loads(state.read_text())["pi"], "configuration-pending")

    def test_missing_model_limits_are_not_silently_invented(self):
        with patch.object(c, "locate", return_value=str(self.executable)), patch(
            "builtins.input", side_effect=["", "", ""]
        ), patch.object(
            c.urllib.request,
            "urlopen",
            return_value=io.BytesIO(b'{"data":[{"id":"omniroute-auto"}]}'),
        ), patch.object(
            c.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)
        ):
            with self.assertRaises(ValueError):
                c.configure_pi(self.repo)
        self.assertFalse((self.home / ".pi/agent/models.json").exists())

    def test_claude_install_continues_to_configuration_guidance(self):
        with patch.object(c, "locate", return_value=str(self.executable)), patch(
            "builtins.input", side_effect=["u", "s", "i", "y"]
        ), patch.object(
            c.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)
        ) as run:
            c.components(self.repo)
        self.assertTrue(any(call.args[0] == [str(self.executable)] for call in run.call_args_list))
        self.assertEqual(
            json.loads((self.repo / "data/state/components.json").read_text())["claude"],
            "configured",
        )

    def test_noninteractive_components_never_runs_an_installer(self):
        with patch.object(c.sys.stdin, "isatty", return_value=False), patch.object(
            c.subprocess, "run"
        ) as run:
            c.components(self.repo)
        run.assert_not_called()

    def test_generated_omniroute_env_requires_api_keys(self):
        (self.repo / "data/.env").unlink()
        fake_ss = self.home / "ss"
        fake_ss.write_text("#!/bin/sh\nexit 0\n")
        fake_ss.chmod(0o755)
        shutil.copytree(ROOT / "deploy", self.repo / "deploy")
        result = subprocess.run(
            [str(self.repo / "deploy/install.sh"), "--render-env-only"],
            env={**os.environ, "OMC_INSTANCE": "demo", "OMC_SS": str(fake_ss)},
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0)
        values = dict(
            l.split("=", 1)
            for l in (self.repo / "data/.env.omniroute").read_text().splitlines()
            if "=" in l and not l.startswith("#")
        )
        self.assertEqual(values.get("REQUIRE_API_KEY"), "true")


if __name__ == "__main__":
    unittest.main()
