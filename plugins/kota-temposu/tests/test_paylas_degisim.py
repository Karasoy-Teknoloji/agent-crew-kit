"""The 2-hourly timer run of the usage share: it commits and pushes only when the person's row or the ranking really changed
(timestamp-only drift never counts), says what moved in the commit message, regenerates SIRALAMA.md safely and never touches other
people's files; plus the Slack inbox state column. Stdlib only, synthetic data, no network (local bare remote), no desktop notification.

Run: python3 -I -m unittest discover -s plugins/kota-temposu/tests -p 'test_paylas_degisim.py'
"""
import builtins
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
import test_maliyet as TM  # noqa: E402  (sets the scratch HOME / CLAUDE_CONFIG_DIR and CLAUDE_SHARED_BILDIRIM=0 before the modules are imported)
import maliyet  # noqa: E402
import maliyet_paylas as P  # noqa: E402
from test_paylas import kayit, gun  # noqa: E402


def setUpModule():
    os.makedirs(TM._CFG, exist_ok=True)


def tearDownModule():
    shutil.rmtree(TM._ROOT, ignore_errors=True)


def temiz():
    shutil.rmtree(os.path.join(maliyet.CLAUDE, "maliyet"), ignore_errors=True)


def satir(tarih=None, **kw):
    """A record of the timer run: the fixture row plus the pace and Slack fields."""
    r = kayit(tarih or gun(), **kw.pop("fixture", {}))
    r.update({"mod": "NORMAL", "tahmin": 85, "bitis": P.YETER, "tahmin_zamani": "2026-11-04 10:00", "slack": "acik", "slack_son": "2026-11-04 10:00"})
    r.update(kw)
    return r


