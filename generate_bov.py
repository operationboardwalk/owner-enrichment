# ---- API KEYS — OPTIONAL ----
# Walk Score API key (free tier: https://www.walkscore.com/professional/api-sign-up.php).
# Leave blank to skip walkability auto-fetch (you can still fill scores in the JSON).
WALKSCORE_API_KEY = ""
# -----------------------------

"""
generate_bov.py — Interactive Broker Opinion of Value (BOV) generator.

Reads a per-property JSON file, auto-enriches it with what's freely available
(geocoding for the map via OpenStreetMap Nominatim, and Walk/Transit/Bike scores
via the Walk Score API), computes the financial metrics, and renders a single
self-contained, interactive HTML BOV you can email to a multifamily owner.

You fill in: rents, rent comps, sale comps, investment highlights, regulations,
neighborhood developments/amenities, and broker contact info.
We auto-fill: map coordinates and walkability scores (where available).

Usage:
    python generate_bov.py --input sample_property.json
    python generate_bov.py --input my_deal.json --output my_deal_bov.html
    python generate_bov.py --input my_deal.json --no-geocode --no-walkscore

A starter file is provided in sample_property.json. Copy it and edit per deal.
"""

import argparse
import json
import os
import sys
import time
from datetime import date
from pathlib import Path

import requests

TEMPLATE_FILE = "bov_template.html"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
WALKSCORE_URL = "https://api.walkscore.com/score"
USER_AGENT = "owner-enrichment-bov/1.0 (multifamily BOV generator)"

# Walk Score's published score bands -> short descriptions.
def _band(score, kind):
    if score is None:
        return ""
    s = score
    if kind == "walk":
        if s >= 90: return "Walker's paradise"
        if s >= 70: return "Very walkable"
        if s >= 50: return "Somewhat walkable"
        if s >= 25: return "Car-dependent"
        return "Car-dependent"
    if kind == "transit":
        if s >= 90: return "Rider's paradise"
        if s >= 70: return "Excellent transit"
        if s >= 50: return "Good transit"
        if s >= 25: return "Some transit"
        return "Minimal transit"
    if kind == "bike":
        if s >= 90: return "Biker's paradise"
        if s >= 70: return "Very bikeable"
        if s >= 50: return "Bikeable"
        return "Somewhat bikeable"
    return ""


def geocode(query: str) -> dict | None:
    """Free-form address -> {lat, lng} using OpenStreetMap Nominatim."""
    try:
        resp = requests.get(
            NOMINATIM_URL,
            params={"q": query, "format": "json", "limit": 1},
            headers={"User-Agent": USER_AGENT},
            timeout=15,
        )
        time.sleep(1.0)  # Nominatim usage policy: max 1 req/sec
        if resp.ok:
            data = resp.json()
            if data:
                return {"lat": float(data[0]["lat"]), "lng": float(data[0]["lon"])}
    except Exception as e:
        print(f"  [geocode] '{query}': {e}")
    return None


def full_address(obj: dict) -> str:
    parts = [obj.get("address"), obj.get("city"), obj.get("state"), obj.get("zip")]
    return ", ".join(str(p) for p in parts if p)


def enrich_coords(data: dict, enabled: bool):
    """Fill lat/lng for the subject and every comp that's missing them."""
    if not enabled:
        return
    targets = [data.get("subject_property", {})]
    targets += data.get("rent_comps", [])
    targets += data.get("sale_comps", [])
    for obj in targets:
        if not isinstance(obj, dict):
            continue
        if obj.get("lat") and obj.get("lng"):
            continue
        addr = full_address(obj) if obj.get("address") else obj.get("name", "")
        if not addr:
            continue
        coords = geocode(addr)
        if coords:
            obj["lat"], obj["lng"] = coords["lat"], coords["lng"]
            print(f"  [geocode] {addr} -> {coords['lat']:.5f}, {coords['lng']:.5f}")
        else:
            print(f"  [geocode] no match for: {addr}")


def fetch_walkscore(data: dict, api_key: str, enabled: bool):
    """Populate data['walk_scores'] from the Walk Score API for the subject."""
    if not enabled:
        return
    if data.get("walk_scores", {}).get("walk") is not None:
        return  # already provided manually
    sp = data.get("subject_property", {})
    lat, lng = sp.get("lat"), sp.get("lng")
    if not api_key:
        print("  [walkscore] no WALKSCORE_API_KEY set — skipping (fill manually if needed)")
        return
    if not (lat and lng):
        print("  [walkscore] subject has no coordinates — skipping")
        return
    try:
        resp = requests.get(
            WALKSCORE_URL,
            params={
                "format": "json",
                "address": full_address(sp),
                "lat": lat, "lon": lng,
                "transit": 1, "bike": 1,
                "wsapikey": api_key,
            },
            headers={"User-Agent": USER_AGENT},
            timeout=15,
        )
        if resp.ok:
            j = resp.json()
            ws = data.setdefault("walk_scores", {})
            if j.get("walkscore") is not None:
                ws["walk"] = j["walkscore"]
                ws["walk_desc"] = j.get("description") or _band(j["walkscore"], "walk")
            ts = (j.get("transit") or {}).get("score")
            if ts is not None:
                ws["transit"] = ts
                ws["transit_desc"] = (j.get("transit") or {}).get("description") or _band(ts, "transit")
            bs = (j.get("bike") or {}).get("score")
            if bs is not None:
                ws["bike"] = bs
                ws["bike_desc"] = (j.get("bike") or {}).get("description") or _band(bs, "bike")
            print(f"  [walkscore] walk={ws.get('walk')} transit={ws.get('transit')} bike={ws.get('bike')}")
        else:
            print(f"  [walkscore] HTTP {resp.status_code}")
    except Exception as e:
        print(f"  [walkscore] error: {e}")


