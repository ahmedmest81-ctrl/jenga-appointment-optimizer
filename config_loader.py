"""
Configuration Loader for Jenga Appointment System

Loads and validates configuration from YAML file with Pydantic models.
Provides type-safe, immutable configuration with startup validation.
"""

import os
import yaml
from typing import Dict, List, Optional
from pydantic import BaseModel, Field, field_validator, model_validator
from pathlib import Path
import logging

logger = logging.getLogger(__name__)


# ===== Pydantic Configuration Models =====

class SystemConfig(BaseModel):
    """System-level configuration"""
    version: str
    environment: str = Field(pattern="^(production|staging|development)$")


class DatabaseConfig(BaseModel):
    """Database connection configuration"""
    pool_size: int = Field(ge=1, le=100)
    max_overflow: int = Field(ge=0, le=100)
    pool_pre_ping: bool = True
    connection_timeout: int = Field(ge=1, le=300)
    echo_queries: bool = False


class CascadeConfig(BaseModel):
    """Cascade optimization configuration"""
    max_depth: int = Field(ge=1, le=20)
    shift_offer_expiry_minutes: int = Field(ge=1, le=120)
    enable_proactive_cascade: bool = False


class TimeWindowsConfig(BaseModel):
    """
    Time window behavior configuration.

    Defines how Jenga behaves based on how far in the future an open slot is.
    This enables clinic-grade appointment compression with appropriate urgency.
    """
    # Long-term (>= N days): notify wishlist, no immediate cascade
    long_term_days: int = Field(ge=1, default=14)
    long_term_action: str = Field(pattern="^(notify|offer|auto_move)$", default="notify")

    # Medium-term (>= N days but < long_term): offer earlier slots
    medium_term_days: int = Field(ge=1, default=7)
    medium_term_action: str = Field(pattern="^(notify|offer|auto_move)$", default="offer")
    medium_term_offer_expiry_hours: int = Field(ge=1, default=24)

    # Short-term (< N hours): urgent action
    short_term_hours: int = Field(ge=1, default=48)
    short_term_action: str = Field(pattern="^(notify|offer|auto_move)$", default="auto_move")
    short_term_offer_expiry_minutes: int = Field(ge=1, default=30)

    @model_validator(mode='after')
    def validate_window_ordering(self):
        """Ensure time windows don't overlap incorrectly."""
        if self.medium_term_days >= self.long_term_days:
            raise ValueError("medium_term_days must be < long_term_days")
        return self


class ConsentConfig(BaseModel):
    """
    Consent policy configuration.

    Controls when Jenga auto-moves vs offers for patient consent.
    """
    require_consent_for_moves: bool = False  # true = always offer, never auto-move
    vip_always_offer: bool = True  # VIP clients always get offers
    max_offers_per_slot: int = Field(ge=1, le=10, default=3)
    offer_timeout_action: str = Field(
        pattern="^(next_candidate|abandon)$",
        default="next_candidate"
    )


class EngineConfig(BaseModel):
    """Core engine configuration"""
    appointment_window_days: int = Field(ge=1, le=365)
    cascade: CascadeConfig
    time_windows: Optional[TimeWindowsConfig] = None
    consent: Optional[ConsentConfig] = None


class SchedulerConfig(BaseModel):
    """Scheduler timing configuration"""
    enabled: bool = True
    daily_optimization_hour: int = Field(ge=0, le=23)
    daily_optimization_minute: int = Field(ge=0, le=59)
    reminder_check_interval_hours: int = Field(ge=1, le=24)
    auto_confirmation_interval_hours: int = Field(ge=1, le=24)
    google_calendar_sync_minutes: int = Field(ge=1, le=60)


class RemindersConfig(BaseModel):
    """Reminder timing configuration"""
    seven_day_hours: int = Field(ge=1)
    forty_eight_hour_hours: int = Field(ge=1)
    auto_confirm_hours: int = Field(ge=1)
    window_buffer_hours: int = Field(ge=0, le=24)


