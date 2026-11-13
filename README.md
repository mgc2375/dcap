# Dynamic Capability Adaptive Protocol (DCAP)

**DCAP: the solution to the Model Context Protocol.**

> **Work in progress.** DCAP is under active development. See [Status](#status) for what works today and what's next.

DCAP gives an AI coding agent any capability it asks for, without the AI ever loading a tool definition, choosing a tool or learning what tools exist. The AI says what it wants in its own words. DCAP picks the implementation, wires it into the AI's own code and hands over one plainly named switch, such as `SCAN_NETWORK = "false"`. The AI turns it on and uses it.

## What DCAP does

DCAP replaces "which tool do I use, and how do I call it?" with "this is what I want to do."

An AI working on a task states its intent in plain words, the way it already does: *I need to scan the network.* From there:

- **DCAP decides.** It works out whether it has something for that intent. The AI doesn't search, compare or choose.
- **DCAP adapts a capability to the task.** It selects an implementation, wires up everything that implementation needs, and fits it into the code the AI is already writing.
- **The AI gets the use, not the tool.** It receives one plainly named switch, `scan_network`, off by default. What sits behind it, whether that's nmap, an API or a chain of five steps, is never the AI's concern.
- **The AI carries on.** It recognises what it asked for, turns it on and uses it.

The AI never loads a tool definition, never learns what tools exist and never has to understand one. Capability arrives at the moment it's needed, in the shape of the request, and costs nothing the rest of the time.

## Why it's adaptive

A tool is a fixed thing the AI has to adapt to: learn its name, learn its schema, work out when it applies. DCAP reverses that. The capability adapts to the AI.

- **To the intent.** It starts from what the AI said it wants, not from a tool's name. A specific need gets exactly one switch, and a broad one gets the few that belong to that topic.
- **To the code.** It lands where the AI is working and takes the shape of the code around it: the file in hand, a function already waiting to be filled, names that don't collide.
- **To what's behind it.** Any tool can sit behind a plain use. Tools can be added, swapped or fail over to another, and the AI's side stays the same.
- **To change.** When the tools behind a capability move on, the pasted capability is brought forward with them. The name the AI knows doesn't change.

The intent stays fixed and everything else moves to meet it. That is the whole idea: the AI asked to scan a network, so it has `scan_network`.

## Why it kills regular MCP

Regular MCP works by describing every tool to the model. That design has costs DCAP doesn't pay:

- **MCP spends context before any work starts.** Every connected server's tool list (names, descriptions and input schemas) sits in the context window whether or not a tool is used. A few servers with dozens of tools can cost thousands of tokens on every request. DCAP loads nothing, and a delivered capability costs under 70 tokens.
- **MCP makes the model manage tools.** The model has to work out which tool does what, pick one and build the call. DCAP does the picking, deterministically, and the wiring is already done.
- **MCP shows the model the vendor.** It gets `nmap_scan` and has to know that nmap is how you scan a network. DCAP shows the use.
- **MCP adds more cost with every server.** Each new server makes the context bigger and the choice harder. With DCAP, plugging in a hundred servers costs the AI nothing until it asks for something.

| | Regular MCP | DCAP |
| --- | --- | --- |
| Loaded into context up front | Every connected server's tool list | Nothing |
| Cost of adding another server | More context on every request | None |
| How a tool is chosen | The model reads the definitions and picks | DCAP matches the AI's own words, deterministically |
| What the model sees | Tool names, descriptions and schemas | One line asking yes or no, then the pasted switch |
| Naming | The vendor's (`nmap_scan`) | The use (`scan_network`) |
| Wiring several steps together | The model's job | Done behind the switch |
| Who allows a tool to run | Depends on the client | The operator's policy, per permission and host |

DCAP doesn't throw the MCP ecosystem away. Every existing MCP server works with it: plug one in and its tools become DCAP switches. To see what a server's tool list costs a model when loaded the regular way, run `dcap context -- <server command>`.

## How it works

1. **The AI says what it wants.** DCAP listens to what the AI natively says and writes. There is no tool, instruction or command for the AI to learn.
2. **DCAP decides.** If it recognises something it can deliver, it raises its hand in one line: `DCAP: want help with check site links? Yes or no.` Otherwise it stays silent.
3. **The AI answers in its normal reply.** On no, or if it carries on without answering, nothing is written and that offer isn't made again.
4. **On yes, DCAP pastes the code.** It goes into the file the AI is working on, with the switch off:

```python
# dcap:begin idf3ac29168
# app.py
import os
from dcap_capabilities.web_site_links import run as check_site_links
os.environ["CHECK_SITE_LINKS"] = "false"  # check_site_links(target, depths=None)
# dcap:end idf3ac29168
```

5. **The AI turns it on and calls it.** Everything behind that one name is already wired. Calling it while it's off raises `CHECK_SITE_LINKS is off. Set "CHECK_SITE_LINKS" to "true".`

That paste is the first and only time the AI sees anything DCAP has.

## Status

DCAP is a work in progress and under active development.

**Working today** (294 tests, none needing a network):

- The framework: matching a request, wiring dependencies, running them, and pasting code into a project transactionally, with release and update.
- Real website capabilities: crawling a site, checking its links, reading metadata, and building a sitemap and report.
- `dcap plug`, which wires up an MCP server from its tool list.
- The lock that keeps an AI away from DCAP's commands, definitions and state.
- Adapters for Claude Code, Codex and opencode.

**Next:**

- Live runs with more agents. Codex has run end to end, and Claude Code and opencode are next.
- Plugging in more real MCP servers and tuning the names it gives their tools.
- Matching more ways of saying the same request.
- Pasting into languages other than Python.
- Publishing as a package.

Design choices and current limits are in [docs/DECISIONS.md](docs/DECISIONS.md).

## Getting started

Install it from this folder:

```bash
pip install -e .
```

Check the definitions load:

```bash
dcap validate
```

Check a site's links yourself, as the operator. The grant names the host DCAP may contact, and nothing is requested without one:

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

## A broad request

When the AI asks for something broad, DCAP pastes the handful of switches for that one topic. Saying "I want to investigate my website" and then yes pastes this:

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

Each one is its own module with its own switch, and using one loads nothing else. If the project already has an empty stub that clearly fits, such as `def inspect_site(target): pass`, DCAP connects that instead of adding a block.

DCAP stays silent the rest of the time, including while the AI works on its own code. If it can't deliver, the AI sees nothing and nothing is written. Failures go to `.dcap/door.log` for the operator.

## What the AI can't do

The AI can't ask DCAP what it has. It sees a switch only in pasted code, after its own request. Two things enforce that:

- **Operator commands answer only an operator.** They need a terminal, which an agent's shell doesn't have. Run from an agent, every operator command prints nothing and exits 1, including `--help`. For a script, make a key once with `dcap operator-key` and pass it for that command as `DCAP_OPERATOR=<key>`.
- **The hook turns the AI's tool calls away.** With `PreToolUse` set up, Claude Code blocks any call that runs `dcap`, imports the `dcap` package, sets `DCAP_POLICY`, or reads or writes DCAP's definitions, its `.dcap/` state, the policy file or the operator key. The AI is told only "That isn't available here." Refusals are logged to `.dcap/door.log`.

The pasted code and `dcap_capabilities/` stay open to the AI, because that is what it was given. Other agents can use `dcap guard`, which reads `{"input": {...}}` and exits 2 if the call must not run.

## Permissions

Grants live outside the project, in `~/.config/dcap/policy.json` or the file named by `$DCAP_POLICY`, and the AI can't change them:

```json
{"grants": {"web.fetch": ["127.0.0.0/8", "10.0.0.0/24", "*.example.test"]}}
```

For a single command, use `--grant web.fetch=10.0.0.0/24`.

A target can carry a port, such as `127.0.0.1:8000`, and the grant is for the host. A link or redirect that leaves the granted hosts is reported as `skipped` or `redirect` and never requested.

## Built-in capabilities

DCAP ships with one topic, websites, and it runs live by default. The live provider, `src/dcap/runtime/live.py`, uses only Python's standard library:

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

Draft a provider for capabilities that already exist, from a live MCP server, ready for review:

```bash
dcap scaffold-provider my.provider -- python server.py > definitions/providers/my.yaml
```

## Writing definitions by hand

`dcap plug` writes definitions for you. To write your own, put YAML files in a directory with any of the folders `capabilities/`, `goals/`, `providers/` and `bridges/`, then pass it with `--definitions <dir>`. An MCP provider maps its own tool names onto DCAP's cores, and goals that use those cores don't change:

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
| Code that can be copied into a project | `src/dcap/runtime/` |
| Live website capabilities | `src/dcap/runtime/live.py` |

## Tests

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```

`tests/test_dcap.py` runs the framework against canned data. `tests/test_live.py` runs the live provider against small sites it serves on `127.0.0.1`. `tests/test_guard.py` covers what an AI is kept from, and `tests/test_plug.py` plugs in sample MCP servers. None of them needs a network or touches `~/.config/dcap`.
