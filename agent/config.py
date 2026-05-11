from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv


@dataclass
class Settings:
    home: Path
    brain_vault_path: Path
    rolodex_user_last_name: str = "Pilkington"
    rolodex_tier_days: dict[str, int] = field(
        default_factory=lambda: {
            "T1": 14,
            "T2": 45,
            "T3": 90,
            "T4": 180,
            "T5": 365,
        }
    )
    rolodex_daily_send_cap: int = 5

    def resolve_home(self) -> Path:
        return self.home


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    load_dotenv()
    home = Path(os.environ.get("ROLODEX_HOME", Path.home() / ".rolodex-ai")).expanduser()
    brain = Path(os.environ.get("ROLODEX_BRAIN_PATH", home / "brain")).expanduser()
    return Settings(
        home=home,
        brain_vault_path=brain,
        rolodex_user_last_name=(os.environ.get("ROLODEX_USER_LAST_NAME", "Pilkington") or "Pilkington").strip(),
        rolodex_daily_send_cap=max(1, int(os.environ.get("ROLODEX_DAILY_SEND_CAP", "5") or "5")),
    )
