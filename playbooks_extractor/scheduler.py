"""Standard 5-field crontab parser, validator, and async background library sync/export scheduler."""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_CRON_EXPRESSION = "0 2 * * *"

CRON_ALIASES: dict[str, str] = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}

MONTH_NAMES: dict[str, int] = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

DOW_NAMES: dict[str, int] = {
    "sun": 0,
    "mon": 1,
    "tue": 2,
    "wed": 3,
    "thu": 4,
    "fri": 5,
    "sat": 6,
}

MAX_DAYS_PER_MONTH: dict[int, int] = {
    1: 31,
    2: 29,
    3: 31,
    4: 30,
    5: 31,
    6: 30,
    7: 31,
    8: 31,
    9: 30,
    10: 31,
    11: 30,
    12: 31,
}

INT_TOKEN_RE = re.compile(r"^[+-]?\d+$")


class CronValidationError(ValueError):
    """Raised when a crontab schedule expression is syntactically or semantically invalid."""


def resolve_local_timezone() -> tzinfo:
    """Resolve the active container/host timezone (honoring TZ if set to an IANA zone)."""
    tz_env = os.environ.get("TZ", "").strip()
    if tz_env:
        try:
            return ZoneInfo(tz_env)
        except ZoneInfoNotFoundError:
            pass
    local_tz = datetime.now().astimezone().tzinfo
    return local_tz or timezone.utc


def _parse_single_value(
    raw_token: str,
    *,
    field_name: str,
    min_val: int,
    max_val: int,
    names: dict[str, int] | None = None,
) -> int:
    token = raw_token.strip().lower()
    if not token:
        raise CronValidationError(f"Missing value in {field_name} field.")

    if names and token in names:
        value = names[token]
    elif INT_TOKEN_RE.match(token):
        value = int(token)
    else:
        allowed_hint = f"{min_val}–{max_val}"
        if names:
            allowed_hint += f" or {', '.join(k.upper() for k in names)}"
        raise CronValidationError(
            f"Invalid {field_name} value '{raw_token}': expected {allowed_hint}."
        )

    if value < min_val or value > max_val:
        raise CronValidationError(
            f"Out-of-range {field_name} value '{raw_token}': must be between {min_val} and {max_val}."
        )
    return value


def _parse_cron_field(
    field_str: str,
    *,
    field_name: str,
    min_val: int,
    max_val: int,
    names: dict[str, int] | None = None,
    map_seven_to_zero: bool = False,
) -> tuple[frozenset[int], bool]:
    """Parse a single crontab field into (sorted_allowed_values, is_unrestricted_wildcard)."""
    cleaned = field_str.strip()
    if not cleaned:
        raise CronValidationError(f"Empty {field_name} field.")

    is_wildcard = cleaned == "*"
    effective_span = (6 if map_seven_to_zero else max_val) - min_val + 1
    values: set[int] = set()

    for part in cleaned.split(","):
        item = part.strip()
        if not item:
            raise CronValidationError(
                f"Empty list element in {field_name} field '{field_str}'."
            )

        slash_parts = item.split("/")
        if len(slash_parts) > 2:
            raise CronValidationError(
                f"Invalid step syntax '{item}' in {field_name} field: at most one '/' is allowed."
            )

        base = slash_parts[0].strip()
        step: int | None = None
        if len(slash_parts) == 2:
            step_raw = slash_parts[1].strip()
            if not INT_TOKEN_RE.match(step_raw):
                raise CronValidationError(
                    f"Invalid step value '{step_raw}' in {field_name} field '{item}': must be a positive integer."
                )
            step = int(step_raw)
            if step <= 0:
                raise CronValidationError(
                    f"Step value in {field_name} field '{item}' must be at least 1."
                )
            if step > effective_span:
                raise CronValidationError(
                    f"Step value '{step}' in {field_name} field '{item}' exceeds field span ({effective_span})."
                )

        if base == "*":
            start = min_val
            end = 6 if map_seven_to_zero else max_val
            stride = step or 1
            values.update(range(start, end + 1, stride))
        elif "-" in base:
            dash_parts = base.split("-")
            if len(dash_parts) != 2 or not dash_parts[0].strip() or not dash_parts[1].strip():
                raise CronValidationError(
                    f"Invalid range '{base}' in {field_name} field '{item}'."
                )
            start = _parse_single_value(
                dash_parts[0],
                field_name=field_name,
                min_val=min_val,
                max_val=max_val,
                names=names,
            )
            end = _parse_single_value(
                dash_parts[1],
                field_name=field_name,
                min_val=min_val,
                max_val=max_val,
                names=names,
            )
            if start > end:
                raise CronValidationError(
                    f"Range start ({dash_parts[0]}) cannot be greater than end ({dash_parts[1]}) in {field_name} field '{item}'."
                )
            stride = step or 1
            values.update(range(start, end + 1, stride))
        else:
            single = _parse_single_value(
                base,
                field_name=field_name,
                min_val=min_val,
                max_val=max_val,
                names=names,
            )
            if step is not None:
                if map_seven_to_zero and single == 7:
                    single = 0
                end = 6 if map_seven_to_zero else max_val
                values.update(range(single, end + 1, step))
            else:
                values.add(single)

    if map_seven_to_zero and 7 in values:
        values.discard(7)
        values.add(0)

    if not values:
        raise CronValidationError(f"No valid values produced for {field_name} field '{field_str}'.")

    return frozenset(values), is_wildcard


