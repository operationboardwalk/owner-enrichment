# Owner Enrichment & Outreach Toolkit

Two complementary tools for a multifamily owner-outreach workflow:

1. **`enrich_owners.py`** — enriches a property owner Excel list with contact
   information (RocketReach, LinkedIn public profiles, public records).
2. **`generate_bov.py`** — turns a per-property JSON into a polished, **interactive,
   self-contained HTML Broker Opinion of Value (BOV)** you can email to the owner.

Typical flow: enrich the owner list → build a tailored BOV per property → send it.

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure your API key

Open `enrich_owners.py` and fill in your key at the top of the file:

```python
# ---- API KEYS — FILL IN BEFORE RUNNING ----
ROCKETREACH_API_KEY = "your_key_here"
# -------------------------------------------
```

---

## Input File Format

The script accepts any `.xlsx` file where each row represents a property.
It will auto-detect the following columns (case-insensitive partial match):

| Purpose          | Common column names accepted                          |
|------------------|-------------------------------------------------------|
| Owner Name       | `owner`, `name`, `owner name`, `property owner`       |
| Property Address | `address`, `property address`, `site address`, `street` |
| City             | `city`, `municipality`, `town`                        |
| State            | `state`, `st`, `province`                             |

If a column cannot be auto-detected, you will be prompted to enter the column
name manually at runtime.

---

## Output Columns

These columns are appended to the original file in `[filename]_enriched.xlsx`:

| Column                 | Description                                                           |
|------------------------|-----------------------------------------------------------------------|
| `Email`                | Best available email (work preferred)                                 |
| `Phone`                | Best available phone number                                           |
| `LinkedIn_URL`         | LinkedIn profile URL                                                  |
| `LinkedIn_Verified`    | `True`, `Partial`, `False`, or `N/A` (entities)                       |
| `Public_Records_Match` | `True` / `False` — name found in public property or SOS records       |
| `LLC_Registered_Agent` | Registered agent name (ENTITY owners only)                            |
| `Owner_Type`           | `INDIVIDUAL` or `ENTITY`                                              |
| `Source`               | Where contact info was found                                          |
| `Confidence_Score`     | 0–3 (see below)                                                       |
| `Notes`                | Warnings, mismatches, or manual review flags                          |

### Confidence Score

| Score | Meaning                                             | Row Color |
|-------|-----------------------------------------------------|-----------|
| 3     | RocketReach + LinkedIn verified + public records    | Green     |
| 2     | RocketReach + at least one verification             | —         |
| 1     | RocketReach only, or web fallback                   | —         |
| 0     | Nothing found                                       | Red       |

---

## CLI Options

```
python enrich_owners.py --input <file.xlsx> [options]
```

| Flag                    | Description                                                   |
|-------------------------|---------------------------------------------------------------|
| `--input FILE`          | **(Required)** Path to the input `.xlsx` file                 |
| `--owner-col NAME`      | Manually specify the owner name column                        |
| `--address-col NAME`    | Manually specify the address column                           |
| `--state XX`            | Default state for SOS/public records lookup (default: `CA`)   |
| `--dry-run`             | Print the lookup plan without making any API calls            |
| `--skip-linkedin`       | Skip LinkedIn verification entirely                           |
| `--skip-public-records` | Skip public records / SOS check                               |
| `--resume`              | Skip rows that already have an email filled in                |

---

## Example Commands

**Basic run:**
```bash
python enrich_owners.py --input properties.xlsx
```

**Specify columns manually:**
```bash
python enrich_owners.py --input data.xlsx --owner-col "Owner Full Name" --address-col "Site Address"
```

**Dry run to preview:**
```bash
python enrich_owners.py --input properties.xlsx --dry-run
```

**Texas properties, skip LinkedIn:**
```bash
python enrich_owners.py --input tx_properties.xlsx --state TX --skip-linkedin
```

**Resume a previously interrupted run:**
```bash
python enrich_owners.py --input properties.xlsx --resume
```

**Fast run — public records only, no LinkedIn:**
```bash
python enrich_owners.py --input properties.xlsx --skip-linkedin
```

---

## Output File

Saved as `[original_filename]_enriched.xlsx` in the same directory as the input.

Contains:
- All original columns preserved
- New enrichment columns appended
- Green-highlighted rows (confidence 3)
- Red-highlighted rows (confidence 0, needs manual review)
- A **Summary** sheet with aggregate stats

