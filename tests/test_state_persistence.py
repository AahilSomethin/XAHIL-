"""Shared state-branch persistence: memory-sync, atomic run claims, typed push failures, recovery copies
and state-branch integrity. Offline only: every remote is a temporary bare repo on disk.

The bare remote carries a pre-receive hook that behaves like the Claude cloud git proxy: pushes to
refs/heads/claude/* are accepted; main, other branches and deletions are refused with the proxy's message.
Flag files in the bare repo switch on "repository not authorized" (deny all) or deny one branch.
"""

import datetime as dt
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import FakeAlpaca
from tests.test_cli_execution import CliHarness
from xahil import cli, cli_intel, gitexe, persist, runs

ROOT = Path(__file__).resolve().parent.parent
STATE = "claude/xahil-state"
SETTINGS = {"remote": "origin", "code_branch": "main", "state_branch": STATE}
ET = dt.timezone(dt.timedelta(hours=-4))
SESSION = {"date": "2026-10-28", "open": "09:30", "close": "16:00"}

HOOK = """#!/bin/sh
# Simulates the cloud git proxy: only claude/* branches, no deletions.
deny() {
  echo "access denied by the git proxy: $1" >&2
  exit 1
}
[ -f "$GIT_DIR/DENY_ALL" ] && deny "AahilSomethin/XAHIL- is not in this session's authorized repository set"
while read old new ref; do
  if [ -f "$GIT_DIR/DENY_REF" ] && [ "$ref" = "$(cat "$GIT_DIR/DENY_REF")" ]; then deny "push to $ref refused"; fi
  case "$new" in 0000000000000000000000000000000000000000) deny "branch deletion refused";; esac
  case "$ref" in refs/heads/claude/*) ;; *) deny "push to $ref refused";; esac
done
exit 0
"""

PROXY_403_MAIN = """fatal: --negotiate-only needs one or more --negotiation-tip=*
warning: push negotiation failed; proceeding anyway with push
error: RPC failed; HTTP 403 curl 22 The requested URL returned error: 403
send-pack: unexpected disconnect while reading sideband packet
fatal: the remote end hung up unexpectedly"""
PROXY_403_UNAUTHORIZED = """remote: access denied by the git proxy: AahilSomethin/XAHIL- is not in this session's authorized repository set, so the proxy will not inject a credential for it. To fix, add the repository to the session's sources.
fatal: unable to access 'https://github.com/AahilSomethin/XAHIL-/': The requested URL returned error: 403"""


def git(cwd, *args, check=True):
    res = gitexe.run(cwd, list(args))  # same portable resolution as production (Windows-safe)
    if check and res.returncode != 0:
        raise AssertionError(f"git {args} failed: {res.stderr}")
    return res.stdout


