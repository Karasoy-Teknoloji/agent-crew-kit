"""Tests for maliyet_paylas (daily row in raporlar/kullanim/<kisi>.md + regenerated SIRALAMA.md). Stdlib only, synthetic data only.

Run:  python3 -I -m unittest discover -s plugins/kota-temposu/tests     (from the repo root)

No network: the remote is a local bare repository (CLAUDE_SHARED_UZAK). The cost windows come from a synthetic transcript
run through the real CLI (`--paylas --kuru` prints the row and writes nothing).
"""
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_maliyet as TM  # noqa: E402  (sets the scratch HOME / CLAUDE_CONFIG_DIR before the modules are imported; helpers only)
import maliyet  # noqa: E402
import maliyet_paylas as P  # noqa: E402


def setUpModule():
    # test_maliyet's tearDownModule removes the shared scratch tree when its tests are done: make it again for this module
    os.makedirs(TM._CFG, exist_ok=True)


def tearDownModule():
    shutil.rmtree(TM._ROOT, ignore_errors=True)


def gun(n=0):
    return (datetime.date.today() - datetime.timedelta(days=n)).isoformat()


def kayit(tarih, kota=None, plan=200, hafta="2026-11-01", hafta_usd=None, d7=100, commit=None, opus=1.0, sonnet=2.0, kisi="Ali", ekle=None, sil=None):
    return {"kisi": kisi, "tarih": tarih, "d24": 10, "d7": d7, "d30": 300, "kota": kota, "plan": plan, "hafta": hafta,
            "hafta_usd": hafta_usd, "usd": {"opus": opus, "sonnet": sonnet, "haiku": 0.0, "fable": 0.0, "diger": 0.0},
            "tok": {"opus": 5.0, "sonnet": 20.0, "haiku": 0.0, "fable": 0.0, "diger": 0.0}, "commit": commit, "ekle": ekle, "sil": sil}


class RowFormat(unittest.TestCase):
    def test_row_roundtrip_and_unknowns(self):
        r = kayit("2026-10-28", kota=43, hafta_usd=210, commit=3)
        p = P.satir_ayristir(P.satir_kur(r))
        self.assertEqual({k: p[k] for k in ("tarih", "d24", "d7", "d30", "kota", "plan", "hafta", "hafta_usd", "commit")},
                         {k: r[k] for k in ("tarih", "d24", "d7", "d30", "kota", "plan", "hafta", "hafta_usd", "commit")})
        self.assertEqual(p["usd"]["opus"], 1.0)
        self.assertEqual(p["tok"]["sonnet"], 20.0)
        e = P.satir_ayristir(P.satir_kur(kayit("2026-10-28", plan=None, hafta=None)))
        self.assertEqual((e["kota"], e["plan"], e["hafta"], e["hafta_usd"], e["commit"]), (None, None, None, None, None))

    def test_noise_is_not_a_row(self):
        for s in ("", "| Tarih | 24sa $ |", "|---|---|", "| 2026-13-45 | 1 | 2 |", "text", None, "| 2026-10-28 | x | 1 | 1 |" + " 1 |" * 15):
            self.assertIsNone(P.satir_ayristir(s), s)

    def test_file_append_is_once_per_date_and_keeps_the_header(self):
        m = P.dosyaya_ekle("", "Ali", P.satir_kur(kayit("2026-10-27")), "2026-10-27")
        self.assertIn("# Claude kullanımı — Ali", m)
        m2 = P.dosyaya_ekle(m, "Ali", P.satir_kur(kayit("2026-10-28")), "2026-10-28")
        self.assertEqual([r["tarih"] for r in P.dosya_satirlari(m2)], ["2026-10-27", "2026-10-28"])
        self.assertEqual(P.dosyaya_ekle(m2, "Ali", P.satir_kur(kayit("2026-10-28", d7=999)), "2026-10-28"), m2)

    def test_names_and_file_stems(self):
        self.assertEqual(P.dosya_adi("Çağrı Şen"), "cagri-sen")
        self.assertEqual(P.dosya_adi("../x"), "x")
        self.assertEqual(P.model_ailesi("claude-opus-5-5"), "opus")
        self.assertEqual(P.model_ailesi("claude-fable-5-1"), "fable")
        self.assertEqual(P.model_ailesi("gpt"), "diger")

    def test_model_split_sums_all_token_kinds(self):
        u, t = P.model_dagilim({"model": {"claude-opus-5-5": {"usd": 3.0, "in": 1_000_000, "out": 1_000_000, "cr": 2_000_000, "cw5": 0, "cw1h": 0},
                                          "claude-fable-5-1": {"usd": 1.5}}})
        self.assertEqual((u["opus"], t["opus"], u["fable"], t["fable"]), (3.0, 4.0, 1.5, 0.0))


