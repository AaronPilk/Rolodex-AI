from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.date import DateTrigger
except ModuleNotFoundError:  # pragma: no cover
    AsyncIOScheduler = None  # type: ignore[assignment]
    CronTrigger = None  # type: ignore[assignment]
    DateTrigger = None  # type: ignore[assignment]

from agent.config import get_settings
from agent.store import load_store, store_path

log = logging.getLogger("rolodex.scheduler")

DigestCallback = Callable[[], Awaitable[object]]
_ACTIVE_SCHEDULER: "RolodexScheduler | None" = None


def _parse_quiet_hours() -> tuple[int, int] | None:
    raw = os.environ.get("ROLODEX_QUIET_HOURS") or os.environ.get("TELEGRAM_QUIET_HOURS")
    if not raw or "-" not in raw:
        return None
    try:
        start, end = raw.split("-", 1)
        return int(start), int(end)
    except ValueError:
        return None


def _is_quiet_hour(now: datetime, quiet_hours: tuple[int, int] | None) -> bool:
    if not quiet_hours:
        return False
    start, end = quiet_hours
    hour = now.hour
    if start <= end:
        return start <= hour < end
    return hour >= start or hour < end


def _next_allowed_time(now: datetime, quiet_hours: tuple[int, int] | None) -> datetime:
    if not quiet_hours or not _is_quiet_hour(now, quiet_hours):
        return now
    _start, end = quiet_hours
    candidate = now.replace(hour=end, minute=0, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def digest_already_sent_today(*, settings=None, now: datetime | None = None) -> bool:
    settings = settings or get_settings()
    timezone = ZoneInfo(os.environ.get("ROLODEX_TIMEZONE", "America/New_York"))
    now = now or datetime.now(timezone)
    store = load_store(store_path(settings))
    if not store.last_digest_at:
        return False
    try:
        sent_at = datetime.fromisoformat(store.last_digest_at).astimezone(timezone)
    except ValueError:
        return False
    return sent_at.date() == now.date()


def next_digest_fire_at(*, now: datetime | None = None, cron_expression: str | None = None, timezone: str | None = None) -> datetime:
    timezone = timezone or os.environ.get("ROLODEX_TIMEZONE", "America/New_York")
    tzinfo = ZoneInfo(timezone)
    now = now.astimezone(tzinfo) if now else datetime.now(tzinfo)
    if CronTrigger is None:
        minute, hour, *_rest = (cron_expression or os.environ.get("ROLODEX_DIGEST_CRON", "0 9 * * *")).split()
        next_fire = now.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
        if next_fire <= now:
            next_fire += timedelta(days=1)
    else:
        trigger = CronTrigger.from_crontab(cron_expression or os.environ.get("ROLODEX_DIGEST_CRON", "0 9 * * *"), timezone=tzinfo)
        next_fire = trigger.get_next_fire_time(None, now)
        if next_fire is None:
            raise RuntimeError("Digest cron has no next fire time")
    return _next_allowed_time(next_fire, _parse_quiet_hours())


def get_active_scheduler() -> "RolodexScheduler | None":
    return _ACTIVE_SCHEDULER


class RolodexScheduler:
    def __init__(
        self,
        *,
        run_callback: DigestCallback,
        cron_expression: str | None = None,
        timezone: str | None = None,
        settings=None,
    ) -> None:
        self.run_callback = run_callback
        self.settings = settings or get_settings()
        self.cron_expression = cron_expression or os.environ.get("ROLODEX_DIGEST_CRON", "0 9 * * *")
        self.timezone = timezone or os.environ.get("ROLODEX_TIMEZONE", "America/New_York")
        self.quiet_hours = _parse_quiet_hours()
        self._sched = AsyncIOScheduler(timezone=self.timezone) if AsyncIOScheduler is not None else None

    async def _run_scheduled(self) -> None:
        now = datetime.now(ZoneInfo(self.timezone))
        if digest_already_sent_today(settings=self.settings, now=now):
            log.info("skipping digest; already sent today")
            return
        if _is_quiet_hour(now, self.quiet_hours):
            deferred = _next_allowed_time(now, self.quiet_hours)
            if self._sched is not None and DateTrigger is not None:
                self._sched.add_job(
                    self._run_scheduled,
                    trigger=DateTrigger(run_date=deferred, timezone=self.timezone),
                    id="rolodex_daily_digest_deferred",
                    replace_existing=True,
                )
            log.info("digest deferred to %s due to quiet hours", deferred.isoformat())
            return
        await self.run_callback()

    def start(self) -> None:
        global _ACTIVE_SCHEDULER
        if self._sched is not None and CronTrigger is not None:
            trigger = CronTrigger.from_crontab(self.cron_expression, timezone=self.timezone)
            self._sched.add_job(
                self._run_scheduled,
                trigger=trigger,
                id="rolodex_daily_digest",
                replace_existing=True,
            )
            self._sched.start()
        _ACTIVE_SCHEDULER = self
        log.info("scheduler started cron=%s tz=%s", self.cron_expression, self.timezone)

    def shutdown(self) -> None:
        global _ACTIVE_SCHEDULER
        if self._sched is not None and self._sched.running:
            self._sched.shutdown(wait=False)
        if _ACTIVE_SCHEDULER is self:
            _ACTIVE_SCHEDULER = None

    async def run_now(self) -> None:
        await self.run_callback()
