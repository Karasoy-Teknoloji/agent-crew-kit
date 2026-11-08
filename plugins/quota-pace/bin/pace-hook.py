"""UserPromptSubmit/SessionStart hook: tell Claude the quota pace mode, only when it changes.

The status line writes ~/.claude/pace.json from Claude Code's rate_limits data (no tokens spent).
This hook prints one context line at session start and whenever the mode changes; otherwise nothing.
"""
import json, os, sys, time
try:
    sys.stdout.reconfigure(encoding='utf-8')  # the hook text holds non-ASCII signs; the Windows pipe default is the ANSI code page
except Exception:
    pass

home = os.path.expanduser('~/.claude')
try:
    hook = json.load(sys.stdin)
except Exception:
    hook = {}
try:
    with open(os.path.join(home, 'pace.json'), encoding='utf-8-sig') as f:
        p = json.load(f)
except Exception:
    sys.exit(0)
if time.time() - p.get('updated', 0) > 3 * 3600:
    sys.exit(0)  # stale data: say nothing

# A reset time that is missing or not within the coming 8 days (e.g. 4102444800 = year 2100) is not a real reset:
# unknown -> say nothing, no mode change (older status lines could still write such a pace.json).
try:
    _r = float(p.get('week_resets_at') or 0)
except (TypeError, ValueError):
    _r = 0
if not (time.time() < _r <= time.time() + 8 * 86400):
    sys.exit(0)

sid = hook.get('session_id', '?')
state_path = os.path.join(home, 'pace-announced.json')
try:
    with open(state_path, encoding='utf-8') as f:
        seen = json.load(f)
except Exception:
    seen = {}
high = float(p.get('week_used', 0)) >= 90
# 95% hand-over rule: applies only before the target window (now < target_at). The 95 is absolute (a buffer under the hard limit),
# not a share of the goal (target_percent): with a 90% goal it fires 5 points above the goal, with a 100% goal 5 points below it.
# target_at missing -> week_resets_at - target_hours*3600; both missing -> rule skipped.
def handover_applies(p, now=None):
    now = time.time() if now is None else now
    if float(p.get('week_used', 0)) < 95:
        return False
    tgt_at = p.get('target_at')
    if tgt_at is None:
        if p.get('target_hours') is None or p.get('week_resets_at') is None:
            return False
        tgt_at = float(p['week_resets_at']) - float(p['target_hours']) * 3600
    return now < float(tgt_at)
handover = handover_applies(p)
key = p['mode'] + ('|95' if handover else '|90' if high else '')
if seen.get(sid) == key and hook.get('hook_event_name') != 'SessionStart':
    sys.exit(0)
seen[sid] = key
seen = dict(list(seen.items())[-50:])
try:
    with open(state_path, 'w', encoding='utf-8') as f:
        json.dump(seen, f)
except Exception:
    pass

DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']  # fixed names, never the locale's %a (it differs per machine and language)


def when(ts):
    lt = time.localtime(ts)
    return f"{DAYS[lt.tm_wday]} {time.strftime('%d.%m %H:%M', lt)}"


reset = when(p['week_resets_at'])
r = lambda x: int(float(x) + 0.5)  # same rounding as the status line
# 5-hour window: information only, mentioned while Claude Code reports it (the status line writes five_hour_used null otherwise).
# It is not part of the mode and restricts no work: when it fills, work pauses until it resets and then continues.
try:
    five_sentence = f"5-hour window {r(p['five_hour_used'])}% used"
    try:
        five_sentence += f", resets {time.strftime('%H:%M', time.localtime(float(p['five_hour_resets_at'])))}" if float(p['five_hour_resets_at']) > time.time() else ""
    except (KeyError, TypeError, ValueError):
        pass
    five_sentence += " (information only, restricts no work). "
except (KeyError, TypeError, ValueError):
    five_sentence = ""
# Forecast target: the reset minus N hours (pace_forecast.py, PACE_TARGET_HOURS); N=0 or missing = the reset itself
th = float(p.get('target_hours') or 0)
if p.get('target_at') and th > 0:
    tgt = when(p['target_at'])
    try:
        goal = float(p.get('target_percent') or 100)   # the weekly % to be at the target (pace-plan.json target_percent); 100 = not shown
    except (TypeError, ValueError):
        goal = 100.0
    where = f"at the target ({tgt}, reset -{th:g}h" + (f", goal {goal:g}%" if goal < 100 else "") + ")"
    past = time.time() >= p['target_at']
else:
    where = f"at the reset ({reset})"
    past = False
if past:
    print(f"[Quota pace: {p['mode']}] Weekly limit {r(p['week_used'])}% used. The target ({tgt}, reset -{th:g}h) has passed; "
          f"the reset is {reset}. Unused quota is lost at the reset, so spend it. "
          f"{five_sentence}Follow the quota pace rules in CLAUDE.md for this mode.")
elif 'p50' in p:
    # Forecast from bin/pace_forecast.py, copied into pace.json by the status line
    print(f"[Quota pace: {p['mode']}] Weekly limit {r(p['week_used'])}% used. Forecast {where}: "
          f"~{r(p['p50'])}% (likely {r(p['p10'])}-{r(p['p90'])}%), from {p['rate']:.1f}% per active hour and "
          f"~{r(p['active_hours_left'])} active hours left. "
          + (f"At this pace the limit runs out around {p['runout_label']}, {p['idle_label']} before the reset. "
             if p.get('runout_label') else "")
          + f"{five_sentence}Follow the quota pace rules in CLAUDE.md for this mode.")
else:
    print(f"[Quota pace: {p['mode']}] Weekly limit {r(p['week_used'])}% used with {p['week_elapsed']}% of the week gone; "
          f"at this pace ~{p['week_projected']}% by the reset ({reset}). {five_sentence}"
          f"Follow the quota pace rules in CLAUDE.md for this mode.")
if handover:
    print("[Quota >= 95%, before the target window] Hand over all work now (shared rule \"Hand-over at 95%\"): "
          "push every In Progress task (WIP if needed), post a done/remaining/next hand-over comment on its issue, "
          "unassign yourself and move the card back to \"AI queue\", assign no one else, post \"handed-over\" (no @person) in its Slack thread, "
          "and start nothing new.")
elif high:
    print("[Quota >= 90%] Weekly use at 90% or more: keep working. Work the user asks for starts immediately, and work that "
          "hits the limit resumes after the reset. Commit and push every change as you go, merge each green piece to dev, "
          "push unfinished work to its branch and note it, remove finished worktrees.")
