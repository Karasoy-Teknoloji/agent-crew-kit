"""The one desktop-notification helper of claude-shared (lost-work warning, Slack inbox).

Windows: a toast through Windows PowerShell 5.1 that lands in the Notification Center (never a modal `msg` box).
Linux: notify-send. Anything else, or a failure, is a quiet no-op. Title/body travel in the environment, never in the command line.
"""
import os
import subprocess

from hidden_loader import hidden

TOAST_PS = (
    "$ErrorActionPreference='Stop';"
    "[void][Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime];"
    "$x=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent('ToastText02');"
    "$n=$x.GetElementsByTagName('text');$n.Item(0).AppendChild($x.CreateTextNode($env:NOTIFIER_TITLE))|Out-Null;"
    "$n.Item(1).AppendChild($x.CreateTextNode($env:NOTIFIER_BODY))|Out-Null;"
    r"$id='{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe';"
    "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($id).Show([Windows.UI.Notifications.ToastNotification]::new($x))")


def toast(title, body, app="claude-shared"):
    """True when the notification was handed to the OS."""
    try:
        if os.name == "nt":
            ps = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
            if not os.path.isfile(ps):
                return False
            r = hidden.run([ps, "-NoProfile", "-NonInteractive", "-Command", TOAST_PS], capture_output=True, timeout=30,
                               env=dict(os.environ, NOTIFIER_TITLE=title, NOTIFIER_BODY=body))
            return r.returncode == 0
        hidden.run(["notify-send", "-a", app, title, body], timeout=10)
        return True
    except Exception:
        return False
