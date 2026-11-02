"""Git persistence for agent memory on the shared state branch.

Branch model
* `main` (code_branch) holds code and config. Only the owner changes it. Agent runs never push it.
* `claude/xahil-state` (state_branch) holds the shared memory/. Agent runs only ever add
  fast-forward commits to it that touch memory/ (never the human-only files). Never force-pushed.

Flow
* memory_sync: fetch the state branch and restore memory/ from it (human-only files keep main's copy).
  Records the synced commit as the base for the next save.
* persist: commit this run's memory/ changes on top of that base in a private worktree, push the
  state branch normally, rebase on rejection (append-only logs merge via .gitattributes union),
  then verify the remote tip. With `claim=`, a marker conflict or a foreign run_id = claim lost.
* save_recovery: if the final save fails, push a recovery copy to this session's own claude/*
  branch. It is never read by memory_sync or run claims; the owner merges it with state-recover.

Failures are typed (PersistError.kind) and always fail closed:
  claim_lost, denied (HTTP 403 / permission), network, conflict, integrity, unsafe, git_error.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

from xahil import guard
from xahil.config import ROOT

CLAIM_LOST, DENIED, NETWORK, CONFLICT, INTEGRITY, UNSAFE, GIT = (
    "claim_lost", "denied", "network", "conflict", "integrity", "unsafe", "git_error")
REJECTED = "rejected"  # internal: a normal push lost a race (non-fast-forward)

# CLI exit codes. 5 matches run-start's "skip"; 6 is research's "insufficient".
EXIT_CODES = {CLAIM_LOST: 5, DENIED: 7, NETWORK: 8, CONFLICT: 9}

DEFAULTS = {"remote": "origin", "code_branch": "main", "state_branch": "claude/xahil-state"}
HUMAN_ONLY = sorted(guard.HUMAN_ONLY_FILES)
BASE_FILE = "xahil-state-base"          # inside .git: never committed
WORKTREE_DIR = "xahil-state-worktree"   # inside .git: never committed
RECOVERY_TRAILER = "Xahil-Recovery: not-a-run-claim"
BASE_TRAILER = "Xahil-State-Base:"
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")

DENIED_MARKERS = (
    "access denied by the git proxy", "not in this session's authorized", "returned error: 403",
    "http 403", "error: 403", "returned error: 401", "permission to ", "permission denied",
    "authentication failed", "could not read username", "protected branch", "gh006", "gh013",
    "pre-receive hook declined", "push declined",
)
REJECTED_MARKERS = ("[rejected]", "non-fast-forward", "fetch first", "updates were rejected", "stale info")
NETWORK_MARKERS = (
    "could not resolve host", "temporary failure in name resolution", "failed to connect",
    "connection reset", "connection refused", "connection timed out", "operation timed out",
    "timed out", "network is unreachable", "early eof", "unexpected disconnect",
    "the remote end hung up", "rpc failed", "returned error: 5", "http 5", "proxy connect",
    "ssl_", "gnutls", "tls connection",
)


class PersistError(RuntimeError):
    def __init__(self, kind: str, message: str, **details):
        super().__init__(message)
        self.kind = kind
        self.details = details

    def exit_code(self) -> int:
        return EXIT_CODES.get(self.kind, 1)


# --- settings ----------------------------------------------------------------------

def load_settings(repo: Path = ROOT, overrides: Optional[dict] = None) -> dict:
    s = dict(DEFAULTS)
    path = repo / "config" / "persistence.json"
    if path.exists():
        s.update({k: v for k, v in json.loads(path.read_text(encoding="utf-8")).items() if k in DEFAULTS})
    s.update(overrides or {})
    for k in DEFAULTS:
        v = s[k]
        if not isinstance(v, str) or not BRANCH_RE.match(v) or ".." in v or v.endswith(("/", ".lock")):
            raise PersistError(UNSAFE, f"persistence setting {k!r} is invalid: {v!r}")
    if s["state_branch"] == s["code_branch"]:
        raise PersistError(UNSAFE, "state_branch must differ from code_branch; agent runs never push code")
    return s


# --- git plumbing --------------------------------------------------------------------

def _run(cwd: Path, args: list) -> subprocess.CompletedProcess:
    """Single place every git call goes through (tests patch this to inject failures)."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=env)


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _git(cwd: Path, *args, check: bool = True) -> subprocess.CompletedProcess:
    res = _run(cwd, list(args))
    if check and res.returncode != 0:
        text = (res.stderr or res.stdout).strip()
        raise PersistError(classify_failure(text), f"git {' '.join(args[:3])} failed: {text[:400]}")
    return res


