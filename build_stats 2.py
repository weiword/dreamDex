#!/usr/bin/env python3
"""
build_stats.py  —  fetch live marketplace data from the OpenSea API (v2) and write:

  stats.json     general collection stats for the top bar
  listings.json  active listings, lowest price per token, with owner + created time
                 { updatedAt, count, items: { "<tokenId>": { price, currency, owner, created } } }
  sales.json     sales in the past N days (default 30), newest first
                 { updatedAt, windowDays, count, items: [ { token, price, currency, from, to, time } ] }
  owners.json    owner per token, pulled on-chain via ownerOf()
                 { updatedAt, count, items: { "<tokenId>": "0x<addr>" } }

Uses only the Python standard library (urllib) so the workflow needs no pip install.

Env: OPENSEA_API_KEY (req), COLLECTION_SLUG (default terraforms), CHAIN, MAX_PAGES,
     MAX_SALE_PAGES, SALES_DAYS (default 30), OUTDIR. Docs: https://docs.opensea.io/reference
Owner pull: RPC_URL (comma-separated list OK; rotates on failure),
     CONTRACT, TOKEN_MIN (1), TOKEN_MAX (9911), RPC_BATCH (50), RPC_SLEEP (0.25),
     MAX_PASSES (6), MIN_COVER (0.999 -> exit 2 if owner coverage falls below this).
"""

import os, sys, json, time, datetime, urllib.request, urllib.parse, urllib.error

API   = "https://api.opensea.io/api/v2"
SLUG  = os.environ.get("COLLECTION_SLUG", "terraforms").strip()
KEY   = os.environ.get("OPENSEA_API_KEY", "").strip()
CHAIN = os.environ.get("CHAIN", "ethereum").strip()
MAX_PAGES      = int(os.environ.get("MAX_PAGES", "100"))
MAX_SALE_PAGES = int(os.environ.get("MAX_SALE_PAGES", "80"))
SALES_DAYS     = int(os.environ.get("SALES_DAYS", "30"))
OUTDIR = os.environ.get("OUTDIR", ".").strip() or "."

if not KEY:
    print("ERROR: OPENSEA_API_KEY is not set", file=sys.stderr)
    sys.exit(1)


