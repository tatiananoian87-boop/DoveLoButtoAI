#!/usr/bin/env python3
"""
Resume pushing the project to GitHub via the Contents API.
Skips files that already exist in the repo.
"""
import os
import sys
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
            print(f"[{i}/{total}] SKIP (binary): {rel}")
            continue

        msg = f"Add {rel}"
        try:
            github_client.create_or_update_file(
                OWNER, REPO, rel, content, msg, BRANCH
            )
            print(f"[{i}/{total}] ✅ {rel}")
        except Exception as exc:
            err = str(exc)
            if "already exists" in err or "sha" in err.lower():
                print(f"[{i}/{total}] ✅ {rel} (already there)")
            else:
                print(f"[{i}/{total}] ❌ {rel}: {err}")
        time.sleep(0.3)

    print("\nDone.")


if __name__ == "__main__":
    main()
