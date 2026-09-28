import base64
import json
import os
import subprocess
import tempfile
import time

import brain
import config


def _run_quiet(cmd, cwd=None, env=None, timeout=60):
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        return proc.returncode, out, err
    return proc.returncode, out, err


class Runner:
    def __init__(self, tid):
        self.tid = tid
        data_dir = brain.tenant_data(tid)
        self.xdg = os.path.join(data_dir, "xdg")
        os.makedirs(self.xdg, exist_ok=True)
        self.cmd_base = ["opencode", "run", "-m", config.MODEL, "--log-level", "ERROR"]
        if not _binary_ok():
            raise RuntimeError("opencode binary not found on PATH")

    def env(self):
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/home/runner"),
            "XDG_DATA_HOME": self.xdg,
            "TERM": "dumb",
            "NO_COLOR": "1",
            "LC_ALL": "C.UTF-8",
            "PROXY_PORT": os.environ.get("PROXY_PORT", "8123"),
            "DISCORD_AUTH": os.environ.get("DISCORD_AUTH", ""),
        }
        return env

    def _snapshot_sessions(self):
        code, out, _ = _run_quiet(["opencode", "session", "list"], env=self.env(), timeout=30)
        if code != 0:
            return set()
        return set(self._extract_ids(out))

    @staticmethod
    def _extract_ids(out_text):
        if isinstance(out_text, (bytes, bytearray)):
            out_text = out_text.decode("utf-8", "replace")
        ids = set()
        for line in out_text.splitlines():
            line = line.strip()
            if line.startswith("ses_"):
                ids.add(line.split()[0])
        return ids

    def transcript(self, sid=None, limit=40, max_chars=9000):
        if sid is None:
            ids = self._snapshot_sessions()
            if not ids:
                return None
            sid = max(ids, key=lambda i: _updated_for(self.env(), i) or 0)
        code, out, _ = _run_quiet(["opencode", "export", sid], env=self.env(), timeout=60)
        if code != 0:
            return None
        try:
            data = json.loads(out.decode("utf-8", "replace"))
        except Exception:
            return None
        lines = []
        for msg in data.get("messages", []):
            role = (msg.get("info") or {}).get("role")
            if role not in ("user", "assistant"):
                continue
            text = " ".join(
                str(part.get("text", "")).strip()
                for part in msg.get("parts", [])
                if part.get("type") == "text" and str(part.get("text", "")).strip()
            ).strip()
            if not text:
                continue
            lines.append("%s: %s" % ("user" if role == "user" else "assistant", text))
        if not lines:
            return None
        out_txt = "\n".join(lines[-limit:])
        if len(out_txt) > max_chars:
            out_txt = "...[truncated beginning]...\n" + out_txt[-max_chars:]
        return out_txt

    def _export_text(self, sid):
        code, out, _ = _run_quiet(["opencode", "export", sid], env=self.env(), timeout=60)
        if code != 0:
            return None
        try:
            data = json.loads(out.decode("utf-8", "replace"))
        except Exception:
            return None
        last = None
        for msg in data.get("messages", []):
            role = (msg.get("info") or {}).get("role")
            for part in msg.get("parts", []):
                if part.get("type") == "text" and str(part.get("text", "")).strip():
                    last = part["text"] if role == "assistant" else (last or part["text"])
        return last

    @staticmethod
    def _cmd(cmd_base, prompt, session_id):
        cmd = list(cmd_base)
        if session_id:
            cmd += ["--session", session_id]
        cmd.append(prompt)
        return cmd

    def run(self, prompt, session_id=None, timeout=config.REQUEST_TIMEOUT_S):
        brain.write_auth(self.tid)
        before = self._snapshot_sessions()
        os.makedirs(config.LOGS_DIR, exist_ok=True)
        fd, tmppath = tempfile.mkstemp(prefix="oc_run_", dir=config.LOGS_DIR)
        os.close(fd)
        with open(tmppath, "wb") as out:
            with open(os.devnull, "wb") as err:
                proc = subprocess.Popen(
                    self._cmd(self.cmd_base, prompt, session_id),
                    cwd=brain.tenant_workspace(self.tid),
                    env=self.env(),
                    stdout=out,
                    stderr=err,
                )
                try:
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                    return {"ok": False, "text": "", "sid": None, "approval": None, "timeout": True}
        after = self._snapshot_sessions()
        new_ids = (after - before) if session_id is None else {session_id}
        sid = None
        if new_ids:
            sid = max(new_ids, key=lambda i: _updated_for(self.env(), i) or 0)
        text = None
        if sid:
            text = self._export_text(sid)
        with open(tmppath, "rb") as fh:
            raw = fh.read().decode("utf-8", "replace")
        try:
            os.remove(tmppath)
        except Exception:
            pass
        if text is None:
            text = raw.strip()
        approval = None
        lowered = (text or "").upper()
        if "APPROVAL_REQUIRED:" in lowered:
            idx = text.find("APPROVAL_REQUIRED:")
            approval = text[idx + len("APPROVAL_REQUIRED:"):].strip()
        return {"ok": proc.returncode == 0 and bool(text), "text": text, "sid": sid, "approval": approval, "timeout": False}


def _updated_for(env, sid):
    try:
        code, out, _ = _run_quiet(["opencode", "session", "list"], env=env, timeout=30)
        for line in out.decode("utf-8", "replace").splitlines():
            if line.strip().startswith(sid):
                return time.time()
    except Exception:
        pass
    return 0


def _binary_ok():
    try:
        _run_quiet(["opencode", "--version"], timeout=10)
        return True
    except Exception:
        return False


def save_session(state, tid, sid):
    if sid:
        state["session_last"][tid] = sid
    return state