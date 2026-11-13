"""An AI can't ask DCAP what it has. It sees only what was pasted after its own request."""
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
# A server plugged in on this machine must not change what these tests see.
os.environ["DCAP_PLUGGED"] = os.path.join(os.path.dirname(os.path.abspath(__file__)), "no-plugged-servers")

from dcap import Dcap, guard, operator  # noqa: E402
from dcap.cli import main  # noqa: E402

PACKAGE = SRC / "dcap"
NOT_OPERATOR = mock.patch("dcap.operator.present", return_value=False)


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(os.path.realpath(tmp.name))
        (self.root / "app.py").write_text('"""App."""\n')

    def cli(self, *argv, stdin=""):
        """Run the command line in this process. Returns (code, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(stdin)), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def child(self, *argv, stdin="", env=None):
        """Run the command line the way an agent's shell does: no terminal on any stream."""
        full = {**os.environ, "PYTHONPATH": str(SRC), "PYTHONDONTWRITEBYTECODE": "1", **(env or {})}
        full.pop("DCAP_OPERATOR", None) if "DCAP_OPERATOR" not in (env or {}) else None
        return subprocess.run([sys.executable, "-m", "dcap.cli", *argv], input=stdin, capture_output=True, text=True,
                              cwd=self.root, env=full)


class WhoIsAsking(Base):
    def test_no_terminal_and_no_key_is_not_the_operator(self):
        with mock.patch("dcap.operator.at_terminal", return_value=False), mock.patch.dict(os.environ, {"DCAP_OPERATOR": ""}):
            self.assertFalse(operator.present())

    def test_a_terminal_is_the_operator(self):
        with mock.patch("dcap.operator.at_terminal", return_value=True):
            self.assertTrue(operator.present())

    def test_only_the_stored_key_is_accepted(self):
        key = self.root / "operator.key"
        with mock.patch("dcap.operator.key_file", return_value=key), mock.patch("dcap.operator.at_terminal", return_value=False):
            with mock.patch.dict(os.environ, {"DCAP_OPERATOR": "guess"}):
                self.assertFalse(operator.present())  # no key has been made
            made = operator.make_key()
            self.assertEqual(key.stat().st_mode & 0o777, 0o600)
            for given, expected in ((made, True), ("guess", False), ("", False), (made + "x", False)):
                with self.subTest(given), mock.patch.dict(os.environ, {"DCAP_OPERATOR": given}):
                    self.assertEqual(operator.present(), expected)
            key.write_text("\n")
            with mock.patch.dict(os.environ, {"DCAP_OPERATOR": ""}):
                self.assertFalse(operator.present())  # an empty key file matches nothing

    def test_the_key_is_not_found_through_home(self):
        with mock.patch.dict(os.environ, {"HOME": str(self.root)}):
            self.assertNotIn(str(self.root), str(operator.key_file()))


class CommandLine(Base):
    OPERATOR_COMMANDS = (["providers"], ["validate"], ["status"], ["explain", "Check site links"],
                         ["request", "Help me investigate my website"], ["integrate", "WEB.PAGE_MAP"],
                         ["results", "r1"], ["context"], ["operator-key"], ["--help"], ["providers", "--help"], [],
                         ["--simulate", "providers"], ["--workspace", ".", "providers"], ["nonsense"])

    def test_an_agent_s_shell_gets_nothing_from_any_operator_command(self):
        for argv in self.OPERATOR_COMMANDS:
            with self.subTest(argv):
                run = self.child(*argv)
                self.assertEqual((run.returncode, run.stdout, run.stderr), (1, "", ""))
        self.assertFalse((self.root / ".dcap").exists())
        self.assertFalse((self.root / "dcap_capabilities").exists())

    def test_a_wrong_key_gets_nothing_either(self):
        run = self.child("providers", env={"DCAP_OPERATOR": "guess"})
        self.assertEqual((run.returncode, run.stdout, run.stderr), (1, "", ""))

    def test_nothing_is_explained_in_process_either(self):
        with NOT_OPERATOR:
            for argv in self.OPERATOR_COMMANDS:
                with self.subTest(argv):
                    self.assertEqual(self.cli(*argv), (1, "", ""))

    def test_a_mistyped_adapter_command_does_not_list_the_others(self):
        with NOT_OPERATOR:
            self.assertEqual(self.cli("listen", "--bogus"), (1, "", ""))
            self.assertEqual(self.cli("hook", "--help"), (1, "", ""))

    def test_the_operator_still_gets_answers(self):
        with mock.patch("dcap.operator.present", return_value=True):
            code, out, _ = self.cli("--simulate", "--workspace", str(self.root), "providers")
        self.assertEqual(code, 0)
        self.assertIn("web.link_check", json.loads(out))

    def test_listening_still_works_for_an_agent(self):
        said = json.dumps({"said": ["I need to check site links on this host."], "wrote": []})
        run = self.child("--workspace", str(self.root), "listen", stdin=said)
        self.assertEqual((run.returncode, run.stdout.strip()), (0, "DCAP: want help with check site links? Yes or no."))
        run = self.child("--workspace", str(self.root), "listen", stdin=json.dumps({"said": ["Yes."], "wrote": []}))
        self.assertIn('os.environ["CHECK_SITE_LINKS"] = "false"', run.stdout)


