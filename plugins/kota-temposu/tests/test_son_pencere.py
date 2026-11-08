"""Tests for son_pencere (the last-window wrapper): gate, quota readings, journal, shared log rows, the choice of the live session, and full
runs (assignment to a live session through a fake relay `claude`, and the tmux fallback through a fake `tmux`).

Run:  python3 -I -m unittest discover -s plugins/kota-temposu/tests     (from the repo root)

Stdlib only, no network, no real claude or tmux: the relay is a small fake script (SON_PENCERE_CLAUDE), tmux another (SON_PENCERE_TMUX),
live sessions are fixture files in <config dir>/sessions; the shared repo a local bare repository (CLAUDE_SHARED_UZAK), the picks a JSON file (SON_PENCERE_SECIM). Every process runs under a scratch HOME.
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
SP = os.path.join(PLUG, "bin", "son_pencere.py")
sys.path.insert(0, os.path.join(PLUG, "bin"))
import son_pencere as S  # noqa: E402

GERCEK_SEKME = S.SekmeOturum      # the real Windows tab class (the scene replaces it with a fake by default)

H = 3600
NOW = 1_800_000_000


def rd(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def jrd(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def jwr(p, obj):
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f)


def git(*args, cwd=None):
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env).stdout


class Gate(unittest.TestCase):
    RESET = NOW + 3 * H            # reset in 3 h, window of 5 h: we are inside

    def kota(self, used=93, reset=None, **kw):
        return {"week_used": used, "week_resets_at": reset or self.RESET, "five_hour_used": None, "five_hour_resets_at": None, **kw}

    def test_inside_the_window_with_quota_left_starts(self):
        self.assertEqual(S.karar(NOW, self.kota(), 5, False, None)[:2], ("baslat", "pencerede"))

    def test_before_the_window_after_the_reset_and_unknown_reset_wait(self):
        self.assertEqual(S.karar(NOW, self.kota(reset=NOW + 6 * H), 5, False, None)[1], "pencere-oncesi")
        self.assertEqual(S.karar(NOW, self.kota(reset=NOW - 1), 5, False, None)[1], "reset-sonrasi")
        self.assertEqual(S.karar(NOW, {"week_resets_at": None}, 5, False, None)[1], "reset-bilinmiyor")
        self.assertEqual(S.karar(NOW, self.kota(), 0, False, None)[1], "pencere-yok")     # target hours 0 = no window

    def test_window_opens_exactly_at_reset_minus_target(self):
        self.assertEqual(S.karar(NOW, self.kota(reset=NOW + 5 * H), 5, False, None)[0], "baslat")
        self.assertEqual(S.karar(NOW - 1, self.kota(reset=NOW + 5 * H), 5, False, None)[1], "pencere-oncesi")

    def test_used_up_weekly_limit_and_full_five_hour_window_wait(self):
        self.assertEqual(S.karar(NOW, self.kota(used=100), 5, False, None)[1], "kota-bitti")
        self.assertEqual(S.karar(NOW, self.kota(five_hour_used=100, five_hour_resets_at=NOW + H), 5, False, None)[1], "bes-saat-dolu")
        self.assertEqual(S.karar(NOW, self.kota(used=None), 5, False, None)[0], "baslat")   # unknown use: the stream will tell

    def test_one_coordinator_and_at_most_one_start_per_15_minutes(self):
        self.assertEqual(S.karar(NOW, self.kota(), 5, True, None)[1], "calisiyor")
        self.assertEqual(S.karar(NOW, self.kota(), 5, False, NOW - 14 * 60)[1], "tekrar-araligi")
        self.assertEqual(S.karar(NOW, self.kota(), 5, False, NOW - 15 * 60)[0], "baslat")

    def test_no_restart_in_the_last_ten_minutes_and_backoff_after_quick_failures(self):
        self.assertEqual(S.karar(NOW, self.kota(reset=NOW + 9 * 60), 5, False, NOW - 3600)[1], "reset-yakin")
        self.assertEqual(S.karar(NOW, self.kota(), 5, False, NOW - 20 * 60, hizli_hata=3)[1], "hata-beklemesi")
        self.assertEqual(S.karar(NOW, self.kota(), 5, False, NOW - 3601, hizli_hata=3)[0], "baslat")


class Kota(unittest.TestCase):
    def test_newer_source_wins_and_use_is_trusted_only_for_the_chosen_reset(self):
        pace = {"week_used": 90, "week_resets_at": NOW + H, "updated": NOW - 5 * H}
        durum = {"week_used": 97.5, "week_resets_at": NOW + H, "updated": NOW - 60}
        k = S.kota_durumu(pace, durum, NOW)
        self.assertEqual((k["week_used"], k["kaynak"]), (97.5, "durum"))

    def test_stale_reset_is_rolled_forward_by_whole_weeks_with_unknown_use(self):
        k = S.kota_durumu({"week_used": 50, "week_resets_at": NOW - 8 * 86400, "updated": NOW - 9 * 86400}, {}, NOW)
        self.assertIsNone(k["week_used"])
        self.assertEqual(k["kaynak"], "tahmin")
        self.assertTrue(NOW < k["week_resets_at"] <= NOW + 7 * 86400)
        self.assertEqual((k["week_resets_at"] - (NOW - 8 * 86400)) % (7 * 86400), 0)

    def test_nothing_known(self):
        self.assertIsNone(S.kota_durumu({}, {}, NOW)["week_resets_at"])

    def test_five_hour_reading_expires_with_its_reset(self):
        k = S.kota_durumu({}, {"week_resets_at": NOW + H, "updated": NOW, "five_hour_used": 100, "five_hour_resets_at": NOW - 1}, NOW)
        self.assertIsNone(k["five_hour_used"])


class Satirlar(unittest.TestCase):
    def kayit(self, bas, **kw):
        k = {"bas": bas, "bit": bas + 600, "hafta_bas": 93, "hafta_son": 100, "neden": "limit", "maliyet": 41.7,
             "biten": [("https://github.com/example-org/product-a/issues/61", "x | y"), ("https://example.com/not-github", "z")],
             "devam": [("https://github.com/example-org/product-c/issues/7", "wip")]}
        k.update(kw)
        return k

    def test_row_roundtrip_and_links_only_for_issue_or_pr_urls(self):
        r = S.satir_ayristir(S.satir_kur(self.kayit(NOW)))
        self.assertEqual((r["Week %"], r["Reason"], r["Done"], r["WIP"], r["API $"]), ("93→100", "limit", "2", "1", "42"))
        self.assertEqual(r["Items"], "[product-a#61](https://github.com/example-org/product-a/issues/61)")

    def test_running_row_is_replaced_by_the_final_one_and_other_rows_stay(self):
        a = S.kisi_dosyasi("Ali", "", {"bas": NOW, "hafta_bas": 93, "neden": None, "biten": [], "devam": []})
        self.assertIn("running", a)
        b = S.kisi_dosyasi("Ali", a, self.kayit(NOW + 86400))
        c = S.kisi_dosyasi("Ali", b, self.kayit(NOW, neden="reset"))
        rows = S.dosya_satirlari(c)
        self.assertEqual([r["Reason"] for r in rows], ["reset", "limit"])
        self.assertEqual(len(rows), 2)

    def test_pipe_in_text_never_breaks_the_table(self):
        self.assertEqual(S.hucre("a | b\nc"), "a / b c")

    def test_noise_is_not_a_row(self):
        for s in ("", "| Start | End |", "|---|---|", "text", "| 2026-13-45 | x |"):
            self.assertIsNone(S.satir_ayristir(s))

    def test_all_people_table_is_newest_first(self):
        veri = {"Ali": S.dosya_satirlari(S.kisi_dosyasi("Ali", "", self.kayit(NOW))),
                "Veli": S.dosya_satirlari(S.kisi_dosyasi("Veli", "", self.kayit(NOW + 86400)))}
        m = S.kayitlar_metni(veri)
        self.assertLess(m.index("Veli"), m.index("Ali"))


class Secim(unittest.TestCase):
    def test_board_picks_skip_foreign_assignees_and_prod_approval_and_sort_by_priority(self):
        o = [{"repo": "O/A", "number": 5, "url": "u5", "title": "t", "state": "open", "labels": [], "assignees": [], "rank": 2},
             {"repo": "O/A", "number": 6, "url": "u6", "title": "t", "state": "open", "labels": ["prod-onay-bekliyor"], "assignees": [], "rank": 0},
             {"repo": "O/B", "number": 1, "url": "u1", "title": "t", "state": "open", "labels": [], "assignees": ["other"], "rank": 0},
             {"repo": "O/B", "number": 2, "url": "u2", "title": "t", "state": "open", "labels": [], "assignees": ["Me"], "rank": 1},
             {"repo": "O/B", "number": 3, "url": "u3", "title": "t", "state": "closed", "labels": [], "assignees": [], "rank": 0},
             {"repo": "O/C", "number": 9, "url": "u9", "title": "t", "state": "open", "labels": [], "assignees": [], "rank": 99},
             {"repo": "O/C", "number": 4, "url": "u4", "title": "t", "state": "open", "labels": [], "assignees": [], "rank": 1}]
        b = S.sec_b("me", o, dislanan={"u4"})
        self.assertEqual([i["number"] for i in b], [2, 5, 9])
        self.assertEqual(b[0]["repo"], "B")

    def test_journal_lines_and_short_refs(self):
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "j.md")
            S.jurnal_ekle(p, "## old\n- [10:00] DONE https://github.com/O/R/issues/1 | old\n")
            ofset = os.path.getsize(p)
            S.jurnal_ekle(p, "- [11:00] DONE https://github.com/O/R/issues/2 | fixed | with pipe\n- [11:05] WIP https://github.com/O/R/pull/3 | branch x\n"
                             "- [11:06] SKIP https://github.com/O/R/issues/4 | needs decision\n- [11:07] NOTE hand-off text\nrandom line\n")
            ogeler = S.jurnal_ogeleri(p, ofset)
            self.assertEqual([(k, S.kisa_ref(r)) for k, r, _ in ogeler], [("DONE", "R#2"), ("WIP", "R#3"), ("SKIP", "R#4"), ("NOTE", None)])
            self.assertEqual(ogeler[0][2], "fixed | with pipe")
        finally:
            shutil.rmtree(d, ignore_errors=True)




def oturum_json(pid, ad, sid, cwd, basladi, durum="idle", giris="cli"):
    return {"pid": pid, "sessionId": sid, "cwd": cwd, "startedAt": basladi * 1000, "kind": "interactive", "entrypoint": giris, "name": ad, "status": durum}


def asistan(model, mid="m1"):
    return json.dumps({"type": "assistant", "message": {"id": mid, "model": model, "content": []}}) + "\n"


def limit_satiri(t, metin="You've hit your session limit · resets 6:30pm (Europe/Istanbul)", uuid="dur-1"):
    """The synthetic entry Claude Code writes when a usage limit stops a turn (fields as in a real transcript of 16.10.2026)."""
    return json.dumps({"type": "assistant", "uuid": uuid, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t)),
                       "error": "rate_limit", "isApiErrorMessage": True, "apiErrorStatus": 429,
                       "message": {"id": "x", "model": "<synthetic>", "content": [{"type": "text", "text": metin}]}}) + "\n"


class Oturumlar(unittest.TestCase):
    """The choice of the live session: registry + transcript, in a scratch config dir (S.CLAUDE patched)."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="sp-oturum-")
        self.eski = (S.CLAUDE, S.DIZIN)
        S.CLAUDE, S.DIZIN = os.path.join(self.root, ".claude"), os.path.join(self.root, ".claude", "son-pencere")
        os.makedirs(os.path.join(S.CLAUDE, "sessions"))
        self.canli = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])   # a live pid to register
        self.n = 0

    def tearDown(self):
        self.canli.kill()
        self.canli.wait()
        S.CLAUDE, S.DIZIN = self.eski
        shutil.rmtree(self.root, ignore_errors=True)

    def ekle(self, ad, model, basladi, pid=None, giris="cli", durum="idle", cwd="/home/u/work"):
        self.n += 1
        sid = "00000000-0000-0000-0000-%012d" % self.n
        pid = pid or self.canli.pid
        jwr(os.path.join(S.CLAUDE, "sessions", "%d-%d.json" % (pid, self.n)), oturum_json(pid, ad, sid, cwd, basladi, durum, giris))
        if model:
            yol = S.transkript_yolu(cwd, sid)
            os.makedirs(os.path.dirname(yol), exist_ok=True)
            with open(yol, "w", encoding="utf-8") as f:
                f.write("garbage line\n" + asistan("claude-haiku-4-5", "x") + asistan(model, "y"))   # the LAST assistant entry decides
        return sid

    def sirala(self, **kw):
        aday, elenen = S.adaylari_sirala(S.oturumlari_oku(), **kw)
        return [o["ad"] for o in aday], {o["ad"]: n for o, n in elenen}

    def test_model_scores_and_versions(self):
        self.assertEqual(S.model_puani("claude-opus-5-5"), (58, (5, 5)))
        self.assertEqual(S.model_puani("claude-opus-4-8"), (58, (4, 8)))
        self.assertEqual(S.model_puani("claude-sonnet-5-5[1m]"), (54, (5, 5)))
        self.assertEqual(S.model_puani("claude-haiku-4-5-20251001"), (40, (4, 5)))
        self.assertEqual(S.model_puani("claude-fable-5-1")[0], 49)
        self.assertEqual(S.model_puani(None), (-1, ()))

    def test_transcript_path_and_last_model(self):
        self.assertEqual(S.transkript_yolu("/home/u/a.b", "ID"), os.path.join(S.CLAUDE, "projects", "-home-u-a-b", "ID.jsonl"))
        self.ekle("A", "claude-sonnet-5-5", 100)
        self.assertEqual(S.oturumlari_oku()[0]["model"], "claude-sonnet-5-5")

    def test_best_model_first_then_newer_version_then_oldest(self):
        self.ekle("old-sonnet", "claude-sonnet-5-5", 10)
        self.ekle("new-opus", "claude-opus-5-5", 300)
        self.ekle("older-opus-version", "claude-opus-4-8", 5)
        self.ekle("old-opus", "claude-opus-5-5", 200)
        self.ekle("fable", "claude-fable-5-1", 1)
        self.ekle("no-transcript", None, 1)
        adaylar, _ = self.sirala()
        self.assertEqual(adaylar, ["old-opus", "new-opus", "older-opus-version", "old-sonnet", "fable", "no-transcript"])

    def test_headless_son_pencere_excluded_dead_and_shared_names_are_out(self):
        self.ekle("terminal", "claude-opus-5-5", 100)
        self.ekle("headless", "claude-opus-5-5", 1, giris="sdk-cli")
        self.ekle("son-pencere-emre", "claude-opus-5-5", 1)
        self.ekle("son-pencere-test", "claude-opus-5-5", 1)
        self.ekle("dup", "claude-opus-5-5", 1)
        self.ekle("dup", "claude-opus-5-5", 2)
        self.ekle("", "claude-opus-5-5", 1)
        olu = subprocess.Popen([sys.executable, "-c", "pass"])
        olu.wait()
        self.ekle("dead", "claude-opus-5-5", 1, pid=olu.pid)
        adaylar, elenen = self.sirala()
        self.assertEqual(adaylar, ["terminal"])
        self.assertIn("sdk-cli", elenen["headless"])
        self.assertIn("own session", elenen["son-pencere-emre"])
        self.assertIn("shared", elenen["dup"])
        self.assertNotIn("dead", elenen)                        # a dead process is not a session at all

    def test_a_newer_model_version_ranks_first_even_when_shorter_ids_compare(self):
        self.ekle("opus-5", "claude-opus-5", 1)
        self.ekle("opus-5-5", "claude-opus-5-5", 2)
        self.ekle("opus-4-8", "claude-opus-4-8", 0)
        self.assertEqual(self.sirala()[0], ["opus-5-5", "opus-5", "opus-4-8"])

    @unittest.skipUnless(os.path.exists("/proc/self/stat"), "procStart is the Linux /proc start time")
    def test_a_leftover_entry_with_a_reused_pid_does_not_shadow_the_live_session(self):
        self.ekle("emre-07", "claude-opus-5-5", 1)                       # the live session (no procStart: not compared)
        sid = "55555555-5555-5555-5555-555555555555"
        d = oturum_json(self.canli.pid, "emre-07", sid, "/home/u/work", 0)
        d["procStart"] = "1"                                               # written by an older process with the same pid number
        jwr(os.path.join(S.CLAUDE, "sessions", "leftover.json"), d)
        adaylar, elenen = self.sirala()
        self.assertEqual(adaylar, ["emre-07"])
        self.assertEqual(elenen, {})
        d["procStart"] = S.surec_baslangici(self.canli.pid)                # the same process: valid
        self.assertTrue(S.kayit_gecerli(d))

    def test_a_lock_is_live_for_the_whole_longest_window(self):
        os.makedirs(S.DIZIN, exist_ok=True)
        eski = S.KILIT
        S.KILIT = os.path.join(S.DIZIN, "koordinator.kilit")
        try:
            jwr(S.KILIT, {"pid": self.canli.pid, "t": NOW - 50 * H})       # a window run, 50 h in (the cap is 72 h by default, 96 h at most: stale only after 97 h)
            self.assertTrue(S.kilit_canli(NOW))
            jwr(S.KILIT, {"pid": self.canli.pid, "t": NOW - 100 * H})
            self.assertFalse(S.kilit_canli(NOW))
        finally:
            S.KILIT = eski

    def test_user_exclusions_by_pattern_and_the_test_hook(self):
        self.ekle("emre-07", "claude-opus-5-5", 1)
        self.ekle("worker", "claude-opus-5-5", 2)
        self.assertEqual(self.sirala(haric=["emre-*"])[0], ["worker"])
        os.makedirs(S.DIZIN)
        with open(os.path.join(S.DIZIN, "haric.txt"), "w") as f:
            f.write("# the coordinator\nemre-07\n")
        self.assertEqual(S.haric_desenleri(), ["emre-07"])
        self.ekle("son-pencere-test-x", "claude-opus-5-5", 3)
        self.assertEqual(self.sirala(hedef="son-pencere-test-x")[0], ["son-pencere-test-x"])


