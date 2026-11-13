"""Live website capabilities: real HTTP requests, stdlib only.

Every request goes through `_fetch`, which checks the operator's `web.fetch` grant for
the host first. That holds for redirects and for links to other sites, so a page can
never send DCAP somewhere the operator has not allowed.
"""
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
import http.client
import os
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

from .policy import NotAuthorized, authorize, load_grants

PERMISSION = "web.fetch"
AGENT = "dcap/0.1 (site check)"
_REDIRECTS = {301, 302, 303, 307, 308}
_BLOCKED = {401, 403, 407, 429}
_LINK_TAGS = {
    "a": "href", "area": "href", "link": "href", "img": "src", "script": "src",
    "iframe": "src", "source": "src", "video": "src", "audio": "src", "embed": "src",
}
_PAGE_TAGS = {"a", "area"}
_NOT_PAGES = re.compile(
    r"\.(?:png|jpe?g|gif|svg|webp|ico|css|js|mjs|json|xml|pdf|zip|gz|tgz|mp[34]|webm|woff2?|ttf|eot)$", re.IGNORECASE
)
_LOC = re.compile(r"<loc>\s*(?:<!\[CDATA\[)?\s*([^<\]\s]+)", re.IGNORECASE)


def _limit(name, default):
    try:
        return max(1, int(os.environ.get(f"DCAP_WEB_{name}", default)))
    except ValueError:
        return default


def _limits():
    return {
        "pages": _limit("MAX_PAGES", 50),      # pages crawled for one site
        "links": _limit("MAX_LINKS", 500),     # links returned or checked in one call
        "depth": _limit("MAX_DEPTH", 3),       # how far link discovery follows a site
        "timeout": _limit("TIMEOUT", 10),      # seconds for one request
        "budget": _limit("BUDGET", 90),        # seconds for one capability call
        "bytes": _limit("MAX_BYTES", 2_000_000),
        "workers": _limit("WORKERS", 8),
        "hops": 5,
    }


class _Failed(Exception):
    """The request got no HTTP answer."""


class _Denied(Exception):
    """The operator has not granted web.fetch for this host."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # each hop is checked against the grants before it is requested


_OPENER = urllib.request.build_opener(_NoRedirect)


class _Reply:
    def __init__(self, status, headers, body=b""):
        self.status = status
        self.headers = headers
        self.body = body

    def is_html(self):
        kind = self.headers.get("Content-Type") or ""
        return "html" in kind.lower() or (not kind and b"<" in self.body[:512])

    def text(self):
        charset = self.headers.get_content_charset() if hasattr(self.headers, "get_content_charset") else None
        try:
            return self.body.decode(charset or "utf-8", errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")


def _why(exc):
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, socket.timeout) or isinstance(exc, TimeoutError):
        return "timed out"
    if isinstance(reason, socket.gaierror):
        return "host name not found"
    if isinstance(reason, ConnectionRefusedError):
        return "connection refused"
    if isinstance(reason, ssl.SSLError):
        return "TLS error"
    return (str(reason) or type(reason).__name__)[:120]


def _norm(url):
    """One spelling per URL: no fragment, lower-case host, no default port, safe characters."""
    parts = urllib.parse.urlsplit(urllib.parse.urldefrag(url.strip())[0])
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if ":" in host:
        host = f"[{host}]"
    try:
        port = parts.port
    except ValueError:
        port = None
    if port and port != {"http": 80, "https": 443}.get(scheme):
        host = f"{host}:{port}"
    path = urllib.parse.quote(parts.path or "/", safe="/%:@!$&'()*+,;=~-._")
    query = urllib.parse.quote(parts.query, safe="/%:@!$&'()*+,;=~-._?")
    return urllib.parse.urlunsplit((scheme, host, path, query, ""))


def _site(url):
    return urllib.parse.urlsplit(url).netloc.lower()


def _fetch(url, grants, limits, method="GET", read=True):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise _Failed("not a web address")
    try:
        authorize(PERMISSION, parts.hostname, grants)
    except NotAuthorized:
        raise _Denied(parts.hostname) from None
    request = urllib.request.Request(url, method=method, headers={"User-Agent": AGENT, "Accept": "*/*"})
    try:
        with _OPENER.open(request, timeout=limits["timeout"]) as response:
            return _Reply(response.status, response.headers, response.read(limits["bytes"]) if read else b"")
    except urllib.error.HTTPError as exc:
        with exc:
            return _Reply(exc.code, exc.headers)
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
        raise _Failed(_why(exc)) from None


def _follow(url, grants, limits, method="GET", read=True):
    """Fetch a URL through its redirects. Returns (reply, final url, hops)."""
    seen = [url]
    while True:
        reply = _fetch(url, grants, limits, method, read)
        location = reply.headers.get("Location") if reply.status in _REDIRECTS else None
        if not location:
            return reply, url, len(seen) - 1
        try:
            url = _norm(urllib.parse.urljoin(url, location))
        except ValueError:
            raise _Failed("redirect to an invalid address") from None
        if url in seen or len(seen) > limits["hops"]:
            raise _Failed("redirect loop")
        seen.append(url)


def _address(target):
    return target.split("/")[0]


def _netloc(address):
    return f"[{address}]" if address.count(":") > 1 and not address.startswith("[") else address


def _root(address, grants, limits):
    """The site's front page: (final url, reply). Tries https and http in a sensible order."""
    port = address.rsplit(":", 1)[1] if address.count(":") == 1 else None
    schemes = ("https", "http") if port in (None, "443", "8443") else ("http", "https")
    problem = "unreachable"
    for scheme in schemes:
        try:
            reply, final, _ = _follow(_norm(f"{scheme}://{_netloc(address)}/"), grants, limits)
            return final, reply
        except _Denied as exc:
            raise PermissionError(f"{PERMISSION} is not granted for {exc}") from None
        except _Failed as exc:
            problem = str(exc)
    raise ConnectionError(f"{address} could not be reached: {problem}")


