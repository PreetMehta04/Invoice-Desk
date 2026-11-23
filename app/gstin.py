"""GSTIN (Indian GST registration number) format + checksum validation."""
import re

CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")

STATES = {"27": "Maharashtra", "29": "Karnataka", "33": "Tamil Nadu", "36": "Telangana",
          "07": "Delhi", "24": "Gujarat", "32": "Kerala", "19": "West Bengal"}


def checksum_char(first14: str) -> str:
    total = 0
    for i, ch in enumerate(first14):
        prod = CHARS.index(ch) * (1 if i % 2 == 0 else 2)
        total += prod // 36 + prod % 36
    return CHARS[(36 - total % 36) % 36]


def make(state: str, pan: str, entity: str = "1") -> str:
    base = f"{state}{pan}{entity}Z"
    return base + checksum_char(base)


def validate(gstin: str | None) -> tuple[bool, str]:
    if not gstin:
        return False, "missing"
    g = gstin.strip().upper().replace(" ", "")
    if not GSTIN_RE.match(g):
        return False, "invalid format"
    if checksum_char(g[:14]) != g[14]:
        return False, "checksum digit does not match"
    return True, "valid"


def state_of(gstin: str | None) -> str | None:
    if gstin and len(gstin) >= 2:
        return gstin[:2]
    return None
