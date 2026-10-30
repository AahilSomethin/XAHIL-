"""The five routine prompts: every command is real, steps run in a safe order, prompts match the builder."""

import importlib.util
import re
import unittest
from pathlib import Path

from xahil import cli

ROOT = Path(__file__).resolve().parent.parent
ROUTINES = ["pre-market", "market-open", "midday", "daily-summary", "weekly-review"]
CMD_RE = re.compile(r"python3 -m xahil ([a-z-]+)([^\n`]*)")


def text(name):
    return (ROOT / "routines" / f"{name}.md").read_text(encoding="utf-8")


def subparsers():
    parser = cli.build_parser()
    action = next(a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction")
    return action.choices


class RoutineCommands(unittest.TestCase):
    def test_every_command_and_flag_exists(self):
        subs = subparsers()
        for name in ROUTINES:
            for cmd, rest in CMD_RE.findall(text(name)):
                with self.subTest(routine=name, cmd=cmd):
                    self.assertIn(cmd, subs, f"unknown command {cmd}")
                    known = {s for a in subs[cmd]._actions for s in a.option_strings}
                    for flag in re.findall(r"(?<![\w-])(--[a-z][a-z-]*)", rest):
                        self.assertIn(flag, known, f"{cmd} has no flag {flag}")

    def test_prompts_are_generated_from_builder(self):
        spec = importlib.util.spec_from_file_location("build", ROOT / "routines" / "_build.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        for tag, (nm, commit, body) in mod.BODIES.items():
            expected = mod.HEADER.format(name=nm, tag=tag) + body + mod.FOOTER.format(tag=tag, commit=commit)
            with self.subTest(tag=tag):
                self.assertEqual(text(tag), expected, "run `python routines/_build.py`")

    def test_safe_ordering(self):
        for name in ROUTINES:
            t = text(name)
            with self.subTest(routine=name):
                order = [t.index("run-start --workflow"), t.index('persist --message "run-start'),
                         t.index("xahil preflight"), t.index("xahil reconcile --workflow")]
                self.assertEqual(order, sorted(order))
                tail = [t.index("run-finish --workflow"), t.rindex("xahil guard"), t.rindex("persist --message")]
                self.assertEqual(tail, sorted(tail))
                self.assertIn("exits 5", t)
                used = [rest for _, rest in CMD_RE.findall(t) if "--notional" in rest]
                self.assertEqual(used, [], "no command may size by --notional")
                self.assertNotIn("git push", t)  # pushing only through `xahil persist`
        mo = text("market-open")
        self.assertLess(mo.index("STEP 2 - Protection FIRST"), mo.index("STEP 3 - Buys"))
        self.assertLess(mo.index("xahil buy SYM"), mo.index('--reason "initial GTC stop"'))

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
            self.assertIn(f"run-start --workflow {name}", text(name))


if __name__ == "__main__":
    unittest.main()
