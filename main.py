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
        return 1
    th = title_hash_from_string(title)
    ch = category_hash_from_string(category)
    store = WishSpellStore()
    spell_id = store.list_spell(seller, th, ch, price, block=1000)
    print("Simulated list: spellId=%s titleHash=%s categoryHash=%s priceWei=%s" % (
        spell_id, bytes32_to_hex(th), bytes32_to_hex(ch), price
    ))
    return 0


# -----------------------------------------------------------------------------
# CLI: config
# -----------------------------------------------------------------------------

def cmd_config(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if args.set_contract:
        cfg.contract_address = args.set_contract
        save_config(cfg, args.config)
        print("Contract address set to", cfg.contract_address)
        return 0
    if args.set_rpc:
        cfg.rpc_url = args.set_rpc
        save_config(cfg, args.config)
        print("RPC URL set")
        return 0
    print("Contract:", cfg.contract_address or "(not set)")
    print("RPC URL:", cfg.rpc_url or "(not set)")
    print("Chain ID:", cfg.chain_id)
    print("Fee bps:", cfg.fee_bps)
    return 0


# -----------------------------------------------------------------------------
# CLI: constants
# -----------------------------------------------------------------------------

def cmd_constants(args: argparse.Namespace) -> int:
    print("SPEL_BPS_BASE", SPEL_BPS_BASE)
    print("SPEL_MAX_FEE_BPS", SPEL_MAX_FEE_BPS)
    print("SPEL_MAX_SPELLS", SPEL_MAX_SPELLS)
    print("SPEL_MAX_BATCH_LIST", SPEL_MAX_BATCH_LIST)
    print("SPEL_MAX_BATCH_DELIST", SPEL_MAX_BATCH_DELIST)
    print("SPEL_PLATFORM_SALT", hex(SPEL_PLATFORM_SALT))
    print("VAULT", VAULT_ADDRESS)
    print("TREASURY", TREASURY_ADDRESS)
    print("KEEPER", KEEPER_ADDRESS)
    return 0


# -----------------------------------------------------------------------------
# Batch fee calculator
# -----------------------------------------------------------------------------

def cmd_batch_fee(args: argparse.Namespace) -> int:
    prices = [int(x) for x in args.prices.split(",")]
    fee_bps = int(args.fee_bps) if args.fee_bps else 12
    if fee_bps < 0 or fee_bps > SPEL_MAX_FEE_BPS:
        print("Invalid fee_bps", file=sys.stderr)
        return 1
    total_fee = 0
    total_to_seller = 0
    for p in prices:
        if p < 0:
            continue
        f = compute_fee_wei(p, fee_bps)
        s = compute_seller_receives(p, fee_bps)
        total_fee += f
        total_to_seller += s
        print("Price %s wei -> fee %s, to seller %s" % (p, f, s))
    print("Total fee: %s wei" % total_fee)
    print("Total to seller: %s wei" % total_to_seller)
    return 0


# -----------------------------------------------------------------------------
# Validate address (simple hex length)
# -----------------------------------------------------------------------------

def is_valid_address(addr: str) -> bool:
    if not addr or not addr.startswith(HEX_PREFIX):
        return False
    raw = addr[2:].strip()
    return len(raw) == 40 and all(c in "0123456789abcdefABCDEF" for c in raw)


def cmd_validate_address(args: argparse.Namespace) -> int:
    addr = args.address
    if is_valid_address(addr):
        print("Valid 40-char hex address")
        return 0
    print("Invalid address", file=sys.stderr)
    return 1


# -----------------------------------------------------------------------------
# Simulate buy (local store)
# -----------------------------------------------------------------------------

def cmd_simulate_buy(args: argparse.Namespace) -> int:
    store = WishSpellStore()
    seller = args.seller or "0x1111111111111111111111111111111111111111"
    th = title_hash_from_string(args.title)
    ch = category_hash_from_string(args.category)
    price = int(args.price)
    spell_id = store.list_spell(seller, th, ch, price, block=1000)
    store.delist(spell_id)
    fee = compute_fee_wei(price, int(args.fee_bps or "12"))
    to_seller = price - fee
    print("Simulated buy: spellId=%s price=%s fee=%s toSeller=%s" % (spell_id, price, fee, to_seller))
    return 0


# -----------------------------------------------------------------------------
# Main CLI
# -----------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Wish — Spella spell-book trading helper")
    parser.add_argument("--config", default="", help="Path to wish_config.json")
    sub = parser.add_subparsers(dest="command", help="Commands")

    p_fee = sub.add_parser("fee", help="Compute fee and seller receives for a price")
    p_fee.add_argument("price", help="Price in wei")
    p_fee.add_argument("--fee-bps", default="12", help="Fee in basis points")
    p_fee.set_defaults(func=cmd_fee)

    p_hash = sub.add_parser("hash", help="Hash a string to bytes32 (title or category)")
    p_hash.add_argument("string", help="String to hash")
    p_hash.add_argument("--kind", choices=["title", "category"], default="title")
    p_hash.set_defaults(func=cmd_hash)

    p_sim = sub.add_parser("simulate-list", help="Simulate listing a spell")
    p_sim.add_argument("title", help="Spell title string")
    p_sim.add_argument("category", help="Category string")
    p_sim.add_argument("price", help="Price in wei")
    p_sim.add_argument("--seller", default="", help="Seller address")
    p_sim.set_defaults(func=cmd_simulate_list)

    p_cfg = sub.add_parser("config", help="Show or set config")
    p_cfg.add_argument("--set-contract", metavar="ADDR", help="Set contract address")
    p_cfg.add_argument("--set-rpc", metavar="URL", help="Set RPC URL")
    p_cfg.set_defaults(func=cmd_config)
