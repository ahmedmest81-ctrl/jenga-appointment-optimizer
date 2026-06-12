from datetime import datetime, timedelta

from config_loader import config
from ml import MLEngineV2


def test_risk_score_is_deterministic_and_bounded() -> None:
    engine = MLEngineV2(config.ml)
    appointment_time = datetime.now().replace(hour=16, minute=0, second=0, microsecond=0)
    appointment_time += timedelta(days=10)
    client = {
        "no_show_rate": 0.25,
        "cancellation_rate": 0.10,
        "total_appointments": 8,
        "segment": "regular",
    }

    first = engine.predict_no_show_risk(appointment_time, client, "consultation")
    second = engine.predict_no_show_risk(appointment_time, client, "consultation")

    assert first == second
    assert 0.0 <= first <= 1.0


def test_client_history_changes_risk_in_expected_direction() -> None:
    engine = MLEngineV2(config.ml)
    appointment_time = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
    appointment_time += timedelta(days=5)

    reliable = {
        "no_show_rate": 0.0,
        "cancellation_rate": 0.0,
        "total_appointments": 20,
        "segment": "regular",
    }
    unreliable = {
        "no_show_rate": 0.8,
        "cancellation_rate": 0.4,
        "total_appointments": 20,
        "segment": "high_risk",
    }

    assert engine.predict_no_show_risk(
        appointment_time, unreliable
    ) > engine.predict_no_show_risk(appointment_time, reliable)
