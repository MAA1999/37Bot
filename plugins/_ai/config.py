"""共享 LLM 配置"""

import json
from dataclasses import dataclass
from pathlib import Path

SHARED_CONFIG_PATH = Path("data/_ai/llm_config.json")


@dataclass
class LLMConfig:
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    backups: list[dict] = None  # [{base_url, api_key, model}, ...]

    def __post_init__(self):
        if self.backups is None:
            self.backups = []


def load_llm_config() -> LLMConfig:
    if SHARED_CONFIG_PATH.exists():
        try:
            data = json.loads(SHARED_CONFIG_PATH.read_text("utf-8"))
            return LLMConfig(
                base_url=data.get("base_url", ""),
                api_key=data.get("api_key", ""),
                model=data.get("model", ""),
                backups=data.get("backups", []),
            )
        except Exception:
            pass
    return LLMConfig()


def save_llm_config(cfg: LLMConfig):
    SHARED_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    SHARED_CONFIG_PATH.write_text(
        json.dumps({
            "base_url": cfg.base_url, "api_key": cfg.api_key, "model": cfg.model,
            "backups": cfg.backups,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
