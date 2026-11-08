"""Tests for tools/gunluk.py: the rolling run log (1 MB x 64 files, utf-8, secrets masked, never raises).

Run: python -m unittest discover -s tools/tests
"""
import logging
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import gunluk as G  # noqa: E402


def oku(yol):
    with open(yol, encoding="utf-8") as f:
        return f.read()


class GunlukTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gunluk-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.n = 0

    def lg(self, **kw):
        self.n += 1
        ad = "gunluk-test-%d-%d" % (os.getpid(), id(self))
        self.addCleanup(self.temizle, ad)
        return G.gunluk(ad, **kw), ad

    def temizle(self, ad):
        lg = logging.getLogger(ad)
        for h in list(lg.handlers):
            h.close()
            lg.removeHandler(h)

    def test_writes_one_line_with_time_level_and_message(self):
        yol = os.path.join(self.tmp, "sub", "a.log")
        lg, _ = self.lg(yol=yol)
        lg.info("merhaba %s", "dünya")
        with open(yol, encoding="utf-8") as f:
            self.assertRegex(f.read(), r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d INFO merhaba dünya\n$")

    def test_default_path_and_env_override_follow_the_name(self):
        self.assertTrue(G.log_yolu("kayip-is").endswith(os.path.join(".local", "state", "kayip-is.log")))
        yol = os.path.join(self.tmp, "env.log")
        with mock.patch.dict(os.environ, {"GUNLUK_TEST_ENV_LOG": yol}):
            G.gunluk("gunluk-test-env").warning("x")
        self.temizle("gunluk-test-env")
        self.assertIn("WARNING x", oku(yol))

    def test_calling_twice_does_not_duplicate_lines(self):
        yol = os.path.join(self.tmp, "b.log")
        lg, ad = self.lg(yol=yol)
        G.gunluk(ad, yol=yol)
        lg.info("bir")
        self.assertEqual(oku(yol).count("bir"), 1)

    def test_rotates_at_the_size_limit_and_keeps_at_most_64_files(self):
        self.assertEqual(G.LOG_YEDEK + 1, 64)
        yol = os.path.join(self.tmp, "r.log")
        with mock.patch.object(G, "LOG_BYTES", 300), mock.patch.object(G, "LOG_YEDEK", 2):
            lg, _ = self.lg(yol=yol)
            for i in range(60):
                lg.info("satir %d %s", i, "x" * 40)
        dosyalar = sorted(os.listdir(self.tmp))
        self.assertEqual(dosyalar, ["r.log", "r.log.1", "r.log.2"])

    def test_unopenable_log_never_raises_and_logging_still_works(self):
        d = os.path.join(self.tmp, "dir.log")
        os.mkdir(d)                                    # a directory cannot be opened as the log file
        lg, _ = self.lg(yol=d)
        lg.info("bos")
        lg.exception("x")
        engel = os.path.join(self.tmp, "engel")
        open(engel, "w").close()                       # the parent is a file
        G.gunluk("gunluk-test-engel", yol=os.path.join(engel, "k.log")).info("bos")
        self.temizle("gunluk-test-engel")

    def test_secrets_are_masked(self):
        yol = os.path.join(self.tmp, "m.log")
        lg, _ = self.lg(yol=yol)
        lg.error("clone https://kul:parola@github.com/x.git ghp_%s Bearer %s", "a" * 30, "b" * 24)
        t = oku(yol)
        self.assertNotIn("parola", t)
        self.assertNotIn("a" * 30, t)
        self.assertNotIn("b" * 24, t)
        self.assertIn("://***@github.com", t)

    def test_safe_without_stderr_and_stdout(self):
        yol = os.path.join(self.tmp, "n.log")
        with mock.patch.object(sys, "stderr", None), mock.patch.object(sys, "stdout", None):
            lg, _ = self.lg(yol=yol)
            lg.info("pythonw")
            lg.warning("bu da")
        self.assertIn("pythonw", oku(yol))

    def test_a_new_path_replaces_the_old_handler(self):
        a, b = os.path.join(self.tmp, "1.log"), os.path.join(self.tmp, "2.log")
        lg, ad = self.lg(yol=a)
        lg.info("ilk")
        G.gunluk(ad, yol=b).info("ikinci")
        self.assertIn("ilk", oku(a))
        self.assertNotIn("ikinci", oku(a))
        self.assertIn("ikinci", oku(b))

    def test_custom_format(self):
        yol = os.path.join(self.tmp, "f.log")
        lg, _ = self.lg(yol=yol, bicim="%(asctime)s [gorev] %(message)s")
        lg.info("100% iş")
        self.assertRegex(oku(yol), r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d \[gorev\] 100% iş\n$")


if __name__ == "__main__":
    unittest.main()
