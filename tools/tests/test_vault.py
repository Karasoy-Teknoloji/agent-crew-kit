"""Tests for tools/vault.py with a fake Vault (mock HTTP). Run: python -m unittest discover -s tools/tests"""
import base64
import contextlib
import hashlib
import tempfile
import http.server
import io
import json
import os
import subprocess
import sys
import threading
import time
import unittest
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import vault  # noqa: E402

SECRET = "p@ss w/rd\"+9Zq"
RECORDS = {"app/db": {"password": SECRET, "user": "svc"}}
VERSIONS = {}
META = {}  # path -> custom_metadata
META_SETTINGS = {}  # path -> version settings as last POSTed
SEEN = []  # (method, path) of every request, to prove which endpoints an operation used
DELAY = 0.0  # seconds every GET waits, to make a build slow enough for concurrent processes to overlap
NO_SUBKEYS = set()  # paths whose subkeys read is denied (403), like a token without that right


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.headers.get("X-Vault-Token") != "t0k":
            self.send_response(403)
            self.end_headers()
            return
        p = urllib.parse.unquote(self.path.split("?")[0])
        SEEN.append(("GET", self.path))
        if DELAY:
            time.sleep(DELAY)
        if p == "/v1/sys/internal/ui/mounts":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"data": {"secret": {"secret/": {"type": "kv", "options": {"version": "2"}},
                                                             "cubbyhole/": {"type": "cubbyhole"}}}}).encode())
            return
        if p.startswith("/v1/secret/metadata/") and "list=true" in self.path:
            pre = p[len("/v1/secret/metadata/"):]
            keys = sorted({(k[len(pre):].split("/")[0] + ("/" if "/" in k[len(pre):] else ""))
                           for k in RECORDS if k.startswith(pre)})
            self.send_response(200 if keys else 404)
            self.end_headers()
            self.wfile.write(json.dumps({"data": {"keys": keys}}).encode())
            return
        for kind in ("subkeys", "metadata"):
            pre = f"/v1/secret/{kind}/"
            if p.startswith(pre):
                key = p[len(pre):]
                code = 404 if key not in RECORDS else 403 if kind == "subkeys" and key in NO_SUBKEYS else 200
                self.send_response(code)
                self.end_headers()
                if code == 200:
                    d = ({"subkeys": {k: None for k in RECORDS[key]}, "metadata": {"version": 1}} if kind == "subkeys"
                         else {"custom_metadata": META.get(key), "current_version": VERSIONS.get(key, 1),
                               **META_SETTINGS.get(key, {"max_versions": 7, "cas_required": True, "delete_version_after": "720h0m0s"})})
                    self.wfile.write(json.dumps({"data": d}).encode())
                return
        prefix = "/v1/secret/data/"
        rec = RECORDS.get(p[len(prefix):]) if p.startswith(prefix) else None
        if rec is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()
        ver = VERSIONS.get(p[len(prefix):], 1)
        self.wfile.write(json.dumps({"data": {"data": rec, "metadata": {"version": ver}}}).encode())

    def do_POST(self):
        if self.headers.get("X-Vault-Token") != "t0k":
            self.send_response(403)
            self.end_headers()
            return
        p = urllib.parse.unquote(self.path.split("?")[0])
        SEEN.append(("POST", self.path))
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if p.startswith("/v1/secret/metadata/"):
            key = p[len("/v1/secret/metadata/"):]
            META[key] = body["custom_metadata"]
            META_SETTINGS[key] = {k: body.get(k) for k in vault.META_KEEP}  # what a full-replace POST would leave behind
            self.send_response(204)
            self.end_headers()
            return
        key = p[len("/v1/secret/data/"):]
        cur = VERSIONS.get(key, 0)
        if body["options"]["cas"] != cur:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"errors":["check-and-set parameter did not match the current version"]}')
            return
        RECORDS[key] = body["data"]
        VERSIONS[key] = cur + 1
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps({"data": {"version": cur + 1}}).encode())

    def log_message(self, *a):
        pass


def feed_all(red, data, size):
    out = b""
    for i in range(0, len(data), size):
        out += red.feed(data[i:i + size])
    return out + red.finish()