class Ranking(unittest.TestCase):
    def test_line_columns_roundtrip_and_old_rows_show_dash(self):
        p = P.satir_ayristir(P.satir_kur(kayit("2026-10-28", commit=2, ekle=1234, sil=56)))
        self.assertEqual((p["commit"], p["ekle"], p["sil"]), (2, 1234, 56))
        eski = P.satir_kur(kayit("2026-10-27", commit=3)).rsplit(" | — | — | — | — | — | — | — | — | — |", 1)[0] + " |"      # a 19-column row from before the change
        self.assertEqual(len(eski.strip("|").split("|")), len(P.SUTUNLAR))
        e = P.satir_ayristir(eski)
        self.assertEqual((e["commit"], e["ekle"], e["sil"]), (3, None, None))
        self.assertEqual(P.satir_metni(e["ekle"], e["sil"]), "—")
        m = P.dosyaya_ekle("# x\n\n| %s |\n|%s\n%s\n" % (" | ".join(P.SUTUNLAR), "---|" * len(P.SUTUNLAR), eski), "Ali",
                           P.satir_kur(kayit("2026-10-28", ekle=1, sil=2)), "2026-10-28")
        self.assertEqual([(r["ekle"], r["sil"]) for r in P.dosya_satirlari(m)], [(None, None), (1, 2)])
        self.assertIn("Satır +", m)

    def test_weekly_quota_columns_present_missing_and_old_rows(self):
        r = dict(kayit("2026-10-28", kota=78), tahmin=107, sifirla="2026-11-01 12:00", mod="SIKI")
        p = P.satir_ayristir(P.satir_kur(r))
        self.assertEqual((p["kota"], p["tahmin"], p["sifirla"], p["mod"]), (78, 107, "2026-11-01 12:00", "SIKI"))
        e = P.satir_ayristir(P.satir_kur(kayit("2026-10-28")))      # missing data: empty, not an error
        self.assertEqual((e["tahmin"], e["sifirla"], e["mod"]), (None, None, None))
        for n in (len(P.SUTUNLAR), len(P.ARA_SUTUN)):               # old 19- and 21-column rows still parse
            eski = "|".join(P.satir_kur(kayit("2026-10-27", commit=3, ekle=5, sil=6)).split("|")[:n + 1]) + " |"
            o = P.satir_ayristir(eski)
            self.assertEqual((o["commit"], o["tahmin"], o["sifirla"], o["mod"]), (3, None, None, None))
            m = P.dosyaya_ekle("# x\n\n| %s |\n|%s\n%s\n" % (" | ".join(P.TUM_SUTUN[:n]), "---|" * n, eski), "Ali", P.satir_kur(r), "2026-10-28")
            self.assertIn("| Tahmin % | Sıfırlanma | Mod |", m)
            self.assertEqual([x["mod"] for x in P.dosya_satirlari(m)], [None, "SIKI"])
        bos = P.dosyaya_ekle("", "Ali", P.satir_kur(kayit("2026-10-28", kota=78)), "2026-10-28")
        dolu = P.dosyaya_ekle(bos, "Ali", P.satir_kur(r), "2026-10-28")          # same-day row without the fields is filled in once
        self.assertEqual([(x["tahmin"], x["mod"]) for x in P.dosya_satirlari(dolu)], [(107, "SIKI")])
        self.assertEqual(P.dosyaya_ekle(dolu, "Ali", P.satir_kur(kayit("2026-10-28")), "2026-10-28"), dolu)   # filled rows are never touched
        veri = {"Ali": [r], "Veli": [kayit("2026-10-28", d7=50, kota=10)]}
        t = P.siralama_metni(veri, "2026-10-28")
        self.assertIn("| # | Kişi | Haftalık % | Tahmin | Tahmini bitiş | Sıfırlanma | Mod | Slack gelen | Son güncelleme |", t)
        self.assertIn("| # | Kişi | Plan | 7 gün | Geçen haftaya göre | Commit (7 gün) | Satır (7 gün) | $ / 1000 satır |", t)
        self.assertIn("| %78 | %107 | — | 01.11 12:00 | SIKI |", t)
        self.assertIn("| %10 | — | — | — | — |", t)
        r2 = dict(r, bitis="2026-10-31 21:00", tahmin_zamani="2026-10-31 10:00")
        self.assertIn("| %107 | 31.10 21:00 | 01.11 12:00 |", P.siralama_metni({"Ali": [r2]}, "2026-10-28"))
        self.assertIn("| %107 | sıfırlanmaya yetiyor |", P.siralama_metni({"Ali": [dict(r, bitis="yeter")]}, "2026-10-28"))
        q = P.satir_ayristir(P.satir_kur(r2))
        self.assertEqual((q["bitis"], q["tahmin_zamani"]), ("2026-10-31 21:00", "2026-10-31 10:00"))
        self.assertEqual(P.satir_ayristir(P.satir_kur(dict(r, bitis="yeter")))["bitis"], "yeter")
        eski24 = "|".join(P.satir_kur(r).split("|")[:len(P.ARA2_SUTUN) + 1]) + " |"   # 24-column row from before the run-out columns
        self.assertEqual((P.satir_ayristir(eski24)["mod"], P.satir_ayristir(eski24)["bitis"]), ("SIKI", None))
        m = P.dosyaya_ekle("# x\n\n| %s |\n|%s\n%s\n" % (" | ".join(P.ARA2_SUTUN), "---|" * len(P.ARA2_SUTUN), eski24), "Ali", P.satir_kur(r2), "2026-10-28")
        self.assertEqual([x["bitis"] for x in P.dosya_satirlari(m)], ["2026-10-31 21:00"])   # same-day row gets the run-out filled in
        self.assertIn("| Bitiş | Tahmin zamanı |", m)

    def test_ranking_uses_github_commits_and_shows_last_update(self):
        veri = {"Ali": [kayit("2026-10-28", d7=200, commit=None)], "Veli": [kayit("2026-10-28", d7=100, commit=5)]}
        t0 = datetime.datetime(2026, 10, 28, 10, 5, tzinfo=datetime.timezone.utc).timestamp()
        m = P.siralama_metni(veri, "2026-10-28", {"Ali": 42}, {"Ali": t0})
        self.assertIn("| Mod | Slack gelen | Son güncelleme |", m)
        self.assertRegex(m, r"\| Ali \|.*\| 28\.10 13:05 \|")      # Istanbul time = UTC+3
        self.assertRegex(m, r"\| Ali \|[^\n]*\| 42 \| ")             # GitHub commit figure in the cost table
        self.assertRegex(m, r"\| Veli \|.*\| 2026-10-28 \|")           # no GitHub figure: row date
        self.assertRegex(m, r"\| Veli \|[^\n]*\| 5 \| ")                # row sum
        self.assertEqual(P.github_commit_say.__name__, "github_commit_say")

    def test_github_commit_count_leaves_out_chore_kullanim_commits(self):
        # the leaderboard's own automatic commits (chore(kullanim), and chore(son-pencere) from the same plugin) must not inflate the column
        konular = ["chore(kullanim): Ali 2026-11-04 · kota %47→%50", "feat(x): real work", "chore(kullanim): Ali 2026-11-03",
                   "chore(son-pencere): Ali 2026-11-02 10:00", "fix(y): more work", "chore(deps): not one of ours"]
        cagri = subprocess.CompletedProcess([], 0, stdout="\n".join(konular) + "\n", stderr="")
        with tempfile.TemporaryDirectory() as d, mock.patch.object(P, "_onbellek_yolu", return_value=os.path.join(d, "onbellek.json")), \
                mock.patch.object(P.subprocess, "run", return_value=cagri) as kos, mock.patch.dict(os.environ, {"GH_ORG": "example-org"}):
            n = P.github_commit_say("ali", "2026-10-29", simdi=1000.0)
        self.assertEqual(n, 3)                                   # feat, fix, chore(deps): the two automation subjects are not counted
        self.assertIn("--paginate", kos.call_args[0][0])

    def test_ranking_shows_7_day_line_sum_in_one_column_after_commits(self):
        veri = {"Ali": [kayit("2026-10-27", d7=150, commit=2, ekle=10000, sil=3000), kayit("2026-10-28", d7=200, commit=3, ekle=2300, sil=1100)],
                "Veli": [kayit("2026-10-28", d7=300, plan=100, commit=1)],
                "Cem": [kayit("2026-10-28", d7=50, commit=1, ekle=950, sil=0)]}
        s = {r["kisi"]: r for r in P.siralama_hesapla(veri, "2026-10-28")}
        self.assertEqual((s["Ali"]["ekle"], s["Ali"]["sil"], s["Veli"]["ekle"]), (12300, 4100, None))
        m = P.siralama_metni(veri, "2026-10-28")
        self.assertIn("| Commit (7 gün) | Satır (7 gün) |", m)
        self.assertIn("| 5 | +12,3K / −4,1K | $12,20 |", m)      # 200 / (16400 / 1000)
        self.assertIn("| 1 | — | — |", m)
        self.assertIn("| 1 | +950 / −0 | $52,63 |", m)         # 50 / 0.95
        self.assertIn("$ / 1000 satır", m)
        self.assertEqual(P.dolar_bin_satir(100, 0, 0), "—")
        self.assertLess(m.index("| Veli |"), m.index("| Ali |"))      # order is still by 7-day spend

    def test_rank_trend_commits_and_stale_marker(self):
        veri = {"Ali": [kayit("2026-10-21", d7=100), kayit("2026-10-27", d7=150, commit=2), kayit("2026-10-28", d7=200, commit=3, kota=40)],
                "Veli": [kayit("2026-10-28", d7=300, plan=100)],
                "Eski": [kayit("2026-10-10", d7=999)]}
        s = P.siralama_hesapla(veri, "2026-10-28")
        self.assertEqual([r["kisi"] for r in s], ["Veli", "Ali"])          # Eski has no row in the last 7 days
        ali = s[1]
        self.assertEqual((ali["d7"], ali["onceki_d7"], ali["commit"], ali["kota"]), (200, 100, 5, 40))
        self.assertEqual(P.trend_metni(200, 100), "▲ +100%")
        self.assertEqual(P.trend_metni(100, None), "yeni")
        self.assertEqual(P.trend_metni(101, 100), "≈ aynı")

    def test_empty_board_text(self):
        self.assertIn("satır yok", P.siralama_metni({}, "2026-10-28"))


