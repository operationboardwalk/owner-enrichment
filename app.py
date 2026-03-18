"""
app.py — Owner Enrichment Web App (Flask)
Enriches property owner lists with contact info via the RocketReach API.
"""

import json
import os
import threading
import time
import uuid
from io import BytesIO, StringIO

import pandas as pd
import requests
from flask import Flask, Response, jsonify, render_template, request, send_file

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024  # 32 MB

# In-memory job store  {job_id: {...}}
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()

RR_BASE = "https://api.rocketreach.co/api/v2"

ENTITY_KEYWORDS = [
    "LLC", "LP", "LLP", "Inc", "Corp", "Trust", "Fund", "Partners",
    "Holdings", "Properties", "Investments", "Group", "Realty", "Capital",
    "Enterprises", "Associates", "Management",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe(val) -> str:
    if val is None:
        return ""
    try:
        if pd.isna(val):
            return ""
    except (TypeError, ValueError):
        pass
    s = str(val).strip()
    return "" if s.lower() in ("nan", "none", "") else s


def _is_entity(name: str) -> bool:
    upper = name.upper()
    return any(kw.upper() in upper for kw in ENTITY_KEYWORDS)


def _rr_headers(api_key: str) -> dict:
    return {"Api-Key": api_key, "Content-Type": "application/json"}


def _extract_email(profile: dict) -> str:
    emails = profile.get("emails") or []
    if not emails:
        return _safe(profile.get("email", ""))
    # Prefer work/professional emails
    for e in emails:
        if isinstance(e, dict) and e.get("type", "").lower() in ("work", "professional"):
            return _safe(e.get("email", ""))
    first = emails[0]
    return _safe(first.get("email", "") if isinstance(first, dict) else first)


def _extract_phone(profile: dict) -> str:
    phones = profile.get("phones") or []
    if not phones:
        return _safe(profile.get("phone", ""))
    first = phones[0]
    return _safe(first.get("number", "") if isinstance(first, dict) else first)


def _lookup_poll(url: str, params: dict, headers: dict, retries: int = 3) -> tuple[dict | None, str]:
    """GET RocketReach lookup; poll up to `retries` times on 202.
    Returns (data, debug_msg)."""
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=20)
        except requests.RequestException as exc:
            return None, f"Request error: {exc}"
        if r.status_code == 200:
            return r.json(), "HTTP 200"
        if r.status_code == 202 and attempt < retries:
            time.sleep(3 * (attempt + 1))
            continue
        return None, f"HTTP {r.status_code}: {r.text[:200]}"
    return None, "Max retries on 202"


def _search_post(query: dict, headers: dict) -> tuple[list, str]:
    """POST /person/search — returns (profiles_list, debug_msg)."""
    body = {"query": query, "start": 1, "page_size": 3}
    try:
        r = requests.post(f"{RR_BASE}/person/search", json=body, headers=headers, timeout=20)
    except requests.RequestException as exc:
        return [], f"Request error: {exc}"
    if r.status_code == 200:
        data = r.json()
        profiles = data.get("profiles") or data.get("results") or []
        return profiles, f"HTTP 200, {len(profiles)} profiles"
    return [], f"HTTP {r.status_code}: {r.text[:200]}"


# ---------------------------------------------------------------------------
# RocketReach enrichment logic
# ---------------------------------------------------------------------------

