"""Tests for the maliyet cost statistics (bin/maliyet*.py). Stdlib only, synthetic data only.

Run:  python3 -I -m unittest discover -s plugins/kota-temposu/tests     (from the repo root)

The transcripts are generated at run time in a temporary directory (timestamps relative to "now" where the
rolling windows matter, fixed dates where calendar grouping matters). Nothing reads or writes the real ~/.claude.
CLI tests run `python3 -I bin/maliyet.py` as a subprocess with CLAUDE_CONFIG_DIR / HOME pointing at the temp tree.
"""
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.normpath(os.path.join(HERE, "..", "bin"))
MALIYET = os.path.join(BIN, "maliyet.py")

# Module-level constants of the code under test are derived from the environment at import time:
# point them at a scratch tree BEFORE importing.
_ROOT = tempfile.mkdtemp(prefix="maliyet-test-")
_HOME = os.path.join(_ROOT, "home")
_CFG = os.path.join(_HOME, ".claude")
os.makedirs(_CFG)
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
os.environ["CLAUDE_CONFIG_DIR"] = _CFG
os.environ["MALIYET_TZ"] = "UTC"
os.environ["CLAUDE_SHARED_BILDIRIM"] = "0"   # no desktop notification from a test run (maliyet_paylas.bildir)
os.environ["CLAUDE_SHARED_VERIM"] = "0"       # no cost-report scan and no GitHub Projects call from the share tests (maliyet_verim)
os.environ.pop("MALIYET_FIYATLAR", None)
sys.path.insert(0, BIN)
import maliyet  # noqa: E402
import maliyet_ayristir as AY  # noqa: E402
import maliyet_cekirdek as CK  # noqa: E402

# Fake models with round prices (USD per 1M tokens): the expected numbers below are plain arithmetic.
PRICES = {"modeller": {
    "claude-test-1": {"in": 10.0, "out": 50.0, "read": 1.0},
    "claude-adv-1": {"in": 20.0, "out": 100.0, "read": 2.0},
}}


def tearDownModule():
    shutil.rmtree(_ROOT, ignore_errors=True)


