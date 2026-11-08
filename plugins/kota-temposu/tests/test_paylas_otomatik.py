"""The status line's throttled automatic share (maliyet --paylas --otomatik): throttle, lock, change detection (weekly % only),
the in-place row update, and the shell gate in statusline.sh. Stdlib only, synthetic data, no network (local bare remote).

Run: python3 -I -m unittest discover -s plugins/kota-temposu/tests -p 'test_paylas_otomatik.py'
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_maliyet as TM  # noqa: E402  (sets the scratch HOME / CLAUDE_CONFIG_DIR before the modules are imported)
import maliyet  # noqa: E402
import maliyet_paylas as P  # noqa: E402
from test_paylas import kayit, gun  # noqa: E402

PLUG = os.path.normpath(os.path.join(HERE, ".."))


def setUpModule():
    os.makedirs(TM._CFG, exist_ok=True)


def tearDownModule():
    shutil.rmtree(TM._ROOT, ignore_errors=True)


def temiz():
    shutil.rmtree(os.path.join(maliyet.CLAUDE, "maliyet"), ignore_errors=True)
    try:
        os.unlink(os.path.join(maliyet.CLAUDE, "pace-plan.json"))
    except OSError:
        pass


class Throttle(unittest.TestCase):
    def setUp(self):
        temiz()

    def test_due_only_after_the_interval(self):
        self.assertTrue(P.oto_gerekli({}, 1000, 2))                                   # never checked
        self.assertFalse(P.oto_gerekli({"son_kontrol": 1000}, 1000 + 2 * 3600 - 1, 2))
        self.assertTrue(P.oto_gerekli({"son_kontrol": 1000}, 1000 + 2 * 3600, 2))
        self.assertFalse(P.oto_gerekli({"son_kontrol": 1000}, 1000 + 23 * 3600, 24))

    def test_interval_default_and_config(self):
        self.assertEqual(P.oto_aralik_saat(), 2.0)
        for v, want in ((6, 6.0), (0.5, 0.5), (0, 2.0), (-1, 2.0), ("x", 2.0), (True, 2.0), (99999, 2.0)):
            with open(os.path.join(maliyet.CLAUDE, "pace-plan.json"), "w") as f:
                json.dump({"paylas_aralik_saat": v}, f)
            self.assertEqual(P.oto_aralik_saat(), want, v)

    def test_state_file_moves_the_marker_to_the_next_due_time(self):
        P.oto_yaz({"itilen_kota": 40}, 5000.0, 2)
        self.assertEqual(P.oto_durum_oku(), {"itilen_kota": 40, "son_kontrol": 5000.0})
        self.assertEqual(int(os.stat(P.oto_yol("paylas-sonraki")).st_mtime), 5000 + 7200)   # what statusline.sh stats


class Lock(unittest.TestCase):
    def setUp(self):
        temiz()

    def test_one_share_at_a_time_and_stale_lock_is_free(self):
        now = time.time()
        self.assertTrue(P.oto_kilit_al(now))
        self.assertFalse(P.oto_kilit_al(now + 60))                                    # running
        self.assertFalse(P.oto_kilit_al(now + 29 * 60))
        self.assertTrue(P.oto_kilit_al(now + 31 * 60))                                # older than 30 min: taken over
        P.oto_kilit_birak()
        self.assertTrue(P.oto_kilit_al(now + 32 * 60))
        P.oto_kilit_birak()
        self.assertFalse(os.path.exists(P.oto_yol("paylas.kilit")))

    def test_a_run_that_finds_the_lock_taken_does_nothing_and_keeps_it(self):
        self.assertTrue(P.oto_kilit_al(time.time()))
        with mock.patch.object(P, "kisi_adi", side_effect=AssertionError("must not run")):
            P.paylas(otomatik=True)
        self.assertTrue(os.path.exists(P.oto_yol("paylas.kilit")))                    # not released by the loser
        P.oto_kilit_birak()


class Karar(unittest.TestCase):
    def test_only_the_integer_weekly_percent_decides(self):
        self.assertEqual(P.oto_karar(None, {}), "atla")
        self.assertEqual(P.oto_karar(40, {}), "it")
        self.assertEqual(P.oto_karar(40, {"itilen_kota": 40}), "degisiklik yok")
        self.assertEqual(P.oto_karar(41, {"itilen_kota": 40}), "it")


@unittest.skipUnless(shutil.which("git"), "git not installed")
class Akis(unittest.TestCase):
    """bir_deneme(otomatik=True) and the whole _oto flow against a local bare remote."""

    def setUp(self):
        temiz()
        self.d = tempfile.mkdtemp(prefix="oto-", dir=TM._ROOT)
        self.uzak = os.path.join(self.d, "uzak.git")
        self.env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x",
                        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")
        self.sh("git", "init", "-q", "--bare", "-b", "main", self.uzak)
        tohum = os.path.join(self.d, "tohum")
        self.sh("git", "clone", "-q", self.uzak, tohum)
        yol = os.path.join(tohum, *P.KISILER_YOL.split("/"))
        os.makedirs(os.path.dirname(yol))
        with open(yol, "w") as f:
            json.dump({"gh-ali": "Ali"}, f)
        self.sh("git", "-C", tohum, "add", "-A")
        self.sh("git", "-C", tohum, "commit", "-q", "-m", "seed")
        self.sh("git", "-C", tohum, "push", "-q", "origin", "HEAD:main")
        self.tohum = tohum
        self.klon = os.path.join(self.d, "klon")
        for p in (mock.patch.dict(os.environ, dict(self.env, CLAUDE_SHARED_UZAK=self.uzak, CLAUDE_SHARED_KLON=self.klon)),
                  mock.patch.object(P, "GIT_ENV", dict(self.env, GIT_TERMINAL_PROMPT="0")),
                  mock.patch.object(P, "kisi_adi", return_value="Ali"),
                  mock.patch.object(P.time, "sleep")):
            p.start()
            self.addCleanup(p.stop)

    def sh(self, *a):
        r = subprocess.run(a, capture_output=True, text=True, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def kafa(self):
        return self.sh("git", "--git-dir", self.uzak, "rev-parse", "main")

    def kisi_dosyasi(self):
        self.sh("git", "-C", self.tohum, "pull", "-q", "origin", "main")
        return TM.rd(os.path.join(self.tohum, "raporlar", "kullanim", "ali.md"))

    def deneme(self, r):
        return P.bir_deneme(self.klon, self.uzak, r, r["tarih"], otomatik=True)

    def test_same_percent_writes_and_commits_nothing_even_when_other_figures_moved(self):
        self.assertEqual(self.deneme(kayit(gun(), kota=40, d7=100, commit=1)), "itildi")
        kafa = self.kafa()
        r = kayit(gun(), kota=40, d7=999, commit=9, hafta_usd=777)                    # $ and commits moved, % did not
        r["mod"], r["tahmin"] = "SIKI", 120
        self.assertEqual(self.deneme(r), "degisiklik yok")
        self.assertEqual(self.kafa(), kafa)

    def test_changed_percent_replaces_todays_row_in_place_and_pushes(self):
        self.deneme(kayit(gun(1), kota=30, d7=100))
        self.deneme(kayit(gun(), kota=40, d7=100))
        self.assertEqual(self.deneme(kayit(gun(), kota=47, d7=150, commit=2)), "itildi")
        rows = P.dosya_satirlari(self.kisi_dosyasi())
        self.assertEqual([(x["tarih"], x["kota"], x["d7"]) for x in rows], [(gun(1), 30, 100), (gun(), 47, 150)])   # one row per date
        self.assertEqual(len(P.satir_ayristir(P.satir_kur(kayit(gun(), kota=47)))["tarih"]), 10)
        self.assertEqual(sum(l.startswith("| %s |" % gun()) for l in self.kisi_dosyasi().splitlines()), 1)   # rows that BEGIN with today (the fixture's week-reset cell may equal today)

    def test_daily_job_still_never_replaces_a_row_of_the_same_day(self):
        self.deneme(kayit(gun(), kota=40))
        m = self.kisi_dosyasi()
        self.assertEqual(P.dosyaya_ekle(m, "Ali", P.satir_kur(kayit(gun(), kota=55)), gun()), m)

    def test_whole_flow_throttle_unchanged_changed_and_retry(self):
        t0 = 1_800_000_000.0
        durum = {"kota": 40}
        calls = []

        def bk(kisi, bugun, simdi=None):
            calls.append(1)
            return kayit(bugun, kota=durum["kota"])
        with mock.patch.object(P, "bugunku_kayit", side_effect=bk), \
                mock.patch.object(P, "kota_bilgi", side_effect=lambda s=None: (durum["kota"], t0 + 86400)):
            P.paylas(otomatik=True, simdi=t0)
            self.assertEqual(len(calls), 1)
            self.assertEqual(P.oto_durum_oku()["itilen_kota"], 40)
            kafa = self.kafa()
            P.paylas(otomatik=True, simdi=t0 + 3600)                                  # inside the 2 h interval: nothing at all
            self.assertEqual(len(calls), 1)
            P.paylas(otomatik=True, simdi=t0 + 3 * 3600)                              # due, % unchanged: no work, no commit
            self.assertEqual((len(calls), self.kafa()), (1, kafa))
            self.assertEqual(P.oto_durum_oku()["son_kontrol"], t0 + 3 * 3600)         # only the check time moved
            self.assertEqual(P.oto_durum_oku()["son_paylasim"], t0)
            durum["kota"] = 41
            real = P.bir_deneme
            n = {"i": 0}

            def flaky(*a, **k):
                n["i"] += 1
                if n["i"] < 3:
                    raise RuntimeError("push: rejected")                              # lost the race twice
                return real(*a, **k)
            with mock.patch.object(P, "bir_deneme", side_effect=flaky):
                P.paylas(otomatik=True, simdi=t0 + 6 * 3600)
            self.assertEqual(n["i"], 3)
            self.assertNotEqual(self.kafa(), kafa)
            self.assertEqual(P.oto_durum_oku()["itilen_kota"], 41)
        self.assertFalse(os.path.exists(P.oto_yol("paylas.kilit")))

    def test_failed_push_keeps_the_old_baseline_and_waits_for_the_next_interval(self):
        t0 = 1_800_000_000.0
        with mock.patch.object(P, "bugunku_kayit", return_value=kayit(gun(), kota=40)), \
                mock.patch.object(P, "kota_bilgi", return_value=(40, t0 + 86400)), \
                mock.patch.object(P, "bir_deneme", side_effect=RuntimeError("push: down")):
            P.paylas(otomatik=True, simdi=t0)
        d = P.oto_durum_oku()
        self.assertNotIn("itilen_kota", d)
        self.assertEqual(d["son_kontrol"], t0)


STUB = """#!/usr/bin/env python3
import sys
if "--otomatik" in sys.argv:
    open(sys.argv[0] + ".cagri", "a").write("x\\n")
