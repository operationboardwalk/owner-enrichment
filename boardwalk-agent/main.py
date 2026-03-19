"""
main.py — FastAPI backend for the Boardwalk Troubleshooting Agent.

Run with:
  uvicorn main:app --host 0.0.0.0 --port 8080 --reload
"""

import json
import logging
import requests as _requests
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

import config as cfg_module
import diagnostics
import fixes
import scheduler
import ai_analyst

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("boardwalk.main")

app = FastAPI(title="Boardwalk Troubleshooting Agent", version="1.0.0")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


# ──────────────────────────────────────────────────────────────────────────────
# Startup / shutdown
# ──────────────────────────────────────────────────────────────────────────────

@app.on_event("startup")
def on_startup():
    cfg = cfg_module.load()
    if cfg_module.is_setup_complete(cfg):
        scheduler.start(cfg.get("health_check_interval_minutes", 15))
        logger.info("Boardwalk troubleshooting agent started with scheduler")
    else:
        logger.info("Boardwalk agent started — setup required before monitoring begins")


@app.on_event("shutdown")
def on_shutdown():
    scheduler.stop()


# ──────────────────────────────────────────────────────────────────────────────
# Web pages
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    cfg = cfg_module.load()
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "setup_complete": cfg_module.is_setup_complete(cfg),
    })


# ──────────────────────────────────────────────────────────────────────────────
# API: Setup
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/api/config")
def get_config():
    cfg = cfg_module.load()
    # Mask sensitive values for display
    safe = {k: ("***" if "key" in k.lower() or "token" in k.lower() or "password" in k.lower() else v)
            for k, v in cfg.items()}
    safe["setup_complete"] = cfg_module.is_setup_complete(cfg)
    return safe


@app.post("/api/config")
async def save_config(request: Request):
    data = await request.json()
    cfg = cfg_module.update(data)
    # Restart scheduler with new interval if needed
    if cfg_module.is_setup_complete(cfg):
        scheduler.start(cfg.get("health_check_interval_minutes", 15))
    return {"success": True, "setup_complete": cfg_module.is_setup_complete(cfg)}


@app.get("/api/test-ssh")
def test_ssh():
    cfg = cfg_module.load()
    result = diagnostics.check_ssh_connectivity(cfg)
    return result