class RedactionTests(unittest.TestCase):
    def test_all_encodings_and_every_chunk_size(self):
        forms = [SECRET, urllib.parse.quote(SECRET, safe=""), urllib.parse.quote_plus(SECRET),
                 json.dumps(SECRET)[1:-1], base64.b64encode(SECRET.encode()).decode(),
                 base64.urlsafe_b64encode(SECRET.encode()).decode().rstrip("=")]
        for form in forms:
            data = ("before " + form + " after\n").encode()
            for size in (1, 2, 3, 5, 7, 64, 4096):
                out = feed_all(vault.Redactor([SECRET]), data, size)
                self.assertNotIn(form.encode(), out, (form, size))
                self.assertIn(b"***", out)
                self.assertTrue(out.startswith(b"before ") and out.rstrip().endswith(b"after"))

    def test_token_shapes(self):
        for s in (b"ghp_" + b"A" * 36, b"github_pat_" + b"B" * 30, b"hvs." + b"C" * 40,
                  b"eyJhbGciOiJIUzI1NiJ9." b"eyJzdWIiOiIxMjM0In0.abcdefghijk"):
            for size in (1, 9, 4096):
                out = feed_all(vault.Redactor([]), b"x " + s + b" y", size)
                self.assertNotIn(s, out)
        out = feed_all(vault.Redactor([]), b"password=hunter22 ok", 3)
        self.assertEqual(out, b"password=*** ok")

    def test_plain_output_untouched(self):
        self.assertEqual(feed_all(vault.Redactor(["abcd1234"]), b"hello world\n", 2), b"hello world\n")


class FakeVaultTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        os.environ["VAULT_ADDR"] = "http://127.0.0.1:%d" % cls.srv.server_port
        os.environ["VAULT_TOKEN"] = "t0k"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = vault.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def run_child(self, argv):
        p = subprocess.run([sys.executable, vault.__file__, *argv], capture_output=True, env=dict(os.environ))
        return p.returncode, p.stdout, p.stderr

    def test_check_and_fields_never_show_value(self):
        rc, out, _ = self.run_main(["check", "secret/app/db#password", "secret/app/db#nope"])
        self.assertEqual(rc, 1)
        self.assertIn("secret/app/db#password: var (%d karakter)" % len(SECRET), out)
        self.assertIn("secret/app/db#nope: yok", out)
        self.assertNotIn(SECRET, out)
        rc, out, _ = self.run_main(["fields", "secret/app/db"])
        self.assertEqual(out.split(), ["password", "user"])
        self.assertNotIn(SECRET, out)

    def test_missing_record_error(self):
        rc, out, _ = self.run_main(["check", "secret/none/x#a"])
        self.assertEqual(rc, 1)
        self.assertIn("record not found", out)

    def test_refused_subcommands(self):
        for w in ("print", "get", "cat"):
            rc, _, err = self.run_main([w, "secret/app/db#password"])
            self.assertEqual(rc, 2)
            self.assertNotIn(SECRET, err)

    def test_run_redacts_stdout_stderr_and_exit_code(self):
        code = ("import os,sys,urllib.parse;v=os.environ['DBPW'];print('pw='+v);print(urllib.parse.quote(v,safe=''));"
                "sys.stderr.write('err '+v+'\\n');sys.exit(7)")
        rc, out, err = self.run_child(["run", "--env", "DBPW=secret/app/db#password", "--", sys.executable, "-c", code])
        self.assertEqual(rc, 7)
        for stream in (out, err):
            self.assertNotIn(SECRET.encode(), stream)
            self.assertNotIn(urllib.parse.quote(SECRET, safe="").encode(), stream)
        self.assertIn(b"***", out)
        self.assertIn(b"err ***", err)

    def test_env_name_defaults_to_field_name(self):
        code = "import os;print('set' if os.environ.get('PASSWORD') else 'unset')"
        rc, out, _ = self.run_child(["run", "--env", "secret/app/db#password", "--", sys.executable, "-c", code])
        self.assertEqual((rc, out.strip()), (0, b"set"))

    def test_run_file_is_deleted_and_content_not_printed(self):
        code = "import os;p=os.environ['PWF'];print(p);print(open(p).read())"
        rc, out, _ = self.run_child(["run", "--file", "PWF=secret/app/db#password", "--", sys.executable, "-c", code])
        self.assertEqual(rc, 0)
        self.assertNotIn(SECRET.encode(), out)
        path = out.decode().splitlines()[0]
        self.assertFalse(os.path.exists(path))

    def test_run_unknown_field_fails_before_running(self):
        rc, out, err = self.run_child(["run", "--env", "X=secret/app/db#zzz", "--", sys.executable, "-c", "print('ran')"])
        self.assertEqual(rc, 3)
        self.assertNotIn(b"ran", out)
        self.assertIn(b"field not found", err)


