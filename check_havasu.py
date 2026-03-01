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
ADULTS = int(os.getenv("ADULTS", "2"))

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
    # UI accepts like "May 25 2026"
    dt = datetime.strptime(date_yyyy_mm_dd, "%Y-%m-%d")
    return dt.strftime("%B %d %Y")


def write_debug(page, note: str):
    # Always capture artifacts so we can see what the runner saw
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


def fill_input_near_text(page, label_text: str, value: str) -> bool:
    """
    Works with this UI: the visible label is plain text ("Arrival date:")
    and the input is the first following input element.
    """
    try:
        lbl = page.get_by_text(label_text, exact=False)
        lbl.wait_for(state="visible", timeout=20000)
        inp = lbl.locator("xpath=following::input[1]")
        inp.wait_for(state="visible", timeout=20000)

        inp.click(timeout=5000)
        # replace whatever is there
        inp.press("Control+A", timeout=2000)
        inp.fill(value, timeout=5000)
        inp.press("Tab", timeout=2000)
        return True
    except Exception:
        return False


def open_guests_popover(page) -> bool:
    """
    Click Guests value (e.g. '1A') so the popover with +/- and Apply opens.
    """
    try:
        # click the Guests input right after "Guests:"
        lbl = page.get_by_text("Guests:", exact=False)
        lbl.wait_for(state="visible", timeout=20000)
        guests_input = lbl.locator("xpath=following::input[1]")
        guests_input.wait_for(state="visible", timeout=20000)
        guests_input.click(timeout=5000)
        return True
    except Exception:
        # fallback: click "1A" text if visible
        try:
            page.get_by_text("1A", exact=True).click(timeout=5000)
            return True
        except Exception:
            return False


def set_guests_and_apply(page, adults: int) -> bool:
    """
    In your screenshot, guests are adjusted in a popover:
    'People (over 6 years old):' with - / value / + and an Apply button.
    """
    if not open_guests_popover(page):
        return False

    # Find the popover by the text inside it
    try:
        pop = page.get_by_text("People (over 6 years old):", exact=False)
        pop.wait_for(state="visible", timeout=20000)
    except Exception:
        return False

    # The number box is typically an input in between - and +
    # We'll locate the nearest input after that text.
    try:
        count_input = page.get_by_text("People (over 6 years old):", exact=False).locator(
            "xpath=following::input[1]"
        )
        count_input.wait_for(state="visible", timeout=20000)
        current_raw = count_input.input_value(timeout=3000)
        current = int("".join([c for c in current_raw if c.isdigit()]) or "1")
    except Exception:
        # If we can't read it, assume starting at 1
        current = 1

    # Click + until we reach desired adults
    try:
        plus_btn = page.get_by_text("People (over 6 years old):", exact=False).locator(
            "xpath=following::button[.='+' or contains(.,'+')][1]"
        )
        plus_btn.wait_for(state="visible", timeout=20000)

        if adults > current:
            for _ in range(adults - current):
                plus_btn.click(timeout=5000)
    except Exception:
        return False

    # Click Apply inside the popover
    try:
        apply_btn = page.locator("button:has-text('Apply')").filter(has_not=page.locator("[disabled]")).first
        # If filter above is too strict, just click the first visible Apply:
        try:
            apply_btn.wait_for(state="visible", timeout=20000)
            apply_btn.click(timeout=10000)
        except Exception:
            page.get_by_text("Apply", exact=True).click(timeout=10000)
        return True
    except Exception:
        return False


def click_show_availability_for_campground(page) -> bool:
    """
    Click 'Show availability' on the Campground Permits card.
    """
    try:
        page.get_by_text("Campground Permits", exact=False).wait_for(timeout=30000)
    except Exception:
        return False

    # Click first "Show availability" under campground section.
    # (There is also one for Lodge, so we anchor near Campground Permits.)
    try:
        section = page.get_by_text("Campground Permits", exact=False).locator("xpath=ancestor::div[1]")
        btn = section.get_by_text("Show availability", exact=False)
        btn.first.click(timeout=15000)
        return True
    except Exception:
        # fallback: click any visible Show availability (may still work)
        try:
            page.get_by_text("Show availability", exact=False).first.click(timeout=15000)
            return True
        except Exception:
            return False


def is_open_signal(page) -> tuple[bool, str]:
    """
    If bookings are open for the chosen dates/guests, the Campground card often shows
    a 'Book now' button and price. If not, you often see the red criteria message.
    """
    body = page.inner_text("body").lower()

    has_book_now = "book now" in body
    has_criteria_error = "do not meet the required criteria" in body
    has_sold_out = any(k in body for k in ["sold out", "unavailable", "no availability"])

    if has_book_now and not has_criteria_error and not has_sold_out:
        return True, "Book now visible and no error text."
    return False, f"Signals: book_now={has_book_now}, criteria_error={has_criteria_error}, sold_out={has_sold_out}"


def check_once():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        page.goto(BOOK_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(8000)

        arrival_str = fmt_newbook(ARRIVAL)
        departure_str = fmt_newbook(DEPARTURE)

        arrival_ok = fill_input_near_text(page, "Arrival date:", arrival_str)
        departure_ok = fill_input_near_text(page, "Departure date:", departure_str)
        guests_ok = set_guests_and_apply(page, ADULTS)

        note = (
            f"arrival_ok={arrival_ok}, departure_ok={departure_ok}, guests_ok={guests_ok}, "
            f"arrival='{arrival_str}', departure='{departure_str}', adults={ADULTS}"
        )

        # Click show availability (optional but helps refresh)
        show_ok = click_show_availability_for_campground(page)

        # Wait for UI refresh
        page.wait_for_timeout(6000)

        open_now, open_detail = is_open_signal(page)

        write_debug(page, f"{note}, show_ok={show_ok}, open_detail={open_detail}")
        browser.close()

        return open_now, f"{open_detail} | {note} | show_ok={show_ok}"


def main():
    # 1-time test email to verify SMTP works
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
            f"Tip: You may need to click 'Book now' quickly; permits can vanish fast."
        )
        send_email(subject, body)
        state[key] = {"alerted": True, "last_alert": now}
        save_state(state)
        print("ALERT SENT:", detail)
    elif not is_open:
        # reset alert so we can notify again when it becomes open later
        if already_alerted:
            state[key] = {"alerted": False}
            save_state(state)
        print("CLOSED:", detail)
    else:
        print("OPEN but already alerted:", detail)


if __name__ == "__main__":
    main()
