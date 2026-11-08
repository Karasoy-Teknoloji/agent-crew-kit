#!/usr/bin/env bash
# Claude Code status line: model · effort | %ctx (used/size) | HH:MM | [branch] dir
d=$(cat)
# Sibling scripts sit in bin/ next to this file: ~/.claude/bin for the copied install, plugins/kota-temposu/bin when run from the
# claude-shared clone (kur.sh --repodan). Runtime data (pace.json, history, maliyet/) always stays under ~/.claude.
case "${BASH_SOURCE[0]}" in */*) kt="${BASH_SOURCE[0]%/*}" ;; *) kt=. ;; esac
# Live-session guard (same rule as statusline.ps1): only a payload from a live Claude Code session may write the live state. ~/.claude/sessions/<pid>.json
# lists the live sessions; a payload whose session_id is not in it (test fixture, benchmark against the real profile) is rendered but writes nothing.
# No registry, or none with a sessionId = cannot judge = write as before. Incident 05.11.2026: a fixture run made the pace hook say "SIKI, 63%" at a real 54%.
canli=1
if grep -qs '"sessionId"' ~/.claude/sessions/*.json 2>/dev/null; then
  sid=$(jq -r '.session_id // empty' <<<"$d" 2>/dev/null)
  case "$sid" in ''|*[!A-Za-z0-9_-]*) canli=0 ;; *) grep -qsE "\"sessionId\" *: *\"$sid\"" ~/.claude/sessions/*.json 2>/dev/null || canli=0 ;; esac
fi
# Debug aid (kept permanently): the last stdin JSON Claude Code sent, ~/.claude/statusline-girdi.json (0600, overwritten, atomic)
[ "$canli" = 1 ] && ( umask 077; printf '%s' "$d" > ~/.claude/statusline-girdi.json.tmp$$ && mv -f ~/.claude/statusline-girdi.json.tmp$$ ~/.claude/statusline-girdi.json ) 2>/dev/null
j() { jq -r "$1 // empty" <<<"$d"; }
dir=$(j .workspace.current_dir)
branch=$(git -C "$dir" branch --show-current 2>/dev/null)
effort=$(j .effort.level)
if [ -z "$effort" ]; then
  mid=$(j .model.id)
  e=$(jq -r --arg m "$mid" '.modelSettings[$m].effortLevel // .effortLevel // empty' ~/.claude/settings.json 2>/dev/null)
  effort="${e:-high}*"
fi
# model name without the "(1M context)" suffix; the effective window is shown next to it
name=$(j .model.display_name); name="${name% (*context)}"
out="$name · $effort"
pct=$(j .context_window.used_percentage)
if [ -n "$pct" ]; then
  used=$(jq -r '.context_window.current_usage | ((.input_tokens//0)+(.cache_creation_input_tokens//0)+(.cache_read_input_tokens//0))' <<<"$d")
  size=$(j .context_window.context_window_size)
  fmt() { awk -v n="$1" 'BEGIN{ if (n>=1000000) { s=sprintf("%.1f",n/1000000); sub(/\.0$/,"",s); printf "%sM",s } else printf "%.0fK", n/1000 }'; }
  # effective window: the auto-compact window when it is smaller than the model's window
  acw="${CLAUDE_CODE_AUTO_COMPACT_WINDOW:-$(jq -r --arg m "$(jq -r '.model.id // ""' <<<"$d" | sed -E 's/\[.*\]$//; s/-[0-9]{8}$//')" '.env.CLAUDE_CODE_AUTO_COMPACT_WINDOW // .modelSettings[$m].autoCompactWindow // .autoCompactWindow // empty' ~/.claude/settings.json 2>/dev/null)}"
  case "$acw" in *[kK]) acw=$(( ${acw%[kK]} * 1000 )) ;; *[mM]) acw=$(( ${acw%[mM]} * 1000000 )) ;; esac
  size=${size%.*}
  if [[ "$acw" =~ ^[0-9]+$ ]] && [ "$acw" -gt 0 ] && { [ -z "$size" ] || [ "$acw" -lt "$size" ]; }; then size=$acw; fi
  pct=$(awk -v u="$used" -v s="${size:-0}" 'BEGIN{ if (s>0) printf "%.0f", 100*u/s; else print 0 }')
  out+=" | %$pct ($(fmt "$used")/$(fmt "${size:-0}"))"
fi
# Optional cost statistics switch (see the "Optional cost statistics" block at the end): off unless bin/maliyet.py exists next to this file;
# MALIYET_DURUM=0 or the file ~/.claude/maliyet/kapali turns it off without uninstalling.
mal=0
if [ -f "$kt/bin/maliyet.py" ] && [ "${MALIYET_DURUM:-1}" != 0 ] && [ ! -e ~/.claude/maliyet/kapali ]; then mal=1; fi
# "oturum" (session total) and the last action's cost come from one `maliyet.py --durum --girdi` call (it reads this JSON from stdin,
# keeps the per-prompt baseline and formats the amounts in lira; line 1 is also the "son 24sa" string). Plain Python, no extra tools.
malsatir=()
if [ "$mal" = 1 ]; then
  i=0
  while IFS= read -r l || [ -n "$l" ]; do malsatir[$i]="$l"; i=$((i + 1)); done < <(printf '%s' "$d" | python3 -S -E "$kt/bin/maliyet.py" --durum --girdi 2>/dev/null)
fi
# HH:MM of an epoch in local time, portable (GNU `date -d @N` is not available on macOS/BSD; python3 is already required)
hhmm() { python3 -S -E -c 'import sys,time;sys.stdout.write(time.strftime("%H:%M",time.localtime(int(sys.argv[1]))))' "$1" 2>/dev/null; }
out+=" | $(date +%H:%M)"
# KOTA_ISLEM_TUTARI=0 hides the last action's amounts after the clock (personal preference, default shown)
[ "${KOTA_ISLEM_TUTARI:-1}" != 0 ] && [ -n "${malsatir[2]:-}" ] && out+=" ${malsatir[2]}"
# Quota pace: where the weekly limit lands at the forecast target (reset minus PACE_HEDEF_SAAT hours, default 5; 0 = the reset) -> BOL/NORMAL/SIKI, written to pace.json for the hook. The mode is weekly only; the 5-hour window is displayed, never part of it.
# bin/pace_forecast.py gives the median and a P10-P90 range from the active rate and the remaining work hours;
# used / share of the week elapsed is the fallback while there is no fresh forecast.
# A session that has not called the API lately reports older, lower values: keep the highest value of the same window.
nowts=$(date +%s)
prev=$(cat ~/.claude/pace.json 2>/dev/null); jq -e . >/dev/null 2>&1 <<<"$prev" || prev='{}'
fc=$(cat ~/.claude/pace-forecast.json 2>/dev/null); jq -e . >/dev/null 2>&1 <<<"$fc" || fc='{}'
pace=$(jq -c --argjson now "$nowts" --argjson prev "$prev" --argjson fc "$fc" '.rate_limits // empty | select(.seven_day) |
  # Only the overall weekly limit (.seven_day) drives the mode. A reset time that is missing or not within the coming
  # 8 days (e.g. the 4102444800 placeholder = year 2100) is not a real reset: skip the whole reading, no pace.json write.
  select((.seven_day.resets_at | type) == "number" and .seven_day.resets_at > $now and .seven_day.resets_at <= $now + 691200) |
  .seven_day.resets_at as $r7 | ((.five_hour.resets_at // 0) | if type == "number" and . > $now and . <= $now + 21600 then . else 0 end) as $r5 |
  # A reading at least as recent as the last one (same or newer 5-hour window, 5h value not lower) is current:
  # take its weekly value even when lower, since a manual limit reset keeps the same weekly resets_at.
  # $fresh needs the 5-hour window and only matters for the SAME weekly resets_at. A new week (different seven_day.resets_at) never
  # carries the old value over (see the $prev.week_resets_at == $r7 test below), so a weekly reset works without a 5-hour window too.
  ((.five_hour.used_percentage // 0) as $u5a | $r5 > 0 and ($r5 > ($prev.five_hour_resets_at // 0) or ($r5 == $prev.five_hour_resets_at and $u5a >= ($prev.five_hour_used // 0)))) as $fresh |
  ([.seven_day.used_percentage, (if $prev.week_resets_at == $r7 and ($fresh | not) then $prev.week_used else 0 end)] | max) as $u7 |
  # No five_hour.used_percentage in the input = the 5-hour window is not reported (docs: each window may be absent): $u5 is null, never 0.
  (if (.five_hour.used_percentage | type) == "number" then ([.five_hour.used_percentage, (if $r5 > 0 and $prev.five_hour_resets_at == $r5 then $prev.five_hour_used else 0 end)] | max) else null end) as $u5 |
  ([[1 - (($r7 - $now) / 604800), 0.05] | max, 1] | min) as $el |
  (($u7 / $el + 0.5) | floor) as $simple |
  {week_used: $u7, week_elapsed: (($el*100)|floor), five_hour_used: $u5, week_resets_at: $r7, five_hour_resets_at: $r5, updated: $now} +
  (if $fc.week_resets_at == $r7 and $fc.p50 != null and ($now - ($fc.updated // 0)) < 3600 then
     {week_projected: (($fc.p50 + 0.5) | floor), p10: $fc.p10, p50: $fc.p50, p90: $fc.p90, rate: $fc.rate,
      active_hours_left: $fc.active_hours_left, mode_reason: $fc.mode_reason,
      target_at: $fc.target_at, target_hours: $fc.target_hours, target_percent: $fc.target_percent,
      runout_at: $fc.runout_at, runout_label: $fc.runout_label, idle_label: $fc.idle_label,
      mode: $fc.mode}
   else
     {week_projected: $simple, mode: (if $simple > 105 then "SIKI" elif $simple < 80 then "BOL" else "NORMAL" end)}
   end)' <<<"$d" 2>/dev/null)
if [ -n "$pace" ]; then
  if [ "$canli" = 1 ]; then
  # history for the forecast: a row when a value changes, else every 10 minutes (no tokens)
  last=$(jq -r '.logged_at // 0 | floor' <<<"$prev" 2>/dev/null); [ -n "$last" ] || last=0
  changed=$(jq -rn --argjson a "$prev" --argjson b "$pace" 'if ($a.week_used != $b.week_used or $a.five_hour_used != $b.five_hour_used) then 1 else 0 end' 2>/dev/null)
  if [ "$changed" = 1 ] || [ $(( nowts - last )) -ge 600 ]; then
    hist=~/.claude/pace-history.csv
    [ -f "$hist" ] || echo 'ts,week_used,five_hour_used,week_resets_at,five_hour_resets_at,host' > "$hist"
    jq -r --arg h "$(hostname -s 2>/dev/null)" '"\(.updated),\(.week_used),\(.five_hour_used // ""),\(.week_resets_at),\(if .five_hour_resets_at > 0 then .five_hour_resets_at else "" end),\($h)"' <<<"$pace" >> "$hist"
    last=$nowts
  fi
  pace=$(jq -c --argjson l "$last" '. + {logged_at: $l}' <<<"$pace")
  printf '%s' "$pace" > ~/.claude/pace.json 2>/dev/null
  # refresh the forecast in the background when the weekly value moved or it is 10 minutes old;
  # SCHED_IDLE (chrt -i) runs it only when the CPU is otherwise idle
  fcpy="$kt/bin/pace_forecast.py"; lock=~/.claude/pace-forecast.lock
  if [ -f "$fcpy" ]; then
    stale=$(jq -rn --argjson f "$fc" --argjson p "$pace" 'if ($p.updated - ($f.updated // 0)) >= 600 or $f.week_used != $p.week_used then 1 else 0 end' 2>/dev/null)
    # mtime of the lock: GNU stat -c %Y, else BSD/macOS stat -f %m, else 0 (no lock yet)
    lockage=$(( nowts - $(stat -c %Y "$lock" 2>/dev/null || stat -f %m "$lock" 2>/dev/null || echo 0) ))
    if [ "$stale" = 1 ] && [ "$lockage" -ge 120 ]; then
      (if command -v chrt >/dev/null; then chrt -i 0 python3 "$fcpy"; else nice -n 19 python3 "$fcpy"; fi </dev/null >/dev/null 2>&1 &)
    fi
  fi
  # Team usage ranking: the throttled automatic share (maliyet --paylas --otomatik). Gate = ONE stat of the marker
  # ~/.claude/maliyet/paylas-sonraki, whose mtime is the next due time (the job re-aims it after every check from
  # paylas_aralik_saat in pace-plan.json, default 2 h); only a joined machine (--paylas wrote ~/.config/claude-shared/plan) and only
  # when it is due do we look further (the lock: a share still running, younger than 30 min, means wait). Detached, never waited for.
  if [ "$mal" = 1 ] && [ "${KOTA_PAYLAS_OTO:-1}" != 0 ] && [ -f "$kt/bin/maliyet_paylas.py" ]; then
    due=$(stat -c %Y ~/.claude/maliyet/paylas-sonraki 2>/dev/null || stat -f %m ~/.claude/maliyet/paylas-sonraki 2>/dev/null || echo 0)
    if [ "$due" -le "$nowts" ] && [ -f ~/.config/claude-shared/plan ]; then
      plock=$(stat -c %Y ~/.claude/maliyet/paylas.kilit 2>/dev/null || stat -f %m ~/.claude/maliyet/paylas.kilit 2>/dev/null || echo 0)
      if [ "$plock" = 0 ] || [ $(( nowts - plock )) -ge 1800 ]; then
        if command -v setsid >/dev/null; then
          (setsid nohup python3 -E "$kt/bin/maliyet.py" --paylas --otomatik </dev/null >/dev/null 2>&1 &)
        else
          (nohup python3 -E "$kt/bin/maliyet.py" --paylas --otomatik </dev/null >/dev/null 2>&1 &)
        fi
      fi
    fi
  fi
  fi
  r5=$(jq -r '.five_hour_resets_at // 0 | floor' <<<"$pace")
  # 5-hour forecast: projected % at the reset ("→P") or the time the limit runs out ("→bitiş HH:MM"), from the
  # current window's history rows (10-60 min old) or the window average; empty when there is nothing to project
  f5=''
  if [ "$r5" -gt 0 ] && [ $(( r5 - nowts )) -gt 0 ] && [ "$(jq -r '.five_hour_used == null' <<<"$pace")" = false ]; then
    u5n=$(jq -r '.five_hour_used' <<<"$pace")
    fc5=$(tail -n 400 ~/.claude/pace-history.csv 2>/dev/null | awk -F, -v now="$nowts" -v r="$r5" -v u="$u5n" -v ws="$(( r5 - 18000 ))" '
      $5+0 == r+0 && $1+0 >= now-3600 && $1+0 <= now-600 { if (!f || $1+0 < t0) { f=1; t0=$1+0; u0=$3+0 } }
      END {
        if (f) rate = (u-u0) / (now-t0); else { d = now-ws; if (d < 600) d = 600; rate = u/d }
        if (rate <= 0) { print "X"; exit }
        p = u + rate*(r-now)
        if (p >= 100) printf "B %d\n", now + (100-u)/rate; else printf "P %d\n", int(p)
      }')
    case "$fc5" in
      P\ *) f5="→%${fc5#P }" ;;
      B\ *) f5="→bitiş $(hhmm "${fc5#B }")" ;;
    esac
  fi
  if [ "$r5" -gt 0 ]; then l=$(( r5 - nowts )); [ $l -lt 0 ] && l=0; h=$((l/3600)); m=$((l%3600/60)); [ $h -gt 0 ] && r5=" (${h}sa${m}dk)" || r5=" (${m}dk)"; else r5=''; fi
  # past 100%: when the limit runs out and how long it stays empty before the reset
  # The 5sa segment is shown only while Claude Code reports a 5-hour window (five_hour_used null = not reported -> segment dropped)
  out+=" | $(jq -r '(if .five_hour_used != null then "5sa %\(.five_hour_used|floor)'"$f5$r5"' · " else "" end) + "hafta %\(.week_used|floor)→\(if .runout_label then "bitiş \(.runout_label) (\(.idle_label) boş)" else "%\(.week_projected)\(if .p10 != null then ((((.p90 - .p10) / 2 + 0.5) | floor) as $h | if $h >= 1 then "±\($h)" else "" end) else "" end)" end) \({"BOL": "🟢", "NORMAL": "🟡", "SIKI": "🔴"}[.mode] // .mode)"' <<<"$pace")"
fi
# folder: ~ for $HOME, shortened to ~/... below it
case "$dir" in
  "$HOME"|"$HOME/") dir="~" ;;
  "$HOME"/*) dir="~/${dir#"$HOME"/}" ;;
esac
# Slack inbox (tools/slack-triage/gelen.py, kur --slack-gelen): " | Slack: N yeni" while unread items wait. Reads the inbox file and its
# .okundu byte offset directly (no Python spawn, no network, at most 256 KB); a missing file or any error shows nothing.
sg="${SLACK_GELEN_INBOX:-$HOME/.claude/slack-gelen.jsonl}"
if [ -s "$sg" ]; then
  sgo=$(cat "$sg.okundu" 2>/dev/null); case "$sgo" in ''|*[!0-9]*) sgo=0 ;; esac
  sgn=$(tail -c +$((sgo + 1)) "$sg" 2>/dev/null | head -c 262144 | grep -c '[^[:space:]]')
  [ "${sgn:-0}" -gt 0 ] 2>/dev/null && out+=" | Slack: $sgn yeni"
fi
# Burn mode (son-pencere bitir --ac): " | BİTİR" while it is on and its weekly reset has not passed (same rule as statusline.ps1)
bf="$HOME/.claude/son-pencere/bitir.json"
if [ -s "$bf" ]; then
  bhr=$(jq -r 'if .aktif == true then (.hafta_reset // 0 | floor) else 0 end' "$bf" 2>/dev/null)
  case "$bhr" in ''|*[!0-9]*) bhr=0 ;; esac
  [ "$bhr" -gt "$nowts" ] 2>/dev/null && out+=" | BİTİR"
fi
if [ -n "$branch" ] || [ -n "$dir" ]; then
  out+=" |"
  [ -n "$branch" ] && out+=" [$branch]"
  [ -n "$dir" ] && out+=" $dir"
fi
# Optional cost statistics (installed by `kur.sh --maliyet`, see MALIYET.md): cache hit rate + cost segments. Off unless
# bin/maliyet.py exists next to this file; turn off without uninstalling with MALIYET_DURUM=0 or by creating the file ~/.claude/maliyet/kapali
# (the file works even when the environment variable does not reach the status line process).
# Cost is the API list-price equivalent of token usage, not a bill. Nothing here scans transcripts or touches the network:
# --durum only reads ~/.claude/maliyet/durum.cache and, when that is older than 5 minutes, starts ONE detached background refresh (flock).
if [ "$mal" = 1 ]; then
  # prompt cache hit rate of the main conversation (statusline JSON prompt_cache, Claude Code >= 2.1.251): "önb %NN", misses in brackets
  cache=$(jq -r 'if .prompt_cache.hit_ratio != null then "\((.prompt_cache.hit_ratio * 100 + 0.5) | floor) \(.prompt_cache.misses // 0)" else empty end' <<<"$d" 2>/dev/null)
  if [ -n "$cache" ]; then
    read -r hit misses <<<"$cache"
    out+=" | önb %$hit"
    [ "${misses:-0}" -gt 0 ] && out+=" ($misses kaçış)"
  fi
  # "oturum ₺X": cost.total_cost_usd from the statusline JSON (resets to 0 on /clear), shown in lira (maliyet.py --para)
  [ -n "${malsatir[1]:-}" ] && out+=" | oturum ${malsatir[1]}"
  # "son 24sa ₺A (₺a) · 7g ₺B $C (₺b) · 30g ₺D (₺d) · ×N": rolling windows of hourly buckets, advisor included
  [ -n "${malsatir[0]:-}" ] && out+=" | ${malsatir[0]}"
fi
# Wrap at segment boundaries (" | ") when the row is wider than the terminal: same rule as statusline-yerel.ps1 (bin/satir_sar.py; width from
# KOTA_SATIR_GENISLIK / COLUMNS / the tty, none known = no wrapping). Without the helper the line is printed as is.
if [ -f "$kt/bin/satir_sar.py" ]; then
  wrapped=$(printf '%s' "$out" | python3 -S -E "$kt/bin/satir_sar.py" ~/.claude 2>/dev/null) && [ -n "$wrapped" ] && out=$wrapped
fi
printf '%s' "$out"
