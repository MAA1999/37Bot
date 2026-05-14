from .config import load_llm_config, save_llm_config
from .llm import LLMClient


def get_llm() -> LLMClient:
    """返回基于当前共享配置的 LLMClient，每次读取最新配置"""
    cfg = load_llm_config()
    return LLMClient(base_url=cfg.base_url, api_key=cfg.api_key, model=cfg.model)


def is_llm_configured() -> bool:
    cfg = load_llm_config()
    return bool(cfg.base_url and cfg.api_key and cfg.model)


__all__ = ["LLMClient", "get_llm", "is_llm_configured", "load_llm_config", "save_llm_config"]
