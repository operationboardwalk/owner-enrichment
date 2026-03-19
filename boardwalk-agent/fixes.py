"""
fixes.py — Auto-fix engine for known boardwalk issues.

Each fix function:
  - Takes cfg dict + optional extra args
  - Runs SSH commands to apply the fix
  - Returns {"success": bool, "message": str, "details": str}
"""

import json
import time
import datetime
from pathlib import Path

import config as cfg_module
from ssh_client import run_ssh, run_ssh_script, SSHError

HISTORY_PATH = Path(__file__).parent / "fix_history.json"


# ──────────────────────────────────────────────────────────────────────────────
# History logging
# ──────────────────────────────────────────────────────────────────────────────

def _log_fix(issue: str, action: str, result: dict) -> None:
    history = []
    if HISTORY_PATH.exists():
        try:
            with open(HISTORY_PATH) as f:
                history = json.load(f)
        except Exception:
            history = []

    history.append({
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "issue": issue,
        "action": action,
        "success": result.get("success", False),
        "message": result.get("message", ""),
        "details": result.get("details", "")[:500],
    })

    # Keep last 200 entries
    history = history[-200:]
    with open(HISTORY_PATH, "w") as f:
        json.dump(history, f, indent=2)


def _result(success: bool, message: str, details: str = "") -> dict:
    return {"success": success, "message": message, "details": details}


# ──────────────────────────────────────────────────────────────────────────────
# Fix: restart boardwalk process
# ──────────────────────────────────────────────────────────────────────────────

def fix_restart_boardwalk(cfg: dict = None) -> dict:
    if cfg is None:
        cfg = cfg_module.load()

    service = cfg.get("boardwalk_service_name", "")
    start_cmd = cfg.get("boardwalk_start_cmd", "")
    bdir = cfg.get("boardwalk_dir", "")

    try:
        if service:
            out = run_ssh(cfg, f"systemctl restart '{service}' 2>&1 && echo 'OK'")
            success = "OK" in out or "ok" in out.lower()
            result = _result(success, f"Restarted systemd service '{service}'", out)
        elif start_cmd:
            # Kill existing, start fresh in background
            pname = cfg.get("boardwalk_process_name", "boardwalk")
            kill_out = run_ssh(cfg, f"pkill -f '{pname}' 2>/dev/null || true; sleep 2")
            start_out = run_ssh(cfg, f"nohup bash -c '{start_cmd}' > /tmp/boardwalk_restart.log 2>&1 & echo $!")
            pid = start_out.strip()
            result = _result(bool(pid), f"Restarted boardwalk (PID {pid})", f"Kill: {kill_out}\nStart: {start_out}")
        elif bdir:
            # Try common startup patterns
            script = f"""
cd '{bdir}'
pkill -f boardwalk 2>/dev/null || true
sleep 2
if [ -f main.py ]; then
  nohup python main.py > boardwalk.log 2>&1 & echo "Started main.py PID=$!"
elif [ -f app.py ]; then
  nohup python app.py > boardwalk.log 2>&1 & echo "Started app.py PID=$!"
elif [ -f index.js ]; then
  nohup node index.js > boardwalk.log 2>&1 & echo "Started index.js PID=$!"
else
  echo "No startup file found"
fi
"""
            out = run_ssh_script(cfg, script)
            success = "Started" in out or "PID" in out
            result = _result(success, "Attempted to restart boardwalk", out)
        else:
            result = _result(False, "Cannot restart: no service name, start command, or directory configured")

        _log_fix("process_down", "restart_boardwalk", result)
        return result
    except SSHError as e:
        result = _result(False, f"SSH error during restart: {e}")
        _log_fix("process_down", "restart_boardwalk", result)
        return result


# ──────────────────────────────────────────────────────────────────────────────
# Fix: address API rate limits
# ──────────────────────────────────────────────────────────────────────────────

