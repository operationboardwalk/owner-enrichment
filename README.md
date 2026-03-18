# Owner Enrichment Script

Enriches a property owner Excel list with contact information sourced from
RocketReach, LinkedIn public profiles, and public records (California SOS).

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
