"""Portable Git invocation for subprocess calls.

* The executable is resolved with shutil.which (PATH, and PATHEXT on Windows) and cached as an
  absolute path, so later calls do not depend on how the child process searches PATH.
* Essential variables captured at import (PATH, SYSTEMROOT, HOME, ...) fill in when a caller has
  cleared os.environ: Windows cannot find git.exe without PATH, and Git for Windows needs SYSTEMROOT.
* Output is decoded as UTF-8 regardless of the console code page.
"""

from __future__ import annotations

import os
import shutil
import subprocess

_ESSENTIAL = ("PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "HOME", "USERPROFILE",
              "HOMEDRIVE", "HOMEPATH", "TEMP", "TMP")
_BOOT_ENV = {k: os.environ[k] for k in _ESSENTIAL if k in os.environ}
_cached = None


class GitNotFound(RuntimeError):
    pass


def executable() -> str:
    global _cached
    if _cached is None:
        found = shutil.which("git") or shutil.which("git", path=_BOOT_ENV.get("PATH"))
        if not found:
            raise GitNotFound("git executable not found on PATH")
        _cached = os.path.abspath(found)
    return _cached


def environment(**extra) -> dict:
    env = {**_BOOT_ENV, **os.environ}
    env.update(extra)
    return env


def run(cwd, args: list, **extra_env) -> subprocess.CompletedProcess:
    return subprocess.run([executable(), *args], cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=environment(**extra_env))
