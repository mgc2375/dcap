"""The live provider, against small sites served on this machine. Nothing leaves it."""
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
# A server plugged in on this machine must not change what these tests see.
os.environ["DCAP_PLUGGED"] = os.path.join(os.path.dirname(os.path.abspath(__file__)), "no-plugged-servers")

from dcap import Dcap  # noqa: E402
from dcap.cli import main  # noqa: E402
from dcap.registry import Registries  # noqa: E402
from dcap.runtime import live  # noqa: E402
from dcap.runtime.policy import covers, granted, load_grants  # noqa: E402

# These tests act as the operator.
_AS_OPERATOR = mock.patch("dcap.operator.present", return_value=True)


def setUpModule():
    _AS_OPERATOR.start()


def tearDownModule():
    _AS_OPERATOR.stop()


def serve(routes, hits=None):
    """Serve {path: (status, headers, body)} on a free local port. Returns the server."""
    class Handler(BaseHTTPRequestHandler):
        def _answer(self, body=True):
            if hits is not None:
                hits.append((self.command, self.path))
            status, headers, content = routes.get(self.path, (404, {}, "missing"))
            if callable(status):
                status, headers, content = status(self)
            data = content.encode()
            self.send_response(status)
            for name, value in {"Content-Type": "text/html; charset=utf-8", **headers}.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if body:
                self.wfile.write(data)

        def do_GET(self):
            self._answer()

        def do_HEAD(self):
            self._answer(body=False)

        def log_message(self, *args):
            pass

        def version_string(self):
            return "test-site"

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    return server


