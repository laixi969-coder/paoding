from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler

from paoding.services.worker import schedule_daily, schedule_weekly


def run_scheduler(runtime):
    zone = ZoneInfo(runtime.settings.timezone)
    scheduler = BlockingScheduler(timezone=zone)

    def daily():
        schedule_daily(runtime, datetime.now(zone).strftime("%Y-%m-%d"))

    def weekly():
        schedule_weekly(runtime, datetime.now(zone).strftime("%G-W%V"))

    scheduler.add_job(
        daily, "cron", hour=2, minute=0, id="daily-watch", coalesce=True, max_instances=1
    )
    scheduler.add_job(
        weekly,
        "cron",
        day_of_week="mon",
        hour=3,
        minute=0,
        id="weekly-evolution",
        coalesce=True,
        max_instances=1,
    )
    # Catch up the current day/week after a restart; DB dedupe prevents duplicate fan-out.
    daily()
    weekly()
    scheduler.start()