def classify_failure(text: str) -> str:
    """Map git's output to a failure kind. Order matters: a proxy 403 also says 'RPC failed'."""
    t = (text or "").lower()
    if any(m in t for m in DENIED_MARKERS):
        return DENIED
    if any(m in t for m in REJECTED_MARKERS):
        return REJECTED
    if any(m in t for m in NETWORK_MARKERS):
        return NETWORK
    return GIT


def _git_dir(repo: Path) -> Path:
    p = Path(_git(repo, "rev-parse", "--git-common-dir").stdout.strip())
    return p if p.is_absolute() else (repo / p).resolve()


def _remote_tip(repo: Path, s: dict) -> Optional[str]:
    ref = f"refs/heads/{s['state_branch']}"
    out = _git(repo, "ls-remote", "--heads", s["remote"], ref).stdout.split()
    return out[0] if out else None


def _tracking_ref(s: dict) -> str:
    return f"refs/remotes/{s['remote']}/{s['state_branch']}"


def fetch_state(repo: Path, s: dict, retries: int = 3) -> str:
    """Fetch the state branch into its tracking ref (non-forced: a rewritten history fails). Returns the tip."""
    spec = f"refs/heads/{s['state_branch']}:{_tracking_ref(s)}"
    for attempt in range(retries + 1):
        res = _run(repo, ["fetch", "--no-tags", s["remote"], spec])
        if res.returncode == 0:
            break
        text = (res.stderr or res.stdout).strip()
        kind = classify_failure(text)
        if kind == REJECTED:
            raise PersistError(INTEGRITY, "state branch history was rewritten (non-fast-forward fetch); "
                                          "refusing to continue. Owner must inspect it.")
        if "couldn't find remote ref" in text.lower():
            raise PersistError(INTEGRITY, f"state branch {s['state_branch']} does not exist on {s['remote']}")
        if kind == NETWORK and attempt < retries:
            _sleep(2 ** (attempt + 1))
            continue
        raise PersistError(kind, f"fetching the state branch failed: {text[:400]}")
    return _git(repo, "rev-parse", _tracking_ref(s)).stdout.strip()


def _is_ancestor(repo: Path, a: str, b: str) -> bool:
    return _run(repo, ["merge-base", "--is-ancestor", a, b]).returncode == 0


# --- memory file sets ---------------------------------------------------------------

def _memory_files(repo: Path) -> set:
    """memory/ files present on disk that Git would track (tracked or untracked, not ignored)."""
    out = _git(repo, "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", "memory/").stdout
    return {p for p in out.split("\0") if p and (repo / p).is_file() and p not in guard.HUMAN_ONLY_FILES}


def _mirror_memory(src: Path, dst: Path) -> None:
    """Make dst's agent-writable memory/ files identical to src's (human-only files untouched)."""
    want = _memory_files(src)
    for p in _memory_files(dst) - want:
        (dst / p).unlink()
    for p in want:
        target = dst / p
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src / p, target)


def _local_blobs(repo: Path) -> dict:
    """{path: blob id} of the agent-writable memory files on disk, hashed as `git add` would."""
    files = sorted(_memory_files(repo))
    if not files:
        return {}
    ids = _git(repo, "hash-object", "--", *files).stdout.split()
    return dict(zip(files, ids))


def _commit_blobs(repo: Path, commit: str) -> dict:
    """{path: blob id} of the agent-writable memory files in a commit."""
    out = _git(repo, "ls-tree", "-r", "-z", commit, "--", "memory/").stdout
    blobs = {}
    for entry in out.split("\0"):
        if entry:
            meta, path = entry.split("\t", 1)
            if path not in guard.HUMAN_ONLY_FILES:
                blobs[path] = meta.split()[2]
    return blobs


def _base_path(repo: Path) -> Path:
    return _git_dir(repo) / BASE_FILE


def read_base(repo: Path) -> Optional[str]:
    p = _base_path(repo)
    return p.read_text(encoding="utf-8").strip() if p.exists() else None


def _write_base(repo: Path, sha: str) -> None:
    _base_path(repo).write_text(sha + "\n", encoding="utf-8")


