import io
import json

import pytest

from sozai.protocol import (METHOD_NOT_FOUND, PROTOCOL_VERSION_KEY, SERVER_INFO_KEY, UNSUPPORTED_PROTOCOL_VERSION,
                            StdioServer, Tool, ToolError, check_arguments)

SCHEMA = {"type": "object", "properties": {
    "q": {"type": "string"},
    "n": {"type": "integer", "minimum": 1, "maximum": 5},
    "kind": {"anyOf": [{"type": "string", "enum": ["a", "b"]}, {"type": "array", "items": {"type": "string", "enum": ["a", "b"]}}]},
}, "required": ["q"]}


def echo(q, n=1, kind=None):
    if q == "boom":
        raise ToolError("asked to fail")
    return {"q": q, "n": n, "kind": kind}


@pytest.fixture
def server():
    return StdioServer("t", "0.0", "hello", [Tool("echo", "Echo.", SCHEMA, echo)])


def call(server, method, params=None, id=1):
    message = {"jsonrpc": "2.0", "id": id, "method": method}
    if params is not None:
        message["params"] = params
    return server.handle(message)


def test_check_arguments():
    assert check_arguments({"q": "x", "n": 2.0, "kind": ["a"]}, SCHEMA) == {"q": "x", "n": 2, "kind": ["a"]}
    for bad, words in (({}, "missing"), ({"q": 1}, "string"), ({"q": "x", "n": 9}, "at most"),
                       ({"q": "x", "n": True}, "boolean"), ({"q": "x", "kind": "c"}, "one of"),
                       ({"q": "x", "kind": ["a", "c"]}, "allowed"), ({"q": "x", "zzz": 1}, "unknown")):
        with pytest.raises(ToolError, match=words):
            check_arguments(bad, SCHEMA)


def test_handshake_era(server):
    init = call(server, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})["result"]
    assert init["protocolVersion"] == "2025-06-18" and init["instructions"] == "hello"
    assert call(server, "initialize", {"protocolVersion": "1999-01-01"})["result"]["protocolVersion"] == "2025-11-25"
    listed = call(server, "tools/list")["result"]
    assert listed["tools"][0]["name"] == "echo" and "resultType" not in listed


def test_modern_era_stamps_results(server):
    meta = {"_meta": {PROTOCOL_VERSION_KEY: "2026-07-28"}}
    discover = call(server, "server/discover", meta)["result"]
    assert "2026-07-28" in discover["supportedVersions"] and discover["resultType"] == "complete"
    assert discover["_meta"][SERVER_INFO_KEY] == {"name": "t", "version": "0.0"}
    assert discover["ttlMs"] == 0 and discover["cacheScope"] == "private"
    assert call(server, "tools/list", meta)["result"]["cacheScope"] == "private"
    result = call(server, "tools/call", {"name": "echo", "arguments": {"q": "x"}, **meta})["result"]
    assert result["structuredContent"] == {"q": "x", "n": 1, "kind": None} and result["resultType"] == "complete"


def test_errors(server):
    assert call(server, "nope")["error"]["code"] == METHOD_NOT_FOUND
    unsupported = call(server, "tools/list", {"_meta": {PROTOCOL_VERSION_KEY: "2099-01-01"}})["error"]
    assert unsupported["code"] == UNSUPPORTED_PROTOCOL_VERSION and "2026-07-28" in unsupported["data"]["supported"]
    failed = call(server, "tools/call", {"name": "echo", "arguments": {"q": "boom"}})["result"]
    assert failed["isError"] and failed["content"][0]["text"] == "asked to fail"
    assert call(server, "tools/call", {"name": "missing"})["error"]["code"] == -32602
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_run_loop(server):
    lines = [json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}), "", "not json",
             json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})]
    out = io.BytesIO()
    server.run(io.BytesIO("\n".join(lines).encode()), out)
    responses = [json.loads(line) for line in out.getvalue().splitlines()]
    assert responses[0] == {"jsonrpc": "2.0", "id": 1, "result": {}}
    assert responses[1]["error"]["code"] == -32700 and len(responses) == 2
