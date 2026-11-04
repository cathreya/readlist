"""LLM provider configuration. Reads from environment and .env file."""

import os
from pathlib import Path

def _load_dotenv():
    """Load .env from project root if python-dotenv is available, else parse manually."""
    try:
        from dotenv import load_dotenv
        env_path = Path(__file__).resolve().parent.parent / ".env"
        load_dotenv(env_path)
        return
    except ImportError:
        pass
    # Fallback: manual .env parser
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key not in os.environ:
            os.environ[key] = val

_load_dotenv()

def get_llm_config():
    api_key = os.getenv("ANTHROPIC_API_KEY") or os.getenv("LLM_API_KEY", "")
    base_url = os.getenv("LLM_BASE_URL", "https://api.anthropic.com")
    model = os.getenv("LLM_MODEL", "claude-haiku-4-5-20251001")
    return {"api_key": api_key, "base_url": base_url, "model": model}