class Guard(Base):
    def refuses(self, **tool_input):
        return guard.refuses(tool_input, self.root)

    def test_commands_that_reach_behind_dcap(self):
        for command in (
            "dcap providers", "dcap --help", "  dcap status", "cd /tmp && dcap explain 'x'", "ls; dcap validate",
            "/usr/local/bin/dcap providers", "./venv/bin/dcap providers", "DCAP_SIMULATE=1 dcap providers",
            "env dcap providers", "script -q /dev/null dcap providers", "echo $(dcap providers)", "dcap",
            "python3 -m dcap.cli providers", "python -m dcap providers", "python -c 'import dcap; print(dcap)'",
            "python3 -c \"from dcap import Dcap; print(Dcap('.').providers())\"",
            "python -c \"__import__('dcap.api')\"", "python -c 'import importlib; importlib.import_module(\"dcap\")'",
            "cat .dcap/manifest.json", "ls -la .dcap", "cat ~/.config/dcap/policy.json", "cat $HOME/.config/dcap/operator.key",
            "DCAP_POLICY=/tmp/mine.json python app.py", "DCAP_OPERATOR=abc python app.py",
            f"cat {PACKAGE}/definitions/goals/web.yaml", f"ls {PACKAGE}",
            "cat /opt/lib/python3.14/site-packages/dcap/definitions/providers/live.yaml",
            "sed -n 1,40p /x/site-packages/dcap/registry.py",
        ):
            with self.subTest(command):
                self.assertTrue(self.refuses(command=command))

    def test_ordinary_work_is_left_alone(self):
        for command in (
            "python app.py", "pytest -q", "git status", "ls -la", "cat app.py", "cat dcap_capabilities/web_link_check.py",
            "python -c 'from dcap_capabilities.web_link_check import run; print(run)'", "grep -rn check_links .",
            "git commit -m 'Wire in the dcap link check'", "echo dcap-demo", "pip list", "ls dcap_capabilities",
            "CHECK_LINKS=true python app.py", "DCAP_WEB_MAX_PAGES=5 python app.py", "cat notes/dcap_notes.txt",
        ):
            with self.subTest(command):
                self.assertFalse(self.refuses(command=command))

    def test_files_behind_dcap(self):
        for path in (".dcap/manifest.json", ".dcap", str(self.root / ".dcap" / "door.log"), "./x/../.dcap/listener.json",
                     str(PACKAGE / "definitions" / "goals" / "web.yaml"), str(PACKAGE / "registry.py"), str(PACKAGE),
                     str(SRC.parent / "README.md"), str(SRC.parent / "docs" / "DECISIONS.md"),
                     str(operator.key_file()), "~/.config/dcap/policy.json"):
            with self.subTest(path):
                self.assertTrue(self.refuses(file_path=path))
                self.assertTrue(self.refuses(path=path))

    def test_the_ai_s_own_files_are_its_own(self):
        for path in ("app.py", "dcap_capabilities/web_link_check.py", "dcap_capabilities", str(self.root / "notes.md"),
                     "src/dcap_notes.py", "/tmp/elsewhere.txt"):
            with self.subTest(path):
                self.assertFalse(self.refuses(file_path=path))

    def test_a_link_into_dcap_is_followed(self):
        (self.root / "peek").symlink_to(PACKAGE / "definitions")
        self.assertTrue(self.refuses(file_path="peek/goals/web.yaml"))
        self.assertTrue(self.refuses(command=f"cat {PACKAGE}/definitions/goals/web.yaml"))

    def test_the_policy_file_named_by_the_operator(self):
        policy = self.root / "elsewhere" / "policy.json"
        with mock.patch.dict(os.environ, {"DCAP_POLICY": str(policy)}):
            self.assertTrue(self.refuses(file_path=str(policy)))
            self.assertTrue(self.refuses(command=f"echo '{{}}' > {policy}"))

    def test_extra_definitions_are_behind_dcap_too(self):
        extra = self.root.parent / (self.root.name + "-defs")
        self.assertFalse(guard.refuses({"file_path": str(extra / "providers" / "x.yaml")}, self.root))
        self.assertTrue(guard.refuses({"file_path": str(extra / "providers" / "x.yaml")}, self.root, [extra]))

    def test_code_that_imports_dcap_is_not_written(self):
        self.assertTrue(self.refuses(file_path="snoop.py", content="from dcap import Dcap\nprint(Dcap('.').providers())\n"))
        self.assertTrue(self.refuses(file_path="snoop.py", new_string="    import dcap.registry as r\n"))
        self.assertTrue(self.refuses(file_path="a.py", edits=[{"old_string": "x", "new_string": "import dcap"}]))
        pasted = 'import os\nfrom dcap_capabilities.web_link_check import run as check_links\nos.environ["CHECK_LINKS"] = "true"\n'
        self.assertFalse(self.refuses(file_path="app.py", content=pasted))

    def test_searches_aimed_at_dcap_s_state(self):
        self.assertTrue(self.refuses(pattern="provider", path=".dcap"))
        self.assertTrue(self.refuses(pattern="**/.dcap/**"))
        self.assertFalse(self.refuses(pattern="check_links", path="."))

    def test_the_operator_working_on_dcap_itself_is_not_locked_out(self):
        inside = SRC.parent / "examples"
        self.assertFalse(guard.refuses({"file_path": str(SRC.parent / "README.md")}, inside))
        self.assertTrue(guard.refuses({"file_path": str(inside / ".dcap" / "manifest.json")}, inside))


