"""
ML Advisor - Advisory Layer for Risk Prediction

This module wraps the ML engine to implement the RiskAdvisor protocol.

IMPORTANT: This is ADVISORY ONLY.
- Reads workflow state
- Produces suggestions (risk scores, flexibility)
- NEVER executes anything
- NEVER mutates state

The orchestrator makes final decisions based on advisory input.
"""

from typing import Dict, Any, Optional
from datetime import datetime
import math

from jenga.core.state.workflow_state import WorkflowInstance
from jenga.core.decisions.decision_gateway import RiskAssessment, RiskLevel, RiskAdvisor


class MLRiskAdvisor:
    """
    ML-based risk advisor implementing the RiskAdvisor protocol.

    This is a thin wrapper around the configuration-driven ML engine.
    All weights and thresholds come from configuration.

    Can be replaced with any implementation that follows the protocol.
    """

    def __init__(self, ml_config: Optional[Dict[str, Any]] = None):
        """
        Initialize ML advisor.

        Args:
            ml_config: ML configuration (weights, thresholds, etc.)
                       If None, uses sensible defaults.
        """
        self._config = ml_config or {}
        self._version = self._config.get("version", "v2.0")

        # Feature weights (must sum to ~1.0)
        self._weights = self._config.get("weights", {
            "day_of_week": 0.10,
            "time_bucket": 0.10,
            "client_history": 0.30,
            "appointment_type": 0.10,
            "segment": 0.15,
            "days_until": 0.15,
            "weather": 0.00,
            "move_history": 0.10
        })

        # Risk thresholds
        self._thresholds = self._config.get("risk_thresholds", {
            "high": 0.7,
            "medium": 0.4,
            "low": 0.2
        })

        # Day of week risk (0=Monday, 6=Sunday)
        self._day_risk = self._config.get("day_of_week_risk", {
            0: 0.5, 1: 0.4, 2: 0.4, 3: 0.5, 4: 0.6, 5: 0.7, 6: 0.3
        })

        # Time bucket risk
        self._time_risk = self._config.get("time_bucket_risk", {
            "early_morning": 0.7,
            "morning": 0.4,
            "afternoon": 0.5,
            "late_afternoon": 0.6,
            "evening": 0.7
        })

        # Segment risk
        self._segment_risk = self._config.get("segment_risk", {
            "vip": 0.2,
            "regular": 0.4,
            "new": 0.5,
            "high_risk": 0.8
        })

        # Flexibility config
        self._flexibility = self._config.get("flexibility", {
            "base_score": 0.7,
            "vip_multiplier": 0.5,
            "high_risk_multiplier": 0.3,
            "regular_multiplier": 1.0,
            "move_decay_rate": 0.3
        })

    def assess_risk(
        self,
        workflow: WorkflowInstance,
        client_data: Dict[str, Any]
    ) -> RiskAssessment:
        """
        Assess no-show risk for a workflow.

        Implements the RiskAdvisor protocol.
        Returns advisory output only - no side effects.
        """
        factors = {}

        # Day of week factor
        day = workflow.appointment_time.weekday()
        day_risk = self._day_risk.get(day, 0.5)
        factors["day_of_week"] = day_risk

        # Time bucket factor
        hour = workflow.appointment_time.hour
        time_bucket = self._get_time_bucket(hour)
        time_risk = self._time_risk.get(time_bucket, 0.5)
        factors["time_bucket"] = time_risk

        # Client history factor
        history_risk = self._calculate_history_risk(client_data)
        factors["client_history"] = history_risk

        # Segment factor
        segment = client_data.get("segment", "regular")
        segment_risk = self._segment_risk.get(segment, 0.4)
        factors["segment"] = segment_risk

        # Days until factor
        days_until = (workflow.appointment_time - datetime.utcnow()).days
        days_risk = self._calculate_days_until_risk(days_until)
        factors["days_until"] = days_risk

        # Move history factor
        move_risk = min(0.2 + (workflow.move_count * 0.15), 0.8)
        factors["move_history"] = move_risk

        # Weighted combination
        risk_score = (
            self._weights.get("day_of_week", 0.1) * day_risk +
            self._weights.get("time_bucket", 0.1) * time_risk +
            self._weights.get("client_history", 0.3) * history_risk +
            self._weights.get("segment", 0.15) * segment_risk +
            self._weights.get("days_until", 0.15) * days_risk +
            self._weights.get("move_history", 0.1) * move_risk
        )

        # Clamp to [0, 1]
        risk_score = max(0.0, min(1.0, risk_score))

        # Classify level
        risk_level = self._classify_risk(risk_score)

        return RiskAssessment(
            workflow_id=workflow.id,
            risk_score=risk_score,
            risk_level=risk_level,
            confidence=0.85,  # Model confidence
            factors=factors,
            model_version=self._version,
            calculated_at=datetime.utcnow()
        )

    def calculate_flexibility(
        self,
        workflow: WorkflowInstance,
        client_data: Dict[str, Any]
    ) -> float:
        """
        Calculate how flexible/movable a workflow is.

        Higher score = better candidate for cascade optimization.
        """
        if not workflow.is_movable:
            return 0.0

        cfg = self._flexibility
        flex = cfg.get("base_score", 0.7)

        # Segment multiplier
        segment = client_data.get("segment", "regular")
        if segment == "vip":
            flex *= cfg.get("vip_multiplier", 0.5)
        elif segment == "high_risk":
            flex *= cfg.get("high_risk_multiplier", 0.3)
        else:
            flex *= cfg.get("regular_multiplier", 1.0)

        # Move count decay
        decay_rate = cfg.get("move_decay_rate", 0.3)
        flex *= math.exp(-decay_rate * workflow.move_count)

        # Client flexibility flag
        if client_data.get("is_flexible", True):
            flex *= 1.2
        else:
            flex *= 0.6

        return min(flex, 1.0)

    def _get_time_bucket(self, hour: int) -> str:
        """Map hour to time bucket"""
        if 6 <= hour < 9:
            return "early_morning"
        elif 9 <= hour < 12:
            return "morning"
        elif 12 <= hour < 15:
            return "afternoon"
        elif 15 <= hour < 18:
            return "late_afternoon"
        else:
            return "evening"

    def _calculate_history_risk(self, client_data: Dict[str, Any]) -> float:
        """Calculate risk from client history"""
        no_show_rate = client_data.get("no_show_rate", 0.0)
        cancel_rate = client_data.get("cancellation_rate", 0.0)
        total = client_data.get("total_appointments", 0)

        # Weight no-shows more heavily
        base_risk = no_show_rate * 0.7 + cancel_rate * 0.3

        # Reduce confidence for new clients
        if total < 3:
            base_risk = base_risk * 0.5 + 0.5 * 0.5  # Blend with neutral

        return min(base_risk, 1.0)

    def _calculate_days_until_risk(self, days: int) -> float:
        """Calculate risk based on days until appointment"""
        if days <= 1:
            return 0.2  # Very close - low risk
        elif days <= 3:
            return 0.3
        elif days <= 7:
            return 0.4
        elif days <= 14:
            return 0.5
        elif days <= 30:
            return 0.6
        else:
            return 0.7  # Far out - higher risk

    def _classify_risk(self, score: float) -> RiskLevel:
        """Classify risk score into level"""
        if score >= self._thresholds.get("high", 0.7):
            return RiskLevel.HIGH
        elif score >= self._thresholds.get("medium", 0.4):
            return RiskLevel.MEDIUM
        else:
            return RiskLevel.LOW


class NullAdvisor:
    """
    Null advisor for when ML is disabled.

    Returns neutral assessments.
    Demonstrates that Jenga works WITHOUT ML.
    """

    def assess_risk(
        self,
        workflow: WorkflowInstance,
        client_data: Dict[str, Any]
    ) -> RiskAssessment:
        """Return neutral risk assessment"""
        return RiskAssessment(
            workflow_id=workflow.id,
            risk_score=0.5,
            risk_level=RiskLevel.MEDIUM,
            confidence=0.0,
            factors={"advisory": "disabled"},
            model_version="null",
            calculated_at=datetime.utcnow()
        )

    def calculate_flexibility(
        self,
        workflow: WorkflowInstance,
        client_data: Dict[str, Any]
    ) -> float:
        """Return default flexibility"""
        if not workflow.is_movable:
            return 0.0
        return max(0.5, 1.0 - (workflow.move_count * 0.2))


def create_advisor_from_config(config: Dict[str, Any]) -> MLRiskAdvisor:
    """
    Factory function to create advisor from configuration.

    Args:
        config: Full application config containing 'ml' section

    Returns:
        Configured MLRiskAdvisor
    """
    ml_config = config.get("ml", {})
    return MLRiskAdvisor(ml_config)
