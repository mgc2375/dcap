# DCAP

Dynamic Capability Adaptation Protocol lets an AI coding agent request outcomes instead of managing tools. The AI says what it needs, and DCAP selects an implementation and integrates it into the code the AI is working on.

The built-in capabilities are real: they crawl a site, check its links and report what is broken, using only Python's standard library. Every request they make is limited to the hosts the operator has granted. Canned data is still available with `--simulate`. Choices made for open design questions, and the known limits, are in [docs/DECISIONS.md](docs/DECISIONS.md).

## What the AI sees

DCAP costs the AI nothing until it has something to offer. There is no tool definition, no instruction and no command to learn.

1. DCAP listens to what the AI natively says and writes.
2. When it recognises something it can help with, it puts its hand up: `DCAP: want help with list api endpoints? Yes or no.`
3. The AI answers in its normal reply.
4. On yes, DCAP pastes the code in, with everything off. On no, or if the AI carries on without answering, it writes nothing and doesn't offer that again.

DCAP stays silent the rest of the time, including while the AI works on its own code. Nothing lands while the AI is writing, because nothing lands without a yes.

Saying "I want to investigate my website" and then yes pastes this into the file being worked on, and shows the AI the same text:

```python
# dcap:begin i5bc884f341
# app.py
import os
from dcap_capabilities.web_broken_links import run as find_broken_links
os.environ["FIND_BROKEN_LINKS"] = "false"  # find_broken_links(checked)
from dcap_capabilities.web_link_check import run as check_links
os.environ["CHECK_LINKS"] = "false"  # check_links(links)
from dcap_capabilities.web_link_discovery import run as discover_links
os.environ["DISCOVER_LINKS"] = "false"  # discover_links(pages, depths=None)
from dcap_capabilities.web_metadata import run as read_site_metadata
os.environ["READ_SITE_METADATA"] = "false"  # read_site_metadata(target)
from dcap_capabilities.web_page_discovery import run as discover_pages
os.environ["DISCOVER_PAGES"] = "false"  # discover_pages(target)
from dcap_capabilities.web_sitemap_core import run as build_sitemap
os.environ["BUILD_SITEMAP"] = "false"  # build_sitemap(pages, checked)
# dcap:end i5bc884f341
```

Each capability is its own module, with its own import and its own switch, and using one loads nothing else. The same input always pastes the same bytes. The AI sets the use case it wants to `"true"` and calls it. Calling one that's off raises `DISCOVER_PAGES is off. Set "DISCOVER_PAGES" to "true".`

A clear need works the same way with one switch. "I need to check site links on this host" pastes `check_site_links` and `os.environ["CHECK_SITE_LINKS"] = "false"`, with the whole chain already wired behind it. If the project has an empty stub that clearly fits, such as `def inspect_site(target): pass`, DCAP connects that instead.

If DCAP can't deliver, the AI sees nothing and nothing is written. Failures go to `.dcap/door.log` for the operator.

## Try it

```bash
pip install -e .
```

```bash
dcap validate
```

Check a site's links. The grant names the host DCAP may contact, and nothing is requested without one:

```bash
dcap --grant web.fetch=example.com request "Review links" --param target=example.com
```

To have DCAP listen to a Claude Code agent, add this to the project's `.claude/settings.json`:

```json
{"hooks": {
  "PreToolUse": [{"hooks": [{"type": "command", "command": "dcap hook"}]}],
  "PostToolUse": [{"hooks": [{"type": "command", "command": "dcap hook"}]}],
  "Stop": [{"hooks": [{"type": "command", "command": "dcap hook"}]}]
}}
```

