"""Settings from .env and the LLM files in secrets/."""
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from . import budget

ROOT = Path(__file__).resolve().parent.parent
SECRETS = ROOT / "secrets"

# override: a terminal can hold old values (VS Code's useEnvFile copies .env into it), and .env must win over those
load_dotenv(ROOT / ".env", override=True)

DEFAULT_BIGQUERY_TABLE = "internal-apps-465610.dev_proximo_cantag.topk_options"
EFFORTS = ("low", "medium", "high")
DEFAULT_EFFORT = "medium"
DEFAULT_MODELS = {"anthropic": "claude-opus-5-5", "openai": "gpt-5.5", "gemini": "gemini-3.8-flash"}


@dataclass(frozen=True)
class Settings:
    database_url: str | None
    google_credentials: str | None
    bigquery_table: str
    tag_workers: int


def load_settings() -> Settings:
    credentials = os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or None
    if credentials:
        path = Path(credentials)
        credentials = str(path if path.is_absolute() else ROOT / path)
        # The BigQuery client reads this variable itself, so give it the absolute path.
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = credentials
    return Settings(
        database_url=os.getenv("DATABASE_URL") or None,
        google_credentials=credentials,
        bigquery_table=os.getenv("BIGQUERY_TABLE") or DEFAULT_BIGQUERY_TABLE,
        tag_workers=int(os.getenv("TAG_WORKERS") or 4),
    )


@dataclass(frozen=True)
class LLMConfig:
    name: str
    provider: str
    model: str
    api_key: str
    input_price: float   # USD per 1M input tokens
    output_price: float  # USD per 1M output tokens
    search_price: float  # USD per web search
    effort: str = DEFAULT_EFFORT  # low | medium | high
    base_url: str | None = None   # set for a local OpenAI-style proxy, e.g. the Gemini-web one


def provider_of_key(api_key: str) -> str | None:
    """The provider a key belongs to, from its prefix, or None for a format we don't know."""
    if api_key.startswith("sk-ant-"):
        return "anthropic"
    if api_key.startswith("sk-"):
        return "openai"
    if api_key.startswith(("AIza", "AQ.")):  # Google's older and newer key formats
        return "gemini"
    return None


def provider_of_model(model: str) -> str:
    if model.startswith("claude"):
        return "anthropic"
    if model.startswith("gemini"):
        return "gemini"
    return "openai"


def _llm_files() -> list[Path]:
    return sorted(SECRETS.glob("*.json")) if SECRETS.is_dir() else []


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _default_search_price(provider: str, model: str) -> float:
    if provider == "openai" and model.startswith(("gpt-4", "gpt-3")):
        return budget.NON_REASONING_SEARCH_PRICE
    if provider == "gemini":
        return budget.GEMINI_2_SEARCH_PRICE if model.startswith("gemini-2") else budget.GEMINI_SEARCH_PRICE
    return budget.SEARCH_PRICE


def load_llm_config(name: str) -> LLMConfig:
    path = Path(name)
    if not path.is_file():
        path = SECRETS / (name if name.endswith(".json") else f"{name}.json")
    if not path.is_file():
        available = ", ".join(p.stem for p in _llm_files() if (_read_json(p) or {}).get("api_key")) or "none"
        sys.exit(f"LLM file '{name}' not found. Available in secrets/: {available}")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        sys.exit(f"{path} is not valid JSON: {e}")
    if not isinstance(data, dict) or not data.get("api_key"):
        sys.exit(f"{path} has no api_key")

    api_key = data["api_key"]
    if "REPLACE_ME" in api_key:
        sys.exit(f"{path} still has the placeholder api_key. Put the real key in it first.")
    key_provider = provider_of_key(api_key)
    provider = data.get("provider", key_provider)
    if provider is None:
        sys.exit(f"{path}: can't tell the provider from the api_key. Add \"provider\" (anthropic, openai or gemini).")
    if provider not in DEFAULT_MODELS:
        sys.exit(f"{path}: unknown provider '{provider}' (use anthropic, openai or gemini)")
    if key_provider is not None and provider != key_provider:
        sys.exit(f"{path}: provider is '{provider}' but the api_key looks like a {key_provider} key")

    model = data.get("model") or DEFAULT_MODELS[provider]
    if "input_price" in data and "output_price" in data:
        input_price, output_price = float(data["input_price"]), float(data["output_price"])
    elif model in budget.PRICES:
        input_price, output_price = budget.PRICES[model]
    else:
        sys.exit(f"{path}: no price known for model '{model}'. Add input_price and output_price "
                 "(USD per 1M tokens) to the file.")
    search_price = float(data["search_price"]) if "search_price" in data else _default_search_price(provider, model)
    effort = data.get("effort", DEFAULT_EFFORT)
    if effort not in EFFORTS:
        sys.exit(f"{path}: effort is '{effort}' (use {', '.join(EFFORTS)})")

    return LLMConfig(path.stem, provider, model, api_key, input_price, output_price, search_price, effort,
                     data.get("base_url") or None)


def find_llm_configs(provider: str) -> list[Path]:
    """The LLM files in secrets/ whose key belongs to `provider`."""
    found = []
    for path in _llm_files():
        data = _read_json(path)
        if not data or not data.get("api_key"):
            continue  # e.g. the BigQuery service account
        if data.get("provider", provider_of_key(data["api_key"])) == provider:
            found.append(path)
    return found


def find_llm_config(provider: str) -> LLMConfig:
    found = find_llm_configs(provider)
    if not found:
        sys.exit(f"No {provider} LLM file in secrets/. Create one or pass --llm.")
    if len(found) > 1:
        names = ", ".join(p.stem for p in found)
        sys.exit(f"Several {provider} LLM files in secrets/ ({names}). Pick one with --llm.")
    return load_llm_config(str(found[0]))
