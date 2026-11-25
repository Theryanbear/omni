"""Background worker management on macOS (launchd), so sending runs without any window open."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

LABEL = "com.omni.mail"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _plist(workdir: Path) -> str:
    log = workdir / "data" / "worker.log"
    env = {"OMNI_MAIL_DB": str(workdir / "data" / "omni_mail.db"), "OMNI_MAIL_ENV": str(workdir / ".env"),
           "PYTHONUNBUFFERED": "1"}
    env_xml = "".join(f"<key>{k}</key><string>{v}</string>" for k, v in env.items())
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>{LABEL}</string>
<key>ProgramArguments</key><array><string>{sys.executable}</string><string>-m</string><string>omni_mail</string><string>run</string></array>
<key>WorkingDirectory</key><string>{workdir}</string>
<key>EnvironmentVariables</key><dict>{env_xml}</dict>
<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
<key>StandardOutPath</key><string>{log}</string><key>StandardErrorPath</key><string>{log}</string>
</dict></plist>
"""


def run(cmd: str) -> None:
    if sys.platform != "darwin":
        print(json.dumps({"ok": False, "error": "launchd workers are macOS only; use `omni-mail run` or cron"}))
        return
    workdir = Path.cwd()
    if cmd == "install-worker":
        (workdir / "data").mkdir(exist_ok=True)
        PLIST.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["launchctl", "unload", str(PLIST)], capture_output=True)
        PLIST.write_text(_plist(workdir))
        r = subprocess.run(["launchctl", "load", "-w", str(PLIST)], capture_output=True, text=True)
        print(json.dumps({"ok": r.returncode == 0, "plist": str(PLIST), "stderr": r.stderr.strip()}))
    elif cmd == "uninstall-worker":
        subprocess.run(["launchctl", "unload", "-w", str(PLIST)], capture_output=True)
        if PLIST.exists():
            PLIST.unlink()
        print(json.dumps({"ok": True, "removed": str(PLIST)}))
    else:
        r = subprocess.run(["launchctl", "list", LABEL], capture_output=True, text=True)
        pid = None
        for line in r.stdout.splitlines():
            if '"PID"' in line:
                pid = line.split("=")[-1].strip(" ;")
        print(json.dumps({"ok": True, "installed": PLIST.exists(), "running": bool(pid), "pid": pid}))
