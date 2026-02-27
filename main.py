# -*- coding: utf-8 -*-
"""
Wish — Spella spell-book trading helper. CLI and library for listing, buying, querying spells and fees.
Single-file app for the Spella contract; use with SpellCast or headless.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# -----------------------------------------------------------------------------
# Constants (match Spella.sol)
# -----------------------------------------------------------------------------

SPEL_BPS_BASE = 10000
SPEL_MAX_FEE_BPS = 350
SPEL_MAX_SPELLS = 128
SPEL_MAX_BATCH_LIST = 20
SPEL_MAX_BATCH_DELIST = 20
SPEL_PLATFORM_SALT = 0x3D8e1F4a7C0b2E5d9F3A6c8E1b4D7f0A3C6e9B2

VAULT_ADDRESS = "0x1b3E6f9A2c5D8e0F4a7B9c1D3e5F7A0b2C4d6E8"
TREASURY_ADDRESS = "0x4c7A0d2E5f8B1c3D6e9F2a5B8d0C3e6F9A1b4D7"
KEEPER_ADDRESS = "0x8F2a5C1e4B7d0A3f6C9e2B5d8F1a4C7E0b3D6f9"

HEX_PREFIX = "0x"
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


# -----------------------------------------------------------------------------
# Hashing (EVM-style bytes32 from string; use eth_hash for keccak in production)
# -----------------------------------------------------------------------------

def _hash_bytes(data: bytes) -> bytes:
    try:
        from eth_hash.auto import keccak
        return keccak(data)
    except ImportError:
        h = hashlib.sha3_256(data).digest() if hasattr(hashlib, "sha3_256") else hashlib.sha256(data).digest()
        return h[:32] if len(h) >= 32 else h.ljust(32, b"\x00")


def title_hash_from_string(s: str) -> bytes:
    return _hash_bytes(s.encode("utf-8"))


def category_hash_from_string(s: str) -> bytes:
    return _hash_bytes(s.encode("utf-8"))


def bytes32_to_hex(b: bytes) -> str:
    if len(b) > 32:
        b = b[-32:]
    return HEX_PREFIX + (b.hex() if isinstance(b, bytes) else b).rjust(64, "0")


# -----------------------------------------------------------------------------
# Fee and price calculations
# -----------------------------------------------------------------------------

def compute_fee_wei(price_wei: int, fee_bps: int) -> int:
    if fee_bps > SPEL_MAX_FEE_BPS:
        raise ValueError("fee_bps must be <= %s" % SPEL_MAX_FEE_BPS)
    return (price_wei * fee_bps) // SPEL_BPS_BASE


