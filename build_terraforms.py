#!/usr/bin/env python3
"""
build_terraforms.py

One-step pipeline: pull Terraforms on-chain data via Alchemy and pack it
DIRECTLY into the two files your viewer reads:

    terraformsData.bin          (packed binary, byte format identical to packTerraforms.py)
    terraformsData_index.json   (lightweight index)

No intermediate terraformsData.json is needed.

The Alchemy key is read from the ALCHEMY_KEY environment variable (never hard-code it).

Usage:
    ALCHEMY_KEY=xxxx python build_terraforms.py --output data/terraformsData.bin
    ALCHEMY_KEY=xxxx MAX_TOKENS=5 python build_terraforms.py      # quick test
"""

import os, json, struct, time, base64, argparse, threading, random
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from web3 import Web3
from eth_abi import encode as abi_encode

# ── Config ──────────────────────────────────────────────────────────────────
ALCHEMY_KEY  = os.environ.get("ALCHEMY_KEY", "").strip()
if not ALCHEMY_KEY:
    raise SystemExit("ALCHEMY_KEY environment variable is not set.")

RPC_URL      = f"https://eth-mainnet.g.alchemy.com/v2/{ALCHEMY_KEY}"
MAIN_ADDRESS = "0x4E1f41613c9084FdB9E34E11fAE9412427480e56"
DATA_ADDRESS = "0x8aF860C8F157F4E3B6A54913BFA6Bb96ab2605C2"
GLOBAL_SEED  = 10196

# Minimal ABIs — only the read functions this script actually calls.
MAIN_ABI = json.dumps([
    {"inputs":[],"name":"totalSupply","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"tokenId","type":"uint256"}],"name":"tokenSupplementalData","outputs":[{"components":[
        {"name":"tokenId","type":"uint256"},{"name":"level","type":"uint256"},
        {"name":"xCoordinate","type":"uint256"},{"name":"yCoordinate","type":"uint256"},
        {"name":"elevation","type":"int256"},{"name":"structureSpaceX","type":"int256"},
        {"name":"structureSpaceY","type":"int256"},{"name":"structureSpaceZ","type":"int256"},
        {"name":"zoneName","type":"string"},{"name":"zoneColors","type":"string[10]"},
        {"name":"characterSet","type":"string[9]"}],"name":"result","type":"tuple"}],
        "stateMutability":"view","type":"function"},
    {"inputs":[{"name":"","type":"uint256"}],"name":"tokenToStatus","outputs":[{"type":"uint8"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"","type":"uint256"}],"name":"tokenToPlacement","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"tokenId","type":"uint256"}],"name":"tokenHeightmapIndices","outputs":[{"type":"uint256[32][32]"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"tokenId","type":"uint256"}],"name":"tokenTerrainValues","outputs":[{"type":"int256[32][32]"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"tokenId","type":"uint256"}],"name":"tokenCharacters","outputs":[{"type":"string[32][32]"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"tokenId","type":"uint256"}],"name":"tokenURI","outputs":[{"name":"result","type":"string"}],"stateMutability":"view","type":"function"},
])

DATA_ABI = json.dumps([
    {"inputs":[{"name":"tokenId","type":"uint256"}],"name":"resourceLevel","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"placement","type":"uint256"},{"name":"seed","type":"uint256"}],"name":"levelAndTile","outputs":[{"name":"level","type":"uint256"},{"name":"tile","type":"uint256"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"tokenId","type":"uint256"}],"name":"chroma","outputs":[{"name":"result","type":"string"}],"stateMutability":"view","type":"function"},
])

# ── Per-thread web3 (HTTPProvider is not guaranteed thread-safe) ──────────────
_local = threading.local()
def _contracts():
    if not hasattr(_local, "main"):
        w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 30}))
        _local.main = w3.eth.contract(address=Web3.to_checksum_address(MAIN_ADDRESS), abi=MAIN_ABI)
        _local.data = w3.eth.contract(address=Web3.to_checksum_address(DATA_ADDRESS), abi=DATA_ABI)
    return _local.main, _local.data

