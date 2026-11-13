"""Minimal MCP server whose naming deliberately differs from DCAP's vocabulary."""
import json
import sys

TOOLS = [
    {
        "name": "link_crawl",
        "description": "Find links on pages and return everything found, with long vendor documentation.",
        "inputSchema": {"type": "object", "properties": {
            "targets": {"type": "array", "description": "Pages to crawl"},
            "pages": {"type": "array", "description": "Alias of targets"},
        }},
    },
    {
        "name": "rotate_vendor_logs",
        "description": "Rotate the vendor's internal logs.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    if message["method"] == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "1"}}
    elif message["method"] == "tools/list":
        result = {"tools": TOOLS}
    else:
        args = message["params"]["arguments"]
        found = [{"address": h["address"], "depth": 9999, "kind": "internal"} for h in args["targets"]]
        result = {"content": [{"type": "text", "text": json.dumps({"open": found})}]}
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
