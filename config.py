import os
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict
from dotenv import load_dotenv
import os
load_dotenv()

# SECURITY FIX: Removed hardcoded credentials - now loaded from environment only
# Set SENDGRID_API_KEY and SENDGRID_FROM_EMAIL in .env file

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=True)

    # Database
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL", 
        "sqlite:///./jenga.db"
    )
    
    # Core Engine Configuration
    APPOINTMENT_WINDOW_DAYS: int = int(os.getenv("APPOINTMENT_WINDOW_DAYS", "30"))
    CASCADE_MAX_DEPTH: int = int(os.getenv("CASCADE_MAX_DEPTH", "10"))
    
    # Notification Timing (hours before appointment)
    REMINDER_7_DAY_HOURS: int = 168  # 7 days
    REMINDER_48_HOUR_HOURS: int = 48
    AUTO_CONFIRM_HOURS: int = 24
    
    # Notification Services
    TWILIO_ACCOUNT_SID: Optional[str] = os.getenv("TWILIO_ACCOUNT_SID")
    TWILIO_AUTH_TOKEN: Optional[str] = os.getenv("TWILIO_AUTH_TOKEN")
    TWILIO_PHONE_NUMBER: Optional[str] = os.getenv("TWILIO_PHONE_NUMBER")
    
    SENDGRID_API_KEY: Optional[str] = os.getenv("SENDGRID_API_KEY")
    SENDGRID_FROM_EMAIL: Optional[str] = os.getenv("SENDGRID_FROM_EMAIL")
    
    WHATSAPP_API_KEY: Optional[str] = os.getenv("WHATSAPP_API_KEY")
    WHATSAPP_PHONE_NUMBER: Optional[str] = os.getenv("WHATSAPP_PHONE_NUMBER")
    
    # ML Engine
    ML_ENGINE_VERSION: str = "v2.0"
    RISK_THRESHOLD_HIGH: float = 0.7
    RISK_THRESHOLD_MEDIUM: float = 0.4
    
    # Scheduling
    SCHEDULER_ENABLED: bool = os.getenv("SCHEDULER_ENABLED", "true").lower() == "true"
    DAILY_OPTIMIZATION_HOUR: int = int(os.getenv("DAILY_OPTIMIZATION_HOUR", "2"))
    
    # API
    API_PREFIX: str = "/api/v1"
    API_KEY_HEADER: str = "X-API-Key"
    
    # Logging
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    
settings = Settings()
