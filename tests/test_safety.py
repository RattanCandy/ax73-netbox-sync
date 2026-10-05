"""Non-destructive contract tests for the reconciler. No Docker or router required."""
import ast
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SYNC = ROOT / "ax73_sync.py"
COLLECTOR = ROOT / "ax73_collect_clients.py"


def load_sync():
    spec = importlib.util.spec_from_file_location("ax73_sync_under_test", SYNC)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SafetyTests(unittest.TestCase):
    def test_python_syntax_and_embedded_django_script(self):
        for file in (SYNC, COLLECTOR):
            ast.parse(file.read_text(), filename=str(file))
        module = load_sync()
        ast.parse(module.DJANGO_SCRIPT, filename="embedded_netbox_django.py")

    def test_no_private_lab_identifiers(self):
        # Real credentials, per-device exports, and production IPs belong only in .env/data/.
        for file in (SYNC, COLLECTOR, ROOT / "ax73_cycle.sh"):
            text = file.read_text()
            for private in ("10.88.77.", "rocinante.my", "/opt/docker/tplink-dhcp"):
                self.assertNotIn(private, text, file.name)

    def invoke(self, apply=False, configured=True):
        module = load_sync()
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "clients.json"
            source.write_text(json.dumps({
                "collected_at": datetime.now(timezone.utc).isoformat(),
                "clients": [{"ip": "192.0.2.21", "mac": "02:00:00:00:00:01"}],
            }))
            env = {
                "AX73_SUBNET": "192.0.2.0/26",
                "AX73_DHCP_START": "192.0.2.21",
                "AX73_DHCP_END": "192.0.2.60",
                "AX73_GRACE_MINUTES": "60",
            } if configured else {}
            argv = ["ax73_sync.py"] + (["--apply"] if apply else [])
            with patch.object(module, "SOURCE", source), \
                 patch.dict(os.environ, env, clear=True), \
                 patch.object(sys, "argv", argv), \
                 patch.object(module.subprocess, "run") as runner:
                if not configured:
                    with self.assertRaises(SystemExit) as caught:
                        module.run()
                    self.assertEqual(caught.exception.code, 2)
                else:
                    module.run()
                return [call.args[0] for call in runner.call_args_list]

    def test_dry_run_by_default_and_environment_forwarded(self):
        commands = self.invoke()
        django = next(cmd for cmd in commands if "manage.py" in cmd)
        self.assertIn("AX73_DRY_RUN=1", django)
        self.assertIn("AX73_SUBNET=192.0.2.0/26", django)
        self.assertIn("AX73_DHCP_START=192.0.2.21", django)
        self.assertIn("AX73_DHCP_END=192.0.2.60", django)

    def test_apply_requires_explicit_flag(self):
        commands = self.invoke(apply=True)
        django = next(cmd for cmd in commands if "manage.py" in cmd)
        self.assertIn("AX73_DRY_RUN=0", django)

    def test_missing_network_configuration_rejected_before_docker(self):
        self.assertEqual(self.invoke(configured=False), [])


if __name__ == "__main__":
    unittest.main()