def put(path, text):
    """Write with LF line endings on every OS (Path.write_text would write CRLF on Windows)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


class StateRepo(unittest.TestCase):
    """A remote with main + the state branch, the proxy-like hook, and two agent clones (a, b).

    Every git process runs with an empty global config and no system config, so the developer's
    settings (Git for Windows defaults to core.autocrlf=true; some set commit.gpgsign) cannot change
    the outcome. WindowsCheckout below tests autocrlf=true explicitly."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        put(self.root / "empty.gitconfig", "")
        env = mock.patch.dict("os.environ", {"GIT_CONFIG_GLOBAL": str(self.root / "empty.gitconfig"),
                                             "GIT_CONFIG_NOSYSTEM": "1"})
        env.start()
        self.addCleanup(env.stop)
        self.remote = self.root / "remote.git"
        git(self.root, "init", "--bare", "-b", "main", str(self.remote))
        seed = self.root / "seed"
        git(self.root, "clone", "-c", "core.autocrlf=false", str(self.remote), str(seed))
        self.identity(seed)
        put(seed / "memory" / "state" / "decisions.jsonl", '{"id": "seed"}\n')
        put(seed / "memory" / "state" / "broker_snapshot.json", '{"equity": 100000}\n')
        put(seed / "memory" / "TRADE-LOG.md", "# Trade log\n")
        put(seed / "memory" / "TRADING-STRATEGY.md", "rules v1\n")
        put(seed / "xahil.py", "code v1\n")
        put(seed / ".gitattributes", (ROOT / ".gitattributes").read_text(encoding="utf-8-sig"))
        git(seed, "add", ".")
        git(seed, "commit", "-m", "seed")
        git(seed, "push", "origin", "HEAD:main", f"HEAD:refs/heads/{STATE}")
        hook = self.remote / "hooks" / "pre-receive"
        put(hook, HOOK)  # LF only: "#!/bin/sh\r" would break the hook
        hook.chmod(0o755)
        self.main_sha = self.remote_sha("main")
        self.a, self.b = self.clone("a"), self.clone("b")

    @staticmethod
    def identity(repo, autocrlf="false"):
        for k, v in (("user.email", "test@example.com"), ("user.name", "test"), ("core.autocrlf", autocrlf),
                     ("commit.gpgsign", "false")):
            git(repo, "config", k, v)

    def clone(self, name, branch=None, autocrlf="false"):
        d = self.root / name
        git(self.root, "clone", "-c", f"core.autocrlf={autocrlf}", str(self.remote), str(d))
        self.identity(d, autocrlf)
        if branch:
            git(d, "checkout", "-q", "-b", branch)
        return d

    def remote_sha(self, branch):
        return git(self.remote, "rev-parse", f"refs/heads/{branch}").strip()

    def remote_file(self, path, branch=STATE):
        return git(self.remote, "show", f"{branch}:{path}")

    def sync(self, repo):
        return persist.memory_sync(repo, SETTINGS)

    def save(self, repo, msg="save", **kw):
        return persist.persist(msg, repo=repo, settings=SETTINGS, **kw)

    def append(self, repo, rel, line):
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8", newline="") as fh:
            fh.write(line)

    def claim(self, repo, workflow="midday", when=None):
        when = when or dt.datetime(2026, 10, 28, 12, 7, tzinfo=ET)
        res = runs.start(workflow, when, SESSION, runs_dir=repo / "memory" / "state" / "runs")
        self.assertEqual(res["decision"], runs.PROCEED, res)
        rel = Path(res["path"]).relative_to(repo).as_posix()
        return res, {"path": rel, "run_id": res["marker"]["run_id"]}

    def assert_main_untouched(self):
        self.assertEqual(self.remote_sha("main"), self.main_sha, "main must never change")

    def assert_no_worktree(self, repo):
        self.assertFalse((repo / ".git" / persist.WORKTREE_DIR).exists())
        self.assertNotIn(persist.WORKTREE_DIR, git(repo, "worktree", "list"))


class MemorySync(StateRepo):
    def test_restores_state_memory_and_keeps_main_for_human_only_files(self):
        self.sync(self.b)
        self.append(self.b, "memory/RESEARCH-LOG.md", "## 2026-10-28\n")
        self.save(self.b)
        # The owner changes the rulebook on main; the state branch keeps its old copy.
        put(self.a / "memory" / "TRADING-STRATEGY.md", "rules v2\n")
        git(self.a, "commit", "-qam", "owner rule change")
        res = self.sync(self.a)
        self.assertEqual((self.a / "memory" / "RESEARCH-LOG.md").read_text(), "## 2026-10-28\n")
        self.assertEqual((self.a / "memory" / "TRADING-STRATEGY.md").read_text(), "rules v2\n")
        self.assertEqual(res["state_commit"], self.remote_sha(STATE))
        self.assertEqual(persist.read_base(self.a), self.remote_sha(STATE))
        self.assertEqual(git(self.a, "diff", "--cached", "--name-only"), "", "index stays on main")

    def test_deletes_local_memory_files_absent_from_state(self):
        self.sync(self.b)
        (self.b / "memory" / "TRADE-LOG.md").unlink()
        self.save(self.b, "remove log")
        self.sync(self.a)
        self.assertFalse((self.a / "memory" / "TRADE-LOG.md").exists())

    def test_refuses_to_overwrite_unsaved_changes(self):
        self.append(self.a, "memory/TRADE-LOG.md", "unsaved\n")
        with self.assertRaises(persist.PersistError) as ctx:
            self.sync(self.a)
        self.assertEqual(ctx.exception.kind, persist.UNSAFE)
        self.assertIn("unsaved", (self.a / "memory" / "TRADE-LOG.md").read_text())
        git(self.a, "checkout", "--", "memory/")
        self.sync(self.a)
        self.append(self.a, "memory/TRADE-LOG.md", "unsaved after sync\n")
        with self.assertRaises(persist.PersistError):
            self.sync(self.a)

    def test_resync_after_save_is_allowed(self):
        self.sync(self.a)
        self.append(self.a, "memory/TRADE-LOG.md", "x\n")
        self.save(self.a)
        self.sync(self.a)  # nothing unsaved

    def test_refuses_code_changes(self):
        put(self.a / "xahil.py", "tampered\n")
        with self.assertRaises(persist.PersistError) as ctx:
            self.sync(self.a)
        self.assertEqual(ctx.exception.kind, persist.UNSAFE)

    def test_missing_state_branch_is_an_integrity_error(self):
        with self.assertRaises(persist.PersistError) as ctx:
            persist.memory_sync(self.a, {**SETTINGS, "state_branch": "claude/nope"})
        self.assertEqual(ctx.exception.kind, persist.INTEGRITY)