def _non_memory_changes(repo: Path) -> list:
    return guard.classify(changed_paths(repo))


def changed_paths(repo: Path) -> list:
    out = _git(repo, "status", "--porcelain", "--untracked-files=all").stdout
    paths = []
    for line in out.splitlines():
        entry = line[3:]
        paths.extend(entry.split(" -> ") if " -> " in entry else [entry])
    return [p.strip('"') for p in paths]


# --- memory-sync ----------------------------------------------------------------------

def memory_sync(repo: Path = ROOT, settings: Optional[dict] = None) -> dict:
    """Restore memory/ from the state branch. Refuses to overwrite unsaved local memory changes."""
    s = settings or load_settings(repo)
    bad = _non_memory_changes(repo)
    if bad:
        raise PersistError(UNSAFE, f"uncommitted changes outside memory/ (or to human-only files): {bad}")
    tip = fetch_state(repo, s)
    base = read_base(repo)
    state_files = set(_commit_blobs(repo, tip))
    local = _memory_files(repo)
    in_sync_with_main = not [p for p in changed_paths(repo) if p.startswith("memory/")]
    if not in_sync_with_main:
        # Only safe if nothing changed since the last sync (or save) in this checkout.
        if base is None:
            raise PersistError(UNSAFE, "memory/ has unsaved local changes and was never synced; refusing to "
                                       "overwrite. Persist or revert them first.")
        if _local_blobs(repo) != _commit_blobs(repo, base):
            raise PersistError(UNSAFE, "memory/ has unsaved changes since the last sync; refusing to overwrite")
    for p in local - state_files:
        (repo / p).unlink()
    if state_files:
        _git(repo, "checkout", tip, "--", "memory/", *[f":(exclude){h}" for h in HUMAN_ONLY])
        _git(repo, "reset", "-q", "--", "memory/")  # keep the index on main; only the working tree changes
    _write_base(repo, tip)
    return {"synced": True, "state_branch": s["state_branch"], "state_commit": tip, "files": len(state_files)}


def restore_path(repo: Path, rel: str) -> None:
    """Undo a local change to one memory file: back to the synced state, or removed if it was new."""
    base = read_base(repo)
    if base and _run(repo, ["cat-file", "-e", f"{base}:{rel}"]).returncode == 0:
        _git(repo, "checkout", base, "--", rel)
        _git(repo, "reset", "-q", "--", rel)
    elif (repo / rel).exists():
        (repo / rel).unlink()


# --- persist ----------------------------------------------------------------------------

def _worktree(repo: Path) -> Path:
    return _git_dir(repo) / WORKTREE_DIR


def _drop_worktree(repo: Path) -> None:
    wt = _worktree(repo)
    if wt.exists():
        shutil.rmtree(wt, ignore_errors=True)
    _run(repo, ["worktree", "prune"])


def _conflicted(wt: Path) -> list:
    return [p for p in _run(wt, ["diff", "--name-only", "--diff-filter=U"]).stdout.split() if p]


