import asyncio

import argparse
from datetime import time

import pytest

from rembayung_booker import (
    build_config,
    close_browser_quietly,
    create_booking_attempts,
    extract_time_minutes,
    jitter_delay,
    parse_clock_time,
    should_keep_browser_open,
    wait_for_manual_browser_close,
)


class FakeLogger:
    def __init__(self):
        self.events = []

    def write(self, event, **data):
        self.events.append((event, data))


class BrokenBrowser:
    async def close(self):
        raise Exception("Connection closed while reading from the driver")


class ManualCloseBrowser:
    def __init__(self):
        self.handler = None

    def is_connected(self):
        return True

    def on(self, event, handler):
        if event == "disconnected":
            self.handler = handler

    def trigger_close(self):
        self.handler()


def test_requirements_include_windows_timezone_data():
    requirements = open("requirements.txt", encoding="utf-8").read().splitlines()
    assert any(line.partition(">=")[0] == "tzdata" for line in requirements)


def test_book_mode_defaults_preload_at_850_and_start_at_900():
    args = argparse.Namespace(
        mode="book",
        url="https://example.test",
        max_retry_seconds=7 * 60,
        retry_min_seconds=0.25,
        retry_max_seconds=0.5,
        waiting_room_poll_seconds=1.0,
        tab2_delay_seconds=90,
        screenshot_interval_seconds=10,
        widget_timeout_seconds=120,
        backup_3_pax=False,
        preload_time="20:50",
        start_time="21:00",
        headless=False,
        skip_schedule=False,
        submit_final=False,
    )

    config = build_config(args)

    assert config.preload_time == time(20, 50)
    assert config.start_time == time(21, 0)
    assert config.retry_min_seconds == 0.25
    assert config.retry_max_seconds == 0.5
    assert config.waiting_room_poll_seconds == 1.0
    assert config.backup_3_pax is False


def test_book_mode_uses_one_attempt_by_default():
    args = argparse.Namespace(
        mode="book",
        url="https://example.test",
        max_retry_seconds=7 * 60,
        retry_min_seconds=0.25,
        retry_max_seconds=0.5,
        waiting_room_poll_seconds=1.0,
        tab2_delay_seconds=90,
        screenshot_interval_seconds=10,
        widget_timeout_seconds=120,
        backup_3_pax=False,
        preload_time="20:50",
        start_time="21:00",
        headless=False,
        skip_schedule=False,
        submit_final=False,
    )

    assert create_booking_attempts(build_config(args)) == [(4, "tab1-4pax", 0)]


def test_book_mode_can_enable_three_pax_backup():
    args = argparse.Namespace(
        mode="book",
        url="https://example.test",
        max_retry_seconds=7 * 60,
        retry_min_seconds=0.25,
        retry_max_seconds=0.5,
        waiting_room_poll_seconds=1.0,
        tab2_delay_seconds=90,
        screenshot_interval_seconds=10,
        widget_timeout_seconds=120,
        backup_3_pax=True,
        preload_time="20:50",
        start_time="21:00",
        headless=False,
        skip_schedule=False,
        submit_final=False,
    )

    assert create_booking_attempts(build_config(args)) == [(4, "tab1-4pax", 0), (3, "tab2-3pax", 90)]


def test_parse_clock_time_accepts_hh_mm_and_hh_mm_ss():
    assert parse_clock_time("20:50") == time(20, 50)
    assert parse_clock_time("21:00:05") == time(21, 0, 5)


def test_parse_clock_time_rejects_invalid_values():
    with pytest.raises(argparse.ArgumentTypeError):
        parse_clock_time("8.50pm")


def test_jitter_delay_uses_configured_range():
    for _ in range(100):
        value = jitter_delay(0.25, 0.5)
        assert 0.25 <= value <= 0.5


def test_close_browser_quietly_logs_close_failures():
    logger = FakeLogger()

    asyncio.run(close_browser_quietly(BrokenBrowser(), logger))

    assert logger.events == [
        ("browser_close_failed", {"error": "Exception('Connection closed while reading from the driver')"})
    ]


def test_headed_mode_keeps_browser_open_for_manual_close():
    args = argparse.Namespace(
        mode="book",
        url="https://example.test",
        max_retry_seconds=7 * 60,
        retry_min_seconds=0.25,
        retry_max_seconds=0.5,
        waiting_room_poll_seconds=1.0,
        tab2_delay_seconds=90,
        screenshot_interval_seconds=10,
        widget_timeout_seconds=120,
        backup_3_pax=False,
        preload_time="20:50",
        start_time="21:00",
        headless=False,
        skip_schedule=False,
        submit_final=False,
    )

    assert should_keep_browser_open(build_config(args)) is True


def test_headless_mode_closes_browser_when_done():
    args = argparse.Namespace(
        mode="book",
        url="https://example.test",
        max_retry_seconds=7 * 60,
        retry_min_seconds=0.25,
        retry_max_seconds=0.5,
        waiting_room_poll_seconds=1.0,
        tab2_delay_seconds=90,
        screenshot_interval_seconds=10,
        widget_timeout_seconds=120,
        backup_3_pax=False,
        preload_time="20:50",
        start_time="21:00",
        headless=True,
        skip_schedule=False,
        submit_final=False,
    )

    assert should_keep_browser_open(build_config(args)) is False


def test_wait_for_manual_browser_close_returns_after_browser_disconnects():
    browser = ManualCloseBrowser()
    logger = FakeLogger()

    async def run_wait():
        task = asyncio.create_task(wait_for_manual_browser_close(browser, logger))
        await asyncio.sleep(0)
        browser.trigger_close()
        await task

    asyncio.run(run_wait())

    assert logger.events == [
        ("browser_kept_open", {}),
        ("browser_closed_by_user", {}),
    ]


def test_extract_time_minutes_sorts_12_hour_times():
    assert extract_time_minutes("11:30 AM") == 690
    assert extract_time_minutes("12:00 PM") == 720
    assert extract_time_minutes("6:15 PM") == 1095


def test_extract_time_minutes_sorts_24_hour_times():
    assert extract_time_minutes("09:00") == 540
    assert extract_time_minutes("21:30") == 1290


def test_jitter_delay_stays_inside_allowed_rate():
    for _ in range(100):
        value = jitter_delay()
        assert 0.75 <= value <= 1.25