class Live(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.hits, self.other_hits = [], []
        self.other = serve({"/": (200, {}, "<title>Elsewhere</title>")}, self.other_hits)
        elsewhere = f"http://localhost:{self.other.server_port}/"
        self.site = serve({
            "/": (200, {}, f"""<html lang="en"><head><title>Home  page</title>
                <meta name="description" content="A small site"><link rel="stylesheet" href="/style.css"></head>
                <body><a href="/about">About</a> <a href="/gone">Gone</a> <a href="/old">Old</a>
                <a href="/private">Private</a> <a href="#top">Top</a> <a href="mailto:a@example.test">Mail</a>
                <a href="{elsewhere}">Elsewhere</a> <img src="/logo.png"></body></html>"""),
            "/about": (200, {}, '<title>About</title><a href="/">Home</a> <a href="/deep">Deep</a>'),
            "/deep": (200, {}, '<title>Deep</title><a href="/deeper-missing">Missing</a>'),
            "/old": (301, {"Location": "/about"}, ""),
            "/private": (403, {}, "no"),
            "/style.css": (200, {"Content-Type": "text/css"}, "body{}"),
            "/logo.png": (200, {"Content-Type": "image/png"}, "png"),
            "/robots.txt": (200, {"Content-Type": "text/plain"}, "User-agent: *\nSitemap: /sitemap.xml\n"),
            "/sitemap.xml": (200, {"Content-Type": "application/xml"},
                             "<urlset><url><loc>/listed-only</loc></url></urlset>"),
            "/listed-only": (200, {}, "<title>Listed</title>"),
        }, self.hits)
        for server in (self.site, self.other):
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        self.target = f"127.0.0.1:{self.site.server_port}"
        self.base = f"http://{self.target}"
        self.elsewhere = elsewhere

    def dcap(self, grants=None):
        return Dcap(self.root, grants={"web.fetch": ["127.0.0.0/8"]} if grants is None else grants, simulate=False)

    def by_url(self, items):
        return {item["url"]: item for item in items}


class Selection(Live):
    def test_live_is_the_default_and_canned_data_is_asked_for(self):
        with mock.patch.dict(os.environ, {"DCAP_SIMULATE": ""}):
            self.assertEqual({i.provider for i in Registries().providers_for("web.link_check")}, {"dcap.live"})
        with mock.patch.dict(os.environ, {"DCAP_SIMULATE": "1"}):
            self.assertEqual({i.provider for i in Registries().providers_for("web.link_check")}, {"placeholder.native"})
        self.assertEqual({i.provider for i in Registries(simulate=False).providers_for("web.link_check")}, {"dcap.live"})

    def test_canned_data_never_stands_in_for_a_site_that_is_down(self):
        self.site.shutdown()
        self.site.server_close()
        out = self.dcap().request("Map pages", {"target": self.target})
        self.assertEqual(out["status"], "failed")
        self.assertEqual(out["errors"][0]["code"], "EXECUTION_FAILED")
        self.assertNotIn("result", out)

    def test_every_core_has_a_live_provider(self):
        reg = Registries(simulate=False)
        self.assertEqual({core for core in reg.cores if not reg.providers_for(core)}, set())


class Capabilities(Live):
    def test_pages_come_from_links_and_the_sitemap(self):
        out = self.dcap().request("Map pages", {"target": self.target})
        self.assertEqual(out["status"], "complete", out)
        urls = [page["url"] for page in out["result"]]
        self.assertEqual(urls[0], f"{self.base}/")
        self.assertEqual(set(urls), {f"{self.base}{p}" for p in ("/", "/about", "/deep", "/listed-only")})
        self.assertEqual({page["address"] for page in out["result"]}, {self.target})

    def test_links_are_checked_for_real(self):
        out = self.dcap().request("Check site links", {"target": self.target})
        self.assertEqual(out["status"], "complete", out)
        links = self.by_url(out["result"])
        status = {url.removeprefix(self.base): link["status"] for url, link in links.items()}
        self.assertEqual(status["/about"], "ok")
        self.assertEqual(status["/style.css"], "ok")
        self.assertEqual(status["/gone"], "broken")
        self.assertEqual(status["/deeper-missing"], "broken")
        self.assertEqual(status["/old"], "redirect")
        self.assertEqual(status["/private"], "blocked")
        self.assertEqual(links[f"{self.base}/gone"]["metadata"]["http_status"], 404)
        self.assertEqual(links[f"{self.base}/old"]["metadata"]["final_url"], f"{self.base}/about")
        self.assertEqual(links[f"{self.base}/gone"]["source"], f"{self.base}/")
        self.assertEqual(links[f"{self.base}/gone"]["kind"], "internal")
        self.assertFalse(any("mailto" in url or "#" in url for url in links))

    def test_a_link_to_a_host_without_a_grant_is_never_requested(self):
        out = self.dcap().request("Check site links", {"target": self.target})
        link = self.by_url(out["result"])[self.elsewhere]
        self.assertEqual((link["kind"], link["status"]), ("external", "skipped"))
        self.assertEqual(self.other_hits, [])

    def test_a_link_to_a_granted_host_is_checked(self):
        out = self.dcap({"web.fetch": ["127.0.0.0/8", "localhost"]}).request("Check site links", {"target": self.target})
        self.assertEqual(self.by_url(out["result"])[self.elsewhere]["status"], "ok")
        self.assertTrue(self.other_hits)

    def test_review_reports_only_what_is_broken(self):
        out = self.dcap().request("Review links", {"target": self.target})
        self.assertEqual(out["status"], "complete", out)
        issues = self.by_url(out["result"])
        self.assertEqual(set(issues), {f"{self.base}/gone", f"{self.base}/deeper-missing"})
        self.assertEqual(issues[f"{self.base}/gone"]["severity"], "high")
        self.assertIn("HTTP 404", issues[f"{self.base}/gone"]["title"])

    def test_sitemap_puts_each_link_under_the_page_it_is_on(self):
        out = self.dcap().request("Build a sitemap", {"target": self.target})
        self.assertEqual(out["status"], "complete", out)
        pages = self.by_url(out["result"]["pages"])
        self.assertIn(f"{self.base}/deep", {link["url"] for link in pages[f"{self.base}/about"]["links"]})
        self.assertEqual([link["url"] for link in pages[f"{self.base}/deep"]["links"]], [f"{self.base}/deeper-missing"])

    def test_audit_writes_a_report_from_what_was_found(self):
        out = self.dcap().request("Audit this site and generate a site report", {"target": self.target})
        self.assertEqual(out["status"], "complete", out)
        report = out["result"]["markdown"]
        self.assertIn(f"# Site report: {self.target}", report)
        self.assertIn(f"[high] Broken link: {self.base}/gone (HTTP 404)", report)
        self.assertIn("- broken: 2", report)
        self.assertNotIn("simulated", report)

    def test_metadata_is_read_from_the_page_and_its_headers(self):
        with granted({"web.fetch": ["127.0.0.0/8"]}):
            facts = {fact["type"]: fact["value"] for fact in live.read_metadata(self.target)}
        self.assertEqual(facts["title"], "Home page")
        self.assertEqual(facts["description"], "A small site")
        self.assertEqual(facts["language"], "en")
        self.assertEqual(facts["server"], "test-site")
        self.assertEqual(facts["url"], f"{self.base}/")

    def test_depths_choose_how_far_links_are_followed(self):
        pages = [{"address": self.target, "url": f"{self.base}/"}]
        with granted({"web.fetch": ["127.0.0.0/8"]}):
            near = live.discover_links(pages)
            far = live.discover_links(pages, [2])
        self.assertEqual({link["depth"] for link in near}, {1})
        self.assertIn(f"{self.base}/about", {link["url"] for link in near})
        self.assertEqual({link["url"] for link in far}, {f"{self.base}/", f"{self.base}/deep"})
        self.assertEqual({link["depth"] for link in far}, {2})
        self.assertEqual({link["source"] for link in far}, {f"{self.base}/about"})

    def test_results_are_the_same_each_time(self):
        first = self.dcap().request("Check site links", {"target": self.target})["result"]
        second = self.dcap().request("Check site links", {"target": self.target})["result"]
        self.assertEqual(first, second)

    def test_limits_come_from_the_environment(self):
        with mock.patch.dict(os.environ, {"DCAP_WEB_MAX_PAGES": "2"}):
            out = self.dcap().request("Map pages", {"target": self.target})
        self.assertEqual(len(out["result"]), 2)


class Permissions(Live):
    def test_nothing_is_requested_without_a_grant(self):
        out = self.dcap({}).request("Check site links", {"target": self.target})
        self.assertEqual(out["status"], "authorization_required")
        self.assertEqual(self.hits, [])

    def test_a_grant_for_another_host_does_not_cover_this_one(self):
        out = self.dcap({"web.fetch": ["10.0.0.0/8"]}).request("Check site links", {"target": self.target})
        self.assertEqual(out["status"], "authorization_required")
        self.assertEqual(self.hits, [])

    def test_a_port_does_not_change_which_host_is_granted(self):
        self.assertTrue(covers("127.0.0.0/8", "127.0.0.1:8000"))
        self.assertTrue(covers("*.example.test", "a.example.test:8443"))
        self.assertTrue(covers("::1", "[::1]:8080"))
        self.assertFalse(covers("*.example.test", "evil.org:80"))
        self.assertFalse(covers("127.0.0.0/8", "10.0.0.1:8000"))
        self.assertFalse(covers("*.example.test", "evil.org/.example.test:80"))

    def test_a_redirect_is_not_followed_to_a_host_without_a_grant(self):
        hits = []
        hop = serve({"/": (302, {"Location": self.elsewhere}, "")}, hits)
        self.addCleanup(hop.server_close)
        self.addCleanup(hop.shutdown)
        link = {"address": self.target, "url": f"http://127.0.0.1:{hop.server_port}/", "depth": 1, "kind": "internal"}
        with granted({"web.fetch": ["127.0.0.0/8"]}):
            checked = live.check_links([link])[0]
        self.assertEqual(checked["status"], "redirect")
        self.assertEqual(checked["metadata"]["final_url"], self.elsewhere)
        self.assertTrue(hits)
        self.assertEqual(self.other_hits, [])

    def test_a_site_that_moves_to_another_host_needs_that_host_granted(self):
        hop = serve({"/": (302, {"Location": self.elsewhere}, "")})
        self.addCleanup(hop.server_close)
        self.addCleanup(hop.shutdown)
        out = self.dcap().request("Map pages", {"target": f"127.0.0.1:{hop.server_port}"})
        self.assertEqual(out["status"], "failed")
        self.assertEqual(self.other_hits, [])

    def test_a_run_s_grants_do_not_outlive_it(self):
        with mock.patch.dict(os.environ, {"DCAP_POLICY": str(self.root / "none.json")}):
            with granted({"web.fetch": ["*"]}):
                self.assertEqual(load_grants(), {"web.fetch": ["*"]})
            self.assertEqual(load_grants(), {})

    def test_command_line_grant_reaches_the_capability(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"DCAP_SIMULATE": "", "DCAP_POLICY": str(self.root / "none.json")}):
            with contextlib.redirect_stdout(out):
                code = main(["--workspace", str(self.root), "--grant", "web.fetch=127.0.0.0/8",
                             "request", "Review links", "--param", f"target={self.target}"])
        self.assertEqual(code, 0)
        result = json.loads(out.getvalue())
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual(len(result["result"]), 2)