---

## Notes

- A 0.5-second delay is enforced between every RocketReach API call to stay within rate limits.
- LinkedIn scraping uses public profile pages only; login is not required.
- California SOS lookups use the `bizfileonline.sos.ca.gov` search endpoint. For other states, a Google-based fallback is used.
- The web search fallback uses `googlesearch-python`, which queries Google. Use sparingly to avoid IP blocks.

---

# Interactive BOV Generator (`generate_bov.py`)

Creates a single, self-contained **interactive HTML Broker Opinion of Value** tailored
to one property — built to email directly to a multifamily owner. It covers area
apartment rents, sale comps, an interactive valuation calculator, location &
walkability, property-specific investment highlights, neighborhood developments /
amenities, and the regulations that affect value.

**You fill in** rents, comps, highlights, regulations, and neighborhood notes.
**It auto-fetches** what's free: map coordinates (geocoding) and Walk/Transit/Bike scores.

## What's in the BOV

| Section | Contents |
|---|---|
| **Snapshot** | Key stats, in-place vs. market rent chart, full unit-mix rent roll |
| **Area Rents** | Subject-vs-comps bar chart, submarket rent-trend line, comparable rentals table |
| **Sale Comps** | $/unit chart, cap rates, comparable sales table, implied value |
| **Valuation** | **Interactive calculator** — drag cap rate, % of rent upside captured, and NOI margin to see estimated value update live |
| **Location** | Walk / Transit / Bike score rings + interactive map of subject and all comps |
| **Highlights** | Property-specific investment highlights |
| **Neighborhood** | Developments/drivers and nearby amenities |
| **Regulatory** | Rules affecting value, each tagged Tailwind / Neutral / Watch |
| **Contact** | Your branding + a one-click "email me" call-to-action |

## Quick start

```bash
pip install -r requirements.txt          # only `requests` is needed for this tool

# 1. Copy the sample and edit it for your deal
cp sample_property.json my_deal.json

# 2. (optional) export your Walk Score key for walkability auto-fetch
export WALKSCORE_API_KEY=xxxxxxxx

# 3. Generate the BOV
python generate_bov.py --input my_deal.json
# -> writes my_deal_bov.html  (open in a browser, or attach to an email)
```

## CLI options

| Flag | Description |
|---|---|
| `--input FILE` | **(Required)** Path to the property `.json` file |
| `--output FILE` | Output HTML path (default: `<input>_bov.html`) |
| `--template FILE` | HTML template to use (default: `bov_template.html`) |
| `--no-geocode` | Skip address geocoding (no map auto-population) |
| `--no-walkscore` | Skip the Walk Score API lookup |

## Property JSON

See **`sample_property.json`** for a complete, filled-in example. Highlights of the schema:

- `subject_property` — name, address, `units`, `year_built`, `current_noi`, and a
  `unit_mix` array (`type`, `count`, `avg_sf`, `current_rent`, `market_rent`).
  The market-vs-in-place spread drives the rent-upside and valuation math.
- `rent_comps` / `sale_comps` — competing rentals and recent trades. Provide an
  `address` and they'll be geocoded onto the map automatically (or pass `lat`/`lng`).
- `rent_trend` — optional submarket rent history for the trend chart.
- `investment_highlights`, `neighborhood.developments`, `neighborhood.amenities` —
  free-text lists.
- `regulations` — each has `title`, `impact`, and `sentiment`
  (`positive` / `neutral` / `risk` → shown as Tailwind / Neutral / Watch).
- `walk_scores` — leave `null` to auto-fetch (needs `WALKSCORE_API_KEY`), or fill manually.
- `broker` — your name, company, phone, email, license for the footer and CTA.

Anything you omit is gracefully hidden in the output — fill in only what you have.

## Notes

- **Auto-enrichment needs network access.** Geocoding uses OpenStreetMap Nominatim
  (`nominatim.openstreetmap.org`, no key, rate-limited to 1 req/sec) and walkability
  uses the Walk Score API (`api.walkscore.com`). In a restricted/sandboxed environment,
  allowlist those hosts or run locally; without them the BOV still renders, just
  without the map/scores.
- **The output HTML loads Chart.js and Leaflet from a CDN**, so the *viewer* needs an
  internet connection to see the charts and map. Everything else is embedded in the file.
- Figures are estimates for discussion only — the BOV includes a disclaimer to that effect
  (editable via the `disclaimer` field).