def persist(message: str, repo: Path = ROOT, settings: Optional[dict] = None, claim: Optional[dict] = None,
            max_attempts: int = 3, network_retries: int = 3, dry_run: bool = False) -> dict:
    """Save memory/ changes since the last sync to the state branch. Never touches main, never forces.

    claim = {"path": "memory/state/runs/<date>/<wf>.json", "run_id": "..."}: the run claim being pushed.
    """
    if not message.strip():
        raise PersistError(UNSAFE, "commit message required")
    s = settings or load_settings(repo)
    bad = _non_memory_changes(repo)
    if bad:
        raise PersistError(UNSAFE, f"guard blocked non-memory changes: {bad}")
    base = read_base(repo)
    if not base:
        raise PersistError(UNSAFE, "memory was never synced from the state branch in this checkout; "
                                   "run memory-sync (or run-claim) first")
    if dry_run:
        return {"dry_run": True, "state_branch": s["state_branch"], "base": base,
                "would_stage": sorted(p for p in changed_paths(repo) if p.startswith("memory/"))}
    tip = fetch_state(repo, s)
    if not _is_ancestor(repo, base, tip):
        raise PersistError(INTEGRITY, f"synced base {base[:12]} is no longer in the state branch history "
                                      f"(tip {tip[:12]}); history was rewritten")
    _drop_worktree(repo)
    wt = _worktree(repo)
    try:
        _git(repo, "worktree", "add", "--detach", str(wt), base)
        _mirror_memory(repo, wt)
        _git(wt, "add", "-A", "--", "memory/")
        staged = [p for p in _git(wt, "diff", "--cached", "--name-only", "-z").stdout.split("\0") if p]
        blocked = guard.classify(staged)
        if blocked:
            raise PersistError(UNSAFE, f"refusing to commit to the state branch: {blocked}")
        if not staged:
            if claim:
                raise PersistError(GIT, "run claim produced no change to commit")
            return {"committed": False, "pushed": False, "reason": "nothing to commit", "state_commit": tip}
        _git(wt, "commit", "-q", "-m", message)
        attempts, net_failures = 1, 0
        while True:
            push = _run(wt, ["push", s["remote"], f"HEAD:refs/heads/{s['state_branch']}"])
            if push.returncode == 0:
                break
            text = (push.stderr or push.stdout).strip()
            kind = classify_failure(text)
            if kind == DENIED:
                raise PersistError(DENIED, f"push to {s['state_branch']} denied (not retried): {text[:400]}")
            if kind == NETWORK:
                net_failures += 1
                if net_failures > network_retries:
                    raise PersistError(NETWORK, f"push failed after {net_failures} network errors: {text[:400]}")
                _sleep(2 ** net_failures)
                continue
            if kind != REJECTED:
                raise PersistError(GIT, f"push failed: {text[:400]}")
            if attempts >= max_attempts:
                raise PersistError(CONFLICT, f"push still rejected after {attempts} attempts")
            attempts += 1
            fetch_state(repo, s)
            rebase = _run(wt, ["rebase", _tracking_ref(s)])
            if rebase.returncode != 0:
                files = _conflicted(wt)
                _run(wt, ["rebase", "--abort"])
                if claim and claim["path"] in files:
                    raise PersistError(CLAIM_LOST, "another run claimed this slot first", files=files)
                raise PersistError(CONFLICT, f"rebase conflict with a concurrent save: {files}", files=files)
        head = _git(wt, "rev-parse", "HEAD").stdout.strip()
        remote = _remote_tip(repo, s)
        if remote != head:
            fetch_state(repo, s)
            if not (remote and _is_ancestor(repo, head, _tracking_ref(s))):
                raise PersistError(INTEGRITY, f"push reported success but the remote tip is {remote}, "
                                              f"which does not contain {head[:12]}")
        if claim:
            shown = _run(wt, ["show", f"HEAD:{claim['path']}"])
            try:
                owner = json.loads(shown.stdout).get("run_id") if shown.returncode == 0 else None
            except json.JSONDecodeError:
                owner = None
            if owner != claim["run_id"]:
                raise PersistError(CLAIM_LOST, f"state branch marker belongs to run {owner}, not {claim['run_id']}")
        _mirror_memory(wt, repo)  # bring concurrently merged lines back into this checkout
        _write_base(repo, head)
        return {"committed": True, "pushed": True, "verified": True, "state_branch": s["state_branch"],
                "commit": head, "files": staged, "attempts": attempts}
    finally:
        _drop_worktree(repo)


# --- recovery for failed final saves ----------------------------------------------------

def save_recovery(repo: Path, message: str, error: PersistError, settings: Optional[dict] = None) -> dict:
    """Push this run's memory/ to the session's own claude/* branch. Never a claim, never shared memory."""
    s = settings or load_settings(repo)
    branch = _git(repo, "branch", "--show-current", check=False).stdout.strip()
    if not branch.startswith("claude/") or branch in (s["state_branch"], s["code_branch"]):
        return {"recovery_pushed": False, "reason": f"no session branch to hold a recovery copy (on {branch or 'detached HEAD'})"}
    if _non_memory_changes(repo):
        return {"recovery_pushed": False, "reason": "non-memory changes present; not saving a recovery copy"}
    base = read_base(repo) or "unknown"
    _git(repo, "add", "--", "memory/")
    staged = [p for p in _git(repo, "diff", "--cached", "--name-only", "-z").stdout.split("\0") if p]
    if guard.classify(staged):
        _git(repo, "reset", "-q", "--", "memory/")
        return {"recovery_pushed": False, "reason": "staged paths failed the guard"}
    body = (f"XAHIL RECOVERY (not a run claim): {message}\n\nState save failed ({error.kind}): {str(error)[:300]}\n\n"
            f"{RECOVERY_TRAILER}\n{BASE_TRAILER} {base}")
    _git(repo, "commit", "-q", "--allow-empty", "-m", body)
    push = _run(repo, ["push", s["remote"], f"HEAD:refs/heads/{branch}"])
    commit = _git(repo, "rev-parse", "HEAD").stdout.strip()
    if push.returncode != 0:
        text = (push.stderr or push.stdout).strip()
        return {"recovery_pushed": False, "branch": branch, "commit": commit,
                "reason": f"{classify_failure(text)}: {text[:300]}"}
    return {"recovery_pushed": True, "branch": branch, "commit": commit, "base": base}


