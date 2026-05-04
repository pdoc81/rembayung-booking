# Rembayung Booker

A Python Playwright CLI assistant for Rembayung reservations at:

https://reservation.umai.io/en/widget/rembayung

It is designed to be fast, visible, and human-in-the-loop. It never bypasses CAPTCHA, never guesses OTP, never automates payment, and stops when it detects OTP, CAPTCHA, payment, or deposit language.

## What It Does First

The minimal working path is:

1. Open the reservation page.
2. Wait for the widget to load.
3. Select `20 May 2026`.
4. Select party size.
5. Detect available time slots.
6. Pick the earliest available slot.
7. Save logs and screenshots.

In `book` mode it can also fill your name, phone, and email. Final submit is disabled by default; pass `--submit-final` only after you have confirmed the flow in `dry-run`.

## Files

- `rembayung_booker.py` - CLI tool.
- `requirements.txt` - Python dependencies.
- `.env` - local booking details.
- `logs/` - JSONL run logs.
- `screenshots/` - screenshots on state changes and every 10 seconds.
- `tests/` - small helper tests.

## Safety Rules

- Headed browser mode is the default so you can see everything.
- Maximum two tabs in `book` mode.
- Tab 1 attempts 4 pax.
- Tab 2 waits 90 seconds, then attempts 3 pax.
- Retries use random jitter between 0.75 and 1.25 seconds.
- Default retry window is 7 minutes.
- The browser stays open if manual action is required.
- The tool stops on OTP, CAPTCHA, payment, or deposit screens.

## Modes

```bash
python rembayung_booker.py test
```

Opens the widget and takes screenshots. No booking steps.

```bash
python rembayung_booker.py dry-run --skip-schedule
```

Attempts the inspection flow immediately: date, pax, slot detection, earliest slot selection if available. It does not submit.

```bash
python rembayung_booker.py book
```

Waits until today at `20:57` Malaysia time, opens the tabs to warm the session, then starts booking interactions at `21:00:00`.

```bash
python rembayung_booker.py book --skip-schedule
```

Starts immediately. Useful for testing before the real booking window.

```bash
python rembayung_booker.py book --submit-final
```

After selecting the earliest slot and filling details, clicks a continue/confirm/reserve style button if found, then stops at OTP, CAPTCHA, payment, or deposit.

Useful testing option:

```bash
python rembayung_booker.py dry-run --skip-schedule --widget-timeout-seconds 300
```

If the site places you in the Cloudflare waiting room, the tool waits at a slow interval and screenshots the state. It does not try to bypass the queue.

## Windows Setup

From PowerShell:

```powershell
cd "C:\Users\firda\OneDrive\Documents\New project 2"
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m playwright install chromium
python -m pytest -q
python rembayung_booker.py test
```

If `py -3.11` is not available, use:

```powershell
python -m venv .venv
```

## macOS Setup

From Terminal:

```bash
cd "/path/to/New project 2"
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m playwright install chromium
python -m pytest -q
python rembayung_booker.py test
```

If `python3.11` is not installed, install a current Python 3.11+ build first.

## Configuration

`.env` contains:

```env
REMBAYUNG_NAME=Your Name
REMBAYUNG_PHONE=012-3456789
REMBAYUNG_EMAIL=you@example.com
REMBAYUNG_URL=https://reservation.umai.io/en/widget/rembayung
```

The script reads this file directly, so no extra dotenv dependency is needed.

## Recommended Real Booking Run

1. Run `python rembayung_booker.py test`.
2. Run `python rembayung_booker.py dry-run --skip-schedule`.
3. If screenshots and logs look correct, run:

```bash
python rembayung_booker.py book
```

For the first live attempt, keep `--submit-final` off. After the slot is selected and details are filled, complete the last step manually in the visible browser.

## Notes

Live booking widgets can change labels and structure. This script uses Playwright role, text, aria label, and placeholder locators instead of hard-coded CSS where possible, but the first dry run is important.
