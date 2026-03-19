"""
diagnostics.py — Health checks for the boardwalk agent.

Each check returns a dict:
  {
    "id": str,           # unique check identifier
    "name": str,         # human-readable name
    "status": str,       # "ok" | "warning" | "critical" | "unknown"
    "message": str,      # short summary
    "details": str,      # raw output or extended info
  }
"""

import re
import time
import json
import socket
import requests as _requests
from datetime import datetime, timezone

import config as cfg_module
from ssh_client import run_ssh, SSHError


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _ok(id_, name, message, details=""):
    return {"id": id_, "name": name, "status": "ok", "message": message, "details": details}

def _warn(id_, name, message, details=""):
    return {"id": id_, "name": name, "status": "warning", "message": message, "details": details}

def _crit(id_, name, message, details=""):
    return {"id": id_, "name": name, "status": "critical", "message": message, "details": details}

def _unknown(id_, name, message, details=""):
    return {"id": id_, "name": name, "status": "unknown", "message": message, "details": details}


# ──────────────────────────────────────────────────────────────────────────────
# Individual checks
# ──────────────────────────────────────────────────────────────────────────────

def check_droplet_status(cfg: dict) -> dict:
    """Use Digital Ocean API to verify the droplet is powered on."""
    token = cfg.get("do_api_token", "")
    droplet_id = cfg.get("droplet_id", "")

    if not token:
        return _unknown("droplet_status", "Droplet Status", "No DO API token configured")
    if not droplet_id:
        return _unknown("droplet_status", "Droplet Status", "No droplet ID configured")

    try:
        r = _requests.get(
            f"https://api.digitalocean.com/v2/droplets/{droplet_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json().get("droplet", {})
            status = data.get("status", "unknown")
            name = data.get("name", droplet_id)
            if status == "active":
                return _ok("droplet_status", "Droplet Status", f"Droplet '{name}' is active", str(data.get("networks", "")))
            else:
                return _crit("droplet_status", "Droplet Status", f"Droplet '{name}' status: {status}")
        else:
            return _warn("droplet_status", "Droplet Status", f"DO API returned {r.status_code}", r.text[:300])
    except Exception as e:
        return _unknown("droplet_status", "Droplet Status", f"DO API error: {e}")


def check_ssh_connectivity(cfg: dict) -> dict:
    """Verify we can open an SSH connection to the droplet."""
    ip = cfg.get("droplet_ip", "")
    if not ip:
        return _unknown("ssh_connectivity", "SSH Connectivity", "No droplet IP configured")
    try:
        run_ssh(cfg, "echo ok", timeout=10)
        return _ok("ssh_connectivity", "SSH Connectivity", f"SSH to {ip} successful")
    except SSHError as e:
        return _crit("ssh_connectivity", "SSH Connectivity", f"SSH failed: {e}")
    except Exception as e:
        return _unknown("ssh_connectivity", "SSH Connectivity", f"SSH error: {e}")


def check_process_running(cfg: dict) -> dict:
    """Check if boardwalk process is running via ps aux."""
    pname = cfg.get("boardwalk_process_name", "boardwalk")
    try:
        out = run_ssh(cfg, f"ps aux | grep -i '{pname}' | grep -v grep || true")
        if out.strip():
            lines = out.strip().splitlines()
            return _ok("process_running", "Boardwalk Process", f"Running ({len(lines)} process(es) found)", out.strip())
        else:
            return _crit("process_running", "Boardwalk Process", f"No '{pname}' process found — boardwalk is DOWN")
    except SSHError as e:
        return _unknown("process_running", "Boardwalk Process", f"SSH error: {e}")


def check_disk_space(cfg: dict) -> dict:
    """Check disk usage on the droplet."""
    try:
        out = run_ssh(cfg, "df -h / 2>/dev/null | tail -1")
        # Example: /dev/vda1   25G  18G  6.5G  74% /
        match = re.search(r'(\d+)%', out)
        if match:
            pct = int(match.group(1))
            if pct >= 90:
                return _crit("disk_space", "Disk Space", f"Disk {pct}% full — CRITICAL", out.strip())
            elif pct >= 75:
                return _warn("disk_space", "Disk Space", f"Disk {pct}% full", out.strip())
            else:
                return _ok("disk_space", "Disk Space", f"Disk {pct}% used", out.strip())
        return _ok("disk_space", "Disk Space", "Disk usage OK", out.strip())
    except SSHError as e:
        return _unknown("disk_space", "Disk Space", f"SSH error: {e}")


def check_memory_usage(cfg: dict) -> dict:
    """Check RAM usage on the droplet."""
    try:
        out = run_ssh(cfg, "free -m | awk '/^Mem:/{print $2,$3}'")
        parts = out.strip().split()
        if len(parts) == 2:
            total, used = int(parts[0]), int(parts[1])
            pct = int(used / total * 100) if total else 0
            if pct >= 90:
                return _crit("memory_usage", "RAM Usage", f"RAM {pct}% used ({used}MB/{total}MB)")
            elif pct >= 75:
                return _warn("memory_usage", "RAM Usage", f"RAM {pct}% used ({used}MB/{total}MB)")
            else:
                return _ok("memory_usage", "RAM Usage", f"RAM {pct}% used ({used}MB/{total}MB)")
        return _ok("memory_usage", "RAM Usage", "RAM usage OK", out.strip())
    except SSHError as e:
        return _unknown("memory_usage", "RAM Usage", f"SSH error: {e}")


def check_api_rate_limits(cfg: dict) -> dict:
    """Scan boardwalk logs for OpenAI rate limit (429) errors."""
    log_file = cfg.get("boardwalk_log_file", "")
    if not log_file:
        # Try to auto-discover
        bdir = cfg.get("boardwalk_dir", "")
        if bdir:
            log_file = f"{bdir.rstrip('/')}/boardwalk.log"
        else:
            return _unknown("api_rate_limits", "API Rate Limits", "No log file configured")

    try:
        # Get last 500 lines, count 429/rate limit occurrences
        out = run_ssh(cfg, f"tail -500 '{log_file}' 2>/dev/null || echo '__NO_LOG__'")
        if "__NO_LOG__" in out or not out.strip():
            return _unknown("api_rate_limits", "API Rate Limits", f"Log file not found: {log_file}")

        rate_limit_hits = len(re.findall(r'(429|rate.?limit|RateLimitError|quota)', out, re.IGNORECASE))
        if rate_limit_hits >= 10:
            return _crit("api_rate_limits", "API Rate Limits", f"{rate_limit_hits} rate limit errors in recent logs", out[-2000:])
        elif rate_limit_hits >= 3:
            return _warn("api_rate_limits", "API Rate Limits", f"{rate_limit_hits} rate limit errors in recent logs", out[-2000:])
        else:
            return _ok("api_rate_limits", "API Rate Limits", f"No significant rate limit errors ({rate_limit_hits} hits)", "")
    except SSHError as e:
        return _unknown("api_rate_limits", "API Rate Limits", f"SSH error: {e}")


def check_memory_files(cfg: dict) -> dict:
    """Check that OpenClaw memory files exist and are valid JSON."""
    bdir = cfg.get("boardwalk_dir", "")
    if not bdir:
        return _unknown("memory_files", "Memory Files", "No boardwalk directory configured")

    try:
        # List files in boardwalk dir
        out = run_ssh(cfg, f"find '{bdir}' -name '*.json' -not -path '*/node_modules/*' 2>/dev/null | head -20")
        json_files = [f.strip() for f in out.strip().splitlines() if f.strip()]

        if not json_files:
            return _warn("memory_files", "Memory Files", f"No JSON files found in {bdir}")

        # Check each JSON file for validity
        broken = []
        for jf in json_files[:10]:  # limit to 10 files
            content = run_ssh(cfg, f"cat '{jf}' 2>/dev/null || echo '__READ_ERROR__'")
            if "__READ_ERROR__" in content or not content.strip():
                broken.append(f"{jf}: empty or unreadable")
                continue
            try:
                json.loads(content)
            except json.JSONDecodeError as je:
                broken.append(f"{jf}: invalid JSON — {je}")

        if broken:
            return _crit("memory_files", "Memory Files", f"{len(broken)} corrupted memory file(s)", "\n".join(broken))
        return _ok("memory_files", "Memory Files", f"{len(json_files)} memory file(s) valid", "\n".join(json_files))
    except SSHError as e:
        return _unknown("memory_files", "Memory Files", f"SSH error: {e}")


def check_incomplete_tasks(cfg: dict) -> dict:
    """Look for in-progress task markers that suggest incomplete work."""
    bdir = cfg.get("boardwalk_dir", "")
    log_file = cfg.get("boardwalk_log_file", "")
    if not bdir and not log_file:
        return _unknown("incomplete_tasks", "Incomplete Tasks", "No boardwalk directory or log configured")

    findings = []

    try:
        if bdir:
            # Look for lock files or in-progress markers
            lock_out = run_ssh(cfg, f"find '{bdir}' \\( -name '*.lock' -o -name '*in_progress*' -o -name '*pending*' \\) 2>/dev/null | head -10")
            if lock_out.strip():
                findings.append(f"Lock/in-progress files found:\n{lock_out.strip()}")

        if log_file:
            # Look for started-but-not-finished patterns
            log_out = run_ssh(cfg, f"tail -200 '{log_file}' 2>/dev/null | grep -i 'starting\\|began\\|working on' | tail -5 || true")
            done_out = run_ssh(cfg, f"tail -200 '{log_file}' 2>/dev/null | grep -i 'completed\\|finished\\|done' | tail -5 || true")
            started = len(log_out.strip().splitlines()) if log_out.strip() else 0
            done = len(done_out.strip().splitlines()) if done_out.strip() else 0
            if started > done:
                findings.append(f"Possibly {started - done} task(s) started but not completed in recent logs")

        if findings:
            return _warn("incomplete_tasks", "Incomplete Tasks", f"{len(findings)} incomplete task indicator(s) found", "\n\n".join(findings))
        return _ok("incomplete_tasks", "Incomplete Tasks", "No stuck tasks detected")
    except SSHError as e:
        return _unknown("incomplete_tasks", "Incomplete Tasks", f"SSH error: {e}")


def check_last_activity(cfg: dict) -> dict:
    """Check when boardwalk was last active via log timestamps."""
    log_file = cfg.get("boardwalk_log_file", "")
    bdir = cfg.get("boardwalk_dir", "")
    if not log_file and bdir:
        log_file = f"{bdir.rstrip('/')}/boardwalk.log"
    if not log_file:
        return _unknown("last_activity", "Last Activity", "No log file configured")

    try:
        # Get last line of log
        out = run_ssh(cfg, f"tail -1 '{log_file}' 2>/dev/null || echo '__NO_LOG__'")
        if "__NO_LOG__" in out or not out.strip():
            return _unknown("last_activity", "Last Activity", "Log file not found or empty")

        # Try to extract a timestamp from the last line
        # Common formats: 2024-01-15 10:30:00, Jan 15 10:30:00, [2024-01-15T10:30:00]
        ts_match = re.search(r'(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})', out)
        if ts_match:
            ts_str = ts_match.group(1).replace("T", " ")
            try:
                ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                now = datetime.utcnow()
                delta_minutes = (now - ts).total_seconds() / 60
                if delta_minutes > 60:
                    return _warn("last_activity", "Last Activity", f"Last log entry {int(delta_minutes)} min ago — boardwalk may be idle", out.strip())
                return _ok("last_activity", "Last Activity", f"Last activity {int(delta_minutes)} min ago", out.strip())
            except ValueError:
                pass

        # Fallback: check file modification time
        mtime_out = run_ssh(cfg, f"stat -c '%Y' '{log_file}' 2>/dev/null || echo 0")
        try:
            mtime = int(mtime_out.strip())
            now_ts = int(time.time())
            delta_minutes = (now_ts - mtime) / 60
            if delta_minutes > 60:
                return _warn("last_activity", "Last Activity", f"Log last modified {int(delta_minutes)} min ago")
            return _ok("last_activity", "Last Activity", f"Log last modified {int(delta_minutes)} min ago")
        except ValueError:
            return _ok("last_activity", "Last Activity", "Active (log exists)", out.strip())
    except SSHError as e:
        return _unknown("last_activity", "Last Activity", f"SSH error: {e}")


def check_telegram_responsiveness(cfg: dict) -> dict:
    """Check if boardwalk's Telegram bot is reachable (getMe API call)."""
    token = cfg.get("telegram_bot_token", "")
    if not token:
        return _unknown("telegram", "Telegram Bot", "No Telegram bot token configured")

    try:
        r = _requests.get(
            f"https://api.telegram.org/bot{token}/getMe",
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            if data.get("ok"):
                bot_name = data["result"].get("username", "unknown")
                return _ok("telegram", "Telegram Bot", f"Bot @{bot_name} is reachable")
            else:
                return _warn("telegram", "Telegram Bot", f"Bot API returned not-ok: {data}")
        elif r.status_code == 401:
            return _crit("telegram", "Telegram Bot", "Invalid Telegram bot token")
        else:
            return _warn("telegram", "Telegram Bot", f"Telegram API returned {r.status_code}", r.text[:300])
    except Exception as e:
        return _unknown("telegram", "Telegram Bot", f"Telegram API error: {e}")


# ──────────────────────────────────────────────────────────────────────────────
# Run all checks
# ──────────────────────────────────────────────────────────────────────────────

ALL_CHECKS = [
    check_droplet_status,
    check_ssh_connectivity,
    check_process_running,
    check_disk_space,
    check_memory_usage,
    check_api_rate_limits,
    check_memory_files,
    check_incomplete_tasks,
    check_last_activity,
    check_telegram_responsiveness,
]


def run_all(cfg: dict = None) -> list:
    """Run all diagnostic checks and return a list of results."""
    if cfg is None:
        cfg = cfg_module.load()
    results = []
    for check_fn in ALL_CHECKS:
        try:
            result = check_fn(cfg)
        except Exception as e:
            result = _unknown(
                check_fn.__name__.replace("check_", ""),
                check_fn.__name__.replace("check_", "").replace("_", " ").title(),
                f"Unexpected error: {e}",
            )
        results.append(result)
    return results


def collect_raw_context(cfg: dict = None) -> str:
    """Collect comprehensive raw context from boardwalk for AI analysis."""
    if cfg is None:
        cfg = cfg_module.load()

    sections = []

    def ssh(cmd, label):
        try:
            out = run_ssh(cfg, cmd, timeout=15)
            sections.append(f"=== {label} ===\n{out.strip()}")
        except Exception as e:
            sections.append(f"=== {label} ===\n[Error: {e}]")

    ssh("uptime", "System Uptime")
    ssh("free -m", "Memory Usage")
    ssh("df -h", "Disk Usage")
    ssh("ps aux | head -30", "Running Processes (top 30)")

    pname = cfg.get("boardwalk_process_name", "boardwalk")
    ssh(f"ps aux | grep -i '{pname}' | grep -v grep || echo 'NOT RUNNING'", "Boardwalk Process")

    log_file = cfg.get("boardwalk_log_file", "")
    bdir = cfg.get("boardwalk_dir", "")
    if not log_file and bdir:
        log_file = f"{bdir.rstrip('/')}/boardwalk.log"

    if log_file:
        ssh(f"tail -500 '{log_file}' 2>/dev/null || echo 'Log not found'", "Recent Logs (last 500 lines)")

    if bdir:
        ssh(f"ls -la '{bdir}' 2>/dev/null || echo 'Dir not found'", "Boardwalk Directory Contents")
        ssh(f"find '{bdir}' -name '*.json' -not -path '*/node_modules/*' | head -10 | xargs -I{{}} sh -c 'echo \"--- {{}} ---\"; cat \"{{}}\" 2>/dev/null | head -50' 2>/dev/null || echo 'No JSON files'", "Memory/Config JSON Files")
        ssh(f"find '{bdir}' -name '*.env' -o -name 'config.*' | head -5 | xargs -I{{}} sh -c 'echo \"--- {{}} ---\"; cat \"{{}}\" 2>/dev/null' 2>/dev/null || true", "Config Files")

    return "\n\n".join(sections)