class _Page(HTMLParser):
    """The links, title and metadata of one HTML page."""

    def __init__(self, url):
        super().__init__(convert_charrefs=True)
        self.base = url
        self.found = []  # (absolute url, tag)
        self.meta = {}
        self.title = None
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        attrs = {name: value for name, value in attrs if value is not None}
        if tag == "base" and attrs.get("href"):
            self.base = urllib.parse.urljoin(self.base, attrs["href"])
        elif tag == "html" and attrs.get("lang"):
            self.meta["language"] = attrs["lang"]
        elif tag == "title" and self.title is None:
            self._in_title, self.title = True, ""
        elif tag == "meta":
            name = (attrs.get("name") or attrs.get("property") or "").lower()
            if name in {"description", "generator", "og:title", "og:site_name"} and attrs.get("content"):
                self.meta.setdefault(name, attrs["content"].strip())
        if tag == "link" and "canonical" in (attrs.get("rel") or "").lower() and attrs.get("href"):
            self.meta.setdefault("canonical", urllib.parse.urljoin(self.base, attrs["href"]))
        attribute = _LINK_TAGS.get(tag)
        value = (attrs.get(attribute) or "").strip() if attribute else ""
        if value and not value.startswith("#"):
            try:
                url = _norm(urllib.parse.urljoin(self.base, value))
            except ValueError:
                return
            if url.startswith(("http://", "https://")):
                self.found.append((url, tag))

    def handle_data(self, data):
        if self._in_title:
            self.title += data

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False


def _parse(url, reply):
    page = _Page(url)
    try:
        page.feed(reply.text())
        page.close()
    except Exception:  # a malformed page still gives whatever was read before the fault
        pass
    return page


def _parallel(work, items, limits):
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=min(limits["workers"], len(items))) as pool:
        return list(pool.map(work, items))


def _load(url, grants, limits, deadline):
    """A page's parsed HTML and where it finally loaded from, or None if it isn't a loadable page."""
    if time.monotonic() > deadline:
        return None
    try:
        reply, final, _ = _follow(url, grants, limits)
    except (_Failed, _Denied):
        return None
    if reply.status != 200 or not reply.is_html():
        return None
    return final, _parse(final, reply)


def _listed(root, grants, limits, deadline):
    """Page addresses the site publishes in robots.txt and its sitemaps."""
    sitemaps, pages = [urllib.parse.urljoin(root, "/sitemap.xml")], []
    try:
        robots = _fetch(urllib.parse.urljoin(root, "/robots.txt"), grants, limits)
        if robots.status == 200:
            for line in robots.text().splitlines():
                key, _, value = line.partition(":")
                if key.strip().lower() == "sitemap" and value.strip():
                    sitemaps.append(urllib.parse.urljoin(root, value.strip()))
    except (_Failed, _Denied):
        pass
    seen = set()
    while sitemaps and len(seen) < 5 and time.monotonic() < deadline:
        sitemap = sitemaps.pop(0)
        if sitemap in seen or _site(sitemap) != _site(root):
            continue
        seen.add(sitemap)
        try:
            reply = _fetch(sitemap, grants, limits)
        except (_Failed, _Denied):
            continue
        if reply.status != 200:
            continue
        for found in _LOC.findall(reply.text()):
            try:
                url = _norm(urllib.parse.urljoin(root, found.replace("&amp;", "&")))
            except ValueError:
                continue
            (sitemaps if url.lower().endswith((".xml", ".xml.gz")) else pages).append(url)
    return pages


