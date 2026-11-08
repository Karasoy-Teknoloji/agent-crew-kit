# Required tools / missing tools

Agents and the scripts in this repo call a small set of command-line tools. A machine that lacks one fails in a confusing way (for example Claude Code's Read tool cannot open a PDF without `pdftoppm`, and `python3` on a stock Windows 11 is only the Microsoft Store stub). The list lives in one place, `tools/arac_kontrol.py` (`ARACLAR`); the installers run it, so a new machine is completed with one flag.

## Check and complete

| OS | Report only (default) | Install the missing tools (consent flag) |
|---|---|---|
| Windows | `pwsh -File plugins/kota-temposu/kur.ps1` (every install prints the report) or `py tools/arac_kontrol.py` | `pwsh -File plugins/kota-temposu/kur.ps1 -Araclar` or `py tools/arac_kontrol.py --kur` |
| Linux / macOS | `bash plugins/kota-temposu/kur.sh` or `python3 tools/arac_kontrol.py` | `bash plugins/kota-temposu/kur.sh --araclar` or `python3 tools/arac_kontrol.py --kur` |

- Without the flag nothing is installed: the check only prints `tamam` / `EKSIK` per tool and the one-line command for this OS. `--kur` with `--whatif` (`-WhatIf`) prints the commands and runs nothing.
- With the flag: winget on Windows, `apt-get` on Linux (with `sudo`; `apt-get update` runs first), `brew` on macOS; Python packages with `pip install --user` (on Linux with apt: the distro package, because of PEP 668). The check runs again afterwards and prints the result; the exit code is non-zero when an install failed or a required tool is still missing.
- A freshly installed tool is on PATH in new terminals and sessions; restart Claude Code after the first run. The check itself re-reads the registry PATH, so it sees the new tools at once.
- Docker, govc and rsync-on-Windows are never installed by the check (system setup or no package); it prints what to do instead.
- Not saved for bare re-runs: the consent is given per run.

## Windows `python3`

Windows 11 has the `py` launcher and `python.exe`, but `python3` resolves to the Store stub in `WindowsApps`, which opens the Store instead of running Python. Many docs and agents call `python3`. Every `kur.ps1` run therefore writes a shim to `~/.local/bin` (the directory of the `gh` shim; `kur.ps1 -Araclar` also puts it first on the user PATH when it is missing):

- `python3.cmd` for cmd and pwsh: `py -3 %*`, falling back to the interpreter found at install time when `py` is missing.
- `python3` (sh) for Git Bash: the same.

It is written only when no real `python3` is on PATH (the Store stub and our own shim do not count) and never over a file that is not ours. `kur.ps1 -Kaldir` removes it.

## Tools

`Who uses it` shows the first places in this repo (file:line); "agent need" means the tool is called from agent sessions, not from a repo file.

| Tool | Level | Who uses it | Windows (winget id) | Linux (apt) |
|---|---|---|---|---|
| `python3` | required | `plugins/kota-temposu/kur.sh:63`, `plugins/kota-temposu/statusline.sh:54`, `plugins/ekip-ajanlari/tests/test_ajanlar.py:3` | shim (above); real: `Python.Python.3.13` | `python3` |
| `git` | required | `plugins/kota-temposu/bin/maliyet_paylas.py:111`, every agent | `Git.Git` | `git` |
| `gh` | required | `tools/gh_board.py:42`, `plugins/kota-temposu/bin/son_pencere.py:304`, shim `tools/gh_sinir.py` | `GitHub.cli` | `gh` |
| `ssh` | required | agent need: remote hosts and lab/build VMs, git over ssh | built in (Windows OpenSSH, Git for Windows); `Microsoft.OpenSSH.Beta` | `openssh-client` |
| `jq` | required | `plugins/kota-temposu/kur.sh:74` (refuses to install the status line without it), `plugins/kota-temposu/statusline.sh:12`, sh scripts and tests | `jqlang.jq` | `jq` |
| `curl` | required | `docs/kasa.md:44`, `tools/kasa.py:5` | built in (Windows 11, Git for Windows); `cURL.cURL` | `curl` |
| `node` / `npx` | recommended | `docs/browser-mcp.md:17` (Playwright and DevTools MCP servers) | `OpenJS.NodeJS.LTS` | `nodejs npm` |
| `pdftoppm`, `pdftotext` (poppler) | recommended | agent need: Claude Code's Read tool renders PDF pages with `pdftoppm` | `oschwartz10612.Poppler` | `poppler-utils` |
| `pypdf` (Python package) | recommended | agent need: PDF text extraction from Python | `pip install --user pypdf` | `python3-pypdf` |
| `tesseract` (OCR) + Turkish data | recommended | agent need: scanned PDFs and images | `UB-Mannheim.TesseractOCR` (needs administrator rights; see below) | `tesseract-ocr tesseract-ocr-tur` |
| `shellcheck` | recommended | sh script reviews, `actionlint` calls it for `run:` steps | `koalaman.shellcheck` | `shellcheck` |
| `actionlint` | recommended | agent need: `.github/workflows/*.yml` reviews | `rhysd.actionlint` | `actionlint` (Debian 13 / Ubuntu 24.10 or newer; otherwise the release binary of github.com/rhysd/actionlint) |
| `openssl` | optional | agent need: certificate and key checks | built in with Git for Windows (`usr/bin`); `ShiningLight.OpenSSL.Light` | `openssl` |
| `dig` / `nslookup` | optional | `rules/WORKING-RULES.md:70`, `agents/operations-sm.md:14` (live DNS evidence for usage claims) | `nslookup` is built in; `dig`: `ISC.Bind` (manual) | `dnsutils` |
| `rsync` | optional | agent need: file sync with Linux hosts | no package; use `scp` / `robocopy` | `rsync` |
| `docker` | optional | agent need: lab services and containers | `Docker.DockerDesktop` (manual) | `docker.io` (manual) |
| `govc` | optional | agent need: vSphere / vCenter lab VM work | no package: release of github.com/vmware/govmomi on PATH | same |

macOS uses `brew install <name>` for the same tools (`poppler`, `jq`, `shellcheck`, `tesseract`, `node`, ...).

## Notes per tool

- **tesseract on Windows**: the installer needs administrator rights and cannot be installed per user (winget has no user scope; run non-elevated it ends with `0x800704c7`, "cancelled by the user"). Run `kur.ps1 -Araclar` once from an elevated PowerShell. The installer ships English and OSD data only: the check then downloads `tur.traineddata` (tessdata_fast) into the install's `tessdata` folder, or, when that is not writable, into `~/.local/share/tessdata` (with `eng`/`osd` copied) and sets the user variable `TESSDATA_PREFIX`. On Linux the `tesseract-ocr-tur` package carries it. `tesseract --list-langs` must list `tur`.
- **poppler** is a portable winget package; its directory is added to PATH by winget. Open a new terminal (or restart Claude Code) before expecting `pdftoppm` there.
- **actionlint** runs without `shellcheck`, but then skips the shell checks of `run:` steps; install both.
- **Lab / build VMs** are not completed with this check: they are shared and identical and change only through the team's provisioning script or lab installer. Build toolchains (for example the Visual Studio MSBuild that WebForms builds need) are such lab-VM items, never installed onto a workstation.

## Adding a tool

Add one `Arac(...)` line to `ARACLAR` in `tools/arac_kontrol.py` (commands to probe, level, winget id, apt package, optional brew / pip) and one row to the table above. There is no second list: the installers, the report and the tests read `ARACLAR`.
