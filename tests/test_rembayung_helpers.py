import asyncio

from rembayung_booker import close_browser_quietly, extract_time_minutes, jitter_delay


class FakeLogger:
    def __init__(self):
        self.events = []

    def write(self, event, **data):
        self.events.append((event, data))


class BrokenBrowser:
    async def close(self):
        raise Exception("Connection closed while reading from the driver")


def test_requirements_include_windows_timezone_data():
    requirements = open("requirements.txt", encoding="utf-8").read().splitlines()
    assert any(line.partition(">=")[0] == "tzdata" for line in requirements)


def test_close_browser_quietly_logs_close_failures():
    logger = FakeLogger()

    asyncio.run(close_browser_quietly(BrokenBrowser(), logger))

    assert logger.events == [
        ("browser_close_failed", {"error": "Exception('Connection closed while reading from the driver')"})
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