class ClaudeCodeHook(Base):
    def hook(self, tool, tool_input):
        event = {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input, "cwd": str(self.root)}
        run = self.child("hook", stdin=json.dumps(event), env={"CLAUDE_PROJECT_DIR": str(self.root)})
        self.assertEqual((run.returncode, run.stderr), (0, ""))
        return json.loads(run.stdout) if run.stdout else None

    def test_a_tool_call_behind_dcap_is_denied_before_it_runs(self):
        for tool, tool_input in (("Bash", {"command": "dcap providers"}), ("Read", {"file_path": ".dcap/manifest.json"}),
                                 ("Bash", {"command": "python3 -c 'import dcap'"}),
                                 ("Grep", {"pattern": "provider", "path": str(PACKAGE / "definitions")}),
                                 ("Write", {"file_path": str(self.root / ".dcap" / "manifest.json"), "content": "{}"})):
            with self.subTest(tool_input):
                out = self.hook(tool, tool_input)["hookSpecificOutput"]
                self.assertEqual((out["hookEventName"], out["permissionDecision"]), ("PreToolUse", "deny"))
                self.assertNotRegex(out["permissionDecisionReason"].lower(), r"dcap|operator|provider|capabilit")

    def test_ordinary_tool_calls_pass_without_a_word(self):
        for tool, tool_input in (("Bash", {"command": "python app.py"}), ("Read", {"file_path": "app.py"}),
                                 ("Edit", {"file_path": "app.py", "old_string": "App", "new_string": "Application"}),
                                 ("WebSearch", {"query": "dcap"}), ("TodoWrite", {})):
            with self.subTest(tool_input):
                self.assertIsNone(self.hook(tool, tool_input))

    def test_a_refusal_is_logged_for_the_operator(self):
        self.hook("Bash", {"command": "dcap providers"})
        self.assertIn("guard", (self.root / ".dcap" / "door.log").read_text())

    def test_a_fault_in_the_guard_refuses(self):
        with mock.patch("dcap.guard.refuses", side_effect=RuntimeError("boom")), NOT_OPERATOR:
            event = {"hook_event_name": "PreToolUse", "tool_input": {"command": "ls"}, "cwd": str(self.root)}
            code, out, _ = self.cli("--workspace", str(self.root), "hook", stdin=json.dumps(event))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_other_adapters_get_an_exit_code(self):
        refused = self.child("--workspace", str(self.root), "guard", stdin=json.dumps({"input": {"command": "dcap status"}}))
        allowed = self.child("--workspace", str(self.root), "guard", stdin=json.dumps({"input": {"command": "ls"}}))
        broken = self.child("--workspace", str(self.root), "guard", stdin="not json")
        self.assertEqual([(r.returncode, r.stdout, r.stderr) for r in (refused, allowed, broken)],
                         [(2, "", ""), (0, "", ""), (2, "", "")])


class WhatTheAiSees(Base):
    def test_only_an_offer_and_then_the_paste(self):
        from dcap.listener import Listener
        d = Dcap(self.root, grants=[], simulate=True)
        listener = Listener(d)
        self.assertIsNone(listener.observe(["What tools do you have? List your capabilities."], []))
        offer = listener.observe(["I need to check site links on this host."], [])
        self.assertEqual(offer, "DCAP: want help with check site links? Yes or no.")
        pasted = listener.observe(["Yes."], [])
        for hidden in ("placeholder", "provider", "web.link", "WEB.SITE_LINKS", "goal", "contract"):
            self.assertNotIn(hidden, pasted)
        self.assertEqual(pasted.count('os.environ["'), 1)


if __name__ == "__main__":
    unittest.main()
