# ▟╔㆔⍝m▟㆔✗

A single-file, in-browser renderer and explorer for **[Terraforms by Mathcastles](https://opensea.io/collection/terraforms)** — the fully on-chain land-art collection. DreamDex faithfully re-creates the contract's renderer (Terrain, Daydream, Terraformed, and Origin modes, both the v0 and v2 engines) in plain HTML/JS/SVG, adds a real-time 3D view, a daydream paint tool, and a fast way to browse every parcel in the collection.

> DreamDex is an independent, community-built tool. It is not affiliated with or endorsed by Mathcastles. All token data is read directly from the Terraforms smart contracts on Ethereum mainnet.

---

## Features

- **Every parcel, instantly.** Browse all ~9,910 Terraforms by token ID, with left/right navigation and wraparound.
- **Faithful 2D rendering.** Re-implements the on-chain SVG renderer including the v0 and v2 engines, zone color palettes, biome glyph sets, resource gating, chroma animation speed, and the seed-based font distortion.
- **Live 3D view.** A Three.js heightmap of the parcel you can orbit, zoom, and pan — on desktop (mouse + wheel) and mobile (one finger to orbit, two to pinch-zoom and pan).
- **Daydream paint tool.** The contract's round brush, ported from the on-chain code, for painting on Daydream parcels.
- **Deep-linking.** Open any parcel directly with a `?q=<tokenId>` URL.
- **Export.** Save the current parcel as a self-contained, standalone HTML file (2D or 3D).
- **Fullscreen** with a mobile-friendly fallback; the device back button exits.
- **Light / dark themes**, a collapsible control panel, art/heightmap presets, and editable biome glyphs and zone colors.
- **No build step, no dependencies to install.** One HTML file. Data is fetched on demand from a hosted dataset.

---

## Using it

Open the app HTML file in any modern browser, or host it (e.g. GitHub Pages) and visit the URL. It loads the parcel dataset automatically on startup.

### Deep-link to a parcel

Append a token ID to the URL:

```
.../dreamdex.html?q=5000
```

Out-of-range or invalid values fall back to token 1.

### Keyboard shortcuts

| Key   | Action              |
|-------|---------------------|
| `←` `→` | Previous / next parcel |
| `s`   | Toggle the drop shadow |
| `c`   | Change background color |
| `e`   | Export standalone HTML |
| `f`   | Toggle fullscreen   |

Shortcuts are ignored while typing in a field.

### Controls

The left panel exposes everything that drives the render: token ID, engine version (v0 / v2), mode (Terrain, Daydream, Terraformed, Origin variants), 2D / 3D dimension, zone color palette, the nine biome glyph tiers, and a set of heightmap/art presets. Hide the panel with the **❮** button; bring it back with the **❯** tab on the left edge.

---

## How the data works

Rendering every parcel from raw contract calls in the browser would be far too slow, so DreamDex reads from a pre-packed dataset:

- **`terraformsData.bin`** — every token packed into one compact binary file (~73 MB).
- **`terraformsData_index.json`** — a lightweight index mapping each `tokenId` to its byte `offset` + `length` in the bin, plus the scalar trait fields.

The dataset is hosted on a dedicated **`data`** branch of this repo and served via `raw.githubusercontent.com`, which supports HTTP range requests. When you open a parcel, DreamDex fetches **only that token's byte slice** from the bin — not the whole 73 MB — so navigation stays fast.

The app reads from:

```
https://raw.githubusercontent.com/weiword/dreamDex/refs/heads/data/terraformsData_index.json
https://raw.githubusercontent.com/weiword/dreamDex/refs/heads/data/terraformsData.bin
```

---

## Refreshing the data

A Terraform's terrain is deterministic from its seed, but parcels that get **daydreamed** or **terraformed** change over time. A scheduled GitHub Action keeps the dataset current.

### Pipeline

- **`build_terraforms.py`** — connects to Ethereum via Alchemy, reads each token from the Terraforms main + data contracts, and packs the result **directly** into `terraformsData.bin` + `terraformsData_index.json` in one pass. The token count is read from the contract's `totalSupply()` automatically.
- **`.github/workflows/refresh-data.yml`** — runs `build_terraforms.py` every Monday (06:00 UTC) and force-pushes the result to the `data` branch as a single parentless commit, so the branch never accumulates git history.

### One-time setup

1. Add your Alchemy key as a repository secret named `ALCHEMY_KEY`
   (**Settings → Secrets and variables → Actions → New repository secret**). Never commit the key.
2. Make sure `build_terraforms.py` and `.github/workflows/refresh-data.yml` are on the **default branch** (GitHub only schedules workflows from there).
3. From the **Actions** tab, run the workflow once manually to create the `data` branch.
   Tip: set the `max_tokens` input to `5` for a quick smoke test first, then run it again blank for the full ~9,910-token pull.

After that it runs weekly on its own. (Note: GitHub pauses cron schedules after ~60 days of repo inactivity — a manual run or any push re-arms it.)

### Run it locally

```bash
pip install "web3>=6" eth-abi
ALCHEMY_KEY=your_key python build_terraforms.py --output data/terraformsData.bin
# quick test: ALCHEMY_KEY=your_key MAX_TOKENS=5 python build_terraforms.py
```

---

## Repository layout

```
dreamdex.html                      # the app (single file)
build_terraforms.py                # fetch + pack pipeline
.github/workflows/refresh-data.yml # weekly data refresh
data branch:
  terraformsData.bin               # packed binary (all tokens)
  terraformsData_index.json        # token index
```

---

## Binary format

Each token is packed little-endian, back to back, in token order. The index gives the offset/length of each record. Per token:

- 13 × scalar ints (`<IIIIiiiiiIIII`): tokenId, level, x, y, elevation, structureSpaceX/Y/Z, status, placement, tile, seed, resource
- length-prefixed strings: zoneName, chroma, version, biomeValue
- 1 byte: antenna (1/0)
- zoneColors: count byte + length-prefixed strings
- characterSet: count byte + length-prefixed strings
- `1024 × int32` terrain values (row-major, 32 per row)
- `1024 × uint16` heightmap indices (row-major)
- characters grid: `uint16` unique-count + length-prefixed unique chars + `1024 × uint8` index array

This is produced by `build_terraforms.py` (and matches the original `packTerraforms.py`).

---

## Tech notes

- Pure client-side: HTML + vanilla JS + SVG for 2D, Three.js (r128) for 3D. No bundler, no framework.
- The renderer mirrors the contract's logic (heightmap tiers `a`–`i` plus a blank tier, 10-color zone palettes, 9-glyph biomes, brush kernels, font-size distortion for high seeds, etc.).
- Fetches use byte-range requests so each parcel pulls only a few KB.

## Credits

- **Terraforms** is created by **[Mathcastles](https://mathcastles.xyz/)**. All on-chain data and the original renderer logic are theirs; DreamDex re-implements the renderer for exploration and tooling.
- RPC access via [Alchemy](https://www.alchemy.com/).
- 3D via [Three.js](https://threejs.org/).
- Terraforms Explorer
- Terrafans
- Enter Dream