class Persist(StateRepo):
    def test_saves_only_memory_to_state_branch_and_never_main(self):
        self.sync(self.a)
        self.append(self.a, "memory/TRADE-LOG.md", "PROPOSED (dry-run) AAPL\n")
        r = self.save(self.a, "eod")
        self.assertTrue(r["pushed"] and r["verified"])
        self.assertEqual(r["commit"], self.remote_sha(STATE))
        self.assertEqual(git(self.remote, "diff", "--name-only", f"{r['commit']}~1", r["commit"]).split(),
                         ["memory/TRADE-LOG.md"])
        self.assert_main_untouched()
        self.assert_no_worktree(self.a)
        self.assertEqual(git(self.a, "rev-parse", "HEAD").strip(), self.main_sha, "local branch untouched")

    def test_requires_a_sync_first(self):
        self.append(self.a, "memory/TRADE-LOG.md", "x\n")
        with self.assertRaises(persist.PersistError) as ctx:
            self.save(self.a)
        self.assertEqual(ctx.exception.kind, persist.UNSAFE)

    def test_nothing_to_commit(self):
        self.sync(self.a)
        self.assertFalse(self.save(self.a)["committed"])

    def test_guard_blocks_code_and_human_only_changes(self):
        self.sync(self.a)
        put(self.a / "xahil.py", "evil\n")
        with self.assertRaises(persist.PersistError) as ctx:
            self.save(self.a)
        self.assertEqual(ctx.exception.kind, persist.UNSAFE)
        git(self.a, "checkout", "--", "xahil.py")
        put(self.a / "memory" / "TRADING-STRATEGY.md", "agent edit\n")
        with self.assertRaises(persist.PersistError):
            self.save(self.a)
        self.assertEqual(self.remote_file("memory/TRADING-STRATEGY.md"), "rules v1\n")

    def test_concurrent_appends_merge_via_union(self):
        self.sync(self.a), self.sync(self.b)
        self.append(self.a, "memory/state/decisions.jsonl", '{"id": "a"}\n')
        self.append(self.b, "memory/state/decisions.jsonl", '{"id": "b"}\n')
        self.save(self.a, "run a")
        r = self.save(self.b, "run b")
        self.assertEqual(r["attempts"], 2)
        content = self.remote_file("memory/state/decisions.jsonl")
        for x in ('"seed"', '"a"', '"b"'):
            self.assertIn(x, content)
        self.assertIn('"a"', (self.b / "memory" / "state" / "decisions.jsonl").read_text(), "merged result synced back")

    def test_does_not_revert_files_another_run_changed(self):
        self.sync(self.a), self.sync(self.b)
        put(self.a / "memory" / "state" / "broker_snapshot.json", '{"equity": 101000}\n')
        self.save(self.a, "a snapshot")
        self.append(self.b, "memory/TRADE-LOG.md", "b line\n")
        self.save(self.b, "b log")
        self.assertEqual(self.remote_file("memory/state/broker_snapshot.json"), '{"equity": 101000}\n')
        self.assertIn("b line", self.remote_file("memory/TRADE-LOG.md"))

    def test_conflict_on_non_union_file_fails_closed(self):
        self.sync(self.a), self.sync(self.b)
        put(self.a / "memory" / "state" / "broker_snapshot.json", '{"equity": 1}\n')
        put(self.b / "memory" / "state" / "broker_snapshot.json", '{"equity": 2}\n')
        self.save(self.a)
        tip = self.remote_sha(STATE)
        with self.assertRaises(persist.PersistError) as ctx:
            self.save(self.b)
        self.assertEqual(ctx.exception.kind, persist.CONFLICT)
        self.assertIn("memory/state/broker_snapshot.json", ctx.exception.details["files"])
        self.assertEqual(self.remote_sha(STATE), tip)
        self.assertEqual((self.b / "memory" / "state" / "broker_snapshot.json").read_text(), '{"equity": 2}\n')
        self.assert_no_worktree(self.b)

    def test_rewritten_state_history_is_refused(self):
        self.sync(self.a)
        other = self.clone("owner")
        git(other, "checkout", "-q", "--orphan", "x")
        (other / "memory").mkdir(exist_ok=True)
        put(other / "memory" / "TRADE-LOG.md", "rewritten\n")
        git(other, "add", "-A")
        git(other, "commit", "-qm", "rewrite")
        git(other, "push", "origin", "HEAD:refs/heads/claude/rewrite")  # get the object onto the remote
        git(self.remote, "update-ref", f"refs/heads/{STATE}", git(other, "rev-parse", "HEAD").strip())
        self.append(self.a, "memory/TRADE-LOG.md", "x\n")
        with self.assertRaises(persist.PersistError) as ctx:
            self.save(self.a)
        self.assertEqual(ctx.exception.kind, persist.INTEGRITY)

    def test_remote_tip_mismatch_after_push_is_an_integrity_error(self):
        self.sync(self.a)
        self.append(self.a, "memory/TRADE-LOG.md", "x\n")
        with mock.patch.object(persist, "_remote_tip", return_value=None):  # branch vanished after the push
            with self.assertRaises(persist.PersistError) as ctx:
                self.save(self.a)
        self.assertEqual(ctx.exception.kind, persist.INTEGRITY)


