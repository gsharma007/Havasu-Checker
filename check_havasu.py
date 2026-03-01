import os
import json
import smtplib
from email.message import EmailMessage
from datetime import datetime
from playwright.sync_api import sync_playwright

STATE_FILE = "state.json"

BOOK_URL = "https://bookingsus.newbook.cloud/online/havasupai"

ARRIVAL = os.getenv("ARRIVAL", "2026-05-25")
DEPARTURE = os.getenv("DEPARTURE", "2026-05-28")
PEOPLE = os.getenv("PEOPLE", "2")

SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
ALERT_TO = os.getenv("ALERT_TO")

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def send_email(subject: str, body: str):
    msg = EmailMessage()
    msg["From"] = SMTP_USER
    msg["To"] = ALERT_TO
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls()
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)

def set_date_like_user(page, label_text: str, value_yyyy_mm_dd: str):
    """
    Tries multiple strategies because Newbook inputs can vary:
    - aria label might match "Arrival date:"
    - input might be a date picker expecting MM/DD/YYYY
    """
    y, m, d = value_yyyy_mm_dd.split("-")
    mmddyyyy = f"{m}/{d}/{y}"

    # 1) Try get_by_label (best if accessible labels exist)
    for v in (value_yyyy_mm_dd, mmddyyyy):
        try:
            page.get_by_label(label_text).fill(v, timeout=2000)
            return
        except Exception:
            pass

    # 2) Fallback: find the label text on page and fill the nearest input
    for v in (value_yyyy_mm_dd, mmddyyyy):
        try:
            label = page.get_by_text(label_text, exact=False)
            container = label.locator("xpath=ancestor-or-self::*[1]")
            # search forward for an input in nearby DOM
            inp = container.locator("xpath=following::input[1]")
            inp.fill(v, timeout=2000)
            return
        except Exception:
            pass

def check_once():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(BOOK_URL, wait_until="domcontentloaded")

        # Wait a bit for dynamic availability UI
        page.wait_for_timeout(3000)

        # Fill fields
        set_date_like_user(page, "Arrival date:", ARRIVAL)
        set_date_like_user(page, "Departure date:", DEPARTURE)

        # People (over 6 years old)
        # Try label first; fallback to any "People" input-like control
        filled_people = False
        try:
            page.get_by_label("People (over 6 years old):").fill(str(PEOPLE), timeout=2000)
            filled_people = True
        except Exception:
            pass

        if not filled_people:
            try:
                page.get_by_text("People (over 6 years old):", exact=False).locator(
                    "xpath=following::input[1]"
                ).fill(str(PEOPLE), timeout=2000)
            except Exception:
                pass

        # Click Apply
        try:
            page.get_by_role("button", name="Apply").click(timeout=5000)
        except Exception:
            # Sometimes it's not a <button>; click by text
            page.get_by_text("Apply", exact=True).click(timeout=5000)

        # Give it time to load results
        page.wait_for_timeout(5000)

        body_text = page.inner_text("body").lower()

        # Heuristic signals of availability
        has_add_booking = "add booking" in body_text
        has_checkout = "check out" in body_text or "checkout" in body_text
        has_no_avail = any(k in body_text for k in ["no availability", "not available", "sold out", "unavailable"])

        browser.close()

        # If we see Add Booking / checkout and NOT an explicit no-availability message → likely open
        if (has_add_booking or has_checkout) and not has_no_avail:
            return True, "Availability signals detected (Add Booking / Checkout visible)."
        return False, "No availability signals detected."

def main():
    key = f"{ARRIVAL}_{DEPARTURE}_{PEOPLE}"
    state = load_state()
    already_alerted = state.get(key, {}).get("alerted", False)

    is_open, detail = check_once()
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

    if is_open and not already_alerted:
        subject = f"Havasupai may be OPEN: {ARRIVAL} to {DEPARTURE} for {PEOPLE} people"
        body = (
            f"{detail}\n"
            f"Checked: {now}\n\n"
            f"Go ASAP: {BOOK_URL}\n\n"
            f"Tip: The site notes you add one permit at a time via 'Add Booking' before checkout."
        )
        send_email(subject, body)
        state[key] = {"alerted": True, "last_alert": now}
        save_state(state)
        print("ALERT SENT:", detail)
    elif not is_open:
        # reset so we can alert next time it opens
        if already_alerted:
            state[key] = {"alerted": False}
            save_state(state)
        print("CLOSED:", detail)
    else:
        print("OPEN but already alerted:", detail)

if __name__ == "__main__":
    main()