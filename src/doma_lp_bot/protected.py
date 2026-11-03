"""Explicit NFT exclusions; no user-specific positions are bundled."""
import os
from dotenv import load_dotenv
load_dotenv()
_raw = os.getenv("PROTECTED_NFT_IDS", "")
try:
    PROTECTED_NFTS = frozenset(int(x.strip()) for x in _raw.split(",") if x.strip())
except ValueError as exc:
    raise ValueError("PROTECTED_NFT_IDS must be comma-separated positive integers") from exc
if any(x <= 0 for x in PROTECTED_NFTS):
    raise ValueError("PROTECTED_NFT_IDS must be positive")
