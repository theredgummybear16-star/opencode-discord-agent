import base64
import os
import re
import signal
import subprocess
import sys
import time

import config

SSHD_CFG = """Port {port}
ListenAddress 127.0.0.1
HostKey /etc/ssh/ssh_host_ed25519_key
HostKey /etc/ssh/ssh_host_rsa_key
PermitRootLogin no
PasswordAuthentication yes
PermitEmptyPasswords no
ChallengeResponseAuthentication no
UsePAM no
X11Forwarding no
AllowTcpForwarding no
AllowUsers {user}
MaxAuthTries 5
LoginGraceTime 30
PidFile /tmp/oc_sshd.pid
LogLevel ERROR
"""

CF_URL = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"


def _run(cmd, check=False, timeout=300):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        if check:
            raise RuntimeError(str(e))
        return None


def gen_password(n=42):
    return base64.urlsafe_b64encode(os.urandom(n + 6)).decode().rstrip("=")[:n]


def get_user():
    return os.environ.get("USER") or os.environ.get("LOGNAME") or "runner"


def set_password(password):
    user = get_user()
    ran = _run(["bash", "-lc", "echo '%s:%s' | sudo chpasswd" % (user, password)], timeout=30)
    return ran is not None and ran.returncode == 0


def install_sshd(password):
    user = get_user()
    _run(["sudo", "apt-get", "update", "-qq"], timeout=300)
    _run(["sudo", "apt-get", "install", "-y", "-qq", "openssh-server"], timeout=300)
    _run(["sudo", "ssh-keygen", "-A"], timeout=60)
    if not os.path.isfile("/usr/sbin/sshd"):
        return False, "sshd binary missing (apt-get install openssh-server failed)"
    _run(["sudo", "mkdir", "-p", "/run/sshd"], timeout=15)
    if not set_password(password):
        return False, "chpasswd failed"
    with open("/tmp/oc_sshd_config", "w") as fh:
        fh.write(SSHD_CFG.format(port=2222, user=user))
    _run(["sudo", "pkill", "-f", "/usr/sbin/sshd -f /tmp/oc_sshd_config"], timeout=15)
    try:
        os.remove("/tmp/oc_sshd.pid")
    except Exception:
        pass
    _run(["sudo", "/usr/sbin/sshd", "-t", "-f", "/tmp/oc_sshd_config"], timeout=30)
    ran = _run(["sudo", "/usr/sbin/sshd", "-f", "/tmp/oc_sshd_config", "-E", "/tmp/oc_sshd.err"], timeout=30)
    time.sleep(1.5)
    if os.path.isfile("/tmp/oc_sshd.pid"):
        return True, "ok"
    err = ""
    try:
        with open("/tmp/oc_sshd.err") as fh:
            err = fh.read()[:900]
    except Exception:
        pass
    if not err:
        err = "pid file not created"
    return False, ("start failed rc=%s: %s" % (getattr(ran, "returncode", "?"), err.strip()))


def ensure_cloudflared():
    path = "/tmp/cloudflared"
    if os.path.isfile(path) and os.access(path, os.X_OK):
        return path
    _run(["curl", "-fsSL", "-o", path, CF_URL], timeout=420)
    if os.path.isfile(path):
        os.chmod(path, 0o755)
    return path if os.path.isfile(path) else None


def start_tunnel(target, logfile):
    cf = ensure_cloudflared()
    if not cf:
        return None, None
    handle = open(logfile, "wb")
    try:
        proc = subprocess.Popen(
            [cf, "tunnel", "--no-autoupdate", "--url", target],
            stdout=handle, stderr=subprocess.STDOUT, start_new_session=True,
        )
    except Exception:
        handle.close()
        return None, None
    host = None
    deadline = time.time() + 45
    while time.time() < deadline:
        time.sleep(1)
        proc.poll()
        try:
            txt = open(logfile, "rb").read().decode("utf-8", "replace")
        except Exception:
            txt = ""
        m = re.search(r"https://([a-z0-9-]+\.trycloudflare\.com)", txt)
        if m:
            host = m.group(0)
            break
        if proc.returncode is not None:
            break
    return proc, host


def bring_up():
    os.makedirs("/tmp", exist_ok=True)
    user = get_user()
    password = gen_password()
    ok, why = install_sshd(password)
    if not ok:
        raise RuntimeError("sshd failed to start (%s)" % why)
    token = base64.urlsafe_b64encode(os.urandom(18)).decode().rstrip("=")
    web_port = 7681
    try:
        handle = open("/tmp/oc_web.out", "wb")
    except Exception:
        handle = None
    webterm = subprocess.Popen(
        [sys.executable, os.path.join(config.ROOT, "webterm.py"), str(web_port), token],
        stdout=handle, stderr=subprocess.STDOUT, start_new_session=True,
    )
    ssh_proc, ssh_host = start_tunnel("ssh://127.0.0.1:2222", "/tmp/oc_cf_ssh.log")
    web_proc, web_host = start_tunnel("http://127.0.0.1:%d" % web_port, "/tmp/oc_cf_web.log")
    if not ssh_host or not web_host:
        tear_down({"pids": [ssh_proc.pid if ssh_proc else None, web_proc.pid if web_proc else None, webterm.pid]})
        raise RuntimeError("could not establish tunnels")
    return {
        "user": user,
        "pass": password,
        "port": 2222,
        "web_port": web_port,
        "web_token": token,
        "ssh_host": ssh_host,
        "web_host": web_host,
        "pids": [ssh_proc.pid, web_proc.pid, webterm.pid],
        "started": time.time(),
    }


def alivetree(pids):
    alive = []
    for pid in (pids or []):
        if not pid:
            continue
        try:
            os.kill(pid, 0)
            alive.append(pid)
        except Exception:
            pass
    return alive


def tear_down(info):
    for pid in alivetree((info.get("pids") or [])):
        try:
            os.killpg(pid, signal.SIGKILL)
        except Exception:
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass
    _run(["sudo", "pkill", "-f", "/usr/sbin/sshd -f /tmp/oc_sshd_config"], timeout=15)