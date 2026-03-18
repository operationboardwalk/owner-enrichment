# ---- API KEYS —  ----
ROCKETREACH_API_KEY = "PASTE_YOUR_KEY"
# -------------------------------------------

"""
enrich_owners.py — Property Owner Contact Enrichment Script
Enriches an Excel property list with contact info via RocketReach,
LinkedIn verification, and public records cross-check.
"""

import argparse
import re
import sys
import time
import warnings
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup
from fuzzywuzzy import fuzz
from openpyxl import load_workbook
from openpyxl.styles import PatternFill
from tqdm import tqdm

warnings.filterwarnings("ignore")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ENTITY_KEYWORDS = [
    "LLC", "LP", "Inc", "Corp", "Trust", "Fund", "Partners", "Holdings",
    "Properties", "Investments", "Group", "Realty", "Capital",
]

OUTPUT_COLUMNS = [
    "Email", "Phone", "LinkedIn_URL", "LinkedIn_Verified",
    "Public_Records_Match", "LLC_Registered_Agent", "Owner_Type",
    "Source", "Confidence_Score", "Notes",
]

RR_BASE = "https://api.rocketreach.co/api/v2"
HEADERS_RR = lambda key: {"Api-Key": key, "Content-Type": "application/json"}

GREEN_FILL = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
RED_FILL   = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def delay():
    time.sleep(0.5)


def safe_str(val) -> str:
    if pd.isna(val):
        return ""
    return str(val).strip()


def is_entity(owner_name: str) -> bool:
    upper = owner_name.upper()
    return any(kw.upper() in upper for kw in ENTITY_KEYWORDS)


def extract_emails_from_text(text: str) -> list[str]:
    return re.findall(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", text)


def extract_phones_from_text(text: str) -> list[str]:
    return re.findall(
        r"\+?1?[\s.\-]?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}", text
    )


def google_search_snippets(query: str, num: int = 5) -> list[str]:
    """Return a list of snippet strings from a Google search."""
    try:
        from googlesearch import search  # type: ignore
        results = list(search(query, num_results=num, sleep_interval=1))
        return results  # URLs; we'll fetch them below if needed
    except Exception:
        return []


def fetch_url_text(url: str, timeout: int = 10) -> str:
    try:
        resp = requests.get(url, timeout=timeout, headers={
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
            )
        })
        if resp.ok:
            soup = BeautifulSoup(resp.text, "html.parser")
            return soup.get_text(separator=" ", strip=True)
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# Column auto-detection
# ---------------------------------------------------------------------------

OWNER_HINTS    = ["owner", "name", "owner name", "property owner"]
ADDRESS_HINTS  = ["address", "property address", "site address", "street"]
CITY_HINTS     = ["city", "municipality", "town"]
STATE_HINTS    = ["state", "st", "province"]


def find_column(df: pd.DataFrame, hints: list[str], label: str, manual: str | None) -> str:
    if manual:
        if manual not in df.columns:
            print(f"[ERROR] Manually specified column '{manual}' not found in file.")
            sys.exit(1)
        return manual
    cols_lower = {c.lower().strip(): c for c in df.columns}
    for hint in hints:
        if hint in cols_lower:
            return cols_lower[hint]
    # partial match
    for hint in hints:
        for col_lower, col in cols_lower.items():
            if hint in col_lower:
                return col
    # prompt user
    print(f"\nCould not auto-detect the '{label}' column.")
    print("Available columns:", list(df.columns))
    chosen = input(f"Enter the column name for '{label}': ").strip()
    if chosen not in df.columns:
        print(f"[ERROR] '{chosen}' is not a valid column.")
        sys.exit(1)
    return chosen


# ---------------------------------------------------------------------------
# STEP 1 — Classify Owner Type
# ---------------------------------------------------------------------------

def classify_owner_type(owner_name: str) -> str:
    return "ENTITY" if is_entity(owner_name) else "INDIVIDUAL"