class Claims(StateRepo):
    def test_only_one_of_two_concurrent_claims_wins(self):
        self.sync(self.a), self.sync(self.b)
        res_a, claim_a = self.claim(self.a)
        res_b, claim_b = self.claim(self.b)  # both saw no marker
        self.assertTrue(self.save(self.a, "claim a", claim=claim_a)["verified"])
        with self.assertRaises(persist.PersistError) as ctx:
            self.save(self.b, "claim b", claim=claim_b)
        self.assertEqual(ctx.exception.kind, persist.CLAIM_LOST)
        self.assertEqual(ctx.exception.exit_code(), 5)
        marker = json.loads(self.remote_file(claim_a["path"]))
        self.assertEqual(marker["run_id"], res_a["marker"]["run_id"])
        self.assert_no_worktree(self.b)
        self.assertNotIn("rebase", git(self.b, "status"))

    def test_claim_is_seen_by_the_next_sync(self):
        self.sync(self.a)
        _, claim_a = self.claim(self.a)
        self.save(self.a, "claim a", claim=claim_a)
        self.sync(self.b)
        r = runs.start("midday", dt.datetime(2026, 10, 28, 12, 20, tzinfo=ET), SESSION,
                       runs_dir=self.b / "memory" / "state" / "runs")
        self.assertEqual(r["decision"], runs.SKIP_RUNNING)

    def test_claim_with_foreign_marker_after_clean_rebase_is_lost(self):
        self.sync(self.a)
        _, claim_a = self.claim(self.a)
        with self.assertRaises(persist.PersistError) as ctx:
            self.save(self.a, "claim", claim={**claim_a, "run_id": "someone-else"})
        self.assertEqual(ctx.exception.kind, persist.CLAIM_LOST)

    def test_recovery_branches_never_count_as_claims(self):
        session = self.clone("session", branch="claude/session-1")
        self.sync(session)
        _, claim_s = self.claim(session)
        put(self.remote / "DENY_REF", f"refs/heads/{STATE}")
        with self.assertRaises(persist.PersistError) as ctx:
            self.save(session, "claim", claim=claim_s)
        self.assertEqual(ctx.exception.kind, persist.DENIED)
        rec = persist.save_recovery(session, "claim", ctx.exception, SETTINGS)
        self.assertTrue(rec["recovery_pushed"])
        self.assertIn(claim_s["path"], git(self.remote, "ls-tree", "-r", "--name-only", "claude/session-1"))
        (self.remote / "DENY_REF").unlink()
        self.sync(self.a)  # reads only the state branch
        res, claim_a = self.claim(self.a)
        self.assertTrue(self.save(self.a, "claim a", claim=claim_a)["verified"])