@dataclass(frozen=True)
class CronSchedule:
    """Validated 5-field crontab schedule capable of matching and computing next run times."""

    raw_expression: str
    normalized_expression: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days_of_month: frozenset[int]
    months: frozenset[int]
    days_of_week: frozenset[int]
    dom_wildcard: bool
    dow_wildcard: bool

    def _day_matches(self, dt: datetime) -> bool:
        dom_match = dt.day in self.days_of_month
        cron_dow = (dt.weekday() + 1) % 7  # Cron: 0 = Sunday .. 6 = Saturday
        dow_match = cron_dow in self.days_of_week

        if self.dom_wildcard and self.dow_wildcard:
            return True
        if self.dom_wildcard:
            return dow_match
        if self.dow_wildcard:
            return dom_match
        # Standard Vixie/POSIX cron semantics when both DOM and DOW are restricted: OR
        return dom_match or dow_match

    def matches(self, dt: datetime) -> bool:
        """Return True if dt's minute, hour, day, month, and weekday match this schedule."""
        return (
            dt.minute in self.minutes
            and dt.hour in self.hours
            and dt.month in self.months
            and self._day_matches(dt)
        )

    def next_run_after(self, after: datetime) -> datetime:
        """Return the earliest minute-aligned timestamp strictly after `after` matching this cron."""
        tz = after.tzinfo
        # Work in naive local wall-clock fields so month/day jumps don't drift across DST offsets,
        # then re-attach tzinfo at the end.
        cand = after.replace(tzinfo=None, second=0, microsecond=0) + timedelta(minutes=1)
        sorted_hours = sorted(self.hours)
        sorted_minutes = sorted(self.minutes)
        first_hour = sorted_hours[0]
        first_minute = sorted_minutes[0]

        max_year = cand.year + 8
        while cand.year <= max_year:
            if cand.month not in self.months:
                if cand.month == 12:
                    cand = cand.replace(
                        year=cand.year + 1, month=1, day=1, hour=first_hour, minute=first_minute
                    )
                else:
                    cand = cand.replace(
                        month=cand.month + 1, day=1, hour=first_hour, minute=first_minute
                    )
                continue

            if not self._day_matches(cand):
                cand = (cand + timedelta(days=1)).replace(hour=first_hour, minute=first_minute)
                continue

            if cand.hour not in self.hours:
                next_hours = [h for h in sorted_hours if h > cand.hour]
                if next_hours:
                    cand = cand.replace(hour=next_hours[0], minute=first_minute)
                else:
                    cand = (cand + timedelta(days=1)).replace(hour=first_hour, minute=first_minute)
                continue

            if cand.minute not in self.minutes:
                next_mins = [m for m in sorted_minutes if m > cand.minute]
                if next_mins:
                    cand = cand.replace(minute=next_mins[0])
                else:
                    cand = (cand + timedelta(hours=1)).replace(minute=first_minute)
                continue

            return cand.replace(tzinfo=tz) if tz is not None else cand

        raise CronValidationError(
            f"Crontab expression '{self.raw_expression}' has no matching date within the next 8 years."
        )