def _enrich_one(api_key: str, row: dict, mapping: dict) -> dict:
    """
    Enrich a single row. Returns:
      {email, phone, source, confidence, status, error, debug}
    Priority:
      1. LinkedIn URL   → person/lookup?li_url=...
      2. Name + Company → person/lookup?name=...&current_employer=...
      3. Name + Location→ person/lookup?name=...&location_city=...&location_state=...
      4. Entity/LLC     → company/lookup → person/lookup?current_employer_id=...
    """
    def g(key):
        col = mapping.get(key, "")
        return _safe(row.get(col, "")) if col else ""

    linkedin = g("linkedin")
    name     = g("name")
    company  = g("company")
    city     = g("city")
    state    = g("state")

    hdrs = _rr_headers(api_key)
    result = {"email": "", "phone": "", "source": "", "confidence": "", "status": "not_found", "debug": []}

    def _dbg(msg):
        result["debug"].append(msg)

    def _fill(profile: dict, source: str, confidence: str):
        result["email"]      = _extract_email(profile)
        result["phone"]      = _extract_phone(profile)
        result["source"]     = source
        result["confidence"] = confidence
        result["status"]     = "found" if (result["email"] or result["phone"]) else "not_found"

    try:
        # --- Priority 1: LinkedIn URL ---
        if linkedin:
            data, dbg = _lookup_poll(f"{RR_BASE}/person/lookup", {"li_url": linkedin}, hdrs)
            _dbg(f"LinkedIn lookup: {dbg}")
            if data:
                profile = data.get("profile") or data.get("person") or data
                if profile and profile.get("id"):
                    _fill(profile, "LinkedIn lookup", "High")
                    return result
                else:
                    _dbg(f"LinkedIn: no profile id in response keys={list(data.keys())}")

        # --- Priority 2: Name + Company (direct lookup, then search) ---
        if name and company:
            data, dbg = _lookup_poll(
                f"{RR_BASE}/person/lookup",
                {"name": name, "current_employer": company},
                hdrs,
            )
            _dbg(f"Name+Company lookup: {dbg}")
            if data:
                profile = data.get("profile") or data.get("person") or data
                if profile and profile.get("id"):
                    _fill(profile, "Name + Company", "Medium")
                    return result

            # Fallback: POST search → lookup by id
            query: dict = {"name": [name], "current_employer": [company]}
            profiles, dbg2 = _search_post(query, hdrs)
            _dbg(f"Name+Company search: {dbg2}")
            if profiles:
                pid = profiles[0].get("id")
                if pid:
                    data2, dbg3 = _lookup_poll(f"{RR_BASE}/person/lookup", {"id": pid}, hdrs)
                    _dbg(f"Name+Company lookup by id: {dbg3}")
                    if data2:
                        profile = data2.get("profile") or data2.get("person") or data2
                        if profile and profile.get("id"):
                            _fill(profile, "Name + Company (search)", "Medium")
                            return result

        # --- Priority 3: Name + Location (POST search → lookup) ---
        if name:
            query2: dict = {"name": [name]}
            if city:
                query2["location_city"] = [city]
            if state:
                query2["location_region"] = [state]
            profiles2, dbg4 = _search_post(query2, hdrs)
            _dbg(f"Name+Location search: {dbg4}")
            if profiles2:
                pid = profiles2[0].get("id")
                if pid:
                    data3, dbg5 = _lookup_poll(f"{RR_BASE}/person/lookup", {"id": pid}, hdrs)
                    _dbg(f"Name+Location lookup by id: {dbg5}")
                    if data3:
                        profile = data3.get("profile") or data3.get("person") or data3
                        if profile and profile.get("id"):
                            _fill(profile, "Name + Location", "Low")
                            return result

        # --- Priority 4: Entity / LLC ---
        entity_name = company or name
        if entity_name and _is_entity(entity_name):
            try:
                cr = requests.get(
                    f"{RR_BASE}/company/lookup",
                    params={"name": entity_name},
                    headers=hdrs, timeout=20,
                )
                _dbg(f"Company lookup: HTTP {cr.status_code}")
                if cr.status_code == 200:
                    co_data = cr.json()
                    co = co_data.get("company") or co_data
                    co_id = co.get("id") if co else None
                    _dbg(f"Company id: {co_id}")
                    if co_id:
                        data2, dbg2 = _lookup_poll(
                            f"{RR_BASE}/person/lookup",
                            {"current_employer_id": co_id},
                            hdrs,
                        )
                        _dbg(f"Entity person lookup: {dbg2}")
                        if data2:
                            profile = data2.get("profile") or data2.get("person") or data2
                            if profile and profile.get("id"):
                                _fill(profile, "Entity / Company lookup", "Low")
                                return result
            except requests.RequestException as e:
                _dbg(f"Company lookup error: {e}")

    except requests.RequestException as exc:
        result["status"] = "error"
        result["error"]  = str(exc)

    return result


# ---------------------------------------------------------------------------
# Background enrichment worker
# ---------------------------------------------------------------------------

