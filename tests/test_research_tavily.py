"""Tavily research adapter: mocked HTTP only. Budget caps, caching, fail-closed errors, evidence
metadata, sufficiency, and the rule that missing research is never a bullish signal."""

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import FakeAlpaca
from tests.test_cli_execution import CliHarness
from xahil import cli, journal, learning, research, tavily
from xahil.risk import OrderRequest, RiskContext, evaluate_order

NOW = dt.datetime(2026, 10, 28, 12, 0, tzinfo=dt.timezone.utc)
TODAY = NOW.date()
SETTINGS = {
    "research": {"primary": "tavily", "fallback": "perplexity", "min_sources": 2, "min_distinct_domains": 2,
                 "min_fresh_dated_sources": 1, "max_source_age_days": 7},
    "tavily": {"enabled": True, "search_depth": "basic", "allowed_depths": ["basic"], "max_results": 8,
               "monthly_credit_cap": 900, "daily_credit_cap": 40, "cache_ttl_hours": 6,
               "default_topic": "news", "default_time_range": "week"},
    "perplexity": {"enabled": False, "model": "sonar", "monthly_request_cap": 150},
}
KEY = "tvly-SECRETKEY1234567890"
ENV = {"TAVILY_API_KEY": KEY}


def tavily_body(results=None, credits=1):
    if results is None:
        results = [
            {"title": "Ford beats Q3 estimates", "url": "https://www.reuters.com/business/ford-q3",
             "content": "Ford reported...", "score": 0.91, "published_date": "2026-10-27T13:00:00Z"},
            {"title": "Ford raises guidance", "url": "https://apnews.com/article/ford",
             "content": "Guidance raised...", "score": 0.85, "published_date": "Tue, 27 Oct 2026 14:05:00 GMT"},
            {"title": "Ford history", "url": "https://example.org/ford", "content": "Old", "score": 0.4,
             "published_date": None},
        ]
    return json.dumps({"query": "q", "results": results, "response_time": 0.5, "usage": {"credits": credits},
                       "request_id": "r1"})


class FakePost:
    def __init__(self, status=200, body=None, exc=None):
        self.calls, self.status, self.body, self.exc = [], status, body or tavily_body(), exc

    def __call__(self, url, headers, payload):
        self.calls.append((url, headers, payload))
        if self.exc:
            raise self.exc
        return self.status, self.body


class Base(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp())
        self.usage, self.cache = d / "usage.jsonl", d / "cache"

    def search(self, post, settings=SETTINGS, env=ENV, now=NOW, **kw):
        return tavily.search("Ford earnings", settings, env, post=post, now=now, usage_path=self.usage,
                             cache_dir=self.cache, **kw)

    def ask(self, post, settings=SETTINGS, env=ENV, now=NOW, **kw):
        return research.ask("Ford earnings", env=env, today=now.date(), settings=settings, tavily_post=post,
                            tavily_usage_path=self.usage, tavily_cache_dir=self.cache, now=now, **kw)


class Requests(Base):
    def test_request_shape_and_parsing(self):
        post = FakePost()
        res = self.search(post)
        url, headers, payload = post.calls[0]
        self.assertEqual(url, "https://api.tavily.com/search")
        self.assertEqual(headers["Authorization"], f"Bearer {KEY}")
        self.assertEqual((payload["topic"], payload["search_depth"], payload["time_range"]), ("news", "basic", "week"))
        self.assertFalse(payload["include_answer"])  # Claude analyses the sources itself
        self.assertTrue(payload["include_usage"])
        src = tavily.parse_results(res["response"], res["retrieved_at"])
        self.assertEqual([s["domain"] for s in src], ["reuters.com", "apnews.com", "example.org"])
        self.assertEqual([s["published_date"] for s in src], ["2026-10-27", "2026-10-27", None])
        self.assertTrue(all(s["retrieved_at"] == NOW.isoformat(timespec="seconds") for s in src))

    def test_proxy_mode_sends_no_key(self):
        post = FakePost()
        self.search(post, env={"XAHIL_TAVILY_AUTH": "proxy"})
        self.assertNotIn("Authorization", post.calls[0][1])

    def test_missing_key_fails_closed_without_request(self):
        post = FakePost()
        with self.assertRaises(tavily.TavilyUnavailable):
            self.search(post, env={})
        self.assertEqual(post.calls, [])

    def test_bad_parameters_rejected(self):
        for kw in ({"topic": "crypto"}, {"time_range": "decade"}, {"max_results": 50}):
            with self.subTest(kw=kw), self.assertRaises(tavily.TavilyUnavailable):
                self.search(FakePost(), **kw)

    def test_snippets_and_titles_are_single_line(self):
        body = tavily_body([{"title": "A\n  B", "url": "https://a.com/1", "content": "line1\n\n# line2\n  line3"}])
        res = self.search(FakePost(body=body))
        s = tavily.parse_results(res["response"], res["retrieved_at"])[0]
        self.assertEqual((s["title"], s["snippet"]), ("A B", "line1 # line2 line3"))

    def test_non_http_urls_dropped(self):
        body = tavily_body([{"title": "x", "url": "javascript:alert(1)", "content": ""},
                            {"title": "y", "url": "ftp://x.test/a", "content": ""}])
        res = self.search(FakePost(body=body))
        self.assertEqual(tavily.parse_results(res["response"], res["retrieved_at"]), [])