SAHTE_ROLE = r'''
import json, os, re, subprocess, sys, time
girdi = sys.stdin.read()
m = re.search(r"<ileti>\n(.*)\n</ileti>", girdi, re.S)
ad = re.search(r"whose name is exactly '([^']*)'", girdi).group(1)
isaret = os.environ["FAKE_MARK"]
onceki = sum(1 for _ in open(isaret)) if os.path.exists(isaret) else 0
open(isaret, "a").write(json.dumps({"to": ad, "text": m.group(1)}) + "\n")
sira = [x for x in os.environ.get("FAKE_SIRA", "").split(",") if x]     # one mode per relay call; the last one repeats
mod = sira[min(onceki, len(sira) - 1)] if sira else os.environ.get("FAKE_MODE", "ok")
yol = os.environ.get("FAKE_TRANSCRIPT")
satir = json.dumps({"type": "user", "message": "<cross-session-message from=x>" + m.group(1) + "</cross-session-message>"}) + "\n"
satir += json.dumps({"type": "assistant", "message": {"id": "r%d" % os.getpid(), "model": "claude-opus-5-5"}}) + "\n"
hedef_tamam = yol and ad == os.environ.get("FAKE_AD", ad)
if mod == "zaman":                       # the relay hangs past its timeout
    time.sleep(60)
elif mod == "bos":                       # exits cleanly without an answer word
    print("I could not decide.")
elif mod == "hata":                      # an error, no answer word
    print("API Error: 500", file=sys.stderr)
    sys.exit(1)
elif mod in ("ok", "gec", "isaretsiz") and hedef_tamam:
    if mod == "ok":
        with open(yol, "a") as f:
            f.write(satir)
    elif mod == "gec":                   # a busy target: the message shows up only at the end of its turn
        subprocess.Popen([sys.executable, "-c", "import sys, time; time.sleep(float(sys.argv[1])); open(sys.argv[2], 'a').write(sys.argv[3])",
                          os.environ.get("FAKE_GECIKME", "3"), yol, satir], start_new_session=True)
    print("**TESLIM**" if os.environ.get("FAKE_MARKDOWN") else "TESLIM")
else:
    print("YOK")
'''