class Injected(Live):
    def run_app(self, code, grants):
        policy = self.root / "policy.json"
        policy.write_text(json.dumps({"grants": grants}))
        env = {**os.environ, "DCAP_POLICY": str(policy)}
        env.pop("PYTHONPATH", None)
        return subprocess.run([sys.executable, "-c", code], cwd=self.root, env=env, capture_output=True, text=True)

    def integrate(self):
        (self.root / "app.py").write_text("def inspect_site(target):\n    pass\n")
        self.assertEqual(self.dcap().integrate("WEB.LINK_REVIEW")["status"], "integrated")
        text = (self.root / "app.py").read_text()
        (self.root / "app.py").write_text(text.replace('= "false"', '= "true"'))

    def test_pasted_code_checks_a_real_site_without_dcap_installed(self):
        self.integrate()
        files = {path.name for path in (self.root / "dcap_capabilities").iterdir() if path.suffix == ".py"}
        self.assertEqual(files, {"__init__.py", "contract.py", "live.py", "policy.py", "web_link_review.py"})
        run = self.run_app(f"import json, app; print(json.dumps(app.inspect_site({self.target!r})))",
                           {"web.fetch": ["127.0.0.0/8"]})
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual({issue["url"] for issue in json.loads(run.stdout)},
                         {f"{self.base}/gone", f"{self.base}/deeper-missing"})

    def test_pasted_code_obeys_the_policy_file(self):
        self.integrate()
        run = self.run_app(f"import app; app.inspect_site({self.target!r})", {"web.fetch": ["10.0.0.0/8"]})
        self.assertIn("permission not granted", run.stderr)
        self.assertEqual(self.hits, [])

    def test_the_paste_looks_the_same_whichever_provider_is_behind_it(self):
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        pasted = self.dcap().door("I want to investigate my website")
        canned = Dcap(other.name, grants=["web.fetch"], simulate=True).door("I want to investigate my website")
        self.assertIn('os.environ["CHECK_LINKS"] = "false"', pasted)
        self.assertEqual(pasted, canned)
        self.assertNotIn("live", pasted)


if __name__ == "__main__":
    unittest.main()
