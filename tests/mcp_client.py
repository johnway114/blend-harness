from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from conftest import REPOSITORY


class McpClient:
    def __init__(self) -> None:
        self.process = subprocess.Popen(
            [sys.executable, "-m", "blend_harness.mcp_server"],
            cwd=REPOSITORY,
            env=os.environ.copy(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.next_id = 1
        self.notifications: list[dict[str, Any]] = []
        initialized = self.request("initialize", {"protocolVersion": "2025-06-18"})
        assert initialized["result"]["serverInfo"]["name"] == "blend"
        self.notify("notifications/initialized", {})

    def _send(self, value: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(value, separators=(",", ":")) + "\n")
        self.process.stdin.flush()

    def _receive(self, request_id: int) -> dict[str, Any]:
        assert self.process.stdout is not None
        while True:
            line = self.process.stdout.readline()
            if not line:
                stderr = self.process.stderr.read() if self.process.stderr else ""
                raise AssertionError(f"MCP server exited before response {request_id}: {stderr}")
            message = json.loads(line)
            if message.get("id") == request_id:
                return message
            if message.get("method", "").startswith("notifications/"):
                self.notifications.append(message)

    def begin(self, method: str, params: dict[str, Any]) -> int:
        request_id = self.next_id
        self.next_id += 1
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        return request_id

    def finish(self, request_id: int) -> dict[str, Any]:
        return self._receive(request_id)

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return self.finish(self.begin(method, params))

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        response = self.request("tools/call", {"name": name, "arguments": arguments})
        assert "error" not in response, response
        return response["result"]

    def close(self) -> None:
        if self.process.poll() is not None:
            return
        response = self.request("shutdown", {})
        assert response["result"] == {}
        self.notify("exit", {})
        assert self.process.stdin is not None
        self.process.stdin.close()
        self.process.wait(timeout=10)
        stderr = self.process.stderr.read() if self.process.stderr else ""
        assert self.process.returncode == 0, stderr

    def __enter__(self) -> "McpClient":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self.process.poll() is None:
            try:
                self.close()
            except Exception:
                self.process.kill()
                self.process.wait(timeout=5)
                if exc is None:
                    raise
