"""LLM provider configuration. Reads from environment variables."""

import os

def get_llm_config():
    api_key = os.getenv("ANTHROPIC_API_KEY") or os.getenv("LLM_API_KEY", "")
    base_url = os.getenv("LLM_BASE_URL", "https://api.anthropic.com")
    model = os.getenv("LLM_MODEL", "claude-haiku-4-5-20251001")
    return {"api_key": api_key, "base_url": base_url, "model": model}