class Failures(StateRepo):
    def calls(self):
        seen = []
        real = persist._run

        def spy(cwd, args):
            seen.append(list(args))
            return real(cwd, args)
        return seen, spy

    def test_denied_push_is_not_retried_and_never_rebased(self):
        self.sync(self.a)
        self.append(self.a, "memory/TRADE-LOG.md", "x\n")
        put(self.remote / "DENY_ALL", "")
        seen, spy = self.calls()
        with mock.patch.object(persist, "_run", spy), self.assertRaises(persist.PersistError) as ctx:
            self.save(self.a)
        self.assertEqual(ctx.exception.kind, persist.DENIED)
        self.assertEqual(ctx.exception.exit_code(), 7)
        self.assertEqual(sum(1 for c in seen if c[0] == "push"), 1)
        self.assertFalse([c for c in seen if c[0] == "rebase"])
        self.assert_no_worktree(self.a)

    def test_non_claude_branch_is_refused_like_the_cloud_proxy(self):
        git(self.remote, "branch", "xahil-state", "main")
        s = {**SETTINGS, "state_branch": "xahil-state"}
        persist.memory_sync(self.a, s)
        self.append(self.a, "memory/TRADE-LOG.md", "x\n")
        with self.assertRaises(persist.PersistError) as ctx:
            persist.persist("x", repo=self.a, settings=s)
        self.assertEqual(ctx.exception.kind, persist.DENIED)

    def test_state_branch_may_never_be_the_code_branch(self):
        with self.assertRaises(persist.PersistError) as ctx:
            persist.load_settings(self.a, {"state_branch": "main"})
        self.assertEqual(ctx.exception.kind, persist.UNSAFE)
        for bad in ("", "--force", "+claude/x", "a b"):
            with self.assertRaises(persist.PersistError):
                persist.load_settings(self.a, {"state_branch": bad})

    def flaky(self, failures, text="fatal: unable to access 'https://github.com/x/': Could not resolve host: github.com"):
        real = persist._run
        left = {"n": failures}

        def run(cwd, args):
            if args[0] == "push" and left["n"] > 0:
                left["n"] -= 1
                return subprocess.CompletedProcess(args, 128, "", text)
            return real(cwd, args)
        return run

    def test_network_errors_are_retried_with_backoff(self):
        self.sync(self.a)
        self.append(self.a, "memory/TRADE-LOG.md", "x\n")
        sleeps = []
        with mock.patch.object(persist, "_run", self.flaky(2)), mock.patch.object(persist, "_sleep", sleeps.append):
            r = self.save(self.a)
        self.assertTrue(r["verified"])
        self.assertEqual(sleeps, [2, 4])

    def test_persistent_network_failure_fails_closed(self):
        self.sync(self.a)
        self.append(self.a, "memory/TRADE-LOG.md", "x\n")
        tip = self.remote_sha(STATE)
        with mock.patch.object(persist, "_run", self.flaky(99)), mock.patch.object(persist, "_sleep", lambda s: None):
            with self.assertRaises(persist.PersistError) as ctx:
                self.save(self.a)
        self.assertEqual((ctx.exception.kind, ctx.exception.exit_code()), (persist.NETWORK, 8))
        self.assertEqual(self.remote_sha(STATE), tip)

    def test_repeated_rejections_end_as_conflict(self):
        self.sync(self.a)
        self.append(self.a, "memory/TRADE-LOG.md", "x\n")
        reject = "! [rejected] HEAD -> claude/xahil-state (fetch first)\nerror: failed to push some refs"
        with mock.patch.object(persist, "_run", self.flaky(99, reject)):
            with self.assertRaises(persist.PersistError) as ctx:
                self.save(self.a, max_attempts=3)
        self.assertEqual((ctx.exception.kind, ctx.exception.exit_code()), (persist.CONFLICT, 9))

    def test_classifies_the_observed_cloud_errors(self):
        self.assertEqual(persist.classify_failure(PROXY_403_MAIN), persist.DENIED)
        self.assertEqual(persist.classify_failure(PROXY_403_UNAUTHORIZED), persist.DENIED)
        self.assertEqual(persist.classify_failure(
            "remote: error: GH006: Protected branch update failed for refs/heads/main."), persist.DENIED)
        self.assertEqual(persist.classify_failure(
            " ! [rejected]        HEAD -> claude/xahil-state (non-fast-forward)"), persist.REJECTED)
        self.assertEqual(persist.classify_failure("fatal: Could not resolve host: github.com"), persist.NETWORK)
        self.assertEqual(persist.classify_failure("error: RPC failed; curl 56 Recv failure: Connection reset by peer"),
                         persist.NETWORK)
        self.assertEqual(persist.classify_failure("fatal: bad object deadbeef"), persist.GIT)

    def test_no_push_ever_targets_main_or_forces(self):
        seen, spy = self.calls()
        with mock.patch.object(persist, "_run", spy):
            self.sync(self.a), self.sync(self.b)
            self.append(self.a, "memory/state/decisions.jsonl", '{"id": "a"}\n')
            self.append(self.b, "memory/state/decisions.jsonl", '{"id": "b"}\n')
            self.save(self.a), self.save(self.b)
        pushes = [c for c in seen if c[0] == "push"]
        self.assertTrue(pushes)
        for c in pushes:
            self.assertEqual(c[-1], f"HEAD:refs/heads/{STATE}")
            self.assertFalse([x for x in c if x.startswith(("-f", "--force", "+", "--delete", "--mirror"))])
        self.assert_main_untouched()

    def test_source_has_no_force_or_delete(self):
        src = (ROOT / "xahil" / "persist.py").read_text()
        for bad in ("--force", "--delete", "--mirror", "push --", '"+'):
            self.assertNotIn(bad, src)


