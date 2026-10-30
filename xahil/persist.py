"""Safe Git persistence for scheduled runs.

* Only memory/ is ever staged (the guard refuses anything else).
* Push to main; on rejection, `git pull --rebase` and retry. Never force-push.
* Append-only logs merge with Git's built-in union driver (.gitattributes);
  any other conflict aborts the rebase and fails loudly instead of guessing.
* After pushing, verifies that origin/main contains the local commit.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from xahil import guard
from xahil.config import ROOT


class PersistError(RuntimeError):
    pass


def _git(repo: Path, *args, check=True) -> subprocess.CompletedProcess:
    res = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    if check and res.returncode != 0:
        raise PersistError(f"git {' '.join(args)} failed: {(res.stderr or res.stdout).strip()[:300]}")
    return res


def changed_paths(repo: Path) -> list:
    out = _git(repo, "status", "--porcelain", "--untracked-files=all").stdout
    paths = []
    for line in out.splitlines():
        entry = line[3:]
        paths.extend(entry.split(" -> ") if " -> " in entry else [entry])
    return [p.strip('"') for p in paths]


def persist(message: str, repo: Path = ROOT, remote: str = "origin", branch: str = "main",
            max_attempts: int = 3, dry_run: bool = False) -> dict:
    if not message.strip():
        raise PersistError("commit message required")
    blocked = guard.classify(changed_paths(repo))
    if blocked:
        raise PersistError(f"guard blocked non-memory changes: {blocked}")
    if dry_run:
        return {"dry_run": True, "would_stage": [p for p in changed_paths(repo) if p.startswith("memory/")]}
    _git(repo, "add", "--", "memory/")
    staged = _git(repo, "diff", "--cached", "--name-only").stdout.split()
    if not staged:
        return {"committed": False, "pushed": False, "reason": "nothing to commit"}
    _git(repo, "commit", "-m", message)
    for attempt in range(1, max_attempts + 1):
        push = _git(repo, "push", remote, f"HEAD:{branch}", check=False)
        if push.returncode == 0:
            break
        rebase = _git(repo, "pull", "--rebase", remote, branch, check=False)
        if rebase.returncode != 0:
            _git(repo, "rebase", "--abort", check=False)
            raise PersistError("rebase conflict with a concurrent run (likely the same run marker); "
                               "not forcing. Stop this run.")
    else:
        raise PersistError(f"push still rejected after {max_attempts} attempts")
    _git(repo, "fetch", remote, branch)
    local = _git(repo, "rev-parse", "HEAD").stdout.strip()
    contained = _git(repo, "merge-base", "--is-ancestor", local, f"{remote}/{branch}", check=False).returncode == 0
    if not contained:
        raise PersistError("push reported success but origin does not contain the commit")
    return {"committed": True, "pushed": True, "commit": local[:12], "files": staged, "attempts": attempt}
