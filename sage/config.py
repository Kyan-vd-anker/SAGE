import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = (ROOT / 'VERSION').read_text(encoding='utf-8').strip() if (ROOT / 'VERSION').exists() else 'development'
DATA = Path(os.getenv("SAGE_DATA", ROOT / "data"))
DB_PATH = Path(os.getenv("SAGE_DB", DATA / "sage.db"))
VAULT = Path(os.getenv("SAGE_VAULT", DATA / "vault"))
WORKSPACE = Path(os.getenv("SAGE_WORKSPACE", DATA / "workspace"))
PLUGIN_DIR = Path(os.getenv("SAGE_PLUGINS", ROOT / "plugins"))
STATIC_DIR = Path(__file__).parent / "static"
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
# One context window (num_ctx) for every chat lane, background job and the VS Code endpoints. Every request sends it, so Ollama never
# reloads a model (and drops its prompt cache) because two callers asked for different sizes. 16384 leaves room for code in VS Code and
# costs about 1 GB of KV cache on a 7B model; raise it (SAGE_NUM_CTX=32768) if VS Code still reports context_length_exceeded.
NUM_CTX = max(2048, int(os.getenv("SAGE_NUM_CTX", "16384")))
# Three model lanes (see llm.model_for_request): fast = everyday replies, deep = code, tool calls and long input,
# reasoning = math, physics and trade-off discussions. The old SAGE_MODEL and SAGE_CODING_MODEL names still work.
FAST_MODEL = os.getenv("SAGE_FAST_MODEL", os.getenv("SAGE_MODEL", "qwen2.5:3b"))
DEEP_MODEL = os.getenv("SAGE_DEEP_MODEL", os.getenv("SAGE_CODING_MODEL", "qwen2.5-coder:7b"))
MODEL = FAST_MODEL
CODING_MODEL = DEEP_MODEL
REASONING_MODEL = os.getenv("SAGE_REASONING_MODEL", "deepseek-r1:7b")  # math, physics and trade-off discussions
ROUTER_LONG_INPUT_CHARS = int(os.getenv("SAGE_ROUTER_LONG_INPUT_CHARS", "1500"))  # longer messages go to the deep lane
VISION_MODEL = os.getenv("SAGE_VISION_MODEL", "llava:7b")
EMBED_MODEL = os.getenv("SAGE_EMBED_MODEL", "nomic-embed-text")
HOST = os.getenv("SAGE_HOST", "127.0.0.1")
PORT = int(os.getenv("SAGE_PORT", "8000"))
PROJECT_WORK_TIMEOUT = 600
ALLOWED_APPS = os.getenv("SAGE_ALLOWED_APPS", "{}")
WEB_SEARCH_ENABLED = os.getenv("SAGE_WEB_SEARCH_ENABLED", "true").lower() in ("1", "true", "yes", "on")
WAKE_WORD = os.getenv("SAGE_WAKE_WORD", "sage")
PASSWORD = os.getenv("SAGE_PASSWORD", "")  # dashboard login; empty = no login (set it in the ignored start-sage.local.bat)
VOICE_STREAM_REPLIES = os.getenv("SAGE_VOICE_STREAM", "1").lower() in ("1", "true", "yes", "on")  # speaker talks while the model is still writing
SPEAKER_ROOM = os.getenv("SAGE_SPEAKER_ROOM", "bedroom")  # the room the speaker is in (as named in SAGE_BLE_NODE_ROOMS)
# Document question answering (see docqa.py). NUM_CTX is the whole context window Ollama allocates for a document call
# (prompt + reply); Sage sizes every document prompt to fit inside it instead of letting Ollama silently truncate.
DOC_MODEL = os.getenv("SAGE_DOC_MODEL", REASONING_MODEL)
DOC_NUM_CTX = int(os.getenv("SAGE_DOC_NUM_CTX", "32000"))
DOC_MAP_MAX_CALLS = int(os.getenv("SAGE_DOC_MAP_MAX_CALLS", "16"))  # model passes used to read a document too long for one window
# Prompt prefix caching (see llm.py). Ollama only re-reads the part of a prompt that differs from the start of the previous one.
# SAGE_PROMPT_CACHE=0 turns the prompt re-arranging off. SAGE_CHAT_TURNS is how many recent exchanges (question + answer) are sent as real
# conversation messages so they are reused from the cache instead of being retrieved as quoted context; 0 keeps the old one-message prompt.
# The window moves in blocks of SAGE_CHAT_TURN_BLOCK exchanges, so the start of the conversation stays identical (and cached) between turns.
PROMPT_CACHE = os.getenv("SAGE_PROMPT_CACHE", "1") != "0"
CHAT_TURNS = max(0, int(os.getenv("SAGE_CHAT_TURNS", "0")))
CHAT_TURN_BLOCK = max(1, int(os.getenv("SAGE_CHAT_TURN_BLOCK", "3")))
CHAT_HISTORY_CHARS = int(os.getenv("SAGE_CHAT_HISTORY_CHARS", "6000"))      # budget for the transcript (about 1,800 tokens)
CHAT_MESSAGE_CHARS = int(os.getenv("SAGE_CHAT_MESSAGE_CHARS", "1500"))      # one long message is cut here before it enters the transcript
STUDIO_MODEL = os.getenv("SAGE_STUDIO_MODEL", "")  # model that reviews Studio text; empty = fast lane for documents, deep lane for LaTeX and sheets
LOGIN_EXEMPT_IPS = {item.strip() for item in os.getenv("SAGE_LOGIN_EXEMPT_IPS", "").split(",") if item.strip()}  # devices that skip the login (e.g. wall display); empty by default
# VS Code endpoints (see vscode.py). Disabled until SAGE_VSCODE_TOKEN is set; the extension sends it as `Authorization: Bearer <token>`.
# Thinking per lane: the fast and coder lanes answer straight away, the reasoning lane thinks first (override with SAGE_THINK_FAST / _DEEP / _REASONING = true|false).
def _flag(name, default):
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")
VSCODE_TOKEN = os.getenv("SAGE_VSCODE_TOKEN", "")
VSCODE_NUM_CTX = int(os.getenv("SAGE_VSCODE_NUM_CTX", str(NUM_CTX)))               # keep equal to the models' baked num_ctx so Ollama does not reload them
VSCODE_FIM = os.getenv("SAGE_VSCODE_FIM", "auto").strip().lower()             # auto = use the model's fill-in-the-middle when Ollama reports it; on | off
VSCODE_PREFIX_CHARS = int(os.getenv("SAGE_VSCODE_PREFIX_CHARS", "6000"))      # code before the cursor sent for an inline suggestion
VSCODE_SUFFIX_CHARS = int(os.getenv("SAGE_VSCODE_SUFFIX_CHARS", "1500"))     # code after the cursor
VSCODE_MAX_CONCURRENT = int(os.getenv("SAGE_VSCODE_MAX_CONCURRENT", "4"))
FAST_THINK = _flag("SAGE_THINK_FAST", False)
DEEP_THINK = _flag("SAGE_THINK_DEEP", False)
REASONING_THINK = _flag("SAGE_THINK_REASONING", True)
# Memory (memory.py, entities.py, userprofile.py, capture.py)
CORE_PROFILE_CHARS = int(os.getenv("SAGE_CORE_PROFILE_CHARS", "1400"))              # always-on profile block (about 400 tokens), part of the cached prompt prefix
CORE_PROFILE_VOICE_CHARS = int(os.getenv("SAGE_CORE_PROFILE_VOICE_CHARS", "450"))   # the smaller block used for voice and small talk
CAPTURE_ENABLED = _flag("SAGE_MEMORY_CAPTURE", True)                                 # idle-time LLM extraction of memories, entities and relations after a reply
PROJECT_STATE_ENABLED = _flag("SAGE_PROJECT_STATE", True)                            # idle-time "state of play" summaries per project
HA_TOKEN = os.getenv("SAGE_HA_TOKEN", "")
SHORTCUT_TOKEN = os.getenv("SAGE_SHORTCUT_TOKEN", "")
TELEGRAM_BOT_TOKEN = os.getenv("SAGE_TELEGRAM_BOT_TOKEN", "")
OCTOPRINT_URL = os.getenv("SAGE_OCTOPRINT_URL", "").strip()
OCTOPRINT_API_KEY = os.getenv("SAGE_OCTOPRINT_API_KEY", "").strip()
OCTOPRINT_STORAGE = os.getenv("SAGE_OCTOPRINT_STORAGE", "local").strip().lower()
FILE_SEARCH_ROOTS = os.getenv("SAGE_FILE_SEARCH_ROOTS", "")
ALLOWED_CLIENT_IPS = {item.strip() for item in os.getenv("SAGE_ALLOWED_CLIENT_IPS", "").split(",") if item.strip()}

