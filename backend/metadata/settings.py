"""Static metadata settings (S04)."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from backend.config import load_or_fail

# Absolute default, like the migrations directory, so the API and workers find the repository's
# metadata regardless of their working directory.
DEFAULT_METADATA_DIR = Path(__file__).resolve().parents[2] / "metadata"


class MetadataSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
    )

    metadata_dir: Path = DEFAULT_METADATA_DIR
    metadata_max_file_bytes: int = Field(default=1_048_576, ge=1, le=64 * 1_048_576)
    # Every process checks the tree this often and swaps in valid changes; 0 disables.
    metadata_refresh_interval_s: float = Field(default=30.0, ge=0, le=86_400)

    def resolved_dir(self) -> Path:
        return self.metadata_dir.expanduser().resolve()


def load_metadata_settings() -> MetadataSettings:
    return load_or_fail(MetadataSettings)
