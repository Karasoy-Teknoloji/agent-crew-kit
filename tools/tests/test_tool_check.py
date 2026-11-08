"""Tests for tools/tool_check.py: the tool table, report-only default, the consent flag, pip --user, the Windows python3 shim."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TOOL = Path(__file__).resolve().parents[1] / "tool_check.py"
sys.path.insert(0, str(TOOL.parent))
import tool_check as a  # noqa: E402


def missing(*_):
    return None


class Table(unittest.TestCase):
    def test_names_unique_ve_each_tool_one_path_present(self):
        names = [t.name for t in a.TOOLS]
        self.assertEqual(len(names), len(set(names)))
        for t in a.TOOLS:
            self.assertIn(t.importance, (a.REQUIRED, a.RECOMMENDED, a.OPTIONAL))
            self.assertTrue(t.winget or t.apt or t.brew or t.pip or t.manual, t.name)
            self.assertTrue(t.commands or t.pip, t.name)

    def test_requested_tools_in_list(self):
        names = " ".join(t.name for t in a.TOOLS)
        for name in ("python3", "jq", "pdftoppm", "pypdf", "tesseract", "shellcheck", "actionlint", "govc", "docker", "rsync"):
            self.assertIn(name, names)


class Commands(unittest.TestCase):
    def tool(self, name):
        return next(t for t in a.TOOLS if t.name.startswith(name))

    def test_windows_winget_identity(self):
        k = a.install_commands(self.tool("jq"), "windows", "py", pm="winget")
        self.assertEqual(k[0][:4], ["winget", "install", "--id", "jqlang.jq"])
        self.assertIn("--accept-package-agreements", k[0])

    def test_linux_apt(self):
        k = a.install_commands(self.tool("pdftoppm"), "linux", "python3", pm="apt")
        self.assertEqual(k[0][-4:], ["apt-get", "install", "-y", "poppler-utils"][-4:])

    def test_pip_user_windows_ve_apt_linux(self):
        pypdf = self.tool("pypdf")
        k = a.install_commands(pypdf, "windows", "C:/py/python.exe", pm="winget")
        self.assertEqual(k, [["C:/py/python.exe", "-m", "pip", "install", "--user", "pypdf"]])
        self.assertIn("python3-pypdf", a.install_commands(pypdf, "linux", "python3", pm="apt")[0])

    def test_docker_ve_govc_never_automatic(self):
        for os_id, pm in (("windows", "winget"), ("linux", "apt"), ("macos", "brew")):
            self.assertEqual(a.install_commands(self.tool("docker"), os_id, "py", pm=pm), [])
        self.assertEqual(a.install_commands(self.tool("govc"), "linux", "python3", pm="apt"), [])


class Execute(unittest.TestCase):
    def fake_missing(self):
        return mock.patch.object(a, "is_present", side_effect=lambda t, py: t.name in ("git", "gh", "ssh", "curl", "python3"))

    def test_default_only_report_no_install(self):
        output = []
        with self.fake_missing(), mock.patch.object(a, "run_cmd") as c, mock.patch.object(a, "refresh_path"):
            rc = a.execute("py", install=False, write=output.append)
        self.assertEqual(rc, 0)
        c.assert_not_called()
        self.assertTrue(any("MISSING" in s and "jq" in s for s in output))
        self.assertTrue(any("-Tools" in s for s in output))

    def test_install_flag_missing_installs(self):
        output, running = [], []
        with self.fake_missing(), mock.patch.object(a, "refresh_path"), \
                mock.patch.object(a, "run_cmd", side_effect=lambda argv, *x: (running.append(argv), (0, []))[1]), \
                mock.patch.object(a, "package_manager", return_value="winget"), mock.patch.object(a, "os_name", return_value="windows"), \
                mock.patch.object(a, "add_user_path"), mock.patch.object(a, "tesseract_turkish", return_value=True):
            a.execute("py", install=True, write=output.append)
        identities = [x[3] for x in running if x[:2] == ["winget", "install"]]
        for identity in ("jqlang.jq", "koalaman.shellcheck", "oschwartz10612.Poppler", "UB-Mannheim.TesseractOCR"):
            self.assertIn(identity, identities)
        self.assertNotIn("Docker.DockerDesktop", identities)
        self.assertTrue(any(x[1:5] == ["-m", "pip", "install", "--user"] for x in running))

    def test_whatif_no_install(self):
        with self.fake_missing(), mock.patch.object(a, "refresh_path"), mock.patch.object(a, "run_cmd") as c:
            rc = a.execute("py", install=True, whatif=True, write=lambda s: None)
        self.assertEqual(rc, 0)
        c.assert_not_called()


class Tesseract(unittest.TestCase):
    def test_turkish_already_present(self):
        with mock.patch.object(a, "tesseract_languages", return_value=("C:/t", {"eng", "tur"})), mock.patch.object(a, "run_cmd") as c:
            self.assertTrue(a.tesseract_turkish("windows", False, write=lambda s: None))
        c.assert_not_called()

    def test_linux_apt_tur_package(self):
        with mock.patch.object(a, "tesseract_languages", return_value=("/usr/share/tessdata", {"eng"})),                 mock.patch.object(a, "package_manager", return_value="apt"),                 mock.patch.object(a, "run_cmd", return_value=(0, [])) as c:
            self.assertTrue(a.tesseract_turkish("linux", False, write=lambda s: None))
        self.assertIn("tesseract-ocr-tur", c.call_args[0][0])

    def test_unwritable_dir_user_copy_drops(self):
        with tempfile.TemporaryDirectory() as t:
            system, ev = Path(t) / "system", Path(t) / "ev"
            system.mkdir()
            (system / "eng.traineddata").write_bytes(b"x" * 10)
            call = []

            def download(language, dir_):
                call.append(dir_)
                if dir_ == system:
                    return "permission denied"
                dir_.mkdir(parents=True, exist_ok=True)
                (dir_ / "tur.traineddata").write_bytes(b"y")
                return None
            with mock.patch.object(a, "tesseract_languages", return_value=(str(system), {"eng"})),                     mock.patch.object(a, "download_language_data", side_effect=download),                     mock.patch.object(a.Path, "home", return_value=ev), mock.patch.object(a, "run_cmd", return_value=(0, [])),                     mock.patch.dict(os.environ, {}, clear=False):
                self.assertTrue(a.tesseract_turkish("windows", False, write=lambda s: None))
                self.assertEqual(os.environ["TESSDATA_PREFIX"], str(ev / ".local" / "share" / "tessdata"))
                os.environ.pop("TESSDATA_PREFIX", None)
            self.assertTrue((ev / ".local" / "share" / "tessdata" / "eng.traineddata").is_file())


class Shim(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.dir = Path(self.d.name)
        self.py = Path(sys.executable)

    def tearDown(self):
        self.d.cleanup()

    def test_author_after_same_after_removes(self):
        with mock.patch.object(a, "real_python3", return_value=None):
            self.assertTrue(a.python3_shim(str(self.py), target_dir=self.dir, write=lambda s: None))
            sh, cmd = self.dir / "python3", self.dir / "python3.cmd"
            self.assertIn("py -3", cmd.read_text())
            self.assertTrue(cmd.read_bytes().count(b"\r\n") > 3)
            self.assertNotIn(b"\r", sh.read_bytes())
            self.assertIn(a.MARKER, sh.read_text())
            output = []
            a.python3_shim(str(self.py), target_dir=self.dir, write=output.append)
            self.assertTrue(all("same" in s for s in output), output)
            a.python3_shim(str(self.py), remove=True, target_dir=self.dir, write=lambda s: None)
            self.assertFalse(sh.exists() or cmd.exists())

    def test_real_python3_if_any_no_write(self):
        with mock.patch.object(a, "real_python3", return_value="C:/Python/python3.exe"):
            self.assertTrue(a.python3_shim(str(self.py), target_dir=self.dir, write=lambda s: None))
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_foreign_to_file_untouched(self):
        foreign = self.dir / "python3.cmd"
        foreign.write_bytes(b"@echo benim\r\n")
        with mock.patch.object(a, "real_python3", return_value=None):
            self.assertFalse(a.python3_shim(str(self.py), target_dir=self.dir, write=lambda s: None))
        self.assertEqual(foreign.read_bytes(), b"@echo benim\r\n")

    def test_interpreter_else_no_write(self):
        with mock.patch.object(a, "real_python3", return_value=None):
            self.assertFalse(a.python3_shim(str(self.dir / "missing.exe"), target_dir=self.dir, write=lambda s: None))
        self.assertEqual(list(self.dir.iterdir()), [])

    @unittest.skipUnless(os.name == "nt", "WindowsApps stub is a Windows case")
    def test_store_stub_real_uncounted(self):
        stub = self.dir / "WindowsApps"
        stub.mkdir()
        (stub / "python3.exe").write_bytes(b"MZ")
        with mock.patch.dict(os.environ, {"PATH": str(stub)}):
            self.assertIsNone(a.real_python3())


if __name__ == "__main__":
    unittest.main()
