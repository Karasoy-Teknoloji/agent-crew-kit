"""Tests for tools/runlog.py: the rolling run log (1 MB x 64 files, utf-8, secrets masked, never raises).

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
import runlog as G  # noqa: E402


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class DailyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="runlog-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.n = 0

    def lg(self, **kw):
        self.n += 1
        name = "daily-test-%d-%d" % (os.getpid(), id(self))
        self.addCleanup(self.clean, name)
        return G.runlog(name, **kw), name

    def clean(self, name):
        lg = logging.getLogger(name)
        for h in list(lg.handlers):
            h.close()
            lg.removeHandler(h)

    def test_writes_one_line_with_time_level_and_message(self):
        path = os.path.join(self.tmp, "sub", "a.log")
        lg, _ = self.lg(path=path)
        lg.info("hello %s", "wörld")
        with open(path, encoding="utf-8") as f:
            self.assertRegex(f.read(), r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d INFO hello wörld\n$")

    def test_default_path_and_env_override_follow_the_name(self):
        self.assertTrue(G.log_path("lost-work").endswith(os.path.join(".local", "state", "lost-work.log")))
        path = os.path.join(self.tmp, "env.log")
        with mock.patch.dict(os.environ, {"DAILY_TEST_ENV_LOG": path}):
            G.runlog("daily-test-env").warning("x")
        self.clean("daily-test-env")
        self.assertIn("WARNING x", read(path))

    def test_calling_twice_does_not_duplicate_lines(self):
        path = os.path.join(self.tmp, "b.log")
        lg, name = self.lg(path=path)
        G.runlog(name, path=path)
        lg.info("one")
        self.assertEqual(read(path).count("one"), 1)

    def test_rotates_at_the_size_limit_and_keeps_at_most_64_files(self):
        self.assertEqual(G.LOG_BACKUP + 1, 64)
        path = os.path.join(self.tmp, "r.log")
        with mock.patch.object(G, "LOG_BYTES", 300), mock.patch.object(G, "LOG_BACKUP", 2):
            lg, _ = self.lg(path=path)
            for i in range(60):
                lg.info("line %d %s", i, "x" * 40)
        files = sorted(os.listdir(self.tmp))
        self.assertEqual(files, ["r.log", "r.log.1", "r.log.2"])

    def test_unopenable_log_never_raises_and_logging_still_works(self):
        d = os.path.join(self.tmp, "dir.log")
        os.mkdir(d)                                    # a directory cannot be opened as the log file
        lg, _ = self.lg(path=d)
        lg.info("bos")
        lg.exception("x")
        block = os.path.join(self.tmp, "block")
        open(block, "w").close()                       # the parent is a file
        G.runlog("daily-test-block", path=os.path.join(block, "k.log")).info("bos")
        self.clean("daily-test-block")

    def test_secrets_are_masked(self):
        path = os.path.join(self.tmp, "m.log")
        lg, _ = self.lg(path=path)
        lg.error("clone https://usr:password@github.com/x.git ghp_%s Bearer %s", "a" * 30, "b" * 24)
        t = read(path)
        self.assertNotIn("password", t)
        self.assertNotIn("a" * 30, t)
        self.assertNotIn("b" * 24, t)
        self.assertIn("://***@github.com", t)

    def test_safe_without_stderr_and_stdout(self):
        path = os.path.join(self.tmp, "n.log")
        with mock.patch.object(sys, "stderr", None), mock.patch.object(sys, "stdout", None):
            lg, _ = self.lg(path=path)
            lg.info("pythonw")
            lg.warning("bu da")
        self.assertIn("pythonw", read(path))

    def test_a_new_path_replaces_the_old_handler(self):
        a, b = os.path.join(self.tmp, "1.log"), os.path.join(self.tmp, "2.log")
        lg, name = self.lg(path=a)
        lg.info("first")
        G.runlog(name, path=b).info("second")
        self.assertIn("first", read(a))
        self.assertNotIn("second", read(a))
        self.assertIn("second", read(b))

    def test_custom_format(self):
        path = os.path.join(self.tmp, "f.log")
        lg, _ = self.lg(path=path, fmt="%(asctime)s [task] %(message)s")
        lg.info("100% café")
        self.assertRegex(read(path), r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d \[task\] 100% café\n$")


if __name__ == "__main__":
    unittest.main()