class PlanComparison(unittest.TestCase):
    def veri(self):
        # $200 person: 300 $ for 30 % -> 10 $/%; $100 person: 100 $ for 20 % -> 5 $/%
        return {"Ali": [kayit("2026-10-27", kota=10, hafta_usd=100, plan=200), kayit("2026-10-28", kota=30, hafta_usd=300, plan=200)],
                "Veli": [kayit("2026-10-27", kota=5, hafta_usd=25, plan=100, kisi="Veli"),
                         kayit("2026-10-28", kota=20, hafta_usd=100, plan=100, kisi="Veli")]}

    def test_per_plan_dollars_per_percent_uses_the_latest_row_of_each_week(self):
        pk = P.plan_karsilastir(self.veri())
        self.assertAlmostEqual(pk[200]["oran"], 10.0)
        self.assertAlmostEqual(pk[100]["oran"], 5.0)
        self.assertAlmostEqual(pk[200]["kapasite"], 1000.0)
        self.assertAlmostEqual(pk[200]["aylik"], 1000 * 30 / 7.0)
        self.assertAlmostEqual(pk[100]["plan_basi"], 500 * 30 / 7.0 / 100)
        self.assertEqual((pk[200]["kisi"], pk[200]["hafta"]), (1, 1))

    def test_small_quota_and_missing_plan_are_left_out(self):
        v = {"Ali": [kayit("2026-10-28", kota=9, hafta_usd=300), kayit("2026-10-27", kota=50, hafta_usd=300, plan=None)]}
        self.assertEqual(P.plan_karsilastir(v), {})
        self.assertIn("Henüz yeterli veri yok", P.siralama_metni(v, "2026-10-28"))

    def test_text_states_the_ratio_when_both_plans_exist(self):
        m = P.siralama_metni(self.veri(), "2026-10-28")
        self.assertIn("**2.00×** kapasite", m)
        self.assertIn("Ek $100 karşılığını veriyor", m)
        self.assertIn("| $200 | 1 | 1 | 10.00 |", m)

    def test_only_one_plan_says_so(self):
        m = P.siralama_metni({"Ali": self.veri()["Ali"]}, "2026-10-28")
        self.assertIn("iki plandan da veri gerekiyor", m)

    def test_weekday_pattern_uses_consecutive_rows_of_one_week(self):
        v = {"Ali": [kayit("2026-10-26", kota=10, hafta_usd=100), kayit("2026-10-27", kota=20, hafta_usd=250),       # usage day Mon 26
                     kayit("2026-10-28", kota=30, hafta_usd=300),                                                    # usage day Tue 27
                     kayit("2026-10-30", kota=50, hafta_usd=400),                                                    # gap: not consecutive
                     kayit("2026-10-31", kota=60, hafta_usd=100, hafta="2026-11-08")]}                               # other week: skipped
        g = P.gun_deseni(v)
        self.assertAlmostEqual(g[(200, 0)], 15.0)       # Monday: +150 $ / +10 %
        self.assertAlmostEqual(g[(200, 1)], 5.0)        # Tuesday: +50 $ / +10 %
        self.assertEqual(sorted(g), [(200, 0), (200, 1)])   # the gap and the other week add nothing
        self.assertIn("| Pzt | — | 15.00 |", P.siralama_metni(v, "2026-10-31"))

    def test_week_table_and_model_summary(self):
        v = self.veri()
        self.assertEqual(P.hafta_tablosu(v), {"2026-11-01": {200: 10.0, 100: 5.0}})
        mo = P.model_ozeti(v, "2026-10-28")
        self.assertEqual(mo["Ali"][0]["sonnet"], 4.0)
        self.assertIn("**Ekip**", P.siralama_metni(v, "2026-10-28"))

    def test_model_table_is_ranked_like_the_main_table_with_first_names(self):
        v = {"Zeynep Yılmaz": [kayit("2026-10-28", d7=500)], "Ali Veli": [kayit("2026-10-28", d7=900)], "Onur Demir": [kayit("2026-10-28", d7=700)]}
        m = P.siralama_metni(v, "2026-10-28")
        tablo = m.split("## Modeller")[1].splitlines()
        satirlar = [l for l in tablo if l.startswith("| ")][1:]
        self.assertEqual([l.split("|")[1].strip() for l in satirlar[:3]], ["🥇", "🥈", "🥉"])
        self.assertEqual([l.split("|")[2].strip() for l in satirlar], ["Ali", "Onur", "Zeynep", "**Ekip**"])


