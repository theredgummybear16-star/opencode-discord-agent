import asyncio
import os
import re
import secrets
import sys
import time

import discord

import brain
import config
import gitops
import jobs
import opencode_runner
import perms
import ssh as sshlib

EXTRA_MAJOR = (
    "ban ", "kick ", "delete channel", "delete role", "delete ", "clear all",
    "@everyone", "wipe the", "rename the ", "change the server name", "server icon", "permission",
)

RECUR_RE = re.compile(r"(every\s+\d+\s*(sec|second|min|minute|hr|hour|day)s?|\bautomatically\b|\brecurring\b|\bcron\b)", re.I)


class Controller:
    def __init__(self, client, state, proxy):
        self.client = client
        self.state = state
        self.proxy = proxy
        self.runners = {}
        self.secrets = {}
        self.last_activity = time.time()
        self.created_threads = set()
        self.tenants_ready = False
        self.boot = time.time()

    def touch(self):
        self.last_activity = time.time()

    def _runner(self, tid):
        if tid not in self.runners:
            self.runners[tid] = opencode_runner.Runner(tid)
        return self.runners[tid]

    def _register_scope(self, tid, rec):
        secret = secrets.token_urlsafe(24)
        if rec["kind"] == "guild":
            scope = {"kind": "guild", "guild_id": rec["guild_id"]}
        else:
            scope = {"kind": "dm", "user_id": rec["user_id"], "channels": []}
        self.proxy.register(tid, secret, scope)
        self.secrets[tid] = secret
        return secret

    async def on_online(self):
        if self.tenants_ready:
            return
        self.tenants_ready = True
        try:
            self.proxy.set_owner_guilds([g.id for g in self.client.guilds])
        except Exception as e:
            brain.quiet_log("owner guilds err %s" % e)
        for tid, rec in list(self.state["tenants"].items()):
            try:
                brain.unpack_tenant(tid)
                secret = self._register_scope(tid, rec)
                self._runner(tid).secret = secret
            except Exception as e:
                brain.quiet_log("tenant init fail %s %s" % (tid, e))

    async def on_guild_join(self, guild):
        try:
            self.proxy.set_owner_guilds([g.id for g in self.client.guilds])
        except Exception as e:
            brain.quiet_log("guild join err %s" % e)

    def _safe_rel(self, rel):
        rel = (rel or "").strip().replace("\\", "/")
        if not rel or rel.startswith("/") or ":" in rel:
            return None
        parts = [p for p in rel.split("/") if p not in ("", ".", "..")]
        return "/".join(parts) if parts else None

    def _handle_cron_markers(self, result, tid):
        text = result.get("text") or ""
        off = re.search(r"^\s*CRON_OFF:\s*([^\s]+)", text, re.M)
        if off:
            rel = self._safe_rel(off.group(1))
            crons = self.state.setdefault("crons", {})
            for key in [k for k, v in crons.items() if v.get("script") == rel]:
                del crons[key]
            try:
                brain.save_state(self.state)
            except Exception:
                pass
            result["text"] = ("Disabled cron for %s.\n" % (rel or off.group(1))) + text
        reg = re.search(r"^\s*CRON:\s*(\d+)\s*:\s*([^\s]+)", text, re.M)
        if reg:
            interval = int(reg.group(1))
            rel = self._safe_rel(reg.group(2))
            if rel and 5 <= interval <= 86400 * 30:
                full = os.path.join(brain.tenant_workspace(tid), rel)
                if os.path.isfile(full):
                    key = "c_%.0f_%x" % (time.time(), abs(hash(rel)) & 0xffffff)
                    self.state.setdefault("crons", {})[key] = {
                        "tid": tid, "script": rel, "interval_s": interval,
                        "notify_owner": True, "last_run": time.time(), "enabled": True,
                    }
                    try:
                        brain.save_state(self.state)
                    except Exception:
                        pass
                    result["text"] = ("Registered cron: every %ds runs your script, output DM'd to you.\n" % interval) + text
                else:
                    result["text"] = ("(CRON: script file '%s' not found in workspace — write it first)\n" % rel) + text
        result["text"] = re.sub(r"^\s*CRON(?:_OFF)?:\s*[^\n]*\n?", "", result.get("text") or "", flags=re.M)

    async def tick_crons(self):
        crons = self.state.get("crons") or {}
        now = time.time()
        due = [k for k, spec in crons.items()
               if spec.get("enabled") and now - float(spec.get("last_run") or 0) >= int(spec.get("interval_s", 3600))]
        gids = [str(g.id) for g in self.client.guilds]
        for key in due:
            spec = crons.get(key)
            if not spec:
                continue
            self.state["crons"][key]["last_run"] = now
            try:
                out = await asyncio.get_event_loop().run_in_executor(None, jobs.run_script, spec, gids)
            except Exception as e:
                out = "(cron crash %s)" % e
            if out:
                brain.quiet_log("cron %s: %s" % (spec.get("script"), out[:180]))
                if spec.get("notify_owner"):
                    try:
                        await self._send_to_owner(out[:1900])
                    except Exception as e:
                        brain.quiet_log("cron notify err %s" % e)

    async def owner_notify(self, text):
        await self._send_to_owner(text)

    async def _send_to_owner(self, text):
        try:
            cid = self.state.get("owner_dm_channel")
            if cid:
                msgable = self.client.get_partial_messageable(int(cid))
                return (await msgable.send(text[:1900])).id
            user = await self.client.fetch_user(int(config.OWNER_ID))
            ch = await user.create_dm()
            msg = await ch.send(text[:1900])
            self.state["owner_dm_channel"] = ch.id
            return msg.id
        except Exception as e:
            brain.quiet_log("owner notify failed %s" % e)
            return None

    async def handle_message(self, message):
        try:
            if message.author.id == self.client.user.id:
                return
            self.touch()
            if isinstance(message.channel, discord.DMChannel):
                await self._handle_dm(message)
                return
            if isinstance(message.channel, discord.Thread) and message.channel.parent_id in self.created_threads:
                await self._safe_guild(message)
                return
            if self.client.user in message.mentions:
                await self._safe_guild(message)
                return
            if message.reference and getattr(message.reference, "message_id", None):
                try:
                    ref = await message.channel.fetch_message(message.reference.message_id)
                    if ref.author.id == self.client.user.id:
                        await self._safe_guild(message)
                        return
                except Exception:
                    pass
        except Exception as e:
            brain.quiet_log("handle_message err %s" % e)

    async def _safe_guild(self, message):
        try:
            await self._handle_guild(message)
        except Exception as e:
            brain.quiet_log("guild err %s" % e)
            try:
                await message.channel.send(
                    "⚠️ I hit an error handling that in %s: **%s** — this is visible now so I can fix it." % (message.guild, str(e)[:160])
                )
            except Exception:
                pass

    async def _handle_dm(self, message):
        author_id = str(message.author.id)
        brain.record_user(self.state, message.author.id, str(message.author))
        tid = "dm_%s" % author_id
        is_owner = author_id == config.OWNER_ID
        if is_owner:
            self.state["owner_dm_channel"] = message.channel.id
            content = re.sub(r"<@!?\d+>\s*", "", (message.content or "")).strip()
            if content.startswith("/"):
                handled = await self._handle_owner_cmd(message, content)
                if handled:
                    return
        rec = brain.ensure_tenant(self.state, tid, "dm", str(message.author), user_id=author_id)
        if tid not in self.runners or tid not in self.secrets:
            brain.unpack_tenant(tid)
            brain.write_auth(tid)
            secret = self._register_scope(tid, rec)
            self._runner(tid).secret = secret
            self._update_dm_scope(tid, message.channel.id)
        req = {
            "content": message.content or "",
            "author_display": str(message.author),
            "channel_display": "DM",
            "is_owner": is_owner,
        }
        reply = await self.run_agent(tid, rec, None, "your DM with %s" % message.author, req, requester_id=message.author.id, dm_message=message)
        if reply:
            await self._reply(message.channel, None, reply["reply"], mention_ids=None)

    def _update_dm_scope(self, tid, channel_id):
        for info in self.proxy.owner_scopes.values():
            if info["tid"] == tid:
                chans = info["scope"].setdefault("channels", [])
                if str(channel_id) not in chans:
                    chans.append(str(channel_id))
                break

    CMD_HELP = ("**Owner commands (DM only, instant, no AI involved)**\n"
                "/help — this list | /status — uptime, tenants, crons, servers, model\n"
                "/cronjobs — list background crons | /run <file> [tid] — run a script now\n"
                "/logs [n] — last n (encrypted) log lines | /tenants — list tenants\n"
                "/memory [tid] — show a tenant's MEMORY.md | /clear [tid|all] — reset a tenant session\n"
                "/model [name] — show/change model | /ssh on|off|status|pass — cloudflared SSH + web terminal\n"
                "/invite — get the invite link (with slash-command scope) | /restart — restart the agent instance | /stop — shut it down")

    @staticmethod
    def _hms(secs):
        secs = int(secs)
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        return "%dh %dm %ds" % (h, m, s)

    def _owner_tid(self):
        return "dm_%s" % config.OWNER_ID

    async def _handle_owner_cmd(self, message, content):
        reply, action = await self._owner_cmd_reply(content)
        if reply is None:
            return False
        try:
            await self._reply(message.channel, None, reply, None)
        except Exception:
            pass
        await self._act(action)
        return True

    async def _act(self, action):
        if action == "restart":
            await self._do_restart()
        elif action == "stop":
            await self._do_stop()

    async def _owner_cmd_reply(self, content):
        parts = (content or "").split()
        if not parts:
            return "Type a command, e.g. /status", None
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""
        rest = " ".join(parts[1:])
        if cmd == "/help":
            return self.CMD_HELP, None
        if cmd == "/status":
            return self._cmd_status(rest), None
        if cmd == "/cronjobs":
            return self._cmd_cronjobs(), None
        if cmd == "/run":
            return self._cmd_run(rest), None
        if cmd == "/logs":
            return self._cmd_logs(arg), None
        if cmd == "/tenants":
            return self._cmd_tenants(), None
        if cmd == "/memory":
            return self._cmd_memory(arg), None
        if cmd == "/clear":
            return await self._cmd_clear(arg), None
        if cmd == "/model":
            return self._cmd_model(arg), None
        if cmd == "/ssh":
            return await self._cmd_ssh_reply(rest), None
        if cmd == "/invite":
            return self._cmd_invite(), None
        if cmd == "/restart":
            return ("Restarting the agent instance now…",) + ("restart",)
        if cmd == "/stop":
            return ("Shutting down. Offline until the next scheduled run or a manual restart dispatch.",) + ("stop",)
        return None, None

    async def handle_interaction(self, interaction):
        if str(interaction.user.id) != config.OWNER_ID:
            try:
                await interaction.response.send_message("Owner-only commands, sorry.", ephemeral=True)
            except Exception:
                pass
            return
        data = interaction.data or {}
        name = (data.get("name") or "").lstrip("/")
        opts = data.get("options") or []
        vals = []

        def flat(items):
            for o in items:
                t = o.get("type")
                if t in (1, 2):
                    flat(o.get("options") or [])
                elif o.get("value") is not None:
                    vals.append(str(o["value"]))

        flat(opts)
        content = "/%s%s" % (name, (" " + " ".join(vals)) if vals else "")
        reply, action = await self._owner_cmd_reply(content)
        if reply is None:
            reply = "Unknown command."
        brain.audit(self.state, {"kind": "interaction", "cmd": name})
        try:
            await interaction.response.send_message(reply, ephemeral=True)
        except Exception:
            try:
                await interaction.followup.send(reply, ephemeral=True)
            except Exception:
                pass
        await self._act(action)

    async def _do_restart(self):
        if not config.GH_ADMIN_TOKEN:
            return
        try:
            await asyncio.get_event_loop().run_in_executor(None, self._prep_exit)
        except Exception as e:
            brain.quiet_log("restart prep err %s" % e)
        try:
            await asyncio.get_event_loop().run_in_executor(None, gitops.dispatch)
        except Exception as e:
            brain.quiet_log("restart dispatch err %s" % e)
        time.sleep(1)
        os._exit(0)

    async def _do_stop(self):
        try:
            await asyncio.get_event_loop().run_in_executor(None, self._prep_exit)
        except Exception:
            pass
        time.sleep(1)
        os._exit(0)

    def _cmd_status(self, arg):
        tenants = self.state.get("tenants") or {}
        crons = self.state.get("crons") or {}
        sessions = self.state.get("session_last") or {}
        known = self.state.get("known_users") or {}
        try:
            guilds = ", ".join(g.name for g in self.client.guilds) or "(none)"
        except Exception:
            guilds = "(n/a)"
        lines = [
            "**Status**",
            "uptime: %s" % self._hms(time.time() - self.boot),
            "model: `%s` (const %s)" % (config.MODEL, config.CONST_VERSION),
            "tenants: %d | sessions: %d | crons: %d | known users: %d" % (len(tenants), len(sessions), len(crons), len(known)),
            "servers: %s" % guilds,
            "proxy port: %d | owner dm: %s" % (self.proxy.port, bool(self.state.get("owner_dm_channel"))),
            "self-restart token: %s" % ("yes" if config.GH_ADMIN_TOKEN else "no"),
            "slash commands: %s" % (self.state.get("slash_registered") or "not registered yet"),
        ]
        if arg == "deep":
            audit = self.state.get("audit") or []
            lines.append("audit events: %d (last: %s)" % (len(audit), str(audit[-1] if audit else "none")))
            lines.append("crons: " + (", ".join(sorted(crons)) if crons else "none"))
        return "\n".join(lines)

    def _cmd_invite(self):
        cid = None
        try:
            if self.client and self.client.user:
                cid = self.client.user.id
        except Exception:
            cid = None
        cid = cid or 1481049839130247400
        return ("Re-add the bot with BOTH scopes for real slash commands (admin role alone won't enable them):\n"
                "https://discord.com/oauth2/authorize?client_id=%s&permissions=8&scope=bot%%20applications.commands\n\n"
                "Until then, text commands (/status etc.) already work from your DM." % cid)

    def _cmd_cronjobs(self):
        crons = self.state.get("crons") or {}
        if not crons:
            return "No cron jobs registered."
        lines = ["**Cron jobs**"]
        for key, spec in crons.items():
            iv = spec.get("interval_s", 0)
            last = spec.get("last_run") or 0
            due_in = max(0, iv - (time.time() - last))
            lines.append("• `%s` every %ds | enabled=%s | script=`%s` | next in %ds" % (
                key, iv, spec.get("enabled"), spec.get("script"), int(due_in)))
        return "\n".join(lines)

    def _cmd_run(self, arg):
        parts = arg.split()
        if not parts:
            return "/run <file> — run a workspace script now. e.g. /run monitor.py"
        rel = self._safe_rel(parts[0])
        if not rel:
            return "bad filename"
        tid = parts[1] if len(parts) > 1 else self._owner_tid()
        full = os.path.join(brain.tenant_workspace(tid), rel)
        if not os.path.isfile(full):
            return "Script `%s` not found in tenant `%s` workspace." % (rel, tid)
        gids = [str(g.id) for g in self.client.guilds]
        try:
            out = jobs.run_script({"tid": tid, "script": rel}, gids)
        except Exception as e:
            out = "(run failed: %s)" % e
        return "**Output of `%s`**\n%s" % (rel, out or "(no output)")

    def _cmd_logs(self, arg):
        try:
            n = int(arg or "30")
        except Exception:
            n = 30
        return "**Last %d log lines (decrypted)**\n```\n%s\n```" % (n, brain.read_log(n))

    def _cmd_tenants(self):
        tenants = self.state.get("tenants") or {}
        if not tenants:
            return "No tenants yet."
        lines = ["**Tenants**"]
        for tid, rec in tenants.items():
            active = bool(self.state.get("session_last", {}).get(tid))
            lines.append("• `%s` kind=%s name=`%s` session=%s" % (tid, rec.get("kind"), (rec.get("name") or "?")[:40], active))
        return "\n".join(lines)

    def _cmd_memory(self, arg):
        tid = (arg or "").strip() or self._owner_tid()
        mem = brain.load_memory(tid)
        return "**MEMORY.md for `%s`**\n%s" % (tid, mem or "(empty)")

    async def _cmd_clear(self, arg):
        sessions = self.state.get("session_last") or {}
        if arg == "all":
            n = len(sessions)
            self.state["session_last"] = {}
            for tid in list(self.state.get("tenants", {})):
                if self.runners.pop(tid, None):
                    pass
            return "Cleared %d sessions (fresh AI context everywhere)." % n
        tid = arg.strip() or self._owner_tid()
        had = sessions.pop(tid, None)
        self.runners.pop(tid, None)
        return "Session for `%s` %s" % (tid, "cleared." if had else "was already empty.")

    def _cmd_model(self, arg):
        name = (arg or "").strip()
        if not name:
            return "Current model: `%s` (override: %s)" % (config.MODEL, self.state.get("model_override") or "none")
        if re.fullmatch(r"[A-Za-z0-9._/:-]{2,120}", name) is None:
            return "invalid model id."
        old = config.MODEL
        config.MODEL = name
        self.state["model_override"] = name
        self.runners = {}
        try:
            brain.save_state(self.state)
        except Exception as e:
            brain.quiet_log("model save err %s" % e)
        return "Model changed: `%s` -> `%s` (fresh runners on next use)." % (old, name)

    async def _cmd_ssh_reply(self, rest):
        args = rest.split()
        act = (args[0] if args else "").lower()
        info = self.state.setdefault("ssh", {"active": False, "pids": []})
        alive = sshlib.alivetree(info.get("pids"))
        if act == "on":
            if alive:
                return "SSH/web already running. Use `/ssh status` (or `/ssh off` then `/ssh on` to rotate credentials)."
            try:
                info2 = await asyncio.get_event_loop().run_in_executor(None, sshlib.bring_up)
            except Exception as e:
                brain.quiet_log("ssh on err %s" % e)
                return "SSH setup failed: %s" % e
            info.update(info2)
            info["active"] = True
            try:
                brain.save_state(self.state)
            except Exception:
                pass
            host = info["ssh_host"].replace("https://", "")
            msg = []
            msg.append("**SSH + web terminal are up** (username `%s`, TTL = this run only)." % info["user"])
            msg.append("")
            msg.append("• SSH (needs cloudflared on your machine):")
            msg.append("`ssh -o ProxyCommand=\"cloudflared access ssh --hostname %s\" %s@%s`" % (host.replace(".trycloudflare.com", ".trycloudflare.com"), info["user"], host))
            msg.append("• Web terminal: %s/?k=%s" % (info["web_host"], info["web_token"]))
            msg.append("")
            msg.append("password: `%s`" % info["pass"])
            return "\n".join(msg)
        if act == "off":
            if not alive:
                return "Nothing is running (tunnels from a previous run died with that runner). Marking off."
            await asyncio.get_event_loop().run_in_executor(None, sshlib.tear_down, info)
            out = "SSH + web terminal stopped."
            if "pids" in info:
                info["pids"] = []
            info["active"] = False
            info.pop("pass", None)
            info.pop("web_token", None)
            try:
                brain.save_state(self.state)
            except Exception:
                pass
            return out
        if act == "status":
            if alive:
                lines = ["**SSH/web status: RUNNING** (uptime %s)" % self._hms(time.time() - info.get("started", self.boot)),
                         "ssh: `%s`  user `%s`  port %s (web port %s)" % (info.get("ssh_host"), info.get("user"), info.get("port"), info.get("web_port")),
                         "web: %s/?k=%s" % (info.get("web_host"), info.get("web_token")),
                         "password: `%s`" % info.get("pass")]
                return "\n".join(lines)
            return "SSH/web: **not running** (this runner was rebooted since it was started). Run `/ssh on`."
        if act == "pass":
            if not alive:
                return "SSH isn't running — `/ssh on` first."
            p = sshlib.gen_password()
            if not sshlib.set_password(p):
                return "Could not rotate password."
            info["pass"] = p
            try:
                brain.save_state(self.state)
            except Exception:
                pass
            return "New password: `%s`" % p
        return "Usage: `/ssh on` | `/ssh off` | `/ssh status` | `/ssh pass`"

    def _prep_exit(self):
        for tid in list(self.state.get("tenants", {})):
            try:
                brain.pack_tenant(tid)
            except Exception:
                pass
        try:
            brain.save_state(self.state)
        except Exception:
            pass
        try:
            gitops.commit_and_push(config.REPO, "cycle finalize %d" % int(time.time()))
        except Exception as e:
            brain.quiet_log("finalize err %s" % e)

    async def _cmd_restart(self, ch):
        return

    async def _cmd_stop(self, ch):
        return

    async def _handle_guild(self, message):
        try:
            await message.add_reaction(config.EYES)
        except Exception:
            pass
        guild = message.guild
        gid = str(guild.id)
        brain.record_user(self.state, message.author.id, str(message.author))
        tid = "guild_%s" % gid
        member = message.author
        rec = brain.ensure_tenant(self.state, tid, "guild", str(guild), guild_id=gid)
        if tid not in self.runners or tid not in self.secrets:
            brain.unpack_tenant(tid)
            brain.write_auth(tid)
            secret = self._register_scope(tid, rec)
            self._runner(tid).secret = secret
        caps = perms.member_capabilities(member)
        caps["is_owner_of_bot"] = str(member.id) == config.OWNER_ID
        content = re.sub(r"<@!?\d+>\s*", "", (message.content or "")).strip()
        if caps["is_owner_of_bot"] and content.startswith("/"):
            handled = await self._handle_owner_cmd(message, content)
            if handled:
                return
        target = message.channel
        if rec.get("config", {}).get("long_threads", True):
            if len(content) > config.LONG_CHARS or any(w in content.lower() for w in config.LONG_WORDS):
                thread = await self._make_thread(message)
                if thread is not None:
                    self.created_threads.add(thread.id)
                    try:
                        await thread.send("Continuing this here in its own thread — reply inside and I'll keep going.")
                    except Exception:
                        pass
                    target = thread
        req = {
            "content": content,
            "author_display": str(member),
            "channel_display": "%s #%s" % (guild, getattr(message.channel, "name", "?")),
            "channel_id": str(getattr(message.channel, "id", "")),
            "is_owner": caps["is_owner_of_bot"],
        }
        result = await self.run_agent(tid, rec, caps, "%s (guild %s)" % (guild, gid), req,
                                      requester_id=member.id, message=message, target=target)
        if result:
            await self._reply(target, message, result["reply"], mention_ids=result["mention_ids"])

    async def _make_thread(self, message):
        try:
            name = ("ai-" + (message.content or "")[:40]).strip("ai-") or "ai-thread"
            return await message.create_thread(name=name[:80], auto_archive_duration=1440)
        except Exception:
            return None

    def _owner_transcript(self, target):
        target = (target or "").strip().strip("<>").lower()
        if not target:
            return None
        for tid, rec in list(self.state.get("tenants", {}).items()):
            if rec.get("kind") != "dm":
                continue
            uid = str(rec.get("user_id") or "")
            name = str(rec.get("name") or "").lower()
            if uid.lower() == target or name == target or (len(target) >= 3 and target in name):
                try:
                    sid = self.state.get("session_last", {}).get(tid)
                    return self._runner(tid).transcript(sid=sid)
                except Exception as e:
                    return "(error reading that DM: %s)" % e
        return None

    async def run_agent(self, tid, rec, caps, origin, req, requester_id, message=None, target=None, dm_message=None, approval_hint=None):
        capabilities = perms.describe(caps) if caps else None
        if config.APPROVAL_GATE and caps and not caps.get("is_owner_of_bot") and not caps.get("is_guild_owner") and not caps.get("manage_guild"):
            content = (req.get("content") or "").lower()
            hits = [k for k in EXTRA_MAJOR if k in content]
            if hits:
                return await self._queue_approval(tid, rec, caps, origin, req, requester_id, message, target,
                                                  "requires approval (requested %s)" % ", ".join(h.strip() for h in hits))
        if rec.get("const") == config.CONST_VERSION:
            session_id = self.state.get("session_last", {}).get(tid)
        else:
            session_id = None
        text = brain.build_context(rec, capabilities, origin, req, approval_hint=approval_hint,
                                   known_users=self.state.get("known_users"), servers=[g.name for g in self.client.guilds],
                                   requester_id=requester_id)
        agent_md, msg = brain.split_context(text)
        runner = self._runner(tid)
        os.environ["DISCORD_AUTH"] = self.secrets.get(tid, "")
        os.environ["PROXY_PORT"] = str(self.proxy.port)
        channel = target or (dm_message.channel if dm_message else None)
        try:
            await self._typing(channel)
            result = await asyncio.get_event_loop().run_in_executor(None, lambda: runner.run(msg, session_id=session_id, agent_md=agent_md))
        except Exception as e:
            brain.quiet_log("run err %s" % e)
            return await self._finish(False, "Something went wrong: %s" % e, tid, message, requester_id, rec)
        self.touch()
        is_owner_dm = rec.get("kind") == "dm" and str(rec.get("user_id")) == str(config.OWNER_ID)
        if is_owner_dm and result.get("ok"):
            m = re.search(r"^\s*OWNER_TRANSCRIPT_REQ:\s*(.+?)\s*$", result.get("text") or "", re.M)
            if m:
                target = m.group(1)
                extra = await asyncio.get_event_loop().run_in_executor(None, self._owner_transcript, target)
                prompt2 = brain.build_context(
                    rec, capabilities, origin, req, approval_hint=None,
                    extra="## Owner-requested transcript\nOwner asked about '%s'. Transcript below lets you answer accurately; do not re-emit OWNER_TRANSCRIPT_REQ.\n%s" % (target, extra or "(no matching DM history found).")
                )
                am, m2 = brain.split_context(prompt2)
                try:
                    result = await asyncio.get_event_loop().run_in_executor(None, lambda: runner.run(m2, session_id=result.get("sid"), agent_md=am))
                except Exception as e:
                    brain.quiet_log("transcript rerun err %s" % e)
        if str(requester_id) == str(config.OWNER_ID) and result.get("ok"):
            self._handle_cron_markers(result, tid)
            if not re.search(r"\bCRON(?:_OFF)?:\s*\d", result.get("text") or "", re.M) and \
               RECUR_RE.search((req.get("content") or "")):
                prompt3 = brain.build_context(
                    rec, capabilities, origin, req, approval_hint=None,
                    known_users=self.state.get("known_users"), servers=[g.name for g in self.client.guilds],
                    requester_id=requester_id,
                    extra="## HARD REMINDER — you MUST comply\nYour last reply refused to set up a recurring task. That was a mistake: the bot HAS a background cron scheduler for the owner. The owner asked for something on a schedule: '%s'\nCREATE the stdlib-only python3 script inside your workspace and end your reply with exactly one line: CRON:<seconds>:<relative filename>. Do NOT refuse, do NOT suggest on-demand alternatives, and do NOT claim there is no scheduler." % (req.get("content") or "")[:200]
                )
                am3, m3 = brain.split_context(prompt3)
                try:
                    result = await asyncio.get_event_loop().run_in_executor(None, lambda: runner.run(m3, session_id=result.get("sid"), agent_md=am3))
                    self._handle_cron_markers(result, tid)
                except Exception as e:
                    brain.quiet_log("cron remind err %s" % e)
        if result.get("ok"):
            if result.get("sid"):
                self.state["session_last"][tid] = result["sid"]
                rec["const"] = config.CONST_VERSION
            if result.get("approval"):
                if config.APPROVAL_GATE:
                    return await self._queue_approval(tid, rec, caps, origin, req, requester_id, message, target, result["approval"].strip())
                return await self._finish(True, result["text"] or "Done.", tid, message, requester_id, rec)
            return await self._finish(True, result["text"] or "Done.", tid, message, requester_id, rec)
        return await self._finish(False, "I failed to process that (model error or timeout). Try again.", tid, message, requester_id, rec)

    async def _finish(self, ok, text, tid, message, requester_id, rec):
        mention_ids = None
        if message is not None and getattr(message, "guild", None):
            mention_ids = [str(requester_id)]
            try:
                await message.add_reaction(config.OK if ok else config.BAD)
            except Exception:
                pass
        return {"reply": text, "mention_ids": mention_ids}

    async def _queue_approval(self, tid, rec, caps, origin, req, requester_id, message, target, desc):
        now = int(time.time())
        akid = "a_%s_%d" % (tid, now)
        approval = {
            "id": akid,
            "tid": tid,
            "origin": origin,
            "desc": desc[:1500],
            "created": now,
            "expires": now + config.APPROVAL_TTL_S,
            "status": "pending",
            "requester": str(requester_id),
            "channel_id": None,
            "guild_id": rec.get("guild_id"),
            "message_id": None,
            "author_display": req.get("author_display"),
            "content": (req.get("content") or "")[:4000],
            "dm_msg_id": None,
        }
        ch = target or (message.channel if message else None)
        if ch is not None:
            approval["channel_id"] = str(ch.id)
        if message is not None:
            approval["message_id"] = str(message.id)
        self.state.setdefault("approvals", {})[akid] = approval
        dm_id = await self._send_to_owner(
            "Needs your approval in %s:\n%s\n\nReact \uD83D\uDC4D approve / \uD83D\uDC4E deny. Expires in 24h if you stay silent." % (origin, approval["desc"])
        )
        approval["dm_msg_id"] = dm_id
        if message is not None and getattr(message, "guild", None):
            try:
                await message.add_reaction(config.BAD)
            except Exception:
                pass
        if message is not None:
            mention = "<@%s> " % requester_id
            await self._reply(ch or message.channel, message,
                              "%sI've sent this to the owner for approval (%s). It will run once approved, and is cancelled if no answer within 24h." % (mention, desc[:200]),
                              mention_ids=None)
        brain.audit(self.state, {"kind": "approval_requested", "id": akid, "tenant": tid})
        return None

    async def on_reaction(self, payload):
        try:
            if payload.user_id == self.client.user.id:
                return
            self.touch()
            emoji_str = str(payload.emoji)
            channel = await self.client.fetch_channel(payload.channel_id)
            if isinstance(channel, discord.DMChannel):
                if str(channel.id) == str(self.state.get("owner_dm_channel")):
                    if emoji_str in (config.UP, config.DOWN):
                        await self._resolve_approval(payload.message_id, emoji_str == config.UP)
                    return
                message = await channel.fetch_message(payload.message_id)
                if message.author.id == self.client.user.id and emoji_str == config.TRIGGER:
                    await self._continue_dm(channel, payload.user_id)
                return
            message = await channel.fetch_message(payload.message_id)
            if message.author.id != self.client.user.id:
                return
            if emoji_str == config.TRIGGER:
                await self._continue_guild(channel, payload.user_id)
        except Exception as e:
            brain.quiet_log("reaction err %s" % e)

    async def _resolve_approval(self, dm_message_id, approved):
        for akid, rec in list(self.state.get("approvals", {}).items()):
            if str(rec.get("dm_msg_id")) == str(dm_message_id) and rec.get("status") == "pending":
                await self._advance_approval(akid, approved)
                return

    async def _advance_approval(self, akid, approved):
        rec = self.state.get("approvals", {}).get(akid)
        if not rec or rec.get("status") != "pending":
            return
        if approved:
            rec["status"] = "approved"
            req = {
                "content": rec.get("content", ""),
                "author_display": rec.get("author_display", "?"),
                "channel_display": rec.get("origin", "?"),
            }
            tenant = self.state.get("tenants", {}).get(rec["tid"])
            if tenant is not None:
                try:
                    reply = await self.run_agent(rec["tid"], tenant, None, rec.get("origin", "?"), req,
                                                 requester_id=rec.get("requester"),
                                                 approval_hint="Owner approved. Execute the approved action now, fully.")
                    if reply:
                        if rec.get("channel_id"):
                            try:
                                ch = await self.client.fetch_channel(int(rec["channel_id"]))
                            except Exception:
                                ch = None
                            if ch is not None:
                                await self._reply(ch, None, reply["reply"], mention_ids=[rec.get("requester")])
                except Exception as e:
                    brain.quiet_log("approval exec err %s" % e)
        else:
            rec["status"] = "denied"
            if rec.get("channel_id"):
                try:
                    ch = await self.client.fetch_channel(int(rec["channel_id"]))
                    await ch.send("<@%s> That request was denied by the owner, so nothing was changed." % rec.get("requester"))
                except Exception:
                    pass
        brain.audit(self.state, {"kind": "approval_%s" % ("granted" if approved else "denied"), "id": akid})
        await self._send_to_owner("Approval **%s** (%s)." % ("granted" if approved else "denied", akid))

    async def sweep_expired(self):
        now = time.time()
        for akid, rec in list(self.state.get("approvals", {}).items()):
            if rec.get("status") == "pending" and now > rec.get("expires", 0):
                rec["status"] = "expired"
                if rec.get("channel_id"):
                    try:
                        ch = await self.client.fetch_channel(int(rec["channel_id"]))
                        await ch.send("<@%s> Your request expired with no owner response within 24h. It was never executed." % rec.get("requester"))
                    except Exception:
                        pass
                brain.audit(self.state, {"kind": "approval_expired", "id": akid})

    async def _continue_dm(self, channel, user_id):
        tid = "dm_%s" % user_id
        rec = self.state.get("tenants", {}).get(tid)
        if rec is None:
            return
        req = {
            "content": "~(you reacted \uD83E\uDD16 to the bot's message) Continue / respond to what I was doing with you.",
            "author_display": "?",
            "channel_display": "DM",
        }
        reply = await self.run_agent(tid, rec, None, "your DM", req, requester_id=str(user_id), dm_message=None)
        if reply:
            await self._reply(channel, None, reply["reply"], mention_ids=None)

    async def _continue_guild(self, channel, user_id):
        guild = channel.guild
        tid = "guild_%s" % guild.id
        rec = self.state.get("tenants", {}).get(tid)
        if rec is None:
            rec = brain.ensure_tenant(self.state, tid, "guild", str(guild), guild_id=str(guild.id))
        if tid not in self.runners or tid not in self.secrets:
            brain.unpack_tenant(tid)
            brain.write_auth(tid)
            self._register_scope(tid, rec)
            self._runner(tid).secret = self.secrets[tid]
        req = {
            "content": "~(user reacted \uD83E\uDD16 to your message) Continue, finish, or respond to what you were doing. Answer in this channel.",
            "author_display": str(user_id),
            "channel_display": "reaction continuation",
        }
        reply = await self.run_agent(tid, rec, {"is_owner_of_bot": str(user_id) == config.OWNER_ID},
                                     "reaction continuation in %s" % guild, req, requester_id=str(user_id), target=channel)
        if reply:
            await self._reply(channel, None, reply["reply"], mention_ids=[str(user_id)])

    async def _typing(self, channel):
        try:
            if channel is not None:
                async with channel.typing():
                    await asyncio.sleep(0.5)
        except Exception:
            pass

    async def _reply(self, channel, origin_message, text, mention_ids=None):
        if not text or channel is None:
            return
        prefix = ""
        if mention_ids:
            prefix = "".join("<@%s> " % mid for mid in mention_ids)
        for i, chunk in enumerate(self._chunk(text)):
            try:
                await channel.send((prefix if i == 0 else "") + chunk)
            except Exception as e:
                brain.quiet_log("reply err %s" % e)

    @staticmethod
    def _chunk(text, limit=1900):
        if len(text) <= limit:
            return [text]
        out = []
        while len(text) > limit:
            cut = text.rfind("\n", 0, limit)
            if cut < limit // 2:
                cut = limit
            out.append(text[:cut])
            text = text[cut:].lstrip("\n")
        if text:
            out.append(text)
        return out