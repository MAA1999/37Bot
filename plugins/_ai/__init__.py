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


def get_vision_llm() -> LLMClient:
    """返回多模态 LLMClient；未单独配置时复用主 LLM。"""
    cfg = load_llm_config()
    if not (cfg.vision_base_url and cfg.vision_api_key and cfg.vision_model):
        return LLMClient(base_url=cfg.base_url, api_key=cfg.api_key, model=cfg.model, backups=cfg.backups)
    return LLMClient(
        base_url=cfg.vision_base_url,
        api_key=cfg.vision_api_key,
        model=cfg.vision_model,
        backups=cfg.vision_backups,
    )


def is_llm_configured() -> bool:
    cfg = load_llm_config()
    return bool(cfg.base_url and cfg.api_key and cfg.model)


def is_vision_llm_configured() -> bool:
    cfg = load_llm_config()
    return bool(
        (cfg.vision_base_url and cfg.vision_api_key and cfg.vision_model)
        or (cfg.base_url and cfg.api_key and cfg.model)
    )


def start_llm_health_probe():
    """启动 LLM 健康探测（幂等）"""
    start_health_probe(get_llm)


__all__ = ["LLMClient", "get_llm", "is_llm_configured", "load_llm_config", "save_llm_config",
           "get_vision_llm", "is_vision_llm_configured", "start_llm_health_probe", "get_health_status"]
