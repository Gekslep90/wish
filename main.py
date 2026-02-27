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


def compute_seller_receives(price_wei: int, fee_bps: int) -> int:
    fee = compute_fee_wei(price_wei, fee_bps)
    return price_wei - fee


# -----------------------------------------------------------------------------
# Config and state (for CLI)
# -----------------------------------------------------------------------------

@dataclass
class WishConfig:
    contract_address: str = ""
    rpc_url: str = ""
    chain_id: int = 1
    fee_bps: int = 12


def load_config(path: Optional[str] = None) -> WishConfig:
    path = path or os.path.join(os.path.dirname(__file__), "wish_config.json")
    cfg = WishConfig()
    if os.path.isfile(path):
        try:
            with open(path, "r") as f:
                data = json.load(f)
            cfg.contract_address = data.get("contractAddress", "")
            cfg.rpc_url = data.get("rpcUrl", "")
            cfg.chain_id = int(data.get("chainId", 1))
            cfg.fee_bps = int(data.get("feeBps", 12))
        except Exception:
            pass
    return cfg


def save_config(cfg: WishConfig, path: Optional[str] = None) -> None:
    path = path or os.path.join(os.path.dirname(__file__), "wish_config.json")
    data = {
        "contractAddress": cfg.contract_address,
        "rpcUrl": cfg.rpc_url,
        "chainId": cfg.chain_id,
        "feeBps": cfg.fee_bps,
    }
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


# -----------------------------------------------------------------------------
# In-memory spell store (simulation without RPC)
# -----------------------------------------------------------------------------

@dataclass
class SpellEntry:
    spell_id: int
    seller: str
    title_hash: bytes
    category_hash: bytes
    price_wei: int
    listed_at_block: int
    listed: bool


class WishSpellStore:
    def __init__(self) -> None:
        self._spells: Dict[int, SpellEntry] = {}
        self._counter = 0
        self._spell_ids: List[int] = []

    def list_spell(self, seller: str, title_hash: bytes, category_hash: bytes, price_wei: int, block: int = 0) -> int:
        if self._counter >= SPEL_MAX_SPELLS:
            raise ValueError("Max spells reached")
        self._counter += 1
        spell_id = self._counter
        self._spells[spell_id] = SpellEntry(
            spell_id=spell_id,
            seller=seller,
            title_hash=title_hash,
            category_hash=category_hash,
            price_wei=price_wei,
            listed_at_block=block,
            listed=True,
        )
        self._spell_ids.append(spell_id)
        return spell_id

    def delist(self, spell_id: int) -> None:
        if spell_id not in self._spells:
            raise ValueError("Spell not found")
        self._spells[spell_id].listed = False

    def get_spell(self, spell_id: int) -> SpellEntry:
        if spell_id not in self._spells:
            raise ValueError("Spell not found")
        return self._spells[spell_id]

    def get_listed_ids(self) -> List[int]:
        return [s for s in self._spell_ids if self._spells[s].listed]

    def get_spell_ids(self) -> List[int]:
        return list(self._spell_ids)


# -----------------------------------------------------------------------------
# CLI: fee calculator
# -----------------------------------------------------------------------------

def cmd_fee(args: argparse.Namespace) -> int:
    price = int(args.price)
    fee_bps = int(args.fee_bps) if args.fee_bps else 12
    if price < 0 or fee_bps < 0 or fee_bps > SPEL_MAX_FEE_BPS:
        print("Invalid price or fee_bps (0-%s)" % SPEL_MAX_FEE_BPS, file=sys.stderr)
        return 1
    fee = compute_fee_wei(price, fee_bps)
    to_seller = compute_seller_receives(price, fee_bps)
    print("Price: %s wei" % price)
    print("Fee (bps %s): %s wei" % (fee_bps, fee))
    print("To seller: %s wei" % to_seller)
    return 0


# -----------------------------------------------------------------------------
# CLI: hash title/category
# -----------------------------------------------------------------------------

def cmd_hash(args: argparse.Namespace) -> int:
    s = args.string
    kind = (args.kind or "title").lower()
    if kind == "title":
        h = title_hash_from_string(s)
    else:
        h = category_hash_from_string(s)
    print(bytes32_to_hex(h))
    return 0


# -----------------------------------------------------------------------------
# CLI: simulate list
# -----------------------------------------------------------------------------

def cmd_simulate_list(args: argparse.Namespace) -> int:
    title = args.title
    category = args.category
    price = int(args.price)
    seller = args.seller or ZERO_ADDRESS
    if price <= 0:
        print("Price must be positive", file=sys.stderr)