def parse_cron_expression(expression: str) -> CronSchedule:
    """Validate and parse a 5-field crontab string (or standard @daily/@hourly alias)."""
    if not isinstance(expression, str) or not expression.strip():
        raise CronValidationError(
            "Crontab schedule cannot be empty. Expected 5 fields: minute hour day-of-month month day-of-week (e.g. '0 2 * * *')."
        )

    raw = " ".join(expression.strip().split())
    lower = raw.lower()
    expanded = CRON_ALIASES.get(lower, raw)

    parts = expanded.split(" ")
    if len(parts) != 5:
        raise CronValidationError(
            f"Expected 5 crontab fields (minute hour day-of-month month day-of-week), got {len(parts)} in '{raw}'."
        )

    min_field, hour_field, dom_field, month_field, dow_field = parts

    minutes, _ = _parse_cron_field(min_field, field_name="minute", min_val=0, max_val=59)
    hours, _ = _parse_cron_field(hour_field, field_name="hour", min_val=0, max_val=23)
    days_of_month, dom_wildcard = _parse_cron_field(
        dom_field, field_name="day-of-month", min_val=1, max_val=31
    )
    months, _ = _parse_cron_field(
        month_field, field_name="month", min_val=1, max_val=12, names=MONTH_NAMES
    )
    days_of_week, dow_wildcard = _parse_cron_field(
        dow_field,
        field_name="day-of-week",
        min_val=0,
        max_val=7,
        names=DOW_NAMES,
        map_seven_to_zero=True,
    )

    if not any(d <= MAX_DAYS_PER_MONTH[m] for m in months for d in days_of_month):
        raise CronValidationError(
            f"Impossible calendar date in '{raw}': none of the selected months contain day(s) {sorted(days_of_month)}."
        )

    return CronSchedule(
        raw_expression=raw,
        normalized_expression=expanded,
        minutes=minutes,
        hours=hours,
        days_of_month=days_of_month,
        months=months,
        days_of_week=days_of_week,
        dom_wildcard=dom_wildcard,
        dow_wildcard=dow_wildcard,
    )


def describe_cron_expression(schedule: CronSchedule) -> str:
    """Produce a concise human-readable summary for common crontab patterns."""
    DOW_LABELS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
    expr = schedule.normalized_expression
    parts = expr.split(" ")

    if len(schedule.minutes) == 60 and len(schedule.hours) == 24 and schedule.dom_wildcard and len(schedule.months) == 12 and schedule.dow_wildcard:
        return "Every minute"

    if parts[0].startswith("*/") and parts[1:] == ["*", "*", "*", "*"]:
        step = parts[0][2:]
        return "Every minute" if step == "1" else f"Every {step} minutes"

    if len(schedule.minutes) == 1 and parts[1].startswith("*/") and parts[2:] == ["*", "*", "*"]:
        m = next(iter(schedule.minutes))
        step = parts[1][2:]
        return f"Every {step} hours at :{m:02d}"

    if len(schedule.minutes) == 1 and len(schedule.hours) == 24 and schedule.dom_wildcard and len(schedule.months) == 12 and schedule.dow_wildcard:
        m = next(iter(schedule.minutes))
        return f"Hourly at :{m:02d}"

    if len(schedule.minutes) == 1 and len(schedule.hours) == 1 and len(schedule.months) == 12:
        m = next(iter(schedule.minutes))
        h = next(iter(schedule.hours))
        time_str = f"{h:02d}:{m:02d}"
        if schedule.dom_wildcard and schedule.dow_wildcard:
            return f"Daily at {time_str}"
        if schedule.dom_wildcard and len(schedule.days_of_week) == 1:
            dow = next(iter(schedule.days_of_week))
            return f"Every {DOW_LABELS[dow]} at {time_str}"
        if len(schedule.days_of_month) == 1 and schedule.dow_wildcard:
            dom = next(iter(schedule.days_of_month))
            return f"Monthly on day {dom} at {time_str}"

    return f"Crontab ({schedule.raw_expression})"


