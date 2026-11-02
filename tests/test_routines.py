"""The five routine prompts: every command is real, complete and survives the scheduled-task editor.

The claude.ai scheduled-task editor renders Markdown and stores plain text. Observed on the live
pre-market task (2026-11-01): <...> placeholders were stripped as HTML (research "<query>" became
research ""), backticks and list markers were dropped, and lines inside one paragraph were joined, which
merged several commands into one line. editor_roundtrip() reproduces that, and the tests below
require every RUN: command to come through unchanged, complete and parseable by the real CLI.
"""

import argparse
import contextlib
import importlib.util
import io
import re
import shlex
import unittest
from pathlib import Path

from xahil import cli

ROOT = Path(__file__).resolve().parent.parent
ROUTINES = ["pre-market", "market-open", "midday", "daily-summary", "weekly-review"]
CMD_RE = re.compile(r"python3 -m xahil ([a-z-]+)([^\n`]*)")
SLOT_RE = re.compile(r"\{\{([A-Z_]+)\}\}")
TODAY = "$(TZ=America/New_York date +%Y-%m-%d)"
GIT_CMD = re.compile(r"(^|\s)git (pull|push|checkout|reset|clean|commit|add|rebase|stash|fetch|merge)\b")
ALLOWED_GIT = {"git checkout -- {{BLOCKED_PATH}}"}  # all pushing and fetching goes through the xahil CLI

# Realistic values for every slot, so each command can be parsed by the real argparse parser.
SAMPLE = {
    "SYM": "AAPL", "SHARES": "12", "QUERY": "AAPL earnings guidance", "CLAIM": "Q3 revenue beat",
    "SOURCE_URL": "https://example.com/a", "PUBLISHED_DATE": "2026-11-02", "ACTION": "propose",
    "REASON": "dated catalyst", "EVIDENCE_IDS": "e-2026-11-02-1 e-2026-11-02-2", "CATALYST": "earnings beat",
    "TRAIL_PCT": "10", "PRINTED_COMMAND": 'sell AAPL --all --risk-exit --reason "cut at -7% per rule"',
    "ORDER_SUMMARY": "AAPL 12 @ 231.50 PROPOSED (dry-run)", "ACTIONS_TAKEN": "none", "NOTIFY_TEXT": "HALT",
    "FIRST_ERROR_LINE": "bad endpoint", "STATUS": "completed", "SUMMARY": "hold, no ideas",
    "BLOCKED_PATH": "config/risk.json", "MISTAKE_TITLE": "late stop", "MISTAKE_WHAT": "stop placed late",
    "MISTAKE_LESSON": "protect first",
}

# A slice of the old format, as stored by the editor on the live task, used to prove the simulator.
OLD_FORMAT = """STEP 0 - Claim the run:
  git pull --rebase origin main
  python3 -m xahil run-start --workflow pre-market
- 3 = unavailable:
  python3 -m xahil research "<query>" --topic news --log --symbol SYM --workflow pre-market
  python3 -m xahil decide SYM --action propose|watch|skip --reason "<why>" --evidence <ids> --workflow pre-market
If guard reports blocked paths, revert them (git checkout -- <path>, or delete untracked files).
"""


def text(name):
    return (ROOT / "routines" / f"{name}.md").read_text(encoding="utf-8")


