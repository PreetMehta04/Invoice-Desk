"""Runtime configuration (env vars) and editable business rules."""
import json
import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", ROOT / "data"))
SEED_DIR = ROOT / "data" / "seed"
SAMPLES_DIR = ROOT / "samples"
STATIC_DIR = ROOT / "static"
UPLOAD_DIR = DATA_DIR / "uploads"
INBOX_DIR = Path(os.getenv("INBOX_DIR", DATA_DIR / "inbox"))
DB_PATH = DATA_DIR / "invoices.db"
RULES_PATH = DATA_DIR / "rules.json"
RULES_DEFAULT = ROOT / "data" / "rules.default.json"

for d in (DATA_DIR, UPLOAD_DIR, INBOX_DIR):
    d.mkdir(parents=True, exist_ok=True)


def _load_dotenv():
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()

# --- AI provider -------------------------------------------------------------
# EXTRACTION_PROVIDER: auto | anthropic | gemini | openai | local
EXTRACTION_PROVIDER = os.getenv("EXTRACTION_PROVIDER", "auto").lower()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "") or os.getenv("GOOGLE_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
LLM_TIMEOUT_S = float(os.getenv("LLM_TIMEOUT_S", "90"))
WATCH_INBOX = os.getenv("WATCH_INBOX", "1") == "1"


def resolve_provider() -> str:
    p = EXTRACTION_PROVIDER
    if p != "auto":
        return p
    if ANTHROPIC_API_KEY:
        return "anthropic"
    if GEMINI_API_KEY:
        return "gemini"
    if OPENAI_API_KEY:
        return "openai"
    return "local"


def provider_label(p: str | None = None) -> str:
    p = p or resolve_provider()
    return {
        "anthropic": f"Claude ({ANTHROPIC_MODEL})",
        "gemini": f"Gemini ({GEMINI_MODEL})",
        "openai": f"OpenAI ({OPENAI_MODEL})",
        "local": "Local OCR + parser (no API key)",
    }.get(p, p)


# --- Business rules ----------------------------------------------------------
def load_rules() -> dict:
    if not RULES_PATH.exists():
        shutil.copy(RULES_DEFAULT, RULES_PATH)
    rules = json.loads(RULES_DEFAULT.read_text())
    rules.update(json.loads(RULES_PATH.read_text()))
    return rules


def save_rules(updates: dict) -> dict:
    rules = load_rules()
    defaults = json.loads(RULES_DEFAULT.read_text())
    for k, v in updates.items():
        if k not in defaults:
            continue
        d = defaults[k]
        if isinstance(d, bool):
            rules[k] = bool(v)
        elif isinstance(d, (int, float)):
            rules[k] = type(d)(float(v)) if isinstance(d, float) else int(float(v))
        else:
            rules[k] = str(v)
    RULES_PATH.write_text(json.dumps(rules, indent=2))
    return rules


def reset_rules():
    shutil.copy(RULES_DEFAULT, RULES_PATH)
