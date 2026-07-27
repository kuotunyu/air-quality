"""Settings and YAML configuration loading.

Two kinds of configuration, kept deliberately separate:

* **Settings** (``Settings``) — secrets and environment-specific knobs, from
  ``.env`` / the process environment. Never committed.
* **Config files** (``conf/*.yaml``) — the scientific and source definitions:
  which datasets, which stations, which value ranges. Committed and reviewable.
"""

from __future__ import annotations

from functools import cache, lru_cache
from typing import Any

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from twair.paths import CONF_DIR, REPO_ROOT


class Settings(BaseSettings):
    """Environment-derived settings. Missing API keys are allowed until used."""

    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    moenv_api_key: str | None = None
    cwa_api_key: str | None = None
    cdsapi_url: str = "https://cds.climate.copernicus.eu/api"
    cdsapi_key: str | None = None
    gee_project_id: str | None = None
    hf_token: str | None = None
    hf_namespace: str | None = None

    twair_data_dir: str = "data"
    twair_log_level: str = "INFO"
    twair_min_request_interval: float = Field(
        default=1.0,
        description="Minimum seconds between requests to the same host (politeness).",
    )
    twair_user_agent: str = "airlens-tw/0.1 (research)"

    def require(self, field: str) -> str:
        """Fetch a secret, failing loudly with a pointer to the registration docs."""
        value = getattr(self, field, None)
        if not value:
            raise RuntimeError(
                f"Setting '{field.upper()}' is not set. Copy .env.example to .env and "
                f"fill it in — see docs/registrations.md for where to obtain it."
            )
        return str(value)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


@cache
def load_conf(name: str) -> dict[str, Any]:
    """Load ``conf/<name>.yaml``. Cached; call ``load_conf.cache_clear()`` in tests."""
    path = CONF_DIR / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise TypeError(f"{path} must contain a YAML mapping at the top level.")
    return data


def write_conf(name: str, data: dict[str, Any], *, header: str | None = None) -> None:
    """Write ``conf/<name>.yaml``. Used by the Phase 0 probe to record discovered sources."""
    path = CONF_DIR / f"{name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=100)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        if header:
            for line in header.strip().splitlines():
                fh.write(f"# {line}\n".rstrip() + "\n" if line else "#\n")
            fh.write("\n")
        fh.write(body)
    load_conf.cache_clear()