# ---------------------------------------------------------------------------
# STEP 2 — RocketReach Lookup
# ---------------------------------------------------------------------------

def rr_person_lookup(name: str, city: str, state: str, api_key: str) -> dict:
    """Look up an individual via RocketReach Person Lookup API."""
    params = {"name": name, "location_city": city, "location_state": state}
    try:
        resp = requests.get(
            f"{RR_BASE}/person/lookup",
            params=params,
            headers=HEADERS_RR(api_key),
            timeout=15,
        )
        delay()
        if resp.ok:
            return resp.json()
    except Exception as e:
        print(f"  [RR Person] Error: {e}")
    return {}


def rr_company_lookup(name: str, api_key: str) -> dict:
    """Look up a company via RocketReach Company Lookup API."""
    params = {"name": name}
    try:
        resp = requests.get(
            f"{RR_BASE}/company/lookup",
            params=params,
            headers=HEADERS_RR(api_key),
            timeout=15,
        )
        delay()
        if resp.ok:
            return resp.json()
    except Exception as e:
        print(f"  [RR Company] Error: {e}")
    return {}


def rr_person_lookup_by_company(company_id: int, api_key: str) -> dict:
    """Find principal contact at a company via RocketReach Person Lookup."""
    params = {"current_employer_id": company_id}
    try:
        resp = requests.get(
            f"{RR_BASE}/person/lookup",
            params=params,
            headers=HEADERS_RR(api_key),
            timeout=15,
        )
        delay()
        if resp.ok:
            return resp.json()
    except Exception as e:
        print(f"  [RR Person@Company] Error: {e}")
    return {}


def extract_best_email(profile: dict) -> str:
    emails = profile.get("emails", [])
    if not emails:
        # flat field
        return profile.get("email", "")
    # prefer work emails
    work = [e.get("email", "") for e in emails if e.get("type", "").lower() == "work"]
    if work:
        return work[0]
    return emails[0].get("email", "") if emails else ""


def extract_best_phone(profile: dict) -> str:
    phones = profile.get("phones", [])
    if not phones:
        return profile.get("phone", "")
    return phones[0].get("number", "") if phones else ""


def rocketreach_lookup(
    owner_name: str,
    city: str,
    state: str,
    owner_type: str,
    api_key: str,
    dry_run: bool,
) -> dict:
    """
    Returns dict with keys: email, phone, linkedin_url, rr_hit, source, notes,
    registered_agent (for entities)
    """
    result = {
        "email": "", "phone": "", "linkedin_url": "",
        "rr_hit": False, "source": "", "notes": "",
        "registered_agent": "",
    }
    if dry_run:
        return result

    if owner_type == "INDIVIDUAL":
        data = rr_person_lookup(owner_name, city, state, api_key)
        profile = data.get("profile") or data.get("person") or data
        if profile and profile.get("id"):
            result["rr_hit"] = True
            result["email"] = extract_best_email(profile)
            result["phone"] = extract_best_phone(profile)
            result["linkedin_url"] = profile.get("linkedin_url", "")
            result["source"] = "RocketReach - Individual"

    else:  # ENTITY
        co_data = rr_company_lookup(owner_name, api_key)
        company = co_data.get("company") or co_data
        company_id = company.get("id") if company else None
        if company_id:
            # Try to find principal contact
            person_data = rr_person_lookup_by_company(company_id, api_key)
            profile = person_data.get("profile") or person_data.get("person") or person_data
            if profile and profile.get("id"):
                result["rr_hit"] = True
                result["email"] = extract_best_email(profile)
                result["phone"] = extract_best_phone(profile)
                result["linkedin_url"] = profile.get("linkedin_url", "")
                result["source"] = "RocketReach - Entity Principal"

    return result


# ---------------------------------------------------------------------------
# STEP 3 — LinkedIn Verification (INDIVIDUAL only)
# ---------------------------------------------------------------------------

