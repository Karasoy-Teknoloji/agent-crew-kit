"""Tests for maliyet_verim: weekly per-project summary, finished-work count, cost per finished item, the leaderboard section and the
change-only commit. Stdlib only, synthetic data, no network (the board items are plain dicts, the remote is a local bare repository).

Run: python3 -I -m unittest discover -s plugins/kota-temposu/tests -p 'test_verim.py'
"""
import datetime
import json
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_maliyet as TM  # noqa: E402  (scratch HOME, CLAUDE_SHARED_VERIM=0)
import maliyet_paylas as P  # noqa: E402
import maliyet_verim as V  # noqa: E402
import test_paylas_degisim as D  # noqa: E402
from test_paylas import gun  # noqa: E402

def setUpModule():
    os.makedirs(TM._CFG, exist_ok=True)   # test_maliyet's tearDownModule (or another module's) may have removed the scratch tree


UTC = datetime.timezone.utc
BUGUN = "2026-11-04"   # Wednesday of 2026-W45 (Mon 2026-11-02 .. Sun 2026-11-08)


def is_(oturum, proje, usd, tl, kota_tl, **tok):
    d = {"oturum": oturum, "proje": proje, "is": "Musteri ACME fatura isi", "id": "abc123", "usd_toplam": usd, "tl": tl, "kota_payi_tl": kota_tl,
         "hafta_yuzde": usd / 10.0, "in": 10, "out": 20, "cr": 300, "cw5": 4, "cw1h": 5}
    d.update(tok)
    return d


def rapor():
    """A maliyet_raporu.donusum()-shaped result: two jobs of session s1 (project ops), one job of s2 (product-c), session s3 without jobs."""
    j1, j2, j3 = is_("s1", "ops", 10.0, 500.0, 5.0), is_("s1", "ops", 6.0, 300.0, 3.0), is_("s2", "product-c", 4.0, 200.0, 2.0)
    oturum = lambda sid, usd: {"oturum": sid, "proje_klasoru": "/home/ali/musteri-acme", "usd": usd, "usd_toplam": usd, "tl": usd * 50, "kota_payi_tl": usd / 2,
                               "hafta_yuzde": usd / 10.0, "in": 100, "out": 200, "cr": 3000, "cw5": 40, "cw1h": 50}
    return {"kur": 50.0, "projeler": [{"proje": "ops", "isler": [j1, j2]}, {"proje": "product-c", "isler": [j3]}],
            "oturumlar": [oturum("s1", 2.0), oturum("s2", 1.0), oturum("s3", 3.0)]}


class Toplama(unittest.TestCase):
    def setUp(self):
        self.o = V.ozet_kur(rapor(), "Ali", BUGUN, bitti=3)

    def test_week_and_person(self):
        self.assertEqual((self.o["hafta"], self.o["bas"], self.o["bit"], self.o["kisi"], self.o["bitti"]), ("2026-W45", "2026-11-02", "2026-11-08", "Ali", 3))

    def test_per_project_totals_and_unallocated_session_bucket(self):
        p = self.o["projeler"]
        self.assertEqual(sorted(p), ["ana-oturum", "ops", "product-c"])
        self.assertEqual((p["ops"]["usd"], p["ops"]["tl_api"], p["ops"]["tl_abonelik"]), (16.0, 800.0, 8.0))
        self.assertEqual((p["ana-oturum"]["usd"], p["ana-oturum"]["tl_api"]), (3.0, 150.0))     # s3 started no job
        t = self.o["toplam"]
        self.assertEqual(t["usd"], 23.0)
        self.assertEqual(t["usd"], round(sum(x["usd"] for x in p.values()), 2))                 # the total is the sum of the projects
        self.assertEqual(t["tl_api"], round(sum(x["tl_api"] for x in p.values()), 2))

    def test_tokens_by_type_include_the_session_share(self):
        t = self.o["projeler"]["ops"]["tok"]
        self.assertEqual(t["in"], 10 + 10 + 100)      # two jobs + the whole of session s1 (its jobs are both in ops)
        self.assertEqual(t["cr"], 300 + 300 + 3000)
        self.assertEqual(set(t), {"in", "out", "cr", "cw5", "cw1h"})

    def test_nothing_identifying_is_published(self):
        txt = V.ozet_yaz_metni(self.o)
        for yasak in ("ACME", "fatura", "abc123", "/home", "musteri", "s1", "proje_klasoru", "\"is\""):
            self.assertNotIn(yasak, txt)
        self.assertEqual(sorted(self.o), ["bas", "bit", "bitti", "hafta", "kisi", "kur", "projeler", "surum", "toplam"])

    def test_missing_rate_and_calibration_give_none_not_zero(self):
        R = rapor()
        for g in R["projeler"]:
            for j in g["isler"]:
                j.update(tl=None, kota_payi_tl=None, hafta_yuzde=None)
        o = V.ozet_kur(R, "Ali", BUGUN)
        self.assertIsNone(o["projeler"]["ops"]["tl_api"])
        self.assertIsNone(o["projeler"]["ops"]["tl_abonelik"])
        self.assertEqual(o["projeler"]["ops"]["usd"], 16.0)


