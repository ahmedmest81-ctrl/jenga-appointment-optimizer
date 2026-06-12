from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from scheduler import run_jenga_cycle
from calendar_sync import read_google_calendar
from database import get_db

scheduler = BackgroundScheduler()

# Job to run Jenga optimization every day at 6 AM
scheduler.add_job(run_jenga_cycle, CronTrigger(hour=6, minute=0), id="jenga_daily")

# Job to sync Google Calendar every 5 minutes
def sync_job():
    db = next(get_db())
    read_google_calendar(db)

scheduler.add_job(sync_job, IntervalTrigger(minutes=5), id="calendar_sync", replace_existing=True)

def start_scheduler():
    scheduler.start()