class Quota(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="kota-", dir=TM._ROOT)

    def pace(self, **kw):
        p = {"week_used": 34, "updated": 1_800_000_000, "week_resets_at": 1_800_000_000 + 3 * 86400}
        p.update(kw)
        with open(os.path.join(self.d, "pace.json"), "w") as f:
            json.dump(p, f)

    def kota(self, simdi=1_800_000_000 + 3600):
        with mock.patch.object(maliyet, "CLAUDE", self.d):
            return P.haftalik_kota(simdi)

    def test_fresh_value_is_used_and_bad_data_is_omitted(self):
        self.pace()
        self.assertEqual(self.kota(), 34)
        self.pace(updated=1_800_000_000 - 100 * 3600)
        self.assertIsNone(self.kota(), "stale")
        self.pace(week_resets_at=1_800_000_000)
        self.assertIsNone(self.kota(), "week already reset")
        self.pace(week_used=140)
        self.assertIsNone(self.kota())
        self.pace(week_used=True)
        self.assertIsNone(self.kota())
        os.remove(os.path.join(self.d, "pace.json"))
        self.assertIsNone(self.kota(), "missing file")

    def test_forecast_reset_and_mode_come_from_pace_json(self):
        simdi = 1_800_000_000 + 3600
        self.pace(p50=106.6, mode="SIKI")
        with mock.patch.object(maliyet, "CLAUDE", self.d):
            t, sifirla, mod = P.pace_ek(simdi)
            self.assertEqual((t, mod), (107, "SIKI"))
            self.assertRegex(sifirla, r"^\d{4}-\d\d-\d\d \d\d:\d\d$")
            self.pace(mode="bad mode!", p50="x")
            self.assertEqual(P.pace_ek(simdi)[0::2], (None, None))
            self.pace(updated=1_800_000_000 - 100 * 3600, p50=90, mode="RAHAT")
            self.assertEqual(P.pace_ek(simdi), (None, None, None), "stale")
            os.remove(os.path.join(self.d, "pace.json"))
            self.assertEqual(P.pace_ek(simdi), (None, None, None), "missing file")

    def test_runout_from_pace_json_incl_bom_and_lasts_until_reset(self):
        simdi = 1_800_000_000 + 3600
        with mock.patch.object(maliyet, "CLAUDE", self.d):
            self.pace(p50=106.6, runout_at=1_800_000_000 + 7200)
            b, z = P.bitis_ek(simdi)
            self.assertRegex(b, r"^\d{4}-\d\d-\d\d \d\d:\d\d$")
            self.assertEqual(b, time.strftime("%Y-%m-%d %H:%M", time.localtime(1_800_000_000 + 7200)))
            self.assertRegex(z, r"^\d{4}-\d\d-\d\d \d\d:\d\d$")
            self.pace(p50=80.0, runout_at=None)
            self.assertEqual(P.bitis_ek(simdi)[0], "yeter")
            self.pace(p50=80.0)                                      # file from an older pace_forecast: no runout_at key
            self.assertEqual(P.bitis_ek(simdi), (None, None))
            self.pace(updated=1_800_000_000 - 100 * 3600, p50=90, runout_at=None)
            self.assertEqual(P.bitis_ek(simdi), (None, None), "stale")
            with open(os.path.join(self.d, "pace.json"), "w", encoding="utf-8-sig") as f:   # Windows file with a BOM
                json.dump({"week_used": 34, "updated": 1_800_000_000, "week_resets_at": 1_800_000_000 + 3 * 86400,
                           "p50": 90.0, "runout_at": 1_800_000_000 + 7200}, f)
            self.assertIsNotNone(P.bitis_ek(simdi)[0])
            os.remove(os.path.join(self.d, "pace.json"))
            self.assertEqual(P.bitis_ek(simdi), (None, None), "missing file")


