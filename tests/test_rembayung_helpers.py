from rembayung_booker import extract_time_minutes, jitter_delay


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
