"""
config.py — Credential storage and loading for the Boardwalk Troubleshooting Agent.

Credentials are stored in config.json (gitignored). This module provides
helpers to load, save, and validate configuration.
"""

import json
import os
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "config.json"

DEFAULT_CONFIG = {
    # Digital Ocean
    "do_api_token": "",
    "droplet_id": "",
    "droplet_ip": "",
    "droplet_name": "",

    # SSH
    "ssh_user": "root",
    "ssh_password": "",
    "ssh_key_path": "",   # path to private key file, e.g. ~/.ssh/id_rsa
    "ssh_port": 22,

    # Boardwalk process / paths on the droplet
    "boardwalk_process_name": "boardwalk",   # name to grep for in ps aux
    "boardwalk_dir": "",                      # e.g. /root/boardwalk
    "boardwalk_log_file": "",                 # e.g. /root/boardwalk/boardwalk.log
    "boardwalk_start_cmd": "",               # e.g. "cd /root/boardwalk && python main.py"
    "boardwalk_service_name": "",            # systemd service name if applicable

    # Telegram
    "telegram_bot_token": "",   # boardwalk's telegram bot token
    "telegram_chat_id": "",     # your chat ID with boardwalk

    # AI analyst (choose one)
    "ai_provider": "openai",    # "openai" or "anthropic"
    "openai_api_key": "",
    "anthropic_api_key": "",
    "ai_model": "gpt-4o",       # model to use for analysis

    # Scheduler
    "health_check_interval_minutes": 15,
    "auto_fix_critical": True,  # auto-fix critical issues without user confirmation
}


def load() -> dict:
    """Load config from config.json, merging with defaults."""
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            saved = json.load(f)
        cfg = {**DEFAULT_CONFIG, **saved}
    else:
        cfg = dict(DEFAULT_CONFIG)
    return cfg


def save(cfg: dict) -> None:
    """Save config to config.json."""
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def is_setup_complete(cfg: dict) -> bool:
    """Return True if the minimum credentials are filled in."""
    has_ssh = bool(cfg.get("droplet_ip")) and (
        bool(cfg.get("ssh_password")) or bool(cfg.get("ssh_key_path"))
    )
    has_ai = bool(cfg.get("openai_api_key")) or bool(cfg.get("anthropic_api_key"))
    return has_ssh and has_ai


def update(updates: dict) -> dict:
    """Merge updates into config, save, and return the new config."""
    cfg = load()
    cfg.update(updates)
    save(cfg)
    return cfg