`PostToolUse` and `Stop` are how DCAP listens. `PreToolUse` keeps the AI on its own side, as described under [What the AI can't do](#what-the-ai-cant-do).

`examples/codex/drive.py` does the same for Codex from outside, using its event stream, and `examples/opencode/dcap.js` is a plugin for opencode. Other agents need their own small adapter around `Listener` in `src/dcap/listener.py`, or `dcap listen`.

## Plug in an MCP server

Give DCAP any MCP server and it wires up every tool from the server's own tool list:

```bash
dcap plug network -- npx some-scanner-mcp
```

Each tool becomes one plainly named use with its own switch. The name comes from the tool's name and description, so a tool called `nmap_scan` that says it scans a network for open ports becomes `scan_network`. When the AI later says "I need to scan the network", it gets this and nothing else:

```python
from dcap_capabilities.network_scan_network import run as scan_network
os.environ["SCAN_NETWORK"] = "false"  # scan_network(target, ports=None)
```

The tool's name and the server never appear. `plug` prints what it made, for you to check:

| It reports | Meaning |
| --- | --- |
| `name` and `switch` | What the AI will see for each tool |
| `heard_as` | The phrases that trigger an offer |
| `permission` | What to grant before it can run: `network.read` for tools that say they only read, `network.use` for the rest |
| `scope` | The input a grant is limited by, when the tool takes a host, address or URL |
| `review` | Anything it couldn't settle, such as a tool that needs another to run first |

Nothing plugged in runs until you grant its permission in the policy file. Name the plug after its topic, because that name is part of the import path the AI sees.

- `dcap plug --rename nmap_scan="Probe network" network -- ...` chooses a name yourself.
- `dcap plug --replace network -- ...` wires it up again after the server changes.
- `dcap plugged` lists what is plugged in, and `dcap unplug network` removes it.

The definitions are plain YAML in `~/.config/dcap/plugged/`, loaded automatically, and yours to edit.

## What the AI can't do

The AI can't ask DCAP what it has. It sees a switch only in pasted code, after its own request. Two things enforce that:

- **Operator commands answer only an operator.** They need a terminal, which an agent's shell doesn't have. Run from an agent, every command below prints nothing and exits 1, including `--help`. For a script, make a key once with `dcap operator-key` and pass it for that command as `DCAP_OPERATOR=<key>`.
- **The hook turns the AI's tool calls away.** With `PreToolUse` set up, Claude Code blocks any call that runs `dcap`, imports the `dcap` package, sets `DCAP_POLICY`, or reads or writes DCAP's definitions, its `.dcap/` state, the policy file or the operator key. The AI is told only "That isn't available here." Refusals are logged to `.dcap/door.log`.

The pasted code and `dcap_capabilities/` stay open to the AI, because that is what it was given. Other agents can use `dcap guard`, which reads `{"input": {...}}` and exits 2 if the call must not run.

## Operator tools

Everything here is for you, not the AI, and needs a terminal or the operator key.

Make a request and see the full report. This runs a clear goal straight away, without needing its switch:

```bash
dcap --grant web.fetch request "Check site links on this host"
```

List integrations and anything that has drifted:

```bash
dcap status
```

`dcap results <request_id>` returns a stored outcome, and `dcap release <integration_id>` takes an integration back out.

See how a request resolves, without running or writing anything:

```bash
dcap explain "Check site links on this host"
```

Show which providers can serve each core:

```bash
dcap providers
```

See what a server's tool list would cost an AI if it were loaded directly:

```bash
dcap context -- python server.py
```

Draft a provider definition from a live MCP server, ready for review:

```bash
dcap scaffold-provider my.provider -- python server.py > definitions/providers/my.yaml
```

## Permissions

Grants live outside the workspace, in `~/.config/dcap/policy.json` or the file named by `$DCAP_POLICY`, and the AI-facing interface can't change them:

```json
{"grants": {"web.fetch": ["127.0.0.0/8", "10.0.0.0/24", "*.example.test"]}}
```

For a single command, use `--grant web.fetch=10.0.0.0/24`.

A target can carry a port, such as `127.0.0.1:8000`, and the grant is for the host. A link or redirect that leaves the granted hosts is reported as `skipped` or `redirect` and never requested.

## Live and simulated

DCAP runs live by default. The live provider, `src/dcap/runtime/live.py`, does this:

| Capability | What it does |
| --- | --- |
| Discover pages | Crawls from the front page and the site's sitemap, up to 50 pages |
| Discover links | Lists the links on each page, with the page they were found on |
| Check links | Requests each link and reports `ok`, `redirect`, `broken`, `blocked` or `skipped` |
| Read site metadata | Reads the title, description, language and server headers |
| Find broken links | Picks out broken links, rating ones inside the site `high` |
| Build sitemap | Groups checked links under the page they are on |
| Generate site report | Writes the findings as Markdown |

Limits are set with `DCAP_WEB_MAX_PAGES`, `DCAP_WEB_MAX_LINKS`, `DCAP_WEB_MAX_DEPTH`, `DCAP_WEB_TIMEOUT` and `DCAP_WEB_BUDGET`.

For canned data that makes no requests, pass `--simulate` or set `DCAP_SIMULATE=1`. Only one of the two loads, so canned data never stands in for a site that is down.

## How it fits together

| Component | File |
| --- | --- |
| Listening, offering and reading yes or no | `src/dcap/listener.py` |
| What a yes delivers | `Dcap.door` in `src/dcap/api.py` |
| Request resolver | `src/dcap/resolver.py` |
| Capability, goal, provider and bridge registries | `src/dcap/registry.py`, `src/dcap/definitions/` |
| Dependency resolution and wiring | `src/dcap/planner.py` |
| Execution | `src/dcap/engine.py` |
| Background observer | `src/dcap/observer.py` |
| Bridge selection and placement | `src/dcap/bridges.py` |
| Generated local code | `src/dcap/facade.py` |
| Provider scaffolding | `src/dcap/scaffold.py` |
| Plugging in an MCP server | `src/dcap/plug.py` |
| Telling the operator from the AI | `src/dcap/operator.py`, `src/dcap/guard.py` |
| Transactional injection and rollback | `src/dcap/workspace.py` |
| Code that can be copied into a workspace | `src/dcap/runtime/` |
| Live website capabilities | `src/dcap/runtime/live.py` |

## Adding to it

Put YAML files in a directory with any of the folders `capabilities/`, `goals/`, `providers/` and `bridges/`, then pass it with `--definitions <dir>`. An MCP provider maps its own tool names onto DCAP's cores, and goals that use those cores don't change:

```yaml
providers:
  - provider: example.mcp
    kind: mcp
    priority: 1
    command: [python, server.py]
    implements:
      web.link_discovery:
        contract: 1
        operation: link_crawl
        input_map: {pages: targets}
        output_key: links
```

An HTTP provider looks the same with `kind: http` and a `url`. `operation` becomes the path, and secrets are named by environment variable:

```yaml
providers:
  - provider: example.api
    kind: http
    priority: 1
    url: https://api.example.test
    headers_env: {Authorization: EXAMPLE_API_TOKEN}
    implements:
      web.link_discovery:
        contract: 1
        operation: /v1/links
        method: POST
        input_map: {pages: targets}
        output_key: links
```

`examples/swagger/` connects an MCP server this way: a small read-only OpenAPI reader that ships with the example. A command part starting with `./` is a path relative to the definition file. `before` runs other tools first in the same session, with `paths` for inputs that server needs absolute, and `text_output: true` declares a server that answers in plain text.

## Tests

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```

`tests/test_dcap.py` runs the framework against canned data. `tests/test_live.py` runs the live provider against small sites it serves on `127.0.0.1`. `tests/test_guard.py` covers what an AI is kept from, and `tests/test_plug.py` plugs in sample MCP servers. None of them needs a network or touches `~/.config/dcap`.
