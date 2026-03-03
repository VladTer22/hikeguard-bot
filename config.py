from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    # Bot
    bot_token: str
    admin_chat_id: int

    # Quarantine
    quarantine_hours: int = 24

    # Spam detection
    spam_threshold: int = 7

    # Gemini API (optional — bot works without it, keywords-only mode)
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3-flash-preview"
    gemini_timeout: int = 10

    # Moderation
    mute_duration_minutes: int = 60
    ban_on_strike: int = 2
    auto_delete_service_sec: int = 300

    # Database
    db_path: str = "data/hikeguard.db"

    @property
    def gemini_enabled(self) -> bool:
        return bool(self.gemini_api_key)


settings = Settings()  # type: ignore[call-arg]