@app.get("/api/discover-droplets")
def discover_droplets():
    """List Digital Ocean droplets using the configured API token."""
    cfg = cfg_module.load()
    token = cfg.get("do_api_token", "")
    if not token:
        raise HTTPException(400, "No DO API token configured")
    try:
        r = _requests.get(
            "https://api.digitalocean.com/v2/droplets",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        if r.status_code == 200:
            droplets = r.json().get("droplets", [])
            return [{
                "id": d["id"],
                "name": d["name"],
                "status": d["status"],
                "ip": d.get("networks", {}).get("v4", [{}])[0].get("ip_address", ""),
            } for d in droplets]
        else:
            raise HTTPException(r.status_code, f"DO API error: {r.text[:200]}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/discover-boardwalk-paths")
def discover_boardwalk_paths():
    """SSH into the droplet and look for boardwalk-related directories."""
    cfg = cfg_module.load()
    from ssh_client import run_ssh, SSHError
    try:
        # Common paths to check
        out = run_ssh(cfg, """
find /root /home /app /opt /srv -maxdepth 3 \
  \\( -name 'boardwalk*' -o -name 'openclaw*' -o -name 'main.py' -o -name 'app.py' \\) \
  -not -path '*/node_modules/*' 2>/dev/null | head -20
""")
        paths = [p.strip() for p in out.strip().splitlines() if p.strip()]

        # Also look for log files
        log_out = run_ssh(cfg, "find /root /home /app /var/log -name '*.log' -newer /tmp 2>/dev/null | head -10 || true")
        logs = [p.strip() for p in log_out.strip().splitlines() if p.strip()]

        return {"paths": paths, "logs": logs}
    except SSHError as e:
        raise HTTPException(500, str(e))


# ──────────────────────────────────────────────────────────────────────────────
# API: Diagnostics
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/api/status")
def get_status():
    """Return the most recent diagnostic results (from scheduler cache)."""
    status = scheduler.load_last_status()
    cfg = cfg_module.load()
    return {
        "last_run": status.get("last_run"),
        "results": status.get("results", []),
        "setup_complete": cfg_module.is_setup_complete(cfg),
    }


@app.post("/api/diagnose")
def run_diagnose():
    """Trigger a fresh full diagnostic run right now."""
    results = scheduler.run_health_check()
    return {
        "last_run": scheduler.get_last_run(),
        "results": results,
    }


# ──────────────────────────────────────────────────────────────────────────────
# API: Fixes
# ──────────────────────────────────────────────────────────────────────────────

@app.post("/api/fix/{issue_id}")
def apply_fix(issue_id: str):
    cfg = cfg_module.load()
    result = fixes.apply_fix(issue_id, cfg)
    return result


@app.post("/api/restart")
def restart_boardwalk():
    cfg = cfg_module.load()
    result = fixes.fix_restart_boardwalk(cfg)
    return result


@app.post("/api/nudge")
async def nudge_boardwalk(request: Request):
    data = await request.json()
    message = data.get("message", "")
    cfg = cfg_module.load()
    result = fixes.fix_nudge_via_telegram(message=message or None, cfg=cfg)
    return result


@app.post("/api/fix/memory/reinit")
def reinit_memory():
    cfg = cfg_module.load()
    result = fixes.fix_memory_files(cfg=cfg, reinit=True)
    return result


@app.post("/api/apply-ai-fix")
async def apply_ai_fix_endpoint(request: Request):
    data = await request.json()
    command = data.get("command", "")
    description = data.get("description", "AI-suggested fix")
    if not command:
        raise HTTPException(400, "No command provided")
    cfg = cfg_module.load()
    result = fixes.apply_ai_fix(command, description, cfg)
    return result


# ──────────────────────────────────────────────────────────────────────────────
# API: AI Analysis
# ──────────────────────────────────────────────────────────────────────────────

@app.post("/api/analyze")
async def analyze_problem(request: Request):
    """
    Main AI troubleshooting endpoint.
    Body: { "problem": "describe what's wrong" }
    Returns AI diagnosis + suggested fixes.
    """
    data = await request.json()
    problem = data.get("problem", "").strip()
    if not problem:
        raise HTTPException(400, "Please describe the problem")

    cfg = cfg_module.load()
    if not cfg_module.is_setup_complete(cfg):
        raise HTTPException(400, "Setup not complete — please configure credentials first")

    # Collect fresh context from boardwalk's server
    try:
        raw_context = diagnostics.collect_raw_context(cfg)
    except Exception as e:
        raw_context = f"Could not collect SSH context: {e}"

    # Get diagnostic results
    try:
        diag_results = diagnostics.run_all(cfg)
    except Exception:
        diag_results = []

    # Ask AI
    try:
        analysis = ai_analyst.analyze(problem, raw_context, diag_results, cfg)
        return analysis
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/verify-fix")
async def verify_fix_endpoint(request: Request):
    """After applying a fix, ask AI if it worked."""
    data = await request.json()
    fix_description = data.get("fix_description", "")
    cfg = cfg_module.load()
    try:
        raw_context = diagnostics.collect_raw_context(cfg)
        result = ai_analyst.verify_fix(fix_description, raw_context, cfg)
        return result
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/chat")
async def chat(request: Request):
    """Quick AI chat without full context collection."""
    data = await request.json()
    question = data.get("question", "").strip()
    if not question:
        raise HTTPException(400, "No question provided")
    cfg = cfg_module.load()
    try:
        answer = ai_analyst.quick_answer(question, cfg)
        return {"answer": answer}
    except Exception as e:
        raise HTTPException(500, str(e))


# ──────────────────────────────────────────────────────────────────────────────
# API: History
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/api/history")
def get_history():
    return fixes.load_history()


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=True)
