"""Settings and where things live on disk (~/.subtle, or $SUBTLE_HOME)."""
from __future__ import annotations

import json
import os
from pathlib import Path

from .providers import DEFAULT_MODEL

DEFAULTS = {
    "target_code": "",
    "target_name": "",
    "model": DEFAULT_MODEL,
    "escalate_model": "",  # stronger model for lines the main one fails on; "" = none
    "register": "auto",  # auto | informal | formal
    "sdh": "keep",  # keep | strip  (sound descriptions and speaker labels)
    "lyrics": "keep",  # keep | translate
    "max_chars": 42,
    "max_cps": 21,
    "chunk_lines": 120,
    "notes": "",
    "glossary": [],  # [[source term, translation], ...]
    "budget": 0.0,  # USD cap per file; 0 = automatic (4x the estimate)
    "proofread": False,  # read every finished translation once more and correct what needs it
    "proof_model": "",  # the model that does the reading; "" = the one that translated the file
    "concurrency": 3,
    "save_next_to_source": True,
    "bilingual": False,
    "api_key": "",
    # automation
    "webhook_secret": "",  # generated on first run; part of the Sonarr/Radarr webhook URL
    "path_maps": [],  # [[path as Sonarr/Radarr report it, same folder on this machine], ...]
    "sonarr_url": "",
    "sonarr_key": "",
    "radarr_url": "",
    "radarr_key": "",
    "reach_url": "",  # address Sonarr/Radarr use to reach Subtle; "" = worked out when connecting
    "setup_seen": False,  # the first-run screen has been closed once
    "bazarr_url": "",
    "bazarr_key": "",
    "bazarr_wait": 0,  # minutes Bazarr gets to find real subtitles before a file is translated
    "auto_daily_budget": 1.0,  # USD per day for translations nobody clicked; 0 = no limit
}


def home() -> Path:
    path = Path(os.environ.get("SUBTLE_HOME") or Path.home() / ".subtle")
    path.mkdir(parents=True, exist_ok=True)
    return path


def load() -> dict:
    cfg = dict(DEFAULTS)
    try:
        cfg.update({k: v for k, v in json.loads((home() / "config.json").read_text("utf-8")).items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    return cfg


def save(cfg: dict) -> None:
    path = home() / "config.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({k: cfg[k] for k in DEFAULTS if k in cfg}, indent=2, ensure_ascii=False), "utf-8")
    tmp.replace(path)


def key_source(cfg: dict) -> str:
    """Where the API key comes from: env | saved | none. The environment wins."""
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return "env"
    return "saved" if cfg.get("api_key") else "none"
