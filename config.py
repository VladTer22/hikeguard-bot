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
    ban_on_strike: int = 3

    # Database
    db_path: str = "data/hikeguard.db"

    # Join request gate
    join_gate_enabled: bool = True
    raid_threshold: int = 10          # requests within window to trigger raid mode
    raid_window_sec: int = 60         # sliding window size
    raid_mode_minutes: int = 20       # how long raid mode stays on after last surge
    auto_decline_score: int = 5       # score >= this → auto-decline
    auto_approve_score: int = -1      # score <= this → auto-approve, else admin queue

    @property
    def gemini_enabled(self) -> bool:
        return bool(self.gemini_api_key)


settings = Settings()  # type: ignore[call-arg]