SAHTE_TMUX = r'''
import json, os, sys
a = sys.argv[1:]
soket = None
if a[:1] == ["-L"]:
    soket, a = a[1], a[2:]
durum = os.environ["FAKE_TMUX_STATE"]
os.makedirs(durum, exist_ok=True)
open(os.path.join(durum, "log"), "a").write(json.dumps({"L": soket, "a": a}) + "\n")
def ad():
    return a[a.index("-t") + 1].lstrip("=").rstrip(":") if "-t" in a else a[a.index("-s") + 1]
def dosya(n):
    return os.path.join(durum, n)
def kaydet():
    # the started claude registers itself (FAKE_TMUX_KAYIT = "<sessions dir>:<live pid>"), unless FAKE_TMUX_KAYITSIZ=1
    kayit = os.environ.get("FAKE_TMUX_KAYIT")
    if not kayit or os.environ.get("FAKE_TMUX_KAYITSIZ") == "1":
        return
    d, pid = kayit.rsplit(":", 1)
    k = json.load(open(dosya("oturum")))
    json.dump({"pid": int(pid), "sessionId": k["sid"], "cwd": k["cwd"], "startedAt": 0, "kind": "interactive", "entrypoint": "cli",
               "name": k["ad"], "status": "busy"}, open(os.path.join(d, "%s.json" % pid), "w"))
ekran = os.environ.get("FAKE_TMUX_EKRAN")       # a dialog on screen (capture-pane); the trust dialog moves with Down, closes with Enter
if a[0] == "new-session":
    open(dosya("s-" + ad()), "w").write(a[-1])
    k = a[-1].split()
    json.dump({"ad": ad(), "cwd": a[a.index("-c") + 1], "sid": k[k.index("--session-id") + 1]}, open(dosya("oturum"), "w"))
    if not ekran:
        kaydet()
elif a[0] == "has-session":
    sys.exit(0 if os.path.exists(dosya("s-" + ad())) else 1)
elif a[0] == "kill-session":
    try: os.remove(dosya("s-" + ad()))
    except OSError: pass
    kayit = os.environ.get("FAKE_TMUX_KAYIT")
    if kayit:
        try: os.remove(os.path.join(kayit.rsplit(":", 1)[0], "%s.json" % kayit.rsplit(":", 1)[1]))
        except OSError: pass
elif a[0] == "send-keys" and ekran and not os.path.exists(dosya("kapandi")):
    if a[-1] == "Down":
        open(dosya("asagi"), "w").close()
    elif a[-1] == "Enter" and os.path.exists(dosya("asagi")) and "I trust this folder" in open(ekran, encoding="utf-8").read():
        open(dosya("kapandi"), "w").close()
        kaydet()
elif a[0] == "capture-pane" and ekran and not os.path.exists(dosya("kapandi")):
    metin = open(ekran, encoding="utf-8").read()
    if os.path.exists(dosya("asagi")):
        metin = metin.replace(" ❯ No, exit", "   No, exit").replace("   Yes, I trust", " ❯ Yes, I trust")
    sys.stdout.write(metin)
'''


def tmux_cagrilari(durum):
    # [args] of every fake tmux call, without the -L socket (asserted separately)
    return [json.loads(s)["a"] for s in rd(os.path.join(durum, "log")).splitlines()]