class WriteTests(unittest.TestCase):
    setUpClass = FakeVaultTests.setUpClass
    tearDownClass = FakeVaultTests.tearDownClass
    run_main = FakeVaultTests.run_main
    run_child = FakeVaultTests.run_child

    def setUp(self):
        RECORDS["w/rec"] = {"keep": "other-value-1"}
        VERSIONS["w/rec"] = 3
        RECORDS.pop("w/new", None)
        VERSIONS.pop("w/new", None)
        self.clip = {"v": "tok-ABCDEFGH-12345\r\n"}
        self._r, self._c = vault.clipboard_read, vault.clipboard_clear
        vault.clipboard_read = lambda: self.clip["v"]
        vault.clipboard_clear = lambda: self.clip.update(v="") or True

    def tearDown(self):
        vault.clipboard_read, vault.clipboard_clear = self._r, self._c

    def test_pano_merges_clears_clipboard_prints_no_value(self):
        rc, out, err = self.run_main(["write", "secret/w/rec#token", "--clipboard"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "yazildi: secret/w/rec#token (18 karakter, sha256 %s, surum 4)" % hashlib.sha256(b"tok-" b"ABCDEFGH-12345").hexdigest()[:8])
        self.assertNotIn("ABCDEFGH", out + err)
        self.assertEqual(RECORDS["w/rec"], {"keep": "other-value-1", "token": "tok-" "ABCDEFGH-12345"})
        self.assertEqual(self.clip["v"], "")

    def test_clipboard_keep_keeps_clipboard(self):
        self.run_main(["write", "secret/w/rec#token", "--clipboard", "--keep-clipboard"])
        self.assertNotEqual(self.clip["v"], "")

    def test_creates_new_path(self):
        rc, out, _ = self.run_main(["write", "secret/w/new#token", "--clipboard"])
        self.assertEqual((rc, VERSIONS["w/new"]), (0, 1))
        self.assertIn("surum 1", out)

    def test_cas_conflict_leaves_data_and_clipboard(self):
        orig = vault._read_v2
        vault._read_v2 = lambda p: ({"keep": "other-value-1"}, 2)  # stale version
        try:
            rc, out, err = self.run_main(["write", "secret/w/rec#token", "--clipboard"])
        finally:
            vault._read_v2 = orig
        self.assertEqual(rc, 3)
        self.assertIn("check-and-set", err)
        self.assertNotIn("ABCDEFGH", out + err)
        self.assertNotIn("token", RECORDS["w/rec"])
        self.assertNotEqual(self.clip["v"], "")

    def test_refuses_short_empty_multiline(self):
        for bad in ("", "  \n", "short", "abcdefgh\nijklmnop"):
            self.clip["v"] = bad
            rc, out, err = self.run_main(["write", "secret/w/rec#token", "--clipboard"])
            self.assertEqual(rc, 3, bad)
            self.assertNotIn("abcdefgh", out + err)
        self.assertNotIn("token", RECORDS["w/rec"])

    def test_hidden_not_allows_short_non_secret_value(self):
        old = sys.stdin
        try:
            sys.stdin = io.StringIO("993\n")
            rc, out, err = self.run_main(["write", "secret/w/rec#imap_port", "--stdin"])
            self.assertEqual(rc, 3)                       # still refused without the flag, with a hint
            self.assertIn("--not-secret", err)
            sys.stdin = io.StringIO("993\n")
            rc, out, err = self.run_main(["write", "secret/w/rec#imap_port", "--stdin", "--not-secret"])
            self.assertEqual(rc, 0, err)
            self.assertEqual(RECORDS["w/rec"], {"keep": "other-value-1", "imap_port": "993"})
            for bad in ("", "  \n", "99\n3"):             # empty and multi-line-on-clipboard stay refused
                self.clip["v"] = bad
                rc, _, _ = self.run_main(["write", "secret/w/rec#p2", "--clipboard", "--not-secret"])
                self.assertEqual(rc, 3, bad)
            self.assertNotIn("p2", RECORDS["w/rec"])
        finally:
            sys.stdin = old

    def test_hidden_not_subcommand_run_stops_redacting_only_that_field(self):
        RECORDS["w/rec"] = {"user": "9876543210", "password": "pw-Secret-4321"}
        META.pop("w/rec", None)
        code = "import os;print(os.environ['KUL']);print(os.environ['PASSWORD'])"
        argv = ["run", "--env", "KUL=secret/w/rec#kullanici", "--env", "PAROLA=secret/w/rec#parola", "--", sys.executable, "-c", code]
        rc, out, _ = self.run_child(argv)
        self.assertEqual(out.decode().split(), ["***", "***"])          # both masked before the mark
        rc, out, err = self.run_main(["not-secret", "secret/w/rec#kullanici"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(META["w/rec"], {"not_secret": "user"})
        self.assertEqual(RECORDS["w/rec"]["user"], "9876543210")   # data untouched
        rc, out, _ = self.run_child(argv)
        self.assertEqual(out.decode().split(), ["9876543210", "***"])   # only the marked field is shown, parola still masked
        self.assertNotIn(b"pw-Secret-4321", out)
        rc, _, err = self.run_main(["not-secret", "secret/w/rec#yok"])
        self.assertEqual(rc, 3)
        self.assertIn("field not found", err)
        self.run_main(["not-secret", "secret/w/rec#kullanici", "--undo"])
        self.assertEqual(META["w/rec"], {})
        rc, out, _ = self.run_child(argv)
        self.assertEqual(out.decode().split(), ["***", "***"])

    def test_write_hidden_not_marks_the_field_and_keeps_other_metadata(self):
        META["w/rec"] = {"description": "Test kaydi"}
        old = sys.stdin
        try:
            sys.stdin = io.StringIO("993\n")
            rc, _, err = self.run_main(["write", "secret/w/rec#imap_port", "--stdin", "--not-secret"])
        finally:
            sys.stdin = old
        self.assertEqual(rc, 0, err)
        self.assertEqual(META["w/rec"], {"description": "Test kaydi", "not_secret": "imap_port"})

    def test_description_field_is_mirrored_to_metadata(self):
        META.pop("w/rec", None)
        old = sys.stdin
        sys.stdin = io.StringIO("Yedek okuma tokeni\n")
        try:
            rc, _, _ = self.run_main(["write", "secret/w/rec#aciklama", "--stdin"])
        finally:
            sys.stdin = old
        self.assertEqual(rc, 0)
        self.assertEqual(META["w/rec"], {"description": "Yedek okuma tokeni"})
        self.run_main(["delete", "secret/w/rec#aciklama"])
        self.assertEqual(META["w/rec"], {})
        SEEN.clear()
        sys.stdin = io.StringIO("other-secret-123\n")
        try:
            self.run_main(["write", "secret/w/rec#token", "--stdin"])
        finally:
            sys.stdin = old
        self.assertEqual([u for m, u in SEEN if "/metadata/" in u], [])  # non-description fields never touch metadata

    def test_stdin_and_sil(self):
        old = sys.stdin
        sys.stdin = io.StringIO("stdin-secret-9999\n")
        try:
            rc, out, _ = self.run_main(["write", "secret/w/rec#t2", "--stdin"])
        finally:
            sys.stdin = old
        self.assertEqual(rc, 0)
        self.assertNotIn("stdin-secret", out)
        rc, out, _ = self.run_main(["delete", "secret/w/rec#t2"])
        self.assertEqual(RECORDS["w/rec"], {"keep": "other-value-1"})
        self.assertNotIn("stdin-secret", out)

    PEM = "-----BEGIN " "PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEF\nAASCBKcwggSjAgEAAoIBAQC7VJTUt9Us\n-----END PRIVATE KEY-----"

    def _assert_quiet(self, out, err):
        for frag in ("MIIEvQIB", "AASCBKcw", "BEGIN PRIVATE", "END PRIVATE"):
            self.assertNotIn(frag, out + err)

    def test_from_file_multiline_roundtrip_preserves_fields(self):
        with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False, newline="") as fh:
            fh.write(self.PEM.replace("\n", "\r\n") + "\r\n")  # CRLF + trailing newline
        try:
            rc, out, err = self.run_main(["write", "secret/w/rec#key", "--from-file", fh.name])
        finally:
            os.unlink(fh.name)
        self.assertEqual(rc, 0)
        self.assertEqual(RECORDS["w/rec"], {"keep": "other-value-1", "key": self.PEM})
        sha = hashlib.sha256(self.PEM.encode()).hexdigest()[:8]
        self.assertEqual(out.strip(), f"yazildi: secret/w/rec#key ({len(self.PEM)} karakter, sha256 {sha}, surum 4)")
        self._assert_quiet(out, err)

    def test_stdin_multiline(self):
        old = sys.stdin
        sys.stdin = io.StringIO(self.PEM + "\n")
        try:
            rc, out, err = self.run_main(["write", "secret/w/rec#key", "--stdin"])
        finally:
            sys.stdin = old
        self.assertEqual(rc, 0)
        self.assertEqual(RECORDS["w/rec"]["key"], self.PEM)
        self.assertEqual(RECORDS["w/rec"]["keep"], "other-value-1")
        self._assert_quiet(out, err)

    def test_from_file_missing_file_and_short_value_error_quietly(self):
        rc, out, err = self.run_main(["write", "secret/w/rec#key", "--from-file", "/nonexistent/x.pem"])
        self.assertEqual(rc, 3)
        with tempfile.NamedTemporaryFile("w", delete=False) as fh:
            fh.write("abc")
        try:
            rc, out, err = self.run_main(["write", "secret/w/rec#key", "--from-file", fh.name])
        finally:
            os.unlink(fh.name)
        self.assertEqual(rc, 3)
        self.assertNotIn("key", RECORDS["w/rec"])

    def test_run_redaction_still_works(self):
        rc, out, _ = self.run_child(["run", "--env", "DBPW=secret/app/db#password", "--", sys.executable, "-c",
                                     "import os;print(os.environ['DBPW'])"])
        self.assertNotIn(SECRET.encode(), out)


if __name__ == "__main__":
    unittest.main()


class BuildLockTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.saved = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE")}
        os.environ.update(HOME=self.home, USERPROFILE=self.home)

    def tearDown(self):
        for k, v in self.saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

    def test_second_holder_times_out_then_gets_it_after_release(self):
        with vault.BuildLock(timeout=1) as a:
            self.assertTrue(a.acquired)
            if os.name != "nt":
                self.assertEqual(os.stat(vault.lock_file()).st_mode & 0o777, 0o600)
            t0 = time.time()
            with vault.BuildLock(timeout=0.5) as b:
                self.assertFalse(b.acquired)  # timeout: the caller builds without the lock
            self.assertGreaterEqual(time.time() - t0, 0.5)
        with vault.BuildLock(timeout=1) as c:
            self.assertTrue(c.acquired)

    def test_lock_of_a_dead_process_is_taken_over(self):
        code = ("import sys; sys.path.insert(0, %r); import vault; l = vault.BuildLock().__enter__(); "
                "assert l.acquired; import os; os._exit(0)" % os.path.join(os.path.dirname(__file__), ".."))
        subprocess.run([sys.executable, "-c", code], check=True)  # exits holding the lock; the OS drops it
        with vault.BuildLock(timeout=1) as b:
            self.assertTrue(b.acquired)


class BulTests(unittest.TestCase):
    """vault.py find: path / field name / description search, values never printed, cache 0600."""

    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.env = {k: os.environ.get(k) for k in ("VAULT_ADDR", "VAULT_TOKEN", "HOME", "USERPROFILE", "VAULT_FIND_EXCLUDING")}
        cls.home = tempfile.mkdtemp()
        os.environ.update(VAULT_ADDR="http://127.0.0.1:%d" % cls.srv.server_port, VAULT_TOKEN="t0k", HOME=cls.home, USERPROFILE=cls.home,
                          VAULT_FIND_EXCLUDING="secret/arsiv/")
        RECORDS["panel/read-token"] = {"description": "Panel okuma PATi (salt okuma, izinli)", "token": "tok-VALUE-1234567"}
        RECORDS["panel/publish-token"] = {"description": "publish PATi", "token": "tok-" "OTHER-7654321"}
        RECORDS["arsiv/yedek"] = {"password": "tok-ARSIV-1111111"}
        META["panel/read-token"] = {"description": "Panel okuma PATi (salt okuma, izinli)"}  # migrated record

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        for k in ("panel/read-token", "panel/publish-token", "arsiv/yedek"):
            RECORDS.pop(k, None)
            META.pop(k, None)
        for k, v in cls.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def find(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = vault.main(["find", *argv])
        return rc, out.getvalue(), err.getvalue()

    def test_finds_path_by_all_keywords_and_never_prints_values(self):
        rc, out, _ = self.find("panel", "okuma")
        self.assertEqual(rc, 0)
        self.assertIn("secret/panel/read-token", out)
        self.assertNotIn("publish-token", out)  # "okuma" is not in that record
        self.assertIn("fields: aciklama, token", out)
        self.assertIn("Panel okuma", out)  # the description field is shown
        self.assertNotIn("VALUE", out)
        self.assertNotIn("OTHER", out)

    def test_turkish_case_folding_and_description_match(self):
        self.assertIn("read-token", self.find("İZİNLİ", "OKUMA")[1])
        self.assertIn("read-token", self.find("salt")[1])           # description only
        self.assertIn("read-token", self.find("AÇIKLAMA", "okuma")[1])  # field name, diacritics folded

    def test_no_match_is_rc1_with_hint(self):
        rc, out, _ = self.find("zzzqq")
        self.assertEqual(rc, 1)
        self.assertIn("no record matches", out)

    def test_cache_holds_names_not_values_and_is_private(self):
        self.find("panel")
        path = vault.cache_file()
        raw = open(path, encoding="utf-8").read()
        self.assertNotIn("tok-VALUE", raw)
        self.assertNotIn("tok-OTHER", raw)
        if os.name != "nt":
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_unreachable_vault_without_cache_fails_gracefully(self):
        saved = os.environ["VAULT_ADDR"]
        os.environ["VAULT_ADDR"] = "http://127.0.0.1:1"
        try:
            rc, out, err = self.find("panel")
        finally:
            os.environ["VAULT_ADDR"] = saved
        self.assertEqual(rc, 3)
        self.assertIn("NOT evidence", err)


    def test_index_build_reads_no_values_and_skips_offline_tier(self):
        SEEN.clear()
        rc, out, err = self.find("--refresh", "panel")
        self.assertEqual(rc, 0)
        data_reads = [u for m, u in SEEN if "/v1/secret/data/" in u]
        self.assertEqual(data_reads, [])  # field names from subkeys, description from custom_metadata
        self.assertTrue(any("/v1/secret/subkeys/" in u for _, u in SEEN))
        self.assertEqual([u for _, u in SEEN if "arsiv" in u], [])  # never listed, never read
        self.assertNotIn("arsiv", out)
        self.assertIn("fields: aciklama, token", out)

    def test_offline_tier_query_gets_a_hint(self):
        rc, out, err = self.find("arsiv")
        self.assertEqual(rc, 1)
        self.assertIn("secret/arsiv/ is never indexed", err)
        self.assertNotIn("ARSIV-1111", out + err)

    def test_old_format_cache_is_replaced_not_used(self):
        import time
        path = vault.cache_file()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:  # format 1: no "v", fresh, same address
            json.dump({"records": [{"p": "secret/old/rec", "f": ["x"], "d": "old-desc-MARKER"}], "skipped": 0,
                       "addr": os.environ["VAULT_ADDR"], "built": time.time()}, f)
        rc, out, _ = self.find("old")
        self.assertEqual(rc, 1)  # the old record is not served
        with open(path, encoding="utf-8") as f:
            raw = f.read()
        self.assertNotIn("MARKER", raw)
        self.assertEqual(json.loads(raw)["v"], vault.INDEX_VERSION)

    def test_five_cold_processes_build_once(self):
        global DELAY
        os.remove(vault.cache_file()) if os.path.exists(vault.cache_file()) else None
        env = dict(os.environ, VAULT_REQUEST_COUNTER="1")
        script = os.path.join(os.path.dirname(__file__), "..", "vault.py")
        DELAY = 0.15
        try:
            procs = [subprocess.Popen([sys.executable, script, "find", "panel", "okuma"], env=env, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True) for _ in range(5)]
            res = [p.communicate(timeout=60) + (p.returncode,) for p in procs]
        finally:
            DELAY = 0.0
        self.assertEqual([r[2] for r in res], [0] * 5)
        self.assertTrue(all("read-token" in r[0] for r in res))
        builders = [r for r in res if "list=" in r[1]]
        self.assertEqual(len(builders), 1)  # one process built; the other four read its cache (no Vault requests)
        self.assertEqual(sum("none (cache)" in r[1] for r in res), 4)

    def test_names_on_demand_when_subkeys_denied(self):
        NO_SUBKEYS.add("panel/publish-token")
        try:
            self.find("--refresh", "okuma")  # the publish record is indexed but not shown
            with open(vault.cache_file(), encoding="utf-8") as f:
                idx = json.load(f)
            rec = next(r for r in idx["records"] if r["p"] == "secret/panel/publish-token")
            self.assertIsNone(rec["f"])  # path only after the build
            SEEN.clear()
            rc, out, _ = self.find("publish")
            self.assertEqual(rc, 0)
            self.assertIn("fields: aciklama, token", out)  # read on demand for the shown match only
            self.assertEqual([u for _, u in SEEN if "/v1/secret/data/" in u], ["/v1/secret/data/panel/publish-token"])
            self.assertNotIn("OTHER", out)
            SEEN.clear()
            self.find("publish")
            self.assertEqual(SEEN, [])  # names were saved in the cache
        finally:
            NO_SUBKEYS.discard("panel/publish-token")

    def test_description_move_dry_run_then_apply(self):
        def run(*argv):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = vault.main(["migrate-descriptions", *argv])
            return rc, out.getvalue(), err.getvalue()
        META.pop("panel/publish-token", None)
        SEEN.clear()
        rc, out, _ = run()
        self.assertEqual(rc, 0)
        self.assertIn("tasinacak: secret/panel/publish-token", out)
        self.assertNotIn("read-token", out)  # already has a metadata description
        self.assertEqual([u for m, u in SEEN if m == "POST" or "/v1/secret/data/" in u], [])  # dry run reads/writes nothing
        self.assertEqual([u for _, u in SEEN if "arsiv" in u], [])
        rc, out, _ = run("--apply")
        self.assertIn("tasindi: secret/panel/publish-token", out)
        self.assertEqual(META["panel/publish-token"], {"description": "publish PATi"})
        self.assertEqual(META_SETTINGS["panel/publish-token"],  # version settings survive the metadata write
                         {"max_versions": 7, "cas_required": True, "delete_version_after": "720h0m0s"})
        self.assertNotIn("OTHER", out)
        rc, out, _ = self.find("--refresh", "publish")
        self.assertIn("publish PATi", out)
        rc, out, _ = run()
        self.assertIn("-- 0 record(s)", out)

    def test_request_counter(self):
        os.environ["VAULT_REQUEST_COUNTER"] = "1"
        try:
            vault.REQUESTS.clear()
            _, _, err = self.find("--refresh", "panel")
        finally:
            os.environ.pop("VAULT_REQUEST_COUNTER", None)
        self.assertIn("vault: Vault requests: ", err)
        self.assertIn("subkeys=", err)
        self.assertNotRegex(err, r"\bdata=")
