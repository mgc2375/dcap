"""A made-up MCP server with the kinds of tools real ones have. It does nothing but answer."""
import json
import sys

TOOLS = [
    {
        "name": "nmap_scan",
        "description": "Scan a network for open ports. Wraps the vendor's scanner with many options.",
        "inputSchema": {"type": "object", "required": ["target"], "properties": {
            "target": {"type": "string", "description": "Host or range"},
            "ports": {"type": "array", "items": {"type": "integer"}},
            "max-retries": {"type": ["integer", "null"]},
        }},
    },
    {
        "name": "whoisLookup",
        "description": "Look up who registered a domain.",
        "annotations": {"readOnlyHint": True},
        "inputSchema": {"type": "object", "required": ["domain"], "properties": {"domain": {"type": "string"}}},
    },
    {
        "name": "report",
        "title": "Summarise findings",
        "description": "Summarise the findings of the current session.",
        "inputSchema": {"type": "object", "properties": {}},
        "outputSchema": {"type": "object", "properties": {"summary": {"type": "string"}}},
    },
    {
        "name": "fetch",
        "description": "Fetch a page.",
        "inputSchema": {"type": "object", "required": ["url"], "properties": {
            "url": {"type": "string"}, "class": {"type": "string", "enum": ["fast", "full"]},
        }},
    },
]

for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    if message["method"] == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "kit", "version": "1"}}
    elif message["method"] == "tools/list":
        result = {"tools": TOOLS}
    else:
        name, args = message["params"]["name"], message["params"].get("arguments", {})
        if name == "nmap_scan":
            found = {"target": args["target"], "open": args.get("ports") or [22, 80], "retries": args.get("max-retries")}
            result = {"content": [{"type": "text", "text": json.dumps(found)}]}
        elif name == "whoisLookup":
            result = {"content": [{"type": "text", "text": f"{args['domain']} is registered to Example Ltd"}]}
        elif name == "report":
            result = {"content": [{"type": "text", "text": "ignored"}], "structuredContent": {"summary": "2 open ports"}}
        else:
            result = {"content": [{"type": "text", "text": json.dumps({"url": args["url"], "class": args.get("class")})}]}
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
