import os

ROOT = os.path.dirname(os.path.abspath(__file__))
BRAINS_DIR = os.path.join(ROOT, "brains")
WORKSPACES_DIR = os.path.join(ROOT, "workspace")
DATA_DIR = os.path.join(ROOT, "data")
STATE_GPG = os.path.join(BRAINS_DIR, "_state.gpg")
LOGS_DIR = os.path.join(ROOT, "state")

OWNER_ID = os.environ.get("OWNER_ID", "1348335067562377236")
BOT_APPLICATION = os.environ.get("BOT_APPLICATION", "opencode-ai")
REPO = os.environ.get("GH_REPO", "")
MODEL = os.environ.get("OPENCODE_MODEL", "opencode/big-pickle")
ZEN_KEY = os.environ.get("OPENCODE_API_KEY", "")
AUTH_JSON_OVERRIDE = os.environ.get("AUTH_JSON", "")
BRAIN_PASSPHRASE = os.environ.get("BRAIN_PASSPHRASE", "")
DS_TOKEN = os.environ.get("DS_TOKEN", "")
GH_ADMIN_TOKEN = os.environ.get("GH_ADMIN_TOKEN", "")
SYSTEM_NOTES = os.environ.get("SYSTEM_NOTES", "")

EYES = "\U0001F440"
OK = "\u2705"
BAD = "\u274C"
UP = "\U0001F44D"
DOWN = "\U0001F44E"
TRIGGER = os.environ.get("TRIGGER_EMOJI", "\uD83E\uDD16")

IDLE_RESTART_S = 7 * 60
FORCE_S = 5 * 3600 + 40 * 60
MAX_CYCLE_S = int(os.environ.get("MAX_CYCLE_S", str(5 * 3600)))
REQUEST_TIMEOUT_S = int(os.environ.get("REQUEST_TIMEOUT_S", "1500"))
APPROVAL_TTL_S = 24 * 3600
SWEEP_S = 60
APPROVAL_GATE = os.environ.get("APPROVAL_GATE", "").lower() == "on"
MEMORY_FILE = "MEMORY.md"
LONG_CHARS = int(os.environ.get("LONG_CHARS", "700"))
LONG_WORDS = ("long", "big", "huge", "major", "revamp", "overhaul", "setup", "restructure", "reconfigure", "rebuild", "migrate", "complete")

RESERVED_ENV = {
    "DS_TOKEN", "GH_ADMIN_TOKEN", "BRAIN_PASSPHRASE", "OPENCODE_API_KEY",
    "AUTH_JSON", "SYSTEM_NOTES", "OWNER_ID", "GH_REPO", "OPENCODE_MODEL",
    "TRIGGER_EMOJI", "REQUEST_TIMEOUT_S", "MAX_CYCLE_S", "LONG_CHARS",
}