def linkedin_verify(
    owner_name: str,
    city: str,
    linkedin_url: str,
    skip: bool,
) -> dict:
    result = {"linkedin_verified": "N/A", "notes": ""}

    if skip:
        return result

    # Find LinkedIn URL if not provided
    if not linkedin_url:
        query = f'site:linkedin.com/in "{owner_name}" "{city}"'
        urls = google_search_snippets(query, num=3)
        for url in urls:
            if "linkedin.com/in/" in url:
                linkedin_url = url
                break

    if not linkedin_url:
        result["linkedin_verified"] = "False"
        result["notes"] = "LinkedIn profile not found"
        return result

    # Fetch public profile
    text = fetch_url_text(linkedin_url)
    if not text:
        result["linkedin_verified"] = "False"
        result["notes"] = "Could not fetch LinkedIn profile"
        return result

    # Fuzzy match name
    name_score = fuzz.token_sort_ratio(owner_name.lower(), text[:500].lower())
    city_in_text = city.lower() in text[:1000].lower()

    if name_score >= 85:
        if city_in_text:
            result["linkedin_verified"] = "True"
        else:
            result["linkedin_verified"] = "Partial"
            result["notes"] = "Name matches LinkedIn but location not confirmed"
    else:
        result["linkedin_verified"] = "False"
        result["notes"] = f"LinkedIn name mismatch (score={name_score}); flagged for manual review"

    return result


# ---------------------------------------------------------------------------
# STEP 4 — Public Records Verification
# ---------------------------------------------------------------------------

SOS_SEARCH_URL = "https://bizfileonline.sos.ca.gov/search/business"


def public_records_individual(owner_name: str, address: str) -> dict:
    result = {"public_records_match": "False", "notes": ""}
    query = f'"{owner_name}" "{address}" assessor OR "property records"'
    urls = google_search_snippets(query, num=5)
    for url in urls:
        text = fetch_url_text(url)
        name_score = fuzz.token_sort_ratio(owner_name.lower(), text.lower())
        addr_score = fuzz.partial_ratio(address.lower(), text.lower())
        if name_score >= 80 and addr_score >= 70:
            result["public_records_match"] = "True"
            break
    return result