class Recovery(StateRepo):
    def failed_final_save(self):
        session = self.clone("session", branch="claude/session-1")
        self.sync(session)
        self.append(session, "memory/TRADE-LOG.md", "run result\n")
        put(self.remote / "DENY_REF", f"refs/heads/{STATE}")
        with self.assertRaises(persist.PersistError) as ctx:
            self.save(session, "eod")
        return session, ctx.exception

    def test_failed_save_goes_to_the_session_branch_only(self):
        tip = self.remote_sha(STATE)
        session, err = self.failed_final_save()
        rec = persist.save_recovery(session, "eod", err, SETTINGS)
        self.assertTrue(rec["recovery_pushed"])
        self.assertEqual(rec["branch"], "claude/session-1")
        msg = git(self.remote, "log", "-1", "--format=%B", "claude/session-1")
        self.assertIn(persist.RECOVERY_TRAILER, msg)
        self.assertIn(f"{persist.BASE_TRAILER} {tip}", msg)
        self.assertEqual(self.remote_sha(STATE), tip)
        self.assert_main_untouched()

    def test_no_recovery_copy_from_main_or_the_state_branch(self):
        self.sync(self.a)  # on main
        err = persist.PersistError(persist.DENIED, "x")
        self.assertFalse(persist.save_recovery(self.a, "eod", err, SETTINGS)["recovery_pushed"])
        self.assert_main_untouched()

    def test_owner_recover_merges_the_copy_into_state(self):
        session, err = self.failed_final_save()
        persist.save_recovery(session, "eod", err, SETTINGS)
        (self.remote / "DENY_REF").unlink()
        self.sync(self.b)  # meanwhile another run saves
        self.append(self.b, "memory/state/decisions.jsonl", '{"id": "b"}\n')
        self.save(self.b)
        owner = self.clone("owner")
        r = persist.recover(owner, "claude/session-1", SETTINGS)
        self.assertTrue(r["verified"])
        self.assertIn("run result", self.remote_file("memory/TRADE-LOG.md"))
        self.assertIn('"b"', self.remote_file("memory/state/decisions.jsonl"))
        self.assert_main_untouched()

    def test_recover_refuses_non_recovery_branches(self):
        owner = self.clone("owner")
        for branch in ("main", STATE, "feature"):
            with self.assertRaises(persist.PersistError):
                persist.recover(owner, branch, SETTINGS)


class Audit(StateRepo):
    def test_clean_state_branch_passes(self):
        self.sync(self.a)
        self.append(self.a, "memory/TRADE-LOG.md", "x\n")
        self.save(self.a)
        self.assertTrue(persist.audit(self.b, SETTINGS)["ok"])

    def test_non_memory_change_on_state_branch_is_flagged(self):
        other = self.clone("other")
        git(other, "checkout", "-q", "-b", "s", f"origin/{STATE}")
        put(other / "xahil.py", "sneaky\n")
        git(other, "commit", "-qam", "code on state")
        git(other, "push", "origin", f"HEAD:refs/heads/{STATE}")
        r = persist.audit(self.b, SETTINGS)
        self.assertFalse(r["ok"])
        self.assertIn("xahil.py", " ".join(r["problems"]))

    def test_probe_file_removal_by_normal_commit_restores_a_clean_audit(self):
        other = self.clone("other")
        git(other, "checkout", "-q", "-b", "s", f"origin/{STATE}")
        put(other / ".xahil-state-branch-test.txt", "probe\n")
        git(other, "add", "-A")
        git(other, "commit", "-qm", "probe")
        git(other, "push", "origin", f"HEAD:refs/heads/{STATE}")
        self.assertFalse(persist.audit(self.b, SETTINGS)["ok"])
        git(other, "rm", "-q", ".xahil-state-branch-test.txt")
        git(other, "commit", "-qm", "remove probe")
        git(other, "push", "origin", f"HEAD:refs/heads/{STATE}")
        self.assertTrue(persist.audit(self.b, SETTINGS)["ok"])

    def test_rewritten_history_is_flagged(self):
        self.sync(self.a)
        self.append(self.a, "memory/TRADE-LOG.md", "x\n")
        self.save(self.a)
        self.assertTrue(persist.audit(self.b, SETTINGS)["ok"])  # records the audited tip
        git(self.remote, "update-ref", f"refs/heads/{STATE}", self.main_sha)  # history rewound remotely
        with self.assertRaises(persist.PersistError) as ctx:  # the non-forced fetch already refuses
            persist.audit(self.b, SETTINGS)
        self.assertEqual(ctx.exception.kind, persist.INTEGRITY)
        git(self.b, "update-ref", "-d", f"refs/remotes/origin/{STATE}")  # even with a fresh tracking ref
        r = persist.audit(self.b, SETTINGS)
        self.assertFalse(r["ok"])
        self.assertIn("history rewritten", " ".join(r["problems"]))


