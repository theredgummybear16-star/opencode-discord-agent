import json
import os
import subprocess

import config


def _workpath(spec):
    wd = os.path.realpath(os.path.join(config.WORKSPACES_DIR, spec.get("tid", "")))
    full = os.path.realpath(os.path.join(wd, spec.get("script", "")))
    if not full.startswith(wd + os.sep) or not os.path.isfile(full):
        return None
    return full


def run_script(spec, guild_ids):
    full = _workpath(spec)
    if not full:
        return "(cron %s: script missing or outside workspace)" % spec.get("script")
    env = os.environ.copy()
    env["GUILD_IDS"] = json.dumps([str(g) for g in (guild_ids or [])])
    env["OWNER_ID"] = config.OWNER_ID
    try:
        proc = subprocess.run(
            ["python3", full],
            cwd=os.path.dirname(full),
            capture_output=True,
            text=True,
            timeout=int(spec.get("timeout", 60)),
            env=env,
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        return (out + ("\n" + err if err else "")).strip()[:4000]
    except subprocess.TimeoutExpired:
        return "(cron timed out)"
    except Exception as e:
        return "(cron failed: %s)" % e