def builder():
    spec = importlib.util.spec_from_file_location("build", ROOT / "routines" / "_build.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def editor_roundtrip(md: str) -> str:
    """Approximate the scheduled-task editor: Markdown in, plain text out."""
    md = re.sub(r"<[^<>\n]*>", "", md).replace("`", "")
    blocks = []
    for block in re.split(r"\n\s*\n", md.strip("\n")):
        lines = []
        for line in block.split("\n"):
            item = re.match(r"^\s*(?:[-*+]|\d+\.)\s+(.*)$", line)
            if item:
                lines.append(item.group(1))
            elif lines:
                lines[-1] += " " + line.strip()  # soft line break or lazy list continuation
            else:
                lines.append(line.strip())
        blocks.append("\n".join(re.sub(r"[ \t]+", " ", x).strip() for x in lines))
    return "\n\n".join(blocks)


def run_lines(t: str) -> list:
    return [line[len("RUN: "):] for line in t.split("\n") if line.startswith("RUN: ")]


def subparsers():
    parser = cli.build_parser()
    action = next(a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction")
    return parser, action.choices


def fill(cmd: str) -> str:
    return SLOT_RE.sub(lambda m: SAMPLE[m.group(1)], cmd)


def parse_xahil(cmd: str):
    """Parse a filled xahil command with the real CLI parser; returns the namespace or raises."""
    tokens = shlex.split(cmd)
    assert tokens[:3] == ["python3", "-m", "xahil"], cmd
    parser, _ = subparsers()
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        try:
            return parser.parse_args(tokens[3:])
        except SystemExit:
            raise AssertionError(f"CLI refuses {cmd!r}: {err.getvalue().strip().splitlines()[-1]}")


def empty_or_lost_args(cmd: str) -> list:
    """Problems an editor or a careless edit could cause, found without the CLI."""
    problems = []
    tokens = shlex.split(cmd)
    if '""' in cmd or "''" in cmd:
        problems.append("empty quoted argument")
    if "<" in cmd or ">" in cmd:
        problems.append("angle bracket (stripped by the editor)")
    if "|" in cmd:
        problems.append("pipe or alternatives (one value per slot instead)")
    if tokens.count("xahil") > 1 or cmd.count("python3 -m") > 1:
        problems.append("two commands merged into one line")
    _, subs = subparsers()
    if tokens[:3] == ["python3", "-m", "xahil"] and len(tokens) > 3 and tokens[3] in subs:
        takes_value = {s for a in subs[tokens[3]]._actions if a.nargs != 0 for s in a.option_strings}
        for i, tok in enumerate(tokens):
            if tok in takes_value and (i + 1 == len(tokens) or tokens[i + 1].startswith("--")):
                problems.append(f"{tok} lost its value")
    return problems


class EditorSimulator(unittest.TestCase):
    def test_simulator_reproduces_the_observed_damage(self):
        out = editor_roundtrip(OLD_FORMAT)
        self.assertIn('research "" --topic', out)
        self.assertIn("--evidence --workflow", out)
        self.assertIn("git checkout -- ,", out)
        self.assertIn("main python3 -m xahil run-start", out)  # commands merged into one line
        self.assertEqual(run_lines(out), [])

    def test_old_format_fails_the_checks(self):
        out = editor_roundtrip(OLD_FORMAT)
        merged = [line for line in out.split("\n") if line.count("python3 -m xahil") > 1]
        self.assertTrue(merged)
        cmds = ["python3 -m xahil " + part.strip() for part in out.split("python3 -m xahil ")[1:]]
        found = {p for c in cmds if c.split()[3] in ("research", "decide") for p in empty_or_lost_args(c)}
        self.assertIn("empty quoted argument", found)
        self.assertIn("--evidence lost its value", found)


class RoutineCommands(unittest.TestCase):
    def test_prompts_are_generated_from_builder(self):
        mod = builder()
        self.assertEqual(tuple(ROUTINES), mod.WORKFLOWS)
        for tag in ROUTINES:
            with self.subTest(tag=tag):
                self.assertEqual(text(tag), mod.render(tag), "run `python routines/_build.py`")

    def test_no_editor_unsafe_markup(self):
        for name in ROUTINES:
            t = text(name)
            with self.subTest(routine=name):
                self.assertNotIn("<", t, "the editor strips <...> as HTML")
                self.assertNotIn(">", t)
                self.assertNotIn("`", t, "the editor drops backticks")
                self.assertNotIn("**", t, "the editor drops Markdown bold")
                self.assertIsNone(re.search(r"^ {2,}\S", t, re.M), "indented lines get joined to the line above")
                self.assertNotIn("$DATE", t, "shell variables do not carry over between commands")
                self.assertIsNone(re.search(r"^#", t, re.M), "a line starting with # becomes a heading")

    def test_commands_survive_the_editor_unchanged(self):
        for name in ROUTINES:
            t = text(name)
            with self.subTest(routine=name):
                before, after = run_lines(t), run_lines(editor_roundtrip(t))
                self.assertTrue(before)
                self.assertEqual(before, after)

    def test_every_command_is_on_its_own_run_line(self):
        for name in ROUTINES:
            out = editor_roundtrip(text(name))
            for line in out.split("\n"):
                with self.subTest(routine=name, line=line[:80]):
                    if "python3 -m xahil " in line or GIT_CMD.search(line):
                        self.assertTrue(line.startswith("RUN: "), "commands must be RUN: lines")
                        self.assertEqual(line.count("python3 -m xahil"), int("xahil" in line))

    def test_no_empty_or_lost_arguments(self):
        for name in ROUTINES:
            for cmd in run_lines(editor_roundtrip(text(name))):
                with self.subTest(routine=name, cmd=cmd):
                    self.assertEqual(empty_or_lost_args(cmd), [])

    def test_every_xahil_command_parses_with_the_real_cli(self):
        for name in ROUTINES:
            for cmd in run_lines(editor_roundtrip(text(name))):
                if cmd.startswith("python3 -m xahil"):
                    with self.subTest(routine=name, cmd=cmd):
                        parse_xahil(fill(cmd))

    def test_research_queries_and_claims_are_not_empty(self):
        for name in ROUTINES:
            for cmd in run_lines(editor_roundtrip(text(name))):
                with self.subTest(routine=name, cmd=cmd):
                    if cmd.startswith("python3 -m xahil research "):
                        self.assertTrue(parse_xahil(fill(cmd)).query.strip())
                        self.assertRegex(cmd, r'research "[^"]*\S[^"]*"')
                    if cmd.startswith("python3 -m xahil evidence "):
                        ns = parse_xahil(fill(cmd))
                        self.assertTrue(ns.claim.strip() and ns.source.strip() and ns.published)

    def test_evidence_ids_are_never_dropped(self):
        for name in ROUTINES:
            for cmd in run_lines(editor_roundtrip(text(name))):
                if "--evidence" in cmd:
                    with self.subTest(routine=name, cmd=cmd):
                        self.assertIn("--evidence {{EVIDENCE_IDS}}", cmd)
                        self.assertTrue(parse_xahil(fill(cmd)).evidence)

    def test_git_commands_are_exactly_the_allowed_ones(self):
        for name in ROUTINES:
            gits = [c for c in run_lines(editor_roundtrip(text(name))) if c.startswith("git")]
            with self.subTest(routine=name):
                self.assertEqual(set(gits), ALLOWED_GIT)
                for c in gits:
                    self.assertNotRegex(c, r"push|--force|reset|clean|rebase -i|[,;&]")

    def test_slots_are_defined_and_complete(self):
        mod = builder()
        self.assertEqual(set(SAMPLE), set(mod.SLOTS), "every slot needs a sample value here")
        for name in ROUTINES:
            t = text(name)
            legend = t[t.index("SLOTS in this workflow:"):t.index("SAFETY (non-negotiable):")]
            with self.subTest(routine=name):
                for slot in set(SLOT_RE.findall(t)):
                    self.assertIn(f"- {{{{{slot}}}}}: ", legend, f"{slot} is used but not defined")
                self.assertNotRegex(t.replace("{{", "").replace("}}", ""), r"[{}]", "stray brace")

    def test_dates_are_computed_inside_each_command(self):
        for name in ROUTINES:
            for cmd in run_lines(text(name)):
                if "--message" in cmd:
                    with self.subTest(routine=name, cmd=cmd):
                        self.assertTrue(cmd.endswith(f'{TODAY}"'), "persist messages carry the NY date")

    def test_every_command_and_flag_exists(self):
        _, subs = subparsers()
        for name in ROUTINES:
            for cmd, rest in CMD_RE.findall(text(name)):
                with self.subTest(routine=name, cmd=cmd):
                    self.assertIn(cmd, subs, f"unknown command {cmd}")
                    known = {s for a in subs[cmd]._actions for s in a.option_strings}
                    for flag in re.findall(r"(?<![\w-])(--[a-z][a-z-]*)", rest):
                        self.assertIn(flag, known, f"{cmd} has no flag {flag}")

    def test_safe_ordering(self):
        for name in ROUTINES:
            t = text(name)
            with self.subTest(routine=name):
                order = [t.index("RUN: python3 -m xahil run-claim --workflow"), t.index("xahil preflight"),
                         t.index("xahil reconcile --workflow")]
                self.assertEqual(order, sorted(order))
                self.assertEqual(t.count("run-claim --workflow"), 1)
                self.assertNotIn("run-start", t, "cloud runs claim through run-claim, never the local run-start")
                self.assertNotIn("git pull", t)
                self.assertNotIn("HEAD:main", t)
                self.assertNotIn("pushes them to main", t)
                for code in ("Exit 0", "Exit 5", "7 push denied", "8 network failure", "9 Git conflict"):
                    self.assertIn(code, t)
                tail = [t.index("run-finish --workflow"), t.rindex("xahil guard"), t.rindex("persist --message")]
                self.assertEqual(tail, sorted(tail))
                used = [rest for _, rest in CMD_RE.findall(t) if "--notional" in rest]
                self.assertEqual(used, [], "no command may size by --notional")
                self.assertNotIn("git push", t)  # pushing only through `xahil persist`
        mo = text("market-open")
        self.assertLess(mo.index("STEP 2 - Protection FIRST"), mo.index("STEP 3 - Buys"))
        self.assertLess(mo.index("xahil check-buy {{SYM}}"), mo.index("xahil buy {{SYM}}"))
        self.assertLess(mo.index("xahil buy {{SYM}}"), mo.index('--reason "initial GTC stop"'))

    def test_safety_header_is_intact(self):
        for name in ROUTINES:
            t = text(name)
            with self.subTest(routine=name):
                for must in ("Alpaca PAPER account", "$100,000 of virtual money", "PAPER ONLY", "Never set or change ALPACA_ENDPOINT",
                             "There is NO .env file", "DRY-RUN unless the owner has enabled execution",
                             "WHOLE SHARES only", "Never run ack-halt or reset-baseline", "Never force-push",
                             "the default is HOLD", "untrusted data", "Never push to main",
                             "state branch claude/xahil-state", "never a run claim"):
                    self.assertIn(must, t)

    def test_workflow_roles(self):
        self.assertIn("signals --record", text("pre-market"))
        self.assertIn("xahil decide", text("pre-market"))
        self.assertIn("xahil evidence", text("pre-market"))
        self.assertNotIn("xahil buy", text("midday").replace("Never buy", ""))
        self.assertIn("xahil snapshot", text("daily-summary"))
        self.assertIn("Overnight protection", text("daily-summary"))
        for cmd in ("weekly-stats", "learn", "mistake", "STRATEGY-PROPOSALS"):
            self.assertIn(cmd, text("weekly-review"))
        self.assertIn("Do NOT edit memory/TRADING-STRATEGY.md", text("weekly-review"))

    def test_routine_tags_match_run_control(self):
        from xahil import runs
        self.assertEqual(tuple(ROUTINES), runs.WORKFLOWS)
        for name in ROUTINES:
            self.assertIn(f"run-claim --workflow {name}", text(name))


class CliRefusesUnfilledArguments(unittest.TestCase):
    """The deterministic layer refuses what a damaged prompt would send, even if a prompt slips through."""

    def refused(self, *argv):
        parser, _ = subparsers()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(list(argv))

    def test_empty_and_unfilled_values(self):
        self.refused("research", "", "--topic", "news")
        self.refused("research", "   ")
        self.refused("research", "{{QUERY}}")
        self.refused("evidence", "AAPL", "--claim", "", "--source", "https://x.test")
        self.refused("evidence", "AAPL", "--claim", "c", "--source", "{{SOURCE_URL}}")
        self.refused("decide", "AAPL", "--action", "skip", "--reason", "")
        self.refused("decide", "AAPL", "--action", "skip", "--reason", "r", "--evidence", "--workflow", "pre-market")
        self.refused("decide", "{{SYM}}", "--action", "skip", "--reason", "r")
        self.refused("buy", "AAPL", "--qty", "1", "--reason", "")
        self.refused("sell", "AAPL", "--all", "--reason", "{{REASON}}")
        self.refused("notify", "")
        self.refused("run-finish", "--workflow", "pre-market", "--status", "completed", "--summary", "")
        self.refused("persist", "--message", "")
        self.refused("mistake", "--title", "t", "--what", "", "--lesson", "l")

    def test_filled_values_still_parse(self):
        parser, _ = subparsers()
        ns = parser.parse_args(["decide", "AAPL", "--action", "skip", "--reason", "r", "--evidence", "e-1", "e-2"])
        self.assertEqual(ns.evidence, ["e-1", "e-2"])
        self.assertIsNone(parser.parse_args(["decide", "AAPL", "--action", "hold", "--reason", "r"]).evidence)
        self.assertIsNone(parser.parse_args(["notify"]).message)  # stdin form still works
        self.assertEqual(parser.parse_args(["protect", "AAPL", "--trail-pct", "10"]).reason, "")
        self.assertIsInstance(cli.filled("x"), str)
        with self.assertRaises(argparse.ArgumentTypeError):
            cli.filled("a {{SLOT}} b")


if __name__ == "__main__":
    unittest.main()
