"""All configuration comes from environment variables; see .env.example."""

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LLM_PROVIDERS = ("openai_compat", "anthropic", "ollama", "stub")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://procuremind_app:procuremind_app@localhost:55452/procuremind"
    database_url_owner: str = "postgresql+psycopg://procuremind_owner:procuremind_owner@localhost:55452/procuremind"
    redis_url: str = "redis://localhost:56399/0"
    db_pool_size: int = 10

    secret_key: str = ""
    cors_origins: str = "http://localhost:3300"
    cookie_secure: bool = False
    session_hours: int = 12
    seed_demo: bool = True
    demo_scale: float = 0.25  # share of the full demo data set (vendors, orders, documents) loaded at first start
    allow_signup: bool = True
    trusted_proxy: bool = False  # read the client address from X-Forwarded-For (set when behind nginx)
    scrypt_n: int = 2**15
    login_attempts_per_minute: int = 5  # per e-mail address
    login_ip_per_minute: int = 30
    signup_ip_per_hour: int = 10

    llm_provider: str = "stub"
    llm_model: str = ""
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_fallback_model: str = ""
    llm_price_input_per_mtok: float = 3.0  # USD per million tokens; set to your provider's real prices
    llm_price_output_per_mtok: float = 15.0
    llm_max_concurrency: int = 16  # model calls in flight per process
    llm_max_output_tokens: int = 4096
    allow_private_llm_urls: bool = True  # false on a shared deployment: refuse base URLs on private networks

    run_max_steps: int = 25
    run_max_tokens: int = 200_000
    run_max_seconds: int = 300
    run_max_cost_usd: float = 1.00
    org_daily_cost_usd: float = 20.00
    global_daily_cost_usd: float = 200.00  # all organisations together: bounds what open sign-up can spend
    org_max_concurrent_runs: int = 5
    org_max_queued_runs: int = 15  # runs beyond the concurrent limit, per organisation: new runs get 429 once queued + running reaches both together
    runs_per_minute: int = 10  # new runs per user
    run_lease_seconds: int = 30  # a worker that stops renewing for this long loses the run to another worker
    run_max_attempts: int = 5  # times a run may be picked up before it is failed as unrecoverable
    worker_concurrency: int = 4
    auto_investigate: bool = False  # start the Exception Agent on every new exception (needs a real model)

    @field_validator("llm_provider")
    @classmethod
    def known_provider(cls, value: str) -> str:
        # A misspelt provider used to become the scripted stub without a word; now the application will not start.
        if value not in LLM_PROVIDERS:
            raise ValueError(f"LLM_PROVIDER must be one of {', '.join(LLM_PROVIDERS)}; got {value!r}.")
        return value

    stub_responder: str = "app.exceptions.stub_agent:demo_responder"  # behaviour of LLM_PROVIDER=stub

    files_dir: str = (
        "/data/files"  # uploaded documents, by organisation and SHA-256 (a volume shared by api and worker)
    )
    max_file_mb: int = 20
    max_pdf_pages: int = 200
    document_lease_seconds: int = 120

    otel_exporter_otlp_endpoint: str = ""
    metrics_token: str = ""  # when set, /metrics requires "Authorization: Bearer <token>"
    log_level: str = "INFO"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
