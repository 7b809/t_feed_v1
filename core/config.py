from functools import lru_cache
import json
from pathlib import Path
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore")
    app_name: str = "Project Update Service"
    host: str = "0.0.0.0"
    port: int = 8080
    log_level: str = "INFO"
    api_key: str = Field(min_length=16)
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    projects_json: str
    update_python: str = "python3"
    update_script: str = "update_project.py"
    command_timeout_seconds: int = 1800
    telegram_output_max_chars: int = 3000

    @field_validator("projects_json")
    @classmethod
    def validate_projects(cls, value: str) -> str:
        projects = json.loads(value)
        if not isinstance(projects, dict) or not projects:
            raise ValueError("PROJECTS_JSON must be a non-empty JSON object")
        for name, path in projects.items():
            if not isinstance(name, str) or not isinstance(path, str):
                raise ValueError("Every project name and path must be a string")
            if not Path(path).is_absolute():
                raise ValueError(f"Project path must be absolute: {path}")
        return value

    @property
    def projects(self) -> dict[str, Path]:
        return {name: Path(path).resolve() for name, path in json.loads(self.projects_json).items()}

    @property
    def allowed_chat_id(self) -> int | None:
        return int(self.telegram_chat_id) if self.telegram_chat_id.strip() else None

@lru_cache
def get_settings() -> Settings:
    return Settings()
