#!/usr/bin/env python3
"""
Push the entire project to GitHub via the Contents API.
Skips generated/ignored files and uses the working Node bridge.
"""
import os
import sys
import json
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import github_client

OWNER = "tatiananoian87-boop"
REPO  = "DoveLoButtoAI"
BRANCH = "main"

SKIP_PREFIXES = {
    ".git", "node_modules", "artifacts/api-server/node_modules",
    "artifacts/mockup-sandbox/node_modules", "lib/db/node_modules",
    ".local", ".pythonlibs", ".npm", ".config", ".cache", "tmp",
    "artifacts/api-server/dist", "artifacts/api-server/.replit-artifact",
    "__pycache__", ".agents",
}
SKIP_EXTS = {".pyc", ".db", ".db-journal", ".db-shm", ".db-wal",
             ".tsbuildinfo", ".log", ".DS_Store"}
SKIP_NAMES = {"pnpm-lock.yaml", ".gitignore", "replit-git-askpass",
              "replit-git-editor", "waste.db"}


def should_skip(p: Path) -> bool:
    parts = p.parts
    if any(prefix in str(p) for prefix in SKIP_PREFIXES):
        return True
    if p.suffix in SKIP_EXTS:
        return True
    if p.name in SKIP_NAMES:
        return True
    return False


def main() -> None:
    root = Path(__file__).parent.parent.resolve()
    files: list[Path] = []
    for p in root.rglob("*"):
        if p.is_file() and not should_skip(p):
            files.append(p)

    files.sort()
    total = len(files)
    print(f"Found {total} files to push.")

    for i, p in enumerate(files, 1):
        rel = p.relative_to(root).as_posix()
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            # Binary file — skip for now (Contents API is text-only)
            print(f"[{i}/{total}] SKIP (binary): {rel}")
            continue

        msg = f"Add {rel}"
        try:
            github_client.create_or_update_file(
                OWNER, REPO, rel, content, msg, BRANCH
            )
            print(f"[{i}/{total}] ✅ {rel}")
        except Exception as exc:
            print(f"[{i}/{total}] ❌ {rel}: {exc}")
        # Rate-limit ourselves: 1 req/sec is well under GitHub's limit
        time.sleep(0.8)

    print("\nDone.")


if __name__ == "__main__":
    main()