@unittest.skipUnless(shutil.which("git"), "git not installed")
class Commits(unittest.TestCase):
    def setUp(self):
        self.kok = tempfile.mkdtemp(prefix="kok-", dir=TM._ROOT)

    def git(self, repo, *a, env=None):
        e = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull, **(env or {}))
        r = subprocess.run(["git", "-C", repo, *a], capture_output=True, text=True, env=e)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def repo(self, ad, base="main"):
        yol = os.path.join(self.kok, ad)
        os.makedirs(yol)
        self.git(yol, "init", "-q", "-b", base)
        return yol

    def commit(self, repo, eposta, saat_once, ad="f"):
        t = int(time.time() - saat_once * 3600)
        d = "%d +0000" % t
        with open(os.path.join(repo, ad + str(t) + eposta), "w") as f:
            f.write(eposta)
        self.git(repo, "add", "-A")
        self.git(repo, "-c", "user.name=x", "-c", "user.email=" + eposta, "commit", "-q", "-m", "c",
                 env={"GIT_AUTHOR_DATE": d, "GIT_COMMITTER_DATE": d})

    def remote_ref(self, repo, ad):
        self.git(repo, "update-ref", "refs/remotes/origin/" + ad, "HEAD")

    def test_counts_own_recent_commits_on_the_integration_branch_across_clones(self):
        a = self.repo("a")                              # oldest first, like a real history
        self.commit(a, "me@example.com", 30)            # older than 24 h
        self.commit(a, "other@example.com", 3)
        self.commit(a, "ME@example.com", 2)             # e-mail case does not matter
        self.commit(a, "me@example.com", 1)
        self.remote_ref(a, "main")
        self.commit(a, "me@example.com", 0.5)           # local only, never reached origin: not "dev'e giren"
        b = self.repo(os.path.join("grup", "b"), base="dev")
        self.commit(b, "me@example.com", 5)
        self.remote_ref(b, "dev")
        c = self.repo("c")                              # no origin ref at all: skipped
        self.commit(c, "me@example.com", 1)
        self.assertEqual(P.commit_say("me@example.com", self.kok), 3)
        self.assertEqual(P.commit_say("other@example.com", self.kok), 1)
        self.assertEqual(P.commit_say("nobody@example.com", self.kok), 0)

    def test_unknown_when_no_email_or_root(self):
        self.assertIsNone(P.commit_say("", self.kok))
        self.assertIsNone(P.commit_say("me@example.com", None))

    def test_numstat_parse_excludes_noise(self):
        t = lambda *x: "\t".join(x)
        out = "\n".join(["\x01h1", "", t("10", "2", "src/a.cs"), t("-", "-", "img/logo.png"), t("500", "5", "package-lock.json"),
                         t("9", "9", "web/yarn.lock"), t("9", "9", "Cargo.lock"), t("9", "9", "pnpm-lock.yaml"), t("9", "9", "x/packages.lock.json"),
                         t("99", "0", "Data/Migrations/20260101_A.Designer.cs"), t("99", "0", "Data/AppDbContextModelSnapshot.cs"),
                         t("7", "0", "Data/Migrations/20260101_A.cs"), t("9", "9", "w/app.min.js"), t("9", "9", "w/app.min.css"), t("9", "9", "w/app.js.map"),
                         t("9", "9", "bin/x.dll"), t("9", "9", "a/obj/y.cs"), t("9", "9", "node_modules/p/i.js"), t("9", "9", "ui/dist/m.js"),
                         t("9", "9", "ui/build/m.js"), t("9", "9", "web/wwwroot/lib/jq.js"), t("0", "0", "old.cs => new.cs"),
                         t("3", "1", "src/{a => b}/c.cs"), "\x01h2", t("0", "0", "{x => y}/z.cs"), "\x01h3", t("1", "1", "builder/a.cs")])
        self.assertEqual(P.numstat_ayristir(out), {"h1": (10 + 7 + 3, 2 + 0 + 1), "h2": (0, 0), "h3": (1, 1)})
        self.assertEqual(P.numstat_ayristir(""), {})

    def test_generated_and_data_files_are_excluded(self):
        t = lambda *x: "\t".join(x)
        out = "\n".join(["\x01h1", t("9000", "0", "iso/standartlar/TS_EN_ISO_27002.pdf"), t("900", "0", "Yardımcı/Doğru/PRODUCT_A_SMS_CDR_1.cdr"),
                         t("90", "0", "Hata/bad_REHBER_2022.bad"), t("90", "0", "logs/x.jsonl"), t("90", "0", "img/a.svg"),
                         t("90", "0", "Api/Client.g.cs"), t("90", "0", "Api/Client.g.i.cs"), t("90", "0", "Hata/22-10-20/bad_REHBER_202210180930"), t("90", "0", "Rehber/PRODUCT_A_312_20221002"), t("5", "1", "src/Real.cs"), t("4", "0", "docs/a.md")])
        self.assertEqual(P.numstat_ayristir(out), {"h1": (9, 1)})

    def test_non_ascii_paths_are_not_quoted_by_git(self):
        a = self.repo("a")
        t = int(time.time() - 3600)
        d = "%d +0000" % t
        os.makedirs(os.path.join(a, "Yardımcı"))
        with open(os.path.join(a, "Yardımcı", "x.cdr"), "w") as f:
            f.write("1\n" * 40)
        with open(os.path.join(a, "k.py"), "w") as f:
            f.write("1\n")
        self.git(a, "add", "-A")
        self.git(a, "-c", "user.name=x", "-c", "user.email=me@example.com", "commit", "-q", "-m", "c",
                 env={"GIT_AUTHOR_DATE": d, "GIT_COMMITTER_DATE": d})
        self.remote_ref(a, "main")
        self.assertEqual(P.commit_ve_satir("me@example.com", self.kok), (1, 1, 0))

    def test_line_sums_of_the_same_commits_and_dedup_across_clones(self):
        a = self.repo("a")
        t = int(time.time() - 3600)
        d = "%d +0000" % t
        with open(os.path.join(a, "k.py"), "w") as f:
            f.write("1\n2\n3\n")
        with open(os.path.join(a, "package-lock.json"), "w") as f:
            f.write("x\n" * 50)
        self.git(a, "add", "-A")
        self.git(a, "-c", "user.name=x", "-c", "user.email=me@example.com", "commit", "-q", "-m", "c",
                 env={"GIT_AUTHOR_DATE": d, "GIT_COMMITTER_DATE": d})
        self.remote_ref(a, "main")
        b = os.path.join(self.kok, "b")
        subprocess.run(["git", "clone", "-q", a, b], check=True, capture_output=True)
        self.assertEqual(P.commit_ve_satir("me@example.com", self.kok), (1, 3, 0))
        self.assertEqual(P.commit_ve_satir("other@example.com", self.kok), (0, 0, 0))
        self.assertIsNone(P.commit_ve_satir("", self.kok))

    def test_same_commit_in_two_clones_counts_once(self):
        a = self.repo("a")
        self.commit(a, "me@example.com", 1)
        self.remote_ref(a, "main")
        b = os.path.join(self.kok, "b")
        subprocess.run(["git", "clone", "-q", a, b], check=True, capture_output=True)
        self.assertEqual(P.commit_say("me@example.com", self.kok), 1)

    def test_root_that_is_not_there_is_skipped(self):
        with mock.patch.dict(os.environ, {"CLAUDE_SHARED_KOK": os.path.join(self.kok, "yok")}):
            self.assertIsNone(P.kok_dizin())



    def test_default_layout_is_found_without_a_setting(self):
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home, True)
        kok = os.path.join(home, "src")
        os.makedirs(kok)
        with mock.patch.dict(os.environ, {}, clear=False), mock.patch.object(P, "HOME", home),                 mock.patch.object(P, "dosyadan_oku", return_value=""):
            os.environ.pop("CLAUDE_SHARED_KOK", None)
            self.assertEqual(P.kok_dizin(), kok)