class Budget(Base):
    def test_credit_reserved_before_request(self):
        def post(url, headers, payload):
            rows = journal.read(self.usage)
            self.assertEqual(rows[-1]["status"], "reserved")  # logged before the network call
            return 200, tavily_body()
        self.search(post)
        self.assertEqual(tavily.usage_summary(NOW, self.usage)["month_credits"], 1)

    def test_daily_and_monthly_caps(self):
        tight = {**SETTINGS, "tavily": {**SETTINGS["tavily"], "daily_credit_cap": 2, "monthly_credit_cap": 3}}
        post = FakePost()
        for i in range(2):
            self.search(post, settings=tight, max_results=i + 1)  # different payloads, no cache
        with self.assertRaises(tavily.TavilyUnavailable) as ctx:
            self.search(post, settings=tight, max_results=5)
        self.assertIn("daily credit cap", str(ctx.exception))
        tomorrow = NOW + dt.timedelta(days=1)
        self.search(post, settings=tight, now=tomorrow, max_results=6)
        with self.assertRaises(tavily.TavilyUnavailable) as ctx:
            self.search(post, settings=tight, now=tomorrow + dt.timedelta(hours=1), max_results=7)
        self.assertIn("monthly credit cap", str(ctx.exception))
        self.assertEqual(len(post.calls), 3)

    def test_advanced_depth_refused(self):
        post = FakePost()
        with self.assertRaises(tavily.TavilyUnavailable):
            self.search(post, depth="advanced")
        self.assertEqual(post.calls, [])

    def test_disabled(self):
        off = {**SETTINGS, "tavily": {**SETTINGS["tavily"], "enabled": False}}
        with self.assertRaises(tavily.TavilyUnavailable):
            self.search(FakePost(), settings=off)

    def test_plan_limit_blocks_rest_of_month(self):
        with self.assertRaises(tavily.TavilyUnavailable):
            self.search(FakePost(status=432, body='{"detail": "limit"}'))
        post = FakePost()
        with self.assertRaises(tavily.TavilyUnavailable) as ctx:
            self.search(post, max_results=3)
        self.assertIn("plan limit", str(ctx.exception))
        self.assertEqual(post.calls, [])
        self.search(post, now=NOW + dt.timedelta(days=10))  # 2026-11: new month, allowed again
        self.assertEqual(len(post.calls), 1)

    def test_payg_limit_never_continues(self):
        with self.assertRaises(tavily.TavilyUnavailable) as ctx:
            self.search(FakePost(status=433, body="{}"))
        self.assertIn("never enables paid", str(ctx.exception))

    def test_adjustment_when_tavily_charges_more(self):
        self.search(FakePost(body=tavily_body(credits=3)))
        self.assertEqual(tavily.usage_summary(NOW, self.usage)["month_credits"], 3)

    def test_usage_log_is_union_mergeable_jsonl(self):
        self.assertEqual(tavily.USAGE_LOG.suffix, ".jsonl")
        attrs = (Path(__file__).resolve().parent.parent / ".gitattributes").read_text()
        self.assertIn("memory/state/*.jsonl merge=union", attrs)


class Caching(Base):
    def test_cache_hit_costs_nothing(self):
        post = FakePost()
        a = self.search(post)
        b = self.search(post, now=NOW + dt.timedelta(hours=1))
        self.assertEqual(len(post.calls), 1)
        self.assertTrue(b["cached"])
        self.assertEqual(b["credits"], 0)
        self.assertEqual(b["retrieved_at"], a["retrieved_at"])  # original retrieval time is kept
        self.assertEqual(tavily.usage_summary(NOW, self.usage)["month_credits"], 1)

    def test_cache_expires(self):
        post = FakePost()
        self.search(post)
        self.search(post, now=NOW + dt.timedelta(hours=7))
        self.assertEqual(len(post.calls), 2)


