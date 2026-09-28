import asyncio
import os
import sys
import time

import discord

import brain
import config
import gitops
import proxy as proxymod
from controller import Controller

LOOP_S = 2


class Supervisor:
    def __init__(self):
        self.state = brain.load_state()
        self.proxy = proxymod.Proxy(self.state, lambda e: brain.audit(self.state, e))
        self.boot = time.time()
        self.client = None
        self.controller = None
        if not config.DS_TOKEN:
            raise RuntimeError("DS_TOKEN is not set")

    def build_client(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guild_messages = True
        intents.dm_messages = True
        intents.guild_reactions = True
        intents.dm_reactions = True
        intents.members = True
        client = discord.Client(intents=intents)
        controller = Controller(client, self.state, self.proxy)
        self.client = client
        self.controller = controller

        @client.event
        async def on_ready():
            brain.quiet_log("online as %s" % client.user)
            acts = discord.Activity(type=discord.ActivityType.watching, name="the ai kitchen")
            await client.change_presence(activity=acts)
            await controller.on_online()
            asyncio.get_event_loop().run_in_executor(None, gitops.commit_and_push, config.REPO, "online %d" % int(time.time()))
            client.loop.create_task(self._lifecycle())

        @client.event
        async def on_message(message):
            await controller.handle_message(message)

        @client.event
        async def on_raw_reaction_add(payload):
            await controller.on_reaction(payload)

        return client

    async def _lifecycle(self):
        last_save = time.time()
        while True:
            await asyncio.sleep(LOOP_S)
            now = time.time()
            elapsed = now - self.boot
            idle = now - self.controller.last_activity
            if now - last_save > 90:
                last_save = now
                await asyncio.get_event_loop().run_in_executor(None, self._save_state)
            try:
                await self.controller.sweep_expired()
            except Exception as e:
                brain.quiet_log("sweep err %s" % e)
            force = elapsed >= config.FORCE_S
            idle_restart = elapsed >= config.MAX_CYCLE_S - 120 and idle >= config.IDLE_RESTART_S
            if force or idle_restart:
                brain.quiet_log("restarting (%s)" % ("force" if force else "idle"))
                self._finalize()
                await asyncio.get_event_loop().run_in_executor(None, gitops.dispatch)
                try:
                    await self.client.close()
                except Exception:
                    pass
                return

    def _save_state(self):
        try:
            brain.save_state(self.state)
        except Exception as e:
            brain.quiet_log("save err %s" % e)

    def _finalize(self):
        try:
            for tid in list(self.state.get("tenants", {})):
                try:
                    brain.pack_tenant(tid)
                except Exception as e:
                    brain.quiet_log("pack fail %s %s" % (tid, e))
            self._save_state()
            ok = gitops.commit_and_push(config.REPO, "cycle finalize %d" % int(time.time()))
            brain.quiet_log("finalize commit=%s" % ok)
        except Exception as e:
            brain.quiet_log("finalize err %s" % e)

    def run(self):
        self._save_state()
        self.proxy.start()
        brain.quiet_log("proxy on %s" % self.proxy.port)
        client = self.build_client()
        try:
            client.run(config.DS_TOKEN, reconnect=True, log_handler=None)
        except KeyboardInterrupt:
            pass
        except Exception as e:
            brain.quiet_log("client error %s" % e)
            self._finalize()


def main():
    sys.stdout = open(os.devnull, "w")
    sys.stderr = open(os.devnull, "w")
    sup = Supervisor()
    try:
        sup.run()
    except SystemExit:
        raise
    except Exception as e:
        brain.quiet_log("fatal %s" % e)
        try:
            sup._finalize()
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()