def api_get(path, params=None, tries=6):
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    last = None
    for attempt in range(tries):
        req = urllib.request.Request(url, headers={
            "accept": "application/json",
            "x-api-key": KEY,
            "User-Agent": os.environ.get("USER_AGENT",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"),
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429 or 500 <= e.code < 600:
                wait = float(e.headers.get("Retry-After", 0) or 0) or min(2 ** attempt, 30)
                time.sleep(wait); continue
            body = ""
            try: body = e.read().decode("utf-8")[:300]
            except Exception: pass
            raise RuntimeError(f"HTTP {e.code} for {url} :: {body}") from e
        except urllib.error.URLError as e:
            last = e; time.sleep(min(2 ** attempt, 30))
    raise RuntimeError(f"request failed after {tries} tries: {url} :: {last}")


def _to_unix(t):
    if t is None: return None
    if isinstance(t, (int, float)): return int(t)
    s = str(t).strip()
    if s.isdigit(): return int(s)
    try:
        return int(datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def fetch_stats():
    d = api_get(f"/collections/{SLUG}/stats")
    total = d.get("total", {}) or {}
    return {
        "collectors": total.get("num_owners"),
        "floor": total.get("floor_price"),
        "floorSymbol": total.get("floor_price_symbol") or "ETH",
        "volume": total.get("volume"),
        "sales": total.get("sales"),
        "marketCap": total.get("market_cap"),
    }


def fetch_total_supply():
    try:
        d = api_get(f"/collections/{SLUG}")
        for k in ("total_supply", "totalSupply", "supply"):
            if isinstance(d.get(k), (int, float)): return int(d[k])
    except Exception as e:
        print(f"warn: total supply lookup failed: {e}", file=sys.stderr)
    return None


def _parse_listing(listing):
    price = None; cur = None
    p = (listing.get("price") or {}).get("current") or {}
    if p.get("value") is not None:
        try:
            price = int(p["value"]) / (10 ** int(p.get("decimals", 18)))
            cur = p.get("currency") or "ETH"
        except Exception:
            price = None
    params = (listing.get("protocol_data") or {}).get("parameters") or {}
    tok = None
    for item in (params.get("offer") or []):
        if item.get("itemType") in (2, 3) and item.get("identifierOrCriteria") not in (None, ""):
            tok = str(item["identifierOrCriteria"]); break
    return tok, price, cur, params.get("offerer"), _to_unix(params.get("startTime"))


def fetch_listings():
    items = {}
    cursor = None; pages = 0
    while pages < MAX_PAGES:
        params = {"limit": 100}
        if cursor: params["next"] = cursor
        page = api_get(f"/listings/collection/{SLUG}/all", params)
        for lst in (page.get("listings") or []):
            tok, price, cur, owner, created = _parse_listing(lst)
            if tok is None or price is None: continue
            prev = items.get(tok)
            if prev is None or price < prev["price"]:
                items[tok] = {"price": round(price, 6), "currency": cur, "owner": owner, "created": created}
        cursor = page.get("next"); pages += 1
        if not cursor: break
    return items


def fetch_sales(days=30):
    after = int(time.time()) - days * 86400
    out = []
    cursor = None; pages = 0
    while pages < MAX_SALE_PAGES:
        params = {"event_type": "sale", "after": after, "limit": 50}
        if cursor: params["next"] = cursor
        page = api_get(f"/events/collection/{SLUG}", params)
        for ev in (page.get("asset_events") or page.get("events") or []):
            nft = ev.get("nft") or ev.get("asset") or {}
            tok = nft.get("identifier") or nft.get("token_id")
            if tok is None: continue
            pay = ev.get("payment") or {}
            price = None; cur = None
            try:
                qty = pay.get("quantity")
                if qty is not None:
                    price = int(qty) / (10 ** int(pay.get("decimals", 18)))
                    cur = pay.get("symbol") or "ETH"
            except Exception:
                price = None
            out.append({
                "token": str(tok),
                "price": round(price, 6) if price is not None else None,
                "currency": cur,
                "from": ev.get("seller") or ev.get("from_address"),
                "to": ev.get("buyer") or ev.get("to_address"),
                "time": _to_unix(ev.get("closing_date") or ev.get("event_timestamp")),
                "tx": (ev.get("transaction") if isinstance(ev.get("transaction"), str)
                       else (ev.get("transaction") or {}).get("hash")
                       if isinstance(ev.get("transaction"), dict)
                       else ev.get("transaction_hash")),
            })
        cursor = page.get("next"); pages += 1
        if not cursor: break
    out.sort(key=lambda x: (x["time"] or 0), reverse=True)
    return out


# ── on-chain owner pull ────────────────────────────────────────────────────
# The previous version sent one 400-call JSON-RPC batch and, on any failure,
# dropped the WHOLE batch (silent `return []`). A free public node throttles at
# a fixed cadence, so the same ~8 batches (3,200 tokens) vanished every run.
# This version sends small batches and, on a unit failure, splits the batch in
# half recursively down to single calls — throttling only slows the run, it can
# never drop a batch. A multi-pass sweep retries whatever is still missing.

SELECTOR = "0x6352211e"                 # ownerOf(uint256)
ZERO_ADDR = "0x" + "0" * 40
RPC_URLS = [u.strip() for u in os.environ.get(
    "RPC_URL", "https://ethereum-rpc.publicnode.com").split(",") if u.strip()]
_rpc_idx = 0


def _rpc_post(calls, tries=6):
    """POST one JSON-RPC batch. Returns (ok, list|None).

    ok is False only on a UNIT failure (transport error, HTTP error, or the node
    rejecting the whole batch with a lone error object) — the caller then splits
    and retries. Honors Retry-After on 429 and rotates through RPC_URLS.
    """
    global _rpc_idx
    body = json.dumps(calls).encode("utf-8")
    for attempt in range(tries):
        url = RPC_URLS[_rpc_idx % len(RPC_URLS)]
        req = urllib.request.Request(url, data=body, headers={
            "content-type": "application/json",
            "accept": "application/json",
            "User-Agent": os.environ.get("USER_AGENT", "dreamdex-stats/1.1"),
        })
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                out = json.loads(r.read().decode("utf-8"))
                if isinstance(out, dict):           # node rejected the batch as a unit
                    if "error" in out:
                        raise RuntimeError(str(out.get("error"))[:200])
                    out = [out]
                return True, out
        except urllib.error.HTTPError as e:
            wait = float(e.headers.get("Retry-After", 0) or 0) or min(2 ** attempt, 20)
            time.sleep(wait); _rpc_idx += 1
        except Exception:
            time.sleep(min(2 ** attempt, 20)); _rpc_idx += 1
    return False, None


def _owner_calls(ids, contract):
    return [{
        "jsonrpc": "2.0", "id": t, "method": "eth_call",
        "params": [{"to": contract, "data": SELECTOR + format(t, "064x")}, "latest"],
    } for t in ids]


def _decode_owners(res):
    got = {}
    for item in (res or []):
        tid = item.get("id"); r = item.get("result")
        if tid is None or not r or len(r) < 66:        # reverted / nonexistent token
            continue
        addr = "0x" + r[-40:]
        if addr != ZERO_ADDR:
            got[int(tid)] = addr
    return got


def _resolve_ids(ids, contract, batch, sleep):
    """Resolve ids to {tokenId:addr}; split any failed batch so none is dropped."""
    out = {}

    def _chunk(c):
        ok, res = _rpc_post(_owner_calls(c, contract))
        if ok:
            return _decode_owners(res)               # reverts left out; retried next pass
        if len(c) == 1:
            return {}                                # single call still failing -> skip for now
        mid = len(c) // 2
        g = _chunk(c[:mid]); g.update(_chunk(c[mid:]))
        return g

    for i in range(0, len(ids), batch):
        out.update(_chunk(ids[i:i + batch]))
        time.sleep(sleep)
    return out


def _load_owners(path):
    try:
        d = json.load(open(path))
        items = d.get("items", {}) if isinstance(d, dict) else {}
        return {int(k): v for k, v in items.items() if v}
    except Exception:
        return {}


def fetch_owners():
    """Owner per token via adaptive, self-healing on-chain ownerOf() calls.

    Returns (owners_dict_str_keyed, expected_total). Ownership is re-fetched
    fresh each run (it changes with sales/transfers); any token that still can't
    be reached after all passes is backfilled from the previous owners.json so a
    throttled run never makes a token — or its collector — vanish.
    """
    contract = os.environ.get("CONTRACT", "0x4e1f41613c9084fdb9e34e11fae9412427480e56").strip()
    tok_min  = int(os.environ.get("TOKEN_MIN", "1"))
    tok_max  = int(os.environ.get("TOKEN_MAX", "9911"))   # was 9910 -> dropped the last token
    batch    = int(os.environ.get("RPC_BATCH", "50"))     # was 400 -> oversized batches tripped throttling
    sleep    = float(os.environ.get("RPC_SLEEP", "0.25"))
    passes   = int(os.environ.get("MAX_PASSES", "6"))

    all_ids = list(range(tok_min, tok_max + 1))
    owners = {}
    for p in range(passes):
        todo = [t for t in all_ids if t not in owners]
        if not todo:
            break
        before = len(owners)
        owners.update(_resolve_ids(todo, contract, batch, sleep))
        gained = len(owners) - before
        print(f"owners pass {p + 1}: +{gained} (total {len(owners)}/{len(all_ids)})", file=sys.stderr)
        if gained == 0:                                   # remainder are nonexistent/reverting
            break

    # backfill unreachable tokens from the previous file (never vanish on throttle)
    prev = _load_owners(os.path.join(OUTDIR, "owners.json"))
    filled = 0
    for t in all_ids:
        if t not in owners and t in prev:
            owners[t] = prev[t]; filled += 1
    if filled:
        print(f"owners: backfilled {filled} unreachable tokens from previous file", file=sys.stderr)

    return {str(k): owners[k] for k in sorted(owners)}, len(all_ids)


def main():
    now = int(time.time())
    stats = fetch_stats()
    stats["totalSupply"] = fetch_total_supply()

    listed = None; listings = {}
    try: listings = fetch_listings(); listed = len(listings)
    except Exception as e: print(f"warn: listings fetch failed: {e}", file=sys.stderr)

    sales = []
    try: sales = fetch_sales(SALES_DAYS)
    except Exception as e: print(f"warn: sales fetch failed: {e}", file=sys.stderr)

    owners = {}; expected = None
    try: owners, expected = fetch_owners()
    except Exception as e: print(f"warn: owners fetch failed: {e}", file=sys.stderr)

    # coverage guard: never overwrite a good owners.json with a shrunken pull
    prev = _load_owners(os.path.join(OUTDIR, "owners.json"))
    if len(owners) < len(prev):
        print(f"warn: pull produced {len(owners)} owners < existing {len(prev)}; "
              f"keeping existing file", file=sys.stderr)
        owners = {str(k): v for k, v in sorted(prev.items())}

    stats_out = {
        "updatedAt": now, "slug": SLUG, "chain": CHAIN,
        "collectors": stats["collectors"], "floor": stats["floor"],
        "floorSymbol": stats["floorSymbol"], "listed": listed,
        "totalSupply": stats["totalSupply"], "volume": stats["volume"],
        "sales": stats["sales"], "marketCap": stats["marketCap"],
    }
    listings_out = {"updatedAt": now, "count": len(listings), "items": listings}
    sales_out = {"updatedAt": now, "windowDays": SALES_DAYS, "count": len(sales), "items": sales}
    owners_out = {"updatedAt": now, "count": len(owners), "items": owners}

    os.makedirs(OUTDIR, exist_ok=True)
    for name, data in (("stats.json", stats_out), ("listings.json", listings_out),
                       ("sales.json", sales_out), ("owners.json", owners_out)):
        with open(os.path.join(OUTDIR, name), "w") as f:
            json.dump(data, f, separators=(",", ":"))

    print("stats.json ->", json.dumps(stats_out))
    print(f"listings.json -> {len(listings)} listed")
    print(f"sales.json -> {len(sales)} sales in {SALES_DAYS}d")
    print(f"owners.json -> {len(owners)} owners")

    # flag (but don't erase) a short pull so CI surfaces it
    if expected:
        cover = len(owners) / expected if expected else 1.0
        min_cover = float(os.environ.get("MIN_COVER", "0.999"))
        print(f"owners coverage -> {len(owners)}/{expected} = {cover:.4%}")
        if cover < min_cover:
            print(f"ERROR: owner coverage {cover:.4%} below MIN_COVER {min_cover:.4%}", file=sys.stderr)
            sys.exit(2)


if __name__ == "__main__":
    main()
