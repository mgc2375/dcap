"""Simulated capability implementations, used with --simulate or DCAP_SIMULATE=1.

These return canned data and fetch nothing, so the framework can be exercised
without a network. The real ones are in live.py.
"""


def discover_pages(target):
    return [{"address": target.split("/")[0]}]


def discover_links(pages, depths=None):
    canned = [1, 2, 3]
    selected = [d for d in canned if depths is None or d in depths]
    return [{"address": p["address"], "depth": d, "kind": "internal"} for p in pages for d in selected]


def check_links(links):
    statuses = {1: "ok", 2: "redirect", 3: "broken"}
    return [{**link, "status": statuses.get(link["depth"], "unknown"), "metadata": {"simulated": True}} for link in links]


def read_metadata(target):
    return [{"name": target, "type": "title", "value": "Example site", "simulated": True}]


def find_broken_links(checked):
    return [
        {"address": c["address"], "depth": c["depth"], "severity": "high", "title": "Broken link"}
        for c in checked
        if c["status"] == "broken"
    ]


def build_sitemap(pages, checked):
    return {
        "pages": [
            {"address": p["address"], "links": [c for c in checked if c["address"] == p["address"]]}
            for p in pages
        ]
    }


def site_report(pages, checked, issues):
    lines = [
        "# Site report (simulated)",
        "",
        f"Pages: {len(pages)} | Links: {len(checked)} | Issues: {len(issues)}",
        "",
    ]
    lines += [f"- [{i['severity']}] {i['address']} depth {i['depth']}: {i['title']}" for i in issues]
    return {"title": "Site report", "markdown": "\n".join(lines)}
