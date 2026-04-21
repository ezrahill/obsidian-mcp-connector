#!/usr/bin/env python3
"""Automated MCP verifier for the binary-content path of `get_vault_file`.

Spawns `bun src/index.ts` as a subprocess, speaks MCP JSON-RPC over stdio,
invokes `get_vault_file` for each fixture and asserts the expected content
shape. Exits 0 on full PASS, 1 on any failure.

Platform: developed on macOS. The MCP client logic is portable; only the
fallback path that auto-discovers the API key from Obsidian's config
directory uses the macOS layout (~/Library/Application Support/obsidian).
On other platforms, export OBSIDIAN_API_KEY manually.

Prerequisites:
  - Fixtures already uploaded to the vault (run scripts/smoke-test-binary.sh upload)
  - Obsidian running with a vault open and Local REST API plugin active
  - Either OBSIDIAN_API_KEY exported, OR the macOS auto-discovery fallback
    can locate the data.json of the currently-open vault.
  - OBSIDIAN_API_URL (optional, defaults to https://127.0.0.1:27124)

Usage:
    python3 scripts/smoke-verify-binary.py [--prefix DIR]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_ENTRY = REPO_ROOT / "packages" / "mcp-server" / "src" / "index.ts"
DEFAULT_PREFIX = "smoke-test-binary-pr2"

# macOS-specific location of Obsidian's per-user vault registry. Used as a
# fallback when OBSIDIAN_API_KEY is not set in the environment — we look up
# the vault currently marked `open: true` and read the Local REST API
# plugin's data.json from inside it. On Linux/Windows this path differs, so
# callers on those platforms must export OBSIDIAN_API_KEY explicitly.
MACOS_OBSIDIAN_REGISTRY = (
    Path.home() / "Library" / "Application Support" / "obsidian" / "obsidian.json"
)
LOCAL_REST_API_DATA_REL = Path(
    ".obsidian/plugins/obsidian-local-rest-api/data.json"
)

# ANSI colors
GREEN = "\033[1;32m"
RED = "\033[1;31m"
YELLOW = "\033[1;33m"
BLUE = "\033[1;34m"
RESET = "\033[0m"


def log(msg: str) -> None:
    print(f"{BLUE}[verify]{RESET} {msg}", flush=True)


def discover_api_key_macos() -> tuple[str, Path] | None:
    """Read the API key from the currently-open vault's plugin data.json.

    Returns (api_key, vault_path) on success, None if the Obsidian registry
    or the plugin data file is missing. Deliberately silent on errors — the
    caller falls back to raising on missing env.
    """
    if not MACOS_OBSIDIAN_REGISTRY.is_file():
        return None
    try:
        registry = json.loads(MACOS_OBSIDIAN_REGISTRY.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    for entry in (registry.get("vaults") or {}).values():
        if not entry.get("open"):
            continue
        vault_path = Path(entry.get("path", ""))
        data_json = vault_path / LOCAL_REST_API_DATA_REL
        if not data_json.is_file():
            continue
        try:
            data = json.loads(data_json.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        key = data.get("apiKey")
        if isinstance(key, str) and key:
            return key, vault_path
    return None


def ok(msg: str) -> None:
    print(f"  {GREEN}✓ PASS{RESET} {msg}", flush=True)


def fail(msg: str) -> None:
    print(f"  {RED}✗ FAIL{RESET} {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"  {YELLOW}!{RESET} {msg}", flush=True)


class McpClient:
    """Minimal MCP client over stdio. No reconnect logic, single-shot only."""

    def __init__(self, cmd: list[str], env: dict[str, str]) -> None:
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            bufsize=0,
        )
        self._id_counter = 0
        self._stderr_buf: list[str] = []
        # Drain stderr in background so the server does not block on a full
        # pipe while we wait on stdout.
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, daemon=True
        )
        self._stderr_thread.start()

    def _drain_stderr(self) -> None:
        assert self.proc.stderr is not None
        for line in iter(self.proc.stderr.readline, b""):
            self._stderr_buf.append(line.decode("utf-8", errors="replace"))

    def _next_id(self) -> int:
        self._id_counter += 1
        return self._id_counter

    def _send(self, payload: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        data = json.dumps(payload).encode("utf-8") + b"\n"
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def _recv(self, timeout: float = 30.0) -> dict[str, Any]:
        assert self.proc.stdout is not None
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                if self.proc.poll() is not None:
                    raise RuntimeError(
                        f"server exited with code {self.proc.returncode}. "
                        f"stderr tail:\n{''.join(self._stderr_buf[-40:])}"
                    )
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                # Server may emit banner lines before JSON-RPC starts; skip.
                continue
        raise TimeoutError("no response from server within timeout")

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        rid = self._next_id()
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        # The server may interleave notifications; skip until we find our response.
        while True:
            resp = self._recv()
            if resp.get("id") == rid:
                return resp

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def handshake(self) -> None:
        resp = self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "smoke-verify-binary", "version": "0.0.1"},
            },
        )
        if "error" in resp:
            raise RuntimeError(f"initialize failed: {resp['error']}")
        self.notify("notifications/initialized")

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        resp = self.request(
            "tools/call", {"name": name, "arguments": arguments}
        )
        if "error" in resp:
            raise RuntimeError(f"tools/call error: {resp['error']}")
        return resp["result"]

    def close(self) -> None:
        try:
            if self.proc.stdin and not self.proc.stdin.closed:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()

    def stderr_tail(self, lines: int = 20) -> str:
        return "".join(self._stderr_buf[-lines:])


def check_inline(
    result: dict[str, Any],
    *,
    expected_type: str,
    expected_mime_prefix: str,
) -> tuple[bool, str]:
    content = result.get("content")
    if not isinstance(content, list) or len(content) != 1:
        return False, f"expected content list of length 1, got {content!r}"
    block = content[0]
    if block.get("type") != expected_type:
        return False, f"expected type={expected_type!r}, got {block.get('type')!r}"
    if "data" not in block or not isinstance(block["data"], str) or len(block["data"]) < 32:
        return (
            False,
            f"expected non-empty base64 data, got {len(block.get('data','')) if isinstance(block.get('data'),str) else type(block.get('data'))}",
        )
    mime = block.get("mimeType", "")
    if not mime.startswith(expected_mime_prefix):
        return False, f"expected mimeType starting with {expected_mime_prefix!r}, got {mime!r}"
    return True, f"type={expected_type}, mimeType={mime}, data={len(block['data'])} base64 chars"


def check_metadata_fallback(
    result: dict[str, Any],
    *,
    expected_mime: str,
    expected_hint_substr: str,
) -> tuple[bool, str]:
    content = result.get("content")
    if not isinstance(content, list) or len(content) != 1:
        return False, f"expected content list of length 1, got {content!r}"
    block = content[0]
    if block.get("type") != "text":
        return False, f"expected type='text' (fallback), got {block.get('type')!r}"
    text = block.get("text", "")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return False, f"text payload is not valid JSON: {text[:200]!r}"
    if payload.get("kind") != "binary_file":
        return False, f"expected kind='binary_file', got {payload.get('kind')!r}"
    if payload.get("mimeType") != expected_mime:
        return False, f"expected mimeType={expected_mime!r}, got {payload.get('mimeType')!r}"
    hint = payload.get("hint", "")
    if expected_hint_substr not in hint:
        return False, f"expected hint to contain {expected_hint_substr!r}, got {hint!r}"
    return True, f"kind=binary_file, mimeType={expected_mime}, hint contains {expected_hint_substr!r}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", default=os.environ.get("VAULT_PREFIX", DEFAULT_PREFIX))
    args = parser.parse_args()

    api_key = os.environ.get("OBSIDIAN_API_KEY")
    if not api_key:
        discovered = discover_api_key_macos()
        if discovered is None:
            print(
                f"{RED}OBSIDIAN_API_KEY not set and macOS auto-discovery failed.{RESET} "
                f"Export it manually before running.",
                file=sys.stderr,
            )
            return 2
        api_key, vault_path = discovered
        log(f"API key auto-discovered from {vault_path}")

    api_url = os.environ.get("OBSIDIAN_API_URL", "https://127.0.0.1:27124")

    log(f"Server entry: {SERVER_ENTRY}")
    log(f"API URL: {api_url}")
    log(f"Vault prefix: {args.prefix}")

    env = os.environ.copy()
    env["OBSIDIAN_API_KEY"] = api_key
    env["OBSIDIAN_API_URL"] = api_url

    client = McpClient(["bun", str(SERVER_ENTRY)], env=env)
    try:
        log("Handshake …")
        client.handshake()
        ok("initialize + initialized")

        cases: list[dict[str, Any]] = [
            {
                "name": "PNG small — inline image block",
                "filename": f"{args.prefix}/image-small.png",
                "validator": lambda r: check_inline(
                    r, expected_type="image", expected_mime_prefix="image/"
                ),
            },
            {
                "name": "M4A small — inline audio block",
                "filename": f"{args.prefix}/audio-small.m4a",
                "validator": lambda r: check_inline(
                    r, expected_type="audio", expected_mime_prefix="audio/"
                ),
            },
            {
                "name": "MP4 video — metadata fallback (unsupported_type)",
                "filename": f"{args.prefix}/video-fake.mp4",
                "validator": lambda r: check_metadata_fallback(
                    r, expected_mime="video/mp4", expected_hint_substr="cannot be returned"
                ),
            },
            {
                "name": "PDF — metadata fallback (unsupported_type)",
                "filename": f"{args.prefix}/document-fake.pdf",
                "validator": lambda r: check_metadata_fallback(
                    r,
                    expected_mime="application/pdf",
                    expected_hint_substr="cannot be returned",
                ),
            },
            {
                "name": "PNG oversize — metadata fallback (too_large)",
                "filename": f"{args.prefix}/image-oversize.png",
                "validator": lambda r: check_metadata_fallback(
                    r, expected_mime="image/png", expected_hint_substr="too large"
                ),
            },
        ]

        passed = 0
        failed_names: list[str] = []

        for idx, case in enumerate(cases, start=1):
            log(f"Case {idx}: {case['name']}")
            try:
                result = client.call_tool(
                    "get_vault_file", {"filename": case["filename"]}
                )
            except Exception as exc:
                fail(f"tools/call raised: {exc}")
                failed_names.append(case["name"])
                continue

            ok_flag, detail = case["validator"](result)
            if ok_flag:
                ok(detail)
                passed += 1
            else:
                fail(detail)
                failed_names.append(case["name"])

        print()
        log(f"Summary: {passed}/{len(cases)} passed")
        if failed_names:
            print(f"{RED}Failed cases:{RESET}")
            for n in failed_names:
                print(f"  - {n}")
            tail = client.stderr_tail(30)
            if tail.strip():
                print(f"\n{YELLOW}Server stderr tail:{RESET}\n{tail}")
            return 1
        print(f"{GREEN}All cases passed.{RESET}")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
