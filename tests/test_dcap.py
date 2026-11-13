import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

import yaml

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
# These tests exercise the framework against canned data, in this process and its children.
os.environ["DCAP_SIMULATE"] = "1"
# A server plugged in on this machine must not change what these tests see.
os.environ["DCAP_PLUGGED"] = os.path.join(os.path.dirname(os.path.abspath(__file__)), "no-plugged-servers")

from dcap import Dcap  # noqa: E402
from dcap.cli import main  # noqa: E402
from dcap.listener import Listener, claude_code_hook  # noqa: E402
from dcap.errors import RegistryError, StateError  # noqa: E402
from dcap.facade import facade_name  # noqa: E402
from dcap.registry import Registries  # noqa: E402
from dcap.runtime.policy import covers  # noqa: E402
from dcap.runtime.remote import RemoteError, call_remote, list_tools  # noqa: E402
from dcap.scaffold import draft, render  # noqa: E402
from dcap.workspace import Workspace  # noqa: E402

FAKE_MCP = Path(__file__).parent / "fixtures" / "fake_mcp.py"
# These tests act as the operator. Child processes do too, through the wrapper around the command line.
_AS_OPERATOR = mock.patch("dcap.operator.present", return_value=True)


def setUpModule():
    _AS_OPERATOR.start()


def tearDownModule():
    _AS_OPERATOR.stop()
OPERATOR_CLI = [sys.executable, "-c", "import sys; from dcap import operator; operator.present = lambda: True; "
                "from dcap.cli import main; sys.exit(main(sys.argv[1:]))"]
GRANTS = ["web.fetch"]


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def dcap(self, grants=GRANTS, extra=()):
        return Dcap(self.root, extra_definitions=extra, grants=grants)

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def run_py(self, code, policy=None, switches_on=True):
        """Run code in the workspace. By default every switch is on for that run, then put back."""
        env = {**os.environ, "DCAP_POLICY": str(policy or self.root / "no-policy.json")}
        saved = {}
        if switches_on:
            for path in self.root.rglob("*.py"):
                text = path.read_bytes()
                flipped = re.sub(rb'(\.environ\["[A-Z0-9_]+"\] = )"false"', rb'\1"true"', text)
                if flipped != text:
                    saved[path] = text
                    path.write_bytes(flipped)
        try:
            return subprocess.run([sys.executable, "-c", code], cwd=self.root, env=env, capture_output=True, text=True)
        finally:
            for path, text in saved.items():
                path.write_bytes(text)


class ResolutionModes(Base):
    def test_spec_table(self):
        d = self.dcap()
        params = {"target": "127.0.0.1"}
        expected = {
            "Show me what's available on my website": ("direct", None),
            "Help me investigate my web-site": ("direct", None),
            "Check site links on this host": ("goal", "WEB.SITE_LINKS"),
            "Check links and produce a sitemap": ("goal", "WEB.SITEMAP"),
            "Audit this site and generate a site report": ("goal", "WEB.SITE_AUDIT"),
        }
        for text, (mode, goal) in expected.items():
            with self.subTest(text):
                out = d.request(text, params, execute=False)
                self.assertEqual(out["mode"], mode)
                self.assertEqual(out.get("goal_id"), goal)

    def test_extra_intent_never_orchestrates(self):
        out = self.dcap().request("Check site links on this host and delete them")
        self.assertEqual(out["mode"], "direct")
        self.assertNotIn("result", out)

    def test_missing_parameter_falls_back_with_suggestion(self):
        out = self.dcap().request("Audit this site and generate a site report")
        self.assertEqual(out["mode"], "direct")
        self.assertEqual(out["suggestion"]["goals"][0]["goal_id"], "WEB.SITE_AUDIT")
        self.assertEqual(out["suggestion"]["goals"][0]["missing"], ["target"])

    def test_unknown_request_is_unresolved(self):
        self.assertEqual(self.dcap().request("Bake me a sourdough loaf")["mode"], "unresolved")

    def test_address_is_extracted(self):
        out = self.dcap().request("Check site links on 192.0.2.7")
        self.assertEqual({s["address"] for s in out["result"]}, {"192.0.2.7"})


class Orchestration(Base):
    def test_chain_returns_consolidated_result(self):
        out = self.dcap().request("Check site links on this host")
        self.assertEqual(out["status"], "complete")
        self.assertEqual({s["status"] for s in out["result"]}, {"ok", "redirect", "broken"})
        self.assertEqual(len(out["steps"]), 3)

    def test_composed_goal_deduplicates_shared_stages(self):
        d = self.dcap()
        plan = d.planner.for_goal("WEB.SITE_AUDIT")
        self.assertEqual(sorted(s.core for s in plan.steps), sorted([
            "web.page_discovery", "web.link_discovery", "web.link_check",
            "web.broken_links", "reporting.site_report",
        ]))
        out = d.request("Audit this site and generate a site report", {"target": "192.0.2.1"})
        self.assertEqual(out["status"], "complete")
        self.assertIn("Issues: 1", out["result"]["markdown"])

    def test_no_execution_without_grant(self):
        out = self.dcap(grants=[]).request("Check site links on this host")
        self.assertEqual(out["status"], "authorization_required")
        self.assertEqual(out["permissions"], ["web.fetch"])
        self.assertNotIn("result", out)

    def test_invalid_input_is_rejected(self):
        out = self.dcap().request("Check site links", {"target": 42})
        self.assertEqual(out["status"], "failed")
        self.assertEqual(out["errors"][0]["code"], "INVALID_INPUT")

    def test_failure_skips_dependants_and_keeps_partial_results(self):
        d = self.dcap()
        with mock.patch("dcap.runtime.placeholder.check_links", side_effect=RuntimeError("secret-detail")):
            out = d.request("Check site links on this host")
        self.assertEqual(out["status"], "partial")
        self.assertIn("found_links", out["completed"])
        self.assertEqual(out["errors"][0]["code"], "EXECUTION_FAILED")
        self.assertNotIn("secret-detail", json.dumps(out))

    def test_bad_provider_output_is_rejected(self):
        d = self.dcap()
        with mock.patch("dcap.runtime.placeholder.discover_pages", return_value="nope"):
            out = d.request("Check site links on this host")
        self.assertEqual(out["errors"][0]["code"], "INVALID_OUTPUT")
        self.assertEqual(out["status"], "failed")


class DirectMode(Base):
    SOURCE = '"""App."""\nimport os\n\nVALUE = os.sep\n'

    def switch_on(self, rel, name):
        path = self.root / rel
        text = path.read_text()
        self.assertIn(f'environ["{name}"] = "false"', text)
        path.write_text(text.replace(f'environ["{name}"] = "false"', f'environ["{name}"] = "true"'))

    def test_vague_request_pastes_plain_switches_all_off(self):
        path = self.write("app.py", self.SOURCE)
        out = self.dcap().request("Help me investigate my website", execute=False)
        self.assertEqual(out["integrations"][0]["status"], "integrated")
        text = path.read_text()
        self.assertTrue(text.startswith('"""App."""\nimport os\n# dcap:begin'))
        self.assertIn("from dcap_capabilities.web_page_discovery import run as discover_pages\n", text)
        self.assertIn('os.environ["DISCOVER_PAGES"] = "false"  # discover_pages(target)\n', text)
        self.assertNotIn('"true"', text)
        self.assertIn("VALUE = os.sep", text)

    def test_capability_stays_off_until_the_ai_turns_it_on(self):
        self.write("app.py", self.SOURCE)
        self.dcap().request("Help me investigate my website", execute=False)
        policy = self.write("p.json", json.dumps({"grants": GRANTS}))
        call = "import app; print(app.discover_pages('192.0.2.1'))"
        self.assertIn('DISCOVER_PAGES is off', self.run_py(call, policy, switches_on=False).stderr)
        self.switch_on("app.py", "DISCOVER_PAGES")
        run = self.run_py(call, policy, switches_on=False)
        self.assertIn("192.0.2.1", run.stdout, run.stderr)
        # Only the switch that was turned on works.
        off = self.run_py("import app; app.read_site_metadata('x.test')", policy, switches_on=False)
        self.assertIn("READ_SITE_METADATA is off", off.stderr)

    def test_turning_on_a_switch_is_not_treated_as_tampering(self):
        path = self.write("app.py", self.SOURCE)
        d = self.dcap()
        integration = d.request("Help me investigate my website", execute=False)["integrations"][0]["id"]
        self.switch_on("app.py", "DISCOVER_PAGES")
        self.assertEqual(d.status()["integrations"][integration]["problems"], [])
        self.assertEqual(d.release(integration)["status"], "released")
        self.assertEqual(path.read_text(), self.SOURCE)

    def test_repeat_request_changes_nothing(self):
        path = self.write("app.py", self.SOURCE)
        d = self.dcap()
        first = d.request("Help me investigate my website", execute=False)["integrations"][0]
        self.switch_on("app.py", "DISCOVER_PAGES")
        text = path.read_text()
        second = d.request("Show me what's available on my website", execute=False)["integrations"][0]
        self.assertEqual(second["status"], "existing")
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(path.read_text(), text)

    def test_another_os_in_the_file_is_not_rebound(self):
        path = self.write("app.py", "os = 'mine'\n")
        self.dcap().request("Help me investigate my website", execute=False)
        text = path.read_text()
        self.assertIn("import os as os_2", text)
        self.assertIn('os_2.environ["DISCOVER_PAGES"]', text)
        self.assertIn("os = 'mine'", text)

    def test_variable_name_avoids_existing_code(self):
        path = self.write("app.py", "discover_pages = 'mine'\n")
        self.dcap().request("Help me investigate my website")
        self.assertIn("import run as discover_pages_2\n", path.read_text())
        self.assertIn("# discover_pages_2(target)\n", path.read_text())
        self.assertIn("discover_pages = 'mine'", path.read_text())

    def test_block_goes_in_most_recently_changed_non_test_file(self):
        old = self.write("old.py", "A = 1\n")
        os.utime(old, (1, 1))
        new = self.write("new.py", "B = 1\n")
        self.write("test_new.py", "C = 1\n")
        self.dcap().request("Help me investigate my website")
        self.assertIn("dcap:begin", new.read_text())
        self.assertNotIn("dcap:begin", old.read_text())

    def test_empty_project_gets_the_package_only(self):
        out = self.dcap().request("Help me investigate my website")
        self.assertEqual(out["integrations"][0]["status"], "integrated")
        self.assertTrue((self.root / "dcap_capabilities" / "web_page_discovery.py").exists())
        d = self.dcap()
        self.assertEqual(d.release(out["integrations"][0]["id"])["status"], "released")
        self.assertFalse((self.root / "dcap_capabilities").exists())

    def test_unsupported_language_abstains_and_writes_nothing(self):
        self.write("web.ts", "export const x = 1;\n")
        out = self.dcap().request("Help me investigate my website")
        self.assertEqual(out["integrations"][0]["reason"], "no_compatible_bridge")
        self.assertFalse((self.root / "dcap_capabilities").exists())

    def test_exposure_contains_no_schemas_or_provider_names(self):
        text = json.dumps(self.dcap().request("Help me investigate my website"))
        self.assertNotIn("placeholder", text)
        self.assertNotIn("properties", text)

    def test_large_class_exposes_only_relevant_cores(self):
        extra = self.root / "defs"
        cores = {
            f"big.c{i}": {"name": f"Big thing {i}", "contract": 1, "description": "misc",
                          "inputs": {}, "output": "Report"}
            for i in range(9)
        }
        cores["big.widget"] = {"name": "Polish widget", "contract": 1, "description": "misc",
                               "inputs": {}, "output": "Report"}
        (extra / "capabilities").mkdir(parents=True)
        (extra / "capabilities" / "big.yaml").write_text(
            yaml.safe_dump({"class": "big", "keywords": ["bigclass"], "expose_all_below": 5, "cores": cores})
        )
        (extra / "providers").mkdir()
        (extra / "providers" / "big.yaml").write_text(yaml.safe_dump({"providers": [{
            "provider": "big.http", "kind": "http", "url": "http://127.0.0.1:9",
            "implements": {"big.widget": {"contract": 1, "operation": "/polish"}},
        }]}))
        out = self.dcap(extra=[extra]).request("widget bigclass")
        self.assertEqual(list(out["capabilities"]["big"]), ["Polish widget"])


class Providers(Base):
    def mcp_defs(self, command):
        extra = self.root.parent / (self.root.name + "-defs")
        (extra / "providers").mkdir(parents=True, exist_ok=True)
        (extra / "providers" / "remote.yaml").write_text(yaml.safe_dump({"providers": [{
            "provider": "secretvendor.mcp", "kind": "mcp", "priority": 1, "command": command,
            "implements": {"web.link_discovery": {
                "contract": 1, "operation": "link_crawl",
                "input_map": {"pages": "targets"}, "output_key": "open",
            }},
        }]}))
        self.addCleanup(lambda: __import__("shutil").rmtree(extra, ignore_errors=True))
        return extra

    def test_mcp_provider_is_hidden_behind_the_standard_contract(self):
        extra = self.mcp_defs([sys.executable, str(FAKE_MCP)])
        out = self.dcap(extra=[extra]).request("Check site links on this host")
        self.assertEqual(out["status"], "complete")
        self.assertEqual({s["depth"] for s in out["result"]}, {9999})
        self.assertNotIn("secretvendor", json.dumps(out))

    def test_failover_to_next_provider(self):
        extra = self.mcp_defs([sys.executable, "-c", "raise SystemExit(1)"])
        out = self.dcap(extra=[extra]).request("Check site links on this host")
        self.assertEqual(out["status"], "complete")
        self.assertEqual({s["depth"] for s in out["result"]}, {1, 2, 3})

    def test_facade_for_remote_provider_runs_in_workspace(self):
        extra = self.mcp_defs([sys.executable, str(FAKE_MCP)])
        self.write("app.py", "def inspect_site(target):\n    pass\n")
        d = self.dcap(extra=[extra])
        self.assertEqual(d.integrate("WEB.SITE_LINKS")["status"], "integrated")
        policy = self.write("p.json", json.dumps({"grants": GRANTS}))
        run = self.run_py("import app; print(app.inspect_site('192.0.2.1')[0]['depth'])", policy)
        self.assertEqual(run.stdout.strip(), "9999", run.stderr)


class RegistryValidation(Base):
    def goal_defs(self, deps, result="x"):
        extra = self.root / "defs"
        (extra / "goals").mkdir(parents=True, exist_ok=True)
        (extra / "goals" / "g.yaml").write_text(yaml.safe_dump({"goals": [{
            "goal": "T.G", "version": "1", "name": "t", "aliases": ["t"],
            "parameters": {"target": {"type": "Target"}}, "dependencies": deps, "result": {"source": result},
        }]}))
        return extra

    def test_rejects_type_mismatch(self):
        extra = self.goal_defs([{"core": "web.link_discovery", "input": {"pages": "target"}, "output": "x"}])
        with self.assertRaisesRegex(RegistryError, "needs PageList"):
            Registries([extra])

    def test_rejects_unwired_required_input(self):
        extra = self.goal_defs([{"core": "web.page_discovery", "output": "x"}])
        with self.assertRaisesRegex(RegistryError, "not wired"):
            Registries([extra])

    def test_rejects_goal_cycle(self):
        extra = self.goal_defs([{"goal": "T.G", "input": {"target": "target"}, "output": "x"}])
        with self.assertRaisesRegex(RegistryError, "cycle"):
            Registries([extra])