class SameDayFill(unittest.TestCase):
    def test_same_day_row_with_unknown_commits_and_zero_split_is_refilled(self):
        eski = kayit("2026-10-30", opus=0.0, sonnet=0.0)
        yeni = kayit("2026-10-30", commit=4, ekle=10, sil=2)
        metin = P.dosyaya_ekle("", "Ali", P.satir_kur(eski), "2026-10-30")
        metin = P.dosyaya_ekle(metin, "Ali", P.satir_kur(yeni), "2026-10-30")
        r = P.dosya_satirlari(metin)
        self.assertEqual((len(r), r[0]["commit"], r[0]["usd"]["sonnet"]), (1, 4, 2.0))
        self.assertEqual(P.dosyaya_ekle(metin, "Ali", P.satir_kur(yeni), "2026-10-30"), metin)


class ModelCoverage(unittest.TestCase):
    GUN = {"2026-10-26": ({"opus": 5.0}, {"opus": 1.0}), "2026-10-27": ({"opus": 2.0, "sonnet": 1.0}, {"opus": 1.0}),
           "2026-10-28": ({"sonnet": 4.0}, {"sonnet": 2.0})}

    def rec(self, onceki):
        k = kayit("2026-10-30")
        k["model_gunleri"] = dict(self.GUN)
        P.kapsam_uygula(k, [kayit(t) for t in onceki])
        return k

    def test_first_row_covers_last_seven_days_even_if_yesterday_was_idle(self):
        k = self.rec([])   # 2026-10-29 has no record: used to give an all-zero split
        self.assertEqual(k["usd"], {"opus": 7.0, "sonnet": 5.0})

    def test_daily_row_covers_only_uncovered_days(self):
        self.assertEqual(self.rec(["2026-10-28"])["usd"], {"sonnet": 4.0})   # row 28th covered the 27th; 28th and 29th are new
        self.assertEqual(self.rec(["2026-10-29"])["usd"], {})   # normal day: only yesterday (idle)

    def test_commit_window_follows_the_same_days(self):
        for onceki, saat in (([], 168), (["2026-10-28"], 48), (["2026-10-29"], 24)):
            k = kayit("2026-10-30")
            k["eposta"], k["kok"] = "me@x", "kok"
            with mock.patch.object(P, "commit_ve_satir", return_value=(7, 70, 7)) as m:
                P.kapsam_uygula(k, [kayit(t) for t in onceki])
            m.assert_called_once_with("me@x", "kok", saat)
            self.assertEqual((k["commit"], k["ekle"], k["sil"]), (7, 70, 7))

    def test_unknown_root_keeps_dashes(self):
        k = kayit("2026-10-30")
        k["eposta"], k["kok"] = "me@x", None
        P.kapsam_uygula(k, [])
        self.assertIsNone(k["commit"])

    def test_no_double_count_across_rows(self):
        a = self.rec([])
        self.assertEqual(sum(a["usd"].values()), 12.0)


