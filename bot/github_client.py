"""
GitHub helper for EcoGlass Bot — uses the Node.js Replit connectors bridge.

The bridge (bot/_github_bridge.js) handles OAuth token negotiation via the
@replit/connectors-sdk. This module shells out to it, parses JSON output,
and never touches auth tokens directly.
"""

import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_BRIDGE = str(Path(__file__).with_name("_github_bridge.js"))


def _call(method: str, endpoint: str, body: dict[str, Any] | None = None) -> Any:
    """Shell out to the Node bridge and return the parsed response body."""
    cmd = ["node", _BRIDGE, method, endpoint]
    if body is not None:
        cmd.append(json.dumps(body, ensure_ascii=False))
    else:
        cmd.append("")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            logger.error("[github] Bridge error: %s", stderr or result.stdout.strip())
            raise RuntimeError(f"GitHub bridge error: {stderr or 'unknown'}")

        parsed = json.loads(result.stdout.strip())
        if "error" in parsed:
            logger.error("[github] API error: %s", parsed["error"])
            raise RuntimeError(f"GitHub API error: {parsed['error']}")

        status = parsed.get("status", 0)
        if status >= 400:
            logger.error("[github] HTTP %d %s: %s", status, endpoint, parsed.get("body", ""))
            raise RuntimeError(f"GitHub HTTP {status} for {endpoint}")

        logger.info("[github] %s %s → %d", method, endpoint, status)
        return parsed.get("body")
    except subprocess.TimeoutExpired:
        logger.error("[github] Bridge timeout: %s %s", method, endpoint)
        raise


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def get_user() -> dict[str, Any]:
    return _call("GET", "/user")


def list_repos(page: int = 1, per_page: int = 30) -> list[dict[str, Any]]:
    return _call("GET", f"/user/repos?sort=updated&page={page}&per_page={per_page}")


def create_repo(name: str, description: str = "", private: bool = False) -> dict[str, Any]:
    return _call(
        "POST", "/user/repos",
        {"name": name, "description": description, "private": private, "auto_init": True},
    )


def get_repo(owner: str, repo: str) -> dict[str, Any]:
    return _call("GET", f"/repos/{owner}/{repo}")


def create_or_update_file(
    owner: str,
    repo: str,
    path: str,
    content: str,
    message: str,
    branch: str = "main",
) -> dict[str, Any]:
    """Create or update a single file. Content is plain text (base64-encoded internally)."""
    import base64
    encoded = base64.b64encode(content.encode("utf-8")).decode()

    # Try to fetch existing file to get SHA for update
    sha: str | None = None
    try:
        existing = _call("GET", f"/repos/{owner}/{repo}/contents/{path}?ref={branch}")
        if isinstance(existing, dict) and "sha" in existing:
            sha = existing["sha"]
    except Exception:
        pass

    body: dict[str, Any] = {"message": message, "content": encoded, "branch": branch}
    if sha:
        body["sha"] = sha

    return _call("PUT", f"/repos/{owner}/{repo}/contents/{path}", body)