class ChannelsConfig(BaseModel):
    """Notification channels configuration"""
    fallback_order: List[str]
    retry_attempts: int = Field(ge=0, le=10)
    retry_delay_seconds: int = Field(ge=0, le=3600)

    @field_validator('fallback_order')
    @classmethod
    def validate_channels(cls, v):
        valid_channels = {'sms', 'email', 'whatsapp'}
        for channel in v:
            if channel not in valid_channels:
                raise ValueError(f"Invalid channel: {channel}. Must be one of {valid_channels}")
        return v


class NotificationsConfig(BaseModel):
    """Notifications configuration"""
    reminders: RemindersConfig
    channels: ChannelsConfig


class MLWeightsConfig(BaseModel):
    """ML feature weights - must sum to ~1.0"""
    day_of_week: float = Field(ge=0.0, le=1.0)
    time_bucket: float = Field(ge=0.0, le=1.0)
    client_history: float = Field(ge=0.0, le=1.0)
    appointment_type: float = Field(ge=0.0, le=1.0)
    segment: float = Field(ge=0.0, le=1.0)
    days_until: float = Field(ge=0.0, le=1.0)
    weather: float = Field(ge=0.0, le=1.0)
    move_history: float = Field(ge=0.0, le=1.0)

    @model_validator(mode='after')
    def validate_weights_sum(self):
        total = (
            self.day_of_week + self.time_bucket + self.client_history +
            self.appointment_type + self.segment + self.days_until +
            self.weather + self.move_history
        )
        if not (0.99 <= total <= 1.01):
            raise ValueError(f"ML weights must sum to ~1.0, got {total:.4f}")
        return self


class RiskThresholdsConfig(BaseModel):
    """Risk score thresholds"""
    high: float = Field(ge=0.0, le=1.0)
    medium: float = Field(ge=0.0, le=1.0)
    low: float = Field(ge=0.0, le=1.0)

    @model_validator(mode='after')
    def validate_threshold_ordering(self):
        if not (self.low <= self.medium <= self.high):
            raise ValueError(f"Risk thresholds must be ordered: low <= medium <= high")
        return self


class DayOfWeekRiskConfig(BaseModel):
    """Day of week risk scores"""
    monday: float = Field(ge=0.0, le=1.0)
    tuesday: float = Field(ge=0.0, le=1.0)
    wednesday: float = Field(ge=0.0, le=1.0)
    thursday: float = Field(ge=0.0, le=1.0)
    friday: float = Field(ge=0.0, le=1.0)
    saturday: float = Field(ge=0.0, le=1.0)
    sunday: float = Field(ge=0.0, le=1.0)


class TimeBucketRiskConfig(BaseModel):
    """Time bucket risk scores"""
    early_morning: float = Field(ge=0.0, le=1.0)
    morning: float = Field(ge=0.0, le=1.0)
    afternoon: float = Field(ge=0.0, le=1.0)
    late_afternoon: float = Field(ge=0.0, le=1.0)
    evening: float = Field(ge=0.0, le=1.0)


class TimeBucketsConfig(BaseModel):
    """Time bucket boundaries (24-hour format)"""
    early_morning_start: int = Field(ge=0, le=23)
    early_morning_end: int = Field(ge=0, le=23)
    morning_start: int = Field(ge=0, le=23)
    morning_end: int = Field(ge=0, le=23)
    afternoon_start: int = Field(ge=0, le=23)
    afternoon_end: int = Field(ge=0, le=23)
    late_afternoon_start: int = Field(ge=0, le=23)
    late_afternoon_end: int = Field(ge=0, le=23)
    evening_start: int = Field(ge=0, le=23)
    evening_end: int = Field(ge=0, le=23)

    @model_validator(mode='after')
    def validate_continuous_buckets(self):
        """Ensure time buckets are continuous with no gaps"""
        if self.early_morning_end != self.morning_start:
            raise ValueError("early_morning_end must equal morning_start")
        if self.morning_end != self.afternoon_start:
            raise ValueError("morning_end must equal afternoon_start")
        if self.afternoon_end != self.late_afternoon_start:
            raise ValueError("afternoon_end must equal late_afternoon_start")
        if self.late_afternoon_end != self.evening_start:
            raise ValueError("late_afternoon_end must equal evening_start")
        return self


