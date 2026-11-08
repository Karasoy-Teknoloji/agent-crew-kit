"""Only a payload from a live Claude Code session may write the live pace state (statusline.ps1 and statusline.sh).
Incident 05.11.2026: an agent ran the real status line with tests/fixtures/statusline-girdi.json (63% / 60%, session_id sess-fixture-0001)
against the real profile; pace.json, pace-history.csv and statusline-girdi.json were overwritten and the pace hook told a session
"SIKI, 63%" while the status line showed the real 54%. The registry ~/.claude/sessions/<pid>.json lists the live sessions: a payload whose
session_id is not in it is still rendered, but nothing is written. No registry (or none with a sessionId) = no way to judge = write as before.
Run: python3 -I -m unittest discover -s plugins/kota-temposu/tests"""
import json, os, shutil, subprocess, tempfile, time, unittest

PLUG = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
PWSH = shutil.which("pwsh")
BASH_JQ = shutil.which("bash") and shutil.which("jq")


def payload(sid, now=None):
    now = now or int(time.time())
    return json.dumps({"session_id": sid, "model": {"display_name": "X", "id": "x"}, "workspace": {"current_dir": "/"},
                       "rate_limits": {"seven_day": {"used_percentage": 63, "resets_at": now + 3 * 86400},
                                       "five_hour": {"used_percentage": 60, "resets_at": now + 3600}}})


class Taban:
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="pace-canli-")
        self.claude = os.path.join(self.home, ".claude")
        os.makedirs(self.claude)

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def registry(self, sid, pid="4242"):
        os.makedirs(os.path.join(self.claude, "sessions"), exist_ok=True)
        with open(os.path.join(self.claude, "sessions", pid + ".json"), "w", encoding="utf-8") as f:
            json.dump({"pid": int(pid), "sessionId": sid, "kind": "interactive"}, f, separators=(",", ":"))

    def written(self):
        return {n: os.path.exists(os.path.join(self.claude, n)) for n in ("pace.json", "pace-history.csv", "statusline-girdi.json")}

    def env(self):
        return dict(os.environ, HOME=self.home, USERPROFILE=self.home, MALIYET_DURUM="0", KOTA_PAYLAS_OTO="0", COLUMNS="300")

    def run_status(self, data):
        raise NotImplementedError

    def test_live_session_writes(self):
        self.registry("live-1")
        out = self.run_status(payload("live-1"))
        self.assertIn("hafta %63", out)
        self.assertEqual(self.written(), {"pace.json": True, "pace-history.csv": True, "statusline-girdi.json": True})

    def test_unregistered_session_renders_but_writes_nothing(self):
        self.registry("live-1")
        out = self.run_status(payload("sess-fixture-0001"))
        self.assertIn("hafta %63", out)                                  # still shown to whoever asked
        self.assertEqual(self.written(), {"pace.json": False, "pace-history.csv": False, "statusline-girdi.json": False})

    def test_missing_session_id_is_not_live_when_a_registry_exists(self):
        self.registry("live-1")
        d = json.loads(payload("x")); del d["session_id"]
        self.run_status(json.dumps(d))
        self.assertFalse(self.written()["pace.json"])

    def test_no_registry_writes_as_before(self):
        self.run_status(payload("sess-fixture-0001"))
        self.assertTrue(self.written()["pace.json"])

    def test_registry_without_session_ids_writes_as_before(self):
        os.makedirs(os.path.join(self.claude, "sessions"))
        with open(os.path.join(self.claude, "sessions", "1.key"), "w") as f:
            f.write("x")
        with open(os.path.join(self.claude, "sessions", "2.json"), "w") as f:
            f.write("{}")
        self.run_status(payload("sess-fixture-0001"))
        self.assertTrue(self.written()["pace.json"])


@unittest.skipUnless(PWSH, "pwsh required")
class Ps1(Taban, unittest.TestCase):
    def run_status(self, data):
        return subprocess.run([PWSH, "-NoProfile", "-File", os.path.join(PLUG, "statusline.ps1")], input=data, text=True, encoding="utf-8",
                              env=self.env(), capture_output=True, timeout=90).stdout


@unittest.skipUnless(BASH_JQ, "bash and jq required")
class Sh(Taban, unittest.TestCase):
    def run_status(self, data):
        return subprocess.run(["bash", os.path.join(PLUG, "statusline.sh")], input=data, text=True, encoding="utf-8",
                              env=self.env(), capture_output=True, timeout=90).stdout


if __name__ == "__main__":
    unittest.main()
