"""An MCP server that only answers `read` after `load`, in the same session."""
import json
import sys

loaded = None
for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    reply = {"jsonrpc": "2.0", "id": message["id"]}
    if message["method"] == "initialize":
        reply["result"] = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "s", "version": "1"}}
    elif message["params"]["name"] == "load":
        loaded = message["params"]["arguments"]["source"]
        if loaded == "broken":
            reply["result"] = {"content": [{"type": "text", "text": "cannot load"}], "isError": True}
        else:
            reply["result"] = {"content": [{"type": "text", "text": "{}"}]}
    elif loaded is None:
        reply["error"] = {"code": -32603, "message": "call load first"}
    else:
        assert "source" not in message["params"]["arguments"]
        reply["result"] = {"content": [{"type": "text", "text": json.dumps([{"address": loaded}])}]}
    print(json.dumps(reply), flush=True)
