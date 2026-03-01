import os
import json
import smtplib
from email.message import EmailMessage
from datetime import datetime
from playwright.sync_api import sync_playwright

STATE_FILE = "state.json"

BOOK_URL = os.getenv("BOOK_URL", "https://bookingsus.newbook.cloud/online/havasupai")

# Dates: use YYYY-MM-DD
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
    if not all([SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, ALERT_TO]):
        raise RuntimeError("Missing SMTP env vars. Add GitHub Secrets: SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, ALERT_TO")

    msg = EmailMessage()
    msg["From"] = SMTP_USER
    msg["To"] = ALERT_TO
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
        s.starttls()
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)


def to_mmddyyyy(date_yyyy_mm_dd: str) -> str:
    y, m, d = date_yyyy_mm_dd.split("-")
    return f"{m}/{d}/{y}"


def fill_best_effort(page, label_text: str, value: str) -> bool:
    """
    Try multiple strategies to fill an input associated with a label-ish text.
    Returns True if filled.
    """
    # Strategy 1: accessible label
    try:
        page.get_by_label(label_text).fill(value, timeout=2500)
        return True
    except Exception:
        pass

    # Strategy 2: find text then fill nearest following input
    try:
        page.get_by_text(label_text, exact=False).locator("xpath=following::input[1]").fill(value, timeout=2500)
        return True
    except Exception:
        pass

    return False


def click_apply(page):
    """
    Click the real Apply button (avoid strict-mode issues with duplicate "Apply" text).
    Wait until enabled.
    """
    apply_btn = page.locator("button.newbook-applyBtn", has_text="Apply")
    apply_btn.wait_for(state="visible", timeout=20000)

    # Wait for it to become enabled (not disabled)
    page.wait_for_function("btn => btn && !btn.disabled", apply_btn, timeout=20000)

    apply_btn.click(timeout=20000)


def check_once():
    """
    Returns (is_available: bool, details: str)
    Also writes debug.png and debug.html on failures (via workflow artifact upload).
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        # Load the booking page
        page.goto(BOOK_URL, wait_until="domcontentloaded", timeout=60000)

        # Give the page time to hydrate / render JS widgets on GitHub runner
        page.wait_for_timeout(8000)

        # Fill arrival/departure (try YYYY-MM-DD then MM/DD/YYYY)
        arrival_ok = (
            fill_best_effort(page, "Arrival date:", ARRIVAL)
            or fill_best_effort(page, "Arrival date:", to_mmddyyyy(ARRIVAL))
        )
        departure_ok = (
            fill_best_effort(page, "Departure date:", DEPARTURE)
            or fill_best_effort(page, "Departure date:", to_mmddyyyy(DEPARTURE))
        )

        # Fill people
        people_ok = (
            fill_best_effort(page, "People (over 6 years old):", str(PEOPLE))
            or fill_best_effort(page, "People", str(PEOPLE))
        )

        # If inputs didn't fill, still attempt apply; but keep info in details.
        info = []
        info.append(f"arrival_ok={arrival_ok}")
        info.append(f"departure_ok={departure_ok}")
        info.append(f"people_ok={people_ok}")

        # Click Apply safely
        click_apply(page)

        # Wait for results to update
        page.wait_for_timeout(6000)

        body_text = page.inner_text("body").lower()

        # Heuristics:
        has_add_booking = "add booking" in body_text
        has_checkout = "check out" in body_text or "checkout" in body_text
        has_loading = "loading availability" in body_text
        has_no_avail = any(k in body_text for k in ["no availability", "not available", "sold out", "unavailable"])

        # If still loading, wait a bit more and re-check once
        if has_loading:
            page.wait_for_timeout(6000)
            body_text = page.inner_text("body").lower()
            has_add_booking = "add booking" in body_text
            has_checkout = "check out" in body_text or "checkout" in body_text
            has_no_avail = any(k in body_text for k in ["no availability", "not available", "sold out", "unavailable"])

        # Always save debug snapshot for visibility (small files); helpful even on success
        page.screenshot(path="debug.png", full_page=True)
        with open("debug.html", "w", encoding="utf-8") as f:
            f.write(page.content())

        browser.close()

        # If we see Add Booking / Checkout and NOT explicit "no availability" -> likely open
        if (has_add_booking or has_checkout) and not has_no_avail:
            return True, f"OPEN signals found. ({', '.join(info)})"
        return False, f"CLOSED (no open signals). ({', '.join(info)})"


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
            f"Tip: Newbook adds 1 permit at a time (click 'Add Booking' repeatedly)."
        )
        send_email(subject, body)
        state[key] = {"alerted": True, "last_alert": now}
        save_state(state)
        print("ALERT SENT:", detail)
    elif not is_open:
        # Reset so we can alert again next time it opens
        if already_alerted:
            state[key] = {"alerted": False}
            save_state(state)
        print("CLOSED:", detail)
    else:
        print("OPEN but already alerted:", detail)


if __name__ == "__main__":
    main()