class WindowsCheckout(StateRepo):
    """A checkout made the Git for Windows way (core.autocrlf=true: CRLF on disk, LF in Git)."""

    def setUp(self):
        super().setUp()
        self.win = self.clone("win", autocrlf="true")

    def test_checkout_is_clean_and_files_are_crlf_on_disk(self):
        self.assertEqual(persist.changed_paths(self.win), [])
        self.assertIn(b"\r\n", (self.win / ".gitattributes").read_bytes())

    def test_sync_claim_and_save_work_and_store_lf(self):
        self.sync(self.b)
        self.append(self.b, "memory/RESEARCH-LOG.md", "## from b\n")
        self.save(self.b)
        res = self.sync(self.win)
        self.assertEqual(res["state_commit"], self.remote_sha(STATE))
        self.assertIn(b"## from b\r\n", (self.win / "memory" / "RESEARCH-LOG.md").read_bytes())
        self.sync(self.win)  # CRLF on disk is not mistaken for an unsaved change
        _, claim = self.claim(self.win)
        self.assertTrue(self.save(self.win, "claim", claim=claim)["verified"])
        self.append(self.win, "memory/TRADE-LOG.md", "windows line\r\n")
        self.save(self.win, "eod")
        stored = self.blob_bytes("memory/TRADE-LOG.md")  # raw bytes, no newline translation
        self.assertIn("windows line", stored)
        self.assertNotIn("\r", stored, "Git stores LF; CRLF exists only in the Windows working tree")
        self.assertTrue(persist.audit(self.win, SETTINGS)["ok"])

    def blob_bytes(self, path):
        sha = git(self.remote, "rev-parse", f"{STATE}:{path}").strip()
        raw = subprocess.run([gitexe.executable(), "cat-file", "blob", sha], cwd=self.remote,
                             capture_output=True, env=gitexe.environment()).stdout
        return raw.decode("utf-8")

    def test_unsaved_crlf_edit_is_still_detected(self):
        self.sync(self.win)
        self.append(self.win, "memory/TRADE-LOG.md", "unsaved\r\n")
        with self.assertRaises(persist.PersistError) as ctx:
            self.sync(self.win)
        self.assertEqual(ctx.exception.kind, persist.UNSAFE)


class GitExecutable(StateRepo):
    def test_git_is_found_even_when_path_is_cleared(self):
        self.sync(self.a)
        self.append(self.a, "memory/TRADE-LOG.md", "x\n")
        with mock.patch.dict("os.environ", {"PATH": str(self.root / "no-git-here")}):
            self.assertTrue(self.save(self.a)["verified"])
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(persist.memory_sync(self.a, SETTINGS)["state_commit"], self.remote_sha(STATE))

    def test_resolved_path_is_absolute(self):
        self.assertTrue(Path(gitexe.executable()).is_absolute())

    def test_missing_git_fails_closed_with_a_clear_error(self):
        with mock.patch.object(gitexe, "_cached", None), mock.patch.object(gitexe.shutil, "which", return_value=None):
            with self.assertRaises(persist.PersistError) as ctx:
                persist.memory_sync(self.a, SETTINGS)
        self.assertEqual(ctx.exception.kind, persist.GIT)
        self.assertIn("git executable not found", str(ctx.exception))


