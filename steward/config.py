"""Runtime configuration, read from environment / .env. No secrets are ever stored in the repo."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # dotenv is optional
    pass


@dataclass
class Config:
    data_dir: Path = Path(os.getenv("STEWARD_DATA_DIR", "./data"))
    pack_dir: Path = Path(os.getenv("STEWARD_PACK_DIR", "./company_pack"))
    files_dir: Path = Path(os.getenv("STEWARD_FILES_DIR", "./company_files"))
    model: str = os.getenv("STEWARD_MODEL", "claude-sonnet-5-5")
    verifier_model: str = os.getenv("STEWARD_VERIFIER_MODEL", os.getenv("STEWARD_MODEL", "claude-sonnet-5-5"))
    role: str = os.getenv("STEWARD_ROLE", "ops-agent")
    # Browser: leave both empty to use Playwright's bundled Chromium. On unsupported distros
    # (e.g. Ubuntu 20.04) set STEWARD_BROWSER_CHANNEL=chrome to drive the installed Google Chrome.
    browser_channel: str = os.getenv("STEWARD_BROWSER_CHANNEL", "")
    browser_path: str = os.getenv("STEWARD_BROWSER_PATH", "")
    headless: bool = os.getenv("STEWARD_HEADLESS", "1") != "0"
    max_steps: int = int(os.getenv("STEWARD_MAX_STEPS", "40"))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "steward.db"

    @property
    def evidence_dir(self) -> Path:
        return self.data_dir / "evidence"

    @property
    def workdir(self) -> Path:
        return self.data_dir / "work"
