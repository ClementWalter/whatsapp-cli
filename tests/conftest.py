"""Pytest fixtures — notably the whatsmeow oracle subprocess.

The oracle is a Go binary (`tools/oracle/oracle`) that exposes whatsmeow's
internals as a stdio JSON service. Tests compare Python outputs against the
oracle byte-for-byte. See tools/oracle/main.go for the protocol.
"""

from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path

import pytest

SKILL_ROOT = Path(__file__).resolve().parent.parent
ORACLE_BIN = SKILL_ROOT / "tools" / "oracle" / "oracle"


class Oracle:
    """Bidirectional JSON-line client for the whatsmeow oracle subprocess."""

    def __init__(self, proc: subprocess.Popen[bytes]) -> None:
        self._proc = proc

    def _call(self, op: str, arg: dict | None = None) -> dict:
        req = {"op": op}
        if arg is not None:
            req["arg"] = arg
        line = (json.dumps(req) + "\n").encode()
        assert self._proc.stdin is not None
        assert self._proc.stdout is not None
        self._proc.stdin.write(line)
        self._proc.stdin.flush()
        raw = self._proc.stdout.readline()
        if not raw:
            stderr = self._proc.stderr.read() if self._proc.stderr else b""
            raise RuntimeError(f"oracle died: {stderr.decode(errors='replace')}")
        resp = json.loads(raw)
        if "error" in resp and resp["error"]:
            raise RuntimeError(f"oracle error for op={op}: {resp['error']}")
        return resp["ok"]

    def ping(self) -> bool:
        return self._call("ping").get("pong") is True

    def encode_node(self, node_json: dict) -> bytes:
        result = self._call("encode_node", {"node": node_json})
        return base64.b64decode(result["bytes"])

    def decode_node(self, data: bytes) -> dict:
        result = self._call("decode_node", {"bytes": base64.b64encode(data).decode()})
        return result["node"]

    def lthash_apply(self, base: bytes, sub: list[bytes], add: list[bytes]) -> bytes:
        b64 = lambda b: base64.b64encode(b).decode()
        result = self._call(
            "lthash_apply",
            {"base": b64(base), "sub": [b64(x) for x in sub], "add": [b64(x) for x in add]},
        )
        return base64.b64decode(result["result"])

    def derive_media_keys(self, media_key: bytes, app_info: str) -> dict[str, bytes]:
        result = self._call(
            "derive_media_keys",
            {"media_key": base64.b64encode(media_key).decode(), "app_info": app_info},
        )
        return {k: base64.b64decode(v) for k, v in result.items()}


@pytest.fixture(scope="session")
def oracle() -> "Oracle":
    """One oracle process shared across the test session."""
    if not ORACLE_BIN.exists():
        pytest.skip(f"oracle binary not built: run `cd {ORACLE_BIN.parent} && go build .`")
    proc = subprocess.Popen(
        [str(ORACLE_BIN)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    client = Oracle(proc)
    # Sanity check the pipe before any test runs.
    assert client.ping()
    yield client
    if proc.stdin:
        proc.stdin.close()
    proc.wait(timeout=5)