def fix_api_rate_limits(cfg: dict = None) -> dict:
    """Add/increase delay between API calls in boardwalk's config, then restart."""
    if cfg is None:
        cfg = cfg_module.load()

    bdir = cfg.get("boardwalk_dir", "")
    if not bdir:
        return _result(False, "Cannot fix rate limits: no boardwalk directory configured")

    try:
        # Look for config files that might have delay/rate settings
        find_out = run_ssh(cfg, f"find '{bdir}' -name '*.json' -o -name '*.env' -o -name '*.yaml' -o -name '*.yml' 2>/dev/null | grep -v node_modules | head -10")
        configs = [f.strip() for f in find_out.strip().splitlines() if f.strip()]

        details = []
        patched = False

        for conf in configs:
            content = run_ssh(cfg, f"cat '{conf}' 2>/dev/null || echo ''")
            if not content.strip():
                continue
            # Look for rate/delay/sleep related settings
            import re
            if re.search(r'(rate_limit|delay|sleep|retry|backoff)', content, re.IGNORECASE):
                details.append(f"Found rate-related settings in {conf}")
                patched = True

        # Add a .env override or create a rate-limit patch note
        patch_file = f"{bdir.rstrip('/')}/.rate_limit_note.txt"
        note = (
            "Rate limit fix applied by boardwalk troubleshooter.\n"
            "Recommended: add exponential backoff to all OpenAI API calls.\n"
            "Pattern: wait 2^retry_count seconds between retries (up to 60s max).\n"
            f"Applied at: {datetime.datetime.utcnow().isoformat()}Z\n"
        )
        run_ssh(cfg, f"echo '{note}' > '{patch_file}'")
        details.append(f"Rate limit guidance note written to {patch_file}")

        # Attempt to patch .env if it exists
        env_file = f"{bdir.rstrip('/')}/.env"
        env_exists = run_ssh(cfg, f"test -f '{env_file}' && echo yes || echo no").strip()
        if env_exists == "yes":
            current_env = run_ssh(cfg, f"cat '{env_file}'")
            if "OPENAI_RETRY_DELAY" not in current_env:
                run_ssh(cfg, f"echo 'OPENAI_RETRY_DELAY=2' >> '{env_file}'")
                run_ssh(cfg, f"echo 'OPENAI_MAX_RETRIES=5' >> '{env_file}'")
                details.append("Added OPENAI_RETRY_DELAY=2 and OPENAI_MAX_RETRIES=5 to .env")
                patched = True

        message = "Rate limit mitigation applied — restart boardwalk to take effect" if patched else "Config scanned; manual review recommended"
        result = _result(True, message, "\n".join(details))
        _log_fix("api_rate_limits", "patch_rate_limits", result)
        return result
    except SSHError as e:
        result = _result(False, f"SSH error: {e}")
        _log_fix("api_rate_limits", "patch_rate_limits", result)
        return result


# ──────────────────────────────────────────────────────────────────────────────
# Fix: repair memory files
# ──────────────────────────────────────────────────────────────────────────────

def fix_memory_files(cfg: dict = None, reinit: bool = False) -> dict:
    """Backup corrupted JSON files and optionally re-initialize them."""
    if cfg is None:
        cfg = cfg_module.load()

    bdir = cfg.get("boardwalk_dir", "")
    if not bdir:
        return _result(False, "No boardwalk directory configured")

    try:
        import json as _json

        find_out = run_ssh(cfg, f"find '{bdir}' -name '*.json' -not -path '*/node_modules/*' 2>/dev/null | head -20")
        json_files = [f.strip() for f in find_out.strip().splitlines() if f.strip()]

        fixed = []
        errors = []
        backup_dir = f"{bdir.rstrip('/')}/memory_backup_{int(time.time())}"
        run_ssh(cfg, f"mkdir -p '{backup_dir}'")

        for jf in json_files:
            content = run_ssh(cfg, f"cat '{jf}' 2>/dev/null || echo ''")
            if not content.strip():
                # Empty file — back it up and init
                run_ssh(cfg, f"cp '{jf}' '{backup_dir}/' 2>/dev/null || true")
                if reinit:
                    run_ssh(cfg, f"echo '{{}}' > '{jf}'")
                    fixed.append(f"{jf}: was empty → initialized to {{}}")
                else:
                    errors.append(f"{jf}: empty (not modified — pass reinit=True to fix)")
                continue

            try:
                _json.loads(content)
                # Valid — nothing to do
            except _json.JSONDecodeError as e:
                # Corrupted — back up and optionally reinit
                fname = jf.split("/")[-1]
                run_ssh(cfg, f"cp '{jf}' '{backup_dir}/{fname}.bak' 2>/dev/null || true")
                if reinit:
                    run_ssh(cfg, f"echo '{{}}' > '{jf}'")
                    fixed.append(f"{jf}: corrupted ({e}) → reset to {{}}")
                else:
                    errors.append(f"{jf}: corrupted ({e}) — backed up but not modified")

        details = []
        if fixed:
            details.append(f"Fixed {len(fixed)} file(s):\n" + "\n".join(fixed))
        if errors:
            details.append(f"Issues found (not auto-fixed):\n" + "\n".join(errors))
        details.append(f"Backups saved to: {backup_dir}")

        success = len(errors) == 0 or reinit
        msg = f"Memory repair complete: {len(fixed)} fixed, {len(errors)} need review"
        result = _result(success, msg, "\n\n".join(details))
        _log_fix("memory_files", "repair_memory" if not reinit else "reinit_memory", result)
        return result
    except SSHError as e:
        result = _result(False, f"SSH error: {e}")
        _log_fix("memory_files", "repair_memory", result)
        return result


# ──────────────────────────────────────────────────────────────────────────────
# Fix: clear stuck/incomplete tasks
# ──────────────────────────────────────────────────────────────────────────────

