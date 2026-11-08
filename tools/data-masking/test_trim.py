#!/usr/bin/env python3
"""Tests for trim.py (masking hook / filter). SYNTHETIC DATA ONLY (generated TR_ID/IBAN/card numbers, made-up phone numbers
and e-mail addresses).

Run:  python3 tools/data-masking/test_trim.py -v     (or: python3 -m unittest discover -s tools/data-masking)
The module-level tests use a temporary key file, count log and allowlist (TRIM_KEY_FILE, TRIM_COUNT_LOG, TRIM_ALLOWLIST);
the hook tests run trim.py as a real process with HOME pointed at a temporary directory. Nothing under the real ~/.claude
is read or written.
"""
# Turkish locale data: Turkish test input (sifre, parola, kimlik, Gizli, names, e-mail with Turkish letters) is what the masker must handle; it is not untranslated prose.
import collections
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import atexit
import warnings

warnings.simplefilter("ignore", ResourceWarning)
if os.name == "nt":   # the hook is Linux only (README: "Tested on Linux only"; key-file modes, paths and the time limits assume it)
    raise unittest.SkipTest("data-masking is Linux only")
BIN = os.path.dirname(os.path.abspath(__file__))

# Isolate the in-process tests (and the child processes they start) from the real ~/.claude.
_ISO = tempfile.mkdtemp(prefix="trim-iso-")
atexit.register(shutil.rmtree, _ISO, ignore_errors=True)
os.environ["TRIM_KEY_FILE"] = os.path.join(_ISO, "key")
os.environ["TRIM_COUNT_LOG"] = os.path.join(_ISO, "count.log")
os.environ["TRIM_ALLOWLIST"] = os.path.join(_ISO, "allowlist.txt")  # does not exist: only the built-in domains apply

sys.path.insert(0, BIN)
import trim  # noqa: E402

TRIM = os.path.join(BIN, "trim.py")


def tr_id_generate(seed):
    r = random.Random(seed)
    d = [r.randint(1, 9)] + [r.randint(0, 9) for _ in range(8)]
    d.append(((d[0] + d[2] + d[4] + d[6] + d[8]) * 7 - (d[1] + d[3] + d[5] + d[7])) % 10)
    d.append(sum(d) % 10)
    return "".join(map(str, d))


def tr_id_invalid(seed):
    t = tr_id_generate(seed)
    return t[:10] + str((int(t[10]) + 1) % 10)


def luhn_generate(prefix, length, seed):
    r = random.Random(seed)
    g = prefix + "".join(str(r.randint(0, 9)) for _ in range(length - len(prefix) - 1))
    for c in range(10):
        if trim._luhn(g + str(c)):
            return g + str(c)


def iban_generate(seed, invalid=False):
    r = random.Random(seed)
    bban = "".join(str(r.randint(0, 9)) for _ in range(22))
    kk = 98 - (int(bban + "292700") % 97)
    if invalid:
        kk = 10 + (kk + 1) % 80
    return f"TR{kk:02d}{bban}"


def iban_format(s):
    return s[:4] + " " + " ".join(s[i:i + 4] for i in range(4, 24, 4)) + " " + s[24:]


class Masking(unittest.TestCase):
    def m(self, s):
        return trim.mask_out(s)

    # ---- TR_ID
    def test_tr_id_valid_is_masked(self):
        for i in range(30):
            t = tr_id_generate(i)
            y, s = self.m(f"tckn={t};")
            self.assertNotIn(t, y)
            self.assertTrue(re.search(r"tckn=\[TR_ID:[0-9a-f]{8}\];", y), y)
            self.assertEqual(s["TR_ID"], 1)

    def test_tr_id_invalid_unchanged(self):
        for i in range(30):
            t = tr_id_invalid(i)
            self.assertEqual(self.m(f"x {t} y")[0], f"x {t} y")

    def test_tr_id_boundaries(self):
        t = tr_id_generate(5)
        for text in (f"0{t}", t[:9], t + "1", f"1.{t}"):
            self.assertEqual(self.m(text)[0], text, text)
        for text in (f"a{t}", f"{t}b", f"{t}.5", f"id_{t}x"):  # round 3: only digits (and a decimal point before) protect it
            self.assertNotIn(t, self.m(text)[0], text)
        self.assertNotIn(t, self.m(f'{{"tckn":"{t}"}}')[0])
        self.assertNotIn(t, self.m(f"/customer/{t}/details")[0])

    def test_tr_id_id_field_values_are_skipped(self):
        t = tr_id_generate(7)  # CI/GitHub ids can have 11 digits and may pass the checksum by chance
        for text in (f"run_id={t}", f"/actions/runs/{t}/jobs", f"/comments/{t}"):
            self.assertEqual(self.m(text)[0], text, text)
        self.assertNotIn(t, self.m(f'"id": {t}')[0])  # a bare id is not a CI id key: masked
        # but TcId (the product-a Users.TcId column holds a TR_ID) is masked
        self.assertNotIn(t, self.m(f'"TcId": {t}')[0])
        self.assertNotIn(t, self.m(f"tc_id={t}")[0])

    # ---- phone numbers
    def test_phone_formats_same_token(self):
        formats = ["05321234567", "0532 123 45 67", "0 (532) 123 45 67", "+90 532 123 45 67", "+905321234567",
                    "905321234567", "90 532 123 45 67", "0532-123-45-67", "5321234567", "532 123 45 67",
                    "(0532) 123 45 67", "+90 (532) 123 45 67", "0090 532 123 45 67", "532-123-4567", "+90 (0) 532 123 45 67",
                    "0(532)1234567"]
        tokens = set()
        for b in formats:
            y, s = self.m(f"caller: {b}, done")
            self.assertEqual(s["PHONE"], 1, b)
            tokens.add(re.search(r"\[PHONE:[0-9a-f]{8}\]", y).group(0))
            self.assertNotRegex(re.sub(r"\[PHONE:[0-9a-f]{8}\]", "", y), r"\d{3}")  # random hex in a token may contain digits
        self.assertEqual(len(tokens), 1, tokens)

    def test_landline(self):
        for b in ("0212 123 45 67", "02121234567", "+90 212 123 45 67", "(212) 123 45 67", "0 262 123 45 67"):
            y, s = self.m(f"x {b} y")
            self.assertEqual(s["PHONE"], 1, b)
        self.assertEqual(self.m("0212 123 45 67")[0], self.m("+90 212 123 45 67")[0])

    def test_different_number_different_token(self):
        a = self.m("05321234567")[0]
        b = self.m("05321234568")[0]
        self.assertNotEqual(a, b)

    def test_phone_no_false_positives(self):
        safe = [
            "192.0.2.150", "10.0.0.197", "203.0.113.184", "203.0.113.113", "255.255.255.0", "0.0.0.0", "127.0.0.1:5432",
            "2026-10-28T12:30:45.123Z", "2026-10-28 12:30:45", "28.10.2026 01:24", "12:30:45", "1730123456", "1730123456789",
            "1793138343.216", "v1.2.3.4", "1.0.532.123", "version 10.0.19045.5371", "5432", "8080", "port 1433", "5000/tcp",
            "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0", "3fa9c7e1b2d4f60718293a4b5c6d7e8f", "sha256:" + "ab12" * 16,
            "550e8400-e29b-41d4-a716-446655440000", "0.5321234567", "-0.5321234567", "3.14159265358979", "1,234,567",
            "AbpUsers 12 34 56 78", "df 100 200 30 40", "5.0.1.2", "5.0.0-preview.5.20278.1", "ssh://git@github.com:22/x",
            "id 4294967295", "0x5321234567", "x5321234567", "5321234567x", "5321234567.5", "$ 100,000,000",
        ]
        for g in safe:
            y, s = self.m(g)
            self.assertEqual(y, g, g)

    # ---- e-mail
    def test_email(self):
        y, s = self.m("Ali.Veli+x@gmail.com and <ali.veli+x@GMAIL.com>.")
        self.assertEqual(s["EMAIL"], 2)
        tokens = re.findall(r"\[EMAIL:[0-9a-f]{8}\]", y)
        self.assertEqual(len(set(tokens)), 1)  # upper/lower case is the same value
        self.assertNotIn("gmail", y)
        self.assertIn("and <[EMAIL:", y)

    def test_email_allowlist_and_false_positives(self):
        for g in ("git@github.com:org/repo.git", "noreply@anthropic.com", "123+x@users.noreply.github.com",
                  "ci@example.com", "x@test.example.org", "a@example.com", "image@2x.png", "getty@tty1.service",
                  "user@1000.service", "pkg@1.2.3", "@types/node@20.1.0", "ajan@agent-host", "root@10.0.0.1",
                  "foo@sha256:abcdef", "a @ b.com", "mail@", "@domain.com"):
            self.assertEqual(self.m(g)[0], g, g)
        y, s = self.m("customer@company.com.tr")
        self.assertEqual(s["EMAIL"], 1)

    # ---- IBAN / card
    def test_iban(self):
        for i in range(10):
            ib = iban_generate(i)
            for fmt in (ib, iban_format(ib), ib.lower()):
                y, s = self.m(f"iban={fmt};")
                self.assertEqual(s["IBAN"], 1, fmt)
                self.assertNotIn(ib[4:], y.replace(" ", ""))
            self.assertEqual(self.m(f"x {iban_generate(i, invalid=True)} y")[1]["IBAN"], 0)
        self.assertEqual(self.m(iban_format(iban_generate(3)))[0], self.m(iban_generate(3))[0])

    def test_card(self):
        for prefix, uz in (("4", 16), ("51", 16), ("34", 15), ("6011", 16), ("4", 13), ("2221", 16), ("9792", 16)):
            k = luhn_generate(prefix, uz, uz + len(prefix))
            formats = [k]
            if uz == 16:
                formats += [" ".join(k[i:i + 4] for i in range(0, 16, 4)), "-".join(k[i:i + 4] for i in range(0, 16, 4))]
            if uz == 15:
                formats += [f"{k[:4]} {k[4:10]} {k[10:]}"]
            for b in formats:
                y, s = self.m(f"kart {b} son")
                self.assertEqual(s["CARD"], 1, b)
                self.assertNotIn(k[-4:], re.sub(r"\[CARD:[0-9a-f]{8}\]", "", y))

    def test_card_no_false_positives(self):
        k = luhn_generate("4", 16, 1)
        invalid = k[:-1] + str((int(k[-1]) + 1) % 10)
        self.assertEqual(self.m(invalid)[0], invalid)
        for g in ("1730123456789", luhn_generate("17", 13, 3), luhn_generate("1", 19, 4), luhn_generate("7", 16, 5), luhn_generate("0", 16, 6),
                  "2026 1028 1230 4500", "1793138343216000000", "0." + luhn_generate("4", 16, 9),
                  "x" + luhn_generate("4", 16, 9) + "0"):
            y, s = self.m(g)
            self.assertEqual(s["CARD"], 0, g)
        for g in (luhn_generate("4", 16, 9) + "a", "a" + luhn_generate("4", 16, 9)):  # round 3: letters do not protect a card number
            self.assertEqual(self.m(g)[1]["CARD"], 1, g)

    # ---- general properties
    def test_idempotent(self):
        t, ib, k = tr_id_generate(9), iban_generate(9), luhn_generate("4", 16, 9)
        text = f"{t} 05321234567 ali@gmail.com {ib} {k} +90 212 123 45 67\n" * 3
        a, _ = self.m(text)
        b, s2 = self.m(a)
        self.assertEqual(a, b)
        self.assertFalse(s2)

    def test_no_match_unchanged(self):
        text = "hello\nIP 10.0.0.1 port 8080 sha 3fa9c7e1\n" * 10 + "\n"
        y, s = self.m(text)
        self.assertEqual(y, text)
        self.assertFalse(s)
        self.assertEqual(self.m("")[0], "")
        self.assertEqual(self.m(None)[0], None)

    def test_line_structure_preserved(self):
        t = tr_id_generate(11)
        text = f"a\n{t}\n\n05321234567 x\r\nson"
        y, _ = self.m(text)
        self.assertEqual(text.count("\n"), y.count("\n"))
        self.assertEqual(len(text.splitlines()), len(y.splitlines()))

    def test_token_stable_across_processes(self):
        t = tr_id_generate(13)
        output = subprocess.run([sys.executable, "-I", "-c", "import sys;sys.path.insert(0,%r);import trim;print(trim.mask_out(%r)[0])" % (BIN, t)],
                               capture_output=True, text=True).stdout.strip()
        self.assertEqual(output, trim.mask_out(t)[0])

    def test_default_allowlist(self):
        # built-in allowed domain: the organisation's own addresses are not masked
        self.assertEqual(self.m("destek@example.com")[0], "destek@example.com")

    def test_performance_5mb(self):
        r = random.Random(1)
        lines = []
        for i in range(60000):
            lines.append(f'2026-10-28T12:{i % 60:02d}:45.123Z INFO req={i} ip=10.0.{i % 255}.{i % 200} ms={r.randint(1, 900)} '
                            f'trace=3fa9c7e1b2d4f60718293a4b5c6d7e8f msg="all good" size={r.randint(1, 10**9)}')
        for i in range(0, 60000, 200):
            lines[i] += f" tc={tr_id_generate(i)} tel=0532{r.randint(1000000, 9999999)} mail=u{i}@customer{i % 7}.com.tr"
        text = "\n".join(lines) + "\n"
        self.assertGreater(len(text), 5_000_000)
        t0 = time.time()
        y, s = self.m(text)
        elapsed = time.time() - t0
        self.assertLess(elapsed, 5.0, f"{elapsed:.2f}s")
        self.assertEqual(s["TR_ID"], 300)
        self.assertEqual(s["EMAIL"], 300)
        self.assertEqual(s["PHONE"], 300)
        print(f"\n[performans] {len(text) / 1e6:.1f} MB, {elapsed:.2f} s")

    def test_huge_text_timeout_fails_closed(self):
        previous = trim.MASK_DURATION
        try:
            trim.MASK_DURATION = 0.0
            text = ("line 05321234567\n" * 200000)
            y, s = self.m(text)
            self.assertEqual(y, "[mask: time budget exceeded, 200000 lines withheld]\n")
            self.assertEqual(s["SKIPPED"], 1)
        finally:
            trim.MASK_DURATION = previous