class Observer(Base):
    def test_observes_without_writing(self):
        self.write("app.py", "class A:\n    async def go(self, x, y=1):\n        ...\n\ndef done():\n    return 1\n")
        self.write("package.json", '{"dependencies": {"left-pad": "1"}}')
        self.write("web.ts", "export const x = 1;\n")
        before = sorted(p.as_posix() for p in self.root.rglob("*"))
        d = self.dcap()
        symbols = {s.qualname: s for s in d.observer.symbols()}
        self.assertTrue(symbols["A.go"].is_stub and symbols["A.go"].is_async)
        self.assertEqual(symbols["A.go"].params, [("x", False), ("y", True)])
        self.assertFalse(symbols["done"].is_stub)
        snapshot = d.observer.snapshot()
        self.assertEqual(snapshot["languages"], {"python": 1, "typescript": 1})
        self.assertEqual(snapshot["dependencies"], ["left-pad"])
        self.assertEqual(sorted(p.as_posix() for p in self.root.rglob("*")), before)

    def test_background_thread_picks_up_changes(self):
        d = self.dcap()
        d.observer.start(interval=0.01)
        self.addCleanup(d.observer.stop)
        self.write("new.py", "def added():\n    pass\n")
        for _ in range(500):
            if any(s.qualname == "added" for s in d.observer.symbols()):
                break
            __import__("time").sleep(0.01)
        self.assertTrue(any(s.qualname == "added" for s in d.observer.symbols()))


class Injection(Base):
    SOURCE = 'def inspect_site(target):\n    """Keep me."""\n    pass\n\ndef other():\n    return 1\n'

    def test_function_bridge_runs_in_workspace_and_rolls_back_exactly(self):
        path = self.write("app.py", self.SOURCE)
        d = self.dcap()
        out = d.request("Check site links on this host")
        self.assertEqual(out["status"], "complete")
        self.assertEqual(out["integration"]["entry_point"], "app.py:inspect_site")
        text = path.read_text()
        self.assertIn('"""Keep me."""', text)
        self.assertIn("return 1", text)

        policy = self.write("p.json", json.dumps({"grants": GRANTS}))
        run = self.run_py("import app; print(len(app.inspect_site('192.0.2.1')))", policy)
        self.assertEqual(run.stdout.strip(), "3", run.stderr)

        released = d.release(out["integration"]["id"])
        self.assertEqual(released["status"], "released")
        self.assertEqual(path.read_text(), self.SOURCE)
        self.assertFalse((self.root / "dcap_capabilities").exists())

    def test_injected_code_enforces_authorisation(self):
        self.write("app.py", self.SOURCE)
        self.dcap().integrate("WEB.SITE_LINKS")
        run = self.run_py("import app; app.inspect_site('192.0.2.1')")
        self.assertIn("permission not granted", run.stderr)

    def test_only_selected_implementation_is_vendored(self):
        self.write("app.py", "def report_pages(target):\n    pass\n")
        out = self.dcap().integrate("Discover pages", "report_pages")
        self.assertEqual(out["status"], "integrated")
        names = {p.name for p in (self.root / "dcap_capabilities").glob("*.py")}
        self.assertEqual(
            names, {"__init__.py", "contract.py", "web_page_discovery.py", "placeholder.py", "policy.py"}
        )

    def test_method_and_async_bridges(self):
        self.write("svc.py", (
            "class SiteInspector:\n    def inspect(self, target):\n        pass\n\n"
            "async def crawl_site(address):\n    ...\n"
        ))
        d = self.dcap()
        first = d.integrate("WEB.SITE_LINKS", "SiteInspector.inspect")
        second = d.integrate("WEB.SITE_LINKS", "crawl_site")
        self.assertEqual(first["status"], "integrated")
        self.assertEqual(second["status"], "integrated")
        manifest = d.workspace.manifest()["integrations"]
        self.assertEqual(manifest[first["id"]]["bridge"], "python.method@2.0")
        self.assertEqual(manifest[second["id"]]["bridge"], "python.async_function@2.0")
        policy = self.write("p.json", json.dumps({"grants": GRANTS}))
        run = self.run_py(
            "import asyncio, svc\n"
            "print(len(svc.SiteInspector().inspect('192.0.2.1')), len(asyncio.run(svc.crawl_site('192.0.2.1'))))",
            policy,
        )
        self.assertEqual(run.stdout.strip(), "3 3", run.stderr)
        # Shared files survive until their last user is released.
        d.release(first["id"])
        self.assertEqual(d.workspace.conflicts(second["id"]), [])
        d.release(second["id"])
        self.assertFalse((self.root / "dcap_capabilities").exists())

    def test_one_line_stub_and_package_import_root(self):
        path = self.write("pkg/mod.py", "def inspect_site(target): pass\n")
        self.write("pkg/__init__.py", "")
        d = self.dcap()
        out = d.integrate("WEB.SITE_LINKS", "inspect_site")
        self.assertEqual(out["status"], "integrated")
        self.assertTrue((self.root / "dcap_capabilities").is_dir())
        policy = self.write("p.json", json.dumps({"grants": GRANTS}))
        run = self.run_py("from pkg.mod import inspect_site as f; print(len(f('192.0.2.1')))", policy)
        self.assertEqual(run.stdout.strip(), "3", run.stderr)
        d.release(out["id"])
        self.assertEqual(path.read_text(), "def inspect_site(target): pass\n")

    def test_abstains_rather_than_overwriting_or_guessing(self):
        path = self.write("app.py", "def inspect_site(target):\n    return 'mine'\n\ndef unrelated(x):\n    pass\n")
        d = self.dcap()
        self.assertEqual(d.integrate("WEB.SITE_LINKS")["reason"], "no_compatible_symbol")
        self.assertEqual(d.integrate("WEB.SITE_LINKS", "inspect_site")["reason"], "target_has_user_code")
        self.assertIn("mine", path.read_text())
        self.assertFalse((self.root / "dcap_capabilities").exists())

    def test_abstains_on_ambiguous_target(self):
        self.write("app.py", "def crawl_site(target):\n    pass\n\ndef find_site(target):\n    pass\n")
        out = self.dcap().integrate("WEB.SITE_LINKS")
        self.assertEqual(out["reason"], "ambiguous_target")
        self.assertEqual(len(out["candidates"]), 2)

    def test_abstains_when_parameters_cannot_map(self):
        self.write("app.py", "def inspect_site(a, b):\n    pass\n")
        out = self.dcap().integrate("WEB.SITE_LINKS", "inspect_site")
        self.assertEqual(out["reason"], "no_compatible_bridge")

    def test_refuses_to_overwrite_unowned_file(self):
        self.write("app.py", self.SOURCE)
        unowned = self.write("dcap_capabilities/__init__.py", "# mine\n")
        out = self.dcap().integrate("WEB.SITE_LINKS")
        self.assertEqual(out["reason"], "workspace_conflict")
        self.assertEqual(unowned.read_text(), "# mine\n")
        self.assertEqual((self.root / "app.py").read_text(), self.SOURCE)

    def test_user_edit_elsewhere_survives_release(self):
        path = self.write("app.py", self.SOURCE)
        d = self.dcap()
        integration = d.integrate("WEB.SITE_LINKS")["id"]
        path.write_text(path.read_text() + "\ndef later():\n    return 2\n")
        self.assertEqual(d.release(integration)["status"], "released")
        text = path.read_text()
        self.assertIn("def later", text)
        self.assertIn("    pass\n", text)
        self.assertNotIn("dcap:begin", text)

    def test_edit_inside_region_blocks_release_until_forced(self):
        path = self.write("app.py", self.SOURCE)
        d = self.dcap()
        integration = d.integrate("WEB.SITE_LINKS")["id"]
        path.write_text(path.read_text().replace("return _dcap_run", "print('hi')\n    return _dcap_run"))
        self.assertEqual(d.release(integration)["status"], "conflict")
        self.assertIn(integration, d.workspace.manifest()["integrations"])
        self.assertEqual(d.release(integration, force=True)["status"], "released")

    def test_hand_modified_managed_file_is_never_deleted(self):
        self.write("app.py", self.SOURCE)
        d = self.dcap()
        integration = d.integrate("WEB.SITE_LINKS")["id"]
        changed = self.root / "dcap_capabilities" / "placeholder.py"
        changed.write_text(changed.read_text() + "\n# hand edit\n")
        self.assertEqual(d.release(integration)["status"], "conflict")
        d.release(integration, force=True)
        self.assertIn("hand edit", changed.read_text())

    def test_status_reports_drift_and_update_reintegrates(self):
        path = self.write("app.py", self.SOURCE)
        d = self.dcap()
        integration = d.integrate("WEB.SITE_LINKS")["id"]
        self.assertEqual(d.status()["integrations"][integration]["problems"], [])
        path.write_text(path.read_text().replace("inspect_site(target)", "inspect_site(target, extra=1)"))
        problems = d.status()["integrations"][integration]["problems"]
        self.assertTrue(any("signature" in p for p in problems))
        self.assertEqual(d.update(integration)["status"], "integrated")

    def test_failed_write_rolls_back_and_crash_recovers(self):
        path = self.write("app.py", self.SOURCE)
        d = self.dcap()
        real = Workspace._write
        calls = []

        def flaky(ws, target, content):
            calls.append(target)
            if len(calls) == 3:
                raise OSError("disk full")
            real(ws, target, content)

        with mock.patch.object(Workspace, "_write", flaky):
            out = d.integrate("WEB.SITE_LINKS")
        self.assertEqual(out["reason"], "workspace_write_failed")
        self.assertEqual(path.read_text(), self.SOURCE)
        self.assertFalse((self.root / "dcap_capabilities").exists())
        self.assertFalse((self.root / ".dcap" / "journal.json").exists())

        # A process killed mid-transaction leaves a journal; the next start undoes it.
        (self.root / ".dcap" / "tx").mkdir(parents=True, exist_ok=True)
        (self.root / ".dcap" / "tx" / "0").write_text(self.SOURCE)
        (self.root / ".dcap" / "journal.json").write_text(json.dumps([["app.py", "0"], ["half.py", None]]))
        path.write_text("corrupted")
        self.write("half.py", "partial")
        Workspace(self.root)
        self.assertEqual(path.read_text(), self.SOURCE)
        self.assertFalse((self.root / "half.py").exists())



class Interface(Base):
    def test_goal_integrates_runs_and_returns_results(self):
        path = self.write("app.py", "X = 1\n")
        d = self.dcap()
        out = d.request("Check site links on this host")
        self.assertEqual(out["integration"]["status"], "integrated")
        self.assertEqual(out["status"], "complete")
        self.assertIn("from dcap_capabilities.web_site_links import run as check_site_links", path.read_text())
        policy = self.write("p.json", json.dumps({"grants": GRANTS}))
        run = self.run_py("import app; print(len(app.check_site_links('192.0.2.1')))", policy)
        self.assertEqual(run.stdout.strip(), "3", run.stderr)

    def test_status_and_results_survive_a_new_process(self):
        out = self.dcap().request("Check site links on this host")
        later = self.dcap()
        self.assertEqual(later.status(out["request_id"])["status"], "complete")
        self.assertEqual(later.results(out["request_id"])["result"], out["result"])

    def test_unknown_or_malformed_request_id(self):
        d = self.dcap()
        self.assertEqual(d.results("r000000000000")["status"], "unknown_request")
        self.assertEqual(d.results("../../secret")["status"], "unknown_request")

    def test_no_catalogue_on_unresolved(self):
        out = self.dcap().request("Bake me a sourdough loaf")
        self.assertEqual(out["status"], "nothing_available")
        self.assertNotIn("classes", out)
        self.assertNotIn("capabilities", out)

    def test_observer_alone_never_injects(self):
        path = self.write("app.py", "def inspect_site(target):\n    pass\n")
        before = path.read_text()
        d = self.dcap()
        d.observer.refresh()
        d.status()
        self.assertEqual(path.read_text(), before)
        self.assertFalse((self.root / "dcap_capabilities").exists())


class Door(Base):
    def test_dcap_adds_no_tool_definition(self):
        self.assertFalse((SRC / "dcap" / "mcp_server.py").exists())
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            main(["context"])
        self.assertEqual(json.loads(stdout.getvalue()), {"dcap_tool_definition_bytes": 0})



    def test_reply_is_exactly_what_was_pasted(self):
        path = self.write("app.py", "X = 1\n")
        reply = self.dcap().door("Help me investigate my website")
        self.assertIsInstance(reply, str)
        self.assertIn(reply, path.read_text())
        self.assertIn('"DISCOVER_PAGES"', reply)
        self.assertTrue(reply.startswith("# dcap:begin"))

    def test_reply_says_nothing_about_how_it_got_there(self):
        self.write("app.py", "X = 1\n")
        reply = self.dcap().door("Check site links on 192.0.2.1")
        for internal in ("request_id", "status", "goal_id", "steps", "provider", "placeholder", "complete", "undo"):
            self.assertNotIn(internal, reply)

    def test_a_clear_request_runs_nothing(self):
        path = self.write("app.py", "X = 1\n")
        with mock.patch("dcap.runtime.placeholder.discover_pages") as called:
            reply = self.dcap().door("Check site links on 192.0.2.1")
        called.assert_not_called()
        self.assertIn('"CHECK_SITE_LINKS"] = "false"', reply)
        self.assertIn(reply, path.read_text())

    def test_a_clear_request_needs_no_target_to_deliver_code(self):
        self.write("app.py", "X = 1\n")
        reply = self.dcap().door("Check site links")
        self.assertIn("check_site_links(target, depths=None)", reply)

    def test_goal_stays_off_until_switched_on(self):
        self.write("app.py", "X = 1\n")
        self.dcap().door("Check site links on 192.0.2.1")
        policy = self.write("p.json", json.dumps({"grants": GRANTS}))
        call = "import app; print(len(app.check_site_links('192.0.2.1')))"
        self.assertIn("CHECK_SITE_LINKS is off", self.run_py(call, policy, switches_on=False).stderr)
        self.assertEqual(self.run_py(call, policy).stdout.strip(), "3")

    def test_status_and_lifecycle_words_are_just_requests(self):
        self.write("app.py", "X = 1\n")
        d = self.dcap()
        integration = d.request("Help me investigate my website", execute=False)["integrations"][0]["id"]
        for text in ("status", f"release {integration}", "results r000000000000"):
            self.assertEqual(d.door(text), "", text)
        self.assertIn(integration, d.workspace.manifest()["integrations"])

    def test_nothing_available_means_an_empty_reply(self):
        path = self.write("app.py", "X = 1\n")
        self.assertEqual(self.dcap().door("Bake a sourdough loaf"), "")
        self.assertEqual(path.read_text(), "X = 1\n")

    def test_empty_project_still_gets_the_code(self):
        reply = self.dcap().door("Help me investigate my website")
        self.assertIn("run as discover_pages", reply)
        self.assertTrue((self.root / "dcap_capabilities" / "web_page_discovery.py").exists())

    def test_repeat_shows_the_switches_as_they_stand(self):
        path = self.write("app.py", "X = 1\n")
        d = self.dcap()
        d.door("Help me investigate my website")
        path.write_text(path.read_text().replace('["DISCOVER_PAGES"] = "false"', '["DISCOVER_PAGES"] = "true"'))
        self.assertIn('["DISCOVER_PAGES"] = "true"', d.door("Help me investigate my website"))

    def test_nothing_is_written_without_a_call(self):
        path = self.write("app.py", "def inspect_site(target):\n    pass\n")
        before = path.read_text()
        d = self.dcap()
        d.observer.start(interval=0.01)
        self.addCleanup(d.observer.stop)
        path.write_text(before + "# still being written\n")
        __import__("time").sleep(0.1)
        self.assertEqual(path.read_text(), before + "# still being written\n")
        self.assertFalse((self.root / "dcap_capabilities").exists())



