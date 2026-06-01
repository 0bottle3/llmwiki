from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TEAM_VAULT_", extra="ignore")

    storage_backend: Literal["local", "s3"] = "local"
    local_data_dir: Path = Path(".data/team-vault")
    s3_bucket: str = ""
    s3_prefix: str = ""
    aws_region: str = "ap-northeast-2"
    hostname_map_path: Path | None = None
    trust_os_user_fallback: bool = True
    default_owner: str = "unknown"
    enable_mcp: bool = True
    service_name: str = Field(default="team-vault", min_length=1)