"""


@unittest.skipUnless(shutil.which("bash") and shutil.which("jq"), "bash and jq required")
class KabukKapisi(unittest.TestCase):
    """statusline.sh starts the share detached only when due; renders inside the interval start nothing."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="oto-sh-")
        self.claude = os.path.join(self.home, ".claude")
        os.makedirs(os.path.join(self.claude, "maliyet"))
        os.makedirs(os.path.join(self.home, ".config", "claude-shared"))
        open(os.path.join(self.home, ".config", "claude-shared", "plan"), "w").write("100\n")
        self.plug = os.path.join(self.home, "plug")
        shutil.copytree(PLUG, self.plug, ignore=shutil.ignore_patterns("tests", "__pycache__"))
        self.stub = os.path.join(self.plug, "bin", "maliyet.py")
        open(self.stub, "w").write(STUB)
        self.cagri = self.stub + ".cagri"
        self.marker = os.path.join(self.claude, "maliyet", "paylas-sonraki")
        self.kilit = os.path.join(self.claude, "maliyet", "paylas.kilit")

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def render(self):
        now = int(time.time())
        data = json.dumps({"model": {"display_name": "X", "id": "x"}, "workspace": {"current_dir": "/"}, "rate_limits": {
            "five_hour": {"used_percentage": 10, "resets_at": now + 3 * 3600},
            "seven_day": {"used_percentage": 50, "resets_at": now + 3 * 86400}}})
        t0 = time.time()
        r = subprocess.run(["bash", os.path.join(self.plug, "statusline.sh")], input=data, text=True, capture_output=True, timeout=60,
                           env=dict(os.environ, HOME=self.home, KOTA_PAYLAS_OTO="1"))
        self.assertIn("hafta %50", r.stdout)
        time.sleep(0.6)                                                               # the detached stub is quick
        return time.time() - t0

    def cagri_say(self):
        return len(open(self.cagri).read().split()) if os.path.exists(self.cagri) else 0

    def test_gate(self):
        self.render()
        self.assertEqual(self.cagri_say(), 1)                                         # never shared: starts one
        future = time.time() + 7200                                                   # what the job writes after a check
        os.utime(self.marker, (future, future)) if os.path.exists(self.marker) else (open(self.marker, "w").close(), os.utime(self.marker, (future, future)))
        self.render()
        self.render()
        self.assertEqual(self.cagri_say(), 1)                                         # two renders inside the interval start nothing
        past = time.time() - 3 * 3600                                                 # forced back by more than 2 h
        os.utime(self.marker, (past, past))
        self.render()
        self.assertEqual(self.cagri_say(), 2)                                         # exactly one more start

    def test_running_share_and_stale_lock(self):
        open(self.kilit, "w").close()                                                 # fresh lock: a share is running
        self.render()
        self.assertEqual(self.cagri_say(), 0)
        old = time.time() - 31 * 60
        os.utime(self.kilit, (old, old))                                              # stale lock counts as free
        self.render()
        self.assertEqual(self.cagri_say(), 1)

    def test_not_joined_or_switched_off_starts_nothing(self):
        os.unlink(os.path.join(self.home, ".config", "claude-shared", "plan"))
        self.render()
        self.assertEqual(self.cagri_say(), 0)
        open(os.path.join(self.home, ".config", "claude-shared", "plan"), "w").write("100\n")
        open(os.path.join(self.claude, "maliyet", "kapali"), "w").close()
        self.render()
        self.assertEqual(self.cagri_say(), 0)


if __name__ == "__main__":
    unittest.main()