def _run_enrichment(job_id: str, df: pd.DataFrame, mapping: dict, api_key: str):
    total = len(df)
    results = []

    def _log(msg_type: str, msg: str):
        with _jobs_lock:
            _jobs[job_id]["log"].append({"type": msg_type, "msg": msg})

    with _jobs_lock:
        _jobs[job_id]["status"] = "running"
        _jobs[job_id]["total"]  = total

    for idx, row in df.iterrows():
        with _jobs_lock:
            if _jobs[job_id].get("cancel"):
                _jobs[job_id]["status"] = "cancelled"
                return

        row_num = int(idx) + 1  # type: ignore[arg-type]
        name_col = mapping.get("name", "")
        label = _safe(row.get(name_col, f"Row {row_num}")) if name_col else f"Row {row_num}"

        _log("info", f"[{row_num}/{total}] Processing: {label}")

        enriched = _enrich_one(api_key, row.to_dict(), mapping)

        row_out = row.to_dict()
        row_out["__email"]      = enriched.get("email", "")
        row_out["__phone"]      = enriched.get("phone", "")
        row_out["__source"]     = enriched.get("source", "")
        row_out["__confidence"] = enriched.get("confidence", "")
        results.append(row_out)

        if enriched["status"] == "found":
            parts = []
            if enriched.get("email"):
                parts.append(enriched["email"])
            if enriched.get("phone"):
                parts.append(enriched["phone"])
            _log("success", f"  ✓ {' | '.join(parts) or 'found (no contact)'}")
        elif enriched["status"] == "error":
            _log("error", f"  ✗ Error: {enriched.get('error', 'unknown')}")
        else:
            _log("warn", "  – Not found")
            for dbg in enriched.get("debug", []):
                _log("info", f"    {dbg}")

        with _jobs_lock:
            _jobs[job_id]["done"] = row_num
            _jobs[job_id]["results"] = results

        time.sleep(0.3)  # polite rate-limiting

    _log("success", f"Done! Processed {total} row(s).")
    with _jobs_lock:
        _jobs[job_id]["status"] = "complete"
        _jobs[job_id]["results"] = results


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    f = request.files["file"]
    if not f.filename or not f.filename.lower().endswith((".xlsx", ".xls")):
        return jsonify({"error": "Please upload an Excel file (.xlsx or .xls)"}), 400
    try:
        raw = f.read()
        df = pd.read_excel(BytesIO(raw), dtype=str)
        df = df.fillna("")
        columns = list(df.columns)

        job_id = str(uuid.uuid4())
        with _jobs_lock:
            _jobs[job_id] = {
                "status":  "uploaded",
                "df_json": df.to_json(orient="records"),
                "columns": columns,
                "row_count": len(df),
                "log":     [],
                "results": [],
                "done":    0,
                "total":   len(df),
                "cancel":  False,
            }
        return jsonify({"job_id": job_id, "columns": columns, "row_count": len(df)})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/enrich", methods=["POST"])
def enrich():
    data = request.get_json(force=True)
    job_id  = data.get("job_id", "")
    api_key = data.get("api_key", "").strip()
    mapping = data.get("mapping", {})

    if not job_id or job_id not in _jobs:
        return jsonify({"error": "Unknown job_id"}), 400
    if not api_key:
        return jsonify({"error": "API key required"}), 400

    with _jobs_lock:
        job = _jobs[job_id]
        if job["status"] == "running":
            return jsonify({"error": "Already running"}), 400
        df = pd.read_json(StringIO(job["df_json"]), orient="records", dtype=str)
        df = df.fillna("")
        job["log"]    = []
        job["results"] = []
        job["done"]   = 0
        job["cancel"] = False

    t = threading.Thread(target=_run_enrichment, args=(job_id, df, mapping, api_key), daemon=True)
    t.start()
    return jsonify({"status": "started"})


@app.route("/cancel/<job_id>", methods=["POST"])
def cancel(job_id: str):
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id]["cancel"] = True
    return jsonify({"ok": True})


@app.route("/stream/<job_id>")
def stream(job_id: str):
    """Server-Sent Events progress stream."""
    def generate():
        sent = 0
        while True:
            with _jobs_lock:
                if job_id not in _jobs:
                    yield f"data: {json.dumps({'type':'error','msg':'Job not found'})}\n\n"
                    return
                job  = _jobs[job_id]
                log  = job["log"]
                done = job["done"]
                total = job["total"]
                status = job["status"]

            # Flush unsent log lines
            while sent < len(log):
                yield f"data: {json.dumps(log[sent])}\n\n"
                sent += 1

            # Progress heartbeat
            yield f"data: {json.dumps({'type':'progress','done':done,'total':total,'status':status})}\n\n"

            if status in ("complete", "cancelled", "error"):
                return

            time.sleep(0.4)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/results/<job_id>")
def results(job_id: str):
    with _jobs_lock:
        if job_id not in _jobs:
            return jsonify({"error": "Job not found"}), 404
        r = _jobs[job_id].get("results", [])
    return jsonify({"results": r})


@app.route("/download/<job_id>")
def download(job_id: str):
    with _jobs_lock:
        if job_id not in _jobs:
            return jsonify({"error": "Job not found"}), 404
        results = list(_jobs[job_id].get("results", []))

    if not results:
        return jsonify({"error": "No results available"}), 400

    df = pd.DataFrame(results)
    rename = {
        "__email":      "Enriched Email",
        "__phone":      "Enriched Phone",
        "__source":     "Source",
        "__confidence": "Confidence",
    }
    df = df.rename(columns=rename)

    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Enriched")
    buf.seek(0)

    return send_file(
        buf,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name="enriched_owners.xlsx",
    )


if __name__ == "__main__":
    app.run(debug=False, port=5000, threaded=True)
