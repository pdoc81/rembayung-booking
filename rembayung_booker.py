"""Human-in-the-loop Rembayung reservation assistant.

This tool intentionally stops at OTP, CAPTCHA, payment, and deposit screens.
It does not bypass protected flows or complete payments.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

from playwright.async_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)
from rich.console import Console
from rich.panel import Panel
from rich.table import Table


TIMEZONE = ZoneInfo("Asia/Kuala_Lumpur")
DEFAULT_URL = "https://reservation.umai.io/en/widget/rembayung"
TARGET_DATE = "After 20 May 2026, or any available date"
TARGET_DATE_ISO = "2026-05-20"
TARGET_AFTER_DATE = date(2026, 5, 20)
LOG_DIR = Path("logs")
SCREENSHOT_DIR = Path("screenshots")
SAFE_STOP_WORDS = (
    "otp",
    "one-time password",
    "verification code",
    "captcha",
    "recaptcha",
    "payment",
    "pay now",
    "card number",
    "deposit",
)
TIME_PATTERN = re.compile(r"\b((?:[01]?\d|2[0-3]):[0-5]\d)\s*(am|pm|AM|PM)?\b")
MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "mei": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


console = Console()


@dataclass(frozen=True)
class GuestDetails:
    name: str
    phone: str
    email: str


@dataclass(frozen=True)
class RunConfig:
    mode: str
    url: str
    target_date: str
    target_date_iso: str
    max_retry_seconds: int
    retry_min_seconds: float
    retry_max_seconds: float
    waiting_room_poll_seconds: float
    screenshot_interval_seconds: int
    preload_time: time
    start_time: time
    tab2_delay_seconds: int
    widget_timeout_seconds: int
    waiting_room_timeout_seconds: int
    backup_3_pax: bool
    headed: bool
    skip_schedule: bool
    submit_final: bool


class JsonlLogger:
    def __init__(self, mode: str) -> None:
        LOG_DIR.mkdir(exist_ok=True)
        stamp = datetime.now(TIMEZONE).strftime("%Y%m%d-%H%M%S")
        self.path = LOG_DIR / f"{stamp}-{mode}.jsonl"

    def write(self, event: str, **data: object) -> None:
        payload = {
            "timestamp": datetime.now(TIMEZONE).isoformat(timespec="seconds"),
            "event": event,
            **data,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        console.log(f"[bold cyan]{event}[/bold cyan]", data)


def load_env(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def get_guest_details() -> GuestDetails:
    return GuestDetails(
        name=os.environ.get("REMBAYUNG_NAME", "").strip(),
        phone=os.environ.get("REMBAYUNG_PHONE", "").strip(),
        email=os.environ.get("REMBAYUNG_EMAIL", "").strip(),
    )


def validate_guest_details(details: GuestDetails) -> None:
    missing = [
        label
        for label, value in (
            ("REMBAYUNG_NAME", details.name),
            ("REMBAYUNG_PHONE", details.phone),
            ("REMBAYUNG_EMAIL", details.email),
        )
        if not value
    ]
    if missing:
        raise SystemExit(f"Missing required .env values: {', '.join(missing)}")


def jitter_delay(min_seconds: float = 0.75, max_seconds: float = 1.25) -> float:
    return random.uniform(min_seconds, max_seconds)


def extract_time_minutes(text: str) -> int | None:
    match = TIME_PATTERN.search(text)
    if not match:
        return None
    hour_text, suffix = match.groups()
    hour, minute = [int(part) for part in hour_text.split(":")]
    if suffix:
        suffix = suffix.lower()
        if suffix == "pm" and hour != 12:
            hour += 12
        elif suffix == "am" and hour == 12:
            hour = 0
    return hour * 60 + minute


def slot_sort_key(text: str) -> int:
    minutes = extract_time_minutes(text)
    return minutes if minutes is not None else 24 * 60


def parse_booking_date_text(
    text: str,
    default_year: int = TARGET_AFTER_DATE.year,
    default_month: int = TARGET_AFTER_DATE.month,
) -> date | None:
    clean_text = " ".join(text.replace(",", " ").split())
    day_only = re.fullmatch(r"[0-3]?\d", clean_text)
    if day_only:
        try:
            return date(default_year, default_month, int(clean_text))
        except ValueError:
            return None

    day_first = re.search(r"\b([0-3]?\d)\s+([A-Za-z]+)(?:\s+(\d{4}))?\b", clean_text, re.I)
    if day_first:
        day_text, month_text, year_text = day_first.groups()
        month = MONTHS.get(month_text.lower())
        if month:
            try:
                return date(int(year_text or default_year), month, int(day_text))
            except ValueError:
                return None

    month_first = re.search(r"\b([A-Za-z]+)\s+([0-3]?\d)(?:\s+(\d{4}))?\b", clean_text, re.I)
    if month_first:
        month_text, day_text, year_text = month_first.groups()
        month = MONTHS.get(month_text.lower())
        if month:
            try:
                return date(int(year_text or default_year), month, int(day_text))
            except ValueError:
                return None

    return None


def choose_preferred_date_text(labels: Iterable[str], after_date: date = TARGET_AFTER_DATE) -> str | None:
    candidates: list[tuple[date, str]] = []
    for label in labels:
        parsed_date = parse_booking_date_text(label, default_year=after_date.year)
        if parsed_date:
            candidates.append((parsed_date, label))
    if not candidates:
        return None

    after_candidates = sorted((candidate for candidate in candidates if candidate[0] > after_date), key=lambda item: item[0])
    if after_candidates:
        return after_candidates[0][1]
    return sorted(candidates, key=lambda item: item[0])[0][1]


def format_duration(seconds: float) -> str:
    rounded_seconds = max(0, int(round(seconds)))
    hours, remainder = divmod(rounded_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours}h")
    if minutes or hours:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


def get_widget_load_timeout_seconds(config: RunConfig, waiting_room_detected: bool) -> int:
    if waiting_room_detected:
        return config.waiting_room_timeout_seconds
    return config.widget_timeout_seconds


async def first_visible(locator: Locator, timeout_ms: int = 900) -> Locator | None:
    try:
        count = await locator.count()
    except PlaywrightError:
        return None
    for index in range(min(count, 12)):
        candidate = locator.nth(index)
        try:
            await candidate.wait_for(state="visible", timeout=timeout_ms)
            return candidate
        except PlaywrightTimeoutError:
            continue
        except PlaywrightError:
            continue
    return None


async def click_first(label: str, locators: Iterable[Locator], logger: JsonlLogger) -> bool:
    for locator in locators:
        candidate = await first_visible(locator)
        if candidate is None:
            continue
        try:
            await candidate.click(timeout=1500)
            logger.write("clicked", label=label)
            return True
        except PlaywrightError as exc:
            logger.write("click_failed", label=label, error=str(exc)[:180])
    return False


class RembayungBooker:
    def __init__(
        self,
        page: Page,
        config: RunConfig,
        details: GuestDetails,
        logger: JsonlLogger,
        tab_name: str,
    ) -> None:
        self.page = page
        self.config = config
        self.details = details
        self.logger = logger
        self.tab_name = tab_name
        self.last_state = ""
        self.stop = False

    async def screenshot(self, state: str) -> None:
        SCREENSHOT_DIR.mkdir(exist_ok=True)
        clean_state = re.sub(r"[^a-zA-Z0-9_-]+", "-", state).strip("-").lower()
        stamp = datetime.now(TIMEZONE).strftime("%Y%m%d-%H%M%S")
        path = SCREENSHOT_DIR / f"{stamp}-{self.tab_name}-{clean_state}.png"
        try:
            await self.page.screenshot(path=str(path), full_page=True)
            self.logger.write("screenshot", tab=self.tab_name, state=state, path=str(path))
        except PlaywrightError as exc:
            self.logger.write("screenshot_failed", tab=self.tab_name, error=str(exc)[:180])

    async def mark_state(self, state: str) -> None:
        if state != self.last_state:
            self.last_state = state
            self.logger.write("state", tab=self.tab_name, state=state)
            await self.screenshot(state)

    async def is_waiting_room(self) -> bool:
        try:
            text = (await self.page.locator("body").inner_text(timeout=1500)).lower()
        except PlaywrightError:
            return False
        return any(
            phrase in text
            for phrase in (
                "cloudflare",
                "waiting room",
                "masa menunggu",
                "anda kini dalam talian",
                "halaman ini akan disegarkan",
            )
        )

    async def open_widget(self) -> bool:
        await self.mark_state("open-page")
        await self.page.goto(self.config.url, wait_until="domcontentloaded", timeout=60000)
        try:
            await self.page.wait_for_load_state("networkidle", timeout=25000)
        except PlaywrightTimeoutError:
            self.logger.write("networkidle_timeout", tab=self.tab_name)
        started_at = datetime.now(TIMEZONE)
        deadline = started_at + timedelta(seconds=get_widget_load_timeout_seconds(self.config, waiting_room_detected=False))
        waiting_room_logged = False
        while datetime.now(TIMEZONE) < deadline:
            if await self.is_waiting_room():
                if not waiting_room_logged:
                    waiting_room_logged = True
                    deadline = started_at + timedelta(seconds=get_widget_load_timeout_seconds(self.config, waiting_room_detected=True))
                    await self.mark_state("cloudflare-waiting-room")
                    self.logger.write(
                        "waiting_room_detected",
                        tab=self.tab_name,
                        max_wait_seconds=self.config.waiting_room_timeout_seconds,
                    )
                await self.page.wait_for_timeout(int(self.config.waiting_room_poll_seconds * 1000))
                continue
            await self.page.wait_for_timeout(1500)
            await self.mark_state("widget-loaded")
            return True
        self.logger.write(
            "widget_timeout",
            tab=self.tab_name,
            seconds=self.config.widget_timeout_seconds,
            note="Reservation widget did not load before timeout, likely still in waiting room.",
        )
        await self.screenshot("widget-timeout")
        return False

    async def periodic_screenshots(self, done: asyncio.Event) -> None:
        while not done.is_set():
            await asyncio.sleep(self.config.screenshot_interval_seconds)
            if not done.is_set():
                await self.screenshot("interval")

    async def safe_stop_detected(self) -> bool:
        text = ""
        try:
            text = (await self.page.locator("body").inner_text(timeout=1000)).lower()
        except PlaywrightError:
            return False
        for word in SAFE_STOP_WORDS:
            if word in text:
                self.logger.write("safe_stop_detected", tab=self.tab_name, trigger=word)
                console.print(
                    Panel.fit(
                        f"ACTION REQUIRED\nDetected: {word}\nBrowser will stay open for manual completion.",
                        title=self.tab_name,
                        border_style="red",
                    )
                )
                await self.screenshot(f"action-required-{word}")
                return True
        return False

    async def select_date(self) -> bool:
        await self.mark_state("select-date")
        date_opened = await click_first(
            "open date picker",
            (
                self.page.get_by_role("button", name=re.compile("date|today|select", re.I)),
                self.page.get_by_placeholder(re.compile("date|select", re.I)),
                self.page.get_by_text(re.compile("date|select date|choose date", re.I)),
            ),
            self.logger,
        )
        if date_opened:
            await self.page.wait_for_timeout(500)

        date_buttons = self.page.locator("button, [role=button]")
        candidates: list[tuple[date, str, Locator]] = []
        try:
            count = await date_buttons.count()
        except PlaywrightError:
            count = 0
        for index in range(min(count, 80)):
            candidate = date_buttons.nth(index)
            try:
                if not await candidate.is_visible(timeout=300):
                    continue
                if hasattr(candidate, "is_enabled") and not await candidate.is_enabled(timeout=300):
                    continue
                text = " ".join((await candidate.inner_text(timeout=500)).split())
                aria_label = await candidate.get_attribute("aria-label", timeout=500)
            except PlaywrightError:
                continue
            label = aria_label or text
            parsed_date = parse_booking_date_text(label or "", default_year=TARGET_AFTER_DATE.year)
            if parsed_date:
                candidates.append((parsed_date, label or text, candidate))

        after_candidates = sorted((candidate for candidate in candidates if candidate[0] > TARGET_AFTER_DATE), key=lambda item: item[0])
        fallback_candidates = sorted(candidates, key=lambda item: item[0])
        selected = (after_candidates or fallback_candidates)[0] if candidates else None
        if selected:
            selected_date, selected_label, selected_locator = selected
            try:
                await selected_locator.click(timeout=2000)
                self.logger.write(
                    "date_selected",
                    tab=self.tab_name,
                    date=selected_date.isoformat(),
                    label=selected_label,
                    strategy="after_2026_05_20" if selected_date > TARGET_AFTER_DATE else "any_available",
                )
                await self.mark_state("date-selected")
                return True
            except PlaywrightError as exc:
                self.logger.write("date_click_failed", tab=self.tab_name, label=selected_label, error=str(exc)[:180])

        self.logger.write("date_not_selected", tab=self.tab_name, target=self.config.target_date)
        return False

    async def select_party_size(self, party_size: int) -> bool:
        await self.mark_state(f"select-{party_size}-pax")
        await click_first(
            "open party size selector",
            (
                self.page.get_by_role("button", name=re.compile("guest|pax|people|person|party", re.I)),
                self.page.get_by_text(re.compile("guest|pax|people|person|party", re.I)),
            ),
            self.logger,
        )
        await self.page.wait_for_timeout(400)
        labels = (
            re.compile(fr"\b{party_size}\s*(pax|guests?|people|persons?)\b", re.I),
            re.compile(fr"^{party_size}$"),
            re.compile(fr"\b{party_size}\b"),
        )
        for label in labels:
            clicked = await click_first(
                f"{party_size} pax",
                (
                    self.page.get_by_role("button", name=label),
                    self.page.get_by_text(label, exact=False),
                    self.page.get_by_label(label),
                ),
                self.logger,
            )
            if clicked:
                await self.mark_state(f"{party_size}-pax-selected")
                return True
        self.logger.write("party_size_not_selected", tab=self.tab_name, party_size=party_size)
        return False

    async def find_time_slots(self) -> list[tuple[int, str, Locator]]:
        button_locator = self.page.get_by_role("button").filter(has_text=TIME_PATTERN)
        text_locator = self.page.locator("button, [role=button], a").filter(has_text=TIME_PATTERN)
        candidates: list[tuple[int, str, Locator]] = []
        for locator in (button_locator, text_locator):
            try:
                count = await locator.count()
            except PlaywrightError:
                continue
            for index in range(min(count, 30)):
                item = locator.nth(index)
                try:
                    if not await item.is_visible(timeout=500):
                        continue
                    text = " ".join((await item.inner_text(timeout=700)).split())
                    if not text or re.search("unavailable|sold|closed|full", text, re.I):
                        continue
                    minutes = extract_time_minutes(text)
                    if minutes is None:
                        continue
                    candidates.append((minutes, text, item))
                except PlaywrightError:
                    continue
        unique: dict[str, tuple[int, str, Locator]] = {}
        for minutes, text, locator in candidates:
            unique.setdefault(text, (minutes, text, locator))
        return sorted(unique.values(), key=lambda value: value[0])

    async def select_earliest_slot(self) -> bool:
        slots = await self.find_time_slots()
        if not slots:
            self.logger.write("slots_none", tab=self.tab_name)
            return False
        minutes, text, locator = slots[0]
        self.logger.write("slot_found", tab=self.tab_name, slot=text, minutes=minutes)
        await self.screenshot(f"slot-found-{text}")
        try:
            await locator.click(timeout=2000)
            await self.mark_state("slot-selected")
            return True
        except PlaywrightError as exc:
            self.logger.write("slot_click_failed", tab=self.tab_name, slot=text, error=str(exc)[:180])
            return False

    async def fill_details(self) -> None:
        await self.mark_state("fill-details")
        fields = (
            ("name", self.details.name, re.compile("name|full name", re.I), "input[name*='name' i]"),
            ("phone", self.details.phone, re.compile("phone|mobile|contact", re.I), "input[type='tel'], input[name*='phone' i], input[name*='mobile' i]"),
            ("email", self.details.email, re.compile("email|e-mail", re.I), "input[type='email'], input[name*='email' i]"),
        )
        for label, value, pattern, css in fields:
            filled = False
            for locator in (
                self.page.get_by_placeholder(pattern),
                self.page.get_by_label(pattern),
                self.page.locator(css),
            ):
                candidate = await first_visible(locator)
                if candidate is None:
                    continue
                try:
                    await candidate.fill(value, timeout=1200)
                    self.logger.write("field_filled", tab=self.tab_name, field=label)
                    filled = True
                    break
                except PlaywrightError as exc:
                    self.logger.write("field_fill_failed", tab=self.tab_name, field=label, error=str(exc)[:160])
            if not filled:
                self.logger.write("field_missing", tab=self.tab_name, field=label)

    async def maybe_continue(self) -> None:
        if self.config.mode == "dry-run":
            console.print("[yellow]Dry-run selected a slot if available, but did not submit.[/yellow]")
            return
        if not self.config.submit_final:
            console.print(
                "[yellow]Booking details are filled. Final submit is disabled by default; use --submit-final to continue automatically until a safe-stop screen appears.[/yellow]"
            )
            return
        await click_first(
            "continue/submit reservation",
            (
                self.page.get_by_role("button", name=re.compile("continue|confirm|reserve|book|submit", re.I)),
                self.page.get_by_text(re.compile("continue|confirm|reserve|book|submit", re.I)),
            ),
            self.logger,
        )
        await self.page.wait_for_timeout(1000)

    async def attempt(self, party_size: int, done: asyncio.Event, already_loaded: bool = False) -> bool:
        if already_loaded:
            loaded = True
        else:
            loaded = await self.open_widget()
            if self.config.mode == "test":
                await self.mark_state("test-complete")
                return False
            if not loaded:
                await self.mark_state("widget-not-ready")
                return False
        await self.select_date()
        await self.select_party_size(party_size)
        if self.config.mode == "dry-run":
            await self.select_earliest_slot()
            await self.mark_state("dry-run-complete")
            return False

        deadline = datetime.now(TIMEZONE) + timedelta(seconds=self.config.max_retry_seconds)
        while datetime.now(TIMEZONE) < deadline and not done.is_set():
            if await self.safe_stop_detected():
                done.set()
                return True
            if await self.select_earliest_slot():
                await self.fill_details()
                await self.maybe_continue()
                await self.safe_stop_detected()
                done.set()
                return True
            await asyncio.sleep(jitter_delay(self.config.retry_min_seconds, self.config.retry_max_seconds))
        self.logger.write("retry_deadline_reached", tab=self.tab_name, party_size=party_size)
        return False


async def wait_until_today_at(target: time, logger: JsonlLogger, label: str) -> None:
    now = datetime.now(TIMEZONE)
    target_dt = datetime.combine(now.date(), target, tzinfo=TIMEZONE)
    if now >= target_dt:
        logger.write("schedule_time_already_passed", label=label, target=target.isoformat())
        return
    seconds = (target_dt - now).total_seconds()
    console.print(
        Panel(
            f"Waiting {format_duration(seconds)} until {target_dt.strftime('%H:%M:%S')} Malaysia time for {label}.\n"
            "To start immediately, stop this run and use: python rembayung_booker.py book --skip-schedule",
            title="Scheduled wait",
        )
    )
    logger.write("waiting_until", label=label, target=target_dt.isoformat(), seconds=round(seconds, 2))
    await asyncio.sleep(seconds)


async def close_browser_quietly(browser: Browser, logger: JsonlLogger) -> None:
    try:
        await browser.close()
    except Exception as exc:
        logger.write("browser_close_failed", error=repr(exc))


def create_booking_attempts(config: RunConfig) -> list[tuple[int, str, int]]:
    attempts = [(4, "tab1-4pax", 0)]
    if config.mode == "book" and config.backup_3_pax:
        attempts.append((3, "tab2-3pax", config.tab2_delay_seconds))
    return attempts


def should_keep_browser_open(config: RunConfig) -> bool:
    return config.headed


async def wait_for_manual_browser_close(browser: Browser, logger: JsonlLogger) -> None:
    logger.write("browser_kept_open")
    if not browser.is_connected():
        logger.write("browser_closed_by_user")
        return

    loop = asyncio.get_running_loop()
    closed = loop.create_future()

    def mark_closed() -> None:
        if not closed.done():
            closed.set_result(None)

    browser.on("disconnected", mark_closed)
    await closed
    logger.write("browser_closed_by_user")


async def run_single_tab(
    browser: Browser,
    config: RunConfig,
    details: GuestDetails,
    logger: JsonlLogger,
    party_size: int,
    tab_name: str,
    done: asyncio.Event,
    delay_seconds: int = 0,
) -> None:
    scheduled_book = config.mode == "book" and not config.skip_schedule
    if delay_seconds and not scheduled_book:
        logger.write("tab_standby", tab=tab_name, seconds=delay_seconds)
        await asyncio.sleep(delay_seconds)
    if done.is_set():
        return
    context: BrowserContext = await browser.new_context(timezone_id="Asia/Kuala_Lumpur")
    page = await context.new_page()
    booker = RembayungBooker(page, config, details, logger, tab_name)
    screenshot_task = asyncio.create_task(booker.periodic_screenshots(done))
    try:
        if scheduled_book:
            loaded = await booker.open_widget()
            if not loaded:
                await booker.mark_state("widget-not-ready")
                return
            await wait_until_today_at(config.start_time, logger, f"{tab_name}-booking-start")
            if delay_seconds:
                logger.write("tab_standby", tab=tab_name, seconds=delay_seconds)
                await asyncio.sleep(delay_seconds)
            await booker.attempt(party_size, done, already_loaded=True)
            return
        await booker.attempt(party_size, done)
    except Exception as exc:
        logger.write("tab_error", tab=tab_name, error=repr(exc))
        await booker.screenshot("error")
    finally:
        screenshot_task.cancel()


async def run(config: RunConfig, details: GuestDetails, logger: JsonlLogger) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=not config.headed)
        done = asyncio.Event()
        try:
            if config.mode == "book" and not config.skip_schedule:
                await wait_until_today_at(config.preload_time, logger, "preload")
                logger.write("preload_window_started")
            tasks = [
                asyncio.create_task(
                    run_single_tab(browser, config, details, logger, party_size, tab_name, done, delay_seconds=delay_seconds)
                )
                for party_size, tab_name, delay_seconds in create_booking_attempts(config)
            ]
            await asyncio.gather(*tasks)
            if should_keep_browser_open(config):
                console.print("[bold red]ACTION REQUIRED[/bold red] Browser remains open. Close the browser window when you are done.")
                await wait_for_manual_browser_close(browser, logger)
        finally:
            if not should_keep_browser_open(config):
                await close_browser_quietly(browser, logger)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rembayung reservation assistant")
    parser.add_argument("mode", choices=("test", "dry-run", "book"))
    parser.add_argument("--url", default=os.environ.get("REMBAYUNG_URL", DEFAULT_URL))
    parser.add_argument("--skip-schedule", action="store_true", help="Start immediately instead of waiting for the preload/start times.")
    parser.add_argument("--headless", action="store_true", help="Run browser hidden. Headed mode is the safe default.")
    parser.add_argument("--submit-final", action="store_true", help="In book mode, click the final continue/submit button after filling details.")
    parser.add_argument("--preload-time", type=parse_clock_time, default=parse_clock_time("20:50"), help="When book mode opens the browser and joins the queue. Use HH:MM or HH:MM:SS.")
    parser.add_argument("--start-time", type=parse_clock_time, default=parse_clock_time("21:00"), help="When book mode starts booking interactions. Use HH:MM or HH:MM:SS.")
    parser.add_argument("--max-retry-seconds", type=int, default=7 * 60)
    parser.add_argument("--retry-min-seconds", type=float, default=0.25)
    parser.add_argument("--retry-max-seconds", type=float, default=0.5)
    parser.add_argument("--waiting-room-poll-seconds", type=float, default=1.0)
    parser.add_argument("--backup-3-pax", action="store_true", help="Open a second backup window for 3 pax after --tab2-delay-seconds.")
    parser.add_argument("--tab2-delay-seconds", type=int, default=90)
    parser.add_argument("--screenshot-interval-seconds", type=int, default=10)
    parser.add_argument("--widget-timeout-seconds", type=int, default=120)
    parser.add_argument("--waiting-room-timeout-seconds", type=int, default=4 * 60 * 60)
    return parser.parse_args()


def parse_clock_time(value: str) -> time:
    parts = value.split(":")
    if len(parts) not in (2, 3):
        raise argparse.ArgumentTypeError("Use 24-hour time as HH:MM or HH:MM:SS.")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
        second = int(parts[2]) if len(parts) == 3 else 0
        return time(hour, minute, second)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Use 24-hour time as HH:MM or HH:MM:SS.") from exc


def build_config(args: argparse.Namespace) -> RunConfig:
    retry_min_seconds = args.retry_min_seconds
    retry_max_seconds = args.retry_max_seconds
    if retry_min_seconds <= 0 or retry_max_seconds < retry_min_seconds:
        raise ValueError("--retry-max-seconds must be greater than or equal to --retry-min-seconds, and both must be positive.")
    if args.waiting_room_poll_seconds <= 0:
        raise ValueError("--waiting-room-poll-seconds must be positive.")
    if args.widget_timeout_seconds <= 0:
        raise ValueError("--widget-timeout-seconds must be positive.")
    if args.waiting_room_timeout_seconds < args.widget_timeout_seconds:
        raise ValueError("--waiting-room-timeout-seconds must be greater than or equal to --widget-timeout-seconds.")
    preload_time = parse_clock_time(args.preload_time) if isinstance(args.preload_time, str) else args.preload_time
    start_time = parse_clock_time(args.start_time) if isinstance(args.start_time, str) else args.start_time
    return RunConfig(
        mode=args.mode,
        url=args.url,
        target_date=TARGET_DATE,
        target_date_iso=TARGET_DATE_ISO,
        max_retry_seconds=args.max_retry_seconds,
        retry_min_seconds=retry_min_seconds,
        retry_max_seconds=retry_max_seconds,
        waiting_room_poll_seconds=args.waiting_room_poll_seconds,
        screenshot_interval_seconds=args.screenshot_interval_seconds,
        preload_time=preload_time,
        start_time=start_time,
        tab2_delay_seconds=args.tab2_delay_seconds,
        widget_timeout_seconds=args.widget_timeout_seconds,
        waiting_room_timeout_seconds=args.waiting_room_timeout_seconds,
        backup_3_pax=args.backup_3_pax,
        headed=not args.headless,
        skip_schedule=args.skip_schedule,
        submit_final=args.submit_final,
    )


def show_summary(config: RunConfig, details: GuestDetails, logger: JsonlLogger) -> None:
    table = Table(title="Rembayung Booker")
    table.add_column("Setting")
    table.add_column("Value")
    table.add_row("Mode", config.mode)
    table.add_row("URL", config.url)
    table.add_row("Target date", config.target_date)
    table.add_row(
        "Party priority",
        f"4 pax only; 3 pax backup after {config.tab2_delay_seconds} seconds" if config.backup_3_pax else "4 pax only",
    )
    table.add_row("Timezone", "Asia/Kuala_Lumpur")
    table.add_row("Schedule", "disabled; starts immediately" if config.skip_schedule else "enabled")
    table.add_row("Preload time", config.preload_time.isoformat())
    table.add_row("Booking start", config.start_time.isoformat())
    table.add_row("Retry rate", f"{config.retry_min_seconds}s to {config.retry_max_seconds}s jitter, max {config.max_retry_seconds} seconds")
    table.add_row("Waiting-room poll", f"{config.waiting_room_poll_seconds} seconds")
    table.add_row("Widget timeout", f"{config.widget_timeout_seconds} seconds")
    table.add_row("Waiting-room timeout", format_duration(config.waiting_room_timeout_seconds))
    table.add_row("Final submit", "enabled" if config.submit_final else "disabled")
    table.add_row("Name", details.name)
    table.add_row("Phone", details.phone)
    table.add_row("Email", details.email)
    table.add_row("Log file", str(logger.path))
    console.print(table)


def main() -> None:
    load_env()
    args = parse_args()
    config = build_config(args)
    details = get_guest_details()
    validate_guest_details(details)
    logger = JsonlLogger(config.mode)
    show_summary(config, details, logger)
    logger.write("run_started", mode=config.mode)
    asyncio.run(run(config, details, logger))


if __name__ == "__main__":
    main()