class SegmentRiskConfig(BaseModel):
    """Client segment risk scores"""
    vip: float = Field(ge=0.0, le=1.0)
    regular: float = Field(ge=0.0, le=1.0)
    new: float = Field(ge=0.0, le=1.0)
    high_risk: float = Field(ge=0.0, le=1.0)


class AppointmentTypeRiskConfig(BaseModel):
    """Appointment type risk scores"""
    routine: float = Field(ge=0.0, le=1.0)
    follow_up: float = Field(ge=0.0, le=1.0)
    consultation: float = Field(ge=0.0, le=1.0)
    procedure: float = Field(ge=0.0, le=1.0)
    emergency: float = Field(ge=0.0, le=1.0)
    default: float = Field(ge=0.0, le=1.0)


class DaysUntilRiskConfig(BaseModel):
    """Days until appointment risk configuration"""
    very_close_threshold: int = Field(ge=0)
    very_close_risk: float = Field(ge=0.0, le=1.0)
    close_threshold: int = Field(ge=0)
    close_risk: float = Field(ge=0.0, le=1.0)
    medium_threshold: int = Field(ge=0)
    medium_risk: float = Field(ge=0.0, le=1.0)
    far_threshold: int = Field(ge=0)
    far_risk: float = Field(ge=0.0, le=1.0)
    very_far_risk: float = Field(ge=0.0, le=1.0)

    @model_validator(mode='after')
    def validate_threshold_ordering(self):
        """Ensure thresholds are ordered"""
        if not (self.very_close_threshold < self.close_threshold <
                self.medium_threshold < self.far_threshold):
            raise ValueError("Days until thresholds must be strictly increasing")
        return self


class MovePenaltyConfig(BaseModel):
    """Move history penalty configuration"""
    zero_moves: float = Field(ge=0.0, le=1.0)
    one_move: float = Field(ge=0.0, le=1.0)
    two_moves: float = Field(ge=0.0, le=1.0)
    three_plus_moves: float = Field(ge=0.0, le=1.0)


class ClientHistoryConfig(BaseModel):
    """Client history risk calculation configuration"""
    min_appointments_threshold: int = Field(ge=1, le=10)
    blend_factor: float = Field(ge=0.0, le=1.0)
    neutral_risk: float = Field(ge=0.0, le=1.0)
    no_show_weight: float = Field(ge=0.0, le=1.0)
    cancellation_weight: float = Field(ge=0.0, le=1.0)

    @model_validator(mode='after')
    def validate_weights_sum(self):
        """Ensure history weights sum to 1.0"""
        total = self.no_show_weight + self.cancellation_weight
        if not (0.99 <= total <= 1.01):
            raise ValueError(f"History weights must sum to ~1.0, got {total:.4f}")
        return self


class FlexibilityConfig(BaseModel):
    """Flexibility scoring configuration"""
    base_score: float = Field(ge=0.0)
    vip_multiplier: float = Field(ge=0.0, le=2.0)
    high_risk_multiplier: float = Field(ge=0.0, le=2.0)
    regular_multiplier: float = Field(ge=0.0, le=2.0)
    flexible_client_multiplier: float = Field(ge=0.0, le=2.0)
    inflexible_penalty: float = Field(ge=0.0, le=2.0)
    move_decay_rate: float = Field(ge=0.0, le=2.0)


class MLConfig(BaseModel):
    """Machine Learning configuration"""
    version: str
    weights: MLWeightsConfig
    risk_thresholds: RiskThresholdsConfig
    day_of_week_risk: DayOfWeekRiskConfig
    time_bucket_risk: TimeBucketRiskConfig
    time_buckets: TimeBucketsConfig
    segment_risk: SegmentRiskConfig
    appointment_type_risk: AppointmentTypeRiskConfig
    days_until_risk: DaysUntilRiskConfig
    move_penalty: MovePenaltyConfig
    client_history: ClientHistoryConfig
    flexibility: FlexibilityConfig


