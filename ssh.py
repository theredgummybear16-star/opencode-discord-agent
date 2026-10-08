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


def _apt_update_install():
    steps = []
    for attempt in range(3):
        last = _run(["sudo", "apt-get", "update", "-qq"], timeout=300)
        if last is not None and last.returncode == 0:
            steps.append("apt-update=ok")
            break
        steps.append("apt-update rc=%s try%d" % (getattr(last, "returncode", "none"), attempt + 1))
        time.sleep(5)
    for attempt in range(2):
        last = _run(["sudo", "apt-get", "install", "-y", "-qq", "openssh-server"], timeout=300)
        if last is not None and last.returncode == 0:
            steps.append("apt-install=ok")
            return steps, True
        steps.append("apt-install rc=%s try%d" % (getattr(last, "returncode", "none"), attempt + 1))
        time.sleep(5)
    return steps, False


def install_sshd(password):
    user = get_user()
    steps, apt_ok = _apt_update_install()
    _run(["sudo", "ssh-keygen", "-A"], timeout=60)
    if not os.path.isfile("/usr/sbin/sshd"):
        return False, "%s | sshd binary missing (apt failed)" % "; ".join(steps)
    _run(["sudo", "mkdir", "-p", "/run/sshd"], timeout=15)
    if not set_password(password):
        return False, "%s | chpasswd failed" % "; ".join(steps)
    with open("/tmp/oc_sshd_config", "w") as fh:
        fh.write(SSHD_CFG.format(port=2222, user=user))
    _run(["sudo", "pkill", "-f", "/usr/sbin/sshd -f /tmp/oc_sshd_config"], timeout=15)
    try:
        os.remove("/tmp/oc_sshd.pid")
    except Exception:
        pass
    t = _run(["sudo", "/usr/sbin/sshd", "-t", "-f", "/tmp/oc_sshd_config"], timeout=30)
    if t is not None and t.returncode != 0:
        detail = ((t.stderr or "") + (t.stdout or "")).strip()[:400]
        return False, "%s | config check failed rc=%s: %s" % ("; ".join(steps), t.returncode, detail)
    ran = _run(["sudo", "/usr/sbin/sshd", "-f", "/tmp/oc_sshd_config", "-E", "/tmp/oc_sshd.err"], timeout=30)
    time.sleep(2)
    if os.path.isfile("/tmp/oc_sshd.pid"):
        return True, "ok"
    err = ""
    cat = _run(["sudo", "cat", "/tmp/oc_sshd.err"], timeout=15)
    if cat is not None and (cat.stdout or "").strip():
        err = cat.stdout[:900]
    if not err and ran is not None:
        err = ((ran.stderr or "") + (ran.stdout or "")).strip()[:400] or "pid file not created"
    return False, ("%s | start failed rc=%s: %s" % ("; ".join(steps), getattr(ran, "returncode", "none"), err.strip()))


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


def web_ok(port, token):
    import urllib.request
    try:
        req = urllib.request.urlopen("http://127.0.0.1:%d/?k=%s" % (port, token), timeout=3)
        return req.status == 200
    except Exception:
        return False


def port_pids(port):
    pids = []
    ran = _run(["ss", "-ltnpH", "sport = :%d" % port], timeout=15)
    if ran is None or not ran.stdout:
        ran = _run(["netstat", "-ltnp"], timeout=15)
        if ran is None or not ran.stdout:
            return pids
        for line in (ran.stdout or "").splitlines():
            if (":%d " % port) in line:
                m = re.search(r"pid=(\d+)", line)
                if m:
                    pids.append(int(m.group(1)))
        return pids
    for m in re.finditer(r"pid=(\d+)", ran.stdout or ""):
        pids.append(int(m.group(1)))
    return pids


def kill_port_listener(port):
    for pid in port_pids(port):
        if pid == os.getpid():
            continue
        try:
            os.kill(pid, 9)
        except Exception:
            _run(["sudo", "kill", "-9", str(pid)], timeout=10)
        try:
            os.waitpid(pid, os.WNOHANG)
        except Exception:
            pass


def _is_zombie(pid):
    try:
        with open("/proc/%d/stat" % pid) as fh:
            tail = fh.read().rsplit(")", 1)[1]
        return tail.split()[0] == "Z"
    except Exception:
        return False


def spawn_webterm(port, token):
    handle = None
    try:
        handle = open("/tmp/oc_web.out", "ab")
    except Exception:
        pass
    return subprocess.Popen(
        [sys.executable, os.path.join(config.ROOT, "webterm.py"), str(port), token],
        stdout=handle, stderr=subprocess.STDOUT, start_new_session=True,
    )


def ensure_webterm(port, token, attempts=4):
    last_err = ""
    for attempt in range(attempts):
        if web_ok(port, token):
            pids = port_pids(port)
            return (pids[0] if pids else None), "ok (existing)"
        kill_port_listener(port)
        time.sleep(0.5)
        proc = spawn_webterm(port, token)
        for _ in range(8):
            time.sleep(1)
            if web_ok(port, token):
                return proc.pid, "ok (started try %d)" % (attempt + 1)
            if proc.poll() is not None:
                last_err = "webterm exited rc=%s" % proc.returncode
                break
            last_err = "waiting"
        else:
            last_err = "token not served after 8s"
        kill_port_listener(port)
    raise RuntimeError("web terminal did not come up (%s)" % last_err)


def bring_up():
    os.makedirs("/tmp", exist_ok=True)
    user = get_user()
    password = gen_password()
    ok, why = install_sshd(password)
    if not ok:
        raise RuntimeError("sshd failed to start (%s)" % why)
    token = base64.urlsafe_b64encode(os.urandom(18)).decode().rstrip("=")
    web_port = 7681
    web_pid, web_note = ensure_webterm(web_port, token)
    ssh_proc, ssh_host = start_tunnel("ssh://127.0.0.1:2222", "/tmp/oc_cf_ssh.log")
    web_proc, web_host = start_tunnel("http://127.0.0.1:%d" % web_port, "/tmp/oc_cf_web.log")
    if not ssh_host or not web_host:
        tear_down({"pids": [ssh_proc.pid if ssh_proc else None, web_proc.pid if web_proc else None, web_pid]})
        raise RuntimeError("could not establish tunnels")
    return {
        "user": user,
        "pass": password,
        "port": 2222,
        "web_port": web_port,
        "web_token": token,
        "ssh_host": ssh_host,
        "web_host": web_host,
        "pids": [ssh_proc.pid, web_proc.pid, web_pid],
        "started": time.time(),
    }


def alivetree(pids):
    alive = []
    for pid in (pids or []):
        if not pid:
            continue
        try:
            os.kill(pid, 0)
        except Exception:
            continue
        if _is_zombie(pid):
            continue
        alive.append(pid)
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
    try:
        kill_port_listener(7681)
    except Exception:
        pass