def fix_incomplete_tasks(cfg: dict = None) -> dict:
    """Remove lock files and in-progress markers so boardwalk can restart tasks."""
    if cfg is None:
        cfg = cfg_module.load()

    bdir = cfg.get("boardwalk_dir", "")
    if not bdir:
        return _result(False, "No boardwalk directory configured")

    try:
        # Find and remove lock files
        lock_out = run_ssh(cfg, f"find '{bdir}' \\( -name '*.lock' -o -name '*in_progress*' \\) 2>/dev/null")
        locks = [f.strip() for f in lock_out.strip().splitlines() if f.strip()]

        removed = []
        for lf in locks:
            run_ssh(cfg, f"rm -f '{lf}' 2>/dev/null || true")
            removed.append(lf)

        msg = f"Removed {len(removed)} lock/in-progress file(s)" if removed else "No lock files found"
        result = _result(True, msg, "\n".join(removed) if removed else "Nothing to clear")
        _log_fix("incomplete_tasks", "clear_locks", result)
        return result
    except SSHError as e:
        result = _result(False, f"SSH error: {e}")
        _log_fix("incomplete_tasks", "clear_locks", result)
        return result


# ──────────────────────────────────────────────────────────────────────────────
# Fix: clean disk space
# ──────────────────────────────────────────────────────────────────────────────

def fix_disk_space(cfg: dict = None) -> dict:
    """Clean temp files and truncate old logs to free disk space."""
    if cfg is None:
        cfg = cfg_module.load()

    try:
        script = """
# Clean /tmp files older than 7 days
find /tmp -mtime +7 -delete 2>/dev/null || true

# Clean apt cache
apt-get clean -y 2>/dev/null || true

# Remove old journal logs (keep last 100MB)
journalctl --vacuum-size=100M 2>/dev/null || true

# Show freed space
df -h /
"""
        out = run_ssh_script(cfg, script)
        result = _result(True, "Disk cleanup complete", out)
        _log_fix("disk_space", "clean_disk", result)
        return result
    except SSHError as e:
        result = _result(False, f"SSH error: {e}")
        _log_fix("disk_space", "clean_disk", result)
        return result


# ──────────────────────────────────────────────────────────────────────────────
# Fix: nudge boardwalk via Telegram
# ──────────────────────────────────────────────────────────────────────────────

def fix_nudge_via_telegram(message: str = None, cfg: dict = None) -> dict:
    """Send a Telegram message to boardwalk's chat to prompt autonomous action."""
    import requests as _req

    if cfg is None:
        cfg = cfg_module.load()

    token = cfg.get("telegram_bot_token", "")
    chat_id = cfg.get("telegram_chat_id", "")

    if not token or not chat_id:
        return _result(False, "Telegram bot token or chat ID not configured")

    if not message:
        message = (
            "Hey boardwalk! Please review your current task queue and continue any pending work. "
            "If you have nothing scheduled, proactively check what's next on the agenda and get started."
        )

    try:
        r = _req.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=10,
        )
        if r.status_code == 200 and r.json().get("ok"):
            result = _result(True, f"Nudge sent to boardwalk via Telegram", message)
        else:
            result = _result(False, f"Telegram send failed: {r.status_code}", r.text[:300])

        _log_fix("autonomy_idle", "telegram_nudge", result)
        return result
    except Exception as e:
        result = _result(False, f"Telegram error: {e}")
        _log_fix("autonomy_idle", "telegram_nudge", result)
        return result


# ──────────────────────────────────────────────────────────────────────────────
# Fix: run an AI-generated shell command
# ──────────────────────────────────────────────────────────────────────────────

def apply_ai_fix(command: str, fix_description: str, cfg: dict = None) -> dict:
    """Execute an AI-suggested shell command on the droplet."""
    if cfg is None:
        cfg = cfg_module.load()

    try:
        out = run_ssh(cfg, command, timeout=60)
        result = _result(True, f"AI fix applied: {fix_description}", out[:1000])
        _log_fix("ai_suggested", command[:100], result)
        return result
    except SSHError as e:
        result = _result(False, f"SSH error executing AI fix: {e}")
        _log_fix("ai_suggested", command[:100], result)
        return result


# ──────────────────────────────────────────────────────────────────────────────
# Dispatcher: map issue IDs to fix functions
# ──────────────────────────────────────────────────────────────────────────────

ISSUE_FIX_MAP = {
    "process_running": fix_restart_boardwalk,
    "api_rate_limits": fix_api_rate_limits,
    "memory_files": fix_memory_files,
    "incomplete_tasks": fix_incomplete_tasks,
    "disk_space": fix_disk_space,
}


def apply_fix(issue_id: str, cfg: dict = None, **kwargs) -> dict:
    """Apply the appropriate fix for a given issue ID."""
    fn = ISSUE_FIX_MAP.get(issue_id)
    if not fn:
        return _result(False, f"No auto-fix available for issue '{issue_id}'")
    return fn(cfg=cfg, **kwargs)


def load_history() -> list:
    if HISTORY_PATH.exists():
        try:
            with open(HISTORY_PATH) as f:
                return json.load(f)
        except Exception:
            return []
    return []
