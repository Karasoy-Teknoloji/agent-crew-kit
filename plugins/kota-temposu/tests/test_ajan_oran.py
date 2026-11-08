"""Tests for the sub-agent row cost, the quota calibration ("gerçek $"), the lira rate and the status-line action cost.
Stdlib only, synthetic data only, nothing reads or writes the real ~/.claude.

Run:  python3 -I -m unittest discover -s plugins/kota-temposu/tests     (from the repo root)
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PLUG = os.path.normpath(os.path.join(HERE, ".."))
BIN = os.path.join(PLUG, "bin")
SUBAGENT_SCRIPT = os.path.normpath(os.path.join(PLUG, "..", "ekip-ajanlari", "tools", "subagent-statusline.py"))

_ROOT = tempfile.mkdtemp(prefix="ajan-oran-test-")
_HOME = os.path.join(_ROOT, "home")
_CFG = os.path.join(_HOME, ".claude")
os.makedirs(os.path.join(_CFG, "maliyet"))
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
os.environ["CLAUDE_CONFIG_DIR"] = _CFG
os.environ["MALIYET_TZ"] = "UTC"
os.environ.pop("MALIYET_FIYATLAR", None)
os.environ.pop("CLAUDE_SHARED_PLAN", None)
sys.path.insert(0, BIN)
import maliyet  # noqa: E402
import maliyet_artimli as AR  # noqa: E402
import maliyet_ayristir as AY  # noqa: E402
import maliyet_kur as KUR  # noqa: E402
import maliyet_oran as OR  # noqa: E402

FIYAT = AY.Fiyat({"yazim_carpani": {"5m": 1.25, "1h": 2.0}, "modeller": {
    "claude-test-1": {"in": 10.0, "out": 50.0, "read": 1.0},
    "claude-long-1": {"in": 1.0, "out": 5.0, "read": 0.1, "uzun_istem_esik": 100000, "uzun_istem_carpan": 5},
    "claude-adv-1": {"in": 20.0, "out": 100.0, "read": 2.0}}})


def tearDownModule():
    shutil.rmtree(_ROOT, ignore_errors=True)


def satir(mid, model="claude-test-1", inp=0, out=0, cr=0, cw5=0, cw1h=0):
    u = {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cr,
         "cache_creation_input_tokens": cw5 + cw1h, "cache_creation": {"ephemeral_5m_input_tokens": cw5, "ephemeral_1h_input_tokens": cw1h}}
    return json.dumps({"type": "assistant", "timestamp": "2026-10-29T10:00:00.000Z", "requestId": "req_" + mid, "uuid": "u" + mid + str(out),
                       "message": {"id": mid, "model": model, "usage": u}}) + "\n"


class Cost(unittest.TestCase):
    def test_one_response_all_token_types(self):
        r = {"model": "claude-test-1", "in": 1000, "out": 2000, "cr": 10000, "cw5": 4000, "cw1h": 8000, "adv": []}
        usd, adv, fz = AY.istek_maliyeti(FIYAT, r)
        # (1000*10 + 2000*50 + 10000*1 + 4000*1.25*10 + 8000*2*10) / 1e6
        self.assertAlmostEqual(usd, (10000 + 100000 + 10000 + 50000 + 160000) / 1e6)
        self.assertEqual((adv, fz), ([], 0))

    def test_long_prompt_tier_multiplies_the_whole_response(self):
        kisa = {"model": "claude-long-1", "in": 50000, "out": 1000, "cr": 0, "cw5": 0, "cw1h": 0, "adv": []}
        uzun = dict(kisa, **{"in": 100001})
        self.assertAlmostEqual(AY.istek_maliyeti(FIYAT, kisa)[0], (50000 * 1 + 1000 * 5) / 1e6)
        self.assertAlmostEqual(AY.istek_maliyeti(FIYAT, uzun)[0], (100001 * 1 + 1000 * 5) / 1e6 * 5)

    def test_unknown_model_is_unpriced_and_advisor_is_added(self):
        r = {"model": "claude-nope", "in": 10, "out": 20, "cr": 0, "cw5": 0, "cw1h": 0,
             "adv": [{"model": "claude-adv-1", "in": 1000, "out": 100, "cr": 500, "cw5": 0, "cw1h": 0}]}
        usd, adv, fz = AY.istek_maliyeti(FIYAT, r)
        self.assertIsNone(usd)
        self.assertEqual(fz, 30)
        self.assertEqual(adv[0][0], "claude-adv-1")
        self.assertAlmostEqual(adv[0][1], (1000 * 20 + 100 * 100 + 500 * 2) / 1e6)

    def test_packaged_table_covers_current_models(self):
        f = AY.Fiyat(AY.fiyat_yukle())
        for m in ("claude-opus-5-5-x", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1", "claude-opus-4-8", "claude-haiku-4-5-20251001"):
            self.assertIsNotNone(f.bul(m), m)
        self.assertEqual(f.bul("claude-fable-5-1")["read"], 0.25)
        self.assertEqual(f.bul("claude-haiku-5-5")["uzun_istem_carpan"], 5)


class Incremental(unittest.TestCase):
    def setUp(self):
        self.yol = os.path.join(_ROOT, "agent-x.jsonl")
        if os.path.exists(self.yol):
            os.remove(self.yol)

    def tam(self):
        t = 0.0
        for r in AY.ayristir(self.yol)["reqs"]:
            usd, adv, _ = AY.istek_maliyeti(FIYAT, r)
            t += (usd or 0.0) + sum(x for _m, x in adv)
        return t

    def test_resume_equals_full_parse_with_streamed_duplicates_and_half_lines(self):
        AR._fiyat = FIYAT
        a = satir("m1", out=100, inp=1000) + satir("m1", out=300, inp=1000)           # one response streamed in two lines
        b = satir("m2", out=50, cr=2000, cw1h=500)
        c = satir("m2", out=70, cr=2000, cw1h=500) + satir("m3", out=10)
        with open(self.yol, "w") as f:
            f.write(a)
        v1 = AR.toplam(self.yol, "t1")
        self.assertAlmostEqual(v1, 1000 * 10 / 1e6 + 300 * 50 / 1e6)                   # final usage of m1, not the sum
        with open(self.yol, "a") as f:
            f.write(b[:40])                                                            # half-written last line: not read yet
        self.assertAlmostEqual(AR.toplam(self.yol, "t1"), v1)
        with open(self.yol, "a") as f:
            f.write(b[40:] + c)
        self.assertAlmostEqual(AR.toplam(self.yol, "t1"), self.tam())
        off = json.load(open(AR._durum_yolu("t1")))["off"]
        self.assertEqual(off, os.path.getsize(self.yol))
        self.assertAlmostEqual(AR.toplam(self.yol, "t1"), self.tam())                  # no growth: same result, offset unchanged
        self.assertEqual(json.load(open(AR._durum_yolu("t1")))["off"], off)

    def test_truncated_file_starts_over_and_missing_file_is_none(self):
        AR._fiyat = FIYAT
        with open(self.yol, "w") as f:
            f.write(satir("a", out=1000) + satir("b", out=1000))
        big = AR.toplam(self.yol, "t2")
        with open(self.yol, "w") as f:
            f.write(satir("c", out=10))
        self.assertLess(AR.toplam(self.yol, "t2"), big)
        self.assertIsNone(AR.toplam(os.path.join(_ROOT, "nope.jsonl"), "t3"))

    def test_agent_file_lookup(self):
        main = os.path.join(_ROOT, "proj", "sess.jsonl")
        os.makedirs(os.path.join(_ROOT, "proj", "sess", "subagents", "workflows", "wf1"), exist_ok=True)
        direct = os.path.join(_ROOT, "proj", "sess", "subagents", "agent-aa1.jsonl")
        wf = os.path.join(_ROOT, "proj", "sess", "subagents", "workflows", "wf1", "agent-bb2.jsonl")
        for p in (direct, wf):
            open(p, "w").close()
        self.assertEqual(AR.ajan_dosyasi(main, "aa1"), direct)
        self.assertEqual(AR.ajan_dosyasi(main, "bb2"), wf)
        self.assertIsNone(AR.ajan_dosyasi(main, "zz9"))
        self.assertIsNone(AR.ajan_dosyasi(main, "../x"))
        self.assertIsNone(AR.ajan_dosyasi(None, "aa1"))


class Calibration(unittest.TestCase):
    NOW = 1_800_000_000.0

    def veri(self, orani=10.0, adim=1, n=12, web_penceresi=()):
        """History: +1 % every hour inside one week; local spend per hour = orani (in the minute buckets), except web windows."""
        reset = int(self.NOW + 86400)
        rows, dk = [(self.NOW - 100 * 3600, 10, reset)], {}
        for i in range(1, n * 4 + 1):
            ts = self.NOW - 100 * 3600 + i * 3600
            rows.append((ts, 10 + i, reset))
            dk[int(ts // 60) - 5] = 0.0 if i in web_penceresi else orani
        return rows, dk

    def test_ratio_is_dollars_per_percentage_point(self):
        rows, dk = self.veri()
        r = OR.oran_hesapla(rows, dk, self.NOW)
        self.assertAlmostEqual(r["usd_per_pct"], 10.0)
        self.assertEqual((r["n"], r["atilan"], r["adim"]), (11, 0, 4))

    def test_percent_with_no_local_spend_is_dropped_as_outlier(self):
        rows, dk = self.veri(web_penceresi=set(range(6, 10)))                         # window 2 (hours 6..9): percent rose, no local spend
        r = OR.oran_hesapla(rows, dk, self.NOW)
        self.assertAlmostEqual(r["usd_per_pct"], 10.0)
        self.assertEqual(r["atilan"], 1)

    def test_not_enough_windows_or_old_windows_give_none(self):
        rows, dk = self.veri(n=2)
        self.assertIsNone(OR.oran_hesapla(rows, dk, self.NOW))
        rows, dk = self.veri()
        self.assertIsNone(OR.oran_hesapla(rows, dk, self.NOW + 30 * 86400))         # everything older than GUN days
        self.assertIsNone(OR.oran_hesapla([], {}, self.NOW))

    def test_stale_lower_value_and_placeholder_reset_are_ignored(self):
        reset = int(self.NOW + 86400)
        rows = [(1, 10, reset), (2, 14, reset), (3, 12, reset), (4, 15, reset), (5, 50, OR.SAHTE_RESET)]
        pts = OR.noktalar(rows)
        self.assertEqual(pts, {reset: [(2, 14), (4, 15)]})                          # 12 (stale session) is no rise; first row is no rise

    def test_gercek_factor_from_plan(self):
        self.assertAlmostEqual(OR.hafta_usd(200), 200 * 12 / 52)
        # 100 % of the week = 10 $/% x 100 = $1000 API; W = 46.15 -> an agent that cost $4 API cost $0.1846 for real
        self.assertAlmostEqual(4 * OR.hafta_usd(200) / (10.0 * 100), 0.18461538, places=6)

    def test_katsayi_oku_missing_stale_and_fresh(self):
        if os.path.exists(OR.ORAN):
            os.remove(OR.ORAN)
        self.assertIsNone(OR.katsayi_oku(self.NOW))
        with open(OR.ORAN, "w") as f:
            json.dump({"t": self.NOW, "katsayi": 0.02}, f)
        self.assertEqual(OR.katsayi_oku(self.NOW + 60), 0.02)
        self.assertIsNone(OR.katsayi_oku(self.NOW + 4 * 86400))
        os.remove(OR.ORAN)


_KEYCHAIN_GERCEK = OR._keychain_tier


class Plan(unittest.TestCase):
    def setUp(self):
        for ad in ("pace-plan.json", ".credentials.json"):
            p = os.path.join(_CFG, ad)
            if os.path.exists(p):
                os.remove(p)
        os.environ.pop("CLAUDE_SHARED_PLAN", None)
        OR._keychain_tier = lambda cfg=None: ""   # never touch the real Keychain in tests

    def test_claude_json_account_tier(self):
        # macOS: no .credentials.json; the tier is in oauthAccount of .claude.json (synthetic HOME)
        home = tempfile.mkdtemp()
        yol = os.path.join(home, ".claude.json")
        with open(yol, "w") as f:
            json.dump({"oauthAccount": {"userRateLimitTier": "default_claude_max_5x", "organizationRateLimitTier": "default_raven"}}, f)
        eski = os.path.expanduser
        os.path.expanduser = lambda p: home if p == "~" else eski(p)
        try:
            self.assertEqual(OR.plan_kimlik(), 100)
            with open(yol, "w") as f:
                json.dump({"oauthAccount": {"userRateLimitTier": "default_raven"}}, f)
            self.assertIsNone(OR.plan_kimlik())
        finally:
            os.path.expanduser = eski
            shutil.rmtree(home)

    def test_keychain_cache_is_tier_only(self):
        c = os.path.join(_CFG, "plan-tier.cache")
        with open(c, "w") as f:
            f.write("ok\ndefault_claude_max_20x team")
        try:
            self.assertEqual(_KEYCHAIN_GERCEK(_CFG), "default_claude_max_20x team" if sys.platform == "darwin" else "")
        finally:
            os.remove(c)

    def kimlik(self, tip, tier):
        with open(os.path.join(_CFG, ".credentials.json"), "w") as f:
            json.dump({"claudeAiOauth": {"subscriptionType": tip, "rateLimitTier": tier, "accessToken": "SECRET"}}, f)

    def test_tier_mapping(self):
        for tip, tier, beklenen in (("max", "default_claude_max_20x", 200.0), ("max", "default_claude_max_5x", 100.0),
                                    ("pro", "default_claude_pro", 20.0), ("team", "default_raven", None)):
            self.kimlik(tip, tier)
            self.assertEqual(OR.plan_aylik(), beklenen, tier)

    def test_override_beats_account_tier_and_missing_file_is_none(self):
        self.assertIsNone(OR.plan_kimlik())
        self.kimlik("max", "default_claude_max_20x")
        with open(os.path.join(_CFG, "pace-plan.json"), "w") as f:
            json.dump({"target": 90, "plan_usd_monthly": 100}, f)
        self.assertEqual(OR.plan_aylik(), 100.0)

    def test_env_plan_beats_account_tier(self):
        self.kimlik("max", "default_claude_max_20x")
        os.environ["CLAUDE_SHARED_PLAN"] = "100"
        self.assertEqual(OR.plan_aylik(), 100.0)


TCMB = ('<Tarih_Date Tarih="29.10.2026" Date="10/29/2026"><Currency CrossOrder="0" Kod="USD" CurrencyCode="USD">'
        '<Isim>ABD DOLARI</Isim><ForexBuying>49.7000</ForexBuying><ForexSelling>49.7892</ForexSelling></Currency>'
        '<Currency Kod="EUR" CurrencyCode="EUR"><ForexSelling>57.1</ForexSelling></Currency></Tarih_Date>')


class Lira(unittest.TestCase):
    def setUp(self):
        if os.path.exists(KUR.DOSYA):
            os.remove(KUR.DOSYA)

    def test_xml_parse_and_sanity_range(self):
        self.assertEqual(KUR.ayristir(TCMB), (49.7892, "2026-10-29"))
        self.assertIsNone(KUR.ayristir(TCMB.replace("49.7892", "0.5")))
        self.assertIsNone(KUR.ayristir("<html>not xml</html>"))

    def test_latest_publication_is_1530_istanbul(self):
        # 2026-10-29 12:00 UTC = 15:00 Istanbul -> latest publication was yesterday 15:30 Istanbul = 12:30 UTC
        t = 1793275200.0
        self.assertEqual(KUR.son_yayin(t), t - 86400 + 30 * 60)
        self.assertEqual(KUR.son_yayin(t + 3600), t + 30 * 60)     # 16:00 Istanbul -> today's 15:30

    def test_fetch_caches_failure_keeps_rate_and_is_throttled(self):
        simdi = 1793283600.0
        d = KUR.yenile(simdi, lambda: TCMB)
        self.assertEqual(d["kur"], 49.7892)
        calls = []
        KUR.yenile(simdi + 10, lambda: calls.append(1) or TCMB)                    # fresh enough: no fetch
        self.assertEqual(calls, [])
        def fail():
            calls.append(1)
            raise OSError("offline")
        d = KUR.yenile(simdi + 86400 + 4 * 3600, fail)                              # a new publication is due, fetch fails
        self.assertEqual((calls, d["kur"]), ([1], 49.7892))
        KUR.yenile(simdi + 86400 + 4 * 3600 + 60, fail)                             # failed attempt counts: not retried within the hour
        self.assertEqual(calls, [1])

    def test_format(self):
        self.assertEqual(maliyet.para(4.46, 49.79), "₺222")
        self.assertEqual(maliyet.para(0.0464, 49.79), "₺2,31")
        self.assertEqual(maliyet.para(380.62, 49.79), "₺18.951")
        self.assertEqual(maliyet.para(0.0, 49.79), "₺0,00")
        self.assertEqual(maliyet.para(2.5, None) if not os.path.exists(maliyet.KUR) else "$2.50", "$2.50")   # no rate yet: dollars
        self.assertEqual(maliyet.para(205.4, None) if not os.path.exists(maliyet.KUR) else "$205", "$205")


class ActionCost(unittest.TestCase):
    """maliyet.tur_tutari / girdi_tutarlar: pure Python, runs on every platform."""

    def test_baseline_carry_forward_reset_and_never_negative(self):
        d = os.path.join(_ROOT, "tur-test")
        self.assertEqual(maliyet.tur_tutari("s1", "p1", 10.0, d), 0.0)               # first sighting: baseline = now
        self.assertAlmostEqual(maliyet.tur_tutari("s1", "p1", 10.5, d), 0.5)
        self.assertAlmostEqual(maliyet.tur_tutari("s1", "p2", 12.5, d), 2.0)         # new prompt: baseline = last total of p1
        self.assertAlmostEqual(maliyet.tur_tutari("s1", "p2", 12.6, d), 2.1)
        self.assertAlmostEqual(maliyet.tur_tutari("s1", "p3", 1.0, d), 1.0)          # total dropped (/clear): restart at 0
        self.assertGreaterEqual(maliyet.tur_tutari("s1", "p3", 0.5, d), 0.0)
        self.assertEqual(maliyet.tur_tutari("../evil", "p1", 3.0, d), 0.0)           # session id cannot escape the folder
        self.assertFalse(os.path.exists(os.path.join(_ROOT, "evil")))

    def test_status_line_json(self):
        self.assertEqual(maliyet.girdi_tutarlar("not json"), ["-", "-"])
        self.assertEqual(maliyet.girdi_tutarlar('{"cost": {}}'), ["-", "-"])
        self.assertEqual(maliyet.girdi_tutarlar('{"cost": {"total_cost_usd": 2.5}}'), ["2.500000", "-"])   # no prompt_id yet
        self.assertEqual(maliyet.girdi_tutarlar('{"session_id": "s9", "prompt_id": "p", "cost": {"total_cost_usd": true}}'), ["-", "-"])


@unittest.skipUnless(shutil.which("jq") and shutil.which("bash") and os.name == "posix", "needs bash and jq")
class StatusLineAction(unittest.TestCase):
    """statusline.sh: the cost of the current/last action = cost.total_cost_usd minus its value when the prompt_id began."""

    @classmethod
    def setUpClass(cls):
        cls.home = os.path.join(_ROOT, "slhome")
        os.makedirs(os.path.join(cls.home, ".claude", "bin"))
        os.makedirs(os.path.join(cls.home, ".claude", "maliyet"))
        for f in os.listdir(BIN):
            if f.startswith("maliyet"):
                shutil.copy(os.path.join(BIN, f), os.path.join(cls.home, ".claude", "bin", f))
        for f in ("gizli.py", "gizli_yukle.py", "gunluk.py"):      # a copy install ships these next to the scripts (kur_ortak FILES)
            shutil.copy(os.path.join(BIN, "..", "..", "..", "tools", f), os.path.join(cls.home, ".claude", "bin", f))
        with open(os.path.join(cls.home, ".claude", "maliyet", "kur.json"), "w") as f:
            json.dump({"kur": 50.0}, f)
        with open(os.path.join(cls.home, ".claude", "settings.json"), "w") as f:
            f.write("{}")

    def run_line(self, pid, total, sid="sess-1"):
        d = {"session_id": sid, "prompt_id": pid, "model": {"id": "claude-opus-5-5", "display_name": "Opus 5.5"},
             "workspace": {"current_dir": self.home}, "cost": {"total_cost_usd": total}}
        env = dict(os.environ, HOME=self.home)
        env.pop("CLAUDE_CONFIG_DIR", None)
        p = subprocess.run(["bash", os.path.join(PLUG, "statusline.sh")], input=json.dumps(d), capture_output=True, text=True, env=env, timeout=60)
        return p.stdout

    def test_delta_per_prompt_reset_and_session_total(self):
        self.assertRegex(self.run_line("p1", 10.0), r"\d\d:\d\d ₺0,00(?! \()")                 # no state yet: baseline = now
        self.assertRegex(self.run_line("p1", 10.5), r"\d\d:\d\d ₺25,00")                # same prompt: +0.5 USD x 50
        out = self.run_line("p2", 12.5)                                               # new prompt: baseline = last total of p1 (10.5)
        self.assertRegex(out, r"\d\d:\d\d ₺100\b")
        self.assertIn("oturum ₺625", out)
        self.assertNotRegex(out, r"oturum ₺625 \(")                                    # no calibration: no parentheses
        self.assertRegex(self.run_line("p3", 1.0), r"\d\d:\d\d ₺50,00")               # total dropped (/clear): restart at 0, never negative

    def test_without_prompt_id_no_action_cost(self):
        out = self.run_line(None, 3.0, sid="sess-2")
        self.assertNotRegex(out, r"\d\d:\d\d ₺")

    def test_real_cost_in_parentheses_only_with_a_calibration(self):
        oran = os.path.join(self.home, ".claude", "maliyet", "oran.json")
        self.assertNotRegex(self.run_line("g1", 20.0, sid="sess-3"), r"\d\d:\d\d ₺\S+ \(")         # no calibration: the API amount only
        with open(oran, "w") as f:
            json.dump({"t": time.time(), "katsayi": 0.5}, f)                                # gerçek = API x 0.5
        try:
            self.run_line("g1", 20.0, sid="sess-3")
            out = self.run_line("g1", 22.0, sid="sess-3")                                   # +2 USD x 50 = ₺100 API, ₺50,00 gerçek
            self.assertRegex(out, r"\d\d:\d\d ₺100 \(₺50,00\)")
            self.assertIn("oturum ₺1.100 (₺550)", out)                                      # the session total gets the same ratio
        finally:
            os.remove(oran)

    def test_weekly_window_in_lira_and_dollars_only_for_7g(self):
        mal = os.path.join(self.home, ".claude", "maliyet")
        now = int(time.time())
        saat = {str(now // 3600 - 2): 100.0}          # $100 two hours ago: inside 24h, 7g and 30g
        with open(os.path.join(mal, "durum.cache"), "w") as f:
            json.dump({"t": time.time(), "gunler": {}, "e": None, "saat": saat}, f)
        oran = os.path.join(mal, "oran.json")

        def durum():
            r = subprocess.run([sys.executable, "-I", os.path.join(self.home, ".claude", "bin", "maliyet.py"), "--durum"], capture_output=True,
                               env={k: v for k, v in dict(os.environ, HOME=self.home).items() if k != "CLAUDE_CONFIG_DIR"}, timeout=60)
            return r.stdout.decode("utf-8")
        self.assertEqual(durum(), "son 24sa ₺5.000 · 7g ₺5.000 $100 · 30g ₺5.000 · API eşdeğeri (fatura değil)")      # no calibration (no ×N): no parentheses, the amounts are labelled API equivalents
        with open(oran, "w") as f:
            json.dump({"t": time.time(), "katsayi": 0.1}, f)                          # gerçek = API x 0.1
        try:
            self.assertEqual(durum(), "son 24sa ₺5.000 (₺500) · 7g ₺5.000 $100 (₺500) · 30g ₺5.000 (₺500) · ×10")
        finally:
            os.remove(oran)


@unittest.skipUnless(os.name == "posix", "posix paths")
class SubagentRow(unittest.TestCase):
    def rows(self, tasks, transcript):
        env = dict(os.environ, HOME=_HOME, CLAUDE_CONFIG_DIR=_CFG)
        p = subprocess.run([sys.executable, "-I", SUBAGENT_SCRIPT], input=json.dumps({"transcript_path": transcript, "tasks": tasks}),
                           capture_output=True, text=True, env=env, timeout=60)
        return {json.loads(l)["id"]: json.loads(l)["content"] for l in p.stdout.splitlines()}

    def test_row_shows_api_and_gercek_or_only_api(self):
        main = os.path.join(_ROOT, "p2", "s.jsonl")
        sd = os.path.join(_ROOT, "p2", "s", "subagents")
        os.makedirs(sd, exist_ok=True)
        with open(os.path.join(sd, "agent-ab12.jsonl"), "w") as f:
            f.write(satir("z1", model="claude-opus-5-5", inp=1000000))                  # 1M input tokens of Opus 5.5 = $4.00
        task = {"id": "ab12", "agentType": "x", "description": "d", "tokenCount": 223000, "startTime": time.time() * 1000 - 65000}
        with open(os.path.join(_CFG, "maliyet", "kur.json"), "w") as f:
            json.dump({"kur": 50.0}, f)
        if os.path.exists(OR.ORAN):
            os.remove(OR.ORAN)
        self.assertRegex(self.rows([task], main)["ab12"], r"223k · ₺200( ·|$)")                     # $4 x 50
        self.assertNotIn("(", self.rows([task], main)["ab12"].split(" · sessiz")[0].replace("\x1b[2m", "").replace("\x1b[0m", ""))   # no calibration yet: no parentheses
        with open(OR.ORAN, "w") as f:
            json.dump({"t": time.time(), "katsayi": 0.05}, f)
        row = self.rows([task], main)["ab12"]
        self.assertIn("₺200 (₺10,00) · ", row)
        self.assertIn("223k", row)
        os.remove(OR.ORAN)

    def test_output_survives_a_legacy_console_code_page(self):
        # Windows pipes use cp1252/cp1254: "·" and "₺" must still arrive as valid UTF-8 JSON (the row used to vanish silently)
        task = {"id": "zz", "agentType": "x", "description": "Türkçe açıklama", "tokenCount": 5}
        env = dict(os.environ, HOME=_HOME, CLAUDE_CONFIG_DIR=_CFG, PYTHONIOENCODING="cp1252", PYTHONUTF8="0")
        p = subprocess.run([sys.executable, "-I", SUBAGENT_SCRIPT], input=json.dumps({"tasks": [task]}, ensure_ascii=False).encode("utf-8"),
                           capture_output=True, env=env, timeout=60)
        row = json.loads(p.stdout.decode("ascii").splitlines()[0])["content"]
        self.assertIn("Türkçe açıklama · x", row.replace("\x1b[2m", "").replace("\x1b[0m", ""))

    def test_unknown_agent_or_garbage_keeps_the_plain_row(self):
        row = self.rows([{"id": "nope", "agentType": "x", "description": "d", "tokenCount": 5}], os.path.join(_ROOT, "p2", "s.jsonl"))["nope"]
        self.assertNotIn("₺", row)
        self.assertEqual(self.rows([{"id": "nope"}], "garbage").get("nope"), "subagent")


if __name__ == "__main__":
    unittest.main()
