import sys

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    anthropic_api_key: str = Field(default="")
    anthropic_base_url: str = Field(default="")
    model: str = Field(default="claude-sonnet-4-6")
    max_tokens: int = Field(default=8096)

    profiler_enabled: bool = Field(default=False)
    verbose: bool = Field(default=True)

    search_api_key: str = Field(default="")

    compact_threshold: int = Field(default=50_000)
    compact_micro_age: int = Field(default=3)

    def check_required(self) -> None:
        errors: list[str] = []
        if not self.anthropic_api_key:
            errors.append("ANTHROPIC_API_KEY is not set")
        if errors:
            for e in errors:
                print(f"  [config error] {e}", file=sys.stderr)
            sys.exit(1)


settings = Settings()
