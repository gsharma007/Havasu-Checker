import os
import json
import smtplib
from email.message import EmailMessage
from datetime import datetime
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

STATE_FILE = "state.json"

BOOK_URL = os.getenv("BOOK_URL", "https://bookingsus.newbook.cloud/online/havasupai")

# Inputs: YYYY-MM-DD
ARRIVAL = os.getenv("ARRIVAL", "2026-05-25")
DEPARTURE = os.getenv("DEPARTURE", "2026-05-28")
ADULTS = int(os.getenv("ADULTS", "2"))  # Guests shown like 1A, 2A

SEND_TEST_EMAIL = os.getenv("SEND_TEST_EMAIL", "0") == "1"

SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
ALERT_TO = os.getenv("ALERT_TO")


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def send_email(subject: str, body: str):
    if not all([SMTP_HOST, SMTP_USER, SMTP_PASS, ALERT_TO]):
        raise RuntimeError(
            "Missing SMTP env vars. Add GitHub Secrets: SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, ALERT_TO"
        )

    msg = EmailMessage()
    msg["From"] = SMTP_USER
    msg["To"] = ALERT_TO
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
        s.starttls()
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)


def fmt_newbook(date_yyyy_mm_dd: str) -> str:
    # UI shows like "May 25 2026"
    dt = datetime.strptime(date_yyyy_mm_dd, "%Y-%m-%d")
    return dt.strftime("%B %d %Y")


def write_debug(page, note: str):
    # Always capture artifacts for visibility
    try:
        page.screenshot(path="debug.png", full_page=True)
    except Exception:
        pass
    try:
        with open("debug.html", "w", encoding="utf-8") as f:
            f.write(page.content())
    except Exception:
        pass
    try:
        with open("debug_note.txt", "w", encoding="utf-8") as f:
            f.write(note)
    except Exception:
        pass


def fill_by_label(page, label: str, value: str) -> bool:
    """
    Fill a labeled input and trigger change.
    """
    try:
        inp = page.get_by_label(label)
        inp.wait_for(state="visible", timeout=20000)
        inp.click(timeout=5000)
        inp.fill(value, timeout=5000)
        inp.press("Tab", timeout=2000)  # trigger blur/change
        return True
    except Exception:
        return False


def set_guests(page, adults: int) -> bool:
    """
    Guests UI shows something like 1A/2A.
    We click the Guests field, then choose "2A".
    If that isn't available, try plus button fallback.
    """
    target = f"{adults}A"

    # 1) Click the Guests input/box (near label "Guests:")
    opened = False
    try:
        page.get_by_text("Guests:", exact=False).wait_for(timeout=20000)
        # click something right after "Guests:" label (input/button/div)
        box = page.get_by_text("Guests:", exact=False).locator(
            "xpath=following::*[self::input or self::button or self::div][1]"
        )
        box.click(timeout=5000)
        opened = True
    except Exception:
        pass

    # 2) Fallback: click existing value "1A" if visible
    if not opened:
        try:
            page.get_by_text("1A", exact=True).click(timeout=5000)
            opened = True
        except Exception:
            pass

    if not opened:
        return False

    # 3) Try click exact option text like "2A"
    try:
        page.get_by_text(target, exact=True).click(timeout=5000)
        return True
    except Exception:
        pass

    # 4) Fallback: try plus button inside guest picker
    try:
        # Some pickers show + / - controls for guests.
        # Click + (adults-1) times. (Assumes it started at 1)
        plus = page.locator("button:has-text('+')").first
        for _ in range(max(0, adults - 1)):
            plus.click(timeout=3000)
        page.keyboard.press("Escape")
        return True
    except Exception:
        return False


def click_show_availability(page) -> bool:
    """
    Click 'Show availability' button in the Campground Permits card.
    """
    try:
        page.get_by_text("Campground Permits", exact=False).wait_for(timeout=30000)
    except Exception:
        return False

    # Prefer role-based button locator
    try:
        btn = page.get_by_role("button", name="Show availability").first
        btn.wait_for(state="visible", timeout=30000)
        btn.click(timeout=15000)
        return True
    except Exception:
        pass

    # Fallback by text
    try:
        page.get_by_text("Show availability", exact=False).first.click(timeout=15000)
        return True
    except Exception:
        return False


def check_once():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        page.goto(BOOK_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(8000)

        arrival_str = fmt_newbook(ARRIVAL)
        departure_str = fmt_newbook(DEPARTURE)

        arrival_ok = fill_by_label(page, "Arrival date:", arrival_str)
        departure_ok = fill_by_label(page, "Departure date:", departure_str)
        guests_ok = set_guests(page, ADULTS)

        note = (
            f"arrival_ok={arrival_ok}, departure_ok={departure_ok}, guests_ok={guests_ok}, "
            f"arrival='{arrival_str}', departure='{departure_str}', adults={ADULTS}"
        )

        show_ok = click_show_availability(page)
        if not show_ok:
            write_debug(page, "Could not click 'Show availability'. " + note)
            browser.close()
            return False, "CLOSED (could not open availability). " + note

        page.wait_for_timeout(8000)

        body = page.inner_text("body").lower()

        # Signals
        has_add_booking = "add booking" in body
        has_checkout = "check out" in body or "checkout" in body
        has_criteria_error = "do not meet the required criteria" in body
        has_no_avail = any(k in body for k in ["no availability", "not available", "sold out", "unavailable"])

        write_debug(page, "Run completed. " + note)
        browser.close()

        if (has_add_booking or has_checkout) and not has_no_avail and not has_criteria_error:
            return True, "OPEN signals found (Add Booking/Checkout). " + note

        if has_criteria_error:
            return False, "CLOSED (criteria error shown). " + note

        return False, "CLOSED (no open signals). " + note


def main():
    # Optional: send a test email to confirm SMTP works
    if SEND_TEST_EMAIL:
        send_email(
            subject="Havasupai checker: test email ✅",
            body="If you received this, GitHub Actions → Gmail SMTP is working."
        )
        print("TEST EMAIL SENT")

    key = f"{ARRIVAL}_{DEPARTURE}_{ADULTS}"
    state = load_state()
    already_alerted = state.get(key, {}).get("alerted", False)

    is_open, detail = check_once()
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

    if is_open and not already_alerted:
        subject = f"Havasupai may be OPEN: {ARRIVAL} to {DEPARTURE} for {ADULTS} adults"
        body = (
            f"{detail}\n"
            f"Checked: {now}\n\n"
            f"Go ASAP: {BOOK_URL}\n\n"
            f"Tip: Add permits one at a time via 'Add Booking'."
        )
        send_email(subject, body)
        state[key] = {"alerted": True, "last_alert": now}
        save_state(state)
        print("ALERT SENT:", detail)
    elif not is_open:
        if already_alerted:
            state[key] = {"alerted": False}
            save_state(state)
        print("CLOSED:", detail)
    else:
        print("OPEN but already alerted:", detail)


if __name__ == "__main__":
    main()
