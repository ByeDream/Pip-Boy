import sys

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
    )

    anthropic_api_key: str = Field(default="")
    anthropic_base_url: str = Field(default="")
    model: str = Field(default="claude-sonnet-4-6")
    max_tokens: int = Field(default=8096)

    profiler_enabled: bool = Field(default=False)
    verbose: bool = Field(default=True)

    search_api_key: str = Field(default="")
    subagent_max_rounds: int = Field(default=15)

    def check_required(self) -> None:
        errors: list[str] = []
        if not self.anthropic_api_key:
            errors.append("ANTHROPIC_API_KEY is not set")
        if not self.anthropic_base_url:
            errors.append("ANTHROPIC_BASE_URL is not set")
        if errors:
            for e in errors:
                print(f"  [config error] {e}", file=sys.stderr)
            sys.exit(1)


settings = Settings()
