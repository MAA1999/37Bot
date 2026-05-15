from .config import load_llm_config, save_llm_config
from .llm import LLMClient, start_health_probe, get_health_status


def get_llm() -> LLMClient:
    """返回基于当前共享配置的 LLMClient，每次读取最新配置"""
    cfg = load_llm_config()
    client = LLMClient(base_url=cfg.base_url, api_key=cfg.api_key, model=cfg.model, backups=cfg.backups)
    if cfg.backups:
        from ncatbot.utils import get_log
        get_log("AiMod").debug(f"LLM 配置: 主={cfg.model}, 备用={len(cfg.backups)}个")
    return client


def is_llm_configured() -> bool:
    cfg = load_llm_config()
    return bool(cfg.base_url and cfg.api_key and cfg.model)


def start_llm_health_probe():
    """启动 LLM 健康探测（幂等）"""
    start_health_probe(get_llm)


__all__ = ["LLMClient", "get_llm", "is_llm_configured", "load_llm_config", "save_llm_config",
           "start_llm_health_probe", "get_health_status"]