class Listening(Base):
    def speak(self, transcript, text):
        with transcript.open("a") as handle:
            handle.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}) + "\n")

    def hook(self, transcript, name="Stop", **extra):
        event = {"hook_event_name": name, "transcript_path": str(transcript), **extra}
        return claude_code_hook(self.dcap(), event)

    def setUp(self):
        super().setUp()
        self.app = self.write("app.py", "X = 1\n")
        self.transcript = self.root / "t.jsonl"

    def test_silent_when_there_is_nothing_to_offer(self):
        for text in ("I need to fix the link parsing bug.", "Let me read the config file.", "The website is fine."):
            self.speak(self.transcript, text)
            self.assertIsNone(self.hook(self.transcript), text)
        self.assertEqual(self.app.read_text(), "X = 1\n")

    def test_raises_a_hand_and_writes_nothing_until_yes(self):
        self.speak(self.transcript, "I need to check site links on this host.")
        offer = self.hook(self.transcript)
        self.assertEqual(offer["decision"], "block")
        self.assertIn("want help with check site links", offer["reason"])
        self.assertEqual(self.app.read_text(), "X = 1\n")
        self.speak(self.transcript, "Yes please.")
        reply = self.hook(self.transcript)
        self.assertIn(reply["reason"], self.app.read_text())
        self.assertIn('"CHECK_SITE_LINKS"] = "false"', reply["reason"])

    def test_no_writes_nothing_and_is_not_asked_again(self):
        self.speak(self.transcript, "I need to check site links on this host.")
        self.assertIsNotNone(self.hook(self.transcript))
        self.speak(self.transcript, "No, thanks.")
        self.assertIsNone(self.hook(self.transcript))
        self.speak(self.transcript, "I need to check site links on this host.")
        self.assertIsNone(self.hook(self.transcript))
        self.assertEqual(self.app.read_text(), "X = 1\n")

    def test_carrying_on_without_answering_is_a_no(self):
        self.speak(self.transcript, "I need to check site links on this host.")
        self.hook(self.transcript)
        for text in ("Carrying on with the parser.", "Still on the parser."):
            self.speak(self.transcript, text)
            self.assertIsNone(self.hook(self.transcript))
        self.assertIsNone(Listener(self.dcap()).state["pending"])
        self.assertEqual(self.app.read_text(), "X = 1\n")

    def test_notices_what_the_ai_writes_natively(self):
        out = self.hook(self.transcript, "PostToolUse", tool_name="Write",
                        tool_input={"content": "def check_site_links(target):\n    pass\n"})
        self.assertIn("want help", out["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "PostToolUse")

    def test_a_word_in_code_is_not_a_need(self):
        out = self.hook(self.transcript, "PostToolUse", tool_name="Edit",
                        tool_input={"new_string": "def site_timeout():\n    return 5\n# the site layer\n"})
        self.assertIsNone(out)

    def test_vague_need_gets_switches_on_yes(self):
        self.speak(self.transcript, "I want to investigate my website.")
        self.assertIn("web capabilities", self.hook(self.transcript)["reason"])
        self.speak(self.transcript, "Yes.")
        self.assertIn('"DISCOVER_PAGES"] = "false"', self.hook(self.transcript)["reason"])

    def test_old_speech_is_read_once(self):
        self.speak(self.transcript, "I need to check site links on this host.")
        self.hook(self.transcript)
        self.speak(self.transcript, "Yes.")
        self.hook(self.transcript)
        self.assertIsNone(self.hook(self.transcript))
        self.assertEqual(self.app.read_text().count("dcap:begin"), 1)

    def test_already_delivered_is_not_offered_again(self):
        self.dcap().door("Check site links")
        self.speak(self.transcript, "I need to check site links on this host.")
        self.assertIsNone(self.hook(self.transcript))

    def test_any_adapter_can_listen_through_the_command_line(self):
        env = {**os.environ, "PYTHONPATH": str(SRC)}
        command = [sys.executable, "-m", "dcap.cli", "--workspace", str(self.root), "listen"]
        ask = json.dumps({"said": ["I need to check site links on this host."], "wrote": []})
        offer = subprocess.run(command, input=ask, capture_output=True, text=True, env=env)
        self.assertIn("want help", offer.stdout)
        self.assertEqual(self.app.read_text(), "X = 1\n")
        reply = subprocess.run(command, input=json.dumps({"said": ["Yes."], "wrote": []}), capture_output=True, text=True, env=env)
        self.assertIn(reply.stdout.strip(), self.app.read_text())

    def test_recognises_a_need_inside_a_natural_sentence(self):
        self.speak(self.transcript, "I\u2019ll go through the website and look at each page.")
        self.assertIn("web capabilities", self.hook(self.transcript)["reason"])
        self.speak(self.transcript, "Yes.")
        self.assertIn('"DISCOVER_PAGES"] = "false"', self.hook(self.transcript)["reason"])

    def test_real_agent_sentences(self):
        """Sentences a live Codex agent actually said, and whether DCAP should react."""
        speech = Listener(self.dcap())
        extra = Path(__file__).resolve().parents[1] / "examples" / "swagger"
        speech = Listener(self.dcap(extra=[extra]))
        need = "I\u2019ll check the project\u2019s conventions, add a small endpoint lister in `main.py`, run it against the local OpenAPI document, and show the result."
        self.assertIn("api capabilities", speech.notice([need]).lower())
        speech.state["pending"] = None
        for quiet in (
            "I\u2019ll inspect the project instructions and `main.py`, then add `slugify(text)` and run a focused check against representative inputs.",
            "`main.py` is currently just a module docstring, with no project-specific test setup visible. I\u2019ll use a standard Unicode-aware slug convention: lowercase text, normalize accents, and collapse non-alphanumeric runs into single hyphens.",
            "The implementation is in place, but this environment has no `python` executable on `PATH`. I\u2019ll check for `python3` and run the same focused checks with that interpreter.",
        ):
            self.assertIsNone(speech.notice([quiet]), quiet)

    def test_one_shared_word_in_a_sentence_stays_silent(self):
        self.speak(self.transcript, "I'll inspect the project layout and add a service class for orders.")
        self.assertIsNone(self.hook(self.transcript))

    def test_ai_needs_no_tool_or_instruction(self):
        self.assertFalse((SRC / "dcap" / "mcp_server.py").exists())
        parser_help = subprocess.run([*OPERATOR_CLI, "--help"], capture_output=True, text=True,
                                     env={**os.environ, "PYTHONPATH": str(SRC)}).stdout
        self.assertNotIn(" ask ", parser_help)
        self.assertNotRegex(parser_help, r"\{[^}]*\b(?:serve|ask)\b")

    def test_failures_stay_silent_and_go_to_the_log(self):
        self.write(".dcap/manifest.json", "{bad")
        self.speak(self.transcript, "I need to check site links on this host.")
        event = json.dumps({"hook_event_name": "Stop", "transcript_path": str(self.transcript), "cwd": str(self.root)})
        run = subprocess.run([sys.executable, "-m", "dcap.cli", "hook"], input=event, capture_output=True, text=True,
                             env={**os.environ, "PYTHONPATH": str(SRC), "CLAUDE_PROJECT_DIR": str(self.root)})
        self.assertEqual((run.returncode, run.stdout, run.stderr), (0, "", ""))
        self.assertIn("corrupt", (self.root / ".dcap" / "door.log").read_text())


class Hardening(Base):
    def test_declarations_are_never_filled(self):
        path = self.write("app.py", (
            "from abc import ABC, abstractmethod\nfrom typing import Protocol, overload\n\n"
            "class SiteBase(ABC):\n    @abstractmethod\n    def inspect_site(self, target):\n        pass\n\n"
            "class SitePort(Protocol):\n    def crawl_site(self, target): ...\n\n"
            "@overload\ndef find_site(target: str): ...\n"
        ))
        before = path.read_text()
        d = self.dcap()
        self.assertEqual(d.integrate("WEB.SITE_LINKS")["reason"], "no_compatible_symbol")
        out = d.integrate("WEB.SITE_LINKS", "SiteBase.inspect_site")
        self.assertEqual(out["reason"], "target_is_declaration")
        self.assertEqual(path.read_text(), before)

    def test_tests_are_not_auto_selected(self):
        self.write("tests/helpers.py", "def inspect_site(target):\n    pass\n")
        self.write("test_app.py", "def inspect_site(target):\n    pass\n")
        self.assertEqual(self.dcap().integrate("WEB.SITE_LINKS")["reason"], "no_compatible_symbol")

    def test_stub_comment_survives_release_after_other_edits(self):
        path = self.write("app.py", "def inspect_site(target):\n    pass  # TODO later\n")
        d = self.dcap()
        integration = d.integrate("WEB.SITE_LINKS")["id"]
        path.write_text(path.read_text() + "\nX = 1\n")
        d.release(integration)
        self.assertEqual(path.read_text(), "def inspect_site(target):\n    pass  # TODO later\n\nX = 1\n")

    def test_symlink_out_of_workspace_is_refused(self):
        with tempfile.TemporaryDirectory() as outside:
            real = Path(outside) / "real.py"
            real.write_text("def inspect_site(target):\n    pass\n")
            (self.root / "link.py").symlink_to(real)
            out = self.dcap().integrate("WEB.SITE_LINKS")
            self.assertEqual(out["status"], "abstained")
            self.assertNotIn("dcap", real.read_text())

    def test_generated_names_cannot_collide(self):
        extra = self.root / "defs"
        (extra / "goals").mkdir(parents=True)
        (extra / "goals" / "g.yaml").write_text(yaml.safe_dump({"goals": [{
            "goal": "T.NAMES", "version": "1", "name": "Collide", "aliases": ["zzz collide"],
            "parameters": {"target": {"type": "Target"}},
            "dependencies": [
                {"core": "web.page_discovery", "input": {"target": "target"}, "output": "discover_pages"},
                {"core": "web.link_discovery", "input": {"pages": "discover_pages"}, "output": "authorize"},
            ],
            "result": {"source": "authorize"},
        }]}))
        self.write("app.py", "def run_site(target):\n    pass\n")
        out = self.dcap(extra=[extra]).integrate("T.NAMES", "run_site")
        self.assertEqual(out["status"], "integrated", out)
        policy = self.write("p.json", json.dumps({"grants": GRANTS}))
        run = self.run_py("import app; print(len(app.run_site('192.0.2.1')))", policy)
        self.assertEqual(run.stdout.strip(), "3", run.stderr)

    def test_invalid_identifier_rejected(self):
        extra = self.root / "defs"
        (extra / "goals").mkdir(parents=True)
        (extra / "goals" / "g.yaml").write_text(yaml.safe_dump({"goals": [{
            "goal": "T.BAD", "version": "1", "name": "Bad", "aliases": ["zzz"],
            "parameters": {"target": {"type": "Target"}},
            "dependencies": [{"core": "web.page_discovery", "input": {"target": "target"}, "output": "class"}],
            "result": {"source": "class"},
        }]}))
        with self.assertRaisesRegex(RegistryError, "valid identifier"):
            Registries([extra])

    def test_update_leaves_working_integration_when_it_cannot_land(self):
        path = self.write("app.py", "def inspect_site(target):\n    pass\n")
        d = self.dcap()
        integration = d.integrate("WEB.SITE_LINKS")["id"]
        text = path.read_text().replace("(target)", "(a, b)")
        path.write_text(text)
        self.assertEqual(d.update(integration)["reason"], "no_compatible_bridge")
        self.assertIn(integration, d.workspace.manifest()["integrations"])
        self.assertEqual(path.read_text(), text)


class ScopedPermissions(Base):
    def test_scope_limits_targets(self):
        d = self.dcap(grants={"web.fetch": ["192.0.2.0/24"]})
        self.assertEqual(d.request("Check site links on 192.0.2.7")["status"], "complete")
        out = d.request("Check site links on 198.51.100.7")
        self.assertEqual(out["status"], "authorization_required")
        self.assertEqual(out["scope"], ["198.51.100.7"])
        self.assertNotIn("result", out)

    def test_wider_range_than_grant_is_refused(self):
        d = self.dcap(grants={"web.fetch": ["192.0.2.0/24"]})
        self.assertEqual(d.request("Check site links on 192.0.0.0/16")["status"], "authorization_required")

    def test_targets_discovered_mid_run_are_checked(self):
        d = self.dcap(grants={"web.fetch": ["192.0.2.0/24"]})
        with mock.patch("dcap.runtime.placeholder.discover_pages", return_value=[{"address": "203.0.113.9"}]):
            out = d.request("Check site links on 192.0.2.7")
        self.assertEqual(out["status"], "partial")
        self.assertEqual(out["errors"][0]["code"], "UNAUTHORIZED")

    def test_host_name_patterns(self):
        d = self.dcap(grants={"web.fetch": ["*.example.test"]})
        self.assertEqual(d.request("Check site links on a.example.test")["status"], "complete")
        self.assertEqual(d.request("Check site links on example.test.evil.org")["status"], "authorization_required")

    def test_injected_code_enforces_scope(self):
        self.write("app.py", "def inspect_site(target):\n    pass\n")
        self.dcap().integrate("WEB.SITE_LINKS")
        policy = self.write("p.json", json.dumps({"grants": {"web.fetch": ["192.0.2.0/24"]}}))
        self.assertEqual(self.run_py("import app; print(len(app.inspect_site('192.0.2.1')))", policy).stdout.strip(), "3")
        self.assertIn("permission not granted", self.run_py("import app; app.inspect_site('198.51.100.1')", policy).stderr)

    def test_malformed_policy_fails_closed(self):
        policy = self.write("bad.json", "{not json")
        with mock.patch.dict(os.environ, {"DCAP_POLICY": str(policy)}):
            out = Dcap(self.root).request("Check site links on this host")
        self.assertEqual(out["status"], "authorization_required")


class HttpProvider(Base):
    def setUp(self):
        super().setUp()
        seen = self.seen = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                seen["auth"] = self.headers.get("X-Key")
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/links")
                    self.end_headers()
                    return
                found = [{"address": h["address"], "depth": 7777, "kind": "internal"} for h in body["targets"]]
                data = json.dumps({"open": found}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def defs(self, path):
        extra = self.root / "defs"
        (extra / "providers").mkdir(parents=True, exist_ok=True)
        (extra / "providers" / "api.yaml").write_text(yaml.safe_dump({"providers": [{
            "provider": "secretapi.http", "kind": "http", "priority": 1,
            "url": f"http://127.0.0.1:{self.server.server_port}", "headers_env": {"X-Key": "DCAP_TEST_KEY"},
            "implements": {"web.link_discovery": {
                "contract": 1, "operation": path, "input_map": {"pages": "targets"}, "output_key": "open",
            }},
        }]}))
        return extra

    def test_http_provider_behind_standard_contract(self):
        with mock.patch.dict(os.environ, {"DCAP_TEST_KEY": "abc"}):
            out = self.dcap(extra=[self.defs("/links")]).request("Check site links on this host")
        self.assertEqual({s["depth"] for s in out["result"]}, {7777})
        self.assertEqual(self.seen["auth"], "abc")
        self.assertNotIn("secretapi", json.dumps(out))

    def test_redirect_is_not_followed_and_fails_over(self):
        with mock.patch.dict(os.environ, {"DCAP_TEST_KEY": "abc"}):
            out = self.dcap(extra=[self.defs("/redirect")]).request("Check site links on this host")
        self.assertEqual({s["depth"] for s in out["result"]}, {1, 2, 3})

    def test_missing_secret_fails_over(self):
        env = {k: v for k, v in os.environ.items() if k != "DCAP_TEST_KEY"}
        with mock.patch.dict(os.environ, env, clear=True):
            out = self.dcap(extra=[self.defs("/links")]).request("Check site links on this host")
        self.assertEqual({s["depth"] for s in out["result"]}, {1, 2, 3})
        self.assertNotIn("auth", self.seen)


class StatefulProvider(Base):
    def defs(self, source="ok"):
        extra = self.root.parent / (self.root.name + "-stateful")
        (extra / "providers").mkdir(parents=True, exist_ok=True)
        (extra / "providers" / "s.yaml").write_text(yaml.safe_dump({"providers": [{
            "provider": "stateful.mcp", "kind": "mcp", "priority": 1,
            "command": [sys.executable, str(Path(__file__).parent / "fixtures" / "stateful_mcp.py")],
            "implements": {"web.page_discovery": {
                "contract": 1, "operation": "read",
                "before": [{"tool": "load", "input_map": {"target": "source"}}],
            }},
        }]}))
        self.addCleanup(lambda: __import__("shutil").rmtree(extra, ignore_errors=True))
        return extra

    def test_earlier_tool_runs_in_the_same_session_and_uses_its_input(self):
        out = self.dcap(extra=[self.defs()]).request("Map pages on 192.0.2.1")
        self.assertEqual(out["result"], [{"address": "192.0.2.1"}])

    def test_injected_code_does_the_same(self):
        self.write("app.py", "X = 1\n")
        self.dcap(extra=[self.defs()]).door("Map pages")
        policy = self.write("p.json", json.dumps({"grants": GRANTS}))
        run = self.run_py("import app; print(app.map_pages('192.0.2.1'))", policy)
        self.assertIn("192.0.2.1", run.stdout, run.stderr)

    def test_a_failed_earlier_tool_fails_over(self):
        extra = self.defs()
        out = self.dcap(extra=[extra]).request("Map pages", {"target": "broken"})
        self.assertEqual(out["status"], "complete")
        self.assertEqual(out["result"], [{"address": "broken"}])


class Vocabulary(Base):
    def test_stopwords_are_really_stripped(self):
        tokens = self.dcap().resolver.lex.tokens("Check site links on 10.1.2.3")
        self.assertEqual(tokens, {"check", "site", "link"})

    def test_rewording_with_a_preposition_still_matches(self):
        out = self.dcap().request("Check the links on 192.0.2.1", execute=False)
        self.assertEqual(out["goal_id"], "WEB.SITE_LINKS")

    def test_unquoted_yaml_boolean_is_rejected(self):
        extra = self.root / "defs"
        extra.mkdir()
        (extra / "vocabulary.yaml").write_text("stopwords: [on]\n")
        with self.assertRaisesRegex(RegistryError, "quote it"):
            Registries([extra])


class FallbackAndExplain(Base):
    def test_one_shared_word_is_not_a_suggestion(self):
        out = self.dcap().request("Read site metadata for example.test")
        self.assertEqual(out["mode"], "direct")
        self.assertNotIn("suggestion", out)

    def test_goal_can_be_requested_by_id(self):
        out = self.dcap().request("web.site_links", {"target": "192.0.2.1"})
        self.assertEqual(out["goal_id"], "WEB.SITE_LINKS")
        self.assertEqual(out["status"], "complete")

    def test_near_miss_suggests_without_running(self):
        out = self.dcap().request("Check site links on this host and delete them")
        self.assertEqual(out["mode"], "direct")
        self.assertEqual(out["suggestion"]["goals"][0]["goal_id"], "WEB.SITE_LINKS")
        self.assertNotIn("steps", out)

    def test_explain_shows_plan_and_runs_nothing(self):
        d = self.dcap(grants=[])
        with mock.patch("dcap.runtime.placeholder.discover_pages") as called:
            out = d.explain("Audit this site and generate a site report", {"target": "192.0.2.1"})
        called.assert_not_called()
        self.assertEqual(out["goal"], "WEB.SITE_AUDIT")
        self.assertEqual(sum(len(level) for level in out["stages"]), 5)
        self.assertEqual(out["stages"][0][0]["providers"], ["placeholder.native"])
        self.assertEqual(out["authorization"]["status"], "authorization_required")

    def test_provider_coverage(self):
        coverage = self.dcap().providers()
        self.assertTrue(coverage["web.sitemap"][0]["available"])


class Scaffolding(Base):
    def test_draft_maps_tools_and_loads_as_a_provider(self):
        d = self.dcap()
        result = draft("vendor.mcp", [sys.executable, str(FAKE_MCP)], d.registries, d.resolver.lex)
        provider = result["definition"]["providers"][0]
        entry = provider["implements"]["web.link_discovery"]
        self.assertEqual(entry["operation"], "link_crawl")
        self.assertEqual(result["unmapped_tools"], ["rotate_vendor_logs"])
        self.assertEqual(len(provider["implements"]), 1)
        extra = self.root / "defs" / "providers"
        extra.mkdir(parents=True)
        (extra / "v.yaml").write_text(render(result))
        self.assertEqual(yaml.safe_load(render(result))["providers"][0]["provider"], "vendor.mcp")
        Registries([self.root / "defs"])

    def test_context_comparison_uses_the_real_server(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            main(["context", "--", sys.executable, str(FAKE_MCP)])
        out = json.loads(stdout.getvalue())
        self.assertEqual(out["server"]["tools"], 2)
        self.assertEqual(out["dcap_tool_definition_bytes"], 0)



class DefinitionsHelper(Base):
    def defs(self, contract=1, goal_version="1", child_version="1", cores=None, priority=None):
        """A widget class, a one-stage goal and a two-level goal, in a folder outside the workspace."""
        extra = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(extra, ignore_errors=True))
        for folder in ("capabilities", "goals", "providers"):
            (extra / folder).mkdir()
        (extra / "capabilities" / "widget.yaml").write_text(yaml.safe_dump({
            "class": "widget", "title": "Widget", "keywords": ["widget"], "cores": cores or {
                "widget.polish": {
                    "name": "Polish widget", "contract": contract, "description": "Polish one.",
                    "inputs": {"target": {"type": "Target"}}, "output": "PageList",
                },
            },
        }))
        (extra / "goals" / "widget.yaml").write_text(yaml.safe_dump({"goals": [
            {
                "goal": "WIDGET.CHILD", "version": child_version, "name": "Rub widget", "aliases": ["rub the widget"],
                "parameters": {"target": {"type": "Target", "extract": "target"}}, "classes": ["widget"],
                "dependencies": [{"core": "widget.polish", "input": {"target": "target"}, "output": "out"}],
                "result": {"source": "out"},
            },
            {
                "goal": "WIDGET.SHINE", "version": goal_version, "name": "Shine the widget",
                "aliases": ["shine the widget"],
                "parameters": {"target": {"type": "Target", "extract": "target"}}, "classes": ["widget"],
                "dependencies": [{"goal": "WIDGET.CHILD", "input": {"target": "target"}, "output": "out"}],
                "result": {"source": "out"},
            },
        ]}))
        if not cores:
            (extra / "providers" / "widget.yaml").write_text(yaml.safe_dump({"providers": [{
                "provider": "widget.native", "version": "1", "kind": "native", "vendor": ["placeholder.py"],
                "priority": 5 if priority is None else priority,
                "implements": {"widget.polish": {
                    "contract": contract, "operation": "dcap.runtime.placeholder:discover_pages",
                }},
            }]}))
        return extra

    def server(self, reply):
        """An MCP server that always returns this reply."""
        script = Path(tempfile.mkdtemp()) / "server.py"
        self.addCleanup(lambda: __import__("shutil").rmtree(script.parent, ignore_errors=True))
        script.write_text(
            "import json, sys\n"
            f"REPLY = {json.dumps(reply)!r}\n"
            "for line in sys.stdin:\n"
            "    m = json.loads(line)\n"
            "    if 'id' not in m:\n"
            "        continue\n"
            "    r = {'content': [{'type': 'text', 'text': REPLY}]} if m['method'] == 'tools/call' else {}\n"
            "    print(json.dumps({'jsonrpc': '2.0', 'id': m['id'], 'result': r}), flush=True)\n"
        )
        return [sys.executable, str(script)]

    def provider_defs(self, spec):
        extra = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(extra, ignore_errors=True))
        (extra / "providers").mkdir()
        (extra / "providers" / "p.yaml").write_text(yaml.safe_dump({"providers": [spec]}))
        return extra


class ScopeNames(Base):
    def test_only_real_names_in_the_domain_are_covered(self):
        for value in ("203.0.113.9/.example.test", "evil.org/.example.test", "evil.org#.example.test",
                      "evil.org .example.test", "http://203.0.113.9/.example.test", ".example.test"):
            with self.subTest(value):
                self.assertFalse(covers("*.example.test", value))
        self.assertTrue(covers("*.example.test", "a.b.example.test"))

    def test_request_cannot_pass_an_unnamed_target_through_a_host_grant(self):
        d = self.dcap(grants={"web.fetch": ["*.example.test"]})
        out = d.request("Map pages", {"target": "203.0.113.9/.example.test"})
        self.assertNotEqual(out["status"], "complete")
        self.assertNotIn("result", out)

    def test_injected_guard_refuses_it_too(self):
        self.write("app.py", "def inspect_site(target):\n    pass\n")
        self.dcap().integrate("WEB.PAGE_MAP")
        policy = self.write("p.json", json.dumps({"grants": {"web.fetch": ["*.example.test"]}}))
        run = self.run_py("import app; app.inspect_site('203.0.113.9/.example.test')", policy)
        self.assertIn("permission not granted", run.stderr)

    def test_empty_target_is_invalid_even_with_an_unscoped_grant(self):
        for value in ("", "   "):
            out = self.dcap().request("Check site links", {"target": value})
            self.assertEqual(out["errors"][0]["code"], "INVALID_INPUT")
            self.assertNotIn("result", out)


class TargetParsing(Base):
    def outcome(self, text, grants=GRANTS):
        return self.dcap(grants=grants).request(text)

    def test_more_than_one_target_goes_to_direct(self):
        for text in ("Check site links on 192.0.2.1 and 192.0.2.2",
                     "Check site links on 10.0.0.1, 10.0.0.2 and 10.0.0.3",
                     "Discover pages on 10.0.0.0/24 and 192.0.2.0/24",
                     "Check site links on this host and 10.0.0.5",
                     "Check site links on 10.0.0.1-10.0.0.50",
                     "Check site links on 999.1.1.1 and 192.0.2.2"):
            with self.subTest(text):
                out = self.outcome(text)
                self.assertEqual(out["mode"], "direct")
                self.assertNotIn("result", out)

    def test_out_of_scope_second_target_is_not_hidden_by_complete(self):
        out = self.outcome("Check site links on 10.0.0.1 and 8.8.8.8", {"web.fetch": ["10.0.0.1/32"]})
        self.assertEqual(out["mode"], "direct")

    def test_misread_hosts_go_to_direct(self):
        for text in ("Check site links in main.py", "Check site links on mylocalhost",
                     "Check site links on 10.0.0.1.5", "Check site links on 10.0.0.1:8080",
                     "Check site links on this host 8080", "Check site links in foo.config"):
            with self.subTest(text):
                out = self.outcome(text)
                self.assertEqual(out["mode"], "direct")
                self.assertNotIn("result", out)

    def test_ordinary_targets_still_bind(self):
        d = self.dcap()
        for text, target in (("Discover pages on a.example.test.", "a.example.test"), ("Discover pages on localhost", "127.0.0.1"),
                             ("Discover pages on 10.0.0.0/24", "10.0.0.0/24")):
            with self.subTest(text):
                self.assertEqual(d.explain(text)["params"], {"target": target})

    def test_goal_id_is_not_a_host(self):
        for goal_id in ("WEB.SITE_AUDIT", "WEB.SITEMAP", "WEB.LINK_REVIEW"):
            with self.subTest(goal_id):
                out = self.dcap().request(goal_id)
                self.assertEqual(out["mode"], "direct")
                self.assertEqual(out["suggestion"]["goals"][0]["missing"], ["target"])

    def test_words_outside_ascii_count_as_extra_intent(self):
        for extra in ("удалить их", "删除所有"):
            with self.subTest(extra):
                out = self.outcome(f"Check site links on this host {extra}")
                self.assertEqual(out["mode"], "direct")
                self.assertNotIn("result", out)


class Domain(Base):
    SOURCE = "X = 1\n"

    def test_one_keyword_does_not_integrate_an_unrelated_request(self):
        path = self.write("app.py", self.SOURCE)
        for text in ("Delete all files on this host", "Fix the customer service page",
                     "Bake a loaf at docs.example.test", "Write a summary of my holiday", "Bake a loaf on this host"):
            with self.subTest(text):
                out = self.dcap().request(text)
                self.assertEqual(out["status"], "nothing_available")
                self.assertEqual(path.read_text(), self.SOURCE)
        self.assertFalse((self.root / "dcap_capabilities").exists())

    def test_requests_in_the_domain_still_expose(self):
        self.write("app.py", self.SOURCE)
        for text in ("Help me investigate this site", "Read site metadata for example.test", "Run a page check"):
            with self.subTest(text):
                self.assertEqual(self.dcap().request(text)["mode"], "direct")

    def test_two_goals_named_together_are_suggested_not_refused(self):
        self.write("app.py", self.SOURCE)
        out = self.dcap().request("Map pages and review links on 192.0.2.1")
        self.assertEqual(out["mode"], "direct")
        self.assertEqual({g["goal_id"] for g in out["suggestion"]["goals"]},
                         {"WEB.PAGE_MAP", "WEB.LINK_REVIEW"})
        self.assertNotIn("result", out)


class OutputContract(DefinitionsHelper):
    def vendor_defs(self, reply, output_key=None):
        entry = {"contract": 1, "operation": "x"}
        if output_key:
            entry["output_key"] = output_key
        return self.provider_defs({
            "provider": "secretvendor.mcp", "kind": "mcp", "priority": 1, "command": self.server(reply),
            "implements": {"web.page_discovery": entry},
        })

    def test_undeclared_fields_never_reach_the_result(self):
        extra = self.vendor_defs([{"address": "192.0.2.1", "secretvendor_job_id": "abc", "engine": "secretvendor 9"}])
        d = self.dcap(extra=[extra])
        out = d.request("Map pages on 192.0.2.1")
        self.assertEqual(out["result"], [{"address": "192.0.2.1"}])
        self.assertNotIn("secretvendor", json.dumps(out))
        self.assertNotIn("secretvendor", json.dumps(d.results(out["request_id"])))

    def test_open_types_stay_open(self):
        out = self.dcap().request("Check site links on 192.0.2.1")
        self.assertEqual(out["result"][0]["metadata"], {"simulated": True})

    def injected(self, extra):
        self.write("app.py", "X = 1\n")
        self.dcap(extra=[extra]).request("Help me investigate my website")
        path = self.root / "app.py"
        path.write_text(path.read_text().replace("'Discover pages': False", "'Discover pages': True"))
        return self.write("p.json", json.dumps({"grants": GRANTS}))

    def test_injected_code_checks_output_and_uses_standard_errors(self):
        policy = self.injected(self.vendor_defs({"totally": "wrong"}))
        run = self.run_py("import app\n"
                          "from dcap_capabilities.contract import CapabilityError\n"
                          "try:\n    app.discover_pages('192.0.2.1')\n"
                          "except CapabilityError as e:\n    print(e.code)\n", policy)
        self.assertEqual(run.stdout.strip(), "INVALID_OUTPUT", run.stderr)

    def test_injected_code_hides_provider_field_names(self):
        policy = self.injected(self.vendor_defs({"totally": "wrong"}, output_key="vendorfield"))
        run = self.run_py("import app; app.discover_pages('192.0.2.1')", policy)
        self.assertIn("CapabilityError", run.stderr)
        self.assertIn("EXECUTION_FAILED", run.stderr)
        self.assertNotIn("vendorfield", run.stderr.splitlines()[-1])

    def test_injected_code_checks_input(self):
        self.write("app.py", "def inspect_site(target):\n    pass\n")
        self.dcap().integrate("WEB.PAGE_MAP")
        policy = self.write("p.json", json.dumps({"grants": GRANTS}))
        self.assertIn("INVALID_INPUT", self.run_py("import app; app.inspect_site(42)", policy).stderr)

    def test_injected_code_strips_undeclared_fields_and_uses_core_timeout(self):
        extra = self.vendor_defs([{"address": "192.0.2.1", "engine": "secretvendor 9"}])
        policy = self.injected(extra)
        run = self.run_py("import app; print(app.discover_pages('192.0.2.1'))", policy)
        self.assertEqual(run.stdout.strip(), "[{'address': '192.0.2.1'}]", run.stderr)
        cores = {"widget.polish": {"name": "Polish widget", "contract": 1, "inputs": {}, "output": "PageList",
                                   "execution": {"timeout_s": 7, "retries": 3}}}
        defs = self.defs(cores=cores)
        (defs / "goals" / "widget.yaml").unlink()
        (defs / "providers" / "widget.yaml").write_text(yaml.safe_dump({"providers": [{
            "provider": "w.http", "kind": "http", "url": "http://127.0.0.1:9",
            "implements": {"widget.polish": {"contract": 1, "operation": "/x"}},
        }]}))
        self.dcap(extra=[defs]).request("help widget")
        generated = next((self.root / "dcap_capabilities").glob("widget_polish.py")).read_text()
        self.assertIn(", 7), 3)", generated)


class ModularPaste(Base):
    def test_each_capability_is_its_own_module_import_and_switch(self):
        path = self.write("app.py", "X = 1\n")
        d = self.dcap()
        out = d.request("Help me investigate my website", execute=False)
        usage = out["integrations"][0]["usage"]
        text = path.read_text()
        cores = d.registries.classes["web"].cores
        self.assertEqual(len(usage), len(cores))
        for core_id in cores:
            module = facade_name(core_id, d.registries)
            self.assertTrue((self.root / "dcap_capabilities" / f"{module}.py").exists(), module)
            self.assertEqual(text.count(f"from dcap_capabilities.{module} import run as "), 1)
        self.assertEqual(text.count('.environ["'), len(cores))

    def test_using_one_capability_loads_only_that_module(self):
        self.write("app.py", "X = 1\n")
        self.dcap().request("Help me investigate my website", execute=False)
        code = ("import sys; import dcap_capabilities.web_page_discovery; "
                "print(sorted(m for m in sys.modules if m.startswith('dcap_capabilities.web_')))")
        run = self.run_py(code)
        self.assertEqual(run.stdout.strip(), "['dcap_capabilities.web_page_discovery']", run.stderr)

    def test_import_os_inside_a_string_does_not_count_as_importing_os(self):
        self.write("shim.py", "class os:\n    NAME = 'shim'\n")
        path = self.write("app.py", 'from shim import os\nHELP = """\nimport os\n"""\nprint(os.NAME)\n')
        self.dcap().request("Help me investigate my website", execute=False)
        self.assertIn("import os as os_2", path.read_text())
        self.assertIn("shim", self.run_py("import app").stdout)

    def test_a_module_holds_only_its_own_capability(self):
        self.write("app.py", "X = 1\n")
        self.dcap().request("Help me investigate my website", execute=False)
        generated = (self.root / "dcap_capabilities" / "web_page_discovery.py").read_text()
        self.assertEqual(generated.count("def run("), 1)
        self.assertEqual(re.findall(r"_require\('(\w+)'\)", generated), ["DISCOVER_PAGES"])


_PASTE_SCRIPT = """
import hashlib, json, sys
from pathlib import Path
from dcap import Dcap
from dcap.listener import Listener
root, source, said, extra = Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4:]
(root / "app.py").write_text(source)
listener = Listener(Dcap(root, extra))
offer = listener.observe([said], [])
paste = listener.observe(["Yes."], [])
files = {
    str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
    for p in sorted(root.rglob("*")) if p.is_file() and ".dcap" not in p.parts and "__pycache__" not in p.parts
}
print(json.dumps({"offer": offer, "paste": paste, "files": files}, sort_keys=True))
"""

_REMOTE_DEFINITION = {
    "class": "gizmo", "title": "Gizm\u00f3", "keywords": ["gizmo", "sprocket"],
    "types": {"GizmoText": {"type": "string"}},
    "cores": {
        "gizmo.second": {"name": "Second tool", "contract": 1, "output": "GizmoText"},
        "gizmo.first": {"name": "First tool", "contract": 1, "output": "GizmoText", "inputs": {"text": {"type": "GizmoText"}}},
    },
}
_REMOTE_PROVIDER = {"providers": [{
    "provider": "gizmo.mcp", "kind": "mcp", "command": ["python3", "server.py"], "text_output": True,
    "implements": {
        "gizmo.first": {"contract": 1, "operation": "a"},
        "gizmo.second": {"contract": 1, "operation": "b", "before": [{"tool": "c"}]},
    },
}]}


class Deterministic(unittest.TestCase):
    SCENARIOS = {
        "switches": ("import os\\n\\nVALUE = 1\\n", "I'll help me investigate my website."),
        "goal block": ("X = 1\\n", "I need to check site links on this host."),
        "stub": ("def inspect_site(target):\\n    pass\\n", "I need to check site links on this host."),
        "remote": ("X = 1\\n", "I'll build a gizmo and a sprocket."),
    }

    def definitions(self, tmp, reverse=False):
        root = Path(tmp) / "definitions"
        (root / "capabilities").mkdir(parents=True)
        (root / "providers").mkdir()
        definition = dict(_REMOTE_DEFINITION)
        if reverse:
            definition["cores"] = dict(reversed(list(definition["cores"].items())))
        (root / "capabilities" / "gizmo.yaml").write_text(yaml.safe_dump(definition, sort_keys=False, allow_unicode=True), encoding="utf-8")
        (root / "providers" / "gizmo.yaml").write_text(yaml.safe_dump(_REMOTE_PROVIDER))
        return root

    def paste(self, source, said, seed, name, reverse=False, env=()):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / name
            root.mkdir()
            extra = str(self.definitions(tmp, reverse))
            full = {**os.environ, "PYTHONHASHSEED": str(seed), "PYTHONPATH": str(Path(__file__).parent.parent / "src"),
                    "PYTHONDONTWRITEBYTECODE": "1", **dict(env)}
            run = subprocess.run([sys.executable, "-c", _PASTE_SCRIPT, str(root), source.replace("\\n", "\n"), said, extra],
                                 env=full, capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            return json.loads(run.stdout)

    def test_same_input_gives_the_same_bytes_every_time(self):
        for scenario, (source, said) in self.SCENARIOS.items():
            first = self.paste(source, said, 0, "one")
            self.assertTrue(first["offer"], scenario)
            self.assertTrue(first["paste"], scenario)
            for seed, name in ((1, "one"), (2, "two"), (424242, "a different folder")):
                with self.subTest(scenario=scenario, seed=seed, name=name):
                    self.assertEqual(self.paste(source, said, seed, name), first)

    def test_definition_order_does_not_change_the_paste(self):
        source, said = self.SCENARIOS["remote"]
        first = self.paste(source, said, 0, "one")
        self.assertLess(first["paste"].index("first_tool"), first["paste"].index("second_tool"))
        self.assertEqual(self.paste(source, said, 0, "one", reverse=True), first)

    def test_the_locale_does_not_change_how_definitions_are_read(self):
        source, said = self.SCENARIOS["remote"]
        first = self.paste(source, said, 0, "one")
        other = self.paste(source, said, 0, "one", env={"LC_ALL": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0"})
        self.assertIn("gizm\u00f3", first["offer"])
        self.assertEqual(other, first)


class PasteableDefinitions(unittest.TestCase):
    def load(self, cores):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "capabilities").mkdir()
            (root / "capabilities" / "t.yaml").write_text(yaml.safe_dump({
                "class": "t", "types": {"Out": {"type": "string"}},
                "cores": {core: {"name": name, "contract": 1, "output": "Out"} for core, name in cores.items()},
            }))
            return Registries([root])

    def test_names_that_cannot_be_pasted_never_load(self):
        for cores in ({"t.a": "Import"}, {"t.a": "3D View"}, {"t.a": "\u626b\u63cf"}, {"my-tools.a": "Fine"},
                      {"contract": "Fine"}, {"t.a_b": "One", "t.a.b": "Two"}, {"t.a": "Page Check", "t.b": "page-check"}):
            with self.subTest(cores=cores), self.assertRaises(RegistryError):
                self.load(cores)

    def test_a_good_definition_loads(self):
        self.assertIn("t.a", self.load({"t.a": "Page Check"}).cores)


class SwaggerExample(unittest.TestCase):
    EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "swagger"

    def test_server_path_is_found_beside_its_definition(self):
        command = Registries([self.EXAMPLE]).providers_for("api.list_endpoints")[0].command
        self.assertEqual(command[1], str(self.EXAMPLE / "server.py"))

    def test_the_example_lists_endpoints_of_a_local_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = Path(tmp) / "openapi.json"
            spec.write_text(json.dumps({"openapi": "3.0.0", "info": {"title": "Pets", "version": "1"},
                                        "paths": {"/pets": {"get": {"summary": "List pets"}, "post": {}}}}))
            impl = Registries([self.EXAMPLE]).providers_for("api.list_endpoints")[0]
            from dcap.engine import remote_spec
            found = call_remote(remote_spec(impl), {"spec_url": str(spec)}, 30)
        self.assertEqual([(e["path"], e["method"]) for e in found], [("/pets", "GET"), ("/pets", "POST")])


class TextProvider(unittest.TestCase):
    SERVER = str(Path(__file__).parent / "fixtures" / "text_mcp.py")

    def spec(self, **extra):
        return {"kind": "mcp", "command": [sys.executable, self.SERVER], "tool": "show", "input_map": {},
                "before": [{"tool": "open", "input_map": {"file": "path"}}], **extra}

    def test_declared_text_output_comes_back_as_written(self):
        result = call_remote(self.spec(text_output=True), {"file": "a.bin"}, 10)
        self.assertEqual(result, "0x1 main of a.bin\n")

    def test_a_declared_path_reaches_the_server_absolute(self):
        spec = self.spec(text_output=True)
        spec["before"][0]["paths"] = ["file"]
        self.assertEqual(call_remote(spec, {"file": "a.bin"}, 10), f"0x1 main of {os.path.abspath('a.bin')}\n")

    def test_without_the_declaration_text_is_not_guessed_at(self):
        with self.assertRaises(ValueError):
            call_remote(self.spec(), {"file": "a.bin"}, 10)


class StubFit(Base):
    STUBS = {
        "timeout": "def site_timeout(seconds):\n    pass\n",
        "init": "class SiteClient:\n    def __init__(self, target):\n        pass\n",
        "mutator": "class PageList:\n    def add(self, host):\n        pass\n",
        "extra": "def inspect_site(target, mode):\n    pass\n",
        "shutdown": "def shutdown_site(target):\n    raise NotImplementedError\n",
        "restart": "def restart_page(target):\n    pass\n",
        "crawler": "class SiteCrawler:\n    def __init__(self, target):\n        pass\n",
    }

    def test_stubs_that_do_not_fit_are_left_alone(self):
        for name, source in self.STUBS.items():
            with self.subTest(name):
                self.setUp()
                self.write("app.py", source)
                out = self.dcap().request("Check site links on 192.0.2.1")
                self.assertEqual(out["status"], "complete")
                self.assertEqual(out["integration"]["entry_point"], "app.py:check_site_links")
                self.assertIn(source, (self.root / "app.py").read_text())

    def test_special_method_cannot_be_named_as_a_target(self):
        self.write("app.py", self.STUBS["init"])
        out = self.dcap().integrate("WEB.SITE_LINKS", "SiteClient.__init__")
        self.assertEqual(out["reason"], "target_is_declaration")

    def test_required_parameter_blocks_a_named_target_too(self):
        self.write("app.py", self.STUBS["extra"])
        out = self.dcap().integrate("WEB.SITE_LINKS", "inspect_site")
        self.assertEqual(out["reason"], "no_compatible_bridge")

    def test_stub_that_fits_is_still_connected(self):
        self.write("app.py", "def crawl_site_pages(target, mode=None):\n    pass\n")
        out = self.dcap().request("Check site links on 192.0.2.1")
        self.assertEqual(out["integration"]["entry_point"], "app.py:crawl_site_pages")

    def test_later_definition_is_filled_and_earlier_code_survives(self):
        path = self.write("app.py", "def inspect_site(target):\n    return 'REAL ' + target\n\n"
                                    "def inspect_site(target):\n    pass\n")
        d = self.dcap()
        out = d.integrate("WEB.SITE_LINKS")
        self.assertEqual(out["status"], "integrated")
        self.assertIn("return 'REAL ' + target", path.read_text())
        policy = self.write("p.json", json.dumps({"grants": GRANTS}))
        run = self.run_py("import app; print(len(app.inspect_site('192.0.2.1')))", policy)
        self.assertEqual(run.stdout.strip(), "3", run.stderr)
        self.assertEqual(d.status()["integrations"][out["id"]]["problems"], [])

    def test_earlier_stub_shadowed_by_real_code_is_not_filled(self):
        source = "def inspect_site(target):\n    pass\n\ndef inspect_site(target):\n    return 'REAL'\n"
        path = self.write("app.py", source)
        self.assertEqual(self.dcap().integrate("WEB.SITE_LINKS")["reason"], "no_compatible_symbol")
        self.assertEqual(path.read_text(), source)

    def test_overloads_stay_and_the_implementation_is_filled(self):
        path = self.write("app.py", (
            "from typing import overload\n\n"
            "@overload\ndef inspect_site(target: str): ...\n"
            "@overload\ndef inspect_site(target: bytes): ...\n"
            "def inspect_site(target: str):\n    pass\n"
        ))
        out = self.dcap().integrate("WEB.SITE_LINKS")
        self.assertEqual(out["status"], "integrated")
        self.assertEqual(path.read_text().count("def inspect_site(target: str): ...\n"), 1)
        policy = self.write("p.json", json.dumps({"grants": GRANTS}))
        run = self.run_py("import app; print(len(app.inspect_site('192.0.2.1')))", policy)
        self.assertEqual(run.stdout.strip(), "3", run.stderr)

    def test_valid_stub_layouts_integrate_and_run(self):
        policy_text = json.dumps({"grants": GRANTS})
        for source, call in (
            ('def inspect_site(target):\n\t"""d"""\n', "app.inspect_site"),
            ("class A:\n\tdef inspect_site(self, target): pass\n", "app.A().inspect_site"),
            ('def inspect_site(target): """doc"""\n', "app.inspect_site"),
            ('def inspect_site(target):\n    """Doc."""', "app.inspect_site"),
        ):
            with self.subTest(source):
                self.setUp()
                self.write("app.py", source)
                d = self.dcap()
                out = d.integrate("WEB.SITE_LINKS")
                self.assertEqual(out["status"], "integrated", out)
                policy = self.write("p.json", policy_text)
                run = self.run_py(f"import app; print(len({call}('192.0.2.1')))", policy)
                self.assertEqual(run.stdout.strip(), "3", run.stderr)
                self.assertEqual(d.release(out["id"])["status"], "released")
                self.assertEqual((self.root / "app.py").read_text(), source)

    def test_edit_after_docstring_stub_at_end_of_file_is_noticed(self):
        path = self.write("app.py", 'def inspect_site(target):\n    """Doc."""')
        d = self.dcap()
        out = d.integrate("WEB.SITE_LINKS")
        path.write_text(path.read_text().replace("return _dcap_run", "print('HAND')\n    return _dcap_run"))
        self.assertTrue(d.workspace.conflicts(out["id"]))
        self.assertEqual(d.release(out["id"])["status"], "conflict")

    def test_core_and_goal_of_the_same_name_do_not_collide(self):
        self.write("app.py", "def a(pages, checked):\n    pass\n\ndef b(target):\n    pass\n")
        d = self.dcap()
        self.assertEqual(d.integrate("web.sitemap", "a")["status"], "integrated")
        self.assertEqual(d.integrate("WEB.SITEMAP", "b")["status"], "integrated")
        policy = self.write("p.json", json.dumps({"grants": GRANTS}))
        run = self.run_py("import app; print(sorted(app.b('192.0.2.1')))", policy)
        self.assertIn("pages", run.stdout, run.stderr)

    def test_targeting_the_connected_stub_again_reports_existing(self):
        self.write("app.py", "def inspect_site(target):\n    pass\n")
        d = self.dcap()
        first = d.request("Check site links on this host")["integration"]
        for target in ("app.py:inspect_site", "inspect_site"):
            again = d.request("Check site links on this host", target=target)["integration"]
            self.assertEqual((again["status"], again["id"]), ("existing", first["id"]))


class HonestStatus(Base):
    def test_goal_stops_when_integration_abstains(self):
        self.write("web.ts", "export const x = 1;\n")
        d = self.dcap()
        with mock.patch("dcap.runtime.placeholder.discover_pages") as provider:
            out = d.request("Check site links on 192.0.2.1")
        provider.assert_not_called()
        self.assertEqual(out["status"], "abstained")
        self.assertEqual(out["integration"]["reason"], "no_compatible_bridge")
        self.assertNotIn("result", out)
        self.assertEqual(d.status(out["request_id"])["status"], "abstained")

    def test_goal_stops_when_the_named_target_is_missing(self):
        self.write("app.py", "X = 1\n")
        out = self.dcap().request("Check site links on 192.0.2.1", target="nope")
        self.assertEqual(out["status"], "abstained")
        self.assertEqual(out["integration"]["reason"], "symbol_not_found")

    def test_direct_mode_exposes_nothing_when_nothing_landed(self):
        self.write("web.ts", "export const x = 1;\n")
        out = self.dcap().request("Help me investigate my website")
        self.assertEqual(out["status"], "abstained")
        self.assertEqual(out["capabilities"], {})

    def test_unowned_package_is_not_reported_as_exposed(self):
        self.write("app.py", "X = 1\n")
        self.write("dcap_capabilities/__init__.py", "# mine\n")
        out = self.dcap().request("Help me investigate my website")
        self.assertEqual(out["status"], "abstained")
        self.assertEqual(out["integrations"][0]["reason"], "workspace_conflict")

    def test_unlisted_languages_are_not_treated_as_empty(self):
        for name in ("Main.java", "app.rb", "main.c", "P.cs"):
            with self.subTest(name):
                self.setUp()
                self.write(name, "x\n")
                for text in ("Help me investigate my website", "Check site links on this host"):
                    out = self.dcap().request(text)
                    self.assertEqual(out["status"], "abstained")
                self.assertFalse((self.root / "dcap_capabilities").exists())

    def test_repeat_request_reports_removed_code(self):
        path = self.write("app.py", "X = 1\n")
        d = self.dcap()
        first = d.request("Help me investigate my website")["integrations"][0]
        path.write_text("X = 1\n")
        again = d.request("Help me investigate my website")
        self.assertEqual(again["integrations"][0]["status"], "conflict")
        self.assertEqual(again["status"], "conflict")
        self.assertEqual(again["capabilities"], {})
        self.assertEqual(path.read_text(), "X = 1\n")
        # With DCAP's code already gone, a plain release clears the record, and the AI can ask again.
        self.assertEqual(d.release(first["id"])["status"], "released")
        self.assertEqual(d.request("Help me investigate my website")["integrations"][0]["status"], "integrated")

    def test_repeat_goal_request_reports_removed_code(self):
        path = self.write("app.py", "X = 1\n")
        d = self.dcap()
        d.request("Check site links on this host")
        path.write_text("X = 1\n")
        __import__("shutil").rmtree(self.root / "dcap_capabilities")
        out = d.request("Check site links on this host")
        self.assertEqual(out["integration"]["status"], "conflict")
        self.assertNotIn("result", out)

    def test_status_keeps_a_symbol_in_a_file_that_is_mid_edit(self):
        path = self.write("app.py", "def inspect_site(target):\n    pass\n")
        d = self.dcap()
        integration = d.integrate("WEB.SITE_LINKS")["id"]
        path.write_text(path.read_text() + "\ndef broken(:\n")
        problems = d.status()["integrations"][integration]["problems"]
        self.assertFalse(any("no longer exists" in p for p in problems), problems)
        self.assertTrue(any("does not parse" in p for p in problems), problems)


class LifecycleTracking(DefinitionsHelper):
    def goal_source(self):
        return self.write("app.py", "X = 1\n")

    def test_new_provider_shows_as_outdated_and_repeat_request_moves_to_it(self):
        self.goal_source()
        d = self.dcap()
        first = d.request("Check site links on this host")
        extra = self.provider_defs({
            "provider": "new.mcp", "version": "9", "kind": "mcp", "priority": 1,
            "command": [sys.executable, str(FAKE_MCP)],
            "implements": {"web.link_discovery": {
                "contract": 1, "operation": "link_crawl", "input_map": {"pages": "targets"}, "output_key": "open",
            }},
        })
        later = self.dcap(extra=[extra])
        self.assertTrue(later.status()["integrations"][first["integration"]["id"]]["outdated"])
        out = later.request("Check site links on this host")
        self.assertEqual(out["integration"]["status"], "integrated")
        self.assertEqual({s["depth"] for s in out["result"]}, {9999})
        entry = later.workspace.manifest()["integrations"][out["integration"]["id"]]
        self.assertEqual(entry["providers"]["web.link_discovery"], "new.mcp@9")
        self.assertIn("call_remote", (self.root / "dcap_capabilities" / "web_site_links.py").read_text())

    def test_child_goal_version_shows_as_outdated(self):
        self.goal_source()
        first = self.dcap(extra=[self.defs()]).request("shine the widget on this host")
        later = self.dcap(extra=[self.defs(child_version="2")])
        self.assertTrue(later.status()["integrations"][first["integration"]["id"]]["outdated"])

    def test_core_contract_shows_as_outdated(self):
        self.goal_source()
        first = self.dcap(extra=[self.defs()]).request("shine the widget on this host")
        later = self.dcap(extra=[self.defs(contract=2)])
        self.assertTrue(later.status()["integrations"][first["integration"]["id"]]["outdated"])

    def test_unchanged_definitions_stay_current(self):
        self.goal_source()
        extra = self.defs()
        first = self.dcap(extra=[extra]).request("shine the widget on this host")
        later = self.dcap(extra=[extra])
        self.assertFalse(later.status()["integrations"][first["integration"]["id"]]["outdated"])
        self.assertEqual(later.request("shine the widget on this host")["integration"]["status"], "existing")

    def test_update_keeps_the_variable_and_switches_of_a_tools_block(self):
        path = self.write("app.py", "X = 1\n")
        self.dcap(extra=[self.defs()]).request("help widget")
        path.write_text(path.read_text().replace('["POLISH_WIDGET"] = "false"', '["POLISH_WIDGET"] = "true"')
                        + "\ndef go():\n    return polish_widget('192.0.2.1')\n")
        out = self.dcap(extra=[self.defs(contract=2)]).request("help widget")
        self.assertEqual(out["integrations"][0]["status"], "integrated")
        self.assertEqual(out["integrations"][0]["entry_point"], "app.py")
        self.assertIn('["POLISH_WIDGET"] = "true"', path.read_text())
        run = self.run_py("import app; print(app.go())")
        self.assertIn("192.0.2.1", run.stdout, run.stderr)

    def test_update_keeps_the_name_of_a_goal_block(self):
        path = self.write("app.py", "X = 1\n")
        self.dcap(extra=[self.defs()]).request("shine the widget on this host")
        path.write_text(path.read_text() + "\ndef go():\n    return shine_the_widget('192.0.2.1')\n")
        out = self.dcap(extra=[self.defs(goal_version="2")]).request("shine the widget on this host")
        self.assertEqual(out["integration"]["entry_point"], "app.py:shine_the_widget")
        run = self.run_py("import app; print(app.go())")
        self.assertIn("192.0.2.1", run.stdout, run.stderr)

    def test_update_stays_in_the_same_file(self):
        first = self.write("first.py", "A = 1\n")
        os.utime(first, (5000000000, 5000000000))
        self.write("second.py", "B = 1\n")
        self.dcap(extra=[self.defs()]).request("help widget")
        self.assertIn("dcap:begin", first.read_text())
        os.utime(first, (1, 1))
        out = self.dcap(extra=[self.defs(contract=2)]).request("help widget")
        self.assertEqual(out["integrations"][0]["entry_point"], "first.py")

    def test_update_refuses_rather_than_lose_a_block_sharing_a_module(self):
        path = self.write("app.py", "def polish_widget(target):\n    pass\n")
        d = self.dcap(extra=[self.defs()])
        block = d.request("help widget")["integrations"][0]["id"]
        d.integrate("widget.polish", "polish_widget")
        path.write_text(path.read_text().replace('["POLISH_WIDGET"] = "false"', '["POLISH_WIDGET"] = "true"', 1))
        before = path.read_text()
        out = self.dcap(extra=[self.defs(contract=2)]).update(block)
        self.assertEqual(out["reason"], "shared_file", out)
        self.assertEqual(path.read_text(), before)
        self.assertIn(block, self.dcap(extra=[self.defs()]).workspace.manifest()["integrations"])

    def test_update_keeps_a_renamed_tool_the_code_calls(self):
        path = self.write("app.py", "def polish_widget():\n    return 1\n")
        d = self.dcap(extra=[self.defs()])
        out = d.request("help widget")["integrations"][0]
        self.assertEqual(out["usage"], {"polish_widget_2": "Polish widget"})
        text = path.read_text().replace("def polish_widget():\n    return 1\n", "def go():\n    return polish_widget_2('192.0.2.1')\n")
        path.write_text(text.replace('["POLISH_WIDGET"] = "false"', '["POLISH_WIDGET"] = "true"'))
        updated = self.dcap(extra=[self.defs(contract=2)]).update(out["id"])
        self.assertEqual(updated["status"], "integrated", updated)
        self.assertIn("run as polish_widget_2\n", path.read_text())
        self.assertIn("192.0.2.1", self.run_py("import app; print(app.go())").stdout)

    def test_release_then_request_keeps_the_name_the_code_uses(self):
        path = self.write("app.py", "import os\n")
        d = self.dcap()
        integration = d.request("Help me investigate my website")["integrations"][0]["id"]
        path.write_text(path.read_text() + "\ndef go():\n    return discover_pages\n")
        self.assertEqual(d.release(integration)["status"], "released")
        d.request("Help me investigate my website")
        self.assertIn("import run as discover_pages\n", path.read_text())

    def test_a_star_import_may_provide_the_name(self):
        path = self.write("app.py", "from os.path import *\n\nprint(discover_pages)\n")
        self.dcap().request("Help me investigate my website")
        self.assertIn("import run as discover_pages_2\n", path.read_text())

    def test_tools_update_that_cannot_land_changes_nothing(self):
        path = self.write("app.py", "X = 1\n")
        unavailable = {"provider": "w.mcp", "kind": "mcp", "command": ["/nonexistent/tool"],
                       "implements": {"widget.polish": {"contract": 2, "operation": "x"}}}
        d = self.dcap(extra=[self.defs()])
        integration = d.request("help widget")["integrations"][0]["id"]
        text = path.read_text()
        defs = self.defs(contract=2)
        (defs / "providers" / "widget.yaml").write_text(yaml.safe_dump({"providers": [unavailable]}))
        out = self.dcap(extra=[defs]).request("help widget")
        self.assertEqual(out["integrations"][0]["reason"], "no_injectable_provider")
        self.assertEqual(path.read_text(), text)
        self.assertIn(integration, self.dcap(extra=[defs]).workspace.manifest()["integrations"])
        self.assertTrue((self.root / "dcap_capabilities").exists())


class SharedFiles(DefinitionsHelper):
    SOURCE = '"""App."""\nimport os\n\nVALUE = os.sep\n'

    def test_blocks_never_nest(self):
        path = self.write("app.py", self.SOURCE)
        d = self.dcap()
        first = d.request("Help me investigate my website")["integrations"][0]["id"]
        second = d.request("Check site links on this host")["integration"]["id"]
        self.assertEqual(d.workspace.conflicts(first), [])
        self.assertEqual(d.workspace.conflicts(second), [])
        lines = [line.strip() for line in path.read_text().splitlines()]
        self.assertLess(lines.index(f"# dcap:end {first}"), lines.index(f"# dcap:begin {second}"))
        self.assertEqual(d.release(first)["status"], "released")
        self.assertEqual(d.workspace.conflicts(second), [])
        self.assertEqual(d.release(second)["status"], "released")
        self.assertEqual(path.read_text(), self.SOURCE)

    def test_goal_first_then_tools_and_two_classes_do_not_nest(self):
        path = self.write("app.py", "import os\nX=1\n")
        d = self.dcap()
        d.request("Check site links on this host")
        d.request("Help me investigate my website")
        d.request("Help me with my website report")
        for integration, entry in d.status()["integrations"].items():
            self.assertEqual(entry["problems"], [], integration)
        compile(path.read_text(), "app.py", "exec")

    def test_block_does_not_shadow_a_class_the_file_imports(self):
        (self.root / "mylib.py").write_text("class Site:\n    def __init__(self, name):\n        self.name = name\n")
        path = self.write("app.py", "from mylib import Site\n\nn = Site('eth0')\nprint(n.name)\n")
        out = self.dcap().request("Help me investigate my website")
        self.assertEqual(out["integrations"][0]["status"], "integrated")
        run = self.run_py("import app; print(type(app.discover_pages).__name__)")
        self.assertEqual(run.stdout.split(), ["eth0", "function"], run.stderr)

    def test_form_feeds_and_separators_do_not_shift_a_splice(self):
        source = ('def helper():\n    # page\x0c one\x0c two\n    value = 40 + 2\n    return value\n'
                  'def inspect_site(target):\n    pass\n')
        path = self.root / "app.py"
        path.write_bytes(source.encode())
        out = self.dcap().integrate("WEB.SITE_LINKS")
        self.assertEqual(out["status"], "integrated")
        text = path.read_bytes().decode()
        self.assertIn("return value\ndef inspect_site", text)
        self.assertNotIn("    pass\n", text)
        policy = self.write("p.json", json.dumps({"grants": GRANTS}))
        run = self.run_py("import app; print(app.helper(), len(app.inspect_site('192.0.2.1')))", policy)
        self.assertEqual(run.stdout.strip(), "42 3", run.stderr)

    def test_form_feed_page_break_before_a_stub(self):
        path = self.root / "app.py"
        path.write_bytes(b"import os\n\n\x0c\ndef inspect_site(target):\n    pass\n")
        d = self.dcap()
        out = d.integrate("WEB.SITE_LINKS")
        self.assertEqual(out["status"], "integrated")
        self.assertEqual(self.run_py("import app").returncode, 0)
        path.write_bytes(path.read_bytes() + b"\nX = 1\n")
        self.assertEqual(d.release(out["id"])["status"], "released")
        self.assertIn(b"def inspect_site(target):\n    pass\n", path.read_bytes())

    def test_separators_in_strings_do_not_move_a_block(self):
        for text in ('"""doc\x0cmore"""\nX = 1\n', '"""a\u2028b"""\nX = 1\n', '# c\u2028d\nX = 1\n',
                     '"""a\x0cb"""\nfrom __future__ import annotations\n'):
            with self.subTest(text):
                self.setUp()
                path = self.root / "app.py"
                path.write_bytes(text.encode())
                out = self.dcap().request("Help me investigate my website")
                self.assertEqual(out["integrations"][0]["status"], "integrated")
                run = self.run_py("import app; print(type(app.discover_pages).__name__)")
                self.assertEqual(run.stdout.strip(), "function", run.stderr)

    def test_block_stays_out_of_a_string_after_a_semicolon(self):
        path = self.write("app.py", 'import os; S = """\nabc\n"""\nprint(len(S))\n')
        out = self.dcap().request("Help me investigate my website")
        self.assertEqual(out["integrations"][0]["status"], "integrated")
        run = self.run_py("import app; print(type(app.discover_pages).__name__)")
        self.assertEqual(run.stdout.split(), ["5", "function"], run.stderr)
        self.assertIn('abc\n"""\n# dcap:begin', path.read_text())

    def test_block_on_a_shared_import_line_keeps_the_statement_first(self):
        self.write("app.py", "import os; X = 1\n")
        self.dcap().request("Help me investigate my website")
        self.assertEqual(self.run_py("import app; print(app.X, type(app.discover_pages).__name__)").stdout.split(), ["1", "function"])

    def test_encoding_cookie_stays_in_the_first_two_lines(self):
        for first_line in (b"\n", b"\x0c\n"):
            with self.subTest(first_line):
                self.setUp()
                source = first_line + b"# -*- coding: latin-1 -*-\nS = '\xc3\xa9'\nprint(len(S))\n"
                path = self.root / "app.py"
                path.write_bytes(source)
                out = self.dcap().request("Help me investigate my website")
                self.assertEqual(out["integrations"][0]["status"], "integrated")
                self.assertEqual(self.run_py("import app").stdout.strip(), "2")

    def test_mode_is_kept_through_integrate_and_release(self):
        for mode in (0o755, 0o600):
            with self.subTest(oct(mode)):
                self.setUp()
                path = self.write("tool.py", "#!/usr/bin/env python3\nX = 1\n")
                path.chmod(mode)
                d = self.dcap()
                integration = d.request("Help me investigate my website")["integrations"][0]["id"]
                self.assertEqual(path.stat().st_mode & 0o777, mode)
                manifest = d.workspace.manifest()["integrations"][integration]
                self.assertEqual((self.root / manifest["edit"]["backup"]).stat().st_mode & 0o777, mode)
                self.assertEqual(d.release(integration)["status"], "released")
                self.assertEqual(path.stat().st_mode & 0o777, mode)

    def test_script_still_runs_after_integration(self):
        path = self.write("run.py", "#!/usr/bin/env python3\nprint('hello')\n")
        path.chmod(0o755)
        self.dcap().request("Help me investigate my website")
        env = {**os.environ, "DCAP_POLICY": str(self.root / "none.json")}
        self.assertEqual(subprocess.run([str(path)], cwd=self.root, env=env, capture_output=True, text=True).stdout.strip(), "hello")

    def test_hard_link_survives(self):
        path = self.write("app.py", "X = 1\n")
        other = self.root.parent / (self.root.name + "-link.py")
        os.link(path, other)
        self.addCleanup(other.unlink)
        self.dcap().request("Help me investigate my website")
        self.assertEqual(path.stat().st_ino, other.stat().st_ino)
        self.assertIn("dcap:begin", other.read_text())


class ImportsResolve(Base):
    def test_loose_folder_works_imported_and_as_a_script(self):
        self.write("main.py", "from services.worker import VALUE\nprint(VALUE)\n")
        self.write("services/worker.py", "VALUE = 1\n")
        os.utime(self.root / "main.py", (1, 1))
        out = self.dcap().request("Help me investigate my website")
        self.assertEqual(out["integrations"][0]["entry_point"], "services/worker.py")
        self.assertEqual(self.run_py("import main").stdout.strip(), "1")
        script = subprocess.run([sys.executable, "services/worker.py"], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(script.returncode, 0, script.stderr)
        module = subprocess.run([sys.executable, "-m", "services.worker"], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(module.returncode, 0, module.stderr)

    def test_loose_folder_goal_block_and_stub(self):
        self.write("tools/run.py", "def inspect_site(target):\n    pass\n")
        d = self.dcap()
        out = d.integrate("WEB.SITE_LINKS")
        self.assertEqual(out["status"], "integrated")
        policy = self.write("p.json", json.dumps({"grants": GRANTS}))
        run = self.run_py("import tools.run as r; print(len(r.inspect_site('192.0.2.1')))", policy)
        self.assertEqual(run.stdout.strip(), "3", run.stderr)
        self.assertEqual(d.release(out["id"])["status"], "released")
        self.assertFalse((self.root / "tools" / "dcap_capabilities").exists())

    def test_subfolder_of_a_package(self):
        self.write("src/mypkg/__init__.py", "")
        self.write("src/mypkg/sub/mod.py", "X = 1\n")
        self.dcap().request("Help me investigate my website")
        env = {**os.environ, "PYTHONPATH": str(self.root / "src")}
        run = subprocess.run([sys.executable, "-c", "import mypkg.sub.mod as m; print(type(m.discover_pages).__name__)"],
                             cwd=self.root, env=env, capture_output=True, text=True)
        self.assertEqual(run.stdout.strip(), "function", run.stderr)

    def test_script_inside_a_package(self):
        self.write("pkg/__init__.py", "")
        self.write("pkg/cli.py", "print('ok')\nif __name__ == '__main__':\n    pass\n")
        self.dcap().request("Help me investigate my website")
        for command in ([sys.executable, "pkg/cli.py"], [sys.executable, "-m", "pkg.cli"]):
            run = subprocess.run(command, cwd=self.root, capture_output=True, text=True)
            self.assertEqual(run.stdout.strip(), "ok", run.stderr)
        self.assertEqual(self.run_py("import pkg.cli").returncode, 0)

    def test_dotted_folder_name_is_not_a_conflict(self):
        self.write("v1.2/run.py", "X = 1\n")
        d = self.dcap()
        out = d.request("Help me investigate my website")
        self.assertEqual(out["integrations"][0]["status"], "integrated", out)
        run = subprocess.run([sys.executable, "v1.2/run.py"], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)

    def test_validation_failure_does_not_show_a_traceback(self):
        d = self.dcap()
        with self.assertRaises(Exception) as caught:
            d.workspace._validate({"dcap_capabilities/broken.py": "import does_not_exist_anywhere\n"}, None)
        self.assertNotIn("Traceback", str(caught.exception))

    def test_environments_of_any_name_are_not_project_code(self):
        app = self.write("app.py", "X = 1\n")
        os.utime(app, (1, 1))
        for folder in ("env/lib/python3.14/site-packages/requests", "myenv/Lib/site-packages/thirdparty",
                       ".tox/py/lib/python3.14/site-packages/x"):
            self.write(f"{folder}/__init__.py", "Y = 1\n")
        self.write("env/pyvenv.cfg", "home = /usr\n")
        self.write("myenv/pyvenv.cfg", "home = /usr\n")
        out = self.dcap().request("Help me investigate my website")
        self.assertEqual(out["integrations"][0]["entry_point"], "app.py")
        self.assertEqual(self.dcap().observer.recent_python(), ["app.py"])
        self.assertNotIn("dcap", (self.root / "env/lib/python3.14/site-packages/requests/__init__.py").read_text())

    def test_environment_by_marker_with_no_site_packages_folder(self):
        app = self.write("app.py", "X = 1\n")
        os.utime(app, (1, 1))
        self.write("anything/pyvenv.cfg", "home = /usr\n")
        self.write("anything/lib/thing.py", "Y = 1\n")
        self.assertEqual(self.dcap().observer.recent_python(), ["app.py"])


class SelectedProvider(DefinitionsHelper):
    def native_defs(self, vendor=None):
        extra = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(extra, ignore_errors=True))
        (extra / "providers").mkdir()
        (extra / "thirdmod.py").write_text("def pages(target):\n    return [{'address': 'third-' + target}]\n")
        spec = {
            "provider": "third.native", "version": "2", "kind": "native", "priority": 1,
            "implements": {"web.page_discovery": {"contract": 1, "operation": "thirdmod:pages"}},
        }
        if vendor:
            spec["vendor"] = vendor
        (extra / "providers" / "third.yaml").write_text(yaml.safe_dump({"providers": [spec]}))
        sys.path.insert(0, str(extra))
        self.addCleanup(sys.path.remove, str(extra))
        self.addCleanup(sys.modules.pop, "thirdmod", None)
        __import__("importlib").invalidate_caches()
        return extra

    def test_engine_and_integrated_code_use_the_same_provider(self):
        extra = self.native_defs()
        self.write("app.py", "X = 1\n")
        d = self.dcap(extra=[extra])
        out = d.request("Map pages on 192.0.2.1")
        self.assertEqual(out["result"], [{"address": "third-192.0.2.1"}])
        entry = d.workspace.manifest()["integrations"][out["integration"]["id"]]
        self.assertEqual(entry["providers"]["web.page_discovery"], "third.native@2")
        policy = self.write("p.json", json.dumps({"grants": GRANTS}))
        env = {**os.environ, "DCAP_POLICY": str(policy), "PYTHONPATH": str(extra)}
        app = self.root / "app.py"
        app.write_text(app.read_text().replace('"false"', '"true"'))
        run = subprocess.run([sys.executable, "-c", "import app; print(app.map_pages('192.0.2.1'))"],
                             cwd=self.root, env=env, capture_output=True, text=True)
        self.assertIn("third-192.0.2.1", run.stdout, run.stderr)

    def test_vendored_third_party_file_is_copied_from_its_own_folder(self):
        extra = self.native_defs(vendor=["thirdmod.py"])
        self.write("app.py", "X = 1\n")
        out = self.dcap(extra=[extra]).request("Map pages on 192.0.2.1")
        self.assertEqual(out["integration"]["status"], "integrated", out)
        self.assertIn("third-", (self.root / "dcap_capabilities" / "thirdmod.py").read_text())

    def test_vendor_file_that_cannot_be_found_abstains(self):
        extra = self.native_defs(vendor=["other.py"])
        self.write("app.py", "X = 1\n")
        out = self.dcap(extra=[extra]).request("Map pages on 192.0.2.1")
        self.assertEqual(out["integration"]["reason"], "no_injectable_provider")
        self.assertEqual((self.root / "app.py").read_text(), "X = 1\n")

    def test_one_unserved_core_does_not_block_its_class(self):
        cores = {
            "gizmo.polish": {"name": "Polish gizmo", "contract": 1, "inputs": {}, "output": "PageList"},
            "gizmo.paint": {"name": "Paint gizmo", "contract": 1, "inputs": {}, "output": "PageList"},
        }
        defs = self.defs(cores=cores)
        (defs / "goals" / "widget.yaml").unlink()
        (defs / "capabilities" / "widget.yaml").write_text(yaml.safe_dump({
            "class": "gizmo", "title": "Gizmo", "keywords": ["gizmo"], "cores": cores,
        }))
        (defs / "providers" / "gizmo.yaml").write_text(yaml.safe_dump({"providers": [
            {"provider": "g.http", "kind": "http", "url": "http://127.0.0.1:9",
             "implements": {"gizmo.polish": {"contract": 1, "operation": "/x"}}},
            {"provider": "g.mcp", "kind": "mcp", "command": ["/nonexistent/tool"],
             "implements": {"gizmo.paint": {"contract": 1, "operation": "x"}}},
        ]}))
        path = self.write("app.py", "X = 1\n")
        d = self.dcap(extra=[defs])
        out = d.request("help me with my gizmo")
        self.assertEqual(out["status"], "exposed")
        self.assertEqual(out["capabilities"], {"gizmo": ["Polish gizmo"]})
        self.assertEqual(out["integrations"][0]["unavailable"], ["Paint gizmo"])
        self.assertIn('["POLISH_GIZMO"] = "false"', path.read_text())
        self.assertNotIn("PAINT_GIZMO", path.read_text())
        self.assertEqual(d.request("help me with my gizmo")["integrations"][0]["status"], "existing")

    def test_class_with_no_served_core_abstains(self):
        cores = {"gizmo.paint": {"name": "Paint gizmo", "contract": 1, "inputs": {}, "output": "PageList"}}
        defs = self.defs(cores=cores)
        (defs / "goals" / "widget.yaml").unlink()
        (defs / "capabilities" / "widget.yaml").write_text(yaml.safe_dump({
            "class": "gizmo", "title": "Gizmo", "keywords": ["gizmo"], "cores": cores,
        }))
        self.write("app.py", "X = 1\n")
        out = self.dcap(extra=[defs]).request("help me with my gizmo")
        self.assertEqual(out["status"], "abstained")
        self.assertEqual(out["integrations"][0]["reason"], "no_injectable_provider")


class BridgeDefinitions(Base):
    def load(self, **fields):
        extra = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(extra, ignore_errors=True))
        (extra / "bridges").mkdir()
        spec = {"bridge": "x.y", "language": "python", "match": {"kind": "function"}, "score": 2.0, **fields}
        (extra / "bridges" / "b.yaml").write_text(yaml.safe_dump({"bridges": [spec]}))
        return Registries([extra])

    def test_bad_bridges_never_load(self):
        for template, score in (
            ("pass", 2.0),
            ('x = "{{modul}}"', 2.0),
            ("from {{module}} import run as _dcap_run\nreturn _dcap_runn({{args}})", 2.0),
            ("from {{module}} import run as _dcap_run\nreturn _dcap_run()", 2.0),
            ("from {{module}} import run as _dcap_run\nreturn _dcap_run({{args}})", "high"),
            ("from {{module}} import run as _dcap_run\nreturn (", 2.0),
            ("from elsewhere import run as _dcap_run\n_ = '{{module}}'\nreturn _dcap_run({{args}})", 2.0),
        ):
            with self.subTest(template):
                with self.assertRaises(RegistryError):
                    self.load(template=template, score=score)

    def test_unknown_kind_is_rejected(self):
        with self.assertRaisesRegex(RegistryError, "unknown kind"):
            self.load(match={"kind": "macro"}, template="x")

    def test_good_operator_bridge_loads(self):
        self.load(template="import {{os}}\n{{switches}}\nfrom {{module}} import run as _dcap_run\nreturn _dcap_run({{args}})")


class ConcurrentProcesses(Base):
    SOURCE = "X = 1\n"

    def test_second_start_during_a_transaction_leaves_it_alone(self):
        path = self.write("app.py", self.SOURCE)
        d = self.dcap()
        real = Workspace._write
        seen = []

        def hook(ws, target, content):
            real(ws, target, content)
            if target.name == "app.py":
                seen.append(ws.root)
                self.dcap()  # another process starting now must not treat this as a crash

        with mock.patch.object(Workspace, "_write", hook):
            out = d.request("Help me investigate my website")
        self.assertTrue(seen)
        self.assertEqual(out["integrations"][0]["status"], "integrated")
        self.assertIn("dcap:begin", path.read_text())
        self.assertEqual(self.dcap().workspace.conflicts(out["integrations"][0]["id"]), [])
        self.assertFalse((self.root / ".dcap" / "journal.json").exists())

    def test_a_journal_from_a_dead_process_is_still_recovered(self):
        path = self.write("app.py", self.SOURCE)
        self.crash_during_request()
        self.assertTrue((self.root / ".dcap" / "journal.json").exists())
        Workspace(self.root)
        self.assertEqual(path.read_text(), self.SOURCE)
        self.assertFalse((self.root / "dcap_capabilities").exists())
        self.assertFalse((self.root / ".dcap" / "journal.json").exists())

    def crash_during_request(self):
        code = (
            "import os, sys\n"
            f"sys.path.insert(0, {str(SRC)!r})\n"
            "from dcap import Dcap\n"
            "from dcap.workspace import Workspace\n"
            "real = Workspace._write\n"
            "def hook(ws, path, content):\n"
            "    if path.name == 'manifest.json':\n"
            "        os._exit(9)\n"
            "    real(ws, path, content)\n"
            "Workspace._write = hook\n"
            "Dcap('.', grants=[]).request('Help me investigate my website')\n"
        )
        self.assertEqual(self.run_py(code).returncode, 9)

    def test_crash_recovery_keeps_edits_saved_after_the_crash(self):
        path = self.write("app.py", self.SOURCE)
        self.crash_during_request()
        path.write_text(path.read_text() + "\ndef ai_work_after_crash():\n    return 1\n")
        made = self.write("dcap_capabilities/mine.py", "# mine\n")
        generated = self.root / "dcap_capabilities" / "web_page_discovery.py"
        generated.write_text("# rewritten after the crash\n")
        d = self.dcap()
        text = path.read_text()
        self.assertIn("def ai_work_after_crash", text)
        self.assertNotIn("dcap:begin", text)
        self.assertEqual(generated.read_text(), "# rewritten after the crash\n")
        self.assertEqual(made.read_text(), "# mine\n")
        self.assertFalse((self.root / ".dcap" / "journal.json").exists())
        self.assertEqual(d.status()["recovered"], ["app.py"])

    def test_edit_saved_during_validation_is_not_overwritten(self):
        path = self.write("app.py", self.SOURCE)
        d = self.dcap()
        real = Workspace._validate

        def hook(ws, files, edit):
            real(ws, files, edit)
            with path.open("a") as handle:
                handle.write("def saved_during_validate():\n    return 1\n")

        with mock.patch.object(Workspace, "_validate", hook):
            out = d.request("Help me investigate my website")
        self.assertEqual(out["integrations"][0]["reason"], "workspace_conflict")
        self.assertIn("def saved_during_validate", path.read_text())
        self.assertNotIn("dcap:begin", path.read_text())

    def test_overlapping_integration_is_refused_not_lost(self):
        self.write("a.py", "A = 1\n")
        other = self.write("b.py", "def inspect_site(target):\n    pass\n")
        first, second = self.dcap(), self.dcap()
        real = Workspace._validate
        results = []

        def hook(ws, files, edit):
            real(ws, files, edit)
            if ws is first.workspace and not results:
                results.append(second.integrate("WEB.PAGE_MAP", "b.py:inspect_site"))

        with mock.patch.object(Workspace, "_validate", hook), mock.patch("dcap.workspace.LOCK_TIMEOUT", 0.05):
            out = first.request("Help me investigate my website")
        self.assertEqual(results[0]["reason"], "workspace_conflict")
        self.assertIn("another DCAP process", results[0]["detail"])
        self.assertEqual(out["integrations"][0]["status"], "integrated")
        self.assertEqual(list(self.dcap().workspace.manifest()["integrations"]), [out["integrations"][0]["id"]])
        self.assertIn("    pass\n", other.read_text())
        # With the first finished, the second can go ahead.
        self.assertEqual(second.integrate("WEB.PAGE_MAP", "b.py:inspect_site")["status"], "integrated")
        self.assertEqual(len(self.dcap().workspace.manifest()["integrations"]), 2)

    def test_processes_racing_on_one_workspace_keep_everything_they_report(self):
        self.write("app.py", self.SOURCE)
        env = {**os.environ, "PYTHONPATH": str(SRC), "DCAP_POLICY": str(self.root.parent / "none.json")}
        base = [*OPERATOR_CLI, "--workspace", str(self.root)]
        requests = ["Help me investigate my website", "Write a site report", "Map pages on this host",
                    "Review links on this host"]
        procs = [subprocess.Popen([*base, "request", text, "--no-execute"], env=env, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True) for text in requests]
        procs += [subprocess.Popen([*base, "status"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                  for _ in range(4)]
        outputs = [p.communicate() + (p.returncode,) for p in procs]
        claimed = []
        for stdout, stderr, code in outputs:
            self.assertEqual(code, 0, stderr)
            data = json.loads(stdout)
            for integration in [data.get("integration"), *data.get("integrations", [])] if "mode" in data else []:
                if integration and integration["status"] == "integrated":
                    claimed.append(integration["id"])
        manifest = self.dcap().workspace.manifest()["integrations"]
        self.assertTrue(claimed)
        self.assertEqual(sorted(set(claimed)), sorted(set(claimed) & set(manifest)))
        text = (self.root / "app.py").read_text()
        for integration_id in manifest:
            self.assertIn(f"# dcap:begin {integration_id}", text)


class ReleaseSafety(Base):
    SOURCE = "def inspect_site(target):\n    pass\n"

    def test_mid_edit_file_gives_a_conflict(self):
        path = self.write("app.py", self.SOURCE)
        d = self.dcap()
        integration = d.integrate("WEB.SITE_LINKS")["id"]
        path.write_text(path.read_text() + "\ndef broken(:\n")
        for force in (False, True):
            out = d.release(integration, force=force)
            self.assertEqual(out["status"], "conflict")
            self.assertIn("does not parse", out["detail"])
        self.assertIn(integration, d.workspace.manifest()["integrations"])
        self.assertNotEqual(d.update(integration)["status"], "integrated")
        self.assertIn(integration, d.workspace.manifest()["integrations"])
        path.write_text(path.read_text().replace("\ndef broken(:\n", "\n"))
        self.assertEqual(d.release(integration)["status"], "released")

    def test_damaged_marker_keeps_everything_even_when_forced(self):
        path = self.write("app.py", self.SOURCE)
        d = self.dcap()
        integration = d.integrate("WEB.SITE_LINKS")["id"]
        path.write_text(path.read_text().replace(f"    # dcap:end {integration}\n", ""))
        self.assertEqual(d.release(integration)["status"], "conflict")
        out = d.release(integration, force=True)
        self.assertEqual(out["status"], "conflict")
        self.assertIn("damaged", out["detail"])
        self.assertTrue((self.root / "dcap_capabilities").exists())
        self.assertIn(integration, d.workspace.manifest()["integrations"])

    def test_unreadable_files_are_problems_not_crashes(self):
        path = self.write("app.py", "X = 1\n")
        d = self.dcap()
        integration = d.request("Help me investigate my website")["integrations"][0]["id"]
        original = path.read_bytes()
        path.write_bytes(original + b"# caf\xe9\n")
        problems = d.status()["integrations"][integration]["problems"]
        self.assertTrue(any("cannot be read" in p for p in problems), problems)
        self.assertEqual(d.release(integration)["status"], "conflict")
        self.assertEqual(d.release(integration, force=True)["status"], "conflict")
        self.assertEqual(d.update(integration)["status"], "conflict")
        self.assertEqual(path.read_bytes(), original + b"# caf\xe9\n")

    def test_directory_in_place_of_the_file_is_a_problem_not_a_crash(self):
        path = self.write("app.py", "X = 1\n")
        d = self.dcap()
        integration = d.request("Help me investigate my website")["integrations"][0]["id"]
        path.unlink()
        path.mkdir()
        self.assertTrue(d.status()["integrations"][integration]["problems"])
        self.assertEqual(d.release(integration, force=True)["status"], "released")

    def test_failed_rollback_does_not_block_later_starts(self):
        self.write("app.py", "X = 1\n")
        self.write("dcap_capabilities", "a plain file\n")
        d = self.dcap()
        out = d.request("Help me investigate my website")
        self.assertEqual(out["integrations"][0]["reason"], "workspace_write_failed")
        self.assertFalse((self.root / ".dcap" / "journal.json").exists())
        self.assertEqual((self.root / "app.py").read_text(), "X = 1\n")
        self.dcap()


class BadInput(Base):
    def test_malformed_project_files_do_not_stop_dcap(self):
        for rel, text in (("package.json", "[]"), ("tests/fixtures/package.json", "[1, 2]"),
                          ("package.json", '{"dependencies": null}'), ("package.json", '{"dependencies": [1]}'),
                          ("pyproject.toml", "project = 1\n"), ("pyproject.toml", "[project]\ndependencies = [1]\n")):
            with self.subTest(f"{rel} {text}"):
                self.setUp()
                self.write(rel, text)
                self.write("app.py", "X = 1\n")
                self.assertEqual(self.dcap().status()["project"]["dependencies"], [])

    def test_very_long_expression_does_not_stop_dcap(self):
        self.write("data.py", "x=" + "1+" * 200000 + "1\n")
        good = self.write("good.py", "def inspect_site(target):\n    pass\n")
        d = self.dcap()
        self.assertEqual(d.integrate("WEB.SITE_LINKS")["entry_point"], "good.py:inspect_site")
        self.assertIn("dcap:begin", good.read_text())
        self.assertEqual(d.request("Help me investigate my website")["integrations"][0]["status"], "integrated")

    def test_missing_workspace_is_reported_not_created(self):
        missing = self.root / "typo" / "dir"
        with self.assertRaises(StateError):
            Dcap(missing, grants=GRANTS)
        self.assertFalse((self.root / "typo").exists())
        self.assertEqual(self.cli("--workspace", str(missing), "request", "Help me investigate my website")[0], 1)
        self.assertFalse((self.root / "typo").exists())

    def cli(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(list(args))
        return code, stdout.getvalue(), stderr.getvalue()

    def test_workspace_that_is_a_file_is_reported(self):
        target = self.write("wsfile", "x")
        code, _, err = self.cli("--workspace", str(target), "request", "Help me investigate my website")
        self.assertEqual(code, 1)
        self.assertIn("error:", err)

    def test_corrupt_state_gives_an_error_line(self):
        out = self.dcap().request("Check site links on this host")
        manifest = self.root / ".dcap" / "manifest.json"
        result = self.root / ".dcap" / "results" / f"{out['request_id']}.json"
        for bad in ("{bad", "[]"):
            manifest.write_text(bad)
            for args in (["status"], ["request", "Help me investigate my website"]):
                with self.subTest(f"{bad} {args}"):
                    code, _, err = self.cli("--workspace", str(self.root), *args)
                    self.assertEqual(code, 1)
                    self.assertIn("corrupt", err)
        manifest.unlink()
        result.write_text("{bad")
        code, _, err = self.cli("--workspace", str(self.root), "results", out["request_id"])
        self.assertEqual(code, 1)
        self.assertIn("corrupt", err)

    def test_state_folder_blocked_by_a_file_abstains(self):
        self.write("app.py", "X = 1\n")
        self.write(".dcap", "a file\n")
        code, stdout, _ = self.cli("--workspace", str(self.root), "request", "Help me investigate my website")
        self.assertEqual(code, 0)
        out = json.loads(stdout)
        self.assertEqual(out["status"], "abstained")
        self.assertEqual(out["warnings"][0]["code"], "RESULT_NOT_STORED")

    def test_changes_made_are_reported_when_the_result_cannot_be_stored(self):
        self.write("app.py", "X = 1\n")
        self.write(".dcap/results", "a file\n")
        out = self.dcap().request("Check site links on this host")
        self.assertEqual(out["status"], "complete")
        self.assertEqual(out["integration"]["status"], "integrated")
        self.assertEqual(out["warnings"][0]["code"], "RESULT_NOT_STORED")

    def defs_with(self, rel, text):
        extra = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(extra, ignore_errors=True))
        path = extra / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return extra

    def test_bad_definitions_are_registry_errors(self):
        for rel, text in (("goals/e.yaml", ""), ("capabilities/b.yaml", "class: [unclosed"),
                          ("goals/l.yaml", "- 1\n"), ("goals/w.yaml", "goals: [1]\n"), ("vocabulary.yaml", "- a\n")):
            with self.subTest(f"{rel} {text}"):
                extra = self.defs_with(rel, text)
                with self.assertRaises(RegistryError) as caught:
                    Registries([extra])
                self.assertIn(str(extra), str(caught.exception))
                code, _, err = self.cli("--definitions", str(extra), "validate")
                self.assertEqual(code, 1)
                self.assertIn("error:", err)


    def test_malformed_tool_lists_are_clean_errors(self):
        server = self.write("list.py", "import sys\nfor _ in sys.stdin:\n    print('[1]', flush=True)\n")
        with self.assertRaises(RemoteError):
            list_tools([sys.executable, str(server)])
        nameless = self.write("nameless.py", (
            "import json, sys\n"
            "for line in sys.stdin:\n"
            "    m = json.loads(line)\n"
            "    if 'id' in m:\n"
            "        r = {'tools': [{'description': 'x'}]} if m['method'] == 'tools/list' else {}\n"
            "        print(json.dumps({'id': m['id'], 'result': r}), flush=True)\n"
        ))
        for command in (["context", "--"], ["scaffold-provider", "x", "--"]):
            code, _, err = self.cli(*command, sys.executable, str(nameless))
            self.assertEqual(code, 1)
            self.assertIn("malformed", err)



if __name__ == "__main__":
    unittest.main()