class _RateLimiter:
    """Global throttle shared by all worker threads: caps aggregate requests/sec
    so we stay under Alchemy's compute-units-per-second limit. Set via RPS env."""
    def __init__(self, rps):
        self.interval = (1.0 / rps) if rps and rps > 0 else 0.0
        self.lock = threading.Lock()
        self.next = 0.0
    def wait(self):
        if self.interval <= 0:
            return
        with self.lock:
            now = time.monotonic()
            t = max(now, self.next)
            self.next = t + self.interval
            delay = t - now
        if delay > 0:
            time.sleep(delay)

# Default ~12 req/s ≈ the free-tier ceiling (eth_call ~26 CU, ~330 CU/s). Raise on a paid tier.
RATE = _RateLimiter(float(os.environ.get("RPS", "12") or 0))

def _retry(fn, tries=8, base=0.8, cap=30.0):
    """Throttled RPC call that rides out rate limits: honors a 429 Retry-After
    header when present, otherwise backs off exponentially with jitter."""
    for i in range(tries):
        RATE.wait()
        try:
            return fn()
        except Exception as e:
            if i == tries - 1:
                raise
            wait = None
            resp = getattr(e, "response", None)
            if resp is not None and getattr(resp, "status_code", None) == 429:
                ra = getattr(resp, "headers", {}) or {}
                ra = ra.get("Retry-After")
                if ra:
                    try:
                        wait = float(ra)
                    except (TypeError, ValueError):
                        wait = None
            if wait is None:
                wait = min(cap, base * (2 ** i)) + random.uniform(0, 0.5)
            time.sleep(wait)

# ── Helpers (unchanged logic from terraformsData.py) ──────────────────────────
def compute_seed(level: int, tile: int) -> int:
    packed = abi_encode(["uint256", "uint256"], [level, tile])
    return int(Web3.keccak(packed).hex(), 16) % 10_000

def parse_token_uri(uri: str) -> dict:
    payload = uri.split(",", 1)[1] if "," in uri else uri
    meta = json.loads(base64.b64decode(payload).decode("utf-8"))
    attrs = {a.get("trait_type"): a.get("value") for a in meta.get("attributes", [])}
    return {
        "version": attrs.get("Version", 0),
        "antenna": attrs.get("Antenna") == "On",
        "biome":   attrs.get("Biome"),
    }

def fetch_token(token_id: int) -> dict:
    main, data = _contracts()
    d     = _retry(lambda: main.functions.tokenSupplementalData(token_id).call())
    stat  = _retry(lambda: main.functions.tokenToStatus(token_id).call())
    plac  = _retry(lambda: main.functions.tokenToPlacement(token_id).call())
    hmap  = _retry(lambda: main.functions.tokenHeightmapIndices(token_id).call())
    terr  = _retry(lambda: main.functions.tokenTerrainValues(token_id).call())
    char  = _retry(lambda: main.functions.tokenCharacters(token_id).call())

    level    = d[1] - 1
    resource = _retry(lambda: data.functions.resourceLevel(token_id).call())
    _, tile  = _retry(lambda: data.functions.levelAndTile(plac, GLOBAL_SEED).call())
    chroma   = _retry(lambda: data.functions.chroma(token_id).call())
    seed     = compute_seed(level, tile)
    uri      = parse_token_uri(_retry(lambda: main.functions.tokenURI(token_id).call()))

    return {
        "tokenId": token_id, "level": level,
        "xCoordinate": d[2], "yCoordinate": d[3], "elevation": d[4],
        "structureSpaceX": d[5], "structureSpaceY": d[6], "structureSpaceZ": d[7],
        "zoneName": d[8], "zoneColors": list(d[9]), "characterSet": list(d[10]),
        "status": stat, "placement": plac, "tile": tile, "seed": seed,
        "resource": resource, "chroma": chroma,
        "version": uri["version"], "antenna": uri["antenna"], "biomeValue": uri["biome"],
        "heightmapIndices": [list(r) for r in hmap],
        "terrainValues":    [list(r) for r in terr],
        "characters":       [list(r) for r in char],
    }

# ── Packing (byte-for-byte identical to packTerraforms.py) ────────────────────
def pack_string(s: str) -> bytes:
    enc = s.encode("utf-8")
    return struct.pack("B", len(enc)) + enc