# ---- capabilities ---------------------------------------------------------------

def discover_pages(target):
    """Crawl a site from its front page and its sitemap. Returns the pages that load."""
    address, grants, limits = _address(target), load_grants(), _limits()
    deadline = time.monotonic() + limits["budget"]
    root, reply = _root(address, grants, limits)
    if reply.status >= 400:
        raise ConnectionError(f"{address} answered HTTP {reply.status}")
    site, pages, seen = _site(root), [], {root}
    frontier = [root]
    for url in _listed(root, grants, limits, deadline):
        if _site(url) == site and url not in seen:
            seen.add(url)
            frontier.append(url)
    first = (root, _parse(root, reply)) if reply.is_html() else None
    while frontier and len(pages) < limits["pages"] and time.monotonic() < deadline:
        batch, frontier = frontier[: limits["pages"] - len(pages)], frontier[limits["pages"] - len(pages):]
        loaded = _parallel(
            lambda url: first if url == root else _load(url, grants, limits, deadline), batch, limits
        )
        for result in loaded:
            if result is None:
                continue
            final, page = result
            if _site(final) != site or final in {p["url"] for p in pages}:
                continue
            pages.append({"address": address, "url": final})
            for url, tag in page.found:
                if tag in _PAGE_TAGS and _site(url) == site and url not in seen and not _NOT_PAGES.search(url):
                    seen.add(url)
                    frontier.append(url)
    if not pages:
        pages.append({"address": address, "url": root})
    return pages


def discover_links(pages, depths=None):
    """The links on each page. Depth 1 is a link on the page itself, depth 2 a link on a page it links to."""
    grants, limits = load_grants(), _limits()
    deadline = time.monotonic() + limits["budget"]
    wanted = sorted({d for d in depths or [1] if 1 <= d <= limits["depth"]})
    if not wanted:
        return []
    links, recorded, visited, frontier, loaded_any = [], set(), set(), [], False
    for page in pages:
        url = page.get("url") or _root(page["address"], grants, limits)[0]
        if url not in visited:
            visited.add(url)
            frontier.append((page["address"], url))
    for depth in range(1, max(wanted) + 1):
        results = _parallel(lambda item: _load(item[1], grants, limits, deadline), frontier, limits)
        following = []
        for (address, source), result in zip(frontier, results):
            if result is None:
                continue
            loaded_any = True
            final, page = result
            site = _site(final)
            for url, tag in page.found:
                internal = _site(url) == site
                if depth in wanted and (address, url) not in recorded and len(links) < limits["links"]:
                    recorded.add((address, url))
                    links.append({
                        "address": address, "url": url, "depth": depth,
                        "kind": "internal" if internal else "external", "source": source,
                    })
                if internal and tag in _PAGE_TAGS and url not in visited and not _NOT_PAGES.search(url):
                    if len(visited) < limits["pages"] * 2:
                        visited.add(url)
                        following.append((address, url))
        frontier = following
    if pages and not loaded_any:
        raise ConnectionError("none of the pages could be loaded")
    return links


def _probe(url, grants, limits, deadline):
    """One link's status and what was seen: ok, redirect, broken, blocked or skipped."""
    seen, method = [url], "HEAD"
    while True:
        if time.monotonic() > deadline:
            return "skipped", {"reason": "time budget used up"}
        try:
            reply = _fetch(url, grants, limits, method, read=False)
        except _Denied:
            if len(seen) == 1:
                return "skipped", {"reason": f"{PERMISSION} is not granted for this host"}
            return "redirect", {"final_url": url, "redirects": len(seen) - 1,
                                "reason": f"{PERMISSION} is not granted for the redirect target"}
        except _Failed as exc:
            return "broken", {"reason": str(exc), **({"final_url": url, "redirects": len(seen) - 1} if len(seen) > 1 else {})}
        if reply.status >= 400 and method == "HEAD":
            method = "GET"  # some servers refuse HEAD for pages they serve normally
            continue
        location = reply.headers.get("Location") if reply.status in _REDIRECTS else None
        if location:
            try:
                url = _norm(urllib.parse.urljoin(url, location))
            except ValueError:
                return "broken", {"http_status": reply.status, "reason": "redirect to an invalid address"}
            if url in seen or len(seen) > limits["hops"]:
                return "broken", {"reason": "redirect loop", "redirects": len(seen)}
            seen.append(url)
            method = "HEAD"
            continue
        seen_more = {"final_url": url, "redirects": len(seen) - 1} if len(seen) > 1 else {}
        detail = {"http_status": reply.status, **seen_more}
        if reply.status in _BLOCKED:
            return "blocked", detail
        if reply.status >= 400:
            return "broken", detail
        return ("redirect" if len(seen) > 1 else "ok"), detail


