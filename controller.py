import asyncio
import os
import secrets
import time

import discord

import brain
import config
import opencode_runner
import perms

EXTRA_MAJOR = (
    "ban ", "kick ", "delete channel", "delete role", "delete ", "clear all",
    "@everyone", "wipe the", "rename the ", "change the server name", "server icon", "permission",
)


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
        for tid, rec in list(self.state["tenants"].items()):
            try:
                brain.unpack_tenant(tid)
                secret = self._register_scope(tid, rec)
                self._runner(tid).secret = secret
            except Exception as e:
                brain.quiet_log("tenant init fail %s %s" % (tid, e))
        await self.owner_notify(
            "opencode-ai is online. Cycle runs up to 5h then self-restarts. "
            "Major actions wait for your approval in DMs. React \uD83D\uDC4D/\uD83D\uDC4E or reply to decide."
        )

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
                await self._handle_guild(message)
                return
            if self.client.user in message.mentions:
                await self._handle_guild(message)
                return
            if message.reference and getattr(message.reference, "message_id", None):
                try:
                    ref = await message.channel.fetch_message(message.reference.message_id)
                    if ref.author.id == self.client.user.id:
                        await self._handle_guild(message)
                        return
                except Exception:
                    pass
        except Exception as e:
            brain.quiet_log("handle_message err %s" % e)

    async def _handle_dm(self, message):
        author_id = str(message.author.id)
        tid = "dm_%s" % author_id
        is_owner = author_id == config.OWNER_ID
        if is_owner:
            self.state["owner_dm_channel"] = message.channel.id
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

    async def _handle_guild(self, message):
        guild = message.guild
        gid = str(guild.id)
        tid = "guild_%s" % gid
        member = message.author
        rec = brain.ensure_tenant(self.state, tid, "guild", str(guild), guild_id=gid)
        if gid == "1526896407078895737":
            extras = rec.get("config", {}).get("ping_extra", [])
            for who in ("1348335067562377236", "1305964364595073048"):
                if who not in extras:
                    extras.append(who)
            rec["config"]["ping_extra"] = extras
        if tid not in self.runners or tid not in self.secrets:
            brain.unpack_tenant(tid)
            brain.write_auth(tid)
            secret = self._register_scope(tid, rec)
            self._runner(tid).secret = secret
        caps = perms.member_capabilities(member)
        caps["is_owner_of_bot"] = str(member.id) == config.OWNER_ID
        try:
            await message.add_reaction(config.EYES)
        except Exception:
            pass
        content = message.content or ""
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

    async def run_agent(self, tid, rec, caps, origin, req, requester_id, message=None, target=None, dm_message=None, approval_hint=None):
        capabilities = perms.describe(caps) if caps else None
        if caps and not caps.get("is_owner_of_bot") and not caps.get("is_guild_owner") and not caps.get("manage_guild"):
            content = (req.get("content") or "").lower()
            hits = [k for k in EXTRA_MAJOR if k in content]
            if hits:
                return await self._queue_approval(tid, rec, caps, origin, req, requester_id, message, target,
                                                  "requires approval (requested %s)" % ", ".join(h.strip() for h in hits))
        prompt = brain.build_context(rec, capabilities, origin, req, approval_hint=approval_hint)
        runner = self._runner(tid)
        os.environ["DISCORD_AUTH"] = self.secrets.get(tid, "")
        os.environ["PROXY_PORT"] = str(self.proxy.port)
        session_id = self.state.get("session_last", {}).get(tid)
        channel = target or (dm_message.channel if dm_message else None)
        try:
            await self._typing(channel)
            result = await asyncio.get_event_loop().run_in_executor(None, lambda: runner.run(prompt, session_id=session_id))
        except Exception as e:
            brain.quiet_log("run err %s" % e)
            return await self._finish(False, "Something went wrong: %s" % e, tid, message, requester_id, rec)
        self.touch()
        if result.get("ok"):
            if result.get("sid"):
                self.state["session_last"][tid] = result["sid"]
            if result.get("approval"):
                return await self._queue_approval(tid, rec, caps, origin, req, requester_id, message, target, result["approval"].strip())
            return await self._finish(True, result["text"] or "Done.", tid, message, requester_id, rec)
        return await self._finish(False, "I failed to process that (model error or timeout). Try again.", tid, message, requester_id, rec)

    async def _finish(self, ok, text, tid, message, requester_id, rec):
        mention_ids = None
        if message is not None and getattr(message, "guild", None):
            mention_ids = [str(requester_id)]
            for extra in (rec.get("config", {}) or {}).get("ping_extra", []):
                if extra not in mention_ids:
                    mention_ids.append(str(extra))
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
                await channel.typing()
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