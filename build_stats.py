#!/usr/bin/env python3
"""
build_stats.py  —  fetch live marketplace data from the OpenSea API (v2) and write:

  stats.json     general collection stats for the top bar
                 { updatedAt, collectors, floor, floorSymbol, listed, totalSupply, volume, sales }
  listings.json  per-parcel active listings (lowest price per token)
                 { updatedAt, count, items: { "<tokenId>": { price, currency } } }

Runs from GitHub Actions on a short schedule. Uses only the Python standard library
(urllib) so the workflow needs no `pip install` step.

Env:
  OPENSEA_API_KEY   required — free key from https://docs.opensea.io/reference/api-keys
  COLLECTION_SLUG   OpenSea collection slug (default "terraforms" — VERIFY this for your collection)
  CHAIN             default "ethereum"
  MAX_PAGES         safety cap on listing pages (default 100 -> up to 10k listings)
  OUTDIR            where to write the json (default ".")

NOTE: OpenSea's response field names occasionally shift. Parsing below is defensive
(uses .get with fallbacks); if a number comes back null, check the field names against
the current docs at https://docs.opensea.io/reference .
"""

import os, sys, json, time, urllib.request, urllib.parse, urllib.error

API   = "https://api.opensea.io/api/v2"
SLUG  = os.environ.get("COLLECTION_SLUG", "terraforms").strip()
KEY   = os.environ.get("OPENSEA_API_KEY", "").strip()
CHAIN = os.environ.get("CHAIN", "ethereum").strip()
MAX_PAGES = int(os.environ.get("MAX_PAGES", "100"))
OUTDIR = os.environ.get("OUTDIR", ".").strip() or "."

if not KEY:
    print("ERROR: OPENSEA_API_KEY is not set", file=sys.stderr)
    sys.exit(1)


def api_get(path, params=None, tries=6):
    """GET {API}{path} with the api key header; retry on 429/5xx with backoff."""
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    last = None
    for attempt in range(tries):
        req = urllib.request.Request(url, headers={
            "accept": "application/json",
            "x-api-key": KEY,
            # OpenSea sits behind Cloudflare, which blocks the default urllib
            # User-Agent (error 1010). Send a normal browser UA.
            "User-Agent": os.environ.get("USER_AGENT",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"),
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = e
            # 429 = rate limited (honor Retry-After); 5xx = transient
            if e.code == 429 or 500 <= e.code < 600:
                wait = float(e.headers.get("Retry-After", 0) or 0) or min(2 ** attempt, 30)
                time.sleep(wait)
                continue
            # 4xx other than 429 -> not retryable
            body = ""
            try: body = e.read().decode("utf-8")[:300]
            except Exception: pass
            raise RuntimeError(f"HTTP {e.code} for {url} :: {body}") from e
        except urllib.error.URLError as e:
            last = e
            time.sleep(min(2 ** attempt, 30))
    raise RuntimeError(f"request failed after {tries} tries: {url} :: {last}")


def fetch_stats():
    """Collection-level stats: collectors, floor, volume, sales."""
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
    """Best-effort total supply from the collection endpoint (may be absent)."""
    try:
        d = api_get(f"/collections/{SLUG}")
        # try a few likely field names
        for k in ("total_supply", "totalSupply", "supply"):
            if isinstance(d.get(k), (int, float)):
                return int(d[k])
    except Exception as e:
        print(f"warn: total supply lookup failed: {e}", file=sys.stderr)
    return None


def _listing_token_and_price(listing):
    """Pull (tokenId, priceEth, currency) out of a v2 listing (Seaport format)."""
    # price
    price = None; cur = None
    p = (listing.get("price") or {}).get("current") or {}
    if p.get("value") is not None:
        try:
            price = int(p["value"]) / (10 ** int(p.get("decimals", 18)))
            cur = p.get("currency") or "ETH"
        except Exception:
            price = None
    # token id: first NFT item in the Seaport offer (itemType 2=ERC721, 3=ERC1155)
    tok = None
    params = (listing.get("protocol_data") or {}).get("parameters") or {}
    for item in (params.get("offer") or []):
        if item.get("itemType") in (2, 3) and item.get("identifierOrCriteria") not in (None, ""):
            tok = str(item["identifierOrCriteria"])
            break
    return tok, price, cur


def fetch_listings():
    """Paginate all active listings; keep the lowest price per token."""
    items = {}
    cursor = None
    pages = 0
    while pages < MAX_PAGES:
        params = {"limit": 100}
        if cursor:
            params["next"] = cursor
        page = api_get(f"/listings/collection/{SLUG}/all", params)
        for lst in (page.get("listings") or []):
            tok, price, cur = _listing_token_and_price(lst)
            if tok is None or price is None:
                continue
            prev = items.get(tok)
            if prev is None or price < prev["price"]:
                items[tok] = {"price": round(price, 6), "currency": cur}
        cursor = page.get("next")
        pages += 1
        if not cursor:
            break
    return items


def main():
    now = int(time.time())

    stats = fetch_stats()
    stats["totalSupply"] = fetch_total_supply()

    listed = None
    items = {}
    try:
        items = fetch_listings()
        listed = len(items)
    except Exception as e:
        print(f"warn: listings fetch failed (listed count unavailable): {e}", file=sys.stderr)

    stats_out = {
        "updatedAt": now,
        "slug": SLUG,
        "chain": CHAIN,
        "collectors": stats["collectors"],
        "floor": stats["floor"],
        "floorSymbol": stats["floorSymbol"],
        "listed": listed,
        "totalSupply": stats["totalSupply"],
        "volume": stats["volume"],
        "sales": stats["sales"],
        "marketCap": stats["marketCap"],
    }
    listings_out = {"updatedAt": now, "count": len(items), "items": items}

    os.makedirs(OUTDIR, exist_ok=True)
    with open(os.path.join(OUTDIR, "stats.json"), "w") as f:
        json.dump(stats_out, f, separators=(",", ":"))
    with open(os.path.join(OUTDIR, "listings.json"), "w") as f:
        json.dump(listings_out, f, separators=(",", ":"))

    print("stats.json ->", json.dumps(stats_out))
    print(f"listings.json -> {len(items)} listed")


if __name__ == "__main__":
    main()