class RunClaimCli(CliHarness):
    """run-claim / persist through the real CLI, against a temporary remote."""

    def setUp(self):
        super().setUp()
        self.repo = StateRepo("setUp")
        self.repo.setUp()
        self.addCleanup(self.repo.doCleanups)  # the inner fixture's env patch and temp dir
        self.fake = FakeAlpaca()

    def use(self, clone):
        for obj, name, val in ((cli_intel, "REPO", clone), (runs, "RUNS_DIR", clone / "memory" / "state" / "runs")):
            p = mock.patch.object(obj, name, val)
            p.start()
            self.addCleanup(p.stop)

    def test_claim_then_duplicate_then_finish_and_persist(self):
        self.use(self.repo.a)
        code, text = self.run_cli(["run-claim", "--workflow", "market-open"], self.fake)  # clock 10:00 ET
        self.assertEqual(code, 0, text)
        out = json.loads(text)
        self.assertTrue(out["claim_verified"])
        self.assertEqual(out["claim_commit"], self.repo.remote_sha(STATE))
        code, text = self.run_cli(["run-claim", "--workflow", "market-open"], self.fake)
        self.assertEqual(code, 5, text)
        self.assertEqual(json.loads(text)["decision"], runs.SKIP_RUNNING)
        code, _ = self.run_cli(["run-finish", "--workflow", "market-open", "--status", "completed",
                                "--summary", "hold"], self.fake)
        self.assertEqual(code, 0)
        code, text = self.run_cli(["persist", "--message", "market-open 2026-10-28"], self.fake)
        self.assertEqual(code, 0, text)
        marker = json.loads(self.repo.remote_file("memory/state/runs/2026-10-28/market-open.json"))
        self.assertEqual(marker["status"], "completed")
        self.repo.assert_main_untouched()
        self.assertEqual(self.alerts, [])

    def test_lost_race_exits_5_without_alert_and_leaves_no_local_claim(self):
        # b claims first through the library, a's CLI run raced with a stale view
        self.repo.sync(self.repo.b)
        _, claim_b = self.repo.claim(self.repo.b, "market-open", dt.datetime(2026, 10, 28, 10, 0, tzinfo=ET))
        self.use(self.repo.a)
        real_sync = persist.memory_sync

        def stale_sync(repo, settings=None):
            res = real_sync(repo, settings)
            self.repo.save(self.repo.b, "claim b", claim=claim_b)  # b wins between a's sync and push
            return res
        with mock.patch.object(persist, "memory_sync", stale_sync):
            code, text = self.run_cli(["run-claim", "--workflow", "market-open"], self.fake)
        self.assertEqual(code, 5, text)
        self.assertEqual(json.loads(text)["decision"], "skip_claim_lost")
        self.assertEqual(self.alerts, [])
        self.assertFalse((self.repo.a / "memory/state/runs/2026-10-28/market-open.json").exists())

    def test_denied_claim_exits_7_alerts_and_does_not_proceed(self):
        self.use(self.repo.a)
        put(self.repo.remote / "DENY_ALL", "")
        code, text = self.run_cli(["run-claim", "--workflow", "market-open"], self.fake)
        self.assertEqual(code, 7, text)
        self.assertEqual(json.loads(text)["kind"], persist.DENIED)
        self.assertTrue(self.alerts)
        self.assertFalse((self.repo.a / "memory/state/runs/2026-10-28/market-open.json").exists())

    def test_failed_final_save_alerts_with_recovery_branch(self):
        session = self.repo.clone("session", branch="claude/session-9")
        self.use(session)
        code, _ = self.run_cli(["run-claim", "--workflow", "market-open"], self.fake)
        self.assertEqual(code, 0)
        self.repo.append(session, "memory/TRADE-LOG.md", "result\n")
        put(self.repo.remote / "DENY_REF", f"refs/heads/{STATE}")
        code, text = self.run_cli(["persist", "--message", "market-open 2026-10-28"], self.fake)
        self.assertEqual(code, 7, text)
        out = json.loads(text)
        self.assertTrue(out["recovery"]["recovery_pushed"])
        self.assertIn("claude/session-9", self.alerts[-1])
        self.repo.assert_main_untouched()

    def test_state_recover_is_owner_only(self):
        self.use(self.repo.a)
        with mock.patch.dict("os.environ", {"XAHIL_CLOUD": "1"}):
            code, _ = self.run_cli(["state-recover", "--branch", "claude/session-1"], self.fake)
        self.assertEqual(code, cli.EXIT_REJECTED)


if __name__ == "__main__":
    unittest.main()