class Bitti(unittest.TestCase):
    def oge(self, url, kim, zaman, repo="o/r", n=1):
        return {"url": url, "repo": repo, "number": n, "assignees": kim, "status_at": zaman}

    def test_counts_only_this_week_this_person_distinct(self):
        o = [self.oge("u1", ["ali"], "2026-11-02T00:30:00Z"),            # Monday, inside
             self.oge("u2", ["Ali", "veli"], "2026-11-04T10:00:00Z"),    # inside, login case ignored, two assignees
             self.oge("u2", ["ali"], "2026-11-04T10:00:00Z"),            # same issue on a second board: once
             self.oge("u3", ["ali"], "2026-10-31T22:00:00Z"),            # last week
             self.oge("u4", ["ali"], "2026-11-09T00:10:00Z"),            # next week
             self.oge("u5", ["veli"], "2026-11-03T10:00:00Z"),           # someone else
             self.oge("u6", [], "2026-11-03T10:00:00Z"),                 # unassigned
             self.oge("u7", ["ali"], None)]                              # no timestamp
        self.assertEqual(V.bitti_hesapla(o, "ali", BUGUN, UTC), 2)

    def test_week_boundaries_use_the_local_zone(self):
        tz = datetime.timezone(datetime.timedelta(hours=3))
        o = [self.oge("u1", ["ali"], "2026-11-01T22:00:00Z"),            # Mon 01:00 local (+3): inside although Sunday in UTC
             self.oge("u2", ["ali"], "2026-11-08T21:30:00Z")]            # Mon 00:30 local next week: outside
        self.assertEqual(V.bitti_hesapla(o, "ali", BUGUN, tz), 1)
        self.assertEqual(V.bitti_hesapla(o, "ali", BUGUN, UTC), 1)       # UTC: first is Sunday (out), second is Sunday 21:30 (in)

    def test_cache_hit_makes_no_board_call(self):
        yol = V._onbellek_yolu()
        os.makedirs(os.path.dirname(yol), exist_ok=True)
        with open(yol, "w") as f:
            json.dump({"ali|2026-W45": {"n": 7, "t": 1000.0}}, f)
        with mock.patch.object(V, "_tools_modul", side_effect=AssertionError("network")):
            self.assertEqual(V.bitti_say("Ali", BUGUN, simdi=1000.0 + 3600), 7)
            self.assertIsNone(V.bitti_say("Ali", BUGUN, simdi=1000.0 + 7 * 3600))   # stale cache + no tools -> unknown, not a crash


class Verim(unittest.TestCase):
    def ozet(self, ad, tl, bitti, hafta="2026-W45", usd=100.0, tl_ab=None):
        return {"surum": 1, "kisi": ad, "hafta": hafta, "bitti": bitti,
                "toplam": {"usd": usd, "tl_api": tl, "tl_abonelik": tl_ab, "kota_yuzde": 40.0, "tok": {}},
                "projeler": {"ops": {"usd": 60.0}, "product-c": {"usd": 30.0}, "x": {"usd": 5.0}, "y": {"usd": 5.0}}}

    def test_cost_per_item_and_dash_for_zero(self):
        self.assertEqual(V.is_basina(1000.0, 4), 250.0)
        self.assertIsNone(V.is_basina(1000.0, 0))
        self.assertIsNone(V.is_basina(1000.0, None))
        self.assertIsNone(V.is_basina(None, 3))

    def test_efficiency_ranking_lowest_first_and_needs_one_item(self):
        oz = {"Ali": self.ozet("Ali", 9000.0, 3), "Veli": self.ozet("Veli", 2000.0, 4), "Cem": self.ozet("Cem", 100.0, 0),
              "Eski": self.ozet("Eski", 10.0, 5, hafta="2026-W44")}
        self.assertEqual(V.verimlilik_sirasi(oz, BUGUN), [("Veli", 500.0), ("Ali", 3000.0)])      # Cem has 0 items, Eski is last week

    def test_section_rows(self):
        oz = {"Ali": self.ozet("Ali", 9000.0, 3, tl_ab=300.0), "Cem": self.ozet("Cem", 100.0, 0)}
        md = "\n".join(V.tablo_satirlari(["Ali", "Cem", "Yok"], oz, BUGUN, P.MADALYA))
        self.assertIn("| 🥇 | Ali | %40 | 3 | ₺9.000 | ₺300 | ₺3.000 | ₺100 | ops %60 · product-c %30 · x %5 · +1 |", md)
        self.assertIn("| 🥈 | Cem | %40 | 0 | ₺100 | — | — | — | ops %60 · product-c %30 · x %5 · +1 |", md)                            # zero finished -> dashes
        self.assertIn("| 🥉 | Yok | — | — | — | — | — | — | — |", md)                              # no summary yet
        self.assertIn("🥇 Ali ₺3.000/iş", md)
        self.assertNotIn("Cem ₺", md.split("Verimlilik")[1])

    def test_change_key_ignores_small_dollar_drift_but_sees_a_finished_item(self):
        a = {"Ali": self.ozet("Ali", 1.0, 3, usd=120.0)}
        self.assertEqual(V.verim_anahtari(a, BUGUN), V.verim_anahtari({"Ali": self.ozet("Ali", 1.0, 3, usd=150.0)}, BUGUN))
        self.assertNotEqual(V.verim_anahtari(a, BUGUN), V.verim_anahtari({"Ali": self.ozet("Ali", 1.0, 4, usd=120.0)}, BUGUN))
        self.assertNotEqual(V.verim_anahtari(a, BUGUN), V.verim_anahtari({"Ali": self.ozet("Ali", 1.0, 3, usd=210.0)}, BUGUN))


