"""Pre-commit guard for agent runs.

Scheduled runs may only change memory files (other than the strategy rulebook).
Code, config, prompts, the strategy and this guard are human-owned.
"""

from __future__ import annotations

import subprocess

from xahil.config import ROOT

AGENT_WRITABLE_PREFIXES = ("memory/",)
HUMAN_ONLY_FILES = {"memory/TRADING-STRATEGY.md", "memory/PROJECT-CONTEXT.md"}
FORBIDDEN_NAMES = (".env",)


def classify(paths: list) -> list:
    """Return the paths an agent run is not allowed to commit."""
    bad = []
    for p in paths:
        p = p.replace("\\", "/").strip()
        if not p:
            continue
        name = p.rsplit("/", 1)[-1]
        if name in FORBIDDEN_NAMES or name.startswith(".env."):
            bad.append(p)
        elif p in HUMAN_ONLY_FILES:
            bad.append(p)
        elif not p.startswith(AGENT_WRITABLE_PREFIXES):
            bad.append(p)
    return bad


def changed_paths() -> list:
    out = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout
    paths = []
    for line in out.splitlines():
        entry = line[3:]
        if " -> " in entry:  # rename: check both sides
            paths.extend(entry.split(" -> "))
        else:
            paths.append(entry)
    return [p.strip('"') for p in paths]