# Voice gateway for the ESP32-S3 speaker (see VOICE-GUIDE.md)
_ON = ("1", "true", "yes", "on")
VOICE_ENABLED = os.getenv("SAGE_VOICE", "1").lower() in _ON
VOICE_HOST = os.getenv("SAGE_VOICE_HOST", HOST)
VOICE_PORT = int(os.getenv("SAGE_VOICE_PORT", "8002"))
VOICE_TOKEN = os.getenv("SAGE_VOICE_TOKEN", "")
VOICE_SECRET = os.getenv("SAGE_VOICE_SECRET", "")   # when set, every device request must be signed (HMAC-SHA256)
PHONE_TOKEN = os.getenv("SAGE_PHONE_TOKEN", "")     # the phone (Talk page / Shortcut) can only reach Sage when this is set
VOICE_LANGS = [item.strip() for item in os.getenv("SAGE_VOICE_LANGS", "nl,en").split(",") if item.strip()]
VOICE_DEFAULT_LANG = os.getenv("SAGE_VOICE_DEFAULT_LANG", "en")
VOICE_STT_MODEL = os.getenv("SAGE_STT_MODEL", "small")
VOICE_STT_DEVICE = os.getenv("SAGE_STT_DEVICE", "cpu")
VOICE_PIPER_CMD = os.getenv("SAGE_PIPER_CMD", "piper")
VOICE_PIPER_VOICES = {"nl": os.getenv("SAGE_PIPER_VOICE_NL", ""), "en": os.getenv("SAGE_PIPER_VOICE_EN", "")}
VOICE_MAX_CHARS = int(os.getenv("SAGE_VOICE_MAX_CHARS", "700"))
VOICE_BRIEF_MAX_CHARS = int(os.getenv("SAGE_VOICE_BRIEF_MAX_CHARS", "1600"))
VOICE_ALARM_BRIEF = os.getenv("SAGE_VOICE_ALARM_BRIEF", "1").lower() in _ON
BLE_PRESENCE_ENABLED = os.getenv("SAGE_BLE_PRESENCE", "0").lower() in _ON
BLE_NODE_ROOMS = {}
for _ble_node in os.getenv(
    "SAGE_BLE_NODE_ROOMS",
    "bedroom-speaker:bedroom,engineering-c3:engineering",
).split(","):
    if ":" in _ble_node:
        _node, _room = (part.strip() for part in _ble_node.split(":", 1))
        if _node and _room:
            BLE_NODE_ROOMS[_node] = _room
BLE_NEAR_RSSI = int(os.getenv("SAGE_BLE_NEAR_RSSI", "-80"))
BLE_AWAY_SECONDS = int(os.getenv("SAGE_BLE_AWAY_SECONDS", "600"))
BLE_NODE_TIMEOUT_SECONDS = int(os.getenv("SAGE_BLE_NODE_TIMEOUT_SECONDS", "90"))
if not -127 <= BLE_NEAR_RSSI <= 0:
    raise ValueError("SAGE_BLE_NEAR_RSSI must be between -127 and 0")
if BLE_AWAY_SECONDS <= 0:
    raise ValueError("SAGE_BLE_AWAY_SECONDS must be positive")
if BLE_NODE_TIMEOUT_SECONDS <= 0:
    raise ValueError("SAGE_BLE_NODE_TIMEOUT_SECONDS must be positive")
if BLE_PRESENCE_ENABLED and not {"bedroom", "engineering"}.issubset(BLE_NODE_ROOMS.values()):
    raise ValueError("SAGE_BLE_NODE_ROOMS must include bedroom and engineering receivers")