class Akis(D.Akis):
    """The timer run with a weekly summary: published in the same commit, change-only, shown in SIRALAMA.md."""

    def hafta_id(self):
        return V.hafta_siniri(gun())[0]

    def r(self, bitti, usd=100.0, tl=5000.0, **kw):
        k = D.satir(fixture=dict(d7=100, kota=40), **kw)
        k["hafta_ozet"] = {"surum": 1, "kisi": "Ali", "hafta": self.hafta_id(), "bas": "x", "bit": "y", "kur": 50.0, "bitti": bitti,
                           "toplam": {"usd": usd, "tl_api": tl, "tl_abonelik": 400.0, "kota_yuzde": 40.0, "tok": {"in": 1}},
                           "projeler": {"ops": {"usd": usd, "tl_api": tl, "tl_abonelik": 400.0, "kota_yuzde": 40.0, "tok": {"in": 1}}}}
        return k

    def json(self):
        self.sh("git", "-C", self.tohum, "pull", "-q", "origin", "main")
        return json.loads(TM.rd(os.path.join(self.tohum, "raporlar", "kullanim", "ali.hafta.json")))

    def test_published_in_the_same_commit_and_shown(self):
        self.assertEqual(self.calistir(self.r(3)), "itildi")
        self.assertIn("raporlar/kullanim/ali.hafta.json", self.dosyalar())
        self.assertNotIn("raporlar/kullanim/veli.hafta.json", self.dosyalar())                   # nobody else's file
        self.assertEqual(self.json()["bitti"], 3)
        md = self.sh("git", "--git-dir", self.uzak, "show", "main:raporlar/kullanim/SIRALAMA.md")
        self.assertIn("### Biten iş ve maliyeti (%s)" % self.hafta_id(), md)
        self.assertIn("₺1.667", md)                                                               # 5000 / 3 per finished item
        self.assertIn("**Verimlilik", md)

    def test_unchanged_summary_commits_nothing_a_new_finished_item_does(self):
        self.calistir(self.r(3))
        kafa = self.kafa()
        self.assertEqual(self.calistir(self.r(3, usd=130.0, tl=6500.0)), "degisiklik yok")        # dollar drift inside one step
        self.assertEqual(self.kafa(), kafa)
        self.assertEqual(self.calistir(self.r(4)), "itildi")
        self.assertIn("biten iş 3→4", self.mesaj())
        self.assertEqual(self.json()["bitti"], 4)

    def test_unknown_finished_count_keeps_the_published_one_of_the_same_week(self):
        self.calistir(self.r(3))
        self.assertEqual(self.calistir(self.r(None, usd=260.0)), "itildi")                        # dollars crossed a step
        self.assertEqual(self.json()["bitti"], 3)

    def test_run_without_summary_behaves_as_before(self):
        r = D.satir(fixture=dict(d7=100, kota=40))
        self.assertEqual(self.calistir(r), "itildi")
        self.assertNotIn("raporlar/kullanim/ali.hafta.json", self.dosyalar())
        self.assertNotIn("Biten iş", self.sh("git", "--git-dir", self.uzak, "show", "main:raporlar/kullanim/SIRALAMA.md"))