class Errors(Base):
    def test_http_errors_fail_closed_without_retry_and_redact_key(self):
        for status in (400, 401, 429, 500):
            with self.subTest(status=status):
                post = FakePost(status=status, body=f'{{"detail": "bad key {KEY}"}}')
                with self.assertRaises(tavily.TavilyUnavailable) as ctx:
                    self.search(post, max_results=status % 20 or 1)
                self.assertEqual(len(post.calls), 1)
                self.assertNotIn(KEY, str(ctx.exception))

    def test_network_error(self):
        def boom(url, headers, payload):
            raise tavily.TavilyUnavailable("Tavily network error: TimeoutError")
        with self.assertRaises(tavily.TavilyUnavailable):
            self.search(boom)

    def test_non_json(self):
        with self.assertRaises(tavily.TavilyUnavailable):
            self.search(FakePost(body="<html>"))

    def test_date_normalization(self):
        cases = {"2026-10-27T13:00:00Z": "2026-10-27", "Tue, 27 Oct 2026 14:05:00 GMT": "2026-10-27",
                 "2026-10-27": "2026-10-27", None: None, "": None, "yesterday": None}
        for raw, exp in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(tavily.normalize_date(raw), exp)


class Dispatcher(Base):
    def test_tavily_primary_returns_sources_not_answers(self):
        r = self.ask(FakePost())
        self.assertEqual(r["provider"], "tavily")
        self.assertIsNone(r["answer"])
        self.assertEqual(len(r["sources"]), 3)
        self.assertTrue(r["sufficiency"]["sufficient"])
        md = research.to_markdown(r)
        self.assertIn("published 2026-10-27", md)
        self.assertIn("publish date unknown", md)
        self.assertIn("untrusted data", md)

    def test_no_key_and_perplexity_disabled_is_unavailable(self):
        with self.assertRaises(research.ResearchUnavailable) as ctx:
            self.ask(FakePost(), env={})
        msg = str(ctx.exception)
        self.assertIn("tavily: TAVILY_API_KEY not set", msg)
        self.assertIn("perplexity: Perplexity disabled", msg)

    def test_perplexity_stays_disabled_by_default_in_repo(self):
        from xahil.config import load_integrations
        cfg = load_integrations()
        self.assertFalse(cfg["perplexity"]["enabled"])
        self.assertEqual(cfg["research"]["primary"], "tavily")
        self.assertEqual(cfg["tavily"]["allowed_depths"], ["basic"])
        self.assertLessEqual(cfg["tavily"]["monthly_credit_cap"], 1000)


class Sufficiency(unittest.TestCase):
    REQ = research.DEFAULT_REQUIREMENTS

    def s(self, url, date):
        return {"url": url, "domain": tavily.domain_of(url), "published_date": date}

    def test_rules(self):
        good = [self.s("https://a.com/1", "2026-10-27"), self.s("https://b.com/1", None)]
        cases = {
            "nothing": ([], False),
            "one source": ([self.s("https://a.com/1", "2026-10-27")], False),
            "same domain twice": ([self.s("https://a.com/1", "2026-10-27"), self.s("https://www.a.com/2", "2026-10-27")], False),
            "undated only": ([self.s("https://a.com/1", None), self.s("https://b.com/1", None)], False),
            "stale": ([self.s("https://a.com/1", "2026-09-01"), self.s("https://b.com/1", "2026-09-02")], False),
            "good": (good, True),
        }
        for name, (sources, exp) in cases.items():
            with self.subTest(name):
                r = research.assess_sufficiency(sources, TODAY, self.REQ)
                self.assertEqual(r["sufficient"], exp, r["reasons"])
                if not exp:
                    self.assertIn("never as a bullish signal", r["interpretation"])


class NeverBullish(unittest.TestCase):
    def ctx(self, ev):
        from tests.helpers import CLOCK_TS, account, asset, limits, quote
        from xahil.risk import parse_ts
        return RiskContext(account=account(), positions=[], open_orders=[], week_orders=[], limits=limits(),
                           asset=asset(), quote=quote(20, 19.99), research_documented=True,
                           now=parse_ts(CLOCK_TS), market_open=True, daytrade_count=0, same_day_buy_symbols=set(),
                           research_evidence=ev)

    def test_missing_or_insufficient_evidence_blocks_entry(self):
        order = OrderRequest("buy", "AAPL", qty=1)
        for ev in (None, {"sufficient": False, "reasons": ["0 sources (< 2)"]}, {}):
            with self.subTest(ev=ev):
                v = evaluate_order(order, self.ctx(ev))
                self.assertFalse(v.checks["research_sufficient"])
                self.assertTrue(any("not a buy signal" in r for r in v.reasons))
        self.assertTrue(evaluate_order(order, self.ctx({"sufficient": True, "reasons": []})).approved)

    def test_exits_never_need_research(self):
        from tests.helpers import position
        c = self.ctx(None)
        c.positions = [position("AAPL", 2, 20)]
        sell = OrderRequest("sell", "AAPL", type="stop", time_in_force="gtc", qty=2, stop_price=18, purpose="protective")
        self.assertTrue(evaluate_order(sell, c).approved)


