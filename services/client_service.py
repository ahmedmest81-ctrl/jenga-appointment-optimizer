"""
Client Service

Handles client-related business operations:
- Client creation with validation
- Client statistics updates (safe rate calculations)
- Client segment management
- Client metrics calculation
"""

from sqlalchemy.orm import Session
from typing import Optional
from datetime import datetime

from models import Client, ClientSegment
from config_loader import config
from exceptions import ResourceNotFoundError, ValidationError
import utils


class ClientService:
    """Service for client management operations"""

    def __init__(self, db: Session):
        """
        Initialize client service.

        Args:
            db: Database session
        """
        self.db = db
        self.config = config

    def get_client_by_id(self, client_id: int, business_id: int) -> Client:
        """
        Get client by ID with business isolation.

        Args:
            client_id: Client ID
            business_id: Business ID for multi-tenant isolation

        Returns:
            Client object

        Raises:
            ResourceNotFoundError: If client not found
        """
        client = self.db.query(Client).filter(
            Client.id == client_id,
            Client.business_id == business_id
        ).first()

        if not client:
            raise ResourceNotFoundError(
                f"Client {client_id} not found for business {business_id}"
            )

        return client

    def get_client_by_external_id(
        self,
        external_id: str,
        business_id: int
    ) -> Optional[Client]:
        """
        Get client by external ID.

        Args:
            external_id: External system's client ID
            business_id: Business ID for multi-tenant isolation

        Returns:
            Client object or None if not found
        """
        return self.db.query(Client).filter(
            Client.external_id == external_id,
            Client.business_id == business_id
        ).first()

    def update_client_stats_on_appointment_created(
        self,
        client: Client
    ) -> None:
        """
        Update client statistics when appointment is created.

        Args:
            client: Client to update
        """
        client.total_appointments += 1
        # No need to commit - calling code handles transaction

    def update_client_stats_on_cancellation(
        self,
        client: Client
    ) -> None:
        """
        Update client statistics when appointment is cancelled.

        Uses safe rate calculation to prevent division by zero.

        Args:
            client: Client to update
        """
        client.cancelled_appointments += 1

        # Safe rate calculation (prevents division by zero)
        client.cancellation_rate = utils.safe_rate_calculation(
            client.cancelled_appointments,
            client.total_appointments
        )

        # No need to commit - calling code handles transaction

    def update_client_stats_on_completion(
        self,
        client: Client
    ) -> None:
        """
        Update client statistics when appointment is completed.

        Args:
            client: Client to update
        """
        client.completed_appointments += 1
        # No need to commit - calling code handles transaction

    def update_client_stats_on_no_show(
        self,
        client: Client
    ) -> None:
        """
        Update client statistics when appointment is marked no-show.

        Uses safe rate calculation and updates segment if threshold exceeded.

        Args:
            client: Client to update
        """
        client.no_show_appointments += 1

        # Safe rate calculation (prevents division by zero)
        client.no_show_rate = utils.safe_rate_calculation(
            client.no_show_appointments,
            client.total_appointments
        )

        # Update segment to HIGH_RISK if threshold exceeded
        high_risk_threshold = self.config.client_segments.high_risk_threshold
        if client.no_show_rate > high_risk_threshold:
            client.segment = ClientSegment.HIGH_RISK

        # No need to commit - calling code handles transaction

    def calculate_client_metrics(self, client: Client) -> dict:
        """
        Calculate comprehensive client behavioral metrics.

        Args:
            client: Client to analyze

        Returns:
            Dictionary with calculated metrics
        """
        if client.total_appointments == 0:
            return {
                "no_show_rate": 0.0,
                "cancellation_rate": 0.0,
                "completion_rate": 0.0,
                "total_appointments": 0,
                "segment": client.segment.value,
                "is_flexible": client.is_flexible
            }

        return {
            "no_show_rate": utils.safe_rate_calculation(
                client.no_show_appointments,
                client.total_appointments
            ),
            "cancellation_rate": utils.safe_rate_calculation(
                client.cancelled_appointments,
                client.total_appointments
            ),
            "completion_rate": utils.safe_rate_calculation(
                client.completed_appointments,
                client.total_appointments
            ),
            "total_appointments": client.total_appointments,
            "completed_appointments": client.completed_appointments,
            "cancelled_appointments": client.cancelled_appointments,
            "no_show_appointments": client.no_show_appointments,
            "segment": client.segment.value,
            "is_flexible": client.is_flexible
        }

    def recalculate_all_rates(self, client: Client) -> None:
        """
        Recalculate all client rates from scratch.

        Useful for data reconciliation or fixing inconsistencies.

        Args:
            client: Client to recalculate
        """
        client.no_show_rate = utils.safe_rate_calculation(
            client.no_show_appointments,
            client.total_appointments
        )

        client.cancellation_rate = utils.safe_rate_calculation(
            client.cancelled_appointments,
            client.total_appointments
        )

        # Update segment if needed
        high_risk_threshold = self.config.client_segments.high_risk_threshold
        if client.no_show_rate > high_risk_threshold:
            client.segment = ClientSegment.HIGH_RISK
        elif client.segment == ClientSegment.HIGH_RISK and client.no_show_rate <= high_risk_threshold:
            # Optionally downgrade from HIGH_RISK if rate improves
            # This is a business decision - may want to keep HIGH_RISK sticky
            pass

    def is_high_risk(self, client: Client) -> bool:
        """
        Check if client is high risk.

        Args:
            client: Client to check

        Returns:
            True if client is high risk
        """
        return client.segment == ClientSegment.HIGH_RISK or \
               client.no_show_rate > self.config.client_segments.high_risk_threshold