@unittest.skipUnless(shutil.which("git"), "git not installed")
class SharedFiles(unittest.TestCase):
    """The git flow against a local bare repository: own row, regenerated ranking, ownership, retry after a lost race."""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="uzak-", dir=TM._ROOT)
        self.uzak = os.path.join(self.d, "uzak.git")
        self.env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x",
                        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")
        self.sh("git", "init", "-q", "--bare", "-b", "main", self.uzak)
        tohum = os.path.join(self.d, "tohum")
        self.sh("git", "clone", "-q", self.uzak, tohum)
        yol = os.path.join(tohum, *P.KISILER_YOL.split("/"))
        os.makedirs(os.path.dirname(yol))
        with open(yol, "w") as f:
            json.dump({"gh-ali": "Ali", "gh-veli": "Veli"}, f)
        self.sh("git", "-C", tohum, "add", "-A")
        self.sh("git", "-C", tohum, "commit", "-q", "-m", "seed")
        self.sh("git", "-C", tohum, "push", "-q", "origin", "HEAD:main")
        self.tohum = tohum
        self._env = mock.patch.dict(os.environ, self.env)
        self._env.start()
        self._genv = mock.patch.object(P, "GIT_ENV", dict(self.env, GIT_TERMINAL_PROMPT="0"))
        self._genv.start()

    def tearDown(self):
        self._genv.stop()
        self._env.stop()

    def sh(self, *a):
        r = subprocess.run(a, capture_output=True, text=True, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def calistir(self, klon, r):
        return P.bir_deneme(klon, self.uzak, r, r["tarih"])

    def test_row_and_ranking_are_committed_and_pushed_and_a_second_run_changes_nothing(self):
        k = os.path.join(self.d, "klon1")
        self.assertEqual(self.calistir(k, kayit("2026-10-28", kota=40, hafta_usd=300)), "itildi")
        self.assertEqual(self.calistir(k, kayit("2026-10-28", kota=40, hafta_usd=300)), "degisiklik yok")
        self.sh("git", "-C", self.tohum, "pull", "-q", "origin", "main")
        ali = TM.rd(os.path.join(self.tohum, "raporlar", "kullanim", "ali.md"))
        self.assertEqual(len(P.dosya_satirlari(ali)), 1)
        self.assertIn("Ali", TM.rd(os.path.join(self.tohum, "raporlar", "kullanim", "SIRALAMA.md")))
        self.assertIn("chore(kullanim): Ali 2026-10-28", self.sh("git", "-C", self.tohum, "log", "-1", "--format=%s"))

    def test_second_person_regenerates_the_ranking_from_all_files(self):
        self.calistir(os.path.join(self.d, "k1"), kayit("2026-10-28", d7=100))
        self.calistir(os.path.join(self.d, "k2"), kayit("2026-10-28", d7=500, plan=100, kisi="Veli"))
        self.sh("git", "-C", self.tohum, "pull", "-q", "origin", "main")
        s = TM.rd(os.path.join(self.tohum, "raporlar", "kullanim", "SIRALAMA.md"))
        self.assertLess(s.index("Veli"), s.index("Ali"))
        self.assertEqual(self.sh("git", "-C", self.tohum, "rev-list", "--count", "HEAD"), "3")

    def test_unlisted_name_writes_nothing_and_a_stray_file_is_not_ranked(self):
        with self.assertRaises(RuntimeError) as c:
            self.calistir(os.path.join(self.d, "k3"), kayit("2026-10-28", kisi="Mallory"))
        self.assertIn("kisiler.json", str(c.exception))
        yol = os.path.join(self.tohum, "raporlar", "kullanim")
        os.makedirs(yol)
        with open(os.path.join(yol, "mallory.md"), "w", encoding="utf-8") as f:
            f.write(P.dosya_basligi("Mallory") + P.satir_kur(kayit("2026-10-28", d7=9999, kisi="Mallory")) + "\n")
        self.sh("git", "-C", self.tohum, "add", "-A")
        self.sh("git", "-C", self.tohum, "commit", "-q", "-m", "stray")
        self.sh("git", "-C", self.tohum, "push", "-q", "origin", "HEAD:main")
        self.calistir(os.path.join(self.d, "k4"), kayit("2026-10-28", d7=10))
        self.sh("git", "-C", self.tohum, "pull", "-q", "origin", "main")
        self.assertNotIn("Mallory", TM.rd(os.path.join(yol, "SIRALAMA.md")))

    def test_lost_race_is_redone_once_on_a_fresh_fetch(self):
        k = os.path.join(self.d, "k5")
        gercek = P.klon_hazirla
        olay = {"n": 0}

        def yarisli(klon, uzak):
            gercek(klon, uzak)
            olay["n"] += 1
            if olay["n"] == 1:      # somebody else pushes right after our fetch
                self.sh("git", "-C", self.tohum, "pull", "-q", "origin", "main")
                with open(os.path.join(self.tohum, "x.txt"), "w") as f:
                    f.write("x")
                self.sh("git", "-C", self.tohum, "add", "-A")
                self.sh("git", "-C", self.tohum, "commit", "-q", "-m", "other")
                self.sh("git", "-C", self.tohum, "push", "-q", "origin", "HEAD:main")
        with mock.patch.object(P, "klon_hazirla", yarisli), mock.patch.object(P.time, "sleep"), \
                mock.patch.object(P, "bugunku_kayit", return_value=kayit("2026-10-28")), \
                mock.patch.object(P, "kisi_adi", return_value="Ali"), \
                mock.patch.dict(os.environ, {"CLAUDE_SHARED_KLON": k, "CLAUDE_SHARED_UZAK": self.uzak}):
            self.assertEqual(P.paylas(), 0)
        self.assertEqual(olay["n"], 2)
        self.sh("git", "-C", self.tohum, "pull", "-q", "origin", "main")
        self.assertTrue(os.path.isfile(os.path.join(self.tohum, "raporlar", "kullanim", "ali.md")))


class CostWindowsAndCli(unittest.TestCase):
    """The numbers come from the real CLI on a synthetic transcript; --kuru prints the row and writes nothing."""

    def setUp(self):
        self.cfg = TM.Cfg()
        self.ayar = tempfile.mkdtemp(prefix="home-", dir=TM._ROOT)

    def run_cli(self, *args, kisi="Test Kisi"):
        env = {"HOME": self.ayar, "USERPROFILE": self.ayar, "CLAUDE_SHARED_KISI": kisi, "CLAUDE_SHARED_PLAN": "200",
               "CLAUDE_SHARED_KOK": os.path.join(self.ayar, "yok")}
        return self.cfg.run(*args, env=env)

    def test_row_has_windows_quota_plan_week_and_models(self):
        now = time.time()
        self.cfg.transcript("p", "s1", [TM.asst(now - 600, "m1", model="claude-opus-5-5", out=20000),
                                        TM.asst(now - 30 * 3600, "m2", model="claude-sonnet-5-5", out=40000)])
        with open(os.path.join(self.cfg.dir, "pace.json"), "w") as f:
            json.dump({"week_used": 41, "updated": now - 3600, "week_resets_at": now + 86400}, f)
        r = self.run_cli("--paylas", "--kuru")
        self.assertEqual(r.returncode, 0, r.stderr)
        satir = [l for l in r.stdout.splitlines() if l.startswith("| 20")]
        self.assertEqual(len(satir), 1, r.stdout)
        p = P.satir_ayristir(satir[0])
        self.assertEqual((p["d24"], p["kota"], p["plan"], p["commit"]), (p["d24"], 41, 200, None))
        self.assertIsNotNone(p["hafta"])
        self.assertIsNotNone(p["hafta_usd"])
        self.assertTrue(os.path.isfile(os.path.join(self.cfg.dir, "maliyet", "gunluk.jsonl")))   # --paylas includes --kaydet
        self.assertNotIn("projects", satir[0])

    def test_sharing_failure_keeps_exit_code_zero_and_logs(self):
        self.cfg.transcript("p", "s1", [TM.asst(time.time() - 600, "m1", out=20000)])
        r = self.cfg.run("--paylas", env={"HOME": self.ayar, "USERPROFILE": self.ayar, "CLAUDE_SHARED_KISI": "Test Kisi",
                                           "CLAUDE_SHARED_UZAK": os.path.join(self.ayar, "yok.git"),
                                           "CLAUDE_SHARED_KLON": os.path.join(self.ayar, "klon")})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("BASARISIZ", TM.rd(os.path.join(self.cfg.dir, "maliyet", "paylas.log")))
        self.assertNotIn("Traceback", r.stderr)

    def test_without_a_name_nothing_is_sent(self):
        with open(os.path.join(self.ayar, ".gitconfig"), "w") as f:
            f.write("")
        env = {"HOME": self.ayar, "USERPROFILE": self.ayar, "CLAUDE_SHARED_KISI": "", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
        r = self.cfg.run("--paylas", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("kisi adi yok", TM.rd(os.path.join(self.cfg.dir, "maliyet", "paylas.log")))


class ShippedFiles(unittest.TestCase):
    def test_module_has_no_personal_or_machine_strings(self):
        txt = TM.rd(os.path.join(TM.BIN, "maliyet_paylas.py")).lower()
        # "slack" itself is allowed since the Slack inbox column (the module reads the local poller's state); what stays forbidden is
        # anything that talks to Slack or carries a credential
        for b in ("/home/", "example-user", "example-host", "hooks.slack.com", "api.slack.com", "chat.postmessage", "xoxb", "xoxp", "xapp-",
                  "slack-gelen.jsonl", "okundu"):
            self.assertNotIn(b, txt)



class SiradakiIsKimAlir(unittest.TestCase):
    def test_sira(self):
        sira = [{"kisi": "Ali X", "mod": "SIKI", "tahmin": 10}, {"kisi": "Cem X", "mod": None, "tahmin": None},
                {"kisi": "Bora X", "mod": "BOL", "tahmin": 80}, {"kisi": "Deniz X", "mod": "BOL", "tahmin": 40},
                {"kisi": "Ece X", "mod": "NORMAL", "tahmin": 5}]
        m = "\n".join(P.siradaki_is_satirlari(sira))
        self.assertIn("### Sıradaki işi kim alır", m)
        adlar = re.findall(r"^\| \d \| (\w+) \|", m, re.M)
        self.assertEqual(adlar, ["Deniz", "Bora", "Ece", "Ali", "Cem"])
        self.assertIn("| 5 | Cem | veri yok | — |", m)
        self.assertIn("| 1 | Deniz | BOL | %40 |", m)

    def test_testci_is_almaz(self):
        sira = [{"kisi": "Ali X", "mod": "BOL", "tahmin": 5}, {"kisi": "Ece X", "mod": "NORMAL", "tahmin": 50},
                {"kisi": "Veli X", "mod": "BOL", "tahmin": 1}]
        m = "\n".join(P.siradaki_is_satirlari(sira, {"Ali X": "testci", "Veli X": "yonetici"}))
        self.assertEqual(re.findall(r"^\| \d \| (\w+) \|", m, re.M), ["Ece"])
        self.assertIn("_testçi (iş almaz): Ali_", m)
        self.assertIn("_yönetici (iş almaz): Veli_", m)
        self.assertNotIn("testçi (iş almaz)", "\n".join(P.siradaki_is_satirlari(sira, {})))

    def test_siralama_metninde(self):
        self.assertNotIn("Sıradaki işi kim alır", P.siralama_metni({}, "2026-10-28"))


class GithubCommitSayStdoutYok(unittest.TestCase):
    def _cagir(self, run):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(P, "_onbellek_yolu", return_value=os.path.join(d, "ob.json")), \
                mock.patch.object(P.subprocess, "run", run):
            with mock.patch.dict(os.environ, {"GH_ORG": "example-org"}):
                return P.github_commit_say("x", "2026-11-01", simdi=1.0)

    def test_no_org_configured_is_unknown_without_a_gh_call(self):
        kos = mock.Mock()
        with tempfile.TemporaryDirectory() as d, mock.patch.object(P, "_onbellek_yolu", return_value=os.path.join(d, "ob.json")), \
                mock.patch.object(P.subprocess, "run", kos), mock.patch.object(P, "dosyadan_oku", return_value=""), \
                mock.patch.dict(os.environ, {"GH_ORG": ""}):
            self.assertIsNone(P.github_commit_say("x", "2026-11-01", simdi=1.0))
        kos.assert_not_called()

    def test_stdout_none_unknown(self):
        self.assertIsNone(self._cagir(lambda *a, **k: mock.Mock(returncode=0, stdout=None)))

    def test_utf8_ve_otomasyon_ayri(self):
        kw = {}
        def run(*a, **k):
            kw.update(k)
            return mock.Mock(returncode=0, stdout="feat: ğüş\nchore(kullanim): x\n")
        self.assertEqual(self._cagir(run), 1)
        self.assertEqual(kw.get("encoding"), "utf-8")


if __name__ == "__main__":
    unittest.main()
