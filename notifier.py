from typing import Optional, Dict
from datetime import datetime
from enum import Enum
import logging
from models import NotificationLog, NotificationChannel, NotificationType, Appointment
from sqlalchemy.orm import Session
import os
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail 

import os
from dotenv import load_dotenv

load_dotenv()  # This reads .env values into environment variables
logger = logging.getLogger(__name__)

def send_email(to_email, subject, content):
    message = Mail(
        from_email=os.getenv("SENDGRID_FROM_EMAIL"),
        to_emails=to_email,
        subject=subject,
        plain_text_content=content,
    )
    try:
        sg = SendGridAPIClient(os.getenv("SENDGRID_API_KEY"))
        response = sg.send(message)
        print(f"Email sent to {to_email} | Status Code: {response.status_code}")
    except Exception as e:
        print(f"Error sending email: {e}")


class NotificationStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


class NotificationService:
    """Multi-channel notification service with fallback chain."""
    
    def __init__(self, db: Session):
        self.db = db
    
    def send_notification(
        self,
        appointment: Appointment,
        notification_type: NotificationType,
        message: str,
        force_channel: Optional[NotificationChannel] = None
    ) -> NotificationStatus:
        """
        Send notification with fallback chain: SMS → Email → WhatsApp
        """
        channels = [force_channel] if force_channel else [
            NotificationChannel.SMS,
            NotificationChannel.EMAIL,
            NotificationChannel.WHATSAPP
        ]
        
        for channel in channels:
            status = self._send_via_channel(
                appointment=appointment,
                channel=channel,
                notification_type=notification_type,
                message=message
            )
            
            if status == NotificationStatus.SUCCESS:
                return status
        
        logger.error(f"All notification channels failed for appointment {appointment.id}")
        return NotificationStatus.FAILED
    
    def _send_via_channel(
        self,
        appointment: Appointment,
        channel: NotificationChannel,
        notification_type: NotificationType,
        message: str
    ) -> NotificationStatus:
        """Send via specific channel and log result."""
        
        # Determine recipient
        if channel == NotificationChannel.SMS or channel == NotificationChannel.WHATSAPP:
            recipient = appointment.client.phone
        else:
            recipient = appointment.client.email
        
        if not recipient:
            return NotificationStatus.SKIPPED
        
        # Check idempotency
        existing = self.db.query(NotificationLog).filter(
            NotificationLog.appointment_id == appointment.id,
            NotificationLog.notification_type == notification_type,
            NotificationLog.channel == channel,
            NotificationLog.delivered == True
        ).first()
        
        if existing:
            logger.info(f"Notification already sent: {notification_type} via {channel}")
            return NotificationStatus.SKIPPED
        
        # Send via channel
        success = False
        error_message = None
        external_id = None
        
        try:
            if channel == NotificationChannel.SMS:
                success, external_id = self._send_sms(recipient, message)
            elif channel == NotificationChannel.EMAIL:
                success, external_id = self._send_email(recipient, message)
            elif channel == NotificationChannel.WHATSAPP:
                success, external_id = self._send_whatsapp(recipient, message)
        except Exception as e:
            logger.error(f"Error sending {channel}: {str(e)}")
            error_message = str(e)
            success = False
        
        # Log notification
        log = NotificationLog(
            business_id=appointment.business_id,
            appointment_id=appointment.id,
            notification_type=notification_type,
            channel=channel,
            recipient=recipient,
            message=message,
            delivered=success,
            failed=not success,
            error_message=error_message,
            external_id=external_id
        )
        self.db.add(log)
        self.db.commit()
        
        return NotificationStatus.SUCCESS if success else NotificationStatus.FAILED
    
    def _send_sms(self, phone: str, message: str) -> tuple[bool, Optional[str]]:
        """Send SMS via Twilio."""
        from config import settings
        
        if not settings.TWILIO_ACCOUNT_SID:
            logger.warning("Twilio not configured, skipping SMS")
            return False, None
        
        try:
            from twilio.rest import Client
            client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
            
            msg = client.messages.create(
                body=message,
                from_=settings.TWILIO_PHONE_NUMBER,
                to=phone
            )
            
            return True, msg.sid
        except Exception as e:
            logger.error(f"SMS send failed: {str(e)}")
            return False, None
    
    def _send_email(self, email: str, message: str) -> tuple[bool, Optional[str]]:
        """Send email via SendGrid."""
        from config import settings
        
        if not settings.SENDGRID_API_KEY:
            logger.warning("SendGrid not configured, skipping email")
            return False, None
        
        try:
            from sendgrid import SendGridAPIClient
            from sendgrid.helpers.mail import Mail
            
            mail = Mail(
                from_email=settings.SENDGRID_FROM_EMAIL,
                to_emails=email,
                subject="Appointment Reminder",
                plain_text_content=message
            )
            
            sg = SendGridAPIClient(settings.SENDGRID_API_KEY)
            response = sg.send(mail)
            
            return response.status_code == 202, response.headers.get("X-Message-Id")
        except Exception as e:
            logger.error(f"Email send failed: {str(e)}")
            return False, None
    
    def _send_whatsapp(self, phone: str, message: str) -> tuple[bool, Optional[str]]:
        """Send WhatsApp message."""
        from config import settings
        
        if not settings.WHATSAPP_API_KEY:
            logger.warning("WhatsApp not configured, skipping")
            return False, None
        
        # Placeholder for WhatsApp Business API integration
        # Implementation depends on specific provider
        logger.info(f"WhatsApp message would be sent to {phone}")
        return False, None
    
    def send_7_day_reminder(self, appointment: Appointment) -> NotificationStatus:
        """Send 7-day advance reminder."""
        message = self._build_7_day_message(appointment)
        return self.send_notification(
            appointment=appointment,
            notification_type=NotificationType.REMINDER_7_DAY,
            message=message
        )
    
    def send_48_hour_reminder(self, appointment: Appointment) -> NotificationStatus:
        """Send 48-hour reminder with cancellation option."""
        message = self._build_48_hour_message(appointment)
        return self.send_notification(
            appointment=appointment,
            notification_type=NotificationType.REMINDER_48_HOUR,
            message=message
        )
    
    def send_auto_confirmation(self, appointment: Appointment) -> NotificationStatus:
        """Send 24-hour auto-confirmation."""
        message = self._build_auto_confirm_message(appointment)
        return self.send_notification(
            appointment=appointment,
            notification_type=NotificationType.AUTO_CONFIRM,
            message=message
        )
    
    def send_shift_offer(self, appointment: Appointment, new_time: datetime) -> NotificationStatus:
        """Send notification offering earlier time slot."""
        message = self._build_shift_offer_message(appointment, new_time)
        return self.send_notification(
            appointment=appointment,
            notification_type=NotificationType.SHIFT_OFFER,
            message=message
        )
    
    def _build_7_day_message(self, appointment: Appointment) -> str:
        """Build 7-day reminder message."""
        return (
            f"Hi {appointment.client.name}! This is a friendly reminder about your "
            f"appointment in 7 days on {appointment.appointment_time.strftime('%B %d at %I:%M %p')}. "
            f"If you need to reschedule, please let us know as soon as possible. "
            f"Thank you!"
        )
    
    def _build_48_hour_message(self, appointment: Appointment) -> str:
        """Build 48-hour reminder message."""
        return (
            f"Hi {appointment.client.name}! Your appointment is in 48 hours "
            f"({appointment.appointment_time.strftime('%B %d at %I:%M %p')}). "
            f"Please cancel or reschedule ASAP if you can't make it. "
            f"This helps us serve other clients better. Thank you!"
        )
    
    def _build_auto_confirm_message(self, appointment: Appointment) -> str:
        """Build auto-confirmation message."""
        return (
            f"Hi {appointment.client.name}! Your appointment tomorrow "
            f"({appointment.appointment_time.strftime('%B %d at %I:%M %p')}) "
            f"is confirmed. We look forward to seeing you!"
        )
    
    def _build_shift_offer_message(self, appointment: Appointment, new_time: datetime) -> str:
        """Build shift offer message."""
        return (
            f"Hi {appointment.client.name}! Great news - we have an earlier slot available "
            f"on {new_time.strftime('%B %d at %I:%M %p')} (originally {appointment.appointment_time.strftime('%B %d at %I:%M %p')}). "
            f"Would you like to move your appointment earlier? Reply YES to confirm."
        )