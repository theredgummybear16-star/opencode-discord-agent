# opencode-discord-agent

A Discord agent that is run by OpenAI... no wait, by **opencode** (OpenCode Zen models) inside GitHub Actions.

- Runs headless on a scheduled GitHub Actions job (every 5h, self-restarting, no output logged to the console).
- Joins any server when invited: responds to `@opencode-ai` pings, DMs, and `🤖` reactions on its own messages.
- Long requests get their own Discord thread.
- Per-server / per-DM **isolated** opencode sessions. Every tenant has its own encrypted `brains/*.gpg` memory archive; no tenant can see any other tenant's data or the owner's DMs.
- A request is only ever executed if the asker could do that action themselves (Discord permission parity). Major/irreversible actions wait for the bot owner's approval in DMs (👍/👎, 24h expiry).

## Setup

1. Create the repo secrets:
   - `DS_TOKEN` — Discord bot token
   - `BRAIN_PASSPHRASE` — used to encrypt tenant memory
   - `GH_ADMIN_TOKEN` — a GitHub PAT with contents+workflow write (used for self-restarts and memory commit-back)
   - `OPENCODE_MODEL` — e.g. `opencode/big-pickle` (OpenCode Zen; free models work without a key)
   - `OPENCODE_API_KEY` — optional OpenCode Zen key (needed only for paid Zen models)
   - `OWNER_ID` — the bot owner's Discord user id
2. Enable `MESSAGE_CONTENT` and `GUILD_MEMBERS` privileged intents for the bot in the Discord developer portal.
3. Invite the bot to servers, then run the `opencode-agent` workflow manually once.

Triggers: mention the bot anywhere, DM it, or react `🤖` to one of its messages to continue. React `👍`/`👎` on the owner-approval DMs to decide a requested change.