"""Application configuration via environment variables."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # PostgreSQL
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "echelonos"
    postgres_user: str = "echelonos"
    postgres_password: str = "echelonos_dev"

    # OpenAI (GPT-4o - extraction)
    openai_api_key: str = ""
    openai_model: str = "gpt-4o"

    # Anthropic (Claude - verification)
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-5-20250514"

    # Azure Document Intelligence (OCR)
    azure_doc_intelligence_endpoint: str = ""
    azure_doc_intelligence_key: str = ""

    # Prefect
    prefect_api_url: str = "http://localhost:4200/api"

    @property
    def database_url(self) -> str:
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def async_database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
