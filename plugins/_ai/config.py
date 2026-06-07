"""共享 LLM 配置"""

import json
from dataclasses import dataclass
from pathlib import Path

SHARED_CONFIG_PATH = Path("data/_ai/llm_config.json")


def normalize_openai_base_url(base_url: str) -> str:
    value = str(base_url or "").strip().rstrip("/")
    chat_completions_suffix = "/chat/completions"
    if value.lower().endswith(chat_completions_suffix):
        value = value[: -len(chat_completions_suffix)].rstrip("/")
    return value


def _normalize_model_cfgs(backups: list[dict] | None) -> list[dict]:
    normalized = []
    for backup in backups or []:
        if not isinstance(backup, dict):
            continue
        normalized.append({
            **backup,
            "base_url": normalize_openai_base_url(str(backup.get("base_url", ""))),
        })
    return normalized


@dataclass
class LLMConfig:
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    backups: list[dict] | None = None  # [{base_url, api_key, model}, ...]
    vision_base_url: str = ""
    vision_api_key: str = ""
    vision_model: str = ""
    vision_backups: list[dict] | None = None
    enabled: bool = True
    auto_reply: bool = False
    cooldown: int = 0
    ai_system_prompt: str = ""
    timeout: float = 30.0

    def __post_init__(self):
        self.base_url = normalize_openai_base_url(self.base_url)
        self.vision_base_url = normalize_openai_base_url(self.vision_base_url)
        self.backups = _normalize_model_cfgs(self.backups)
        self.vision_backups = _normalize_model_cfgs(self.vision_backups)


def load_llm_config() -> LLMConfig:
    if SHARED_CONFIG_PATH.exists():
        try:
            data = json.loads(SHARED_CONFIG_PATH.read_text("utf-8"))
            return LLMConfig(
                base_url=data.get("base_url", ""),
                api_key=data.get("api_key", ""),
                model=data.get("model", ""),
                backups=data.get("backups", []),
                vision_base_url=data.get("vision_base_url", ""),
                vision_api_key=data.get("vision_api_key", ""),
                vision_model=data.get("vision_model", ""),
                vision_backups=data.get("vision_backups", []),
                enabled=data.get("enabled", True),
                auto_reply=data.get("auto_reply", False),
                cooldown=data.get("cooldown", 0),
                ai_system_prompt=data.get("ai_system_prompt", ""),
                timeout=data.get("timeout", 30.0),
            )
        except Exception:
            pass
    return LLMConfig()


def save_llm_config(cfg: LLMConfig):
    SHARED_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    cfg.base_url = normalize_openai_base_url(cfg.base_url)
    cfg.vision_base_url = normalize_openai_base_url(cfg.vision_base_url)
    cfg.backups = _normalize_model_cfgs(cfg.backups)
    cfg.vision_backups = _normalize_model_cfgs(cfg.vision_backups)
    SHARED_CONFIG_PATH.write_text(
        json.dumps({
            "base_url": cfg.base_url, "api_key": cfg.api_key, "model": cfg.model,
            "backups": cfg.backups,
            "vision_base_url": cfg.vision_base_url,
            "vision_api_key": cfg.vision_api_key,
            "vision_model": cfg.vision_model,
            "vision_backups": cfg.vision_backups,
            "enabled": cfg.enabled, "auto_reply": cfg.auto_reply,
            "cooldown": cfg.cooldown, "ai_system_prompt": cfg.ai_system_prompt, "timeout": cfg.timeout,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