def _link_url(link, grants, limits, roots):
    """A link from another provider has no URL, so its site's front page stands in for it."""
    if link.get("url"):
        return link["url"]
    address = link["address"]
    if address not in roots:
        try:
            roots[address] = _root(address, grants, limits)[0]
        except (ConnectionError, PermissionError, ValueError):
            roots[address] = f"http://{_netloc(address)}/"
    return roots[address]


def check_links(links):
    """Request every link and report whether it loads."""
    grants, limits = load_grants(), _limits()
    deadline = time.monotonic() + limits["budget"]
    roots = {}
    targets = [_link_url(link, grants, limits, roots) for link in links]
    unique = list(dict.fromkeys(targets))[: limits["links"]]
    outcomes = dict(zip(unique, _parallel(lambda url: _probe(url, grants, limits, deadline), unique, limits)))
    checked = []
    for link, target in zip(links, targets):
        status, metadata = outcomes.get(target, ("skipped", {"reason": "link limit reached"}))
        checked.append({**link, "status": status, "metadata": metadata})
    return checked


def read_metadata(target):
    """A site's title, description and what its server says about itself."""
    address, grants, limits = _address(target), load_grants(), _limits()
    root, reply = _root(address, grants, limits)
    facts = [("url", root), ("http_status", reply.status)]
    if reply.is_html():
        page = _parse(root, reply)
        if page.title and page.title.strip():
            facts.append(("title", " ".join(page.title.split())))
        facts += [(name, page.meta[name]) for name in
                  ("description", "language", "canonical", "generator", "og:title", "og:site_name") if name in page.meta]
    for header, name in (("Server", "server"), ("Content-Type", "content_type"), ("Last-Modified", "last_modified")):
        if reply.headers.get(header):
            facts.append((name, reply.headers.get(header)))
    return [{"name": address, "type": kind, "value": value} for kind, value in facts]


def _detail(checked):
    metadata = checked.get("metadata") or {}
    if "http_status" in metadata:
        return f"HTTP {metadata['http_status']}"
    return metadata.get("reason", "did not load")


def find_broken_links(checked):
    """The links that failed. A broken link inside the site matters more than one that leaves it."""
    issues = []
    for link in checked:
        if link["status"] != "broken":
            continue
        where = link.get("url", link["address"])
        issue = {
            "address": link["address"], "depth": link["depth"],
            "severity": "high" if link.get("kind") == "internal" else "medium",
            "title": f"Broken link: {where} ({_detail(link)})",
        }
        for extra in ("url", "source"):
            if extra in link:
                issue[extra] = link[extra]
        issues.append(issue)
    return issues


def build_sitemap(pages, checked):
    """Each page with the links found on it."""
    def on_page(page, link):
        if "url" in page and "source" in link:
            return link["source"] == page["url"]
        return link["address"] == page["address"]

    return {"pages": [{**page, "links": [link for link in checked if on_page(page, link)]} for page in pages]}


def site_report(pages, checked, issues):
    """A Markdown report of what was found."""
    counts = {}
    for link in checked:
        counts[link["status"]] = counts.get(link["status"], 0) + 1
    sites = sorted({page["address"] for page in pages}) or sorted({link["address"] for link in checked})
    lines = [
        f"# Site report: {', '.join(sites)}" if sites else "# Site report",
        "",
        f"Pages: {len(pages)} | Links: {len(checked)} | Issues: {len(issues)}",
        "",
    ]
    if counts:
        lines += ["## Links", ""] + [f"- {status}: {counts[status]}" for status in sorted(counts)] + [""]
    lines += ["## Issues", ""]
    if issues:
        for issue in sorted(issues, key=lambda i: ({"high": 0, "medium": 1, "low": 2}[i["severity"]], i["title"])):
            found = f" (on {issue['source']})" if issue.get("source") else ""
            lines.append(f"- [{issue['severity']}] {issue['title']}{found}")
    else:
        lines.append("No broken links found.")
    if pages:
        lines += ["", "## Pages", ""] + [f"- {page.get('url', page['address'])}" for page in pages]
    return {"title": "Site report", "markdown": "\n".join(lines)}