def compute_metrics(data: dict):
    """Derive rent-roll, upside, and comp-based value metrics for the header/cards."""
    sp = data.get("subject_property", {})
    mix = sp.get("unit_mix", []) or []
    m = {}

    cur_total = sum((u.get("current_rent") or 0) * (u.get("count") or 0) for u in mix)
    mkt_total = sum((u.get("market_rent") or u.get("current_rent") or 0) * (u.get("count") or 0) for u in mix)
    unit_count = sum((u.get("count") or 0) for u in mix) or sp.get("units") or 0

    if cur_total:
        m["current_monthly_rent"] = cur_total
    if mkt_total:
        m["market_monthly_rent"] = mkt_total
    if unit_count:
        if cur_total:
            m["avg_inplace_rent"] = round(cur_total / unit_count)
        if mkt_total:
            m["avg_market_rent"] = round(mkt_total / unit_count)
    if mkt_total and cur_total:
        m["monthly_rent_upside"] = mkt_total - cur_total
        if cur_total:
            m["rent_upside_pct"] = (mkt_total - cur_total) / cur_total * 100

    # Sale comp aggregates
    sc = data.get("sale_comps", []) or []
    ppus, caps = [], []
    for c in sc:
        ppu = c.get("price_per_unit")
        if not ppu and c.get("price") and c.get("units"):
            ppu = c["price"] / c["units"]
        if ppu:
            ppus.append(ppu)
        if c.get("cap_rate"):
            caps.append(c["cap_rate"])
    if ppus:
        m["avg_comp_price_per_unit"] = round(sum(ppus) / len(ppus))
    if caps:
        m["avg_comp_cap_rate"] = round(sum(caps) / len(caps), 2)

    # Implied value: prefer cap-rate on current NOI, else $/unit on unit count.
    noi = sp.get("current_noi")
    if noi and m.get("avg_comp_cap_rate"):
        m["implied_value"] = round(noi / (m["avg_comp_cap_rate"] / 100))
    elif m.get("avg_comp_price_per_unit") and unit_count:
        m["implied_value"] = round(m["avg_comp_price_per_unit"] * unit_count)
    if m.get("implied_value") and unit_count:
        m["implied_value_per_unit"] = round(m["implied_value"] / unit_count)

    data["metrics"] = m


def render(data: dict, template_path: Path) -> str:
    html = template_path.read_text(encoding="utf-8")
    blob = json.dumps(data, ensure_ascii=False)
    if "/*__BOV_DATA__*/{}" not in html:
        print("[ERROR] Template placeholder not found in", template_path)
        sys.exit(1)
    return html.replace("/*__BOV_DATA__*/{}", blob)


def parse_args():
    p = argparse.ArgumentParser(description="Generate an interactive HTML Broker Opinion of Value.")
    p.add_argument("--input", required=True, help="Path to the property JSON file")
    p.add_argument("--output", default=None, help="Output HTML path (default: <input>_bov.html)")
    p.add_argument("--template", default=TEMPLATE_FILE, help="HTML template file")
    p.add_argument("--no-geocode", action="store_true", help="Skip address geocoding for the map")
    p.add_argument("--no-walkscore", action="store_true", help="Skip Walk Score API lookup")
    return p.parse_args()


def main():
    args = parse_args()
    in_path = Path(args.input)
    if not in_path.exists():
        print(f"[ERROR] Input file not found: {in_path}")
        sys.exit(1)

    template_path = Path(args.template)
    if not template_path.exists():
        # fall back to template next to this script
        alt = Path(__file__).parent / args.template
        if alt.exists():
            template_path = alt
        else:
            print(f"[ERROR] Template not found: {args.template}")
            sys.exit(1)

    with in_path.open(encoding="utf-8") as f:
        data = json.load(f)

    data.setdefault("prepared_date", date.today().strftime("%B %d, %Y"))

    print(f"Loading: {in_path}")
    print("Enriching...")
    enrich_coords(data, enabled=not args.no_geocode)
    api_key = os.environ.get("WALKSCORE_API_KEY", WALKSCORE_API_KEY)
    fetch_walkscore(data, api_key, enabled=not args.no_walkscore)
    compute_metrics(data)

    out_path = Path(args.output) if args.output else in_path.with_name(in_path.stem + "_bov.html")
    out_path.write_text(render(data, template_path), encoding="utf-8")

    m = data.get("metrics", {})
    print(f"\nBOV written: {out_path}")
    if m.get("implied_value"):
        print(f"  Implied value: ${m['implied_value']:,}"
              + (f" (${m.get('implied_value_per_unit'):,}/unit)" if m.get("implied_value_per_unit") else ""))
    print("  Open it in a browser, or attach the .html file to an email.")


if __name__ == "__main__":
    main()