class SyncScheduler:
    """Async background crontab loop for scheduled library sync and automatic export of new/missing books."""

    def __init__(
        self,
        *,
        db: Any,
        cookie_store: Any,
        exporter: Any,
        events: Any,
        default_cron: str = DEFAULT_CRON_EXPRESSION,
        default_enabled: bool = True,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.db = db
        self.cookie_store = cookie_store
        self.exporter = exporter
        self.events = events
        self._now_fn = now_fn or (lambda: datetime.now(resolve_local_timezone()))

        # Ensure initial DB state has a valid crontab expression
        try:
            parse_cron_expression(default_cron)
            initial_default = default_cron
        except CronValidationError:
            initial_default = DEFAULT_CRON_EXPRESSION

        saved = self.db.get_schedule_state(
            default_cron=initial_default,
            default_enabled=default_enabled,
        )
        try:
            self._schedule = parse_cron_expression(saved["cron_expression"])
            self._enabled = bool(saved["enabled"])
        except CronValidationError:
            self._schedule = parse_cron_expression(DEFAULT_CRON_EXPRESSION)
            self._enabled = bool(saved["enabled"])
            self.db.save_schedule_config(
                enabled=self._enabled,
                cron_expression=DEFAULT_CRON_EXPRESSION,
            )

        self._task: asyncio.Task[None] | None = None
        self._wake_event = asyncio.Event()
        self._run_lock = asyncio.Lock()
        self._is_running = False
        self._stopping = False

    def _now(self) -> datetime:
        return self._now_fn()

    def _timezone_label(self, now: datetime) -> str:
        tz_env = os.environ.get("TZ", "").strip()
        if tz_env:
            return tz_env
        return now.tzname() or "UTC"

    def get_status(self) -> dict[str, Any]:
        """Return current schedule settings, next firing timestamp, and last execution summary."""
        now = self._now()
        saved = self.db.get_schedule_state(
            default_cron=self._schedule.raw_expression,
            default_enabled=self._enabled,
        )
        next_run = self._schedule.next_run_after(now).isoformat() if self._enabled else None
        return {
            "enabled": self._enabled,
            "cron_expression": self._schedule.raw_expression,
            "normalized_expression": self._schedule.normalized_expression,
            "description": describe_cron_expression(self._schedule),
            "timezone": self._timezone_label(now),
            "next_run_at": next_run,
            "is_running": self._is_running,
            "last_run_at": saved["last_run_at"],
            "last_status": saved["last_status"],
            "last_message": saved["last_message"],
            "last_queued_count": saved["last_queued_count"],
        }

    async def update_schedule(self, *, enabled: bool, cron_expression: str) -> dict[str, Any]:
        """Validate and persist updated schedule settings, immediately waking the background loop."""
        parsed = parse_cron_expression(cron_expression)
        self._schedule = parsed
        self._enabled = bool(enabled)
        self.db.save_schedule_config(
            enabled=self._enabled,
            cron_expression=parsed.raw_expression,
        )
        self._wake_event.set()
        status = self.get_status()
        await self.events.publish("schedule_updated", status)
        return status

    def start(self) -> None:
        """Start the background crontab loop if not already running."""
        if self._task is not None and not self._task.done():
            return
        self._stopping = False
        self._wake_event.clear()
        self._task = asyncio.create_task(self._loop(), name="playbooks-sync-scheduler")

    async def stop(self) -> None:
        """Stop the background crontab loop cleanly."""
        self._stopping = True
        self._wake_event.set()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def run_scheduled_check(self) -> dict[str, Any]:
        """Execute a single scheduled check: sync library, export any newly available/missing books, or no-op."""
        now_iso = self._now().isoformat()

        if self.exporter is None:
            status = self._record_outcome(
                run_at=now_iso,
                status="skipped",
                message="Skipped scheduled check: extractor service is not initialized.",
                queued_count=0,
            )
            await self.events.publish("schedule_updated", status)
            return status

        if not self.cookie_store.load():
            status = self._record_outcome(
                run_at=now_iso,
                status="skipped",
                message="Skipped scheduled check: no Google Play Books session cookies imported yet.",
                queued_count=0,
            )
            await self.events.publish("schedule_updated", status)
            return status

        active_exports = getattr(self.exporter, "active_count", 0)
        if self._run_lock.locked() or active_exports > 0:
            status = self._record_outcome(
                run_at=now_iso,
                status="skipped",
                message="Skipped scheduled check: a sync or audiobook export is already in progress.",
                queued_count=0,
            )
            await self.events.publish("schedule_updated", status)
            return status

        async with self._run_lock:
            self._is_running = True
            self.db.record_schedule_run(
                last_run_at=now_iso,
                last_status="running",
                last_message="Syncing Google Play Books library and checking /data for new or missing audiobooks…",
                last_queued_count=0,
            )
            await self.events.publish("schedule_updated", self.get_status())

            try:
                books = await self.exporter.sync_library()
                available = [
                    b
                    for b in books
                    if b.get("export_supported") and b.get("status") == "available"
                ]

                if not available:
                    # Nothing new or missing — stop immediately and do nothing until next scheduled tick
                    self._is_running = False
                    status = self._record_outcome(
                        run_at=now_iso,
                        status="no_new_books",
                        message=f"Checked library ({len(books)} tracked): no new or missing audiobooks to export.",
                        queued_count=0,
                    )
                    await self.events.publish("schedule_updated", status)
                    return status

                queued: list[dict[str, Any]] = []
                for book in available:
                    queued.append(await self.exporter.enqueue_export(book["volume_id"]))

                self.db.record_schedule_run(
                    last_run_at=now_iso,
                    last_status="exporting",
                    last_message=f"Found {len(queued)} new or missing audiobook(s); exporting…",
                    last_queued_count=len(queued),
                )
                await self.events.publish("schedule_updated", self.get_status())

                if hasattr(self.exporter, "wait_for_idle"):
                    await self.exporter.wait_for_idle(timeout=None)

                # Summarize post-export results
                refreshed = {b["volume_id"]: b for b in self.db.list_books()}
                extracted_count = sum(
                    1
                    for q in queued
                    if refreshed.get(q["volume_id"], {}).get("status") == "extracted"
                )
                self._is_running = False
                status = self._record_outcome(
                    run_at=now_iso,
                    status="exported" if extracted_count == len(queued) else "partial",
                    message=(
                        f"Scheduled run exported {extracted_count}/{len(queued)} new or missing audiobook(s)."
                    ),
                    queued_count=len(queued),
                )
                await self.events.publish("schedule_updated", status)
                return status

            except asyncio.CancelledError:
                self._is_running = False
                raise
            except Exception as exc:
                self._is_running = False
                status = self._record_outcome(
                    run_at=now_iso,
                    status="error",
                    message=f"Scheduled sync failed: {exc}",
                    queued_count=0,
                )
                await self.events.publish("schedule_updated", status)
                return status

    def _record_outcome(
        self,
        *,
        run_at: str,
        status: str,
        message: str,
        queued_count: int,
    ) -> dict[str, Any]:
        self.db.record_schedule_run(
            last_run_at=run_at,
            last_status=status,
            last_message=message,
            last_queued_count=queued_count,
        )
        return self.get_status()

    async def _loop(self) -> None:
        last_fired_minute: tuple[int, int, int, int, int] | None = None
        while not self._stopping:
            if not self._enabled:
                self._wake_event.clear()
                await self._wake_event.wait()
                continue

            now = self._now()
            target = self._schedule.next_run_after(now)
            delay_seconds = max(0.05, (target - now).total_seconds())

            self._wake_event.clear()
            try:
                # Re-check periodically (at most 30s chunks) so system clock adjustments or updates stay accurate
                await asyncio.wait_for(self._wake_event.wait(), timeout=min(delay_seconds, 30.0))
                # Woken up by schedule configuration change or stop()
                continue
            except asyncio.TimeoutError:
                pass

            if self._stopping or not self._enabled:
                continue

            current = self._now()
            minute_key = (
                current.year,
                current.month,
                current.day,
                current.hour,
                current.minute,
            )
            if current >= target and self._schedule.matches(current) and minute_key != last_fired_minute:
                last_fired_minute = minute_key
                await self.run_scheduled_check()
                # Broadcast refreshed next_run_at after minute completes
                await self.events.publish("schedule_updated", self.get_status())