@unittest.skipUnless(shutil.which("git") and os.name != "nt", "needs git and a POSIX shell (the fakes are executable scripts)")
class Calisma(unittest.TestCase):
    """son_pencere.py tetik / durum end to end under a scratch HOME."""

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="sp-")
        cls.seed = os.path.join(cls.root, "seed.git")
        git("init", "-q", "--bare", "-b", "main", cls.seed)
        tohum = os.path.join(cls.root, "tohum")
        git("clone", "-q", cls.seed, tohum)
        os.makedirs(os.path.join(tohum, "plugins", "kota-temposu"))
        shutil.copy(os.path.join(PLUG, "tests", "fixtures", "kisiler-ornek.json"), os.path.join(tohum, "plugins", "kota-temposu", "kisiler.json"))
        git("add", "-A", cwd=tohum)
        git("commit", "-q", "-m", "seed", cwd=tohum)
        git("push", "-q", "origin", "HEAD:main", cwd=tohum)
        cls.fake = os.path.join(cls.root, "fake_claude.py")
        cls.tmux = os.path.join(cls.root, "fake_tmux.py")
        for yol, kod in ((cls.fake, SAHTE_ROLE), (cls.tmux, SAHTE_TMUX)):
            with open(yol, "w") as f:
                f.write("#!%s\n%s" % (sys.executable, kod))
            os.chmod(yol, 0o755)
        cls.secim = os.path.join(cls.root, "secim.json")
        with open(cls.secim, "w") as f:
            json.dump({"login": "tester", "a": [], "hatalar": [], "c": {"kirmizi_ci": [], "dependabot": []},
                       "b": [{"repo": "product-a", "number": 61, "title": "Security list", "url": "https://github.com/example-org/product-a/issues/61",
                              "state": "open", "labels": [], "assignees": [], "priority": "urgent", "rank": 0}]}, f)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="home-", dir=self.root)
        self.claude = os.path.join(self.home, ".claude")
        os.makedirs(os.path.join(self.claude, "sessions"))
        self.mark = os.path.join(self.home, "mark")
        self.tstate = os.path.join(self.home, "tmux")
        self.klon = os.path.join(self.home, "klon")
        self.uzak = os.path.join(self.home, "uzak.git")                # a fresh copy of the seeded shared repo for every test
        git("clone", "-q", "--bare", self.seed, self.uzak)
        self.canli = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
        self.addCleanup(lambda: (self.canli.kill(), self.canli.wait()))
        self.canli2 = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])   # the pid the fallback claude registers with
        self.addCleanup(lambda: (self.canli2.kill(), self.canli2.wait()))

    def pace(self, reset_in, used=93, **kw):
        d = {"week_used": used, "week_resets_at": int(time.time() + reset_in), "updated": int(time.time()), "target_hours": 5}
        d.update(kw)
        jwr(os.path.join(self.claude, "pace.json"), d)

    def oturum(self, ad="Alpha", model="claude-opus-5-5", durum="idle"):
        """A live interactive session; returns its transcript path."""
        sid, cwd = "11111111-1111-1111-1111-111111111111", "/home/u/w"
        jwr(os.path.join(self.claude, "sessions", "%d.json" % self.canli.pid), oturum_json(self.canli.pid, ad, sid, cwd, int(time.time()) - 3600, durum))
        yol = os.path.join(self.claude, "projects", "-home-u-w", sid + ".jsonl")
        os.makedirs(os.path.dirname(yol), exist_ok=True)
        with open(yol, "w") as f:
            f.write(asistan(model))
        return yol

    def run_sp(self, *args, mode="ok", **extra):
        return subprocess.run([sys.executable, "-I", SP, *args], capture_output=True, text=True, encoding="utf-8", env=self.ortam(mode, **extra), timeout=120)

    def ortam(self, mode="ok", **extra):
        env = {k: v for k, v in os.environ.items() if k not in ("PACE_HEDEF_SAAT", "CLAUDE_CONFIG_DIR", "XDG_CONFIG_HOME", "SON_PENCERE_DIZIN", "SON_PENCERE_HARIC",
                                                                 "SON_PENCERE", "SON_PENCERE_GH_LOGIN")}
        env.update(HOME=self.home, USERPROFILE=self.home, CLAUDE_CONFIG_DIR=self.claude, SON_PENCERE_CLAUDE=self.fake, SON_PENCERE_TMUX=self.tmux,
                   SON_PENCERE_SECIM=self.secim, SON_PENCERE_POLL="0.2", SON_PENCERE_ACK_SN="5", SON_PENCERE_STOP_SN="1", SON_PENCERE_KAYIT_SN="5",
                   FAKE_TMUX_KAYIT="%s:%d" % (os.path.join(self.claude, "sessions"), self.canli2.pid), FAKE_MODE=mode, FAKE_MARK=self.mark,
                   FAKE_TMUX_STATE=self.tstate, CLAUDE_SHARED_UZAK=self.uzak, CLAUDE_SHARED_SON_PENCERE_KLON=self.klon, CLAUDE_SHARED_KISI="Emre",
                   CLAUDE_SHARED_KOK=self.home, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")
        env.update(extra)
        return env

    def sent(self):
        if not os.path.exists(self.mark):
            return []
        with open(self.mark, encoding="utf-8") as f:
            return [json.loads(s) for s in f]

    def brief(self):
        d = os.path.join(self.claude, "son-pencere", "runs")
        return rd(os.path.join(d, [f for f in os.listdir(d) if f.endswith("-brief.md")][0]))

    def shared(self):
        v = os.path.join(self.root, "verify-" + os.path.basename(self.home))
        git("clone", "-q", self.uzak, v)
        try:
            p = os.path.join(v, "raporlar", "son-pencere", "emre.md")
            return rd(p) if os.path.exists(p) else ""
        finally:
            shutil.rmtree(v, ignore_errors=True)

    def test_the_assignment_goes_to_the_best_live_session_and_the_reset_message_ends_the_run(self):
        yol = self.oturum("Alpha")
        self.pace(5)                                                   # reset in 5 s
        t = time.time()
        r = self.run_sp("tetik", PACE_HEDEF_SAAT="5", FAKE_TRANSCRIPT=yol)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertLess(time.time() - t, 60)
        gonderilen = self.sent()
        self.assertEqual([g["to"] for g in gonderilen], ["Alpha", "Alpha"])
        self.assertIn("son-pencere on", gonderilen[0]["text"])        # who sends it
        self.assertIn("-brief.md", gonderilen[0]["text"])             # the brief is a file
        self.assertIn("#ai-status", gonderilen[0]["text"])     # the Slack rule travels with the message
        self.assertIn("Reset reached", gonderilen[1]["text"])         # the cutoff message
        b = self.brief()
        self.assertIn("product-a/issues/61", b)                         # the pick is in the brief
        self.assertNotIn("{{", b)                                      # every placeholder was filled
        self.assertIn("EXCLUDED", b)
        self.assertIn("başladı", b)
        self.assertIn("go idle (do not exit", b)                       # an assigned session is the owner's: never told to exit
        rows = S.dosya_satirlari(self.shared())
        self.assertEqual(len(rows), 1)                                 # the "running" row was replaced, not duplicated
        self.assertEqual(rows[0]["Reason"], "reset")
        self.assertIn("session:Alpha", rows[0]["Items"])
        self.assertFalse(os.path.exists(os.path.join(self.claude, "son-pencere", "koordinator.kilit")))

    def test_a_candidate_that_cannot_be_reached_falls_back_to_the_visible_session(self):
        yol = self.oturum("Alpha")
        self.pace(8)
        r = self.run_sp("tetik", PACE_HEDEF_SAAT="5", FAKE_TRANSCRIPT=yol, FAKE_AD="Nobody", SON_PENCERE_ACK_SN="1")   # the fake relay delivers only to 'Nobody'
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        # the fake answers YOK for Alpha: no candidate took it -> the visible fallback session was started instead
        log = rd(os.path.join(self.tstate, "log"))
        self.assertIn("new-session", log)
        self.assertIn("son-pencere-emre", log)

    def test_without_a_live_session_the_visible_tmux_fallback_starts_and_is_cut_off_at_the_reset(self):
        self.pace(6)
        t = time.time()
        r = self.run_sp("tetik", PACE_HEDEF_SAAT="5")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertLess(time.time() - t, 60)
        self.assertEqual(self.sent(), [])                              # nobody to relay to
        calls = tmux_cagrilari(self.tstate)
        soketler = {json.loads(s)["L"] for s in rd(os.path.join(self.tstate, "log")).splitlines()}
        self.assertEqual(soketler, {"son-pencere"})                   # every call goes to son-pencere's own tmux server, never the user's
        yeni = [c for c in calls if c[0] == "new-session"][0]
        self.assertEqual(yeni[yeni.index("-s") + 1], "son-pencere-emre")
        self.assertIn("--name son-pencere-emre", yeni[-1])
        self.assertIn("--permission-mode bypassPermissions", yeni[-1])
        self.assertIn("-brief.md", yeni[-1])
        self.assertIn("--remote-control son-pencere-emre", yeni[-1])   # visible from claude.ai/code and the app
        self.assertIn("SON_PENCERE_JURNAL=", yeni[-1])
        self.assertTrue(any(c[0] == "send-keys" and "/exit" in c for c in calls))   # /exit first ...
        self.assertTrue(any(c[0] == "kill-session" for c in calls[calls.index(yeni):]))   # ... then the kill
        self.assertFalse(os.path.exists(os.path.join(self.tstate, "s-son-pencere-emre")))
        self.assertIn("exit (`/exit`)", self.brief())                  # our own session may be told to exit
        rows = S.dosya_satirlari(self.shared())
        self.assertEqual((rows[0]["Reason"], "session:son-pencere-emre" in rows[0]["Items"]), ("reset", True))

    def test_outside_the_window_or_with_the_limit_used_nothing_starts(self):
        self.oturum()
        self.pace(8 * H)
        self.assertIn("pencere-oncesi", self.run_sp("tetik").stdout)
        self.pace(3 * H, used=100)
        self.assertIn("kota-bitti", self.run_sp("tetik").stdout)
        self.assertEqual(self.sent(), [])
        self.assertFalse(os.path.exists(self.tstate))

    def test_dry_run_prints_the_ranking_and_sends_and_starts_nothing(self):
        self.oturum("Alpha")
        self.pace(3 * H)
        r = self.run_sp("tetik", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("(b) board", r.stdout)
        self.assertIn("product-a#61", r.stdout)
        self.assertRegex(r.stdout, r"->\s+Alpha ")
        self.assertIn("DRY RUN: would send the assignment to 'Alpha'", r.stdout)
        self.assertEqual(self.sent(), [])
        self.assertFalse(os.path.exists(self.tstate))
        self.assertFalse(os.path.exists(os.path.join(self.claude, "son-pencere", "koordinator.kilit")))
        self.assertEqual(self.shared(), "")
        r = self.run_sp("oturum")
        self.assertIn("Assignment goes to: Alpha", r.stdout)

    def test_sigterm_records_the_run_and_frees_the_lock_without_touching_the_assigned_session(self):
        yol = self.oturum("Alpha")
        self.pace(3 * H)
        env = {k: v for k, v in os.environ.items() if k not in ("PACE_HEDEF_SAAT", "XDG_CONFIG_HOME")}
        env.update(HOME=self.home, CLAUDE_CONFIG_DIR=self.claude, SON_PENCERE_CLAUDE=self.fake, SON_PENCERE_TMUX=self.tmux, SON_PENCERE_SECIM=self.secim,
                   SON_PENCERE_POLL="0.2", SON_PENCERE_ACK_SN="5", FAKE_MARK=self.mark, FAKE_TRANSCRIPT=yol, FAKE_TMUX_STATE=self.tstate,
                   CLAUDE_SHARED_UZAK=self.uzak, CLAUDE_SHARED_SON_PENCERE_KLON=self.klon, CLAUDE_SHARED_KISI="Emre",
                   GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
        p = subprocess.Popen([sys.executable, "-I", SP, "tetik"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            for _ in range(150):                                       # wait until the assignment was delivered
                if len(self.sent()) >= 1:
                    break
                time.sleep(0.1)
            self.assertEqual(len(self.sent()), 1)
            time.sleep(1.5)
            p.terminate()
            p.communicate(timeout=60)
        finally:
            if p.poll() is None:
                p.kill()
        self.assertEqual(p.returncode, 0)
        self.assertEqual(S.dosya_satirlari(self.shared())[0]["Reason"], "durduruldu")
        self.assertFalse(os.path.exists(os.path.join(self.claude, "son-pencere", "koordinator.kilit")))
        self.assertEqual(len(self.sent()), 1)                          # no stop message, the owner's session keeps working
        self.assertEqual(self.canli.poll(), None)

    def test_a_live_lock_blocks_and_a_dead_one_is_taken_over(self):
        self.pace(3 * H)
        os.makedirs(os.path.join(self.claude, "son-pencere"))
        kilit = os.path.join(self.claude, "son-pencere", "koordinator.kilit")
        jwr(kilit, {"pid": os.getpid(), "t": int(time.time())})   # this test process is alive
        self.assertIn("calisiyor", self.run_sp("tetik").stdout)
        self.assertFalse(os.path.exists(self.tstate))
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        jwr(kilit, {"pid": p.pid, "t": int(time.time())})          # a dead pid
        self.pace(6)
        self.run_sp("tetik", PACE_HEDEF_SAAT="5")
        self.assertTrue(os.path.exists(os.path.join(self.tstate, "log")))   # it started (the fallback, no live session here)

    def test_durum_shows_the_session_of_a_running_assignment(self):
        self.pace(3 * H)
        os.makedirs(os.path.join(self.claude, "son-pencere"))
        jwr(os.path.join(self.claude, "son-pencere", "koordinator.kilit"), {"pid": os.getpid(), "t": int(time.time()), "oturum": "Alpha", "mod": "atama"})
        self.assertIn("running now in session 'Alpha' (assigned live session)", self.run_sp("durum", "--yerel").stdout)

    def test_the_fallback_test_never_relays_to_a_live_session(self):
        self.oturum("Alpha")                                           # a live session that WOULD win a real run
        r = self.run_sp("deneme", "--tmux", "--reset-sn", "4")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.sent(), [])                              # nothing was relayed to Alpha
        self.assertIn("new-session", rd(os.path.join(self.tstate, "log")))
        self.assertIn("reason=reset session=son-pencere-test mode=tmux", r.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.claude, "son-pencere", "koordinator.kilit")))   # scratch state only
        self.assertEqual(self.shared(), "")                            # no shared-log row

    def log(self):
        return rd(os.path.join(self.claude, "son-pencere", "son-pencere.log"))

    def test_the_folder_trust_dialog_is_answered_and_the_session_is_then_followed(self):
        self.pace(6)
        r = self.run_sp("tetik", PACE_HEDEF_SAAT="5", FAKE_TMUX_EKRAN=os.path.join(HERE, "fixtures", "guven-ekrani-2.1.316.txt"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        tuslar = [c[-1] for c in tmux_cagrilari(self.tstate) if c[0] == "send-keys"]
        self.assertEqual(tuslar[:2], ["Down", "Enter"])          # the captured 2.1.316 dialog starts on "No, exit"
        self.assertIn("answered the folder-trust dialog", self.log())
        self.assertEqual(S.dosya_satirlari(self.shared())[0]["Reason"], "reset")

    def test_a_session_that_never_registers_is_logged_killed_and_recorded_as_a_quick_failure(self):
        self.pace(60)
        t = time.time()
        r = self.run_sp("tetik", PACE_HEDEF_SAAT="5", SON_PENCERE_KAYIT_SN="2", FAKE_TMUX_KAYITSIZ="1",
                        FAKE_TMUX_EKRAN=os.path.join(HERE, "fixtures", "bypass-ekrani.txt"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertLess(time.time() - t, 45)                     # it did not wait for the reset
        calls = tmux_cagrilari(self.tstate)
        self.assertEqual([c for c in calls if c[0] == "send-keys"], [])   # the bypass warning is never answered, not even by the cutoff
        yeni = [i for i, c in enumerate(calls) if c[0] == "new-session"][0]
        self.assertTrue(any(c[0] == "kill-session" for c in calls[yeni:]))
        log = self.log()
        self.assertIn("bypass-permissions warning: not answered", log)
        self.assertRegex(log, r"\|\s+WARNING: Claude Code running in Bypass Permissions mode")   # the pane is in the log
        self.assertEqual(S.dosya_satirlari(self.shared())[0]["Reason"], "hata")
        self.assertEqual(jrd(os.path.join(self.claude, "son-pencere", "durum.json"))["hizli_hata"], 1)

    def test_pythonw_without_stdout_and_stderr_still_runs_the_gate(self):
        self.pace(8 * H)
        kod = ("import runpy, sys; sys.stdout = None; sys.stderr = None; sys.argv = ['son-pencere', 'tetik']; "
               "runpy.run_path(%r, run_name='__main__')" % SP)
        env = dict(os.environ, HOME=self.home, CLAUDE_CONFIG_DIR=self.claude, PACE_HEDEF_SAAT="5")
        env.pop("SON_PENCERE_DIZIN", None)
        r = subprocess.run([sys.executable, "-I", "-c", kod], capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("pencere-oncesi", self.log())              # the tick ran and logged its decision

    # ---- resume alarm (devam) and burn mode (bitir)
    def durus(self, yol, t, metin="You've hit your session limit · resets 6:30pm (Europe/Istanbul)", uuid="dur-1"):
        """Append the synthetic 429 entry Claude Code writes when the limit stops a session (shape copied from a real transcript)."""
        with open(yol, "a", encoding="utf-8") as f:
            f.write(limit_satiri(t, metin, uuid))

    def bitir_yaz(self, **kw):
        d = {"aktif": True, "acan": "Onur Demir", "acan_login": "example-dev-2", "acilis": int(time.time()) - 60}
        d.update(kw)
        os.makedirs(os.path.join(self.claude, "son-pencere"), exist_ok=True)
        jwr(os.path.join(self.claude, "son-pencere", "bitir.json"), d)

    def bitir_oku(self):
        p = os.path.join(self.claude, "son-pencere", "bitir.json")
        return jrd(p) if os.path.exists(p) else None

    def test_resume_alarm_relays_once_per_reset_to_the_session_the_limit_stopped(self):
        yol = self.oturum("Alpha")
        simdi = time.time()
        self.durus(yol, simdi - 2 * H)
        self.pace(3 * 86400, used=60, five_hour_used=100, five_hour_resets_at=int(simdi - 11 * 60))   # 5 h used up, reset 11 min ago
        r = self.run_sp("tetik", "--kipler", "devam")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([g["to"] for g in self.sent()], ["Alpha"])
        self.assertIn("Devam alarmi", self.sent()[0]["text"])
        self.assertIn("pick nothing new", self.sent()[0]["text"])                  # normal mode: only already-started work
        r = self.run_sp("tetik", "--kipler", "devam")                                 # the next tick: the same reset is not woken twice
        self.assertIn("devam-yok", r.stdout)
        self.assertEqual(len(self.sent()), 1)
        self.assertFalse(os.path.exists(self.tstate))                               # no new session is ever started for a resume
        dvs = jrd(os.path.join(self.claude, "son-pencere", "devam.json"))
        self.assertEqual((dvs["anahtar"], dvs["uyarilan"]), (int(simdi - 11 * 60), ["dur-1"]))

    def test_resume_alarm_after_the_new_window_shows_and_while_a_coordinator_runs(self):
        # review finding: auto-continued sessions already write the NEW window (5 h at 3%) before the alarm's grace is over, and a
        # last-window or burn-mode run may hold the coordinator lock; the session that was stopped with Esc must still be resumed
        yol = self.oturum("Alpha")
        simdi = time.time()
        self.durus(yol, simdi - 4 * H)                                               # stopped in the previous window
        self.pace(3 * 86400, used=60, five_hour_used=3, five_hour_resets_at=int(simdi + 4 * H))   # new window started 1 h ago
        os.makedirs(os.path.join(self.claude, "son-pencere"), exist_ok=True)
        jwr(os.path.join(self.claude, "son-pencere", "koordinator.kilit"), {"pid": self.canli.pid, "t": int(simdi)})   # a live run
        r = self.run_sp("tetik", "--kipler", "devam")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([g["to"] for g in self.sent()], ["Alpha"])
        self.assertTrue(os.path.exists(os.path.join(self.claude, "son-pencere", "koordinator.kilit")))   # the run's lock is left alone
        self.assertFalse(os.path.exists(os.path.join(self.claude, "son-pencere", "devam.kilit")))
        r = self.run_sp("bitir", "--ac", SON_PENCERE_GH_LOGIN="example-dev-2")              # no switching on while a run is live
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertIsNone(self.bitir_oku())

    def test_resume_alarm_waits_for_the_reset_and_claude_codes_own_auto_continue(self):
        yol = self.oturum("Alpha")
        simdi = time.time()
        self.durus(yol, simdi - H)
        self.pace(3 * 86400, used=60, five_hour_used=100, five_hour_resets_at=int(simdi + H))       # 5 h used up, reset in 1 h
        r = self.run_sp("tetik", "--kipler", "devam")
        self.assertIn("devam-bekliyor", r.stdout)
        self.pace(3 * 86400, used=60, five_hour_used=100, five_hour_resets_at=int(simdi - 5 * 60))  # reset 5 min ago: inside the grace
        self.assertIn("devam-bekliyor", self.run_sp("tetik", "--kipler", "devam").stdout)
        self.assertEqual(self.sent(), [])

    def test_resume_alarm_falls_back_to_the_transcripts_without_pace_json(self):
        yol = self.oturum("Alpha")
        self.durus(yol, time.time() - 5 * H - 15 * 60)                               # no pace.json at all: the stop alone dates the reset
        r = self.run_sp("tetik", "--kipler", "devam", FAKE_TRANSCRIPT=yol)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([g["to"] for g in self.sent()], ["Alpha"])
        rows = S.dosya_satirlari(self.shared())
        self.assertEqual((rows[0]["Reason"], "session:Alpha" in rows[0]["Items"]), ("devam/uyandirma", True))
        self.durus(yol, time.time() - 5 * H - 15 * 60, uuid="dur-1")                  # the same stop seen again: never a second relay
        self.run_sp("tetik", "--kipler", "devam")
        self.assertEqual(len(self.sent()), 1)

    def test_resume_alarm_leaves_working_sessions_other_limits_and_last_window_only_timers_alone(self):
        yol = self.oturum("Alpha")
        simdi = time.time()
        self.pace(3 * 86400, used=60, five_hour_used=100, five_hour_resets_at=int(simdi - 20 * 60))
        self.durus(yol, simdi - H, metin="You've hit your org's monthly spend limit · your session limit resets 4:20am")
        self.run_sp("tetik", "--kipler", "devam")                                      # a spend limit: a 5-hour reset does not lift it
        self.assertEqual(self.sent(), [])
        os.remove(os.path.join(self.claude, "son-pencere", "devam.json"))
        self.durus(yol, simdi - H, uuid="dur-2")
        with open(yol, "a") as f:
            f.write(asistan("claude-opus-5-5", "after"))                               # it already went on (auto-continue): leave it
        self.run_sp("tetik", "--kipler", "devam")
        self.assertEqual(self.sent(), [])
        os.remove(os.path.join(self.claude, "son-pencere", "devam.json"))
        self.durus(yol, simdi - H, uuid="dur-3")
        r = self.run_sp("tetik")                                                       # a last-window-only timer (no --kipler devam)
        self.assertIn("resume alarm: not installed", r.stdout)
        self.assertEqual(self.sent(), [])

    def test_burn_mode_is_switched_on_and_off_only_by_a_developer(self):
        self.pace(3 * 86400)
        r = self.run_sp("bitir", "--ac", SON_PENCERE_GH_LOGIN="example-tester-1")          # a tester (rol: testci)
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertIn("Reddedildi", r.stdout)
        self.assertIsNone(self.bitir_oku())
        self.assertEqual(self.run_sp("bitir", "--ac", SON_PENCERE_GH_LOGIN="kimseyok").returncode, 3)   # not in kisiler.json
        r = self.run_sp("bitir", "--ac", SON_PENCERE_GH_LOGIN="example-dev-2", SON_PENCERE="1")              # from inside a son-pencere session
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertIsNone(self.bitir_oku())
        r = self.run_sp("bitir", "--ac", SON_PENCERE_GH_LOGIN="example-dev-2")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Token bitirme modu AÇIK", r.stdout)
        b = self.bitir_oku()
        self.assertEqual((b["aktif"], b["acan"], b["acan_login"]), (True, "Onur Demir", "example-dev-2"))
        self.assertEqual(b["hafta_reset"], jrd(os.path.join(self.claude, "pace.json"))["week_resets_at"])   # auto-off at THIS weekly reset
        self.assertIn("AÇIK", self.run_sp("bitir", "durum").stdout)
        self.assertEqual(self.run_sp("bitir", "--kapat", SON_PENCERE_GH_LOGIN="example-tester-1").returncode, 3)
        self.assertTrue(self.bitir_oku()["aktif"])
        r = self.run_sp("bitir", "--kapat", SON_PENCERE_GH_LOGIN="example-dev-1")      # another developer may switch it off
        self.assertEqual(r.returncode, 0, r.stdout)
        b = self.bitir_oku()
        self.assertEqual((b["aktif"], b["kapatan"]), (False, "Emre"))
        self.assertIn("KAPALI", self.run_sp("bitir").stdout)

    def test_burn_mode_needs_a_known_weekly_reset_and_quota_left(self):
        self.assertEqual(self.run_sp("bitir", "--ac", SON_PENCERE_GH_LOGIN="example-dev-2").returncode, 2)   # no pace.json
        self.pace(3 * 86400, used=100)
        self.assertEqual(self.run_sp("bitir", "--ac", SON_PENCERE_GH_LOGIN="example-dev-2").returncode, 2)
        self.assertIsNone(self.bitir_oku())

    def test_burn_mode_gate_at_99_and_100_percent_and_its_empty_run_backoff(self):
        self.bitir_yaz(hafta_reset=int(time.time() + 3 * 86400))
        self.pace(3 * 86400, used=99)
        r = self.run_sp("tetik", "--dry-run")
        self.assertIn("bitir-acik", r.stdout)                                          # outside the window, quota left: burn mode starts
        self.assertIn("burn mode: ON", r.stdout)
        self.pace(3 * 86400, used=100)
        self.assertIn("kota-bitti", self.run_sp("tetik", "--dry-run").stdout)
        self.pace(3 * 86400, used=50)
        os.makedirs(os.path.join(self.claude, "son-pencere"), exist_ok=True)
        jwr(os.path.join(self.claude, "son-pencere", "durum.json"), {"bos_bekle": int(time.time() + 600)})
        self.assertIn("bos-bekleme", self.run_sp("tetik", "--dry-run").stdout)

    def test_burn_mode_run_outside_the_window_ends_at_the_weekly_reset_and_switches_itself_off(self):
        yol = self.oturum("Alpha")
        self.pace(6)
        reset = jrd(os.path.join(self.claude, "pace.json"))["week_resets_at"]
        self.bitir_yaz(hafta_reset=reset)
        r = self.run_sp("tetik", PACE_HEDEF_SAAT="0", FAKE_TRANSCRIPT=yol)              # no last window at all: only burn mode can start
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        g = self.sent()
        self.assertEqual([x["to"] for x in g], ["Alpha", "Alpha"])
        self.assertIn("burn mode", g[0]["text"])
        self.assertIn("Reset reached", g[1]["text"])
        b = self.brief()
        self.assertIn("switched on burn mode", b)
        self.assertNotIn("{{", b)
        self.assertNotIn("kip:", b)                                                    # the mode blocks are resolved
        self.assertIn("switching burn mode on or off", b)                              # the never-list covers the switch itself
        rows = S.dosya_satirlari(self.shared())
        self.assertEqual(rows[0]["Reason"], "bitir/reset")
        r = self.run_sp("tetik", PACE_HEDEF_SAAT="0")                                  # the next tick, after the weekly reset
        self.assertIn("burn mode: off", r.stdout)
        b = self.bitir_oku()
        self.assertEqual((b["aktif"], b["kapatan"]), (False, "otomatik (haftalik sifirlanma)"))

    def test_switching_burn_mode_off_ends_the_running_assignment(self):
        yol = self.oturum("Alpha")
        self.pace(120)
        self.bitir_yaz(hafta_reset=jrd(os.path.join(self.claude, "pace.json"))["week_resets_at"])
        p = subprocess.Popen([sys.executable, "-I", SP, "tetik"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                             env=self.ortam(PACE_HEDEF_SAAT="0", FAKE_TRANSCRIPT=yol))
        self.addCleanup(lambda: p.poll() is None and p.kill())
        bitis = time.time() + 60
        while not self.sent() and time.time() < bitis:
            time.sleep(0.2)
        self.assertEqual(len(self.sent()), 1)
        b = self.bitir_oku()
        b.update(aktif=False, kapatan="Onur Demir")
        jwr(os.path.join(self.claude, "son-pencere", "bitir.json"), b)
        out, err = p.communicate(timeout=90)
        self.assertEqual(p.returncode, 0, out + err)
        self.assertIn("was switched off", self.sent()[1]["text"])
        self.assertEqual(S.dosya_satirlari(self.shared())[0]["Reason"], "bitir/bitir-kapandi")


class SahneTemel(unittest.TestCase):
    """calistir() in-process with a fake clock: live sessions are registry files of this test process's own pid, the relay (S.ilet) is
    scripted, sleeping advances the clock and fires scheduled events (a late message, a status change). Nothing real is started."""
    RESET_IN = 1200

    def setUp(self):
        from unittest import mock
        self.root = tempfile.mkdtemp(prefix="sp-sahne-")
        self.addCleanup(shutil.rmtree, self.root, True)
        claude = os.path.join(self.root, ".claude")
        dizin = os.path.join(claude, "son-pencere")
        os.makedirs(os.path.join(claude, "sessions"))
        self.t, self.olaylar, self.gonderilen, self.davranis, self.oturumlar = float(NOW), [], [], [], {}
        self.baslatilan = []
        for ad, v in (("CLAUDE", claude), ("DIZIN", dizin), ("KILIT", os.path.join(dizin, "koordinator.kilit")),
                      ("DURUM", os.path.join(dizin, "durum.json")), ("ZAMAN", lambda: self.t), ("ilet", self.role),
                      ("istem_kur", lambda *a, **k: "brief"), ("_kisi", lambda: "Tester"), ("makine_adi", lambda: "m"),
                      ("pencere_adi", lambda: "son-pencere-tester"), ("kok_dizin", lambda: self.root), ("TmuxOturum", self.sahte_oturum_sinifi()),
                      ("SekmeOturum", self.sahte_oturum_sinifi())):
            p = mock.patch.object(S, ad, v)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(S.time, "sleep", self.uyku)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.dict(os.environ, {"SON_PENCERE_TMUX": "/bin/true"})
        p.start()
        self.addCleanup(p.stop)
        for k in ("SON_PENCERE_ACK_SN", "SON_PENCERE_STOP_SN", "SON_PENCERE_IDLE_SN", "SON_PENCERE_RESET_DENEME", "SON_PENCERE_HARIC"):
            os.environ.pop(k, None)

    # --- the scene
    def uyku(self, sn):
        self.t += max(float(sn), 0.01)
        for olay in [o for o in self.olaylar if o[0] <= self.t]:
            self.olaylar.remove(olay)
            olay[1]()

    def sonra(self, sn, f):
        self.olaylar.append((self.t + sn, f))

    def oturum(self, ad, durum="busy", model="claude-opus-5-5"):
        sid = "22222222-2222-2222-2222-%012d" % (len(self.oturumlar) + 1)
        cwd = "/home/u/" + ad
        yol = S.transkript_yolu(cwd, sid)
        os.makedirs(os.path.dirname(yol), exist_ok=True)
        with open(yol, "w") as f:
            f.write(asistan(model, "eski-" + ad))
        self.oturumlar[ad] = {"sid": sid, "cwd": cwd, "yol": yol, "pid": os.getpid() if not self.oturumlar else self.yardimci_pid()}
        self.durum_yaz(ad, durum)
        return yol

    def yardimci_pid(self):
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
        self.addCleanup(lambda: (p.kill(), p.wait()))
        return p.pid

    def durum_yaz(self, ad, durum):
        o = self.oturumlar[ad]
        jwr(os.path.join(S.CLAUDE, "sessions", "%d.json" % o["pid"]), oturum_json(o["pid"], ad, o["sid"], o["cwd"], 1000 + len(self.oturumlar), durum))

    def mesaj_yaz(self, ad, metin, tur=True):
        with open(self.oturumlar[ad]["yol"], "a") as f:
            f.write(json.dumps({"type": "user", "message": "<cross-session-message>" + metin + "</cross-session-message>"}) + "\n")
            if tur:
                f.write(asistan("claude-opus-5-5", "t%d" % len(self.gonderilen)))

    def role(self, ad, metin, zaman_asimi=None):
        """Scripted relay: each call takes the next (answer, delay of the message in s or None = never shows, clock jump)."""
        self.gonderilen.append((ad, metin))
        cevap, gecikme, atla = (self.davranis.pop(0) if self.davranis else ("teslim", 0, 0))
        self.t += atla
        if cevap == "teslim" and gecikme is not None:
            if gecikme == 0:
                self.mesaj_yaz(ad, metin)
            else:
                self.sonra(gecikme, lambda: self.mesaj_yaz(ad, metin))
        return cevap

    def sahte_oturum_sinifi(self):
        test = self

        class Sahte(S.Oturum):
            hata = None

            def __init__(self, ad, cwd):
                super().__init__(ad, "33333333-3333-3333-3333-333333333333", cwd, "tmux")
                self.durdu = 0
                test.baslatilan.append(self)

            def baslat(self, kom, env):
                self.kom = kom

            def kayit_bekle(self, sn=None):
                if Sahte.hata:
                    raise Sahte.hata
                return True

            def canli(self):
                return not self.durdu

            def durdur(self):
                self.durdu += 1
        return Sahte

    def calistir(self):
        kota = {"week_used": 93, "week_resets_at": NOW + self.RESET_IN, "five_hour_used": None, "five_hour_resets_at": None}
        return S.calistir(kota, {}, {}, NOW)

    def log(self):
        p = os.path.join(S.DIZIN, "son-pencere.log")
        return rd(p) if os.path.exists(p) else ""



class Sahne(SahneTemel):
    # --- finding 1: a relay's TESLIM binds the run to that session
    def test_teslim_with_the_message_showing_late_stays_with_that_session_no_second_delivery(self):
        self.oturum("Alpha", "busy")
        self.oturum("Beta", "idle")
        self.davranis = [("teslim", 900, 0)]                 # Alpha is busy: the message shows up 15 min later, after ACK_SN
        yol = self.oturumlar["Alpha"]["yol"]
        self.sonra(100, lambda: open(yol, "a").write(asistan("claude-opus-5-5", "before-the-marker")))   # its own turn: not this run's
        k = self.calistir()
        self.assertEqual([a for a, _ in self.gonderilen], ["Alpha", "Alpha"])   # the assignment once, then "reset reached"
        self.assertIn("Reset reached", self.gonderilen[1][1])
        self.assertEqual((k["neden"], k["oturum"], k["mod"]), ("reset", "Alpha", "atama"))
        self.assertEqual(k["tur"], 1)                          # only the turn after the marker counts
        self.assertEqual(self.baslatilan, [])                  # no fallback session
        self.assertRegex(self.log(), r"shows the assignment, 9\d\d s")

    def test_teslim_without_the_message_on_an_idle_session_gives_up_without_a_second_delivery(self):
        self.oturum("Alpha", "idle")
        self.oturum("Beta", "idle")
        self.davranis = [("teslim", None, 0)]                 # the relay says TESLIM, the message never shows up
        k = self.calistir()
        self.assertEqual([a for a, _ in self.gonderilen], ["Alpha"])
        self.assertEqual(k["neden"], "teslim-gorulmedi")
        self.assertEqual(self.baslatilan, [])

    def test_teslim_without_the_message_on_a_busy_session_follows_it_to_the_reset(self):
        self.oturum("Alpha", "busy")
        self.davranis = [("teslim", None, 0), ("teslim", 0, 0)]
        k = self.calistir()
        self.assertEqual([a for a, _ in self.gonderilen], ["Alpha", "Alpha"])
        self.assertEqual(k["neden"], "reset")
        self.assertEqual(self.baslatilan, [])

    def test_a_relay_that_times_out_hands_nothing_to_another_session_or_the_fallback(self):
        self.oturum("Alpha", "idle")
        self.oturum("Beta", "idle")
        self.davranis = [("belirsiz", None, 0)]
        k = self.calistir()
        self.assertEqual([a for a, _ in self.gonderilen], ["Alpha"])
        self.assertEqual((k["neden"], k["cikis"]), ("teslim-belirsiz", 1))
        self.assertEqual(self.baslatilan, [])

    def test_a_clear_yok_or_a_relay_error_moves_on_and_then_to_the_fallback(self):
        self.oturum("Alpha", "idle")
        self.oturum("Beta", "idle")
        self.davranis = [("yok", None, 0), ("hata", None, 0)]
        k = self.calistir()
        self.assertEqual([a for a, _ in self.gonderilen], ["Alpha", "Beta"])
        self.assertEqual(len(self.baslatilan), 1)
        self.assertEqual((k["mod"], k["neden"]), ("tmux", "reset"))

    # --- finding 2: our own session never outlives the wrapper
    def test_an_exception_after_the_start_stops_our_own_session_and_records_the_run(self):
        S.TmuxOturum.hata = RuntimeError("boom")
        k = self.calistir()
        self.assertEqual(len(self.baslatilan), 1)
        self.assertEqual(self.baslatilan[0].durdu, 1)
        self.assertEqual((k["neden"], k["cikis"]), ("hata", -1))
        self.assertIn("RuntimeError: boom", self.log())

    def test_a_signal_after_the_start_stops_our_own_session(self):
        S.TmuxOturum.hata = S.Durduruldu()
        k = self.calistir()
        self.assertEqual(self.baslatilan[0].durdu, 1)
        self.assertEqual(k["neden"], "durduruldu")

    # --- finding 6: "reset reached" is confirmed, not fire-and-forget
    def test_a_failed_reset_relay_is_retried_until_the_message_shows(self):
        self.oturum("Alpha", "busy")
        self.davranis = [("teslim", 0, 0), ("yok", None, 0), ("hata", None, 0), ("teslim", 0, 0)]
        k = self.calistir()
        self.assertEqual([a for a, _ in self.gonderilen], ["Alpha"] * 4)
        self.assertEqual(k["reset_bildirim"], "gorundu")
        self.assertIn("reset message reached 'Alpha' (relay 3 of 3)", self.log())

    def test_reset_relays_are_bounded_and_the_failure_is_logged(self):
        self.oturum("Alpha", "busy")
        self.davranis = [("teslim", 0, 0)] + [("yok", None, 0)] * 10
        k = self.calistir()
        self.assertEqual(len(self.gonderilen), 1 + 3)
        self.assertEqual(k["reset_bildirim"], "yok")
        self.assertIn("NOT confirmed", self.log())

    def test_an_idle_session_that_does_not_show_the_reset_message_gets_it_again(self):
        self.oturum("Alpha", "busy")
        self.davranis = [("teslim", 0, 0), ("teslim", None, 0), ("teslim", 0, 0)]
        self.sonra(self.RESET_IN - 5, lambda: self.durum_yaz("Alpha", "idle"))
        k = self.calistir()
        self.assertEqual(len(self.gonderilen), 3)
        self.assertEqual(k["reset_bildirim"], "gorundu")

    # --- finding 7: nothing is handed out once the reset (or its last 10 minutes) comes during delivery
    def test_the_reset_coming_during_candidate_delivery_stops_the_hand_out(self):
        self.oturum("Alpha", "idle")
        self.oturum("Beta", "idle")
        self.davranis = [("yok", None, self.RESET_IN - 300)]   # the first relay took until 5 min before the reset
        k = self.calistir()
        self.assertEqual([a for a, _ in self.gonderilen], ["Alpha"])   # Beta is not tried
        self.assertEqual(self.baslatilan, [])                          # and no fallback session
        self.assertEqual(k["neden"], "reset-yakin")


class SahneSekme(SahneTemel):
    """The Windows tab (SekmeOturum) through the scene: wt.exe / cmd are not run (Popen recorded), taskkill is recorded."""

    def setUp(self):
        super().setUp()
        from unittest import mock
        self.oldurulen, self.acilan = [], []
        self.tab_pid = self.yardimci_pid()
        for ad, v in (("TmuxOturum", GERCEK_SEKME), ("_taskkill", self.oldurulen.append), ("_sid_surecleri", lambda sid: [4242])):
            p = mock.patch.object(S, ad, v)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(S.subprocess, "Popen", lambda kom, **kw: self.acilan.append(kom))
        p.start()
        self.addCleanup(p.stop)

    def kaydol(self):
        """The tab's claude registers: its session id is the one in the generated .cmd file."""
        cmd = rd(os.path.join(S.DIZIN, "runs", "son-pencere-tester.cmd"))
        sid = cmd.split("--session-id ")[1].split()[0]
        jwr(os.path.join(S.CLAUDE, "sessions", "%d.json" % self.tab_pid), oturum_json(self.tab_pid, "son-pencere-tester", sid, self.root, 1, "busy"))

    def test_a_windows_tab_that_registers_late_is_followed_not_recorded_as_failed(self):
        self.sonra(60, self.kaydol)                            # wt.exe returned at once; the tab registers a minute later
        k = self.calistir()
        self.assertEqual(len(self.acilan), 1)
        self.assertEqual((k["neden"], k["mod"]), ("reset", "sekme"))
        self.assertEqual(self.oldurulen, [self.tab_pid])       # ended by its registered pid at the reset

    def test_a_windows_tab_that_never_registers_is_stopped_by_its_session_id(self):
        k = self.calistir()
        self.assertEqual((k["neden"], k.get("hizli")), ("hata", True))
        self.assertIn("not registered within 120 s", k["ayrinti"])
        self.assertEqual(self.oldurulen, [4242])


class SahneClear(SahneTemel):
    def test_a_new_session_id_after_clear_is_followed_by_pid_and_name(self):
        self.oturum("Alpha", "busy")
        self.davranis = [("teslim", 0, 0)]

        def clear():                                           # same process, new session id and transcript
            o = self.oturumlar["Alpha"]
            o["sid"] = "44444444-4444-4444-4444-444444444444"
            o["yol"] = S.transkript_yolu(o["cwd"], o["sid"])
            with open(o["yol"], "w") as f:
                f.write(asistan("claude-opus-5-5", "after-clear"))
            self.durum_yaz("Alpha", "busy")
        self.sonra(300, clear)
        k = self.calistir()
        self.assertEqual(k["neden"], "reset")                  # not "oturum-kapandi"
        self.assertEqual(k["reset_bildirim"], "gorundu")       # the reset message was seen in the NEW transcript
        self.assertEqual(k["tur"], 2)                          # the turn after the marker and the one after /clear
        self.assertIn("has a new session id", self.log())


class Ilet(unittest.TestCase):
    """The relay's answer, through the fake relay script (no real claude)."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="sp-ilet-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.fake = os.path.join(self.root, "fake_claude.py")
        with open(self.fake, "w") as f:
            f.write("#!%s\n%s" % (sys.executable, SAHTE_ROLE))
        os.chmod(self.fake, 0o755)
        from unittest import mock
        for ad, v in (("DIZIN", self.root), ("HOME", self.root)):
            p = mock.patch.object(S, ad, v)
            p.start()
            self.addCleanup(p.stop)

    def ilet(self, mod, **env):
        from unittest import mock
        yol = os.path.join(self.root, "t.jsonl")
        with mock.patch.dict(os.environ, dict(SON_PENCERE_CLAUDE=self.fake, FAKE_MARK=os.path.join(self.root, "mark"), FAKE_MODE=mod,
                                              FAKE_TRANSCRIPT=yol, SON_PENCERE_ROLE_SN="2", **env)):
            return S.ilet("Alpha", "hello")

    def test_answers(self):
        self.assertEqual(S.ilet_sonucu("**TESLIM**"), "teslim")
        self.assertEqual(S.ilet_sonucu("Done.\nTESLIM."), "teslim")
        self.assertEqual(S.ilet_sonucu("YOK"), "yok")
        self.assertIsNone(S.ilet_sonucu("TESLIMAT"))
        self.assertEqual(self.ilet("ok", FAKE_MARKDOWN="1"), "teslim")
        self.assertEqual(self.ilet("yok"), "yok")
        self.assertEqual(self.ilet("hata"), "hata")
        self.assertEqual(self.ilet("bos"), "belirsiz")

    @unittest.skipIf(os.name == "nt", "the fake relay is an executable script")
    def test_a_relay_timeout_is_belirsiz(self):
        t = time.time()
        self.assertEqual(self.ilet("zaman"), "belirsiz")
        self.assertLess(time.time() - t, 30)


class Deneme(unittest.TestCase):
    def test_the_test_command_refuses_foreign_sessions(self):
        r = subprocess.run([sys.executable, "-I", SP, "deneme", "--gonder", "emre-07"], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 2)
        self.assertIn("refused", r.stdout)


class KipKapisi(unittest.TestCase):
    """The one gate with its modes: last window, burn mode, resume alarm (pure functions, simulated pace.json states)."""
    RESET = NOW + 3 * 86400        # weekly reset in 3 days: outside the 5 h window

    def kota(self, used=50, reset=None, **kw):
        return {"week_used": used, "week_resets_at": reset or self.RESET, "five_hour_used": None, "five_hour_resets_at": None, **kw}

    def test_burn_mode_runs_outside_the_window_until_the_weekly_limit(self):
        k = lambda **kw: S.karar(NOW, self.kota(**{x: v for x, v in kw.items() if x not in ("son", "bos")}), 5, False, kw.get("son"),
                                 kip="bitir", bos_bekle=kw.get("bos"))
        self.assertEqual(k()[:2], ("baslat", "bitir-acik"))
        self.assertEqual(k(used=99)[0], "baslat")                                       # weekly at 99%: still works
        self.assertEqual(k(used=100)[1], "kota-bitti")
        self.assertEqual(k(five_hour_used=100, five_hour_resets_at=NOW + H)[1], "bes-saat-dolu")   # 5 h used up: waits for its reset
        self.assertEqual(k(bos=NOW + 60)[1], "bos-bekleme")
        self.assertEqual(k(son=NOW - 60)[1], "tekrar-araligi")
        self.assertEqual(S.karar(NOW, self.kota(), 5, False, None)[1], "pencere-oncesi")   # the same state without burn mode: nothing

    def test_resume_skips_the_window_and_spacing_but_not_the_lock_or_the_weekly_limit(self):
        self.assertEqual(S.karar(NOW, self.kota(), 5, False, NOW - 60, kip="devam")[:2], ("baslat", "devam"))
        self.assertEqual(S.karar(NOW, {"week_resets_at": None}, 5, False, None, kip="devam")[0], "baslat")   # no weekly data: not blocked
        self.assertEqual(S.karar(NOW, self.kota(), 5, True, None, kip="devam")[1], "calisiyor")
        self.assertEqual(S.karar(NOW, self.kota(used=100), 5, False, None, kip="devam")[1], "kota-bitti")

    def test_precedence_window_then_burn_mode_then_resume(self):
        sec = lambda kota, kipler, bitir, devam: (S.kip_sec(NOW, kota, 5, False, None, 0, kipler, bitir, devam)[0] or [None])[0]
        self.assertEqual(sec(self.kota(), ("pencere",), True, True), "bitir")
        self.assertEqual(sec(self.kota(), ("pencere", "devam"), False, True), "devam")
        self.assertIsNone(sec(self.kota(), ("pencere",), False, False))
        self.assertEqual(sec(self.kota(reset=NOW + 3 * H), ("pencere",), True, True), "pencere")
        self.assertEqual(len(S.kip_sec(NOW, self.kota(), 5, False, None, 0, ("devam",), False, False)[1]), 0)   # nothing installed/due

    def test_resume_state_from_pace_json(self):
        f = lambda u5, r5, dvs=None: S.devam_durumu({"five_hour_used": u5, "five_hour_resets_at": r5}, dvs or {}, NOW)
        self.assertEqual(f(100, NOW + H)["neden"], "devam-bekliyor")                     # 5 h used up, reset to come
        self.assertEqual(f(100, NOW - 60)["neden"], "devam-bekliyor")                    # reset passed, auto-continue goes first
        d = f(100, NOW - 11 * 60)
        self.assertEqual((d["hazir"], d["anahtar"]), (True, NOW - 11 * 60))              # reset passed: due, keyed by that reset
        self.assertIsNone(f(100, NOW - 11 * 60, {"anahtar": NOW - 11 * 60})["hazir"])    # handled: only unrelayed stops (uuids) remain
        self.assertIsNone(f(3, NOW + 4 * H)["hazir"])                                    # a newer window already shows: the transcripts decide
        self.assertIsNone(S.devam_durumu({}, {}, NOW)["hazir"])                           # pace.json missing: the transcripts decide
        self.assertIsNone(f(100, NOW - 6 * H)["hazir"])                                  # stale: the same fallback, no crash
        self.assertIsNone(S.devam_durumu({"five_hour_used": "x", "five_hour_resets_at": None}, {}, NOW)["hazir"])

    def test_reset_of_a_stop(self):
        self.assertEqual(S.durus_reseti(NOW - H, NOW + H), NOW + H)                       # in pace.json's window: its reset
        self.assertEqual(S.durus_reseti(NOW - 4 * H, NOW + 4 * H), NOW - H)               # a newer window shows: reset by its start at the latest
        self.assertEqual(S.durus_reseti(NOW - H, None), NOW + 4 * H)                      # nothing known: 5 h after the stop at the latest
        self.assertEqual(S.durus_reseti(NOW - H, NOW - 2 * H), NOW + 4 * H)               # an older reading says nothing about this stop


class LimitDurusu(unittest.TestCase):
    """Which live session the 5-hour limit stopped, from the registry and the transcript tail (scratch config dir)."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="sp-limit-")
        self.eski = (S.CLAUDE, S.DIZIN)
        S.CLAUDE, S.DIZIN = os.path.join(self.root, ".claude"), os.path.join(self.root, ".claude", "son-pencere")
        os.makedirs(os.path.join(S.CLAUDE, "sessions"))
        self.canli = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
        self.n = 0

    def tearDown(self):
        self.canli.kill()
        self.canli.wait()
        S.CLAUDE, S.DIZIN = self.eski
        shutil.rmtree(self.root, ignore_errors=True)

    def ekle(self, ad, *satirlar, durum="idle"):
        self.n += 1
        sid, cwd = "00000000-0000-0000-0000-%012d" % self.n, "/home/u/w%d" % self.n
        jwr(os.path.join(S.CLAUDE, "sessions", "%d-%d.json" % (self.canli.pid, self.n)), oturum_json(self.canli.pid, ad, sid, cwd, 1, durum))
        yol = S.transkript_yolu(cwd, sid)
        os.makedirs(os.path.dirname(yol), exist_ok=True)
        with open(yol, "w", encoding="utf-8") as f:
            f.write(asistan("claude-opus-5-5") + "".join(satirlar))
        return yol

    def test_the_stop_is_the_last_assistant_entry_and_only_a_5_hour_one(self):
        t = NOW - H
        oku = lambda *s: S.limit_durusu(self.ekle("x", *s))
        d = oku(limit_satiri(t))
        self.assertEqual((round(d["t"]), d["uuid"]), (t, "dur-1"))
        self.assertIsNotNone(oku(limit_satiri(t), json.dumps({"type": "user", "message": "<task-notification>"}) + "\n"))   # user lines after it
        self.assertIsNone(oku(limit_satiri(t), asistan("claude-opus-5-5", "later")))      # it went on afterwards
        self.assertIsNone(oku(limit_satiri(t, "You've hit your weekly limit · resets Oct 21, 3am")))
        self.assertIsNone(oku(limit_satiri(t, "You've hit your individual spend limit · your session limit resets 9:20am")))
        self.assertIsNone(oku())

    def test_due_sessions_by_pace_reset_or_by_the_stop_alone(self):
        self.ekle("durdu", limit_satiri(NOW - 2 * H))
        self.ekle("calisiyor", limit_satiri(NOW - 2 * H, uuid="d2"), durum="busy")       # busy = it already went on
        self.ekle("eski", limit_satiri(NOW - 13 * H, uuid="d3"))                          # older than DEVAM_AZAMI_YAS
        self.ekle("son-pencere-emre", limit_satiri(NOW - 2 * H, uuid="d4"))             # son-pencere's own session
        ad = lambda r5, **kw: [o["ad"] for o in S.limitte_duranlar(S.oturumlari_oku(model=False), r5, NOW, **kw)]
        self.assertEqual(ad(NOW - 11 * 60), ["durdu"])                                    # reset 11 min ago (pace.json)
        self.assertEqual(ad(NOW - 5 * 60), [])                                            # reset 5 min ago: the grace is not over
        self.assertEqual(ad(NOW - 11 * 60, uyarilan=["dur-1"]), [])                       # already relayed for this stop
        self.assertEqual(ad(NOW - 11 * 60, atla={"durdu"}), [])
        self.assertEqual(ad(None), [])                                                    # no pace: stop + 5 h is still ahead
        self.assertEqual([o["ad"] for o in S.limitte_duranlar(S.oturumlari_oku(model=False), None, NOW + 3 * H + 11 * 60)], ["durdu"])


class BitirDurumu(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="sp-bitir-")
        self.eski = S.DIZIN
        S.DIZIN = self.root

    def tearDown(self):
        S.DIZIN = self.eski
        shutil.rmtree(self.root, ignore_errors=True)

    def test_on_until_the_stored_weekly_reset_then_switched_off_once(self):
        jwr(S.bitir_yolu(), {"aktif": True, "acan": "Onur Demir", "hafta_reset": NOW + 60})
        self.assertTrue(S.bitir_acik(NOW))
        self.assertFalse(S.bitir_suresi_doldu(NOW))
        self.assertFalse(S.bitir_acik(NOW + 60))                                          # at the reset it is off, even unwritten
        self.assertTrue(S.bitir_suresi_doldu(NOW + 61))
        self.assertEqual(jrd(S.bitir_yolu())["kapatan"], "otomatik (haftalik sifirlanma)")
        self.assertFalse(S.bitir_suresi_doldu(NOW + 62))
        self.assertFalse(S.bitir_acik(NOW, {}))                                           # never switched on

    def test_the_brief_keeps_only_the_blocks_of_its_mode(self):
        metin = "a\n<!-- kip:pencere -->\nP\n<!-- /kip -->\n<!-- kip:bitir,pencere -->\nB\n<!-- /kip -->\nz\n"
        self.assertEqual(S.kip_bloklari(metin, "pencere"), "a\nP\nB\nz\n")
        self.assertEqual(S.kip_bloklari(metin, "bitir"), "a\nB\nz\n")


if __name__ == "__main__":
    unittest.main()
