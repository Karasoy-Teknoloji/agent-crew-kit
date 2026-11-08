#!/usr/bin/env bash
# Installer for kota-temposu (Linux/macOS).
#   bash kur.sh            install / repair
#   bash kur.sh --whatif   show the changes, write nothing
#   bash kur.sh --kaldir   remove our files and settings entries (also the optional cost statistics; the recorded history stays)
#   bash kur.sh --maliyetsiz  install / repair without the cost statistics (they are part of the default install; --maliyet is accepted and
#                             means the default)
#   bash kur.sh --paylas --plan 100 [--kisi "Ad Soyad"]
#                          join the team usage ranking: checks the name (kisiler.json) and push access, writes ~/.config/claude-shared/{kisi,plan}
#                          and makes the daily job (systemd timer on Linux, launchd agent on macOS) run `maliyet --kaydet --paylas`.
#                          --kisi is only needed when your git user.name differs from your name in kisiler.json.
#   bash kur.sh --son-pencere [--kisi "Ad Soyad"]
#                          opt in to the "son pencere" mode: in the last hours before your weekly quota reset an interactive coordinator
#                          session (a live one of yours, else a visible tmux session `son-pencere-<name>`) uses ALL leftover quota
#                          on pre-approved work (see KURULUM.md). Installs the `son-pencere` command and a 5-minute user timer (systemd);
#                          runs are logged in claude-shared raporlar/son-pencere/, so it needs the same name/push access as --paylas (not the plan).
#   bash kur.sh --devam-alarmi [--kisi "Ad Soyad"]
#                          opt in to the 5-hour resume alarm (same son-pencere command and timer, every 5 min): once the 5-hour window that
#                          stopped work has reset, every live Claude session still stopped by that limit gets one "continue" relay. The same
#                          timer runs burn mode (`son-pencere bitir --ac`, developers only). Saved for bare re-runs.
#   bash kur.sh --slack-triage
#                          run the Slack/mail request triage on this machine too (systemd user timer every 2-4 min, random; the
#                          one developer's machine owns triage until a request/ack handover; tools/slack-triage/README.md "Several machines").
#   bash kur.sh --bayat
#                          Always-on Linux host only (agent-host): systemd user timer Mon-Fri 09:30 Europe/Istanbul (Persistent) running
#                          tools/slack-triage/bayat.py from this clone (stale-branch cleanup + report; a team add-on, README "Optional team add-ons").
#   bash kur.sh --slack-gelen
#                          opt in to the token-free Slack inbox poller for whoever runs the installer (Linux/macOS; on Windows use kur.ps1 -SlackGelen).
#                          Runs tools/slack-triage/gelen.py from this clone every 5 min (systemd user timer on Linux, launchd agent
#                          on macOS; elsewhere a crontab line is printed), links ~/.claude/bin/slack-gelen, and adds SessionStart + UserPromptSubmit hooks that
#                          print "Slack: N yeni" (the status line shows it too). The Slack user is detected from the claude CLI's Slack login (auth.test). Saved for bare re-runs.
#   bash kur.sh            (default = direct source) the scripts run straight from this claude-shared clone, nothing is copied into
#                          ~/.claude/bin: settings.json (statusLine, hooks), the maliyet command and the daily job point into the clone, so
#                          updates arrive with git pull, which tools/rules_watch.py does at session start with a self-check and a rollback.
#                          Copies left by an older install are backed up (.yedek-<date>) and removed. Every run keeps the
#                          --paylas/--kisi/--plan/--son-pencere options saved by the last install and adds the flags given (none is dropped; --kaldir undoes all).
#   bash kur.sh            (every mode) also installs the team's lost-work protection from this clone: the SessionStart hook
#                          tools/rules_watch.py (rule warnings + own unpushed/uncommitted work report) and a daily systemd user timer
#                          'kayip-is' running tools/kayip_is.py --push-unpushed (own branches only, never main/master/test/dev, never force).
#   bash kur.sh            (every mode) also writes the `gh` shim ~/.local/bin/gh -> tools/gh_sinir.py of this clone (machine-wide GitHub
#                          API limiter: concurrency, write spacing, secondary-limit retry, ETag cache, log; see docs in the tool).
#   bash kur.sh            (every mode) also prints the tool check (tools/arac_kontrol.py: python3, git, gh, jq, poppler, tesseract, shellcheck, ...).
#   bash kur.sh --araclar  consent flag of that check: also installs the missing tools (apt-get on Linux, brew on macOS; pip packages with
#                          --user); see docs/arac-gereksinimleri.md. Without it the check only reports. Not saved for bare re-runs.
#   bash kur.sh            (every mode) also links the team agents: ~/.claude/agents/claude-shared -> <clone>/agents (a symlink; a link,
#                          not a copy; own agents in ~/.claude/agents are not touched; --kaldir removes the link), and enables the plugin
#                          plugins/ekip-ajanlari (tools, output style) from this clone: settings.json declares the claude-shared
#                          marketplace as a local directory source (plugins load in place, no copy), enables ekip-ajanlari@claude-shared
#                          and `claude plugin install` registers it (idempotent, skipped when `claude` is not on PATH); old-name agent
#                          files in ~/.claude/agents are backed up (.yedek-<date>) and removed.
#   bash kur.sh --kopya    legacy copy mode (scripts copied into ~/.claude/bin; every update needs a re-run). --repodan is accepted (= default).
# The real work is in kur_ortak.py (shared with kur.ps1); this script only finds python3 and checks that jq (the status line needs it) exists.
set -eu
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
extra=()
quiet=0
araclar=0
need_val() { [ "$#" -ge 2 ] || { echo "HATA: $1 bir deger ister (ornek: --kisi \"Ad Soyad\", --plan 100)."; exit 2; }; }
while [ "$#" -gt 0 ]; do
  case "$1" in
    --whatif|-n) extra+=(--whatif); quiet=1 ;;
    --kaldir) extra+=(--remove); quiet=1 ;;
    --maliyet) extra+=(--maliyet) ;;
    --maliyetsiz) extra+=(--maliyetsiz) ;;
    --paylas) extra+=(--paylas) ;;
    --son-pencere) extra+=(--son-pencere) ;;
    --devam-alarmi) extra+=(--devam-alarmi) ;;
    --slack-gelen) extra+=(--slack-gelen) ;;
    --slack-triage) extra+=(--slack-triage) ;;
    --bayat) extra+=(--bayat) ;;
    --araclar) extra+=(--araclar); araclar=1 ;;
    --repodan) extra+=(--repodan) ;;
    --kopya) extra+=(--kopya) ;;
    --kisi|--plan) need_val "$@"; extra+=("$1" "$2"); shift ;;
    *) echo "Bilinmeyen secenek: $1 (kullanilabilir: --whatif, --kaldir, --maliyetsiz, --paylas, --son-pencere, --devam-alarmi, --slack-gelen, --slack-triage, --bayat, --araclar, --repodan, --kopya, --kisi \"Ad Soyad\", --plan 100|200)"; exit 2 ;;
  esac
  shift
done
py="$(command -v python3 || true)"
if [ -z "$py" ] || ! "$py" -c 'import sys; sys.exit(0 if sys.version_info[0] == 3 else 1)' 2>/dev/null; then
  echo "HATA: python3 bulunamadi. macOS: 'brew install python' (ya da 'xcode-select --install'); Debian/Ubuntu: 'sudo apt install python3'."; exit 2
fi
# the status line (statusline.sh) is a jq program: without jq it prints nothing, so this is an error (not for --whatif / --kaldir)
# (with --araclar the tool check installs jq below, so the refusal only applies without it)
if [ "$quiet" -eq 0 ] && [ "$araclar" -eq 0 ] && ! command -v jq >/dev/null 2>&1; then
  echo "HATA: jq kurulu degil; durum cubugu (statusline.sh) jq gerektirir. Once kurun: macOS 'brew install jq'; Debian/Ubuntu 'sudo apt install jq'; Fedora 'sudo dnf install jq'; Arch 'sudo pacman -S jq'; or let the installer do it: bash kur.sh --araclar."
  exit 2
fi
exec "$py" -I "$here/kur_ortak.py" --python "$py" ${extra[@]+"${extra[@]}"}