def pack_token(t: dict) -> bytes:
    buf = bytearray()
    buf += struct.pack("<IIIIiiiiiIIII",
        t["tokenId"], t["level"], t["xCoordinate"], t["yCoordinate"], t["elevation"],
        t["structureSpaceX"], t["structureSpaceY"], t["structureSpaceZ"],
        t["status"], t["placement"], t["tile"], t["seed"], t["resource"])
    buf += pack_string(t["zoneName"])
    buf += pack_string(t.get("chroma", ""))
    buf += pack_string(str(t.get("version", 0)))
    buf += pack_string(t.get("biomeValue") or "")
    buf += struct.pack("B", 1 if t.get("antenna") else 0)
    zc = t["zoneColors"]; buf += struct.pack("B", len(zc))
    for c in zc: buf += pack_string(c)
    cs = t["characterSet"]; buf += struct.pack("B", len(cs))
    for c in cs: buf += pack_string(c)
    for row in t["terrainValues"]:    buf += struct.pack(f"<{len(row)}i", *row)
    for row in t["heightmapIndices"]: buf += struct.pack(f"<{len(row)}H", *row)
    chars_flat  = [cell for row in t["characters"] for cell in row]
    unique      = list(dict.fromkeys(chars_flat))
    char_to_idx = {c: i for i, c in enumerate(unique)}
    buf += struct.pack("<H", len(unique))
    for c in unique: buf += pack_string(c)
    buf += bytes(char_to_idx[c] for c in chars_flat)
    return bytes(buf)

def index_entry(t: dict) -> dict:
    """The small scalar record that goes in the index (offset/length added later)."""
    return {
        "tokenId": t["tokenId"], "level": t["level"],
        "x": t["xCoordinate"], "y": t["yCoordinate"], "elevation": t["elevation"],
        "zoneName": t["zoneName"], "status": t["status"], "placement": t["placement"],
        "tile": t["tile"], "seed": t["seed"], "resource": t["resource"],
        "chroma": t.get("chroma", ""), "version": str(t.get("version", 0)),
        "antenna": bool(t.get("antenna")), "biomeValue": t.get("biomeValue") or "",
        "zoneColors": t["zoneColors"],
    }

def build_one(token_id: int):
    t = fetch_token(token_id)
    # pack now and keep only the small index record, so memory stays ~bin-sized
    return pack_token(t), index_entry(t)

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default="data/terraformsData.bin")
    ap.add_argument("--max-tokens", type=int, default=int(os.environ.get("MAX_TOKENS", "0") or 0),
                    help="0 = all tokens (read from totalSupply); >0 limits for testing")
    ap.add_argument("--workers", type=int, default=int(os.environ.get("WORKERS", "8") or 8))
    args = ap.parse_args()

    main_c, _ = _contracts()
    total = args.max_tokens or _retry(lambda: main_c.functions.totalSupply().call())
    print(f"Fetching tokens 1..{total} with {args.workers} workers...")

    results, done = {}, 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(build_one, tid): tid for tid in range(1, total + 1)}
        for fut in as_completed(futs):
            tid = futs[fut]
            try:
                results[tid] = fut.result()          # (blob, index_entry)
            except Exception as e:
                print(f"  token {tid} failed: {e}")
            done += 1
            if done % 250 == 0 or done == total:
                print(f"  {done}/{total}")

    bin_path = Path(args.output)
    bin_path.parent.mkdir(parents=True, exist_ok=True)
    idx_path = bin_path.with_name(bin_path.stem + "_index.json")

    index = []
    with open(bin_path, "wb") as out:
        for tid in sorted(results):
            blob, entry = results[tid]
            entry = {"tokenId": entry["tokenId"], "offset": out.tell(),
                     "length": len(blob), **{k: v for k, v in entry.items() if k != "tokenId"}}
            out.write(blob)
            index.append(entry)

    with open(idx_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False)

    print(f"\nDone — {len(index)} tokens")
    print(f"  {bin_path}  {bin_path.stat().st_size/1_000_000:.1f} MB")
    print(f"  {idx_path}  {idx_path.stat().st_size/1_000_000:.1f} MB")

if __name__ == "__main__":
    main()
