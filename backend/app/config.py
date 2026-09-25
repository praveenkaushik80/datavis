"""Runtime settings. Every value can be overridden with an environment variable of the same name.

Runs unchanged in Docker and on Vercel. On Vercel (VERCEL=1) the app database comes from DATABASE_URL /
POSTGRES_URL (e.g. Neon from the Vercel Marketplace), MCP servers run in-process, and scratch files go
to /tmp (the only writable path).
"""
import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Application database (connections, semantic context versions, users, telemetry).
    # Empty = use DATABASE_URL or POSTGRES_URL (Vercel / Neon), else a local SQLite file.
    app_db_url: str = ""

    # Secrets
    fernet_key: str = ""                  # `python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"`
    jwt_secret: str = "change-me"
    internal_token: str = "change-me-internal"   # used by Cube to fetch data-source config
    hop_api_token: str = "change-me-hop"         # service token used by Apache Hop workflows
    hop_api_user: str = "steward@datafusion.local"
    hop_admin_token: str = ""                    # separate token for Hop admin workflows (users, permissions)
    hop_admin_user: str = "admin@datafusion.local"

    # Auth: "dev" accepts X-User-Email header; "otp" requires email + OTP login (MVP2)
    auth_mode: str = "dev"
    otp_ttl_seconds: int = 600
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "datafusion@localhost"

    # Models via OpenRouter. By default calls go through the LiteLLM gateway (approved models, budgets),
    # which forwards to OpenRouter. To call OpenRouter directly set LLM_BASE_URL=https://openrouter.ai/api/v1
    # and LLM_API_KEY=<your OpenRouter key>. llm_provider=fake runs offline for demos and tests.
    llm_provider: str = "openai"          # openai-compatible client (LiteLLM or OpenRouter) | fake
    llm_base_url: str = "http://litellm:4000/v1"
    llm_api_key: str = "sk-litellm-master"
    llm_model_agent: str = "datafusion-agent"        # LiteLLM alias -> openrouter/anthropic/claude-sonnet-5
    llm_model_research: str = "datafusion-research"  # LiteLLM alias -> openrouter/anthropic/claude-sonnet-5
    llm_temperature: float = 0.0
    llm_timeout_seconds: int = 120
    openrouter_app_url: str = "https://datafusion.local"   # sent as HTTP-Referer for OpenRouter attribution
    openrouter_app_name: str = "DataFusion"

    # Web search for the context research agent (only used when the workflow switches it ON)
    web_search_provider: str = "none"     # "tavily" or "none"
    tavily_api_key: str = ""

    # OpenMetadata (catalog of record). Leave om_url empty to use the built-in catalog extractor only.
    om_url: str = ""
    om_token: str = ""                    # ingestion-bot JWT from OpenMetadata > Settings > Bots
    om_run_ingestion: bool = True

    # Cube Core (semantic layer)
    cube_api_url: str = ""                # e.g. http://cube:4000/cubejs-api/v1
    cube_api_secret: str = ""

    # Optional folder shared with Apache Hop in Docker (documents/from-path). Not used on Vercel, where
    # Hop sends files over HTTP instead.
    shared_dir: str = ""

    # How the agent reaches the auto-generated MCP servers: stdio (one subprocess per server, Docker
    # default), inprocess (in-memory MCP sessions, serverless default), or auto.
    mcp_transport: str = "auto"

    # OpenRouter model fallbacks when calling OpenRouter directly (comma separated model ids)
    llm_fallback_models: str = ""

    # Long requests (context research, agent questions) stream whitespace while they run, so proxies that
    # close idle connections keep them open (Railway: 5 minutes with no data). auto = on when running on
    # Railway. The final JSON body is unchanged (leading whitespace is valid JSON).
    keepalive_streaming: str = "auto"
    keepalive_interval_seconds: float = 20.0

    # Query guardrails
    query_row_limit: int = 200
    query_timeout_seconds: int = 30
    agent_max_steps: int = 8

    @property
    def serverless(self) -> bool:
        return bool(os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))

    @property
    def database_url(self) -> str:
        url = (self.app_db_url or os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
               or ("sqlite:////tmp/datafusion.db" if self.serverless else "sqlite:///./datafusion.db"))
        # Vercel / Neon hand out postgres:// or postgresql:// URLs; use the psycopg 3 driver.
        for prefix in ("postgres://", "postgresql://"):
            if url.startswith(prefix):
                return "postgresql+psycopg://" + url[len(prefix):]
        return url

    @property
    def on_railway(self) -> bool:
        return bool(os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID"))

    @property
    def keepalive_enabled(self) -> bool:
        if self.keepalive_streaming in ("true", "1", "yes"):
            return True
        if self.keepalive_streaming in ("false", "0", "no"):
            return False
        return self.on_railway

    @property
    def effective_mcp_transport(self) -> str:
        if self.mcp_transport in ("stdio", "inprocess"):
            return self.mcp_transport
        return "inprocess" if self.serverless else "stdio"

    @property
    def scratch_dir(self) -> Path:
        base = Path(self.shared_dir) if self.shared_dir else Path("/tmp/datafusion")
        return base

    @property
    def docs_dir(self) -> Path:
        return self.scratch_dir / "docs"

    @property
    def review_dir(self) -> Path:
        return self.scratch_dir / "review"


@lru_cache
def get_settings() -> Settings:
    return Settings()
