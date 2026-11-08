"""Tests for tools/arac_kontrol.py: the tool table, report-only default, the consent flag, pip --user, the Windows python3 shim."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ARAC = Path(__file__).resolve().parents[1] / "arac_kontrol.py"
sys.path.insert(0, str(ARAC.parent))
import arac_kontrol as a  # noqa: E402


def yok(*_):
    return None


class Tablo(unittest.TestCase):
    def test_adlar_benzersiz_ve_her_aracin_bir_yolu_var(self):
        adlar = [t.ad for t in a.ARACLAR]
        self.assertEqual(len(adlar), len(set(adlar)))
        for t in a.ARACLAR:
            self.assertIn(t.onem, (a.ZORUNLU, a.ONERILEN, a.ISTEGE_BAGLI))
            self.assertTrue(t.winget or t.apt or t.brew or t.pip or t.elle, t.ad)
            self.assertTrue(t.komutlar or t.pip, t.ad)

    def test_istenen_araclar_listede(self):
        adlar = " ".join(t.ad for t in a.ARACLAR)
        for ad in ("python3", "jq", "pdftoppm", "pypdf", "tesseract", "shellcheck", "actionlint", "govc", "docker", "rsync"):
            self.assertIn(ad, adlar)


class Komutlar(unittest.TestCase):
    def arac(self, ad):
        return next(t for t in a.ARACLAR if t.ad.startswith(ad))

    def test_windows_winget_kimligi(self):
        k = a.kur_komutlari(self.arac("jq"), "windows", "py", pm="winget")
        self.assertEqual(k[0][:4], ["winget", "install", "--id", "jqlang.jq"])
        self.assertIn("--accept-package-agreements", k[0])

    def test_linux_apt(self):
        k = a.kur_komutlari(self.arac("pdftoppm"), "linux", "python3", pm="apt")
        self.assertEqual(k[0][-4:], ["apt-get", "install", "-y", "poppler-utils"][-4:])

    def test_pip_user_windows_ve_apt_linux(self):
        pypdf = self.arac("pypdf")
        k = a.kur_komutlari(pypdf, "windows", "C:/py/python.exe", pm="winget")
        self.assertEqual(k, [["C:/py/python.exe", "-m", "pip", "install", "--user", "pypdf"]])
        self.assertIn("python3-pypdf", a.kur_komutlari(pypdf, "linux", "python3", pm="apt")[0])

    def test_docker_ve_govc_asla_otomatik(self):
        for os_ad, pm in (("windows", "winget"), ("linux", "apt"), ("macos", "brew")):
            self.assertEqual(a.kur_komutlari(self.arac("docker"), os_ad, "py", pm=pm), [])
        self.assertEqual(a.kur_komutlari(self.arac("govc"), "linux", "python3", pm="apt"), [])


class Calis(unittest.TestCase):
    def sahte_eksik(self):
        return mock.patch.object(a, "var_mi", side_effect=lambda t, py: t.ad in ("git", "gh", "ssh", "curl", "python3"))

    def test_varsayilan_yalniz_rapor_kurmaz(self):
        cikti = []
        with self.sahte_eksik(), mock.patch.object(a, "calistir") as c, mock.patch.object(a, "yol_yenile"):
            rc = a.calis("py", kur=False, yaz=cikti.append)
        self.assertEqual(rc, 0)
        c.assert_not_called()
        self.assertTrue(any("EKSIK" in s and "jq" in s for s in cikti))
        self.assertTrue(any("-Araclar" in s for s in cikti))

    def test_kur_bayragi_eksikleri_kurar(self):
        cikti, calisan = [], []
        with self.sahte_eksik(), mock.patch.object(a, "yol_yenile"), \
                mock.patch.object(a, "calistir", side_effect=lambda argv, *x: (calisan.append(argv), (0, []))[1]), \
                mock.patch.object(a, "paket_yoneticisi", return_value="winget"), mock.patch.object(a, "os_adi", return_value="windows"), \
                mock.patch.object(a, "kullanici_path_ekle"), mock.patch.object(a, "tesseract_turkce", return_value=True):
            a.calis("py", kur=True, yaz=cikti.append)
        kimlikler = [x[3] for x in calisan if x[:2] == ["winget", "install"]]
        for kimlik in ("jqlang.jq", "koalaman.shellcheck", "oschwartz10612.Poppler", "UB-Mannheim.TesseractOCR"):
            self.assertIn(kimlik, kimlikler)
        self.assertNotIn("Docker.DockerDesktop", kimlikler)
        self.assertTrue(any(x[1:5] == ["-m", "pip", "install", "--user"] for x in calisan))

    def test_whatif_kurmaz(self):
        with self.sahte_eksik(), mock.patch.object(a, "yol_yenile"), mock.patch.object(a, "calistir") as c:
            rc = a.calis("py", kur=True, whatif=True, yaz=lambda s: None)
        self.assertEqual(rc, 0)
        c.assert_not_called()


class Tesseract(unittest.TestCase):
    def test_turkce_zaten_var(self):
        with mock.patch.object(a, "tesseract_dilleri", return_value=("C:/t", {"eng", "tur"})), mock.patch.object(a, "calistir") as c:
            self.assertTrue(a.tesseract_turkce("windows", False, yaz=lambda s: None))
        c.assert_not_called()

    def test_linux_apt_tur_paketi(self):
        with mock.patch.object(a, "tesseract_dilleri", return_value=("/usr/share/tessdata", {"eng"})),                 mock.patch.object(a, "paket_yoneticisi", return_value="apt"),                 mock.patch.object(a, "calistir", return_value=(0, [])) as c:
            self.assertTrue(a.tesseract_turkce("linux", False, yaz=lambda s: None))
        self.assertIn("tesseract-ocr-tur", c.call_args[0][0])

    def test_yazilamayan_dizin_kullanici_kopyasina_duser(self):
        with tempfile.TemporaryDirectory() as t:
            sistem, ev = Path(t) / "sistem", Path(t) / "ev"
            sistem.mkdir()
            (sistem / "eng.traineddata").write_bytes(b"x" * 10)
            cagri = []

            def indir(dil, dizin):
                cagri.append(dizin)
                if dizin == sistem:
                    return "izin yok"
                dizin.mkdir(parents=True, exist_ok=True)
                (dizin / "tur.traineddata").write_bytes(b"y")
                return None
            with mock.patch.object(a, "tesseract_dilleri", return_value=(str(sistem), {"eng"})),                     mock.patch.object(a, "tur_verisi_indir", side_effect=indir),                     mock.patch.object(a.Path, "home", return_value=ev), mock.patch.object(a, "calistir", return_value=(0, [])),                     mock.patch.dict(os.environ, {}, clear=False):
                self.assertTrue(a.tesseract_turkce("windows", False, yaz=lambda s: None))
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

    def test_yazar_sonra_ayni_sonra_kaldirir(self):
        with mock.patch.object(a, "gercek_python3", return_value=None):
            self.assertTrue(a.python3_shim(str(self.py), hedef_dizin=self.dir, yaz=lambda s: None))
            sh, cmd = self.dir / "python3", self.dir / "python3.cmd"
            self.assertIn("py -3", cmd.read_text())
            self.assertTrue(cmd.read_bytes().count(b"\r\n") > 3)
            self.assertNotIn(b"\r", sh.read_bytes())
            self.assertIn(a.ISARET, sh.read_text())
            cikti = []
            a.python3_shim(str(self.py), hedef_dizin=self.dir, yaz=cikti.append)
            self.assertTrue(all("ayni" in s for s in cikti), cikti)
            a.python3_shim(str(self.py), kaldir=True, hedef_dizin=self.dir, yaz=lambda s: None)
            self.assertFalse(sh.exists() or cmd.exists())

    def test_gercek_python3_varsa_yazmaz(self):
        with mock.patch.object(a, "gercek_python3", return_value="C:/Python/python3.exe"):
            self.assertTrue(a.python3_shim(str(self.py), hedef_dizin=self.dir, yaz=lambda s: None))
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_yabanci_dosyaya_dokunmaz(self):
        yabanci = self.dir / "python3.cmd"
        yabanci.write_bytes(b"@echo benim\r\n")
        with mock.patch.object(a, "gercek_python3", return_value=None):
            self.assertFalse(a.python3_shim(str(self.py), hedef_dizin=self.dir, yaz=lambda s: None))
        self.assertEqual(yabanci.read_bytes(), b"@echo benim\r\n")

    def test_yorumlayici_yoksa_yazmaz(self):
        with mock.patch.object(a, "gercek_python3", return_value=None):
            self.assertFalse(a.python3_shim(str(self.dir / "yok.exe"), hedef_dizin=self.dir, yaz=lambda s: None))
        self.assertEqual(list(self.dir.iterdir()), [])

    @unittest.skipUnless(os.name == "nt", "WindowsApps stub is a Windows case")
    def test_store_stub_gercek_sayilmaz(self):
        stub = self.dir / "WindowsApps"
        stub.mkdir()
        (stub / "python3.exe").write_bytes(b"MZ")
        with mock.patch.dict(os.environ, {"PATH": str(stub)}):
            self.assertIsNone(a.gercek_python3())


if __name__ == "__main__":
    unittest.main()
