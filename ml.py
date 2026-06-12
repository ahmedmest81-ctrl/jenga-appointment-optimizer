"""
ML Engine V2 - Deterministic Risk Prediction

Configuration-driven ML engine for no-show risk prediction.
All weights, thresholds, and risk scores loaded from config.yaml.

Features:
- No hardcoded business rules
- Configurable feature weights
- Day of week risk scoring
- Time bucket risk scoring
- Client history analysis
- Appointment type risk
- Client segment risk
- Days until appointment risk
- Move history penalty
- Flexibility scoring for cascade candidates
"""

from datetime import datetime
from typing import Dict
import math


class MLEngineV2:
    """
    Deterministic ML Engine V2 for no-show risk prediction.

    Uses feature engineering with configurable weighted scoring.
    All parameters loaded from configuration at initialization.
    """

    def __init__(self, ml_config):
        """
        Initialize ML engine with configuration.

        Args:
            ml_config: MLConfig from config_loader
        """
        self.config = ml_config
        self.version = ml_config.version

        # Feature weights
        self.weights = {
            "day_of_week": ml_config.weights.day_of_week,
            "time_bucket": ml_config.weights.time_bucket,
            "client_history": ml_config.weights.client_history,
            "appointment_type": ml_config.weights.appointment_type,
            "segment": ml_config.weights.segment,
            "days_until": ml_config.weights.days_until,
            "weather": ml_config.weights.weather,
            "move_history": ml_config.weights.move_history
        }

        # Risk scores by day of week (0=Monday, 6=Sunday)
        self.day_of_week_risk = {
            0: ml_config.day_of_week_risk.monday,
            1: ml_config.day_of_week_risk.tuesday,
            2: ml_config.day_of_week_risk.wednesday,
            3: ml_config.day_of_week_risk.thursday,
            4: ml_config.day_of_week_risk.friday,
            5: ml_config.day_of_week_risk.saturday,
            6: ml_config.day_of_week_risk.sunday,
        }

        # Risk scores by time bucket
        self.time_bucket_risk = {
            "early_morning": ml_config.time_bucket_risk.early_morning,
            "morning": ml_config.time_bucket_risk.morning,
            "afternoon": ml_config.time_bucket_risk.afternoon,
            "late_afternoon": ml_config.time_bucket_risk.late_afternoon,
            "evening": ml_config.time_bucket_risk.evening,
        }

        # Time bucket boundaries
        self.time_buckets = ml_config.time_buckets

        # Risk by client segment
        self.segment_risk = {
            "vip": ml_config.segment_risk.vip,
            "regular": ml_config.segment_risk.regular,
            "new": ml_config.segment_risk.new,
            "high_risk": ml_config.segment_risk.high_risk,
        }

        # Appointment type risk
        self.appointment_type_risk = {
            "routine": ml_config.appointment_type_risk.routine,
            "follow_up": ml_config.appointment_type_risk.follow_up,
            "consultation": ml_config.appointment_type_risk.consultation,
            "procedure": ml_config.appointment_type_risk.procedure,
            "emergency": ml_config.appointment_type_risk.emergency,
        }
        self.default_appointment_type_risk = ml_config.appointment_type_risk.default

        # Move history penalties
        self.move_penalty = {
            0: ml_config.move_penalty.zero_moves,
            1: ml_config.move_penalty.one_move,
            2: ml_config.move_penalty.two_moves,
        }
        self.move_penalty_three_plus = ml_config.move_penalty.three_plus_moves

        # Client history config
        self.client_history_config = ml_config.client_history

        # Days until risk config
        self.days_until_config = ml_config.days_until_risk

        # Flexibility config
        self.flexibility_config = ml_config.flexibility

    def get_time_bucket(self, appointment_time: datetime) -> str:
        """
        Determine time bucket from appointment time.

        Args:
            appointment_time: Appointment datetime

        Returns:
            Time bucket name
        """
        hour = appointment_time.hour
        tb = self.time_buckets

        if tb.early_morning_start <= hour < tb.early_morning_end:
            return "early_morning"
        elif tb.morning_start <= hour < tb.morning_end:
            return "morning"
        elif tb.afternoon_start <= hour < tb.afternoon_end:
            return "afternoon"
        elif tb.late_afternoon_start <= hour < tb.late_afternoon_end:
            return "late_afternoon"
        else:
            return "evening"

    def calculate_client_history_risk(self, client_data: Dict) -> float:
        """
        Calculate risk based on client history.

        Uses configurable weights for no-shows vs cancellations.
        Blends with neutral risk for new clients (< threshold appointments).

        Args:
            client_data: Dictionary with client statistics

        Returns:
            Risk score between 0.0 and 1.0
        """
        no_show_rate = client_data.get("no_show_rate", 0.0)
        cancellation_rate = client_data.get("cancellation_rate", 0.0)
        total_appointments = client_data.get("total_appointments", 0)

        # Weight no-shows more heavily than cancellations (configurable)
        cfg = self.client_history_config
        base_risk = (
            no_show_rate * cfg.no_show_weight +
            cancellation_rate * cfg.cancellation_weight
        )

        # Reduce confidence for new clients (blend with neutral risk)
        if total_appointments < cfg.min_appointments_threshold:
            base_risk = (
                base_risk * cfg.blend_factor +
                cfg.neutral_risk * (1 - cfg.blend_factor)
            )

        return min(max(base_risk, 0.0), 1.0)

    def calculate_days_until_risk(self, days_until: int) -> float:
        """
        Calculate risk based on days until appointment.

        Uses configurable thresholds and risk scores.

        Args:
            days_until: Days until appointment

        Returns:
            Risk score between 0.0 and 1.0
        """
        cfg = self.days_until_config

        if days_until < cfg.very_close_threshold:
            return cfg.very_close_risk
        elif days_until < cfg.close_threshold:
            return cfg.close_risk
        elif days_until < cfg.medium_threshold:
            return cfg.medium_risk
        elif days_until < cfg.far_threshold:
            return cfg.far_risk
        else:
            return cfg.very_far_risk

    def calculate_appointment_type_risk(self, appointment_type: str) -> float:
        """
        Calculate risk based on appointment type.

        Args:
            appointment_type: Type of appointment

        Returns:
            Risk score between 0.0 and 1.0
        """
        if not appointment_type:
            return self.default_appointment_type_risk

        # Normalize to lowercase
        type_normalized = appointment_type.lower()

        return self.appointment_type_risk.get(
            type_normalized,
            self.default_appointment_type_risk
        )

    def calculate_move_history_risk(self, move_count: int) -> float:
        """
        Calculate risk penalty for appointments moved multiple times.

        Args:
            move_count: Number of times appointment has been moved

        Returns:
            Risk penalty between 0.0 and 1.0
        """
        if move_count in self.move_penalty:
            return self.move_penalty[move_count]
        else:
            # 3+ moves
            return self.move_penalty_three_plus

    def predict_no_show_risk(
        self,
        appointment_time: datetime,
        client_data: Dict,
        appointment_type: str = "routine",
        move_count: int = 0,
        weather_risk: float = 0.0
    ) -> float:
        """
        Calculate deterministic no-show risk score.

        Combines multiple features with configurable weights.

        Args:
            appointment_time: Appointment datetime
            client_data: Dictionary with client statistics
            appointment_type: Type of appointment
            move_count: Number of times moved
            weather_risk: Weather risk factor (0.0-1.0)

        Returns:
            float: Risk score between 0.0 and 1.0
        """
        # Extract features
        day_of_week = appointment_time.weekday()
        time_bucket = self.get_time_bucket(appointment_time)
        days_until = (appointment_time - datetime.now()).days

        # Calculate component risks
        day_risk = self.day_of_week_risk[day_of_week]
        time_risk = self.time_bucket_risk[time_bucket]
        history_risk = self.calculate_client_history_risk(client_data)
        type_risk = self.calculate_appointment_type_risk(appointment_type)
        segment_risk = self.segment_risk.get(
            client_data.get("segment", "regular"),
            self.segment_risk["regular"]
        )
        days_risk = self.calculate_days_until_risk(days_until)
        move_risk = self.calculate_move_history_risk(move_count)

        # Weighted combination (all weights from config)
        risk_score = (
            self.weights["day_of_week"] * day_risk +
            self.weights["time_bucket"] * time_risk +
            self.weights["client_history"] * history_risk +
            self.weights["appointment_type"] * type_risk +
            self.weights["segment"] * segment_risk +
            self.weights["days_until"] * days_risk +
            self.weights["weather"] * weather_risk +
            self.weights["move_history"] * move_risk
        )

        # Ensure bounds
        return min(max(risk_score, 0.0), 1.0)

    def calculate_flexibility_score(
        self,
        appointment_time: datetime,
        client_data: Dict,
        is_movable: bool = True,
        move_count: int = 0
    ) -> float:
        """
        Calculate how flexible/movable an appointment is.

        Higher score = better candidate for moving in cascade optimization.
        Uses configurable multipliers for different client segments.

        Args:
            appointment_time: Appointment datetime
            client_data: Dictionary with client statistics
            is_movable: Whether appointment is marked as movable
            move_count: Number of times already moved

        Returns:
            float: Flexibility score between 0.0 and 1.0
        """
        if not is_movable:
            return 0.0

        cfg = self.flexibility_config

        # Start with base flexibility
        flexibility = cfg.base_score

        # Adjust based on client segment (configurable multipliers)
        segment = client_data.get("segment", "regular")
        if segment == "vip":
            flexibility *= cfg.vip_multiplier
        elif segment == "high_risk":
            flexibility *= cfg.high_risk_multiplier
        else:
            flexibility *= cfg.regular_multiplier

        # Reduce if moved multiple times (exponential decay with configurable rate)
        flexibility *= math.exp(-cfg.move_decay_rate * move_count)

        # Check if client explicitly marked as flexible
        if client_data.get("is_flexible", True):
            flexibility *= cfg.flexible_client_multiplier
        else:
            flexibility *= cfg.inflexible_penalty

        return min(flexibility, 1.0)