class ResearchCli(CliHarness):
    def run_research(self, argv, post, env=ENV):
        with mock.patch.object(tavily, "_post", post), mock.patch.dict("os.environ", env), \
                mock.patch.object(cli, "load_integrations", lambda: SETTINGS), \
                mock.patch.object(research, "load_integrations", lambda: SETTINGS):
            return self.run_cli(argv, FakeAlpaca())

    def test_research_log_stores_full_evidence_metadata(self):
        code, text = self.run_research(["research", "Ford earnings", "--topic", "finance", "--log",
                                        "--symbol", "F", "--workflow", "pre-market"], FakePost())
        self.assertEqual(code, 0, text)
        rows = [r for r in journal.read(learning.EVIDENCE) if r["symbol"] == "F"]
        self.assertEqual(len(rows), 3)
        r0 = rows[0]
        for field in ("source", "domain", "published_date", "retrieved_at", "provider", "query", "title", "snippet"):
            self.assertIn(field, r0)
        self.assertEqual((r0["provider"], r0["published_date"], r0["kind"]), ("tavily", "2026-10-27", "data"))
        self.assertIsNone(rows[2]["published_date"])
        self.assertIn("Evidence logged", text)

    def test_insufficient_exit_6(self):
        thin = tavily_body([{"title": "x", "url": "https://only.example/1", "content": "", "published_date": None}])
        code, text = self.run_research(["research", "obscure"], FakePost(body=thin))
        self.assertEqual(code, cli.EXIT_INSUFFICIENT)
        self.assertIn("INSUFFICIENT", text)

    def test_unavailable_exit_3(self):
        code, _ = self.run_research(["research", "anything"], FakePost(), env={})
        self.assertEqual(code, cli.EXIT_UNAVAILABLE)

    def test_research_check_and_entry_gate(self):
        code, text = self.run_cli(["research-check", "MSFT", "--date", "2026-10-28"], FakeAlpaca())
        self.assertEqual(code, 6)
        # market-wide evidence and other days never count for a symbol
        learning.record_evidence("MARKET", "macro", "https://x.com/1", "news", "w", date="2026-10-28",
                                 path=learning.EVIDENCE, published_date="2026-10-28")
        learning.record_evidence("MSFT", "old", "https://y.com/1", "news", "w", date="2026-10-27",
                                 path=learning.EVIDENCE, published_date="2026-10-27")
        self.research.write_text(self.research.read_text() + "\n## 2026-10-28 - addendum\nMSFT catalyst\n")
        code, text = self.run_cli(["check-buy", "MSFT", "--qty", "1"], FakeAlpaca())
        self.assertEqual(code, cli.EXIT_REJECTED)
        self.assertIn('"research_sufficient": false', text)
        code, _ = self.run_cli(["research-check", "AAPL", "--date", "2026-10-28"], FakeAlpaca())  # seeded
        self.assertEqual(code, 0)

    def test_evidence_command_with_published_date(self):
        code, text = self.run_cli(["evidence", "MSFT", "--claim", "c", "--source", "https://z.com/1",
                                   "--published", "2026-10-27", "--provider", "websearch"], FakeAlpaca())
        self.assertEqual(code, 0, text)
        self.assertEqual(json.loads(text)["published_date"], "2026-10-27")
        code, _ = self.run_cli(["evidence", "MSFT", "--claim", "c", "--source", "https://z.com/1",
                                "--published", "27/10/2026"], FakeAlpaca())
        self.assertEqual(code, cli.EXIT_ERROR)

    def test_preflight_reports_research_without_key(self):
        with mock.patch.dict("os.environ", ENV), mock.patch.object(cli, "load_integrations", lambda: SETTINGS):
            code, text = self.run_cli(["preflight"], FakeAlpaca())
        self.assertEqual(code, 0)
        self.assertNotIn(KEY, text)
        self.assertEqual(json.loads(text)["research"]["tavily"]["key"], "set")


if __name__ == "__main__":
    unittest.main()
