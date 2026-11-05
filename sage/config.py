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
MODEL = os.getenv("SAGE_MODEL", "deepseek-r1:7b")
CODING_MODEL = os.getenv("SAGE_CODING_MODEL", os.getenv("SAGE_MODEL", "qwen2.5-coder:7b"))
VISION_MODEL = os.getenv("SAGE_VISION_MODEL", "llava:7b")
EMBED_MODEL = os.getenv("SAGE_EMBED_MODEL", "nomic-embed-text")
HOST = os.getenv("SAGE_HOST", "127.0.0.1")
PORT = int(os.getenv("SAGE_PORT", "8000"))
PROJECT_WORK_TIMEOUT = 600
ALLOWED_APPS = os.getenv("SAGE_ALLOWED_APPS", "{}")
WEB_SEARCH_ENABLED = os.getenv("SAGE_WEB_SEARCH_ENABLED", "true").lower() in ("1", "true", "yes", "on")
WAKE_WORD = os.getenv("SAGE_WAKE_WORD", "sage")
PASSWORD = os.getenv("SAGE_PASSWORD", "")  # dashboard login; empty = no login (set it in the ignored start-sage.local.bat)
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
BLE_NEAR_RSSI = int(os.getenv("SAGE_BLE_NEAR_RSSI", "-65"))
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