def iso(e):
    return datetime.datetime.fromtimestamp(e, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def epoch(y, m, d, h=12, mi=0):
    return datetime.datetime(y, m, d, h, mi, tzinfo=datetime.timezone.utc).timestamp()


def asst(ts, mid, model="claude-test-1", inp=0, out=0, cr=0, cw5=0, cw1h=0, adv=None, total_cc=None):
    usage = {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cr,
             "cache_creation_input_tokens": cw5 + cw1h if total_cc is None else total_cc,
             "cache_creation": {"ephemeral_5m_input_tokens": cw5, "ephemeral_1h_input_tokens": cw1h}}
    if adv:
        usage["iterations"] = [dict({"type": "advisor_message"}, **a) for a in adv]
    return {"type": "assistant", "timestamp": iso(ts), "message": {"id": mid, "model": model, "usage": usage}}


def rd(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def rows(path):
    return [json.loads(x) for x in rd(path).splitlines() if x.strip()]


def write(path, lines):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in lines:
            f.write((r if isinstance(r, str) else json.dumps(r)) + "\n")


class Cfg:
    """A throw-away Claude config dir with a price override for the fake models."""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="cfg-", dir=_ROOT)
        os.makedirs(os.path.join(self.dir, "maliyet"))
        with open(os.path.join(self.dir, "maliyet", "fiyatlar.json"), "w") as f:
            json.dump(PRICES, f)

    def transcript(self, project, session, lines, sub=None):
        base = os.path.join(self.dir, "projects", project)
        path = os.path.join(base, session + ".jsonl") if sub is None else os.path.join(base, session, "subagents", sub)
        write(path, lines)
        return path

    def run(self, *args, env=None, flags=("-I",)):
        e = dict(os.environ, CLAUDE_CONFIG_DIR=self.dir, HOME=_HOME, MALIYET_TZ="UTC", LC_ALL="C.UTF-8")
        e.pop("MALIYET_FIYATLAR", None)
        e.update(env or {})
        r = subprocess.run([sys.executable, *flags, MALIYET, *args], capture_output=True, text=True, encoding="utf-8", env=e, timeout=120)
        return r

    def json(self, *args):
        r = self.run("--json", *args)
        assert r.returncode == 0, r.stderr
        return json.loads(r.stdout)


class ParserAndPricing(unittest.TestCase):
    def setUp(self):
        self.cfg = Cfg()

    def parse(self, lines, name="a.jsonl"):
        p = os.path.join(self.cfg.dir, name)
        write(p, lines)
        return AY.ayristir(p)["reqs"]

    def test_token_cache_and_advisor_pricing(self):
        now = epoch(2026, 3, 1)
        adv = [{"model": "claude-adv-1", "input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 200,
                "cache_creation": {"ephemeral_5m_input_tokens": 10, "ephemeral_1h_input_tokens": 5}, "cache_creation_input_tokens": 15}]
        # request: in 1000, out 500, cache read 2000, write 5m 400, write 1h 100, plus one advisor call
        self.cfg.transcript("p", "s1", [asst(now, "m1", inp=1000, out=500, cr=2000, cw5=400, cw1h=100, adv=adv)])
        self.assertEqual(self.cfg.run("--kaydet").returncode, 0)
        rec = rows(os.path.join(self.cfg.dir, "maliyet", "gunluk.jsonl"))[0]   # 6-decimal record
        # main: 1000*10 + 500*50 + 2000*1 + 400*1.25*10 + 100*2.0*10 = 10000+25000+2000+5000+2000 = 44000 -> $0.044
        # advisor: 100*20 + 50*100 + 200*2 + 10*1.25*20 + 5*2.0*20 = 2000+5000+400+250+200 = 7850 -> $0.00785
        self.assertAlmostEqual(rec["model"]["claude-test-1"]["usd"], 0.044, places=6)
        self.assertAlmostEqual(rec["model"]["claude-adv-1"]["usd"], 0.00785, places=6)
        self.assertAlmostEqual(rec["usd"], 0.05185, places=6)
        self.assertAlmostEqual(rec["dan"]["usd"], 0.00785, places=6)
        self.assertEqual(rec["dan"]["n"], 1)
        day = self.cfg.json()["gun"]["satirlar"][0]                       # the view rounds to 4 decimals
        self.assertAlmostEqual(day["usd"], 0.0519, places=4)
        self.assertEqual(day["danisman"]["n"], 1)
        self.assertEqual(day["tok"], {"in": 1000, "out": 500, "cr": 2000, "cw5": 400, "cw1h": 100})
        self.assertAlmostEqual(day["isabet"], 2000 / 3500, places=4)   # cache hit = read / (in + read + writes)
        # advisor tokens are counted under the advisor's own model (Fable runs only as an advisor: it showed "23% · 0")
        self.assertEqual({k: rec["model"]["claude-adv-1"].get(k, 0) for k in ("in", "out", "cr", "cw5", "cw1h")},
                         {"in": 100, "out": 50, "cr": 200, "cw5": 10, "cw1h": 5})

    def test_streaming_duplicates_count_once(self):
        t = epoch(2026, 3, 1)
        r = self.parse([asst(t, "m1", inp=10, out=1), asst(t + 1, "m1", inp=10, out=40), asst(t + 2, "m2", inp=5, out=2)])
        self.assertEqual(len(r), 2)
        self.assertEqual(r[0]["out"], 40)   # the last usage of the same message id wins

    def test_corrupt_and_partial_lines_are_skipped(self):
        t = epoch(2026, 3, 1)
        good = asst(t, "m1", inp=1000, out=0)
        r = self.parse([
            json.dumps(good),
            '{"type":"assistant","timestamp":"2026-03-01T12:00:00.000Z","message":{"id":"m9","model":"claude-test-1","usage":{"input_tok',  # truncated
            "not json at all",
            "[1, 2, 3]",                                   # valid JSON, not an object
            '{"type":"assistant","timestamp":12345,"message":{"id":"m8","model":"claude-test-1","usage":{"input_tokens":7}}}',  # odd timestamp type
            '{"type":"assistant","message":"oops"}',       # message is not an object
            "",
            json.dumps(asst(t + 5, "m2", inp=500)),
        ])
        self.assertEqual(sorted(x["in"] for x in r), [7, 500, 1000])
        # the whole pipeline survives the same file
        self.cfg.transcript("p", "s", [json.dumps(good), "{broken", json.dumps(asst(t + 5, "m2", inp=500))])
        day = self.cfg.json()["gun"]["satirlar"][0]
        self.assertEqual(day["istek"], 2)
        self.assertAlmostEqual(day["usd"], 1500 * 10 / 1e6, places=6)

    def test_unknown_model_is_unpriced_not_guessed(self):
        t = epoch(2026, 3, 1)
        self.cfg.transcript("p", "s", [asst(t, "m1", model="claude-test-1", inp=1000),
                                       asst(t + 1, "m2", model="claude-mystery-9", inp=2000000, out=1000)])
        d = self.cfg.json()
        day = d["gun"]["satirlar"][0]
        self.assertAlmostEqual(day["usd"], 0.01, places=6)              # only the priced model costs anything
        self.assertEqual(day["fiyatsiz_token"], 2001000)                # the rest is reported, not silently dropped
        out = self.cfg.run().stdout
        self.assertIn("UYARI: 2.001.000 token fiyat tablosunda olmayan", out)

    def test_packaged_price_table_is_valid_and_documented(self):
        with open(AY.FIYAT_PAKET, encoding="utf-8") as f:
            d = json.load(f)
        self.assertIn("kaynak", d)
        self.assertRegex(d["kaynak"], r"20\d\d-\d\d-\d\d")             # source and verification date are recorded
        self.assertGreater(len(d["modeller"]), 3)
        for name, p in d["modeller"].items():
            for k in ("in", "out", "read"):
                self.assertIsInstance(p[k], (int, float), (name, k))
        self.assertEqual(AY.FIYAT_PAKET, os.path.join(BIN, "maliyet-fiyatlar.json"))

    def test_local_override_replaces_only_named_models(self):
        keep = AY.FIYAT_YEREL
        try:
            AY.FIYAT_YEREL = os.path.join(self.cfg.dir, "maliyet", "fiyatlar.json")   # PRICES override from Cfg()
            merged = AY.fiyat_yukle()
        finally:
            AY.FIYAT_YEREL = keep
        self.assertEqual(merged["modeller"]["claude-test-1"]["in"], 10.0)
        with open(AY.FIYAT_PAKET, encoding="utf-8") as f:
            pk = json.load(f)
        for m in pk["modeller"]:
            self.assertIn(m, merged["modeller"])                        # packaged models are kept
        self.assertNotEqual(AY.fiyat_ozeti(merged), AY.fiyat_ozeti(pk)) # the cache key follows the effective prices
        f = AY.Fiyat(merged)
        self.assertEqual(f.bul("claude-test-1-20270101")["in"], 10.0)   # prefix match
        self.assertIsNone(f.bul("something-else"))

    def test_subagent_and_workflow_transcripts_are_scanned_and_split(self):
        t = epoch(2026, 3, 1)
        self.cfg.transcript("p", "s1", [asst(t, "m1", inp=1000)])
        self.cfg.transcript("p", "s1", [asst(t + 1, "a1", inp=3000)], sub="agent-aaa.jsonl")
        self.cfg.transcript("p", "s1", [asst(t + 2, "w1", inp=5000)], sub=os.path.join("workflows", "wf1", "agent-bbb.jsonl"))
        with open(os.path.join(self.cfg.dir, "projects", "p", "s1", "subagents", "agent-aaa.meta.json"), "w") as f:
            json.dump({"agentType": "reviewer", "description": "SECRET TEXT"}, f)
        day = self.cfg.json()["gun"]["satirlar"][0]
        self.assertAlmostEqual(day["ana"]["usd"], 1000 * 10 / 1e6, places=6)
        self.assertAlmostEqual(day["alt"]["usd"], 8000 * 10 / 1e6, places=6)
        self.assertIn("reviewer", day["tur"])
        self.assertEqual(day["ajan_kosu"], 2)
        raw = self.cfg.run("--json").stdout
        self.assertNotIn("SECRET TEXT", raw)                            # descriptions are never read into the output


class Aggregation(unittest.TestCase):
    def setUp(self):
        self.cfg = Cfg()

    def test_hourly_and_daily_totals_are_written(self):
        # recent (hourly records keep 45 days) and aligned to a UTC midnight: 23:50 and 00:10 straddle it, 00:10 and 00:50 share an hour
        mid = (time.time() // 86400 - 3) * 86400
        d0 = datetime.datetime.fromtimestamp(mid - 3600, datetime.timezone.utc)
        d1 = datetime.datetime.fromtimestamp(mid, datetime.timezone.utc)
        self.cfg.transcript("p", "s", [
            asst(mid - 600, "a", inp=100000),    # $1.00, day D-1 hour 23
            asst(mid + 600, "b", inp=200000),    # $2.00, day D hour 00
            asst(mid + 3000, "c", inp=300000),   # $3.00, day D hour 00
        ])
        r = self.cfg.run("--kaydet")
        self.assertEqual(r.returncode, 0, r.stderr)
        days = rows(os.path.join(self.cfg.dir, "maliyet", "gunluk.jsonl"))
        self.assertEqual([(d["gun"], round(d["usd"], 6)) for d in days], [(d0.strftime("%Y-%m-%d"), 1.0), (d1.strftime("%Y-%m-%d"), 5.0)])
        hours = rows(os.path.join(self.cfg.dir, "maliyet", "saatlik.jsonl"))
        got = {h["saat"]: round(h["usd"], 6) for h in hours}
        self.assertEqual(got, {d0.strftime("%Y-%m-%dT23"): 1.0, d1.strftime("%Y-%m-%dT00"): 5.0})
        if os.name == "posix":
            for n in ("gunluk.jsonl", "saatlik.jsonl", "onbellek.json", "durum.cache"):
                self.assertEqual(os.stat(os.path.join(self.cfg.dir, "maliyet", n)).st_mode & 0o777, 0o600, n)

    def test_record_with_no_transcripts_does_not_crash(self):
        r = self.cfg.run("--kaydet")                      # fresh install: nothing to record yet
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("0 gün", r.stdout)
        self.assertEqual(self.cfg.run("--durum-yenile").returncode, 0)

    def test_recorded_history_survives_deleted_transcripts(self):
        p = self.cfg.transcript("p", "s", [asst(epoch(2026, 3, 1), "a", inp=100000)])
        self.assertEqual(self.cfg.run("--kaydet").returncode, 0)
        os.remove(p)   # Claude Code removes old transcripts; the daily record must stay
        d = self.cfg.json()
        self.assertEqual([x["anahtar"] for x in d["gun"]["satirlar"]], ["2026-03-01"])
        self.assertAlmostEqual(d["gun"]["satirlar"][0]["usd"], 1.0, places=6)

    def test_rolling_windows_from_hourly_buckets(self):
        simdi = 1_800_000_000 // 3600 * 3600 + 1800          # mid-hour
        sh = simdi // 3600
        saat = {sh: 1.0, sh - 23: 2.0, sh - 24: 4.0, sh - 167: 8.0, sh - 168: 16.0, sh - 719: 32.0, sh - 720: 64.0}
        rec = {h: {"usd": v, "ana": v, "alt": 0.0, "dan": 0.0, "model": {"claude-test-1": v}} for h, v in saat.items()}
        P = {p["etiket"]: p for p in CK.pencere_hesapla({}, rec, simdi=simdi)}
        # the window is the last N*24 hour buckets, current partial hour included: [sh-(24N-1), sh]
        self.assertAlmostEqual(P["son 24sa"]["usd"], 1.0 + 2.0)
        self.assertAlmostEqual(P["7g"]["usd"], 1 + 2 + 4 + 8)
        self.assertAlmostEqual(P["30g"]["usd"], 1 + 2 + 4 + 8 + 16 + 32)
        self.assertAlmostEqual(P["7g"]["ana"]["usd"], 15.0)

    def test_window_start_is_completed_from_daily_totals(self):
        # hourly record starts at 2026-03-10 00:00 UTC; the day before has a daily total only ($24, no hourly buckets).
        # "now" is 2026-03-10 12:30, so the 24h window starts 2026-03-09 13:00 and overlaps 11 of that day's 24 hours.
        maliyet._tz()
        simdi = epoch(2026, 3, 10, 12, 30)
        sh = int(simdi // 3600)
        e = int(epoch(2026, 3, 10, 0, 0) // 3600)
        rec = {h: {"usd": 1.0, "ana": 1.0, "alt": 0.0, "dan": 0.0, "model": {"claude-test-1": 1.0}} for h in range(e, sh + 1)}
        days = {"2026-03-09": {"usd": 24.0, "ana": {"usd": 24.0}, "model": {"claude-test-1": {"usd": 24.0}}}}
        parts = maliyet.pencere_parcalar(1, sh, e, {h: r["usd"] for h, r in rec.items()}, {"2026-03-09": 24.0})
        self.assertEqual([p[:2] for p in parts if p[0] == "g"], [("g", "2026-03-09")])
        self.assertAlmostEqual([p[2] for p in parts if p[0] == "g"][0], 11 / 24)
        P = CK.pencere_hesapla(days, rec, simdi=simdi)[0]
        self.assertAlmostEqual(P["usd"], 13.0 + 11.0)                 # 13 recorded hours + 11/24 of the day total
        self.assertAlmostEqual(P["tamamlanan_usd"], 11.0)
        # the same window without any daily total invents nothing
        parts = maliyet.pencere_parcalar(1, sh, e, {h: 1.0 for h in range(e, sh + 1)}, {})
        self.assertFalse([p for p in parts if p[0] == "g"])

    def test_rolling_windows_cli_and_status_line(self):
        now = time.time()
        # $12 one hour ago, $30 three days ago, $100 twenty days ago, $1000 forty days ago (outside every window)
        for i, (age_h, inp) in enumerate([(1, 1_200_000), (72, 3_000_000), (480, 10_000_000), (960, 100_000_000)]):
            self.cfg.transcript("p", "s%d" % i, [asst(now - age_h * 3600, "m%d" % i, inp=inp)])
        r = self.cfg.run("--pencere")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout, r"son 24sa\s+\$12,00")
        self.assertRegex(r.stdout, r"7g\s+\$42,00")
        self.assertRegex(r.stdout, r"30g\s+\$142,00")
        self.assertEqual(self.cfg.run("--kaydet").returncode, 0)
        # status line fast path: exactly as statusline.sh calls it, reads durum.cache only
        s = self.cfg.run("--durum", flags=("-S", "-E"))
        self.assertEqual(s.returncode, 0, s.stderr)
        self.assertEqual(s.stdout, "son 24sa $12 · 7g $42 · 30g $142 · API eşdeğeri (fatura değil)")

    def test_status_line_refreshes_in_background(self):
        # no cache: prints nothing at once and does not scan itself; starts one detached refresh (python only: no flock/nice
        # binary, so macOS works too) that writes durum.cache
        self.cfg.transcript("p", "s", [asst(time.time() - 3600, "a", inp=1_200_000)])
        t0 = time.time()
        s = self.cfg.run("--durum", flags=("-S", "-E"))
        self.assertEqual((s.returncode, s.stdout), (0, ""))
        self.assertLess(time.time() - t0, 10)                      # the caller never waits for the scan
        cache = os.path.join(self.cfg.dir, "maliyet", "durum.cache")
        for _ in range(120):
            if os.path.isfile(cache):
                break
            time.sleep(0.25)
        self.assertTrue(os.path.isfile(cache), "background refresh did not produce durum.cache")
        time.sleep(1)                                              # let the child release its lock
        s = self.cfg.run("--durum", flags=("-S", "-E"))
        self.assertEqual(s.stdout, "son 24sa $12 · 7g $12 · 30g $12 · API eşdeğeri (fatura değil)")   # UTF-8 bytes whatever the console code page is

    def test_refresh_lock_is_exclusive(self):
        os.makedirs(os.path.join(self.cfg.dir, "maliyet"), exist_ok=True)
        kilit = os.path.join(self.cfg.dir, "maliyet", "durum.kilit")
        fd = os.open(kilit, os.O_RDWR | os.O_CREAT)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.cfg.transcript("p", "s", [asst(time.time() - 3600, "a", inp=1_200_000)])
            r = self.cfg.run("--durum-yenile")                    # another refresh holds the lock: exits quietly, scans nothing
            self.assertEqual((r.returncode, r.stdout), (0, ""))
            self.assertFalse(os.path.exists(os.path.join(self.cfg.dir, "maliyet", "durum.cache")))
        finally:
            os.close(fd)


class BackgroundRefreshWrites(unittest.TestCase):
    """--durum-yenile (the status-line background refresh) keeps the on-disk records and the minute buckets bounded."""

    def setUp(self):
        self.cfg = Cfg()

    def test_refresh_writes_the_daily_record_without_the_timer(self):
        self.cfg.transcript("p", "s", [asst(time.time() - 3600, "a", inp=1_200_000)])
        self.assertEqual(self.cfg.run("--durum-yenile").returncode, 0)
        gunluk = os.path.join(self.cfg.dir, "maliyet", "gunluk.jsonl")      # macOS has no daily timer: the refresh writes it
        self.assertEqual(len(rows(gunluk)), 1)
        self.assertAlmostEqual(sum(r["usd"] for r in rows(gunluk)), 12.0, places=4)
        before = os.stat(gunluk).st_mtime_ns
        time.sleep(0.05)
        self.assertEqual(self.cfg.run("--durum-yenile").returncode, 0)
        self.assertEqual(os.stat(gunluk).st_mtime_ns, before)                # unchanged content is not rewritten

    def test_minute_buckets_older_than_8_days_are_dropped_from_unread_files_too(self):
        now = time.time()
        self.cfg.transcript("p", "s", [asst(now - 3600, "new", inp=1000), asst(now - 6 * 86400, "mid", inp=1000)])
        self.assertEqual(self.cfg.run("--durum-yenile").returncode, 0)
        onb = os.path.join(self.cfg.dir, "maliyet", "onbellek.json")
        d = json.loads(rd(onb))
        F = next(iter(d["dosyalar"].values()))
        self.assertEqual(len(F["dk"]), 2)
        old = str(int((now - 20 * 86400) // 60))                              # a stale bucket from an earlier, longer retention
        F["dk"][old] = 1.0
        with open(onb, "w", encoding="utf-8") as f:
            json.dump(d, f)
        self.assertEqual(self.cfg.run("--durum-yenile").returncode, 0)        # the transcript is unchanged: the cache entry is reused
        F = next(iter(json.loads(rd(onb))["dosyalar"].values()))
        self.assertNotIn(old, F["dk"])
        self.assertEqual(len(F["dk"]), 2)


class Deduplication(unittest.TestCase):
    """One API response is counted once, whatever number of transcript files carries a copy of it
    (resumed sessions copy the earlier history; the sub-agent files are copied into the new session folder)."""

    T = epoch(2026, 3, 1)

    def setUp(self):
        self.cfg = Cfg()

    def day(self):
        return self.cfg.json()["gun"]["satirlar"][0]

    def test_streaming_repeats_take_the_final_usage_not_the_sum(self):
        t = self.T
        p = os.path.join(self.cfg.dir, "a.jsonl")
        # same message id as growing partial usage; the lines may even arrive out of order, the response counts once
        write(p, [asst(t, "m1", inp=100, out=5, cr=1000, cw5=10), asst(t + 1, "m1", inp=100, out=30, cr=1000, cw5=10),
                  asst(t + 2, "m1", inp=100, out=80, cr=1000, cw5=10), asst(t + 3, "m1", inp=100, out=20, cr=1000, cw5=10)])
        r = AY.ayristir(p)["reqs"]
        self.assertEqual(len(r), 1)
        self.assertEqual((r[0]["in"], r[0]["out"], r[0]["cr"], r[0]["cw5"], r[0]["ts"]), (100, 80, 1000, 10, t))

    def test_key_is_message_id_then_request_id_then_uuid(self):
        t = self.T
        p = os.path.join(self.cfg.dir, "a.jsonl")
        a = asst(t, "m1", inp=1)
        b = asst(t + 1, None, inp=2)
        b["message"].pop("id")
        b["requestId"] = "req_9"
        c = asst(t + 2, None, inp=4)
        c["message"].pop("id")
        c["uuid"] = "u1"
        write(p, [a, dict(a, requestId="req_1"), b, dict(b), c, dict(c, uuid="u2")])
        r = AY.ayristir(p)["reqs"]
        # m1 once (the extra requestId does not split it), req_9 once, and the uuid fallback is per line: 2 distinct uuids
        self.assertEqual(sorted(x["in"] for x in r), [1, 2, 4, 4])

    def test_resumed_session_copy_in_another_file_counts_once(self):
        t = self.T
        hist = [asst(t, "a", inp=100000), asst(t + 60, "b", inp=200000, out=1000)]
        self.cfg.transcript("p", "orig", hist)
        self.cfg.transcript("p", "resumed", hist + [asst(t + 7200, "c", inp=300000)])   # copy of the history + a new response
        d = self.day()
        self.assertEqual(d["istek"], 3)
        self.assertAlmostEqual(d["usd"], (100000 + 200000 + 300000) * 10 / 1e6 + 1000 * 50 / 1e6, places=6)
        self.assertAlmostEqual(d["ana"]["usd"], d["usd"], places=6)
        self.assertEqual(d["alt"]["usd"], 0)
        self.assertEqual({x["id"]: round(x["usd"], 6) for x in d["en_oturum"]}, {"orig": 3.05, "resumed": 3.0})   # history -> the original session

    def test_result_does_not_depend_on_file_names_or_cache_state(self):
        t = self.T
        hist = [asst(t, "a", inp=100000), asst(t + 60, "b", inp=200000)]
        self.cfg.transcript("p", "zzz-orig", hist)
        self.assertAlmostEqual(self.day()["usd"], 3.0, places=6)                 # first (cached) scan
        self.cfg.transcript("p", "aaa-resumed", hist + [asst(t + 900, "c", inp=100000)])
        self.assertAlmostEqual(self.day()["usd"], 4.0, places=6)                 # copy appears later: cache must not double count
        self.assertAlmostEqual(self.day()["usd"], 4.0, places=6)                 # and a cached rerun is stable
        r = self.cfg.run("--json", "--onbellek-yok")
        self.assertEqual(json.loads(r.stdout)["gun"]["satirlar"][0]["usd"], self.day()["usd"])
        os.remove(os.path.join(self.cfg.dir, "projects", "p", "zzz-orig.jsonl"))   # the original is deleted: the copy takes over
        self.assertAlmostEqual(self.day()["usd"], 4.0, places=6)

    def test_copied_subagent_files_count_once_and_start_one_run(self):
        t = self.T
        ag = [asst(t + 10, "x1", inp=100000), asst(t + 20, "x2", inp=50000)]
        self.cfg.transcript("p", "s1", [asst(t, "m1", inp=100000)])
        self.cfg.transcript("p", "s1", ag, sub="agent-aaa.jsonl")
        self.cfg.transcript("p", "s2", [asst(t, "m1", inp=100000), asst(t + 5000, "m2", inp=100000)])   # resumed s1
        self.cfg.transcript("p", "s2", ag, sub="agent-aaa.jsonl")                                      # folder copied
        self.cfg.transcript("p", "s2", ag, sub=os.path.join("workflows", "wf1", "agent-aaa.jsonl"))      # workflow copy
        d = self.day()
        self.assertAlmostEqual(d["ana"]["usd"], 2.0, places=6)    # m1 once, m2
        self.assertAlmostEqual(d["alt"]["usd"], 1.5, places=6)    # x1 + x2 once
        self.assertEqual(d["ajan_kosu"], 1)                       # three copies of one agent file = one run
        self.assertEqual(d["istek"], 4)

    def test_fork_agent_does_not_recount_the_parent_history(self):
        t = self.T
        hist = [asst(t, "a", inp=100000), asst(t + 60, "b", inp=100000)]
        self.cfg.transcript("p", "s1", hist)
        self.cfg.transcript("p", "s1", hist + [asst(t + 120, "f1", inp=100000)], sub="agent-fork.jsonl")
        with open(os.path.join(self.cfg.dir, "projects", "p", "s1", "subagents", "agent-fork.meta.json"), "w") as f:
            json.dump({"agentType": "fork"}, f)
        d = self.day()
        self.assertAlmostEqual(d["ana"]["usd"], 2.0, places=6)    # the main conversation keeps its own history
        self.assertAlmostEqual(d["alt"]["usd"], 1.0, places=6)    # the fork only its new response

    def test_yeniden_recomputes_days_with_transcripts_and_keeps_deleted_ones(self):
        t = self.T
        hist = [asst(t, "a", inp=100000), asst(t + 60, "b", inp=200000)]
        self.cfg.transcript("p", "orig", hist)
        self.cfg.transcript("p", "resumed", hist)
        gone = self.cfg.transcript("p", "gone", [asst(epoch(2026, 2, 1), "g", inp=100000)])
        recent = (time.time() // 86400 - 3) * 86400 + 600
        self.cfg.transcript("p", "recent", [asst(recent, "r1", inp=100000)])
        self.cfg.transcript("p", "recent2", [asst(recent, "r1", inp=100000)])    # a copy
        self.assertEqual(self.cfg.run("--kaydet").returncode, 0)
        os.remove(gone)                                                         # its transcript is deleted
        gl = os.path.join(self.cfg.dir, "maliyet", "gunluk.jsonl")
        sl = os.path.join(self.cfg.dir, "maliyet", "saatlik.jsonl")
        # simulate what the old parser wrote: copies counted twice
        for path, key in ((gl, "gun"), (sl, "h")):
            rs = rows(path)
            with open(path, "w", encoding="utf-8") as f:
                for r in rs:
                    if r.get("gun") != "2026-02-01":
                        r["usd"] *= 2
                    f.write(json.dumps(r) + "\n")
        self.assertEqual(self.cfg.run("--kaydet").returncode, 0)                # without --yeniden the stored rows stay
        day = {r["gun"]: r["usd"] for r in rows(gl)}
        self.assertAlmostEqual(day["2026-03-01"], 6.0, places=6)
        r = self.cfg.run("--kaydet", "--yeniden")
        self.assertEqual(r.returncode, 0, r.stderr)
        day = {x["gun"]: round(x["usd"], 6) for x in rows(gl)}
        self.assertEqual(day["2026-03-01"], 3.0)                                # recomputed, copies counted once
        self.assertEqual(day["2026-02-01"], 1.0)                                # transcript gone: old number stays
        self.assertIn("UYARI", r.stdout)
        self.assertIn("2026-02-01", r.stdout)
        self.assertEqual(sorted(round(h["usd"], 6) for h in rows(sl)), [1.0])   # hours recomputed as well

    def test_cached_and_uncached_scans_agree(self):
        t = self.T
        hist = [asst(t, "a", inp=100000), asst(t + 60, "b", inp=200000)]
        self.cfg.transcript("p", "orig", hist)
        self.cfg.transcript("p", "resumed", hist + [asst(t + 90, "c", inp=100000)])
        self.cfg.transcript("p", "resumed", hist, sub="agent-z.jsonl")
        def views(*a):
            d = json.loads(self.cfg.run("--json", *a).stdout)
            return {k: d[k] for k in ("gun", "hafta", "ay", "yil")}
        first = views()
        second = views()                                    # served from the file cache
        third = views("--onbellek-yok")
        self.assertEqual(first, second)
        self.assertEqual(first, third)


class SonWindowFloor(unittest.TestCase):
    """The "son <süre>" window of the status line is at least SON_MIN_SN long."""

    def test_short_window_reaches_back(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        now = time.time()
        with open(os.path.join(d, "s"), "w", encoding="utf-8") as f:
            f.write(("p3 21.0 30.0 %.0f"+chr(10)+"%.0f 15.0"+chr(10)+"%.0f 10.0"+chr(10)+"%.0f 1.0"+chr(10)) % (now - 10, now - 100, now - 400, now - 900))
        g = []
        maliyet._tur("s", "p3", 30.5, d, g)
        self.assertEqual([round(b) for b, _ in g], [round(now - 100), round(now - 400), round(now - 900)])
        b, tb = next(x for x in g if now - x[0] >= maliyet.SON_MIN_SN)
        self.assertEqual((round(now - b), tb), (400, 10.0))


class Views(unittest.TestCase):
    """Calendar grouping with fixed dates: 2025-12-30 (Tue) and 2026-01-02 (Fri) share ISO week 2026-W01."""

    def setUp(self):
        self.cfg = Cfg()
        self.cfg.transcript("p", "s1", [asst(epoch(2025, 12, 30), "a", inp=100000)])   # $1
        self.cfg.transcript("p", "s2", [asst(epoch(2026, 1, 2), "b", inp=200000)])     # $2

    def keys(self, view):
        return [(x["anahtar"], round(x["usd"], 6)) for x in self.cfg.json()[view]["satirlar"]]

    def test_daily_weekly_monthly_yearly_grouping(self):
        self.assertEqual(self.keys("gun"), [("2025-12-30", 1.0), ("2026-01-02", 2.0)])
        self.assertEqual(self.keys("hafta"), [("2026-W01", 3.0)])
        self.assertEqual(self.keys("ay"), [("2025-12", 1.0), ("2026-01", 2.0)])
        self.assertEqual(self.keys("yil"), [("2025", 1.0), ("2026", 2.0)])

    def test_cli_views_print_only_the_requested_view(self):
        for flag, expect, absent in (("--gun", "30.12 Sal", "AY GÖRÜNÜMÜ"), ("--hafta", "2026-W01", "GÜN GÖRÜNÜMÜ"),
                                      ("--ay", "2025-12", "HAFTA GÖRÜNÜMÜ"), ("--yil", "2025", "AY GÖRÜNÜMÜ")):
            r = self.cfg.run(flag)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn(expect, r.stdout)
            self.assertNotIn(absent, r.stdout)
            self.assertIn("EN PAHALI 10 OTURUM", r.stdout)
        r = self.cfg.run("--gun", "--son", "1")
        self.assertIn("02.01 Cum", r.stdout)
        self.assertNotIn("30.12", r.stdout)

    def test_summary_hides_empty_sections(self):
        # all data is months old: nothing for today, nothing in the last 7 days / 4 weeks, no window with spending
        r = self.cfg.run()
        self.assertEqual(r.returncode, 0, r.stderr)
        for hidden in ("BUGÜN", "SON 7 GÜN", "SON 4 HAFTA", "KAYAN PENCERELER", "modeller: -", "henüz kayıtlı istek yok"):
            self.assertNotIn(hidden, r.stdout, hidden)
        for shown in ("AYLAR", "YILLAR", "EN PAHALI 10 OTURUM"):
            self.assertIn(shown, r.stdout)

    def test_summary_shows_today_when_there_is_data(self):
        self.cfg.transcript("p", "today", [asst(time.time() - 1, "t", inp=100000)])
        r = self.cfg.run()
        for shown in ("KAYAN PENCERELER", "BUGÜN", "SON 7 GÜN", "SON 4 HAFTA", "AYLAR", "YILLAR"):
            self.assertIn(shown, r.stdout, shown)

    def test_no_data_at_all(self):
        empty = Cfg()
        r = empty.run()
        self.assertEqual(r.returncode, 0)
        self.assertTrue(r.stdout.startswith("Veri yok"), r.stdout)
        for flag in ("--gun", "--hafta", "--ay", "--yil"):
            self.assertIn("Veri yok.", empty.run(flag).stdout)
        self.assertEqual(empty.run("--pencere").returncode, 0)
        self.assertEqual(empty.json()["gun"]["satirlar"], [])

    def test_panel_export_and_static_dashboard(self):
        out = os.path.join(self.cfg.dir, "panel-out")
        r = self.cfg.run("--panel", out)
        self.assertEqual(r.returncode, 0, r.stderr)
        for n in ("donemler.json", "kirilimlar.json", "ozet.json", "index.html"):
            self.assertTrue(os.path.isfile(os.path.join(out, n)), n)
        html = rd(os.path.join(out, "index.html"))
        self.assertNotIn("__MALIYET_VERI__", html)
        self.assertIn("2026-W01", html)                              # data is embedded: opens with file://, no server
        self.assertNotRegex(html, r"(?i)(src|href)=[\"']https?://")  # nothing is loaded from the network

    def test_help_and_isolated_mode(self):
        r = self.cfg.run("--help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("--pencere", r.stdout)


class PersonalDataGuard(unittest.TestCase):
    def test_no_machine_specific_strings_in_shipped_files(self):
        plug = os.path.normpath(os.path.join(HERE, ".."))
        bad = ["/home/", "example-user", "example-host", "olcum/fiyatlar"]
        for sub in ("bin", "systemd"):
            for root, _, files in os.walk(os.path.join(plug, sub)):
                if "__pycache__" in root:
                    continue
                for n in files:
                    txt = rd(os.path.join(root, n)).lower()
                    for b in bad:
                        self.assertNotIn(b, txt, "%s contains %r" % (os.path.join(root, n), b))
        for n in ("gunluk.jsonl", "saatlik.jsonl", "durum.cache", "onbellek.json"):
            self.assertFalse(os.path.exists(os.path.join(plug, "bin", n)), n)
        self.assertFalse(os.path.exists(os.path.join(plug, "panel")))


class ProjectNames(unittest.TestCase):
    def test_home_prefix_is_derived_from_home(self):
        home_enc = "".join(c if c.isalnum() else "-" for c in _HOME)
        self.assertEqual(CK.kisa_proje(home_enc + "-work-repo"), "work-repo")
        self.assertEqual(CK.kisa_proje("-other-place"), "other-place")


class UyariTest(unittest.TestCase):
    """Under pythonw (Task Scheduler) sys.stderr is None: a warning must not end the run."""

    def test_warning_without_stderr_does_not_raise(self):
        from unittest import mock
        with mock.patch.object(sys, "stderr", None):
            CK._uyari("UYARI: deneme\n")

    def test_warning_reaches_stderr_and_the_run_log(self):
        import io
        import logging
        from unittest import mock
        lg = logging.getLogger("maliyet")
        kayit = []
        h = logging.Handler()
        h.emit = kayit.append
        lg.addHandler(h)
        self.addCleanup(lg.removeHandler, h)
        with mock.patch.object(sys, "stderr", io.StringIO()) as err:
            CK._uyari("UYARI: deneme\n")
        self.assertEqual(err.getvalue(), "UYARI: deneme\n")
        self.assertEqual([r.getMessage() for r in kayit], ["UYARI: deneme"])


if __name__ == "__main__":
    unittest.main()
