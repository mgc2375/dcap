"""A small read-only MCP server for OpenAPI and Swagger documents. Stdlib only.

It reads a document and describes it. It never calls the API the document describes.
Tools: fetch_swagger_info, list_endpoints and get_endpoint_details. Load the document
with fetch_swagger_info first, in the same session.
"""
import json
from pathlib import Path
import sys
import urllib.request

METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
TOOLS = [
    {"name": "fetch_swagger_info", "description": "Load an OpenAPI or Swagger document and describe it.",
     "inputSchema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}},
    {"name": "list_endpoints", "description": "List the endpoints of the loaded document.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "get_endpoint_details", "description": "Describe one endpoint of the loaded document.",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}, "method": {"type": "string"}},
                     "required": ["path", "method"]}},
]


def load(url):
    if url.startswith(("http://", "https://")):
        with urllib.request.urlopen(url, timeout=20) as response:
            return json.loads(response.read())
    return json.loads(Path(url.removeprefix("file://")).read_text(encoding="utf-8"))


def info(document):
    meta = document.get("info", {})
    return {
        "title": meta.get("title", ""), "version": str(meta.get("version", "")),
        "description": meta.get("description", ""), "servers": document.get("servers", []),
        "pathCount": len(document.get("paths", {})),
    }


def endpoints(document):
    found = []
    for path, operations in sorted(document.get("paths", {}).items()):
        for method in METHODS:
            operation = operations.get(method)
            if isinstance(operation, dict):
                found.append({
                    "path": path, "method": method.upper(), "summary": operation.get("summary", ""),
                    "operationId": operation.get("operationId", ""), "tags": operation.get("tags", []),
                })
    return found


def details(document, path, method):
    operation = document.get("paths", {}).get(path, {}).get(method.lower())
    if not isinstance(operation, dict):
        raise LookupError(f"no {method.upper()} {path} in the document")
    return {"path": path, "method": method.upper(), **operation}


def main():
    document = None
    for line in sys.stdin:
        message = json.loads(line)
        if "id" not in message:
            continue
        reply = {"jsonrpc": "2.0", "id": message["id"]}
        method = message.get("method")
        if method == "initialize":
            reply["result"] = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                               "serverInfo": {"name": "openapi-reader", "version": "1"}}
        elif method == "tools/list":
            reply["result"] = {"tools": TOOLS}
        elif method == "tools/call":
            name, args = message["params"]["name"], message["params"].get("arguments", {})
            try:
                if name == "fetch_swagger_info":
                    document = load(args["url"])
                    result = info(document)
                elif document is None:
                    raise RuntimeError("call fetch_swagger_info first")
                elif name == "list_endpoints":
                    result = endpoints(document)
                elif name == "get_endpoint_details":
                    result = details(document, args["path"], args["method"])
                else:
                    raise LookupError(f"unknown tool {name}")
                reply["result"] = {"content": [{"type": "text", "text": json.dumps(result)}]}
            except Exception as exc:  # reported to the caller as a tool error
                reply["result"] = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        else:
            reply["error"] = {"code": -32601, "message": f"unknown method {method}"}
        print(json.dumps(reply), flush=True)


if __name__ == "__main__":
    main()