class Akis(unittest.TestCase):
    def setUp(self):
        temiz()
        self.d = tempfile.mkdtemp(prefix="degisim-", dir=TM._ROOT)
        self.uzak = os.path.join(self.d, "uzak.git")
        self.env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x",
                        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")
        self.sh("git", "init", "-q", "--bare", "-b", "main", self.uzak)
        self.tohum = os.path.join(self.d, "tohum")
        self.sh("git", "clone", "-q", self.uzak, self.tohum)
        yol = os.path.join(self.tohum, *P.KISILER_YOL.split("/"))
        os.makedirs(os.path.dirname(yol))
        with open(yol, "w") as f:
            json.dump({"gh-ali": "Ali", "gh-veli": "Veli"}, f)
        self.veli(satir(fixture=dict(kisi="Veli", d7=150, kota=30)))
        self.klon = os.path.join(self.d, "klon")
        for p in (mock.patch.dict(os.environ, dict(self.env, CLAUDE_SHARED_UZAK=self.uzak, CLAUDE_SHARED_KLON=self.klon)),
                  mock.patch.object(P, "GIT_ENV", dict(self.env, GIT_TERMINAL_PROMPT="0")),
                  mock.patch.object(P, "kisi_adi", return_value="Ali"),
                  mock.patch.object(P, "github_commit_say", return_value=None),
                  mock.patch.object(P.time, "sleep")):
            p.start()
            self.addCleanup(p.stop)

    def sh(self, *a):
        r = subprocess.run(a, capture_output=True, text=True, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def veli(self, r):
        """Seed Veli's file (another person) on the remote."""
        yol = os.path.join(self.tohum, "raporlar", "kullanim", "veli.md")
        os.makedirs(os.path.dirname(yol), exist_ok=True)
        with open(yol, "w", encoding="utf-8") as f:
            f.write(P.dosyaya_ekle("", "Veli", P.satir_kur(r), r["tarih"]))
        self.sh("git", "-C", self.tohum, "add", "-A")
        self.sh("git", "-C", self.tohum, "commit", "-q", "-m", "seed veli")
        self.sh("git", "-C", self.tohum, "push", "-q", "origin", "HEAD:main")

    def kafa(self):
        return self.sh("git", "--git-dir", self.uzak, "rev-parse", "main")

    def mesaj(self):
        return self.sh("git", "--git-dir", self.uzak, "log", "-1", "--format=%s", "main")

    def dosyalar(self):
        return self.sh("git", "--git-dir", self.uzak, "show", "--name-only", "--format=", "main").split()

    def calistir(self, r):
        return P.bir_deneme(self.klon, self.uzak, r, r["tarih"], degisim=True)

    def ali(self):
        self.sh("git", "-C", self.tohum, "pull", "-q", "origin", "main")
        return TM.rd(os.path.join(self.tohum, "raporlar", "kullanim", "ali.md"))

    # ------------------------------------------------------------ change-only
    def test_first_run_of_a_date_writes_row_and_ranking_and_names_the_person(self):
        r = satir(fixture=dict(d7=100, kota=40))
        self.assertEqual(self.calistir(r), "itildi")
        self.assertEqual(sorted(self.dosyalar()), ["raporlar/kullanim/SIRALAMA.md", "raporlar/kullanim/ali.md"])   # nobody else's file
        self.assertTrue(self.mesaj().startswith("chore(kullanim): Ali %s" % gun()), self.mesaj())
        self.assertIn("sıra", r["ozet"])

    def test_timestamp_and_rolling_window_drift_commit_nothing(self):
        self.calistir(satir(fixture=dict(d7=100, kota=40)))
        kafa = self.kafa()
        r = satir(fixture=dict(d7=100, kota=40), tahmin_zamani="2026-11-04 12:00", slack_son="2026-11-04 12:00",
                  bitis=P.YETER)
        r["d24"], r["d30"], r["hafta_usd"] = 77, 555, 999                       # rolling windows moved with the clock
        self.assertEqual(self.calistir(r), "degisiklik yok")
        self.assertEqual(self.kafa(), kafa)

    def test_run_out_hour_jitter_is_not_a_change_but_another_day_is(self):
        self.calistir(satir(fixture=dict(d7=100, kota=95), bitis="2026-11-06 14:00"))
        kafa = self.kafa()
        self.assertEqual(self.calistir(satir(fixture=dict(d7=100, kota=95), bitis="2026-11-06 17:40")), "degisiklik yok")
        self.assertEqual(self.kafa(), kafa)
        self.assertEqual(self.calistir(satir(fixture=dict(d7=100, kota=95), bitis="2026-11-07 09:00")), "itildi")

    def test_weekly_percent_change_replaces_todays_row_in_place(self):
        self.calistir(satir(fixture=dict(d7=100, kota=40)))
        r = satir(fixture=dict(d7=110, kota=41))
        self.assertEqual(self.calistir(r), "itildi")
        self.assertIn("kota %40→%41", self.mesaj())
        rows = P.dosya_satirlari(self.ali())
        self.assertEqual([(x["tarih"], x["kota"], x["d7"]) for x in rows], [(gun(), 41, 110)])        # one row per date, fresh figures

    def test_mode_change_commits_and_is_worth_telling(self):
        self.calistir(satir(fixture=dict(d7=100, kota=40), mod="NORMAL"))
        r = satir(fixture=dict(d7=100, kota=40), mod="SIKI")
        self.assertEqual(self.calistir(r), "itildi")
        self.assertIn("mod NORMAL→SIKI", self.mesaj())
        self.assertTrue(r["haber"])

    def test_slack_state_change_commits_but_is_not_a_notification(self):
        self.calistir(satir(fixture=dict(d7=100, kota=40), slack="acik"))
        r = satir(fixture=dict(d7=100, kota=40), slack="hata")
        self.assertEqual(self.calistir(r), "itildi")
        self.assertIn("slack acik→hata", self.mesaj())
        self.assertFalse(r["haber"])
        self.assertEqual(P.dosya_satirlari(self.ali())[-1]["slack"], "hata")

    def test_rank_change_alone_commits_and_is_worth_telling(self):
        self.calistir(satir(fixture=dict(d7=100, kota=40)))                       # Veli (150) is first, Ali second
        r = satir(fixture=dict(d7=300, kota=40))                                  # only the 7-day dollars moved, past Veli
        self.assertEqual(self.calistir(r), "itildi")
        self.assertIn("sıra 2→1", self.mesaj())
        self.assertTrue(r["haber"])
        md = self.sh("git", "--git-dir", self.uzak, "show", "main:raporlar/kullanim/SIRALAMA.md")
        self.assertRegex(md, r"\| 🥇 \| Ali \|")

    def test_dollar_move_that_keeps_the_ranking_commits_nothing(self):
        self.calistir(satir(fixture=dict(d7=100, kota=40)))
        kafa = self.kafa()
        self.assertEqual(self.calistir(satir(fixture=dict(d7=130, kota=40))), "degisiklik yok")
        self.assertEqual(self.kafa(), kafa)

    def test_a_new_day_always_writes_its_row(self):
        self.calistir(satir(gun(1), fixture=dict(d7=100, kota=40)))
        self.assertEqual(self.calistir(satir(gun(), fixture=dict(d7=100, kota=40))), "itildi")
        self.assertEqual([x["tarih"] for x in P.dosya_satirlari(self.ali())], [gun(1), gun()])

    def test_a_push_that_lost_the_race_is_redone_on_the_fresh_base_without_force(self):
        calls = {"n": 0}
        gercek = P.bir_deneme

        def yarisli(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                self.veli(satir(fixture=dict(kisi="Veli", d7=170, kota=31)))      # another machine pushed meanwhile
                raise RuntimeError("push: rejected")
            return gercek(*a, **k)
        with mock.patch.object(P, "bir_deneme", side_effect=yarisli):
            sonuc, hata = P._it(satir(fixture=dict(d7=100, kota=40)), gun(), degisim=True)
        self.assertEqual((sonuc, hata, calls["n"]), ("itildi", None, 2))
        md = self.sh("git", "--git-dir", self.uzak, "show", "main:raporlar/kullanim/SIRALAMA.md")
        self.assertIn("%31", md)                                                  # Veli's newer row is in the regenerated ranking
        self.assertEqual(self.sh("git", "--git-dir", self.uzak, "rev-list", "--count", "main"), "3")   # seed+veli, veli again, ali: linear history

    # ------------------------------------------------------------ the whole job
    def test_job_takes_the_lock_notifies_only_on_rank_or_mode_and_skips_when_busy(self):
        r = satir(fixture=dict(d7=300, kota=40))
        with mock.patch.object(P, "bugunku_kayit", return_value=r), mock.patch.object(P, "bildir") as bildir, \
                mock.patch("maliyet_cekirdek.bugun_str", return_value=gun()):
            self.assertTrue(P.oto_kilit_al(time.time()))                           # another share is running
            P._paylas(False, None)
            self.assertFalse(bildir.called)
            self.assertEqual(self.sh("git", "--git-dir", self.uzak, "rev-list", "--count", "main"), "1")   # nothing pushed
            P.oto_kilit_birak()
            P._paylas(False, None)
            bildir.assert_called_once()
            self.assertEqual(bildir.call_args[0][0], "Ali")
            self.assertFalse(os.path.exists(P.oto_yol("paylas.kilit")))
            self.assertEqual(P.oto_durum_oku()["itilen_kota"], 40)
            bildir.reset_mock()
            P._paylas(False, None)                                                 # nothing changed: no commit, no notification
            self.assertFalse(bildir.called)
            r2 = satir(fixture=dict(d7=300, kota=41))
            with mock.patch.object(P, "bugunku_kayit", return_value=r2):
                P._paylas(False, None)                                             # % moved, rank and mode did not
            self.assertFalse(bildir.called)

    def test_notification_is_off_in_tests_and_when_switched_off(self):
        with mock.patch.object(P, "komut", return_value=(0, "", "")) as k, mock.patch.dict(os.environ, {"CLAUDE_SHARED_BILDIRIM": "0"}):
            self.assertFalse(P.bildir("Ali", "x"))
            self.assertFalse(k.called)


class ChangeKey(unittest.TestCase):
    def test_key_ignores_what_drifts_with_the_clock(self):
        a, b = satir(), satir()
        b.update(tahmin_zamani="2030-01-01 00:00", slack_son="2030-01-01 00:00", d24=1, d30=2, hafta_usd=3, d7=999)
        self.assertEqual(P.onemli_anahtar(a), P.onemli_anahtar(b))
        for k, v in (("kota", 41), ("mod", "SIKI"), ("tahmin", 99), ("plan", 100), ("slack", "kapali"), ("bitis", "2031-01-01 00:00")):
            c = satir()
            c[k] = v
            self.assertNotEqual(P.onemli_anahtar(a), P.onemli_anahtar(c), k)


class SlackKutusu(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="gelen-", dir=TM._ROOT)
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        for p in (mock.patch.dict(os.environ, {"SLACK_GELEN_DATA": self.d}), mock.patch.object(sys, "platform", "linux")):
            p.start()
            self.addCleanup(p.stop)

    def durum(self, son="2026-11-04 02:35:57", hata=None):
        with open(os.path.join(self.d, "state.json"), "w") as f:
            json.dump({"ch": {"C1": "1.0"}, "status": {"last_run": son, "error": hata}}, f)

    def kom(self, enabled=(0, "enabled", ""), active=(0, "active", "")):
        def f(args, timeout=10):
            return enabled if "is-enabled" in args else active
        return mock.patch.object(P, "komut", side_effect=f)

    def test_running_failing_switched_off_and_not_installed(self):
        self.durum()
        with self.kom():
            self.assertEqual(P.slack_gelen_durumu(), ("acik", "2026-11-04 02:35"))
        self.durum(hata="token expired")
        with self.kom():
            self.assertEqual(P.slack_gelen_durumu()[0], "hata")
        self.durum()
        with self.kom(enabled=(1, "disabled", "")):
            self.assertEqual(P.slack_gelen_durumu(), ("kapali", "2026-11-04 02:35"))
        with self.kom(active=(3, "inactive", "")):
            self.assertEqual(P.slack_gelen_durumu()[0], "kapali")                  # enabled but stopped: it will not poll
        with self.kom(enabled=(1, "", "Failed to get unit file state for slack-gelen.timer: No such file or directory")):
            self.assertEqual(P.slack_gelen_durumu(), ("yok", None))

    def test_unreachable_user_manager_is_unknown_not_missing(self):
        with self.kom(enabled=(1, "", "Failed to connect to bus: No medium found")):
            self.assertEqual(P.slack_gelen_durumu(), (None, None))

    def test_no_systemctl_falls_back_to_the_last_poll(self):
        t = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 120))
        self.durum(son=t)
        with mock.patch.object(P, "komut", return_value=(None, "", "")):
            self.assertEqual(P.slack_gelen_durumu()[0], "acik")
        self.durum(son="2020-01-01 00:00:00")
        with mock.patch.object(P, "komut", return_value=(None, "", "")):
            self.assertEqual(P.slack_gelen_durumu()[0], "kapali")
        os.unlink(os.path.join(self.d, "state.json"))
        with mock.patch.object(P, "komut", return_value=(None, "", "")):
            self.assertEqual(P.slack_gelen_durumu(), ("yok", None))

    def test_windows_reads_the_last_poll_and_garbage_state_is_ignored(self):
        with mock.patch.object(sys, "platform", "win32"):
            self.assertEqual(P.slack_gelen_durumu(), (None, None))   # no poll on record: unknown
            self.durum(son=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 120)))
            self.assertEqual(P.slack_gelen_durumu()[0], "acik")
            self.durum(son=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 120)), hata="token expired")
            self.assertEqual(P.slack_gelen_durumu()[0], "hata")
            self.durum(son="2020-01-01 00:00:00")
            self.assertEqual(P.slack_gelen_durumu(), ("kapali", "2020-01-01 00:00"))
            os.unlink(os.path.join(self.d, "state.json"))
        with open(os.path.join(self.d, "state.json"), "w") as f:
            f.write("{not json")
        with self.kom():
            self.assertEqual(P.slack_gelen_durumu(), ("acik", None))

    def test_only_status_is_read_never_the_inbox_content(self):
        self.durum()
        with open(os.path.join(self.d, "slack-gelen.jsonl"), "w") as f:
            f.write(json.dumps({"text": "GIZLI-ICERIK", "sender": "x"}) + "\n")
        acilan = []
        gercek = builtins.open

        def izle(yol, *a, **k):
            acilan.append(str(yol))
            return gercek(yol, *a, **k)
        with self.kom(), mock.patch.object(builtins, "open", izle):
            durum = P.slack_gelen_durumu()
        self.assertEqual(durum, ("acik", "2026-11-04 02:35"))
        self.assertEqual([p for p in acilan if p.startswith(self.d)], [os.path.join(self.d, "state.json")])
        self.assertNotIn("GIZLI", P.satir_kur(dict(satir(), slack=durum[0], slack_son=durum[1])))


