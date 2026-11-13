"""An MCP server that answers in plain text, and only after `open` in the same session."""
import json
import sys

opened = None
for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    reply = {"jsonrpc": "2.0", "id": message["id"]}
    if message["method"] == "initialize":
        reply["result"] = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "t", "version": "1"}}
    elif message["params"]["name"] == "open":
        opened = message["params"]["arguments"]["path"]
        reply["result"] = {"content": [{"type": "text", "text": "File opened."}]}
    else:
        reply["result"] = {"content": [{"type": "text", "text": f"0x1 main of {opened}\n"}]}
    print(json.dumps(reply), flush=True)