class APIConfig(BaseModel):
    """API configuration"""
    prefix: str
    key_header: str
    rate_limit_per_minute: int = Field(ge=1, le=1000)
    request_timeout_seconds: int = Field(ge=1, le=300)
    default_page_size: int = Field(ge=1, le=1000)


class AppointmentValidationConfig(BaseModel):
    """Appointment validation rules"""
    min_duration_minutes: int = Field(ge=1)
    max_duration_minutes: int = Field(ge=1)
    max_future_days: int = Field(ge=1)
    min_advance_hours: int = Field(ge=0)
    require_provider_id: bool = False

    @model_validator(mode='after')
    def validate_duration_bounds(self):
        """Ensure min < max for durations"""
        if self.min_duration_minutes >= self.max_duration_minutes:
            raise ValueError("min_duration_minutes must be < max_duration_minutes")
        return self


class ClientValidationConfig(BaseModel):
    """Client validation rules"""
    min_name_length: int = Field(ge=1)
    max_name_length: int = Field(ge=1)
    require_email_or_phone: bool = True


class BusinessValidationConfig(BaseModel):
    """Business validation rules"""
    min_name_length: int = Field(ge=1)
    max_name_length: int = Field(ge=1)
    min_appointment_window_days: int = Field(ge=1)
    max_appointment_window_days: int = Field(ge=1)


class RiskScoreValidationConfig(BaseModel):
    """Risk score validation rules"""
    min_value: float = Field(ge=0.0, le=1.0)
    max_value: float = Field(ge=0.0, le=1.0)


class ValidationConfig(BaseModel):
    """Validation configuration"""
    appointment: AppointmentValidationConfig
    client: ClientValidationConfig
    business: BusinessValidationConfig
    risk_score: RiskScoreValidationConfig


class ClientSegmentsConfig(BaseModel):
    """Client segment configuration"""
    high_risk_threshold: float = Field(ge=0.0, le=1.0)


class BusinessHoursDay(BaseModel):
    """Business hours for a single day"""
    start: str = Field(pattern=r"^\d{2}:\d{2}$")
    end: str = Field(pattern=r"^\d{2}:\d{2}$")
    enabled: bool


class BusinessHoursConfig(BaseModel):
    """Business hours configuration"""
    enabled: bool
    default_timezone: str
    monday: BusinessHoursDay
    tuesday: BusinessHoursDay
    wednesday: BusinessHoursDay
    thursday: BusinessHoursDay
    friday: BusinessHoursDay
    saturday: BusinessHoursDay
    sunday: BusinessHoursDay


class LoggingConfig(BaseModel):
    """Logging configuration"""
    level: str = Field(pattern="^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")
    format: str
    file_enabled: bool
    file_path: str


class FeaturesConfig(BaseModel):
    """Feature flags configuration"""
    enable_cascade_optimization: bool = True
    enable_risk_scoring: bool = True
    enable_notifications: bool = True
    enable_google_calendar_sync: bool = False
    enable_auto_confirmation: bool = True
    enable_proactive_cascade: bool = False


class Config(BaseModel):
    """Root configuration model"""
    system: SystemConfig
    database: DatabaseConfig
    engine: EngineConfig
    scheduler: SchedulerConfig
    notifications: NotificationsConfig
    ml: MLConfig
    api: APIConfig
    validation: ValidationConfig
    client_segments: ClientSegmentsConfig
    business_hours: BusinessHoursConfig
    logging: LoggingConfig
    features: FeaturesConfig


# ===== Configuration Loader =====

