#!/usr/bin/env bash
# Installer for quota-pace (Linux/macOS).
#   bash install.sh            install / repair
#   bash install.sh --whatif   show the changes, write nothing
#   bash install.sh --remove   remove our files and settings entries (also the optional cost statistics; the recorded history stays)
#   bash install.sh --free  install / repair without the cost statistics (they are part of the default install; --cost is accepted and
#                             means the default)
#   bash install.sh --share --plan 100 [--person "Ad Soyad"]
#                          join the team usage ranking: checks the name (kisiler.json) and push access, writes ~/.config/claude-shared/{kisi,plan}
#                          and makes the daily job (systemd timer on Linux, launchd agent on macOS) run `cost --save --share`.
#                          --person is only needed when your git user.name differs from your name in kisiler.json.
#   bash install.sh --last-window [--person "Ad Soyad"]
#                          opt in to the "son pencere" mode: in the last hours before your weekly quota reset an interactive coordinator
#                          session (a live one of yours, else a visible tmux session `last-window-<name>`) uses ALL leftover quota
#                          on pre-approved work (see INSTALL.md). Installs the `last-window` command and a 5-minute user timer (systemd);
#                          runs are logged in claude-shared raporlar/last-window/, so it needs the same name/push access as --share (not the plan).
#   bash install.sh --resume-alarm [--person "Ad Soyad"]
#                          opt in to the 5-hour resume alarm (same last-window command and timer, every 5 min): once the 5-hour window that
#                          stopped work has reset, every live Claude session still stopped by that limit gets one "continue" relay. The same
#                          timer runs burn mode (`last-window burn --on`, developers only). Saved for bare re-runs.
#   bash install.sh --slack-triage
#                          run the Slack/mail request triage on this machine too (systemd user timer every 2-4 min, random; the
#                          one developer's machine owns triage until a request/ack handover; tools/slack-triage/README.md "Several machines").
#   bash install.sh --stale
#                          Always-on Linux host only (agent-host): systemd user timer Mon-Fri 09:30 Europe/Istanbul (Persistent) running
#                          tools/slack-triage/bayat.py from this clone (stale-branch cleanup + report; a team add-on, README "Optional team add-ons").
#   bash install.sh --slack-inbox
#                          opt in to the token-free Slack inbox poller for whoever runs the installer (Linux/macOS; on Windows use install.ps1 -SlackInbox).
#                          Runs tools/slack-triage/gelen.py from this clone every 5 min (systemd user timer on Linux, launchd agent
#                          on macOS; elsewhere a crontab line is printed), links ~/.claude/bin/slack-inbox, and adds SessionStart + UserPromptSubmit hooks that
#                          print "Slack: N yeni" (the status line shows it too). The Slack user is detected from the claude CLI's Slack login (auth.test). Saved for bare re-runs.
#   bash install.sh            (default = direct source) the scripts run straight from this claude-shared clone, nothing is copied into
#                          ~/.claude/bin: settings.json (statusLine, hooks), the cost command and the daily job point into the clone, so
#                          updates arrive with git pull, which tools/rules_watch.py does at session start with a self-check and a rollback.
#                          Copies left by an older install are backed up (.yedek-<date>) and removed. Every run keeps the
#                          --share/--person/--plan/--last-window options saved by the last install and adds the flags given (none is dropped; --remove undoes all).
#   bash install.sh            (every mode) also installs the team's lost-work protection from this clone: the SessionStart hook
#                          tools/rules_watch.py (rule warnings + own unpushed/uncommitted work report) and a daily systemd user timer
#                          'lost-work' running tools/lost_work.py --push-unpushed (own branches only, never main/master/test/dev, never force).
#   bash install.sh            (every mode) also writes the `gh` shim ~/.local/bin/gh -> tools/gh_limit.py of this clone (machine-wide GitHub
#                          API limiter: concurrency, write spacing, secondary-limit retry, ETag cache, log; see docs in the tool).
#   bash install.sh            (every mode) also prints the tool check (tools/tool_check.py: python3, git, gh, jq, poppler, tesseract, shellcheck, ...).
#   bash install.sh --tools  consent flag of that check: also installs the missing tools (apt-get on Linux, brew on macOS; pip packages with
#                          --user); see docs/tool-requirements.md. Without it the check only reports. Not saved for bare re-runs.
#   bash install.sh            (every mode) also links the team agents: ~/.claude/agents/claude-shared -> <clone>/agents (a symlink; a link,
#                          not a copy; own agents in ~/.claude/agents are not touched; --remove removes the link), and enables the plugin
#                          plugins/crew-agents (tools, output style) from this clone: settings.json declares the claude-shared
#                          marketplace as a local directory source (plugins load in place, no copy), enables crew-agents@claude-shared
#                          and `claude plugin install` registers it (idempotent, skipped when `claude` is not on PATH); old-name agent
#                          files in ~/.claude/agents are backed up (.yedek-<date>) and removed.
#   bash install.sh --copy    legacy copy mode (scripts copied into ~/.claude/bin; every update needs a re-run). --repodan is accepted (= default).
# The real work is in install_common.py (shared with install.ps1); this script only finds python3 and checks that jq (the status line needs it) exists.
set -eu
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
extra=()
quiet=0
araclar=0
need_val() { [ "$#" -ge 2 ] || { echo "HATA: $1 bir deger ister (ornek: --person \"Ad Soyad\", --plan 100)."; exit 2; }; }
while [ "$#" -gt 0 ]; do
  case "$1" in
    --whatif|-n) extra+=(--whatif); quiet=1 ;;
    --remove) extra+=(--remove); quiet=1 ;;
    --cost) extra+=(--cost) ;;
    --free) extra+=(--free) ;;
    --share) extra+=(--share) ;;
    --last-window) extra+=(--last-window) ;;
    --resume-alarm) extra+=(--resume-alarm) ;;
    --slack-inbox) extra+=(--slack-inbox) ;;
    --slack-triage) extra+=(--slack-triage) ;;
    --stale) extra+=(--stale) ;;
    --tools) extra+=(--tools); araclar=1 ;;
    --repodan) extra+=(--repodan) ;;
    --copy) extra+=(--copy) ;;
    --person|--plan) need_val "$@"; extra+=("$1" "$2"); shift ;;
    *) echo "Bilinmeyen secenek: $1 (kullanilabilir: --whatif, --remove, --free, --share, --last-window, --resume-alarm, --slack-inbox, --slack-triage, --stale, --tools, --repodan, --copy, --person \"Ad Soyad\", --plan 100|200)"; exit 2 ;;
  esac
  shift
done
py="$(command -v python3 || true)"
if [ -z "$py" ] || ! "$py" -c 'import sys; sys.exit(0 if sys.version_info[0] == 3 else 1)' 2>/dev/null; then
  echo "HATA: python3 bulunamadi. macOS: 'brew install python' (ya da 'xcode-select --install'); Debian/Ubuntu: 'sudo apt install python3'."; exit 2
fi
# the status line (statusline.sh) is a jq program: without jq it prints nothing, so this is an error (not for --whatif / --remove)
# (with --tools the tool check installs jq below, so the refusal only applies without it)
if [ "$quiet" -eq 0 ] && [ "$tools" -eq 0 ] && ! command -v jq >/dev/null 2>&1; then
  echo "HATA: jq kurulu degil; durum cubugu (statusline.sh) jq gerektirir. Once kurun: macOS 'brew install jq'; Debian/Ubuntu 'sudo apt install jq'; Fedora 'sudo dnf install jq'; Arch 'sudo pacman -S jq'; or let the installer do it: bash install.sh --tools."
  exit 2
fi
exec "$py" -I "$here/install_common.py" --python "$py" ${extra[@]+"${extra[@]}"}
