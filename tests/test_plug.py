"""Plugging in an MCP server: its tools become plainly named switches, wired and ready."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from dcap import Dcap, guard, plug  # noqa: E402
from dcap.cli import main  # noqa: E402
from dcap.errors import StateError  # noqa: E402
from dcap.listener import Listener  # noqa: E402
from dcap.registry import Registries  # noqa: E402
from dcap.runtime.policy import covers  # noqa: E402

KIT = [sys.executable, str(Path(__file__).parent / "fixtures" / "plug_mcp.py")]
SWAGGER = [sys.executable, str(SRC.parent / "examples" / "swagger" / "server.py")]
_AS_OPERATOR = mock.patch("dcap.operator.present", return_value=True)


def setUpModule():
    _AS_OPERATOR.start()


def tearDownModule():
    _AS_OPERATOR.stop()


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(os.path.realpath(tmp.name)) / "work"
        self.root.mkdir()
        (self.root / "app.py").write_text('"""App."""\n')
        self.store = self.root.parent / "plugged"
        env = mock.patch.dict(os.environ, {"DCAP_PLUGGED": str(self.store), "DCAP_SIMULATE": "1",
                                           "DCAP_POLICY": str(self.root.parent / "policy.json")})
        env.start()
        self.addCleanup(env.stop)

    def plug(self, name="kit", command=KIT, **options):
        return plug.plug(name, command, **options)

    def dcap(self, grants=()):
        return Dcap(self.root, [str(f) for f in plug.folders()], grants=list(grants) if not isinstance(grants, dict) else grants)

    def uses(self, out):
        return {use["tool"]: use for use in out["uses"]}

    def cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = main(list(argv))
        return code, out.getvalue()


class Naming(Base):
    def test_each_tool_gets_a_plain_name_and_switch(self):
        uses = self.uses(self.plug())
        self.assertEqual({tool: use["switch"] for tool, use in uses.items()}, {
            "nmap_scan": "SCAN_NETWORK", "whoisLookup": "WHOIS_LOOKUP", "report": "SUMMARISE_FINDINGS", "fetch": "FETCH_PAGE",
        })
        self.assertEqual(uses["nmap_scan"]["name"], "Scan network")

    def test_a_one_word_tool_takes_its_object_from_the_description(self):
        self.assertEqual(self.uses(self.plug())["fetch"]["name"], "Fetch page")

    def test_a_title_wins_over_the_tool_name(self):
        self.assertEqual(self.uses(self.plug())["report"]["name"], "Summarise findings")

    def test_the_operator_can_choose_a_name(self):
        uses = self.uses(self.plug(renames={"whoisLookup": "Find domain owner"}))
        self.assertEqual(uses["whoisLookup"]["switch"], "FIND_DOMAIN_OWNER")

    def test_a_name_already_in_use_is_told_apart(self):
        self.plug()
        uses = self.uses(self.plug("second kit", KIT))
        self.assertEqual(uses["nmap_scan"]["switch"], "SCAN_NETWORK_SECOND")

    def test_the_same_server_always_gives_the_same_files(self):
        self.plug()
        first = {str(p.relative_to(self.store)): p.read_bytes() for p in self.store.rglob("*.yaml")}
        self.plug(replace=True)
        self.assertEqual(first, {str(p.relative_to(self.store)): p.read_bytes() for p in self.store.rglob("*.yaml")})
        self.assertEqual(len(first), 3)

    def test_a_real_server_s_tools(self):
        uses = self.uses(self.plug("swagger", SWAGGER))
        self.assertEqual({use["switch"] for use in uses.values()},
                         {"FETCH_SWAGGER_INFO", "LIST_ENDPOINTS", "GET_ENDPOINT_DETAILS"})
        self.assertEqual(uses["fetch_swagger_info"]["scope"], "url")


class Wiring(Base):
    def test_what_was_written_loads_with_everything_else(self):
        self.plug()
        reg = Registries([str(f) for f in plug.folders()])
        self.assertIn("kit.scan_network", reg.cores)
        self.assertIn("KIT.SCAN_NETWORK", reg.goals)
        self.assertEqual(reg.providers_for("kit.scan_network")[0].operation, "nmap_scan")
        self.assertIn("web.link_check", reg.cores)

    def test_inputs_keep_their_shape_and_awkward_names_are_mapped_back(self):
        self.plug()
        reg = Registries([str(f) for f in plug.folders()])
        scan = reg.cores["kit.scan_network"]
        self.assertEqual(list(scan.inputs), ["target", "ports", "max_retries"])
        self.assertEqual(reg.types[scan.inputs["ports"]["type"]]["items"], {"type": "integer"})
        self.assertFalse(scan.inputs["ports"]["required"])
        self.assertEqual(reg.providers_for("kit.scan_network")[0].input_map, {"max_retries": "max-retries"})
        fetch = reg.cores["kit.fetch_page"]
        self.assertEqual(reg.providers_for("kit.fetch_page")[0].input_map, {"class_": "class"})
        self.assertEqual(reg.types[fetch.inputs["class_"]["type"]]["enum"], ["fast", "full"])

    def test_permissions_follow_what_the_tool_says_about_itself(self):
        uses = self.uses(self.plug())
        self.assertEqual(uses["whoisLookup"]["permission"], "kit.read")
        self.assertEqual(uses["nmap_scan"]["permission"], "kit.use")
        self.assertEqual((uses["nmap_scan"]["scope"], uses["whoisLookup"]["scope"]), ("target", "domain"))
        self.assertNotIn("scope", uses["report"])

    def test_a_tool_that_may_depend_on_another_is_flagged(self):
        self.assertTrue(any(note.startswith("report:") for note in self.plug()["review"]))

    def test_plugging_twice_needs_replace_and_keeps_edits_until_then(self):
        self.plug()
        with self.assertRaises(StateError):
            self.plug()
        self.plug(replace=True)

    def test_definitions_that_cannot_load_are_not_kept(self):
        with mock.patch("dcap.plug.build", return_value=({"providers/kit.yaml": "providers: [{}]\n"}, {})):
            with self.assertRaises(Exception):
                self.plug()
        self.assertEqual(plug.folders(), [])

    def test_a_server_with_no_tools_is_refused(self):
        with mock.patch("dcap.plug.list_tools", return_value=[]), self.assertRaises(StateError):
            self.plug()
        self.assertEqual(plug.folders(), [])

    def test_unplug_removes_it(self):
        self.plug()
        self.assertEqual(plug.unplug("kit"), {"unplugged": "kit"})
        self.assertEqual(plug.folders(), [])
        with self.assertRaises(StateError):
            plug.unplug("kit")


class WhatTheAiSees(Base):
    def paste(self, said, answer="Yes."):
        listener = Listener(self.dcap())
        offer = listener.observe([said], [])
        return offer, (listener.observe([answer], []) if offer else None)

    def test_asking_in_plain_words_pastes_one_switch(self):
        self.plug()
        offer, pasted = self.paste("I need to scan the network for open ports.")
        self.assertEqual(offer, "DCAP: want help with scan network? Yes or no.")
        self.assertIn('os.environ["SCAN_NETWORK"] = "false"  # scan_network(target, ports=None, max_retries=None)', pasted)
        self.assertEqual(pasted.count('os.environ["'), 1)

    def test_the_tool_and_server_never_show(self):
        self.plug("toolbox", KIT)
        for said in ("I need to scan the network.", "Let me run an nmap scan.", "I want to look up who registered a domain."):
            with self.subTest(said):
                offer, pasted = self.paste(said)
                for hidden in ("nmap", "whoislookup", "mcp", "toolbox.mcp", "plug_mcp"):
                    self.assertNotIn(hidden, f"{offer}\n{pasted}".lower())

    def test_work_on_its_own_code_is_left_alone(self):
        self.plug()
        self.assertEqual(self.paste("I'll fix the scan bug in the network test."), (None, None))

    def test_nothing_is_offered_until_it_is_plugged_in(self):
        self.assertEqual(self.paste("I need to scan the network for open ports."), (None, None))


class Running(Base):
    def run_app(self, call, grants):
        listener = Listener(self.dcap())
        listener.observe(["I need to scan the network for open ports.", "I want to look up who registered a domain.",
                          "I need to summarise the findings."], [])
        listener.observe(["Yes."], [])
        for said in ("I want to look up who registered a domain.", "I need to summarise the findings."):
            listener.observe([said], [])
            listener.observe(["Yes."], [])
        app = self.root / "app.py"
        app.write_text(app.read_text().replace('= "false"', '= "true"') + f"\nimport json\nprint(json.dumps({call}))\n")
        (self.root.parent / "policy.json").write_text(json.dumps({"grants": grants}))
        env = {**os.environ}
        env.pop("PYTHONPATH", None)
        return subprocess.run([sys.executable, "app.py"], cwd=self.root, env=env, capture_output=True, text=True)

    def test_pasted_code_calls_the_server_with_the_tool_s_own_argument_names(self):
        self.plug()
        run = self.run_app("scan_network('10.0.0.5', ports=[443], max_retries=2)", {"kit.use": ["10.0.0.0/8"]})
        self.assertEqual(json.loads(run.stdout), {"target": "10.0.0.5", "open": [443], "retries": 2}, run.stderr)

    def test_plain_text_and_structured_replies_both_come_back(self):
        self.plug()
        run = self.run_app("[whois_lookup('example.test'), summarise_findings()]", ["kit.read", "kit.use"])
        self.assertEqual(json.loads(run.stdout),
                         ["example.test is registered to Example Ltd", {"summary": "2 open ports"}], run.stderr)

    def test_nothing_runs_without_the_operator_s_grant(self):
        self.plug()
        for grants in ({}, {"kit.read": ["*"]}, {"kit.use": ["192.168.0.0/16"]}):
            with self.subTest(grants):
                run = self.run_app("scan_network('10.0.0.5')", grants)
                self.assertIn("permission not granted: kit.use", run.stderr)

    def test_the_operator_can_run_it_directly(self):
        self.plug()
        out = self.dcap({"kit.use": ["10.0.0.0/8"]}).request("Scan network", {"target": "10.0.0.9"})
        self.assertEqual(out["status"], "complete", out)
        self.assertEqual(out["result"]["target"], "10.0.0.9")
        self.assertNotIn("nmap", json.dumps(out))
        refused = self.dcap({"kit.use": ["10.0.0.0/8"]}).request("Scan network", {"target": "192.168.1.1"})
        self.assertEqual(refused["status"], "authorization_required")

    def test_a_web_address_is_limited_by_its_host(self):
        self.assertTrue(covers("*.example.test", "https://docs.example.test/page?x=1"))
        self.assertTrue(covers("10.0.0.0/8", "http://10.1.2.3:8080/"))
        for value in ("https://evil.org/.example.test", "https://a.example.test@evil.org/", "https://evil.org#.example.test",
                      "ftp://evil.org/a.example.test", "https://"):
            with self.subTest(value):
                self.assertFalse(covers("*.example.test", value))
        self.plug()
        out = self.dcap({"kit.use": ["*.example.test"]}).request("Fetch page", {"url": "https://evil.org/"})
        self.assertEqual(out["status"], "authorization_required")
        out = self.dcap({"kit.use": ["*.example.test"]}).request("Fetch page", {"url": "https://a.example.test/x"})
        self.assertEqual(out["result"]["url"], "https://a.example.test/x")


class CommandLine(Base):
    def test_plug_list_and_unplug(self):
        code, out = self.cli("plug", "kit", "--", *KIT)
        self.assertEqual(code, 0)
        result = json.loads(out)
        self.assertEqual(result["grant"], ["kit.read", "kit.use"])
        self.assertEqual(result["folder"], str(self.store / "kit"))
        code, out = self.cli("plugged")
        self.assertEqual(json.loads(out)["kit"]["uses"], ["Fetch page", "Scan network", "Summarise findings", "Whois lookup"])
        self.assertEqual(self.cli("plug", "kit", "--", *KIT)[0], 1)
        self.assertEqual(self.cli("plug", "--replace", "--rename", "fetch=Download page", "kit", "--", *KIT)[0], 0)
        self.assertIn("Download page", json.loads(self.cli("plugged")[1])["kit"]["uses"])
        self.assertEqual(self.cli("unplug", "kit")[0], 0)
        self.assertEqual(json.loads(self.cli("plugged")[1]), {})

    def test_a_plugged_server_is_picked_up_with_no_further_setup(self):
        self.cli("plug", "kit", "--", *KIT)
        self.assertEqual(json.loads(self.cli("validate")[1])["classes"], 3)
        said = json.dumps({"said": ["I need to scan the network for open ports."], "wrote": []})
        env = {**os.environ, "PYTHONPATH": str(SRC), "PYTHONDONTWRITEBYTECODE": "1"}
        run = subprocess.run([sys.executable, "-m", "dcap.cli", "--workspace", str(self.root), "listen"], input=said,
                             capture_output=True, text=True, env=env)
        self.assertEqual(run.stdout.strip(), "DCAP: want help with scan network? Yes or no.")

    def test_an_agent_cannot_plug_or_list(self):
        with mock.patch("dcap.operator.present", return_value=False):
            self.assertEqual(self.cli("plug", "kit", "--", *KIT), (1, ""))
            self.assertEqual(self.cli("plugged"), (1, ""))
        self.assertEqual(plug.folders(), [])

    def test_plugged_definitions_are_behind_the_guard(self):
        self.plug()
        self.assertTrue(guard.refuses({"file_path": str(self.store / "kit" / "providers" / "kit.yaml")}, self.root))
        self.assertTrue(guard.refuses({"command": f"ls {self.store}"}, self.root))
        self.assertTrue(guard.refuses({"command": "DCAP_PLUGGED=/tmp/mine python app.py"}, self.root))


if __name__ == "__main__":
    unittest.main()
