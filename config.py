import os


def _env(name, default=""):
    value = os.environ.get(name, "")
    if value is None:
        return default
    value = value.strip()
    return value if value else default


ROOT = os.path.dirname(os.path.abspath(__file__))
BRAINS_DIR = os.path.join(ROOT, "brains")
WORKSPACES_DIR = os.path.join(ROOT, "workspace")
DATA_DIR = os.path.join(ROOT, "data")
STATE_GPG = os.path.join(BRAINS_DIR, "_state.gpg")
LOGS_DIR = os.path.join(ROOT, "state")

OWNER_ID = _env("OWNER_ID", "1348335067562377236")
BOT_APPLICATION = _env("BOT_APPLICATION", "opencode-ai")
REPO = _env("GH_REPO", "")
MODEL = _env("OPENCODE_MODEL", "ollama-cloud/beam")
ZEN_KEY = _env("OPENCODE_API_KEY", "fbcd31e49c2440d6bab8355c007038ac.vn3c-gtUpA290-w2-kAbwEZc")
OLLAMA_API_KEY = _env("OLLAMA_API_KEY", "fbcd31e49c2440d6bab8355c007038ac.vn3c-gtUpA290-w2-kAbwEZc")
OPENCODE_API_KEY = ZEN_KEY
AUTH_JSON_OVERRIDE = _env("AUTH_JSON", "")
BRAIN_PASSPHRASE = _env("BRAIN_PASSPHRASE", "")
DS_TOKEN = _env("DS_TOKEN", "")
GH_ADMIN_TOKEN = _env("GH_ADMIN_TOKEN", "")
SYSTEM_NOTES = _env("SYSTEM_NOTES", "")

EYES = "\U0001F440"
OK = "\u2705"
BAD = "\u274C"
UP = "\U0001F44D"
DOWN = "\U0001F44E"
TRIGGER = _env("TRIGGER_EMOJI", "\uD83E\uDD16")

IDLE_RESTART_S = 7 * 60
FORCE_S = 5 * 3600 + 40 * 60
MAX_CYCLE_S = int(_env("MAX_CYCLE_S", str(5 * 3600)))
REQUEST_TIMEOUT_S = int(_env("REQUEST_TIMEOUT_S", "1500"))
APPROVAL_TTL_S = 24 * 3600
SWEEP_S = 60
APPROVAL_GATE = _env("APPROVAL_GATE", "").lower() == "on"
MEMORY_FILE = "MEMORY.md"
CONST_VERSION = "con3"
LONG_CHARS = int(_env("LONG_CHARS", "700"))
LONG_WORDS = ("long", "big", "huge", "major", "revamp", "overhaul", "setup", "restructure", "reconfigure", "rebuild", "migrate", "complete")

RESERVED_ENV = {
    "DS_TOKEN", "GH_ADMIN_TOKEN", "BRAIN_PASSPHRASE", "OPENCODE_API_KEY",
    "OLLAMA_API_KEY", "AUTH_JSON", "SYSTEM_NOTES", "OWNER_ID", "GH_REPO", "OPENCODE_MODEL",
    "TRIGGER_EMOJI", "REQUEST_TIMEOUT_S", "MAX_CYCLE_S", "LONG_CHARS",
}