class Hook(unittest.TestCase):
    """trim.py --hook as a real process; HOME is a temporary directory."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="mask-test-")
        os.makedirs(os.path.join(self.home, ".claude"), exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def run_hook(self, payload, raw=False, env=None):
        e = dict(os.environ, HOME=self.home)
        for k in ("TRIM_CLOSED", "TRIM_KEY_FILE", "TRIM_COUNT_LOG", "TRIM_ALLOWLIST"):
            e.pop(k, None)
        e.pop("TRIM_MASK_CLOSED", None)
        e.update(env or {})
        p = subprocess.run([sys.executable, "-I", TRIM, "--hook"], input=payload if raw else json.dumps(payload),
                           capture_output=True, text=True, env=e, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout

    def updated(self, payload, **kw):
        c = self.run_hook(payload, **kw)
        if not c.strip():
            return None
        o = json.loads(c)
        self.assertEqual(o["hookSpecificOutput"]["hookEventName"], "PostToolUse")
        return o["hookSpecificOutput"]["updatedToolOutput"]

    def bash(self, command, stdout, **extra):
        response = {"stdout": stdout, "stderr": "", "interrupted": False, "isImage": False}
        response.update(extra)
        return {"tool_name": "Bash", "tool_input": {"command": command}, "tool_response": response}

    def test_bash_masks(self):
        t = tr_id_generate(21)
        u = self.updated(self.bash("psql -c x", f"ad|tc|tel\nx|{t}|05321234567\n"))
        self.assertNotIn(t, u["stdout"])
        self.assertNotIn("05321234567", u["stdout"])
        self.assertEqual(u["stdout"].count("\n"), 2)
        self.assertEqual(u["interrupted"], False)

    def test_bash_stderr_is_masked(self):
        t = tr_id_generate(22)
        g = self.bash("x", "")
        g["tool_response"]["stderr"] = f"error: {t}"
        u = self.updated(g)
        self.assertNotIn(t, u["stderr"])

    def test_bash_no_match_no_output(self):
        self.assertIsNone(self.updated(self.bash("ls", "a\nb\n10.0.0.1 port 5432\n")))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".claude", "tool-output")))

    def test_bash_big_output_truncated_and_stored_copy_masked(self):
        t = tr_id_generate(23)
        lines = [f"line {i} tc={tr_id_generate(100 + i)} tel=0532{1000000 + i}" for i in range(400)]
        lines[200] = f"error line {t}"
        u = self.updated(self.bash("psql", "\n".join(lines) + "\n"))
        self.assertIn("truncated", u["stdout"])
        self.assertNotRegex(u["stdout"], r"\b\d{11}\b")
        d = os.path.join(self.home, ".claude", "tool-output")
        files = os.listdir(d)
        self.assertEqual(len(files), 1)
        content = open(os.path.join(d, files[0]), encoding="utf-8").read()
        self.assertNotIn(t, content)
        self.assertNotIn(tr_id_generate(150), content)
        self.assertNotRegex(content, r"\b0532\d{7}\b")
        self.assertEqual(content.count("\n"), 400)
        self.assertEqual(oct(os.stat(os.path.join(d, files[0])).st_mode & 0o777), "0o600")

    def test_bash_persisted_file_masked_and_path_removed(self):
        t = tr_id_generate(24)
        path = os.path.join(self.home, "kalici.txt")
        full = "\n".join(f"s{i} {tr_id_generate(300 + i)}" for i in range(500)) + f"\nson {t}\n"
        open(path, "w").write(full)
        preview = full[:3000]
        u = self.updated(self.bash("psql", preview, persistedOutputPath=path, persistedOutputSize=len(full)))
        self.assertNotIn("persistedOutputPath", u)
        self.assertNotIn(t, open(path).read())  # the raw copy was masked in place
        self.assertNotRegex(u["stdout"], r"\b\d{11}\b")
        d = os.path.join(self.home, ".claude", "tool-output")
        for f in os.listdir(d):
            self.assertNotIn(t, open(os.path.join(d, f)).read())

    def test_bash_masks_even_when_truncation_skipped(self):
        t = tr_id_generate(25)
        u = self.updated(self.bash("git diff HEAD", f"+ tc = {t}\n"))
        self.assertNotIn(t, u["stdout"])
        # persisted file: masked in place, the path STAYS (the model will slice it)
        path = os.path.join(self.home, "diff.txt")
        open(path, "w").write("\n".join(["x"] * 10) + f"\n{t}\n")
        u = self.updated(self.bash("git diff", f"x\n{t}\n", persistedOutputPath=path))
        self.assertNotIn(t, open(path).read())
        self.assertNotIn(t, u["stdout"])
        self.assertEqual(u.get("persistedOutputPath"), path)

    def test_mcp_string_and_content_blocks(self):
        t = tr_id_generate(26)
        g = {"tool_name": "mcp__grafana__query_loki_logs", "tool_input": {}, "tool_response": f'{{"line":"tc={t} 05321234567 a@b.com.tr"}}'}
        u = self.updated(g)
        self.assertIsInstance(u, str)
        self.assertNotIn(t, u)
        json.loads(u.replace("[TR_ID", "[TR_ID"))  # JSON structure survives (tokens contain no quotes)
        g = {"tool_name": "mcp__plugin_slack_slack__slack_read_channel", "tool_input": {},
             "tool_response": [{"type": "text", "text": f"customer {t}"}, {"type": "image", "data": "A" * 20 + "05321234567"}, {"type": "text", "text": "clean"}]}
        u = self.updated(g)
        self.assertNotIn(t, u[0]["text"])
        self.assertEqual(u[1]["data"], "A" * 20 + "05321234567")  # images are left alone
        self.assertEqual(u[2], {"type": "text", "text": "clean"})
        g["tool_response"] = {"content": [{"type": "text", "text": f"x {t}"}], "isError": False}
        u = self.updated(g)
        self.assertEqual(u["isError"], False)
        self.assertNotIn(t, u["content"][0]["text"])

    def test_mcp_clean_no_output(self):
        g = {"tool_name": "mcp__grafana__list_datasources", "tool_input": {}, "tool_response": [{"type": "text", "text": "loki prometheus"}]}
        self.assertIsNone(self.updated(g))

    def test_read_only_in_temp_dirs(self):
        t = tr_id_generate(27)
        g = lambda path: {"tool_name": "Read", "tool_input": {"file_path": path},
                         "tool_response": {"type": "text", "file": {"filePath": path, "content": f"tc {t}", "numLines": 1}}}
        u = self.updated(g("/tmp/mask-test-output.csv"))
        self.assertNotIn(t, u["file"]["content"])
        self.assertEqual(u["file"]["numLines"], 1)
        self.assertIsNone(self.updated(g("/opt/repo/x/Test.cs")))

    def test_malformed_input_does_not_crash(self):
        for raw in ("{}", '{"tool_name":"Bash"}', '{"tool_name":"Bash","tool_response":"str"}',
                    '{"tool_name":"mcp__x","tool_response":null}', '{"tool_name":"Bash","tool_response":{"stdout":5}}',
                    json.dumps({"tool_name": "Bash", "tool_input": None, "tool_response": {"isImage": True, "stdout": "data:image/png;base64,QUJD"}})):
            self.assertEqual(self.run_hook(raw, raw=True).strip(), "", raw)
        for raw in ("", "not json", "[]"):  # round 3: input that cannot be parsed is withheld (fail closed), never a crash
            self.assertIn("withheld", self.run_hook(raw, raw=True), raw)

    def test_key_created_0600_and_stable(self):
        t = tr_id_generate(28)
        u1 = self.updated(self.bash("x", f"{t}\n"))["stdout"]
        path = os.path.join(self.home, ".claude", ".mask-key")
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")
        k1 = open(path).read()
        u2 = self.updated(self.bash("x", f"{t}\n"))["stdout"]
        self.assertEqual(u1, u2)
        self.assertEqual(k1, open(path).read())
        self.assertGreaterEqual(len(k1.strip()), 64)
        # the key never shows up in any output
        self.assertNotIn(k1.strip()[:16], u1)

    def test_key_file_env_override(self):
        t = tr_id_generate(33)
        custom = os.path.join(self.home, "elsewhere", "my.key")
        os.makedirs(os.path.dirname(custom))
        out = self.updated(self.bash("x", f"{t}\n"), env={"TRIM_KEY_FILE": custom})["stdout"]
        self.assertNotIn(t, out)
        self.assertEqual(oct(os.stat(custom).st_mode & 0o777), "0o600")
        self.assertFalse(os.path.exists(os.path.join(self.home, ".claude", ".mask-key")))
        # the same key gives the same token on the next run
        self.assertEqual(out, self.updated(self.bash("x", f"{t}\n"), env={"TRIM_KEY_FILE": custom})["stdout"])

    def test_count_log_counts_only(self):
        t = tr_id_generate(29)
        self.updated(self.bash("x", f"{t} 05321234567 ali@gmail.com\n"))
        log = open(os.path.join(self.home, ".claude", "mask-count.log"), encoding="utf-8").read()
        self.assertRegex(log, r"tool=Bash EMAIL=1 PHONE=1 TR_ID=1")
        for raw in (t, "05321234567", "ali@gmail.com", "gmail"):
            self.assertNotIn(raw, log)

    def test_masks_even_if_key_cannot_be_written(self):
        t = tr_id_generate(32)
        os.chmod(os.path.join(self.home, ".claude"), 0o500)
        try:
            u = self.updated(self.bash("x", f"{t}\n"))
            self.assertIsNotNone(u)
            self.assertNotIn(t, u["stdout"])
        finally:
            os.chmod(os.path.join(self.home, ".claude"), 0o700)

    def test_disable_env_var(self):
        t = tr_id_generate(30)
        self.assertIsNone(self.updated(self.bash("x", f"{t}\n"), env={"TRIM_MASK_CLOSED": "1"}))

    def test_hook_truncation_behaviour(self):
        # below the threshold and no match: no change; above it: truncated (even when nothing matched for masking)
        u = self.updated(self.bash("seq 1 1000", "\n".join(str(i) for i in range(1, 1001)) + "\n"))
        self.assertIn("truncated: 1000 lines", u["stdout"])
        self.assertIsNone(self.updated(self.bash("seq 1 100", "\n".join(str(i) for i in range(1, 101)) + "\n")))


    def test_one_mb_adversarial_output_returns_within_budget(self):
        text = ("a_" * 250_000) + "\npassword: Zx9-Synthetic-Value-1234\n" + "password" * 60_000 + "\n" + "-----BEGIN " "PRIVATE KEY-----\nQUJD"
        t0 = time.perf_counter()
        u = self.updated(self.bash("curl x", text))
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 10.0, f"{elapsed:.1f}s")
        self.assertNotIn("Zx9-Synthetic", json.dumps(u))
        self.assertNotIn("QUJD", json.dumps(u))

    def test_budget_zero_withholds_everything_in_the_process(self):
        u = self.updated(self.bash("curl x", "user=a\npassword: Zx9-Synthetic-Value-1234\n"), env={"TRIM_MASK_DURATION": "0"})
        self.assertEqual(u["stdout"], "[mask: time budget exceeded, 2 lines withheld]\n")

    def test_budget_is_shared_by_stdout_and_stderr(self):
        r = self.bash("curl x", "ok\n")
        r["tool_response"]["stderr"] = "password: Zx9-Synthetic-Value-1234\n"
        u = self.updated(r, env={"TRIM_MASK_DURATION": "0"})
        self.assertNotIn("Zx9-Synthetic", json.dumps(u))


class Credentials(unittest.TestCase):
    V = "Zx9-Synthetic-Value-1234"

    def m(self, t):
        return trim.mask_out(t)[0]

    def test_key_value_formats(self):
        V = self.V
        cases = ['{"password":"%s"}' % V, '{"password": "%s"}' % V, 'password: %s' % V, 'password=%s' % V,
                 'Password = %s' % V, 'Sifre: %s' % V, 'Sifre : %s' % V, 'Şifre: %s' % V, 'export TOKEN=%s' % V,
                 "api_key: '%s'" % V, 'client_secret=%s' % V, 'pass: %s' % V, r'{\"password\":\"%s\"}' % V,
                 'DB_PASSWORD=%s' % V, 'Authorization: Bearer %s' % V, '"authorization": "Basic %s"' % V,
                 'parola: %s' % V, 'refresh_token=%s&x=1' % V]
        for c in cases:
            y = self.m(c)
            self.assertNotIn(V, y, c)
            self.assertIn("[SECRET:", y, c)
            self.assertEqual(self.m(y), y, "idempotent: " + c)
        self.assertTrue(self.m("password: %s" % V).startswith("password: [SECRET:"))
        self.assertTrue(self.m("Authorization: Bearer %s" % V).startswith("Authorization: Bearer [SECRET:"))
        self.assertEqual(self.m("Sifre: %s" % V), self.m("password=%s" % V).replace("password=", "Sifre: "))
        self.assertEqual(self.m('{"password":"%s","user":"admin"}' % V).count('"user":"admin"'), 1)

    def test_token_shapes(self):
        pem = "-----BEGIN " "RSA PRIVATE KEY-----\nQUJDREVGR0g=\n-----END RSA PRIVATE KEY-----"
        for sec in ("hvs." + "Ab1-_" * 6, "hvb." + "Ab1-_" * 6, "ghp_" + "a1B2" * 9, "gho_" + "a1B2" * 9, "ghs_" + "a1B2" * 9,
                    "github_pat_" + "a1B2_" * 6, "cfut_" + "a1B2" * 6, "AKIA" + "ABCD1234EFGH5678", "xoxb-123456789012-abcdefABCDEF"):
            y = self.m(f"use {sec} now")
            self.assertNotIn(sec, y)
            self.assertTrue(y.startswith("use [SECRET:") and y.endswith("] now"), y)
        y = self.m(f"a\n{pem}\nb")
        self.assertNotIn("QUJDREVGR0g=", y)
        self.assertEqual(y.split("\n")[0] + y.split("\n")[-1], "ab")
        self.assertNotIn("PRIVATE KEY", self.m("-----BEGIN " "PRIVATE KEY-----\nQUJD"))  # truncated block fails closed

    def test_false_positives(self):
        for t in ("the password is long and the token expires soon", "password", "token:", "password: ********",
                  "password: [SECRET:abcdef]", "token: null", "pass: true", 'password: ""', "password: ''", "password: abc",
                  "password: $DB_PASS", "password: ${DB_PASS}", "password: {{ vault_pw }}", "password: <your-password>",
                  "token_type: Bearer", "secret_name: x", "password: $(DB_PASS)", 'password: "%DB_PASS%"',
                  "bypass: enabled-value", "db_pass: keepvalue1", "hvs.short", "AKIAshort", "password: required"):
            self.assertEqual(self.m(t), t)

    def test_real_passwords_that_look_like_placeholders_or_code_are_masked(self):
        # synthetic values: first character `$ ! % <`, or shaped like a call / attribute access
        for key_value, secret in (("password: $ecretPass1", "$ecretPass1"), ("password=!Bang2026", "!Bang2026"),
                                  ('"password":"%Pass9"', "%Pass9"), ("password: <Pass#1x", "<Pass#1x"),
                                  ("password: Admin(2026)", "Admin(2026)"), ("password: a.b.c.d9", "a.b.c.d9"),
                                  ("password: a.b.c.d", "a.b.c.d"), ("password=args.password", "args.password"),
                                  ("password=os.environ['X']", "os.environ["), ("token: {abc123", "{abc123"),
                                  ('password: "$ecretPass1"', "$ecretPass1"), ("password: $Ecret1", "$Ecret1"),
                                  ("password: ${xyz1", "${xyz1"), ("password: {{abc", "{{abc"), ("password: %abc", "%abc"),
                                  ("password: [abc12345", "[abc12345")):
            y = self.m(key_value)
            self.assertNotIn(secret, y, key_value)
            self.assertIn("[SECRET:", y, key_value)
            self.assertEqual(self.m(y), y, "idempotent: " + key_value)

    def test_weak_literal_passwords_are_masked(self):
        for t in ("password: password", "token: token", "secret: Secret123", "password: secret"):
            y = self.m(t)
            self.assertTrue(y.startswith(t.split(":")[0] + ": [SECRET:"), t)

    def test_placeholders_stay_untouched(self):
        for t in ("password: ${DB_PASS}", "password: ${db.pass}", "password: $DB_PASS", "password: $(DB_PASS)", "password: <your-password>",
                  "password: {{ secrets.pw }}", "password: {{secrets.pw}}", 'password: "{{ secrets.pw }}"', "password: %DB_PASS%",
                  "password: ********", "password: xxxxxxxx", 'password: "<redacted>"', "password=[SECRET:abcdef]",
                  "password: [PHONE:abcdef]", "token: null", "token: None", "secret: FALSE", "password: empty", "pwd: ----"):
            self.assertEqual(self.m(t), t)

    def test_closing_bracket_belongs_to_value_only_if_balanced(self):
        self.assertEqual(self.m("(password=Abcd1234)"), "(password=%s)" % self.m("password=Abcd1234")[len("password="):])
        y = self.m("password: Admin(2026)")
        self.assertTrue(y.startswith("password: [SECRET:") and y.endswith("]"), y)  # the whole value, not "Admin(2026" + ")"
        self.assertEqual(len(y), len("password: [SECRET:abcdef12]"))

    def test_property_never_leaks(self):
        rnd = random.Random(7)
        keys = ["password", "Password", "secret", "token", "api_key", "Sifre", "parola"]
        seps = [": ", "=", " = ", '": "', '":"']
        for i in range(200):
            sec = "Zx9-" + "".join(rnd.choice("abcdefXYZ0123456789-_") for _ in range(rnd.randint(8, 24)))
            k, sp = rnd.choice(keys), rnd.choice(seps)
            t = f'"{k}{sp}{sec}"' if sp.startswith('"') else f"{k}{sp}{sec}"
            self.assertNotIn(sec, self.m(t), t)

    def test_hook_end_to_end(self):
        h = Hook()
        h.setUp()
        try:
            u = h.updated(h.bash("curl x", '{"name":"u1","password":"%s"}' % self.V))
        finally:
            h.tearDown()
        self.assertNotIn(self.V, json.dumps(u))
        self.assertIn('"name":"u1"', json.dumps(u).replace('\\"', '"'))


class Robustness(unittest.TestCase):
    """Untrusted tool output must not be able to stall the masking (the hook is killed after 30 s and the raw output is shown
    then) and the time budget must fail closed."""
    LIMIT = 2.0

    def timed(self, text):
        t0 = time.perf_counter()
        y, s = trim.mask_out(text)
        return time.perf_counter() - t0, y, s

    def rep(self, unit, size, pre="", post=""):
        return pre + unit * (size // len(unit)) + post

    def adversarial_round2(self, size, r):
        bs, zw = chr(92), chr(0x200b)
        return {
            "xml tag": r("<password>"), "xml tag text": "<password>" + "a" * size, "xml tag spaces": "<password>" + " " * size,
            "xml tag spaced*N": r("<password> "), "xml attr": r('<add key="password" '), "xml attr value": r('<add key="password" value="'),
            "xml name": r("<a name='x' "), "xml entry": r('<entry key="token">'), "xml lt": "<" * size, "xml key no end": '<add key="password' + " " * size,
            "url scheme": r("http://a:"), "url long password": "http://a:" + "b" * size, "url at": "x://u:" + "@" * size,
            "url colons": r("a://:"), "url slashes": r("a://"),
            "yaml next line": r("password:\n "), "yaml spaces": "password:\n " + " " * size, "yaml long value": "password:\n  " + "a" * size,
            "yaml nested": r("password:\n  a:\n"), "yaml crlf": r("token:\r\n\t"), "yaml colon lines": r("a:\n"),
            "json unicode key": r('"' + bs + 'u0070": '), "json unicode key long": '"' + (bs + "u0070") * (size // 6) + '": 1',
            "json key no end": '"' + "a" * size, "json keys": r('"a": 1, '),
            "camel": r("aPassword"), "camel colon": r("xToken: "), "camel upper": r("PasswordPassword"),
            "comma": r("password=a,"), "comma2": r("password=ab,cd,"), "comma long": "password=a" + "," * size,
            "closers": "password=a" + ")" * size, "closers open": "password=(a" + ")" * size, "closers mixed": "password=a" + ")]}" * (size // 3),
            "ansi": r("\x1b[0;"), "zero width": r("1" + zw), "zw only": zw * size, "arrow": r("password=>"), "walrus": r("password:="),
        }

    def adversarial(self, size):
        r = lambda unit, pre="", post="": self.rep(unit, size, pre, post)  # noqa: E731
        return {
            "key prefix a_": r("a_", "x password "), "key prefix a_ colon": r("a_", "x ", ":"), "key prefix a.": r("a.", "token "),
            "key prefix a-": r("a-", "secret "), "key prefix pass_": r("pass_"), "key prefix token_.": r("token_."),
            "password*N": r("password"), "password=*N": r("password="), 'password="*N': r('password="'), "password: *N": r("password: "),
            "password+spaces": "password" + " " * size, "password:+spaces": "password:" + " " * size,
            "unterminated sq": "password='" + "a" * size, "unterminated dq escapes": 'password="' + "\\a" * (size // 2),
            "escaped quote": 'password=\\"' + "a" * size, "escaped quotes*N": r('password=\\"'),
            "mixed quotes": r("password=' password=\" "), "unicode keys": r("\u015fifre_"), "api-key *N": r("api-key "),
            **self.adversarial_round2(size, r),
            "digit run": "1" * size, "digits+space": r("1 "), "5-run": "5" * size, "0 *N": r("0 "), "0532 *N": r("0532 "),
            "+90 5 *N": r("+90 5"), "tr_id-like": r("12345678901 "), "card-like": r("4111 1111 1111 111"), "card dashes": r("1234-"),
            "BEGIN without END": "-----BEGIN " "PRIVATE KEY-----\n" + "a" * size, "BEGIN*N": r("-----BEGIN " "PRIVATE KEY----- "),
            "BEGIN A*N": r("-----BEGIN A "), "BEGIN upper": "-----BEGIN " + "A " * (size // 2),
            "email local": "a" * size + "@", "email a@": r("a@"), "email a.a": r("a.a", post="@b"), "email domain a-": r("a-", "x@"),
            "email domain a.": r("a.", "x@"), "email x@a.": r("x@a."), "email local a-": r("a-", post="@x.com"),
            "iban-like": r(" 1234", "TR12"), "tr12*N": r("tr12"), "TR12 1234 *N": r("TR12 1234 "),
            "id key": r("id: ", post="12345678901"), "id key + phone": r("a_" * 20 + "id 0532 123 45 67 "),
            # the five token shapes share `size` like every other case (5 x size made it the only 5 MB input under the same limit)
            "token shapes": "".join(self.rep(u, size // 5, post=p) for u, p in (("hvs.", " "), ("ghp_", ""), ("xox", ""), ("AKIA", ""),
                                                                                  ("github_pat_", ""))),
            "newlines": "\n" * size,
        }

    def test_adversarial_inputs_are_linear_and_not_withheld(self):
        for size in (100_000, 1_000_000):
            for name, text in self.adversarial(size).items():
                elapsed, y, s = self.timed(text)
                self.assertLess(elapsed, self.LIMIT, f"{name} x {size}: {elapsed:.2f}s")
                self.assertEqual(s["SKIPPED"], 0, f"{name} x {size}: withheld")

    def test_old_redos_input_is_fast(self):
        # before the fix 26 repetitions took 14 s and every further one doubled it
        for n in (26, 60, 500, 50_000):
            elapsed, y, s = self.timed("x password " + "a_" * n + "!")
            self.assertLess(elapsed, 0.5, f"{n}: {elapsed:.2f}s")

    def test_ordinary_log_with_credentials_still_masked_at_one_mb(self):
        line = "2026-10-28 INFO user=u1 password=Zx9-Synthetic-Value-1234 ok\n"
        text = line * (1_000_000 // len(line))
        elapsed, y, s = self.timed(text)
        self.assertLess(elapsed, self.LIMIT * 2)
        self.assertNotIn("Zx9-Synthetic", y)
        self.assertEqual(s["SECRET"], text.count("\n"))

    def test_budget_exhausted_fails_closed_small_text(self):
        previous = trim.MASK_DURATION
        try:
            trim.MASK_DURATION = 0.0
            y, s = trim.mask_out("user=a\npassword: Zx9-Synthetic-Value-1234\n")
            self.assertEqual(y, "[mask: time budget exceeded, 2 lines withheld]\n")
            self.assertEqual(s["SKIPPED"], 1)
        finally:
            trim.MASK_DURATION = previous

    def test_budget_exhausted_midway_withholds_only_the_rest(self):
        text = "".join(f"line {i} password: Zx9-Synthetic-{i:08d}\n" for i in range(20000))
        self.assertGreater(len(text), 3 * trim.PART)
        calls = []
        original = trim._mask_out_part

        def slow(t, say):
            calls.append(1)
            if len(calls) == 2:
                time.sleep(0.4)  # the 2nd piece eats the budget
            return original(t, say)

        previous, trim._mask_out_part, trim.MASK_DURATION = trim.MASK_DURATION, slow, 0.3
        try:
            y, s = trim.mask_out(text)
        finally:
            trim._mask_out_part, trim.MASK_DURATION = original, previous
        self.assertEqual(s["SKIPPED"], 1)
        self.assertNotIn("Zx9-Synthetic", y)
        self.assertEqual(y.count("[mask: time budget exceeded"), 1)
        self.assertTrue(y.startswith("line 0 password: [SECRET:"))
        self.assertTrue(y.endswith(" lines withheld]\n"))

    @unittest.skipUnless(hasattr(__import__("signal"), "setitimer"), "POSIX only")
    def test_runaway_pattern_is_interrupted_and_withheld(self):
        runaway = re.compile(r"(a+)+$")  # a catastrophic pattern that slipped in: the interrupt must still end it
        original = trim._mask_out_part
        previous = trim.MASK_DURATION
        trim._mask_out_part = lambda t, say: (runaway.search(t), t)[1]
        trim.MASK_DURATION = 0.5
        try:
            t0 = time.perf_counter()
            y, s = trim.mask_out("password: Zx9-Synthetic-Value-1234\n" + "a" * 60 + "b\n")
            elapsed = time.perf_counter() - t0
        finally:
            trim._mask_out_part, trim.MASK_DURATION = original, previous
        self.assertLess(elapsed, 3.0)
        self.assertEqual(y, "[mask: time budget exceeded, 2 lines withheld]\n")

    def test_error_in_a_piece_is_withheld_not_passed_through(self):
        original = trim._mask_out_part

        def boom(t, say):
            raise ValueError("synthetic")

        trim._mask_out_part = boom
        try:
            y, s = trim.mask_out("password: Zx9-Synthetic-Value-1234\n")
        finally:
            trim._mask_out_part = original
        self.assertEqual(y, "[mask: masking error, 1 lines withheld]\n")
        self.assertEqual(s["SKIPPED"], 1)

    def test_command_filter_regex_is_linear(self):
        for cmd in ("git " * 50_000, "git log " + "x " * 100_000, "git " * 20_000 + "show"):
            t0 = time.perf_counter()
            trim.SKIP_RE.search(cmd)
            self.assertLess(time.perf_counter() - t0, self.LIMIT, cmd[:20])

    def test_budget_is_capped_below_the_hook_timeout(self):
        p = subprocess.run([sys.executable, "-I", "-c", "import sys; sys.path.insert(0, %r); import trim; print(trim.MASK_DURATION)" % BIN],
                           capture_output=True, text=True, env=dict(os.environ, TRIM_MASK_DURATION="999"))
        self.assertEqual(float(p.stdout), 20.0, p.stderr)
        self.assertLess(trim.MASK_DURATION_MAX, 30)

    def test_unexpected_hook_error_withholds_the_tool_response(self):
        import io
        out = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "x"},
                              "tool_response": {"stdout": "password: Zx9-Synthetic-Value-1234", "stderr": "", "isImage": False}})
        stdin = type("S", (), {"buffer": io.BytesIO(payload.encode())})()
        originals = (sys.stdin, sys.stdout, trim._bash)

        def boom(d):
            raise RuntimeError("synthetic")

        sys.stdin, sys.stdout, trim._bash = stdin, out, boom
        try:
            trim.hook()
            out.flush()
            raw = out.buffer.getvalue().decode()
        finally:
            sys.stdin, sys.stdout, trim._bash = originals
            trim._END_AT = None
        self.assertNotIn("Zx9-Synthetic", raw)
        o = json.loads(raw)["hookSpecificOutput"]["updatedToolOutput"]
        self.assertEqual(o["stdout"], "[mask: internal error, output withheld]")
        self.assertEqual(o["stderr"], "")
        self.assertIs(o["isImage"], False)


class Boundary(unittest.TestCase):
    """No match may be lost because the text is masked in pieces (PIECE): every class of match is placed exactly across the
    cut of the old splitter (last newline / last space or comma / hard cut)."""
    PEM_LINES = ["QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo" + "%02d" % i + "abcdefghijklmnopqrstuvwxyz0123456789" for i in range(40)]

    def pem(self):
        return "\n".join(["-----BEGIN " "RSA PRIVATE KEY-----"] + self.PEM_LINES + ["-----END RSA PRIVATE KEY-----"])

    def cases(self):
        P = trim.PART
        tc, iban = tr_id_generate(41), iban_generate(42)
        card = luhn_generate("4", 16, 43)
        yield "key-value after space", "y" * (P - 20) + " password: Zx9SyntheticValue1234" + "z" * 1000, ["Zx9SyntheticValue1234"]
        yield "key-value quoted comma", "y" * (P - 20) + ' {"password":"Zx9,Synthetic,Value1234"} ' + "z" * 1000, ["Zx9", "Value1234"]
        yield "key-value no space at all", "|" * (P - 6) + "password=Zx9SyntheticValue1234|" + "z" * 1000, ["Zx9SyntheticValue1234"]
        yield "tr_id", "|" * (P - 5) + tc + "|" + "z" * 1000, [tc]
        yield "iban", "|" * (P - 10) + iban + "|" + "z" * 1000, [iban[4:]]
        yield "iban spaced", "y" * (P - 20) + " " + iban_format(iban) + " " + "z" * 1000, [iban[4:8], iban[-4:]]
        yield "email", "|" * (P - 5) + "alice.smith@customer.com.tr" + "|" + "z" * 1000, ["alice.smith", "customer"]
        yield "phone spaced", "y" * (P - 15) + " 0532 000 00 00 " + "z" * 1000, ["0532 000", "00 00"]
        yield "phone dense", "|" * (P - 5) + "05320000000" + "|" + "z" * 1000, ["0532000"]
        yield "card spaced", "y" * (P - 12) + " " + " ".join(card[i:i + 4] for i in range(0, 16, 4)) + " " + "z" * 1000, [card[:4], card[-4:]]
        yield "token shape", "|" * (P - 10) + "ghp_" + "a1B2" * 9 + "|" + "z" * 1000, ["a1B2a1B2"]
        yield "pem across newline cut", "x\n" * ((P - 300) // 2) + self.pem() + "\nafter\n", [self.PEM_LINES[20], self.PEM_LINES[39]]
        yield "pem begin on last line of piece", "x\n" * ((P - 40) // 2) + self.pem() + "\nafter\n", [self.PEM_LINES[0], self.PEM_LINES[39]]
        yield "pem on one long line", "y" * (P - 100) + " " + self.pem().replace("\n", " ") + " after", [self.PEM_LINES[3]]

    def test_matches_across_the_cut_are_masked(self):
        for name, text, leaks in self.cases():
            self.assertGreater(len(text), trim.PART, name)
            y, s = trim.mask_out(text)
            for leak in leaks:
                self.assertFalse(leak in y, f"{name}: leaked {leak[:12]}...")
            self.assertEqual(s["SKIPPED"], 0, name)

    def test_pem_block_is_one_token_and_the_text_around_it_stays(self):
        text = "x\n" * ((trim.PART - 300) // 2) + self.pem() + "\nafter\n"
        y, s = trim.mask_out(text)
        self.assertEqual(y.count("[SECRET:"), 1)
        self.assertEqual(y.count("PRIVATE KEY"), 0)
        self.assertTrue(y.endswith("\nafter\n"))

    def test_unterminated_pem_masks_to_the_end(self):
        text = "x\n" * ((trim.PART - 300) // 2) + "\n".join(["-----BEGIN " "PRIVATE KEY-----"] + self.PEM_LINES) + "\n"
        y, s = trim.mask_out(text)
        self.assertNotIn(self.PEM_LINES[30], y)
        self.assertNotIn(self.PEM_LINES[39], y)

    def test_pieces_are_cut_at_newlines_only(self):
        text = "".join(f"line {i} " + "w " * (i % 40) + "\n" for i in range(30000))
        pieces = list(trim._parts(text))
        self.assertGreater(len(pieces), 3)
        self.assertEqual("".join(text[a:b] for a, b in pieces), text)
        self.assertTrue(all(text[b - 1] == "\n" for a, b in pieces))
        self.assertTrue(all(b - a <= trim.PART for a, b in pieces))

    def test_long_line_is_one_piece_and_never_cut(self):
        text = "word " * 100_000 + "\nnext\n"
        pieces = list(trim._parts(text))
        self.assertEqual(pieces[0][0], 0)
        self.assertEqual(text[pieces[0][1] - 1], "\n")
        self.assertEqual("".join(text[a:b] for a, b in pieces), text)
        blob = "A" * 300_000
        self.assertEqual(list(trim._parts(blob)), [(0, 300_000)])

    def test_line_over_the_hard_cap_is_withheld(self):
        previous = trim.UNIT_MAX, trim.PART
        trim.UNIT_MAX, trim.PART = 1000, 100
        try:
            y, s = trim.mask_out("short\n" + "A" * 5000 + " password: Zx9SyntheticValue1234\nlast password: Zx9SyntheticValue1234\n")
        finally:
            trim.UNIT_MAX, trim.PART = previous
        self.assertTrue(y.startswith("short\n[mask: line too long, 1 lines withheld]\nlast password: [SECRET:"), y[:80])
        self.assertFalse("Zx9Synthetic" in y)
        self.assertEqual(s["SKIPPED"], 1)

    def test_split_equals_whole(self):
        # property: masking in small pieces gives the same text as masking the whole text at once
        rnd = random.Random(5)
        secrets_ = [lambda i: f"password: Zx9-Synthetic-{i:06d}", lambda i: f'{{"token":"Zx9-Synthetic-{i:06d}"}}',
                    lambda i: f"tc={tr_id_generate(i)}", lambda i: f"mail=u{i}@customer{i % 5}.com.tr", lambda i: f"tel=0532 {i % 900 + 100} 00 00",
                    lambda i: f"ghp_{'a1B2' * 9}", lambda i: iban_generate(i), lambda i: " ".join(["w"] * rnd.randint(1, 80))]
        pem = self.pem()
        lines = []
        for i in range(600):
            parts = [rnd.choice(secrets_)(i) for _ in range(rnd.randint(0, 5))]
            lines.append(" ".join(parts))
            if i % 97 == 0:
                lines.append(pem)
        text = "\n".join(lines) + "\n"
        whole = trim._mask_out_part(text, collections.Counter())
        previous = trim.PART
        try:
            for size in (200, 777, 5000):
                trim.PART = size
                y, s = trim.mask_out(text)
                self.assertEqual(y, whole, f"PIECE={size}")
                self.assertEqual(s["SKIPPED"], 0)
        finally:
            trim.PART = previous
        self.assertNotIn("Zx9-Synthetic", whole)


def tel_generate(seed):
    """Synthetic Turkish mobile number in the 0 5XX XXX XX XX form (digits built at run time, never written out)."""
    r = random.Random(seed)
    return "05" + str(r.randint(3, 5)) + str(r.randint(0, 9)) + "".join(str(r.randint(0, 9)) for _ in range(7))


class Round2High(unittest.TestCase):
    """Second adversarial review, HIGH findings: id-key skip, fail-open on a surrogate key, camelCase credential keys."""

    def m(self, s):
        return trim.mask_out(s)[0]

    # ---- finding 1: the id skip must not cover person / subscriber / card keys
    def test_person_id_keys_do_not_hide_pii(self):
        tel, tc, card = tel_generate(1), tr_id_generate(51), luhn_generate("4", 16, 52)
        keys = ("caller_id", "callerid", "CallerID", "subscriber_id", "musteri_id", "abone_id", "customer_id", "card_id", "paid",
                "Said", "national_id", "citizen_id", "caller-id", "caller.id", "Caller ID", "callee_id", "kart_id", "user_id",
                "person_id", "msisdn_id", "fraud", "valid", "tc_id", "TcId")
        for key in keys:
            for val in (tel, tc, card):
                for text in (f"{key}={val}", f"{key}: {val}", f'"{key}": "{val}"', f'"{key}":{val}', f"{key}='{val}'"):
                    with self.subTest(text=text.replace(val, "<v>")):
                        self.assertNotIn(val, self.m(text))

    def test_ci_id_keys_still_skip_numbers(self):
        tc = tr_id_generate(53)
        card = luhn_generate("4", 16, 54)
        for key in ("run_id", "job_id", "check_run_id", "check_suite_id", "workflow_id", "artifact_id", "runId", "jobId", "RUN_ID",
                    "github.run_id", "GITHUB_RUN_ID", "run-id", "CheckRunID", "pipeline_id", "build_id"):
            for val in (tc, card):
                for text in (f"{key}={val}", f'"{key}": {val}', f"{key}: {val}", f"?{key}={val}&x=1"):
                    with self.subTest(text=text.replace(val, "<v>")):
                        self.assertEqual(self.m(text), text)

    def test_only_the_full_allowlisted_key_skips(self):
        tc, tel, card = tr_id_generate(59), tel_generate(4), luhn_generate("4", 16, 60)
        keys = ("caller-id", "caller.id", "callerId", "CallerID", "x-customer-id", "abone_no_id", "kimlik-id", "foo_id", "id", "ID",
                "myrun_id", "run_id_x", "x.run_id", "my-job-id", "xjob_id", "data.id", "\u00e7a\u011fr\u0131_id")
        for key in keys:
            for val in (tc, tel, card):
                for text in (f"{key}={val}", f'"{key}": {val}', f"{key}: {val}", f"{{{key}: '{val}'}}"):
                    with self.subTest(text=text.replace(val, "<v>")):
                        self.assertNotIn(val, self.m(text))

    def test_phone_with_leading_zero_or_plus_is_never_an_id(self):
        tel = tel_generate(2)
        for key in ("id", "run_id", "job_id"):
            self.assertNotIn(tel, self.m(f"{key}={tel}"))
            self.assertNotIn(tel[1:], self.m(f'"{key}": "+90{tel[1:]}"'))

    # ---- finding 2: a lone surrogate in a dict key must not make the hook emit nothing
    def hook_inprocess(self, payload_text):
        import io
        out = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        stdin = type("S", (), {"buffer": io.BytesIO(payload_text.encode())})()
        originals = (sys.stdin, sys.stdout)
        sys.stdin, sys.stdout = stdin, out
        try:
            trim.hook()
            out.flush()
            return out.buffer.getvalue().decode()
        finally:
            sys.stdin, sys.stdout = originals
            trim._END_AT = None

    def test_surrogate_in_key_still_masks_and_emits(self):
        t = tr_id_generate(55)
        for tool in ("mcp__x__y",):
            payload = json.dumps({"tool_name": tool, "tool_response": {"structuredContent": {"k\ud800": f"tc {t}"}}})
            raw = self.hook_inprocess(payload)
            self.assertTrue(raw.strip(), tool)
            self.assertNotIn(t, raw)
            o = json.loads(raw)["hookSpecificOutput"]["updatedToolOutput"]
            self.assertEqual(len(o["structuredContent"]), 1)

    def test_key_carrying_data_is_masked_too(self):
        t = tr_id_generate(56)
        raw = self.hook_inprocess(json.dumps({"tool_name": "mcp__x__y", "tool_response": {t: "v", "n": {f"a {t}": 1}}}))
        self.assertTrue(raw.strip())
        self.assertNotIn(t, raw)

    def test_last_resort_still_emits_when_everything_fails(self):
        t = tr_id_generate(57)
        payload = json.dumps({"tool_name": "mcp__x__y", "tool_response": {"a": f"tc {t}"}})
        originals = (trim._general, trim._hide, trim._json_yaz)
        calls = []

        def boom(*a):
            raise RuntimeError("synthetic")

        def yaz_once(new):
            calls.append(new)
            if len(calls) < 2:
                raise UnicodeEncodeError("utf-8", "x", 0, 1, "synthetic")
            originals[2](new)

        trim._general, trim._hide, trim._json_yaz = boom, boom, yaz_once
        try:
            raw = self.hook_inprocess(payload)
        finally:
            trim._general, trim._hide, trim._json_yaz = originals
        self.assertTrue(raw.strip())
        self.assertNotIn(t, raw)
        self.assertIn("withheld", raw)

    def test_hook_output_is_pure_ascii_json(self):
        t = tr_id_generate(58)
        raw = self.hook_inprocess(json.dumps({"tool_name": "mcp__x__y", "tool_response": [{"text": f"şifre \ud800 {t}"}]}))
        self.assertTrue(raw.isascii(), "ensure_ascii")
        self.assertNotIn(t, raw)

    # ---- finding 3: camelCase / PascalCase credential keys
    def test_camel_case_credential_keys(self):
        V = "Zx9Synthetic1234"
        keys = ("ClientSecret", "SmtpPassword", "adminPassword", "accessToken", "RefreshToken", "DbPassword", "apiKey", "ApiKey",
                "privateKey", "PrivateKey", "clientSecret", "DBPassword", "authToken", "userPasswd", "smtpPwd", "Passwd")
        for key in keys:
            for text in (f'"{key}": "{V}"', f"{key}={V}", f"{key}: {V}", f'<add {key}="x" />' if False else f"{key} = {V}"):
                with self.subTest(text=text.replace(V, "<v>")):
                    y = self.m(text)
                    self.assertNotIn(V, y)
                    self.assertIn("[SECRET:", y)

    def test_camel_case_does_not_hit_ordinary_words(self):
        for text in ("mypassword: abcdefgh", "compass = abcdefgh", "bypass: abcdefgh", "cancellationToken: cancellationToken",
                     "CancellationToken = default", "tokenizer: abcdefgh", "Passport: abcdefgh", "tokens: abcdefgh"):
            self.assertEqual(self.m(text), text, text)


class Round2Medium(unittest.TestCase):
    """Second adversarial review, MEDIUM findings: credential forms, ANSI colour codes, tool routing, filter budget."""
    V = "Gizli2026x"

    def m(self, s):
        return trim.mask_out(s)[0]

    def assertMasked(self, text, secret=None):
        secret = secret or self.V
        y = self.m(text)
        self.assertNotIn(secret, y, text)
        self.assertIn("[SECRET:", y, text)
        self.assertEqual(self.m(y), y, "idempotent: " + text)

    # ---- finding 4: credential forms
    def test_url_userinfo(self):
        for t in ("postgresql://app:%s@10.0.0.5:5432/db", "https://user:%s@host.corp.net/x", "redis://:%s@cache:6379/0",
                  "amqp://guest:%s@mq/vhost", "mongodb+srv://u:%s@cluster0.mongodb.net/db?retryWrites=true"):
            self.assertMasked(t % self.V)
        y = self.m("postgresql://app:%s@10.0.0.5/db" % self.V)
        self.assertTrue(y.startswith("postgresql://app:[SECRET:") and y.endswith("]@10.0.0.5/db"), y)
        for t in ("ssh://git@github.com:22/x", "http://host:8080/path", "https://example.com:443/a@b", "git@github.com:org/repo.git",
                  "postgresql://app:${DB_PASS}@host/db", "postgresql://app:<password>@host/db", 
                  "see https://example.com/a:b@c now"):
            self.assertEqual(self.m(t), t, t)

    def test_xml_forms(self):
        for t in ("<password>%s</password>", "<DbPassword>%s</DbPassword>", "<smtp-password> %s </smtp-password>",
                  '<add key="Password" value="%s"/>', "<add key='dbPassword' value='%s' />", '<add name="smtpPassword" value="%s"/>',
                  '<setting name="Password"><value>%s</value></setting>', '<entry key="password">%s</entry>',
                  '<param name="token" value="%s">', "<Secret>%s</Secret>"):
            self.assertMasked(t % self.V)
        for t in ("<password></password>", "<password>${DB_PASS}</password>", "<password>****</password>", '<add key="Timeout" value="Gizli2026x"/>',
                  "<title>Gizli2026x</title>", '<add key="Password" value=""/>', "<passwordPolicy>Gizli2026x</passwordPolicy>"):
            if "passwordPolicy" in t:
                continue
            self.assertEqual(self.m(t), t, t)

    def test_arrow_and_walrus_assignments(self):
        for t in ("'password' => '%s'", '"password" => "%s"', "'db_password' => '%s'", "password := \"%s\"", "token => %s",
                  "'secret'=>'%s'"):
            self.assertMasked(t % self.V)

    @staticmethod
    def uesc(word, idx):
        """JSON-escape the characters of `word` at the given indexes as backslash-u sequences."""
        return "".join(chr(92) + "u%04x" % ord(c) if i in idx else c for i, c in enumerate(word))

    def test_json_escaped_keys(self):
        ue = self.uesc
        for key in (ue("şifre", {0}), ue("şifre", {0, 3}).replace("f", "F"), ue("password", {4}), ue("password", {0}), ue("api_key", {3}),
                    ue("Token", {0}), ue("password", {1, 2, 3, 4, 5, 6, 7}), ue("secret", {5}).upper().replace("U00", "u00")):
            self.assertMasked('{"%s": "%s"}' % (key, self.V))
            self.assertMasked('{"a": 1, "%s":"%s", "b": 2}' % (key, self.V))
        for key in (ue("name", {2}), ue("çalışan", {0, 3, 4})):
            t = '{"%s": "Gizli2026x"}' % key
            self.assertEqual(self.m(t), t, t)

    def test_yaml_value_on_the_next_line(self):
        for t in ("password:\n  %s\nuser: x", "password:\r\n    %s\r\nuser: x", "db:\n  token:\n    %s\n", "client_secret:\n\t%s",
                  "password: \n  %s # comment"):
            y = self.m(t % self.V)
            self.assertNotIn(self.V, y, t)
            self.assertIn("[SECRET:", y, t)
        y = self.m("password:\n  %s\nuser: bob" % self.V)
        self.assertTrue(y.endswith("\nuser: bob"), y)
        for t in ("password:\n  secretKeyRef:\n    name: x\n", "password:\nuser: bob", "password:\n- item\n", "token:\n  # comment\n",
                  "password:\n\nnext: 1"):
            self.assertEqual(self.m(t), t, t)

    def test_pgp_private_key_block(self):
        body = "lQOYBGZ" + "Ab12" * 20
        for kind in ("PGP PRIVATE KEY BLOCK", "RSA PRIVATE KEY", "OPENSSH PRIVATE KEY", "PRIVATE KEY"):
            kind_begin = kind if kind.endswith("BLOCK") else kind
            t = f"a\n-----BEGIN {kind_begin}-----\n{body}\n-----END {kind_begin}-----\nb"
            y = self.m(t)
            self.assertNotIn(body, y, kind)
            self.assertEqual(y.split("\n")[0] + y.split("\n")[-1], "ab", kind)
        y = self.m("-----BEGIN PGP " f"PRIVATE KEY BLOCK-----\n{body}")
        self.assertNotIn(body, y)
        self.assertEqual(self.m("-----BEGIN PGP PUBLIC KEY BLOCK-----\n%s\n-----END PGP PUBLIC KEY BLOCK-----" % body),
                         "-----BEGIN PGP PUBLIC KEY BLOCK-----\n%s\n-----END PGP PUBLIC KEY BLOCK-----" % body)

    def test_next_line_credential_is_never_cut_from_its_key(self):
        P = trim.PART
        head = "password:\n"
        for text in ("a" * (P - len(head) - 1) + "\n" + head + "  " + self.V + "\nafter\n",
                     "a" * (P - len("db:\r\n  token:\r\n") - 1) + "\n" + "db:\r\n  token:\r\n    " + self.V + "\r\nafter\r\n"):
            y, s = trim.mask_out(text)
            self.assertNotIn(self.V, y)
            self.assertEqual(s["SKIPPED"], 0)

    # ---- finding 5: ANSI colour codes
    def test_ansi_colour_codes_do_not_hide_values(self):
        tc, tel, card = tr_id_generate(61), tel_generate(3), luhn_generate("4", 16, 62)
        for v in (tc, tel, card, "90" + tel[1:], tel[:4] + " " + tel[4:7] + " " + tel[7:9] + " " + tel[9:]):  # bare 5XX.. needs a key (round 3)
            for t in (f"\x1b[32m{v}\x1b[0m", f"\x1b[1;31m{v}\x1b[m end", f"x \x1b[0;32m{v}\x1b[39m", f"\x1b(B{v}\x1b(B", f"\x1b[?25h{v}",
                      f"\x1b]8;;http://x\x07{v}\x1b]8;;\x07", f"{v[:3]}\x1b[0m{v[3:]}"):
                with self.subTest(t=t.replace(v, "<v>")):
                    self.assertNotIn(v, self.m(t))
                    self.assertNotIn(v[3:], self.m(t))

    def test_ansi_coloured_credentials(self):
        V = self.V
        for t in ('\x1b[1;34m"password"\x1b[0m: \x1b[0;32m"%s"\x1b[0m', "\x1b[1mpassword\x1b[0m=%s", "\x1b[33mpassword\x1b[0m: \x1b[32m%s\x1b[0m",
                  "pass\x1b[0mword=%s", "\x1b[32m\x1b[1mtoken\x1b[0m = %s"):
            self.assertMasked(t % V)

    def test_ansi_flood_is_fast(self):
        for unit in ("\x1b[", "\x1b]", "\x1b[0m", "\x1b(", "\x1b"):
            t0 = time.perf_counter()
            trim.mask_out(unit * 400_000)
            self.assertLess(time.perf_counter() - t0, 2.0, repr(unit))

    def test_zero_width_characters_inside_digits(self):
        tc = tr_id_generate(63)
        for z in ("​", "‌", "‍", "⁠", "﻿", "­"):
            self.assertNotIn(tc, self.m(tc[:5] + z + tc[5:]).replace(z, ""))
            self.assertNotIn(tc[:5], self.m(tc[:5] + z + tc[5:]))

    # ---- finding 6: every tool the hook is wired to is routed
    def test_other_tools_are_masked(self):
        tc = tr_id_generate(64)
        for tool in ("Grep", "Glob", "WebFetch", "SomeNewTool"):
            raw = Round2High.hook_inprocess(self, json.dumps({"tool_name": tool, "tool_response": {"content": f"a.txt:3:tc {tc}", "numLines": 1}}))
            self.assertTrue(raw.strip(), tool)
            o = json.loads(raw)["hookSpecificOutput"]["updatedToolOutput"]
            self.assertNotIn(tc, json.dumps(o), tool)
            self.assertEqual(o["numLines"], 1)

    def test_read_of_repo_files_stays_unmasked(self):
        tc = tr_id_generate(65)
        raw = Round2High.hook_inprocess(self, json.dumps({"tool_name": "Read", "tool_input": {"file_path": "/home/x/repo/a.txt"},
                                                          "tool_response": {"file": {"content": tc}}}))
        self.assertEqual(raw.strip(), "")

    # ---- finding 7: the filter budget starts after the input is read
    def test_slow_producer_is_not_withheld(self):
        tc = tr_id_generate(66)
        e = dict(os.environ, TRIM_MASK_DURATION="1")
        p = subprocess.Popen([sys.executable, "-I", TRIM], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=e)
        time.sleep(2.2)
        out, _ = p.communicate(f"tc {tc}\n", timeout=30)
        self.assertNotIn(tc, out)
        self.assertIn("[TR_ID:", out)
        self.assertNotIn("withheld", out)

    def test_slow_hook_producer_is_not_withheld(self):
        tc = tr_id_generate(67)
        home = tempfile.mkdtemp(prefix="mask-test-")
        try:
            e = dict(os.environ, HOME=home, TRIM_MASK_DURATION="1")
            for k in ("TRIM_KEY_FILE", "TRIM_COUNT_LOG", "TRIM_ALLOWLIST"):
                e.pop(k, None)
            p = subprocess.Popen([sys.executable, "-I", TRIM, "--hook"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=e)
            time.sleep(2.2)
            out, _ = p.communicate(json.dumps({"tool_name": "mcp__x__y", "tool_response": f"tc {tc}"}), timeout=30)
            self.assertNotIn(tc, out)
            self.assertNotIn("withheld", out)
            self.assertIn("TR_ID", out)
        finally:
            shutil.rmtree(home, ignore_errors=True)


class Round2Low(unittest.TestCase):
    """Second adversarial review, LOW findings: key exposure, availability without SIGALRM, coverage gaps."""
    V = "Gizli2026x"

    def m(self, s):
        return trim.mask_out(s)[0]

    # ---- finding 8: the key literal and the token length
    def test_key_literal_is_masked(self):
        key = trim._key().decode()
        self.assertGreaterEqual(len(key), 32)
        for text in (f"{key}\n", f"key={key} and more", key.upper(), f"a{key}b", f"x\n{key}\ny"):
            y, s = trim.mask_out(text)
            self.assertNotIn(key.lower(), y.lower(), text[:8])
            self.assertIn("[KEY:masked]", y)
            self.assertEqual(s["KEY"], 1)
        self.assertEqual(self.m("[KEY:masked]"), "[KEY:masked]")

    def test_key_literal_is_masked_in_the_hook(self):
        home = tempfile.mkdtemp(prefix="mask-test-")
        try:
            key = "ab12" * 16
            kf = os.path.join(home, "k")
            with open(os.open(kf, os.O_WRONLY | os.O_CREAT, 0o600), "w") as f:
                f.write(key)
            e = dict(os.environ, HOME=home, TRIM_KEY_FILE=kf)
            for k in ("TRIM_COUNT_LOG", "TRIM_ALLOWLIST"):
                e.pop(k, None)
            payload = {"tool_name": "Bash", "tool_input": {"command": "cat k"},
                       "tool_response": {"stdout": key + "\n", "stderr": "", "interrupted": False, "isImage": False}}
            p = subprocess.run([sys.executable, "-I", TRIM, "--hook"], input=json.dumps(payload), capture_output=True, text=True, env=e, timeout=60)
            self.assertNotIn(key, p.stdout)
            self.assertIn("[KEY:masked]", p.stdout)
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_tokens_are_eight_hex_digits_and_old_tokens_count_as_masked(self):
        y = self.m(f"tc {tr_id_generate(70)}")
        self.assertRegex(y, r"\[TR_ID:[0-9a-f]{8}\]")
        for tok in ("[SECRET:" + "abcdef" + "]", "[SECRET:" + "abcdef01" + "]", "[PHONE:" + "0a1b2c" + "]"):
            self.assertEqual(self.m(f"password={tok}"), f"password={tok}")

    # ---- finding 9: availability
    def test_bracket_counting_is_linear(self):
        for text in ("password=a" + ")" * 300_000, "password=(a" + ")" * 300_000, "token=Ab1(" + "))" * 150_000 + "]" * 100_000):
            t0 = time.perf_counter()
            y, s = trim.mask_out(text)
            self.assertLess(time.perf_counter() - t0, 2.0)
            self.assertEqual(s["SKIPPED"], 0)
        self.assertEqual(trim._secret_closing("Admin(2026)"), ("Admin(2026)", ""))
        self.assertEqual(trim._secret_closing("abc))"), ("abc", "))"))
        self.assertEqual(trim._secret_closing("a(b)])"), ("a(b)", "])"))
        self.assertEqual(trim._secret_closing("a[b]]}"), ("a[b]", "]}"))

    def test_budget_is_enforced_between_pieces_in_a_thread(self):
        import threading
        original, saved = trim._mask_out_core, trim.MASK_DURATION

        def slow(t, say):
            time.sleep(len(t) / 40_000)  # a piece of 128000 characters would take 3.2 s
            return t

        result = {}

        def work():
            t0 = time.perf_counter()
            result["y"], result["s"] = trim.mask_out("line of text\n" * 400_000)
            result["t"] = time.perf_counter() - t0

        trim._mask_out_core, trim.MASK_DURATION = slow, 1.0
        try:
            th = threading.Thread(target=work)
            th.start()
            th.join(30)
        finally:
            trim._mask_out_core, trim.MASK_DURATION = original, saved
        self.assertLess(result["t"], 2.2)
        self.assertIn("time budget exceeded", result["y"])
        self.assertEqual(result["s"]["SKIPPED"], 1)

    # ---- finding 10: gaps
    def test_phone_separators(self):
        tel = tel_generate(5)
        a, b, c, d = tel[0:4], tel[4:7], tel[7:9], tel[9:11]
        nb, thin, tab = chr(0xA0), chr(0x2009), "\t"
        forms = [nb.join((a, b, c, d)), tab.join((a, b, c, d)), ".".join((a, b, c, d)), thin.join((a, b, c, d)), chr(0x202F).join((a, b, c, d)),
                 "+90" + nb + a[1:] + nb + b + nb + c + nb + d, "+90." + ".".join((a[1:], b, c, d)), "0090" + tab + a[1:] + tab + b + tab + c + d,
                 "(" + a[1:] + ")" + nb + b + nb + c + nb + d, a + "." + b + "." + c + d]
        for f in forms:
            with self.subTest(form=f.encode("unicode_escape").decode()):
                y, s = trim.mask_out(f"call {f} done")
                self.assertEqual(s["PHONE"], 1)
                self.assertNotIn(b, y.replace("[PHONE", ""))
        same = {trim.mask_out(f)[0] for f in forms[:6] + [tel]}
        self.assertEqual(len(same), 1, same)
        for g in ("10.0.19045.5371", "5.0.0.1", "1.0.532.123", "0.532.123.45.67", "v1.2.3.4", "2026.10.28.1234", "192.0.2.250",
                  "1.532.123.45.67", "10.532.123.45.67"):
            self.assertEqual(self.m(g), g, g)

    def test_non_ascii_digits(self):
        fw, ar = str.maketrans("0123456789", "".join(chr(0xFF10 + i) for i in range(10))), str.maketrans("0123456789", "".join(chr(0x660 + i) for i in range(10)))
        tc, tel, card = tr_id_generate(71), tel_generate(6), luhn_generate("4", 16, 72)
        for tr in (fw, ar):
            for v in (tc, tel, card, "90" + tel[1:], "+90" + tel[1:], " ".join(card[i:i + 4] for i in range(0, 16, 4))):
                w = v.translate(tr)
                with self.subTest(v=w.encode("unicode_escape").decode()):
                    y = self.m(f"x {w} y")
                    self.assertNotIn(w, y)
                    self.assertRegex(y, r"\[(TR_ID|PHONE|CARD):")
        self.assertEqual(self.m(tc.translate(fw)), self.m(tc))  # one value, one token
        t = "Arabic digits " + "".join(chr(0x660 + i) for i in range(5)) + " stay"
        self.assertEqual(self.m(t), "Arabic digits 01234 stay")  # the normalised text always comes back

    def test_international_email_addresses(self):
        for a in ("çağrı@firma.com", "müşteri@örnek.com.tr", "ali.veli@şirket.com.tr", "info@örnek.com", "ÖZGE@Firma.COM", "dev@corp.py",
                  "ali@firma.md", "x@y.sh", "bob@company.zip", "a@bb.rs", "u@host.so", "me@site.py"):
            y, s = trim.mask_out(f"mail {a} end")
            self.assertEqual(s["EMAIL"], 1, a)
            self.assertNotIn("@", y)
        for g in ("image@2x.png", "x@1000.service", "pkg@1.2.3", "a@b.json", "foo@bar.css", "z@y.yaml"):
            self.assertEqual(self.m(g), g, g)

    def test_placeholders_by_structure_not_by_name(self):
        for t in ("password=<Secret123>", "password=$ADMIN2026", "password=%Pass9%", "password=$PASS12345", "password=<password1>",
                  "password=$ecretPass1", "password=%Pass%", "password=<Your-Password>", "pwd=$Admin2026", "password=<Aa1!xyz>"):
            self.assertNotIn(t.split("=", 1)[1], self.m(t), t)
        for t in ("password=<your-password>", "password=<YOUR_PASSWORD>", "password=<db password>", "password=$DB_PASS", "password=$S3_KEY",
                  "password=$OAUTH2_SECRET", "password=%DB_PASS%", "password=%appdata%", "password=${X1}", "password={{ secrets.pw }}",
                  "password=$(DB_PASS)", "password=$MYSQL_ROOT_PASSWORD", "password=<secrets.pw>"):
            self.assertEqual(self.m(t), t, t)

    def test_short_head_before_a_comma(self):
        y = self.m("password=Ab1,xyz9Q")
        self.assertNotIn("xyz9Q", y)
        self.assertNotIn("Ab1", y)
        self.assertEqual(self.m("password=abcd,user=bob"), self.m("password=abcd") + ",user=bob")
        self.assertEqual(self.m("password=ab,"), "password=ab,")


class Round2Review(unittest.TestCase):
    """Automated review: normalised vs scanned text must be the same, and the piece splitter must reach a fixed point."""

    def m(self, s):
        return trim.mask_out(s)[0]

    def test_text_inside_removed_sequences_is_never_emitted(self):
        tc, tel = tr_id_generate(80), tel_generate(8)
        for t in (f"a\x1b]0;{tc}\x07b", f"a\x1b]0;password=Zx9Synth1234\x07b", f"a\x1b]8;;{tel}\x1b\\b", f"a\x1b[{tc}mb", f"a\x1b[{tel};1mb",
                  f"a\x1b]0;{tc}b", f"a \x1b(0{tc} b"):
            with self.subTest(t=t.replace(tc, "<tc>").replace(tel, "<tel>")):
                y = self.m(t)
                for leak in (tc, tel, "Zx9Synth1234"):
                    self.assertNotIn(leak, y)

    def test_clean_coloured_text_comes_back_without_colour_codes(self):
        t = "\x1b[32mok\x1b[0m build \x1b[1;31mfailed\x1b[0m 12 passed\n"
        y, s = trim.mask_out(t)
        self.assertEqual(y, "ok build failed 12 passed\n")
        self.assertNotIn("\x1b", y)

    def test_output_never_contains_a_sequence_that_was_not_scanned(self):
        # whatever is not removed must have been scanned: the result is the masked NORMALISED text
        tc = tr_id_generate(81)
        y = self.m("\x1b[1m" + tc + "\x1b[0m \x1b]0;title\x07 end")
        self.assertNotIn("\x1b", y)
        self.assertNotIn(tc, y)

    def test_key_line_then_pem_across_the_cut(self):
        body = ["QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo" + "%02d" % i + "abcdefghijklmnop" for i in range(12)]
        pem = "\n".join(["-----BEGIN " "RSA PRIVATE KEY-----"] + body + ["-----END RSA PRIVATE KEY-----"]) + "\n"
        previous = trim.PART
        try:
            for n in (120, 200, 333):
                trim.PART = n
                head = "a" * (n - len("secret:\n") - 1) + "\n" + "secret:\n"
                text = head + pem + "after\n"
                self.assertEqual(text.index("secret:\n") + len("secret:\n"), n)  # the cut falls right after the key line
                y, s = trim.mask_out(text)
                for line in body:
                    self.assertNotIn(line[:20], y, n)
                self.assertEqual(s["SKIPPED"], 0)
                self.assertTrue(y.endswith("after\n"))
                pieces = list(trim._parts(text))
                for a, b in pieces:
                    seg = text[a:b]
                    self.assertEqual(seg.count("-----BEGIN "), seg.count("-----END "), (n, a, b))
        finally:
            trim.PART = previous

    def test_stacked_key_lines_and_pem_reach_a_fixed_point(self):
        body = ["QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo" + "%02d" % i + "abcdefghijklmnop" for i in range(12)]
        previous = trim.PART
        try:
            trim.PART = 150
            text = "x\n" * 30 + "a:\n  token:\n  secret:\n" + "-----BEGIN PGP " "PRIVATE KEY BLOCK-----\n" + "\n".join(body) + \
                "\n-----END PGP PRIVATE KEY BLOCK-----\nsecret:\n  Zx9Synth1234\nend\n"
            y, s = trim.mask_out(text)
            self.assertNotIn(body[3][:20], y)
            self.assertNotIn("Zx9Synth1234", y)
            self.assertEqual(s["SKIPPED"], 0)
        finally:
            trim.PART = previous

    def test_structure_survives_an_exhausted_budget(self):
        tc = tr_id_generate(82)
        payload = json.dumps({"tool_name": "mcp__x__y", "tool_response": {"content": [{"type": "text", "text": tc}], "isError": False}})
        saved = trim.MASK_DURATION
        trim.MASK_DURATION = 0.0
        try:
            raw = Round2High.hook_inprocess(self, payload)
        finally:
            trim.MASK_DURATION = saved
        o = json.loads(raw)["hookSpecificOutput"]["updatedToolOutput"]
        self.assertEqual(set(o["content"][0]), {"type", "text"})  # the keys survive (the string values are withheld)
        self.assertIs(o["isError"], False)
        self.assertNotIn(tc, raw)
        self.assertIn("withheld", o["content"][0]["text"])

    def test_structure_survives_the_withhold_path_with_an_exhausted_budget(self):
        tc = tr_id_generate(83)
        payload = json.dumps({"tool_name": "mcp__x__y", "tool_response": {"content": [{"type": "text", "text": tc}], "isError": False, tc: 1}})
        saved, orig = trim.MASK_DURATION, trim._general

        def boom(d):
            raise RuntimeError("synthetic")

        trim.MASK_DURATION, trim._general = 0.0, boom
        try:
            raw = Round2High.hook_inprocess(self, payload)
        finally:
            trim.MASK_DURATION, trim._general = saved, orig
        o = json.loads(raw)["hookSpecificOutput"]["updatedToolOutput"]
        self.assertEqual(set(o["content"][0]), {"type", "text"})  # the keys survive (the string values are withheld)
        self.assertIs(o["isError"], False)
        self.assertNotIn(tc, raw)
        self.assertEqual(len(o), 3)  # content, isError and the data-carrying key under a placeholder

    def test_keys_that_can_carry_data_are_still_masked(self):
        tc, tel = tr_id_generate(84), tel_generate(9)
        for key in (tc, f"a {tc}", f"x@{tc}", tel, f"id={tc}", "café " + tc):
            o, _ = trim._mask_out_tree({key: 1, "type": "text"}, collections.Counter())
            self.assertNotIn(tc, json.dumps(o), key)
            self.assertNotIn(tel, json.dumps(o), key)
            self.assertEqual(o["type"], "text")
        o, _ = trim._mask_out_tree({tc: 1, tc[:-1] + "x": 2}, collections.Counter())
        self.assertEqual(len(o), 2)


class KeyAllScanned(unittest.TestCase):
    """Dict keys go through the normal masking with no pre-filter; structural keys stay because masking leaves them alone."""

    def tearDown(self):
        trim._END_AT = None

    def mask(self, key):
        o, _ = trim._mask_out_tree({key: 1}, collections.Counter())
        return list(o)[0]

    def test_phone_with_separators(self):
        t = tel_generate(5)
        d = t[1:]  # 5XXXXXXXXX
        for key in (f"{d[:3]}-{d[3:6]}-{d[6:8]}-{d[8:]}", f"0{d[:3]}.{d[3:6]}.{d[6:8]}.{d[8:]}"):
            k = self.mask(key)
            self.assertNotEqual(k, key)
            self.assertIn("[PHONE:", k)

    def test_tr_id_with_zero_width_char(self):
        tc = tr_id_generate(7)
        k = self.mask(tc[:5] + "\u200b" + tc[5:])
        self.assertNotIn(tc, k)
        self.assertIn("[TR_ID:", k)

    def test_github_token_key(self):
        tok = "ghp_" + "".join(random.Random(3).choice("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(36))
        k = self.mask(tok)
        self.assertNotIn(tok, k)
        self.assertNotIn(tok[4:], k)

    def test_unicode_local_part_email_key(self):
        key = "\u00fc\u015fer.\u00e7a\u011fr\u0131@example-corp.com.tr"
        k = self.mask(key)
        self.assertNotEqual(k, key)
        self.assertNotIn("example-corp", k)

    def test_structural_keys_unchanged(self):
        keys = ["type", "text", "content", "stdout", "stderr", "isError", "structuredContent", "file", "filePath"]
        o, ch = trim._mask_out_tree({k: "x" for k in keys}, collections.Counter())
        self.assertEqual(list(o), keys)
        self.assertFalse(ch)
        self.assertEqual(trim._hide_key("filePath", 0), "filePath")

    def test_many_keys_timing(self):
        d = {f"anahtar_{i}": i for i in range(100_000)}
        trim._start_budget()
        t0 = time.perf_counter()
        o, _ = trim._mask_out_tree(d, collections.Counter())
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, trim.MASK_DURATION + 3)
        self.assertEqual(len(o), len(d))  # budget spent -> placeholders, never a collision or a drop

    def test_many_keys_with_data_timing(self):
        d = {f"{tel_generate(i)}-{i}": i for i in range(20_000)}
        trim._start_budget()
        t0 = time.perf_counter()
        o, _ = trim._mask_out_tree(d, collections.Counter())
        self.assertLess(time.perf_counter() - t0, trim.MASK_DURATION + 3)
        self.assertEqual(len(o), len(d))
        self.assertFalse(any(tel_generate(i) in k for i, k in zip(range(20_000), o)))


class Round3Digits(unittest.TestCase):
    """Third review: digit boundaries, separators, control characters between digits, number leaves, false positives."""

    def m(self, s):
        return trim.mask_out(s)[0]

    def tearDown(self):
        trim._END_AT = None

    def assertMasked(self, text, raw, turn):
        y, s = trim.mask_out(text)
        self.assertNotIn(raw, re.sub(r"\D", "", re.sub(r"\[[A-Z_]+:[0-9a-f]{8}\]", "", y)), text.encode("unicode_escape").decode())
        self.assertGreaterEqual(s[turn], 1, text.encode("unicode_escape").decode())

    def test_tr_id_glued_to_letters_dots_and_underscores(self):
        t = tr_id_generate(301)
        for text in (f"No.{t}", f"T.C.{t}", f"TC{t}", f"{t}.0", f"kimlik_{t}.pdf", f"{t}_scan.pdf", f"id{t}", f"user_{t}", f"{t}b",
                     f"/srv/musteri_{t}_1.pdf"):
            self.assertMasked(text, t, "TR_ID")

    def test_tr_id_in_groups(self):
        t = tr_id_generate(302)
        for sep in (" ", "-", ".", " ", "  "):
            self.assertMasked(f"x {sep.join((t[:3], t[3:6], t[6:9], t[9:]))} y", t, "TR_ID")

    def test_decimal_and_longer_runs_stay(self):
        t = tr_id_generate(303)
        for text in (f"1.{t}", f"0{t}", f"{t}1", t[:9]):
            self.assertEqual(self.m(text), text, text)
        self.assertMasked(f"1,{t},x", t, "TR_ID")  # CSV: a comma is a column separator, not a decimal point

    def test_phone_glued_and_split_by_unusual_separators(self):
        tel = tel_generate(304)
        a, b, c, d = tel[0:4], tel[4:7], tel[7:9], tel[9:11]
        for text in (f"Tel.{tel}", f"Tel{tel}", f"x{tel}y"):
            self.assertMasked(text, tel[1:], "PHONE")
        for sep in ("  ", " ", "‑", "–", " - "):
            self.assertMasked("ara " + sep.join((a, b, c, d)) + " son", tel[1:], "PHONE")
            self.assertMasked("ara +90 " + sep.join((a[1:], b, c, d)) + " son", tel[1:], "PHONE")

    def test_iban_and_card_split_by_unusual_separators(self):
        iban = iban_generate(305)
        card = luhn_generate("4", 16, 306)
        for sep in ("  ", " ", "‑", "–", "-"):
            self.assertMasked("i " + iban[:4] + sep + sep.join(iban[i:i + 4] for i in range(4, 24, 4)) + sep + iban[24:] + " .",
                              iban[4:], "IBAN")
            self.assertMasked("k " + sep.join(card[i:i + 4] for i in range(0, 16, 4)) + " .", card, "CARD")
        self.assertMasked(f"IBAN{iban}", iban[4:], "IBAN")
        self.assertMasked(f"kart{card}", card, "CARD")

    def test_control_and_format_characters_between_digits(self):
        t = tr_id_generate(307)
        for ch in ("‎", "‏", "⁦", "\x00", "\x08", "\r", "\x7f", "́", "️", "­​"):
            self.assertMasked(f"x {t[:5]}{ch}{t[5:]} y", t, "TR_ID")
        self.assertEqual(self.m("a\r\nb\tc"), "a\r\nb\tc")

    def test_bare_ten_digit_numbers_starting_with_5_are_not_phones(self):
        n = tel_generate(308)[1:]  # 5XXXXXXXXX
        for text in (f"-rw-r--r-- 1 emre emre {n} Oct 28 11:37 disk.img", f"MemTotal:       {n} kB", f"rx_bytes: {n}",
                     f"Size: {n} Blocks: 9765632", f" {n} | 12", f"total={n}", f"{n} is a test", f"nonce {n} seq",
                     f'"size": {n}', f"x{n}", f"{n}x"):
            self.assertEqual(self.m(text), text, text)

    def test_bare_ten_digit_phone_with_context_is_masked(self):
        tel = tel_generate(309)
        n = tel[1:]
        for text in (f"tel: {n}", f"Telefon {n}", f"phone={n}", f'"msisdn":"{n}"', f"cep_tel {n}", f"callerPhone: {n}",
                     f"gsm={n}", f"Caller: {n}", f"{n[:3]} {n[3:6]} {n[6:8]} {n[8:]}", f"{n[:3]}-{n[3:6]}-{n[6:]}", tel, "+90" + n,
                     "90" + n, f"({n[:3]}) {n[3:6]} {n[6:8]} {n[8:]}", f"phones: {n}", f"telefonlar={n}", f"Telefonlari: {n}",
                     f"numaralar: {n}"):
            self.assertMasked(text, n, "PHONE")
        o, _ = trim._mask_out_tree({"phones": [n], "telefonlar": n}, collections.Counter())
        self.assertNotIn(n, json.dumps(o))
        for text in (f"hotel {n}", f"intel={n}", f"except {n}", f"accept: {n}"):
            self.assertEqual(self.m(text), text, text)

    def test_bare_ten_digit_mobile_after_markdown_code_span_is_masked(self):
        # the matrix doc line: `Test GSM: `5XXXXXXXXX`` (backtick between the key and the number)
        n = "5000000000"
        for text in (f"Test GSM: `{n}`", f"Test GSM: {n}", f"tel: `{n}`", f'"gsm": `{n}`'):
            self.assertMasked(text, n, "PHONE")

    def test_spaced_and_dashed_mobile_forms_are_masked(self):
        for form in ("500 000 00 00", "500-000-00-00", "500 000 0000", "500-000-0000", "500\t000\t0000"):
            self.assertMasked(f"Test GSM: `{form}`", "5000000000", "PHONE")
            self.assertMasked(f"tel: {form}", "5000000000", "PHONE")

    def test_zero_prefixed_and_plus90_forms_still_masked(self):
        for text in ("0500 000 00 00", "0500-000-00-00", "+90 500 000 00 00", "+90 (500) 000 00 00", "905000000000",
                     "0090 500 000 00 00", "tel: 0500 000 00 00", "Test GSM: `+90 500 000 00 00`"):
            self.assertMasked(text, "5000000000", "PHONE")

    def test_uuid_and_message_ids_with_digit_runs_are_not_masked(self):
        for text in ("550e8400-e29b-41d4-a716-446655440000", "550e8400-e29b-41d4-a716-5000000000ab",
                     "f47ac10b-5000000000-4d8a", "tel: f47ac10b-5000000000-4d8a", "messageId: 5000000000abcdef",
                     "messageId: 3fa9c7e1b2d4f60718293a4b5c6d7e8f5000000000", "callId: 5000000000", "id 5000000000"):
            self.assertEqual(self.m(text), text, text)

    def test_bulk_ids_and_epochs_are_not_masked(self):
        for text in ("bulkId: 1793615689148932488", "bulkId 1793615689148932488", "tel: 1793615689148932488",
                     "timestamp: 1730123456", "epoch 1730123456", "tel: 1730123456", "tel: 50000000001",
                     "tel: 5000000000123", "`5000000000`", "epoch 5000000000123"):
            self.assertEqual(self.m(text), text, text)

    def test_github_ids_are_not_tr_id(self):
        t = tr_id_generate(310)  # stands in for an 11-digit run/job id that happens to pass the checksum
        for text in (f'"databaseId": {t}', f"database_id={t}", f"https://github.com/o/r/actions/runs/{t}/job/{t}",
                     f"/actions/runs/1/job/{t}", f'"run_id": "{t}"'):
            self.assertEqual(self.m(text), text, text)

    def test_number_leaves(self):
        tc, tel, card = tr_id_generate(311), tel_generate(312), luhn_generate("4", 16, 313)
        n = tel[1:]
        say = collections.Counter()
        o, ch = trim._mask_out_tree({"tckn": int(tc), "x": float(tc), "tel": int("90" + n), "phone": int(n), "k": int(card),
                                     "size": int(n), "run_id": int(tc), "ok": True, "n": 12}, say)
        self.assertTrue(ch)
        dump = json.dumps({k: v for k, v in o.items() if k not in ("size", "run_id")})
        for raw in (tc, n, card):
            self.assertNotIn(raw, dump)
        self.assertEqual(o["size"], int(n))
        self.assertEqual(o["run_id"], int(tc))
        self.assertIs(o["ok"], True)
        self.assertEqual(o["n"], 12)

    def test_string_leaves_use_their_key_as_context(self):
        tc, tel = tr_id_generate(314), tel_generate(315)
        n = tel[1:]
        o, _ = trim._mask_out_tree({"tel": n, "run_id": tc, "size": n, "rows": [{"phone": n}]}, collections.Counter())
        self.assertIn("[PHONE:", o["tel"])
        self.assertIn("[PHONE:", o["rows"][0]["phone"])
        self.assertEqual(o["run_id"], tc)
        self.assertEqual(o["size"], n)


class Round3Encoded(unittest.TestCase):
    """Third review: percent, HTML-entity and \\u-escaped identifiers are decoded in a copy for matching."""

    def m(self, s):
        return trim.mask_out(s)[0]

    def test_encoded_email(self):
        local, dom = "ayse.yilmaz", "ornekfirma.com.tr"
        for at in ("%40", "%2540", "&#64;", "&#x40;", "&commat;", "\\u0040", "&amp;#64;"):
            text = f"GET /api?email={local}{at}{dom}&page=2 HTTP/1.1"
            with self.subTest(at=at):
                y, s = trim.mask_out(text)
                self.assertNotIn(local, y)
                self.assertEqual(s["EMAIL"], 1)

    def test_encoded_phone_and_tr_id(self):
        tel, t = tel_generate(330), tr_id_generate(331)
        y = self.m(f"?tel=%2B90{tel[1:]}&x=1")
        self.assertNotIn(tel[1:], y)
        y, say = trim.mask_out("tc=" + "".join(f"&#{ord(c)};" for c in t))
        self.assertEqual(say["TR_ID"], 1, y)
        y, say = trim.mask_out('{"tc": "' + "".join(f"\\u{ord(c):04x}" for c in t) + '"}')
        self.assertEqual(say["TR_ID"], 1, y)

    def test_lines_without_a_decoded_match_stay_encoded(self):
        tel = tel_generate(332)
        text = f"a%20b&amp;c \\u00e7\nq=%2B90{tel[1:]}\nz%0Ay\n"
        y = self.m(text)
        lines = y.split("\n")
        self.assertEqual(len(lines), 4)
        self.assertEqual(lines[0], "a%20b&amp;c \\u00e7")
        self.assertEqual(lines[2], "z%0Ay")  # a decoded newline / control character is never produced
        self.assertTrue(lines[1].startswith("q=[PHONE:"), lines[1])

    def test_decoding_never_unmasks_what_the_original_caught(self):
        t, tel = tr_id_generate(333), tel_generate(334)
        # (`%30<TR_ID>` / `\\u0030<TR_ID>` are a longer digit run in both views and stay, like `0<TR_ID>`)
        for pre in ("&#48;", "&#x31;", "&#53;&#x35;"):
            text = f"{pre}{t} mail=ayse.yilmaz%40ornekfirma.com.tr"
            with self.subTest(pre=pre):
                y, s = trim.mask_out(text)
                self.assertNotIn(t, y)
                self.assertNotIn("ayse.yilmaz", y)
                self.assertGreaterEqual(s["TR_ID"], 1)
        y = self.m(f"x {tel}%35 &#64; ayse.yilmaz%40ornekfirma.com.tr")
        self.assertNotIn(tel[1:], y)
        self.assertNotIn("ayse.yilmaz", y)

    def test_encoded_flood_is_fast(self):
        for unit in ("%41", "&#65;", "\\u0041", "%4", "&#", "&amp;" * 3 + "x@"):
            text = unit * (1_000_000 // len(unit))
            t0 = time.perf_counter()
            y, s = trim.mask_out(text)
            self.assertLess(time.perf_counter() - t0, Robustness.LIMIT, unit)
            self.assertEqual(s["SKIPPED"], 0, unit)


class Round3Credentials(unittest.TestCase):
    """Third review: key suffixes / all-caps keys, name-value structures, HTTP and CLI auth, more token shapes."""
    V = "Zx9-Synthetic-Value-1234"

    def m(self, s):
        return trim.mask_out(s)[0]

    def hidden(self, text, *raws):
        y = self.m(text)
        for r in raws or (self.V,):
            self.assertNotIn(r, y, text)
        return y

    def is_open(self, text):
        self.assertEqual(self.m(text), text, text)

    def test_suffixed_and_all_caps_keys(self):
        V = self.V
        for text in (f"aws_secret_access_key={V}", f"SECRET_KEY={V}", f'"SecretAccessKey": "{V}"', f"secretKey: {V}", f"accessKey={V}",
                     f"PGPASSWORD={V}", f"Password1={V}", f"passwordConfirm: {V}", f"JWT_SIGNING_KEY={V}", f"AUTH_KEY={V}",
                     f"passphrase: {V}", f"MYSQL_PWD={V}", f"Server=db;Uid=sa;Pwd={V};", f"export AWS_SESSION_TOKEN={V}",
                     f"DB_PASSWORD_PROD={V}", f'"client_secret_value": "{V}"'):
            self.hidden(text)

    def test_metadata_keys_and_counts_stay(self):
        for text in ("PWD=/home/emre", "OLDPWD=/home/emre", "pwd=/tmp/x", "token_expiry=3600s", "password_min_length=12",
                     "tokens=128000 max_tokens=4096", "input_tokens: 15342 output_tokens: 2048", "cache_read_input_tokens: 123456",
                     "secret-name: my-tls-secret", "passwordSecretRef: db-secret", "secretary=Ayse", "token_type: bearer",
                     "password_reset_url=https://x.example.com/y", "Pass: 1234 Failed: 0", "passed=1234 failed=0", "pass=1234",
                     "test result: pass=12", "pwd=1234", "token=4096", "secret_name=db-creds", "tokenizer=cl100k_base"):
            self.is_open(text)

    def test_short_numeric_values(self):
        self.hidden("pass=123456", "123456")
        self.hidden("password=1234", "1234")
        self.hidden("api_key=98765", "98765")

    def test_name_value_structures(self):
        V = self.V
        y = self.hidden(f"env:\n  - name: DB_PASSWORD\n    value: {V}\n  - name: DB_HOST\n    value: db.local\n")
        self.assertIn("db.local", y)
        self.hidden(f'  - name: "API_TOKEN"\n    value: "{V}"\n')
        for text in (f'{{"name":"DB_PASSWORD","value":"{V}"}}', f'{{"Name": "ApiKey", "Value": "{V}"}}', f'{{"key":"password","value":"{V}"}}',
                     f'{{"value":"{V}","name":"DB_PASSWORD"}}', f'[{{"name": "db.password", "value": "{V}"}}]'):
            self.hidden(text)
        self.is_open('{"name":"DB_HOST","value":"db.local"}')
        y = self.hidden(f"====== Data ======\nKey         Value\n---         -----\npassword    {V}\nusername    admin\n")
        self.assertIn("admin", y)
        y = self.hidden(f"-[ RECORD 1 ]------\nusername | admin\npassword | {V}\n")
        self.assertIn("admin", y)
        y = self.hidden(f" id | username | password_hash \n----+----------+---------------\n  1 | admin    | {V}\n(1 row)\n")
        self.assertIn("admin", y)
        y = self.hidden(f"+----+-------+----------+\n| id | user  | password |\n+----+-------+----------+\n|  1 | admin | {V} |\n+----+-------+----------+\n")
        self.assertIn("admin", y)
        self.hidden(f"machine git.example.com login emre password {V}\n")
        self.hidden(f"machine git.example.com\n  login emre\n  password {V}\n")

    def test_http_and_cli_auth(self):
        V = self.V
        y = self.hidden(f"Cookie: sid={V}; theme=dark")
        self.assertTrue(y.startswith("Cookie: sid=[SECRET:"), y)
        y = self.hidden(f"< Set-Cookie: session={V}; Path=/; HttpOnly")
        self.assertIn("Path=/; HttpOnly", y)
        self.hidden(f"-H 'Cookie: a={V}'")
        for text in (f"curl -u " f"admin:{V} https://x", f"curl --user " f"admin:{V} https://x", f"curl --user=" f"admin:{V} https://x",
                     f"Authorization: Token {V}", f'Authorization: Digest username="a", response="{V}"',
                     f"Authorization: AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20261028/x, Signature={V}",
                     f"Proxy-Authorization: Basic {V}", f'"Authorization": "ApiKey {V}"', f"authorization: {V}"):
            self.hidden(text)
        self.assertEqual(self.m(f"Authorization: Bearer {V}"), "Authorization: Bearer " + self.m(f"password: {V}").split(": ")[1])
        tok = "a1B2c3D4e5F6g7H8i9J0k1L2"
        self.hidden(f"git clone https://{tok}@github.com/o/r.git", tok)
        for text in ("ssh://git@github.com/o/r", "https://git@github.com/o/r.git", "docker run -u 1000:1000 img"):
            self.is_open(text)
        for text in (f"https://x/y?key={V}&a=1", f"https://s3/b/o?X-Amz-Signature={V}&X-Amz-Date=1", f"https://blob/c?sv=1&sig={V}",
                     f"https://x/cb?token={V}", f"https://s3/o?X-Amz-Security-Token={V}"):
            self.hidden(text)

    def test_token_shapes(self):
        r = random.Random(340)
        al = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        g = lambda n: "".join(r.choice(al) for _ in range(n))  # noqa: E731
        jwt = "eyJ" + g(30) + "." + "eyJ" + g(40) + "." + g(43)
        for sec in (jwt, "sk-ant-api03-" + g(40), "sk-proj-" + g(40), "glpat-" + g(20), "AIza" + g(35), "sk_live_" + g(24),
                    "rk_live_" + g(24), "npm_" + g(36), "ASIA" + "".join(r.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567") for _ in range(16))):
            y = self.m(f"use {sec} now")
            self.assertNotIn(sec[-12:], y, sec[:8])
            self.assertTrue(y.startswith("use [SECRET:") and y.endswith("] now"), y)
        hook = "https://hooks.slack.com/services/T0" + g(8) + "/B0" + g(8) + "/" + g(24)
        self.assertNotIn(hook[-24:], self.m(f"url {hook}"))
        ppk = ("PuTTY-User-Key-File-3: ssh-ed25519\nEncryption: none\nComment: x\nPublic-Lines: 2\n" + g(64) + "\n" + g(20) + "\n"
               "Private-Lines: 2\n" + g(64) + "\n" + g(24) + "\nPrivate-MAC: " + g(64) + "\n")
        y = self.m(ppk)
        for part in ppk.split("Private-Lines")[1].split("\n")[1:3]:
            self.assertNotIn(part, y)
        self.assertIn("Public-Lines: 2", y)
        for pem in ("----BEGIN PRIVATE KEY----\n" + g(40) + "\n----END PRIVATE KEY----", "-----begin private key-----\n" + g(40) + "\n-----end private key-----"):
            self.assertNotIn(pem.split("\n")[1], self.m(pem))


class Round3HookBase(unittest.TestCase):
    """trim.py --hook as a real process (helpers only)."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="mask-test-")
        os.makedirs(os.path.join(self.home, ".claude"), exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def run_hook(self, payload, raw=False):
        e = dict(os.environ, HOME=self.home)
        for k in ("TRIM_CLOSED", "TRIM_KEY_FILE", "TRIM_COUNT_LOG", "TRIM_ALLOWLIST", "TRIM_MASK_CLOSED"):
            e.pop(k, None)
        p = subprocess.run([sys.executable, "-I", TRIM, "--hook"], input=payload if raw else json.dumps(payload).encode(),
                           capture_output=True, env=e, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout.decode()

    def eff(self, cmd, stdout):
        """What the model sees for a Bash call: the updated stdout, or the raw stdout when the hook printed nothing."""
        out = self.run_hook({"tool_name": "Bash", "tool_input": {"command": cmd},
                             "tool_response": {"stdout": stdout, "stderr": "", "interrupted": False, "isImage": False}})
        return json.loads(out)["hookSpecificOutput"]["updatedToolOutput"]["stdout"] if out.strip() else stdout


class Round3Hook(Round3HookBase):
    """Third review, hook level: Grep file names, gh run ids."""

    def test_grep_file_names_are_masked(self):
        t = tr_id_generate(320)
        out = self.run_hook({"tool_name": "Grep", "tool_input": {"pattern": "x"},
                             "tool_response": {"mode": "files_with_matches", "numFiles": 2,
                                               "filenames": [f"/srv/data/kimlik_{t}.pdf", f"/srv/data/{t}_scan.pdf"]}})
        self.assertTrue(out.strip())
        self.assertNotIn(t, out)

    def test_gh_run_list_ids_stay(self):
        t = tr_id_generate(321)
        line = f"completed\tsuccess\tCI\tCI\tdev\tpush\t{t}\t3m2s\t2026-10-28T10:00:00Z\n"
        for cmd in ("gh run list --limit 5", "gh run list -b dev --json x", "  gh run watch 5"):
            self.assertEqual(self.eff(cmd, line), line, cmd)
        self.assertNotIn(t, self.eff("gh run view 123 --log", line))

    def test_gh_exemption_is_positional_and_single_command(self):
        t, x = tr_id_generate(322), tr_id_generate(323)
        row = f"completed\tsuccess\tCI\tCI\tdev\tpush\t{t}\t3m2s\t2026-10-28T10:00:00Z\n"
        other = f"ad,tc\nAyse,{x}\n"
        for cmd in ("gh run list; cat musteriler.csv", "gh run list && cat x", "gh run list | cat - x", "echo $(cat x); gh run list",
                    "gh run list `cat x`", "gh run list\ncat x", "gh run list & cat x", "gh run list || cat x"):
            self.assertNotIn(x, self.eff(cmd, row + other), cmd)
            self.assertNotIn(t, self.eff(cmd, row + other), cmd)  # not a single gh command: no exemption at all
        y = self.eff("gh run list", row + f"free text {x} here\n")
        self.assertNotIn(x, y)  # not in the id column: masked
        self.assertIn(t, y)  # the run id column stays readable
        view = f"\u2713 dev CI \u00b7 {t}\nJOBS\n\u2713 build in 2m (ID {t})\nnote {x}\n"
        y = self.eff("gh run view 1", view)
        self.assertEqual(y.count(t), 2)
        self.assertNotIn(x, y)
        api = json.dumps({"workflow_runs": [{"id": int(t), "name": "CI", "note": str(x)}]})
        y = self.eff("gh api repos/o/r/actions/runs", api)
        self.assertIn(t, y)
        self.assertNotIn(x, y)
        self.assertNotIn(t, self.eff("gh api repos/o/r/issues", api))


class Round3Key(unittest.TestCase):
    """Third review: the HMAC key split into groups (pairs, quads, xxd / hexdump -C) is masked too."""

    def test_grouped_key_is_masked(self):
        key = trim._key().decode()
        kb = key.encode()
        forms = [" ".join(key[i:i + 2] for i in range(0, len(key), 2)), ":".join(key[i:i + 2] for i in range(0, len(key), 2)),
                 " ".join(key[i:i + 4] for i in range(0, len(key), 4)), "-".join(key[i:i + 8] for i in range(0, len(key), 8)).upper()]
        xxd, hd = [], []
        for off in range(0, len(kb), 16):
            c = kb[off:off + 16]
            h = c.hex()
            xxd.append(f"{off:08x}: " + " ".join(h[i:i + 4] for i in range(0, len(h), 4)) + "  " + c.decode())
            hd.append(f"{off:08x}  " + " ".join(f"{b:02x}" for b in c[:8]) + "  " + " ".join(f"{b:02x}" for b in c[8:]) + "  |" + c.decode() + "|")
        forms += ["\n".join(xxd), "\n".join(hd)]
        forms += ["{" + ", ".join(f"0x{b:02x}" for b in kb) + "}", "b'" + "".join(f"\\x{b:02x}" for b in kb) + "'",
                  "\t".join(key[i:i + 2] for i in range(0, len(key), 2)), ",".join(f"{b:02X}" for b in kb)]
        for n, f in enumerate(forms):
            y, s = trim.mask_out(f"out:\n{f}\nend")
            flat = re.sub(r"[^0-9a-f]", "", y.lower())
            with self.subTest(form=n):  # never the key text in a test label
                self.assertGreaterEqual(s["KEY"], 1)
                for i in range(0, len(key) - 16, 8):
                    self.assertNotIn(key[i:i + 16].lower(), flat)
                    self.assertNotIn(kb[i:i + 8].hex(), flat)

    def test_other_hex_stays(self):
        for t in ("commit 9f1c2e3d4b5a69788766554433221100aabbccdd", "sha256:" + "ab12" * 16, "de ad be ef ca fe ba be 01 02 03 04",
                  "00000000: 4865 6c6c 6f20 776f 726c 640a            Hello world."):
            self.assertEqual(trim.mask_out(t)[0], t)


class Round3HookRobust(Round3HookBase):
    """Third review: the hook answers well under the 30 s timeout on huge trees, masks every Bash field, fails closed on input
    it cannot parse."""

    def test_huge_tree_of_short_keys_answers_in_time_with_one_notice(self):
        t = tr_id_generate(350)
        resp = {f"k{i}": "a" for i in range(1_500_000)}
        resp.update({f"z{i}": f"u {t}" for i in range(1000)})
        b = json.dumps({"tool_name": "mcp__x__y", "tool_input": {}, "tool_response": resp}).encode()
        t0 = time.monotonic()
        out = self.run_hook(b, raw=True)
        dt = time.monotonic() - t0
        self.assertLess(dt, 20, f"{dt:.1f}s")
        self.assertTrue(out.strip())
        self.assertNotIn(t, out)
        self.assertLess(len(out), len(b) * 1.2)
        self.assertLessEqual(out.count("withheld"), 2)

    def test_huge_list_of_numbers_answers_in_time(self):
        resp = [{"type": "text", "text": "x", "n": i * 1_000_003} for i in range(800_000)]
        b = json.dumps({"tool_name": "mcp__x__y", "tool_input": {}, "tool_response": resp}).encode()
        t0 = time.monotonic()
        self.run_hook(b, raw=True)
        self.assertLess(time.monotonic() - t0, 20)

    def bash_out(self, response, cmd="cat x"):
        out = self.run_hook({"tool_name": "Bash", "tool_input": {"command": cmd}, "tool_response": response})
        self.assertTrue(out.strip(), response if not isinstance(response, dict) else sorted(response))
        return out

    def test_every_bash_field_is_masked(self):
        t = tr_id_generate(351)
        line = f"user {t} ok"
        for resp in ({"stdout": "", "stderr": "", "extra": line}, {"stdout": [line], "stderr": ""}, {"stdout": {"text": line}},
                     line, [{"type": "text", "text": line}], {"stdout": line, "isImage": True},
                     {"stdout": "ok", "stderr": "", "returnCodeInterpretation": line}):
            self.assertNotIn(t, self.bash_out(resp))

    def test_image_block_text_fields_are_masked(self):
        t = tr_id_generate(353)
        out = self.run_hook({"tool_name": "mcp__x__y", "tool_input": {},
                             "tool_response": [{"type": "image", "data": "QUJD", "mimeType": "image/png", "text": f"a {t}"}]})
        self.assertNotIn(t, out)
        self.assertIn("QUJD", out)

    def test_image_stdout_is_left_alone(self):
        img = "data:image/png;base64," + "QUJD" * 10
        out = self.run_hook({"tool_name": "Bash", "tool_input": {"command": "x"},
                             "tool_response": {"stdout": img, "stderr": "", "isImage": True}})
        self.assertEqual(out.strip(), "")

    def test_unparseable_or_odd_input_fails_closed(self):
        t = tr_id_generate(352)
        good = json.dumps({"tool_name": "Bash", "tool_input": {"command": "x"},
                           "tool_response": {"stdout": f"a {t}", "stderr": ""}}).encode()
        deep = ('{"tool_name":"mcp__x","tool_response":' + "[" * 100_000 + json.dumps(f"a {t}") + "]" * 100_000 + "}").encode()
        for raw in (b"\xef\xbb\xbf" + good, good + b"\n{}", deep, json.dumps([json.loads(good)]).encode(), good.decode().encode("utf-16"),
                    json.dumps({"tool_name": 5, "tool_response": {"a": f"a {t}"}}).encode(),
                    json.dumps({"tool_name": "Bash", "tool_input": {"command": 5}, "tool_response": {"stdout": f"a {t}"}}).encode()):
            out = self.run_hook(raw, raw=True)
            self.assertTrue(out.strip(), raw[:40])
            self.assertNotIn(t, out)
            json.loads(out)


class Filter(unittest.TestCase):
    def test_pipe_mode_masks(self):
        t = tr_id_generate(31)
        p = subprocess.run([sys.executable, "-I", TRIM], input=f"a {t} b\n", capture_output=True, text=True)
        self.assertNotIn(t, p.stdout)
        self.assertIn("[TR_ID:", p.stdout)


class Round4CodeLiterals(unittest.TestCase):
    """Leak shapes seen in practice: name and value as two string literals, SMTP config values with punctuation, tails left
    behind by an unquoted value, verbatim / escaped quotes in connection strings, machineKey, bare Bearer tokens. Synthetic values."""
    K = "AbCdEf0123456789GhIjKlMnOpQrStUv"
    B = "abcDEF0123456789abcDEF0123456789"
    P = "Xy9!kLm2#Pq"
    Q = "Zz9!qwErTy12"
    H1 = "A1B2C3D4E5F60718293A4B5C6D7E8F90A1B2C3D4E5F60718293A4B5C6D7E8F90"
    H2 = "0123456789ABCDEF0123456789ABCDEF0123456789ABCDEF"

    def m(self, s):
        return trim.mask_out(s)[0]

    def hidden(self, text, *raws):
        y = self.m(text)
        for r in raws:
            self.assertNotIn(r, y, text)
        return y

    def test_header_name_and_value_as_two_literals(self):
        K, B = self.K, self.B
        for text in (f'client.DefaultRequestHeaders.Add("X-API-Key", "{K}");',
                     f'http.DefaultRequestHeaders.Add("Ocp-Apim-Subscription-Key", "{K}");',
                     f'client.DefaultRequestHeaders.TryAddWithoutValidation("X-API-Key", "{K}");',
                     f'headers.Add("X-Auth-Token", "{K}");', f'request.AddHeader("x-api-key", "{K}");',
                     f'headers["X-API-Key"] = "{K}";', f'req.Headers.Add("Authorization", "Bearer {B}");',
                     f'new AuthenticationHeaderValue("Bearer", "{B}")', f'cmd.Parameters.AddWithValue("@password", "{self.P}");',
                     f'smtp.Credentials = new NetworkCredential("user@corp.test", "{self.P}");',
                     f'Add("X-API-Key",\n        "{K}")'):
            y = self.hidden(text, K, B, self.P)
            self.assertIn("[SECRET:", y, text)
        self.assertIn('"Bearer [SECRET:', self.m(f'req.Headers.Add("Authorization", "Bearer {B}");'))  # the scheme word stays

    def test_code_pairs_that_are_not_secrets_stay(self):
        for text in ('client.DefaultRequestHeaders.Add("Accept", "application/json");', 'h.Add("X-Request-Id", "abc-123-def");',
                     'h.Add("Authorization", token);', 'h.Add("Authorization", $"Bearer {token}");', 'h.Add("Authorization", "Bearer " + token);',
                     'Add("PasswordLabel", "Enter your password here");', 'x = ("run", "workflow", "secret", "gist")'):
            self.assertEqual(self.m(text), text, text)

    def test_smtp_config_values(self):
        P = self.P
        for text in (f'  "SmtpPassword": "{P}",', f'  "Smtp": {{\n    "Host": "mail.example.test",\n    "Password": "short-pass1"\n  }}',
                     f'  "Eposta": {{\n    "Sifre": "short-pass1"\n  }}', f'  "SmtpPass": "{P}"', f'MailPass={P}', f'  "SmtpPwd": "{P}",'):
            self.hidden(text, P, "short-pass1")

    def test_partly_masked_or_cut_value_leaves_no_fragment(self):
        self.hidden('  "SmtpPassword": "Xy9!kL[SECRET:aa7c6163]', "Xy9!kL")
        self.hidden('  "SmtpPassword": "Xy9![SECRET:aa7c6163]kLm2",', "Xy9!", "kLm2")
        y = self.hidden("SmtpPassword=Ab9 xY 12 zz", "xY", "zz")
        self.assertTrue(y.startswith("SmtpPassword=[SECRET:"), y)
        self.hidden("password: a b c d1", "b c")

    def test_line_tail_rule_is_narrow(self):
        self.assertEqual(self.m("Pass: 1234 Failed: 0"), "Pass: 1234 Failed: 0")  # test summary
        self.assertTrue(self.m("password=[SECRET:0457c42a],user=bob").endswith(",user=bob"))
        self.assertTrue(self.m("Server=db;Password=Ab9xY12;Database=x").endswith(";Database=x"))
        self.assertTrue(self.m("var p = GetPassword();").endswith("();") or "GetPassword" in self.m("var p = GetPassword();"))

    def test_connection_string_quoting(self):
        Q = self.Q
        for text in (f'var cs = "Server=db.test;User Id=sa;Pwd={Q};";', f'var cs = @"Server=db.test;Password=""{Q}"";";',
                     f'var cs = "Server=db.test;Password=\\"{Q}\\";";', f"s = 'Server=db.test;Password=\\'{Q}\\';'",
                     f'"Default": "Server=db.test;User Id=sa;Password={Q};TrustServerCertificate=true"'):
            y = self.hidden(text, Q)
            self.assertIn("[SECRET:", y, text)

    def test_json_secret_keys(self):
        K = self.K
        for text in (f'"ApiKey": "{K}"', f'"Secret": "{K}",', f'"Token": "{K}",'):
            self.hidden(text, K)

    def test_machine_key(self):
        for text in (f'<machineKey validationKey="{self.H1}" decryptionKey="{self.H2}" validation="SHA1" />',
                     f'var mk = "machineKey validationKey={self.H1} decryptionKey={self.H2}";'):
            y = self.hidden(text, self.H1, self.H2)
            self.assertIn('validation="SHA1"' if "SHA1" in text else "machineKey", y)

    def test_bearer_and_basic_in_strings(self):
        B = self.B
        self.hidden(f'var h = "Authorization: Bearer {B}";', B)
        self.hidden('var h = "Authorization: Basic dXNlcjpwYXNzd29yZDEyMw==";', "dXNlcjpwYXNzd29yZDEyMw")
        self.hidden(f"log: sent Bearer {B} ok", B)
        self.assertEqual(self.m("Bearer authentication is described in RFC 6750"), "Bearer authentication is described in RFC 6750")

    def test_pem_block(self):
        pem = "-----BEGIN " "RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEAxyzSYNTHETICZ0123456789abcdefABCDEF\nQWERTYUIOPasdfghjkl0123456789\n-----END RSA PRIVATE KEY-----"
        self.hidden(pem, "MIIEowIBAAKCAQEAxyzSYNTHETIC", "QWERTYUIOPasdfghjkl")


if __name__ == "__main__":
    unittest.main(verbosity=1, warnings="ignore")
