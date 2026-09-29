import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config

DISCORD_API = "https://discord.com/api/v10"


def _request(method, url, body=None, token=None, headers=None):
    hdrs = {"User-Agent": "opencode-ai-agent/1.0"}
    if token:
        hdrs["Authorization"] = "Bot " + token
    if headers:
        hdrs.update(headers)
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            if "json" in ctype:
                return resp.status, raw, None
            return resp.status, raw, None
    except urllib.error.HTTPError as e:
        return e.code, e.read(), None
    except Exception as e:
        return 0, b"", str(e)


class ScopeCache:
    def __init__(self):
        self.channel_guild = {}
        self.ttl = 300
        self._lock = threading.Lock()

    def resolve(self, cid):
        now = time.time()
        with self._lock:
            hit = self.channel_guild.get(cid)
            if hit and now - hit[1] < self.ttl:
                return hit[0]
        status, raw, _ = _request("GET", "%s/channels/%s" % (DISCORD_API, cid), token=config.DS_TOKEN)
        guild_id = None
        if status == 200:
            try:
                guild_id = str(json.loads(raw).get("guild_id") or "")
            except Exception:
                pass
        with self._lock:
            self.channel_guild[cid] = (guild_id, now)
        return guild_id


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _handle(self, method):
        try:
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                self._respond(401, {"error": "missing bearer token"})
                return
            token = auth[len("Bearer "):].strip()
            tid = self.server.owner.resolve_token(token)
            if not tid:
                self._respond(403, {"error": "forbidden"})
                return
            parsed = urllib.parse.urlparse(self.path)
            if self.server.owner.check_scope(tid, method, parsed.path):
                body = None
                if "Content-Length" in self.headers:
                    try:
                        length = int(self.headers["Content-Length"])
                        body = self.rfile.read(length) or None
                    except Exception:
                        body = None
                qs = parsed.query
                url = DISCORD_API + parsed.path + ("?" + qs if qs else "")
                status, raw, err = _request(method, url, body=body, token=config.DS_TOKEN)
                self.server.owner.audit(tid, method, parsed.path, status)
                if method == "POST" and parsed.path == "/users/@me/channels" and status == 200:
                    try:
                        cid = json.loads(raw).get("id")
                        if cid:
                            self.server.owner.note_dm_channel(tid, str(cid))
                    except Exception:
                        pass
                self._respond(status, raw, is_bytes=True)
            else:
                self._respond(403, {"error": "out of tenant scope"})
        except Exception as e:
            self._respond(500, {"error": str(e)})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PATCH(self):
        self._handle("PATCH")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")

    def log_message(self, *args):
        pass

    def _respond(self, status, data, is_bytes=False):
        if not is_bytes:
            data = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class Proxy:
    def __init__(self, state, audit_fn):
        self.owner_scopes = {}
        self.state = state
        self.audit_fn = audit_fn
        self.cache = ScopeCache()
        self.httpd = None
        self.port = int(os.environ.get("PROXY_PORT", "8123"))

    def start(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.httpd.owner = self
        t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        t.start()
        return self.port

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()

    def register(self, tid, secret, scope):
        self.owner_scopes[secret] = {"tid": tid, "scope": scope}

    def resolve_token(self, token):
        info = self.owner_scopes.get(token)
        return info["tid"] if info else None

    def check_scope(self, tid, method, path):
        scope = None
        for info in self.owner_scopes.values():
            if info["tid"] == tid:
                scope = info["scope"]
                break
        if scope is None:
            return False
        segs = [s for s in path.split("/") if s]
        if not segs or segs[0] not in ("users", "channels", "guilds"):
            return False
        if segs[0] == "users":
            if len(segs) >= 2 and segs[1] == "@me":
                return True
            kind = scope.get("kind")
            if kind == "dm":
                return len(segs) >= 2 and segs[1] == scope.get("user_id")
            return False
        if segs[0] == "guilds":
            return segs[1] == scope.get("guild_id") and (segs[1] if len(segs) > 1 else True) is not None
        if segs[0] == "channels":
            if len(segs) < 2:
                return False
            cid = segs[1]
            guild_id = self.cache.resolve(cid)
            if scope.get("kind") == "dm":
                if str(cid) in (scope.get("channels") or []) or str(cid) in (scope.get("dm_created") or []):
                    return True
                return (not guild_id) and self._dm_write_allowed(method, segs, cid)
            if str(cid) in (scope.get("dm_created") or []):
                return True
            return bool(guild_id) and guild_id == scope.get("guild_id")
        return False

    def _dm_write_allowed(self, method, segs, cid):
        if len(segs) < 3:
            return False
        if segs[2] == "typing" and method == "POST":
            return True
        if segs[2] == "messages":
            if method == "POST":
                return True
            if len(segs) >= 5 and segs[4] == "reactions" and method in ("PUT", "DELETE"):
                return True
        return False

    def note_dm_channel(self, tid, cid):
        for info in self.owner_scopes.values():
            if info["tid"] == tid:
                info["scope"].setdefault("dm_created", []).append(cid)
                break

    def audit(self, tid, method, path, status):
        try:
            self.audit_fn({"kind": "discord_call", "tenant": tid, "method": method, "path": path[:300], "status": status})
        except Exception:
            pass