class ConfigLoader:
    """
    Configuration loader with validation.

    Loads configuration from YAML file, merges with environment variables,
    validates with Pydantic, and provides immutable configuration object.
    """

    _instance: Optional['ConfigLoader'] = None
    _config: Optional[Config] = None

    def __new__(cls, config_path: str = "config.yaml"):
        """Singleton pattern - only one config instance"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, config_path: str = "config.yaml"):
        """
        Initialize configuration loader.

        Args:
            config_path: Path to YAML configuration file (relative to this module or absolute)
        """
        # Only load once
        if self._config is not None:
            return

        # Make path absolute relative to config_loader.py location
        # This allows the app to run from any directory
        if not Path(config_path).is_absolute():
            # Get directory where config_loader.py is located
            module_dir = Path(__file__).parent.resolve()
            self.config_path = module_dir / config_path
        else:
            self.config_path = Path(config_path)

        self._config = self._load_and_validate()
        logger.info(f"Configuration loaded successfully from {self.config_path}")

    def _load_and_validate(self) -> Config:
        """
        Load YAML configuration and validate with Pydantic.

        Returns:
            Validated Config object

        Raises:
            FileNotFoundError: If config file doesn't exist
            ValidationError: If configuration is invalid
        """
        # Check file exists
        if not self.config_path.exists():
            raise FileNotFoundError(f"Configuration file not found: {self.config_path}")

        # Load YAML
        with open(self.config_path, 'r') as f:
            config_dict = yaml.safe_load(f)

        # Merge environment variables (optional overrides)
        config_dict = self._merge_env_overrides(config_dict)

        # Validate with Pydantic
        try:
            config = Config(**config_dict)
        except Exception as e:
            logger.error(f"Configuration validation failed: {e}")
            raise

        return config

    def _merge_env_overrides(self, config_dict: Dict) -> Dict:
        """
        Merge environment variable overrides into configuration.

        Environment variables take precedence over config file values.
        Format: JENGA_<SECTION>_<KEY> (e.g., JENGA_DATABASE_POOL_SIZE)

        Args:
            config_dict: Configuration dictionary from YAML

        Returns:
            Merged configuration dictionary
        """
        # Database URL override (common pattern)
        if 'DATABASE_URL' in os.environ:
            # Store for backward compatibility with old code
            pass

        # Add more specific overrides as needed
        # For now, environment variables are handled by .env file

        return config_dict

    @property
    def config(self) -> Config:
        """Get immutable configuration object"""
        return self._config

    # Convenience accessors for common config sections

    @property
    def system(self) -> SystemConfig:
        return self._config.system

    @property
    def database(self) -> DatabaseConfig:
        return self._config.database

    @property
    def engine(self) -> EngineConfig:
        return self._config.engine

    @property
    def scheduler(self) -> SchedulerConfig:
        return self._config.scheduler

    @property
    def notifications(self) -> NotificationsConfig:
        return self._config.notifications

    @property
    def ml(self) -> MLConfig:
        return self._config.ml

    @property
    def api(self) -> APIConfig:
        return self._config.api

    @property
    def validation(self) -> ValidationConfig:
        return self._config.validation

    @property
    def client_segments(self) -> ClientSegmentsConfig:
        return self._config.client_segments

    @property
    def business_hours(self) -> BusinessHoursConfig:
        return self._config.business_hours

    @property
    def logging(self) -> LoggingConfig:
        return self._config.logging

    @property
    def features(self) -> FeaturesConfig:
        return self._config.features


# ===== Global Configuration Instance =====

# Load configuration on module import (singleton)
try:
    config_loader = ConfigLoader()
    config = config_loader.config
    logger.info("Global configuration initialized successfully")
except Exception as e:
    logger.error(f"Failed to load configuration: {e}")
    raise


# ===== Backward Compatibility Helpers =====

# Expose commonly used values for easy access
APPOINTMENT_WINDOW_DAYS = config.engine.appointment_window_days
CASCADE_MAX_DEPTH = config.engine.cascade.max_depth
REMINDER_7_DAY_HOURS = config.notifications.reminders.seven_day_hours
REMINDER_48_HOUR_HOURS = config.notifications.reminders.forty_eight_hour_hours
AUTO_CONFIRM_HOURS = config.notifications.reminders.auto_confirm_hours
RISK_THRESHOLD_HIGH = config.ml.risk_thresholds.high
RISK_THRESHOLD_MEDIUM = config.ml.risk_thresholds.medium
ML_ENGINE_VERSION = config.ml.version
SCHEDULER_ENABLED = config.scheduler.enabled
DAILY_OPTIMIZATION_HOUR = config.scheduler.daily_optimization_hour
API_PREFIX = config.api.prefix