def recover(repo: Path, branch: str, settings: Optional[dict] = None) -> dict:
    """Owner tool: merge a recovery copy from a session branch into the state branch via persist()."""
    s = settings or load_settings(repo)
    if not branch.startswith("claude/") or branch in (s["state_branch"], s["code_branch"]):
        raise PersistError(UNSAFE, f"{branch!r} is not a session recovery branch")
    if _non_memory_changes(repo) or [p for p in changed_paths(repo) if p.startswith("memory/")]:
        raise PersistError(UNSAFE, "working tree is not clean; commit or stash local changes first")
    ref = f"refs/remotes/{s['remote']}/xahil-recovery"
    _git(repo, "fetch", "--no-tags", s["remote"], f"refs/heads/{branch}:{ref}")
    msg = _git(repo, "log", "-1", "--format=%B", ref).stdout
    if RECOVERY_TRAILER not in msg:
        raise PersistError(UNSAFE, f"{branch} tip is not an XAHIL recovery commit")
    base = next((ln.split(":", 1)[1].strip() for ln in msg.splitlines() if ln.startswith(BASE_TRAILER)), "")
    touched = [p for p in _git(repo, "diff", "--name-only", "-z", f"{ref}~1", ref).stdout.split("\0") if p]
    if guard.classify(touched):
        raise PersistError(UNSAFE, f"recovery commit touches non-memory paths: {guard.classify(touched)}")
    tip = fetch_state(repo, s)
    if not base or _run(repo, ["cat-file", "-e", f"{base}^{{commit}}"]).returncode != 0 or not _is_ancestor(repo, base, tip):
        raise PersistError(INTEGRITY, f"recovery base {base[:12] or '?'} is not in the state branch history")
    _write_base(repo, base)
    files = {p for p in _git(repo, "ls-tree", "-r", "-z", "--name-only", ref, "--", "memory/").stdout.split("\0")
             if p and p not in guard.HUMAN_ONLY_FILES}
    for p in _memory_files(repo) - files:
        (repo / p).unlink()
    _git(repo, "checkout", ref, "--", "memory/", *[f":(exclude){h}" for h in HUMAN_ONLY])
    _git(repo, "reset", "-q", "--", "memory/")
    return persist(f"recover memory from {branch}", repo=repo, settings=s)


# --- audit (read-only) ----------------------------------------------------------------

def audit(repo: Path = ROOT, settings: Optional[dict] = None) -> dict:
    """Check the state branch: only memory/ (no human-only files) differs from where it forked off main,
    and its history only moved forward since the last audit in this checkout."""
    s = settings or load_settings(repo)
    tip = fetch_state(repo, s)
    code = f"refs/remotes/{s['remote']}/{s['code_branch']}"
    _git(repo, "fetch", "--no-tags", s["remote"], f"refs/heads/{s['code_branch']}:{code}")
    fork = _git(repo, "merge-base", code, tip).stdout.strip()
    changed = [p for p in _git(repo, "diff", "--name-only", "-z", fork, tip).stdout.split("\0") if p]
    problems = [f"non-memory or human-only path differs from main: {p}" for p in guard.classify(changed)]
    last_file = _git_dir(repo) / "xahil-state-audited"
    last = last_file.read_text(encoding="utf-8").strip() if last_file.exists() else None
    if last and not _is_ancestor(repo, last, tip):
        problems.append(f"history rewritten: last audited tip {last[:12]} is not an ancestor of {tip[:12]}")
    if not problems:
        last_file.write_text(tip + "\n", encoding="utf-8")
    return {"ok": not problems, "state_branch": s["state_branch"], "tip": tip, "forked_from_main_at": fork,
            "paths_changed": len(changed), "problems": problems}
