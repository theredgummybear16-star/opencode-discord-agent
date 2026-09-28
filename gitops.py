import base64
import json
import os
import subprocess
import time
import urllib.error
import urllib.request

import config
import brain


def _git(cmd, cwd=None, env_extra=None):
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": "opencode-agent",
            "GIT_AUTHOR_EMAIL": "agent@noreply.github.com",
            "GIT_COMMITTER_NAME": "opencode-agent",
            "GIT_COMMITTER_EMAIL": "agent@noreply.github.com",
        }
    )
    if env_extra:
        env.update(env_extra)
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    proc.wait(timeout=60)


def commit_and_push(repo, message):
    if not config.GH_ADMIN_TOKEN:
        return False
    heartbeat = os.path.join(config.ROOT, "heartbeat")
    with open(heartbeat, "w") as fh:
        fh.write("ok %d\n" % int(time.time()))
    _git(["git", "-C", config.ROOT, "add", "-A"])
    _git(["git", "-C", config.ROOT, "commit", "-q", "-m", message])
    if not repo:
        return False
    cred = base64.b64encode(("x-access-token:" + config.GH_ADMIN_TOKEN).encode()).decode()
    _git(
        ["git", "-C", config.ROOT, "-c", "http.extraHeader=Authorization: Basic %s" % cred, "push", "-q", "origin", "HEAD:main"],
    )
    return True


def dispatch():
    if not config.GH_ADMIN_TOKEN or not config.REPO:
        return
    url = "https://api.github.com/repos/%s/actions/workflows/work.yml/dispatches" % config.REPO
    data = json.dumps({"ref": "main"}).encode()
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={
            "Authorization": "Bearer " + config.GH_ADMIN_TOKEN,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as e:
        brain.quiet_log("dispatch failed %s" % e.code)
    except Exception as e:
        brain.quiet_log("dispatch error %s" % e)