def sos_scrape_ca(entity_name: str) -> dict:
    """Scrape California SOS business search for entity info."""
    result = {
        "public_records_match": "False",
        "registered_agent": "",
        "notes": "",
    }
    try:
        # The CA SOS site is a React SPA; attempt a basic search via the API endpoint
        api_url = "https://bizfileonline.sos.ca.gov/api/Records/businesssearch"
        params = {
            "SearchType": "CORP",
            "SearchCriteria": entity_name,
            "SearchSubType": "Exact",
        }
        resp = requests.get(api_url, params=params, timeout=15, headers={
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
            ),
            "Accept": "application/json",
        })
        delay()
        if resp.ok:
            try:
                data = resp.json()
                hits = data.get("rows") or data.get("results") or []
                if hits:
                    entity = hits[0]
                    status = str(entity.get("Status", "")).upper()
                    agent = entity.get("Agent") or entity.get("RegisteredAgent", "")
                    result["registered_agent"] = agent
                    if "ACTIVE" in status:
                        result["public_records_match"] = "True"
                    elif any(s in status for s in ["SUSPENDED", "DISSOLVED", "FORFEITED"]):
                        result["notes"] = f"Entity status: {status} — verify before contact"
                    return result
            except Exception:
                pass

        # Fallback: plain text search
        resp2 = requests.get(SOS_SEARCH_URL, params={"SearchCriteria": entity_name},
                              timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        delay()
        if resp2.ok:
            soup = BeautifulSoup(resp2.text, "html.parser")
            text = soup.get_text(" ", strip=True)
            score = fuzz.partial_ratio(entity_name.lower(), text.lower())
            if score >= 80:
                result["public_records_match"] = "True"

    except Exception as e:
        result["notes"] = f"SOS lookup error: {e}"
    return result


def public_records_entity(
    owner_name: str,
    state: str,
    api_key: str,
    dry_run: bool,
) -> dict:
    result = {
        "public_records_match": "False",
        "registered_agent": "",
        "email": "",
        "phone": "",
        "source": "",
        "notes": "",
    }

    if dry_run:
        return result

    if state.upper() == "CA":
        sos = sos_scrape_ca(owner_name)
        result["public_records_match"] = sos["public_records_match"]
        result["registered_agent"] = sos.get("registered_agent", "")
        result["notes"] = sos.get("notes", "")

        # Second RocketReach lookup on registered agent
        agent_name = result["registered_agent"]
        if agent_name and not dry_run:
            agent_data = rr_person_lookup(agent_name, "", state, api_key)
            profile = agent_data.get("profile") or agent_data.get("person") or agent_data
            if profile and profile.get("id"):
                result["email"] = extract_best_email(profile)
                result["phone"] = extract_best_phone(profile)
                result["source"] = "RocketReach - Registered Agent"
    else:
        # Generic web search for other states
        query = f'"{owner_name}" secretary of state registered agent {state}'
        urls = google_search_snippets(query, num=3)
        for url in urls:
            text = fetch_url_text(url)
            score = fuzz.partial_ratio(owner_name.lower(), text.lower())
            if score >= 75:
                result["public_records_match"] = "True"
                break

    return result


# ---------------------------------------------------------------------------
# STEP 5 — Web Search Fallback
# ---------------------------------------------------------------------------

def web_search_fallback(owner_name: str, city: str) -> dict:
    result = {"email": "", "phone": "", "source": "Web Search Fallback", "notes": ""}
    query = f'"{owner_name}" "{city}" contact email phone real estate'
    urls = google_search_snippets(query, num=5)
    for url in urls:
        text = fetch_url_text(url)
        emails = extract_emails_from_text(text)
        phones = extract_phones_from_text(text)
        if emails:
            result["email"] = emails[0]
        if phones:
            result["phone"] = phones[0]
        if result["email"] or result["phone"]:
            break
    return result


# ---------------------------------------------------------------------------
# STEP 6 — Confidence Score
# ---------------------------------------------------------------------------

def compute_confidence(
    rr_hit: bool,
    linkedin_verified: str,
    public_records_match: str,
    web_fallback_used: bool,
) -> int:
    li = str(linkedin_verified)
    pr = str(public_records_match)

    if rr_hit and li == "True" and pr == "True":
        return 3
    if rr_hit and (li in ("True", "Partial") or pr == "True"):
        return 2
    if rr_hit:
        return 1
    if web_fallback_used:
        return 1
    return 0


# ---------------------------------------------------------------------------
# Summary Sheet
# ---------------------------------------------------------------------------

def write_summary(wb, df: pd.DataFrame):
    if "Summary" in wb.sheetnames:
        del wb["Summary"]
    ws = wb.create_sheet("Summary")

    total = len(df)
    high   = int((df["Confidence_Score"] == 3).sum())
    medium = int((df["Confidence_Score"] == 2).sum())
    low    = int((df["Confidence_Score"] == 1).sum())
    none   = int((df["Confidence_Score"] == 0).sum())
    llc_count = int((df["Owner_Type"] == "ENTITY").sum())
    li_verified = int((df["LinkedIn_Verified"] == "True").sum())

    rows = [
        ("Metric", "Count"),
        ("Total Rows", total),
        ("High Confidence (3)", high),
        ("Medium Confidence (2)", medium),
        ("Low Confidence (1)", low),
        ("Not Found (0)", none),
        ("LLC / Entity Count", llc_count),
        ("LinkedIn Verified", li_verified),
    ]
    for row in rows:
        ws.append(row)

    ws.column_dimensions["A"].width = 28
    ws.column_dimensions["B"].width = 12


# ---------------------------------------------------------------------------
# Row Highlighting
# ---------------------------------------------------------------------------

def apply_highlights(wb, df: pd.DataFrame, sheet_name: str):
    ws = wb[sheet_name]
    score_col_idx = df.columns.get_loc("Confidence_Score") + 1  # 1-indexed

    for i, score in enumerate(df["Confidence_Score"], start=2):  # row 1 = header
        fill = None
        if score == 3:
            fill = GREEN_FILL
        elif score == 0:
            fill = RED_FILL
        if fill:
            for cell in ws[i]:
                cell.fill = fill


# ---------------------------------------------------------------------------
# Main enrichment loop
# ---------------------------------------------------------------------------

def enrich_row(
    row: pd.Series,
    col_owner: str,
    col_address: str,
    col_city: str,
    col_state: str,
    default_state: str,
    api_key: str,
    dry_run: bool,
    skip_linkedin: bool,
    skip_public_records: bool,
) -> dict:
    owner   = safe_str(row.get(col_owner, ""))
    address = safe_str(row.get(col_address, ""))
    city    = safe_str(row.get(col_city, ""))
    state   = safe_str(row.get(col_state, "")) or default_state

    out = {c: "" for c in OUTPUT_COLUMNS}
    out["Confidence_Score"] = 0
    notes_parts: list[str] = []

    if not owner:
        out["Notes"] = "No owner name"
        return out

    # STEP 1
    owner_type = classify_owner_type(owner)
    out["Owner_Type"] = owner_type

    # STEP 2
    rr = rocketreach_lookup(owner, city, state, owner_type, api_key, dry_run)
    out["Email"]        = rr["email"]
    out["Phone"]        = rr["phone"]
    out["LinkedIn_URL"] = rr["linkedin_url"]
    out["Source"]       = rr["source"]
    if rr["notes"]:
        notes_parts.append(rr["notes"])

    # STEP 3 — LinkedIn (INDIVIDUAL only)
    if owner_type == "INDIVIDUAL" and not skip_linkedin:
        li = linkedin_verify(owner, city, rr["linkedin_url"], skip=dry_run)
        out["LinkedIn_Verified"] = li["linkedin_verified"]
        if li["notes"]:
            notes_parts.append(li["notes"])
    else:
        out["LinkedIn_Verified"] = "N/A"

    # STEP 4 — Public Records
    if not skip_public_records:
        if owner_type == "INDIVIDUAL" and not dry_run:
            pr = public_records_individual(owner, address)
            out["Public_Records_Match"] = pr["public_records_match"]
            if pr["notes"]:
                notes_parts.append(pr["notes"])
        elif owner_type == "ENTITY":
            pr_e = public_records_entity(owner, state, api_key, dry_run)
            out["Public_Records_Match"] = pr_e["public_records_match"]
            out["LLC_Registered_Agent"] = pr_e.get("registered_agent", "")
            if pr_e.get("notes"):
                notes_parts.append(pr_e["notes"])
            # If entity lookup found contact info and RocketReach didn't
            if not out["Email"] and pr_e.get("email"):
                out["Email"]  = pr_e["email"]
                out["Phone"]  = pr_e["phone"]
                out["Source"] = pr_e["source"]

    # STEP 5 — Web Fallback
    web_fallback_used = False
    if not out["Email"] and not out["Phone"] and not out["Public_Records_Match"] == "True":
        if not dry_run:
            wb_res = web_search_fallback(owner, city)
            if wb_res["email"] or wb_res["phone"]:
                out["Email"]  = wb_res["email"]
                out["Phone"]  = wb_res["phone"]
                out["Source"] = wb_res["source"]
                web_fallback_used = True

    # STEP 6 — Confidence
    out["Confidence_Score"] = compute_confidence(
        rr_hit=rr["rr_hit"],
        linkedin_verified=out["LinkedIn_Verified"],
        public_records_match=out["Public_Records_Match"],
        web_fallback_used=web_fallback_used,
    )

    if out["Confidence_Score"] == 0:
        notes_parts.append("No data found — flagged for manual review")

    out["Notes"] = "; ".join(notes_parts)
    return out


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(
        description="Enrich property owner contact info using RocketReach, LinkedIn, and public records."
    )
    p.add_argument("--input",               required=True, help="Path to input .xlsx file")
    p.add_argument("--owner-col",           default=None,  help="Owner name column (overrides auto-detect)")
    p.add_argument("--address-col",         default=None,  help="Address column (overrides auto-detect)")
    p.add_argument("--state",               default="CA",  help="Default state for SOS lookup (default: CA)")
    p.add_argument("--dry-run",             action="store_true", help="Print lookup plan without API calls")
    p.add_argument("--skip-linkedin",       action="store_true", help="Skip LinkedIn verification")
    p.add_argument("--skip-public-records", action="store_true", help="Skip public records check")
    p.add_argument("--resume",              action="store_true", help="Skip rows that already have Email filled in")
    return p.parse_args()


def main():
    args = parse_args()
    input_path = Path(args.input)
    if not input_path.exists():
        print(f"[ERROR] File not found: {input_path}")
        sys.exit(1)

    print(f"Loading: {input_path}")
    df = pd.read_excel(input_path, dtype=str)
    df = df.fillna("")

    # Auto-detect columns
    col_owner   = find_column(df, OWNER_HINTS,   "Owner Name",       args.owner_col)
    col_address = find_column(df, ADDRESS_HINTS, "Property Address", args.address_col)
    col_city    = find_column(df, CITY_HINTS,    "City",             None)
    col_state   = find_column(df, STATE_HINTS,   "State",            None)

    print(f"Columns → owner='{col_owner}', address='{col_address}', city='{col_city}', state='{col_state}'")

    if args.dry_run:
        print("\n[DRY RUN] Lookup plan (no API calls will be made):\n")

    # Ensure output columns exist
    for col in OUTPUT_COLUMNS:
        if col not in df.columns:
            df[col] = ""

    # Process rows
    for idx in tqdm(df.index, desc="Enriching rows", unit="row"):
        row = df.loc[idx]

        # Resume mode: skip rows with email already filled
        if args.resume and safe_str(row.get("Email", "")):
            continue

        if args.dry_run:
            owner = safe_str(row.get(col_owner, ""))
            otype = classify_owner_type(owner)
            print(f"  Row {idx+1}: '{owner}' → {otype}")
            continue

        enriched = enrich_row(
            row=row,
            col_owner=col_owner,
            col_address=col_address,
            col_city=col_city,
            col_state=col_state,
            default_state=args.state,
            api_key=ROCKETREACH_API_KEY,
            dry_run=args.dry_run,
            skip_linkedin=args.skip_linkedin,
            skip_public_records=args.skip_public_records,
        )

        for col, val in enriched.items():
            df.at[idx, col] = val

    if args.dry_run:
        print("\n[DRY RUN] Complete. No output file written.")
        return

    # Write output
    output_path = input_path.parent / (input_path.stem + "_enriched.xlsx")
    df.to_excel(output_path, index=False)

    # Apply highlights and summary using openpyxl
    wb = load_workbook(output_path)
    sheet_name = wb.sheetnames[0]
    apply_highlights(wb, df, sheet_name)
    write_summary(wb, df)
    wb.save(output_path)

    print(f"\nOutput saved: {output_path}")

    # Quick stats
    total = len(df)
    high   = (df["Confidence_Score"].astype(str) == "3").sum()
    medium = (df["Confidence_Score"].astype(str) == "2").sum()
    low    = (df["Confidence_Score"].astype(str) == "1").sum()
    none   = (df["Confidence_Score"].astype(str) == "0").sum()
    print(f"\nSummary: Total={total}  High={high}  Medium={medium}  Low={low}  Not Found={none}")


if __name__ == "__main__":
    main()