class SatirVeTablo(unittest.TestCase):
    def test_row_roundtrip_and_older_rows(self):
        r = satir(slack="hata", slack_son="2026-11-04 02:35")
        p = P.satir_ayristir(P.satir_kur(r))
        self.assertEqual((p["slack"], p["slack_son"]), ("hata", "2026-11-04 02:35"))
        self.assertEqual(len(P.TUM_SUTUN), 28)
        eski = "| " + " | ".join(P.satir_kur(r).strip("| ").split(" | ")[:26]) + " |"           # a row from before the Slack columns
        e = P.satir_ayristir(eski)
        self.assertEqual((e["slack"], e["slack_son"], e["mod"]), (None, None, "NORMAL"))
        self.assertIsNone(P.satir_ayristir(P.satir_kur(r).replace("| hata |", "| <script> |"))["slack"])    # only the known words survive

    def test_old_header_grows_the_columns_and_the_ranking_shows_the_cell(self):
        eski_baslik = "| %s |\n|%s\n" % (" | ".join(P.ARA3_SUTUN), "---|" * len(P.ARA3_SUTUN))
        eski_satir = "| " + " | ".join(P.satir_kur(satir(gun(1))).strip("| ").split(" | ")[:26]) + " |"
        m = P.dosyaya_ekle("# x\n\n" + eski_baslik + eski_satir + "\n", "Ali", P.satir_kur(satir(gun())), gun())
        self.assertIn("| Slack | Slack son |", m)
        veri = {"Ali": P.dosya_satirlari(m), "Veli": [satir(gun(), fixture=dict(kisi="Veli", d7=10), slack="kapali", slack_son=None),
                                                       ][:1], "Cem": [satir(gun(), fixture=dict(kisi="Cem", d7=5), slack=None, slack_son=None)]}
        veri["Ali"] = [dict(r, slack="acik", slack_son="2026-11-04 02:35") if r["tarih"] == gun() else r for r in veri["Ali"]]
        md = P.siralama_metni(veri, gun())
        self.assertIn("| Mod | Slack gelen | Son güncelleme |", md)
        self.assertRegex(md, r"\| Ali \|[^\n]*\| açık · 04\.11 02:35 \|")
        self.assertRegex(md, r"\| Veli \|[^\n]*\| kapalı \|")
        self.assertRegex(md, r"\| Cem \|[^\n]*\| — \|")
        self.assertEqual((P.slack_metni("yok", None), P.slack_metni("hata", "2026-11-04 02:35"), P.slack_metni("acik", None)),
                         ("kurulu değil", "hata · 04.11 02:35", "açık"))


if __name__ == "__main__":
    unittest.main()
