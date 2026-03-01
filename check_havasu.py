import os
import json
import smtplib
from email.message import EmailMessage
from datetime import datetime
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

STATE_FILE = "state.json"

BOOK_URL = os.getenv("BOOK_URL", "https://bookingsus.newbook.cloud/online/havasupai")

# Inputs
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
        raise RuntimeError("Missing SMTP env vars/secrets (SMTP_HOST/PORT/USER/PASS, ALERT_TO).")

    msg = EmailMessage()
    msg["From"] = SMTP_USER
    msg["To"] = ALERT_TO
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
        s.starttls()
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)


def write_debug(page, note: str):
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


def parse_ymd(ymd: str):
    dt = datetime.strptime(ymd, "%Y-%m-%d")
    return dt.year, dt.month, dt.day, dt.strftime("%b %Y"), dt.strftime("%B %Y")


def open_datepicker(page, which: str) -> bool:
    """
    which: 'arrival' or 'departure'
    Click the calendar icon next to the corresponding input.
    """
    label = "Arrival date:" if which == "arrival" else "Departure date:"
    try:
        lbl = page.get_by_text(label, exact=False)
        lbl.wait_for(state="visible", timeout=20000)
        # calendar button is usually the first button following the input
        # We'll find the first button after the label.
        # Safer: locate the input after label, then its following button (calendar icon)
        inp = lbl.locator("xpath=following::input[1]")
        inp.wait_for(state="visible", timeout=20000)
        cal_btn = inp.locator("xpath=following::button[1]")
        cal_btn.click(timeout=8000)
        return True
    except Exception:
        # fallback: try clicking any visible calendar icon buttons near the top
        try:
            page.locator("button").filter(has=page.locator("svg")).first.click(timeout=8000)
            return True
        except Exception:
            return False


def click_day_in_month_grid(page, month_name: str, day: int) -> bool:
    """
    Datepicker shows month header text like "May 2026".
    Click the given day number under that month.
    We scope by finding the month header, then clicking a day cell inside its panel.
    """
    # Month headers can appear as text nodes. We find the header and then its ancestor panel.
    try:
        header = page.get_by_text(month_name, exact=False)
        header.wait_for(state="visible", timeout=20000)
        panel = header.locator("xpath=ancestor::div[1]")
        # Click the day within that panel.
        # Use get_by_role('button') if days are buttons; otherwise click text.
        # Try button first:
        try:
            panel.get_by_role("button", name=str(day), exact=True).click(timeout=8000)
            return True
        except Exception:
            # fallback click text day inside panel (but avoid header text)
            panel.get_by_text(str(day), exact=True).click(timeout=8000)
            return True
    except Exception:
        return False


def set_dates_via_datepicker(page, arrival_ymd: str, departure_ymd: str) -> tuple[bool, bool]:
    """
    Click calendar icon and choose arrival+departure days.
    We assume datepicker shows the needed month (May 2026) with arrows, and often shows two months.
    """
    ay, am, ad, a_mon_short, a_mon_long = parse_ymd(arrival_ymd)
    dy, dm, dd, d_mon_short, d_mon_long = parse_ymd(departure_ymd)

    # We’ll use long month format because your screenshot shows "May 2026".
    arrival_month = a_mon_long
    departure_month = d_mon_long

    arrival_ok = False
    departure_ok = False

    if not open_datepicker(page, "arrival"):
        return False, False

    # In your UI, selecting range is: click start day then end day.
    # We'll click both while picker is open.
    arrival_ok = click_day_in_month_grid(page, arrival_month, ad)
    departure_ok = click_day_in_month_grid(page, departure_month, dd)

    # Close picker (Esc) to commit selection
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass

    return arrival_ok, departure_ok


def open_guests_popover(page) -> bool:
    try:
        lbl = page.get_by_text("Guests:", exact=False)
        lbl.wait_for(state="visible", timeout=20000)
        guests_input = lbl.locator("xpath=following::input[1]")
        guests_input.wait_for(state="visible", timeout=20000)
        guests_input.click(timeout=8000)
        return True
    except Exception:
        # fallback click the value like "1A"
        try:
            page.get_by_text("1A", exact=True).click(timeout=8000)
            return True
        except Exception:
            return False


def set_guests_and_apply(page, adults: int) -> bool:
    """
    Guests popover has +/- and Apply (as in your screenshot).
    """
    if not open_guests_popover(page):
        return False

    # Wait for popover text
    try:
        page.get_by_text("People (over 6 years old):", exact=False).wait_for(timeout=20000)
    except Exception:
        return False

    # Find plus button in that popover area
    try:
        # Scope to the popover by grabbing an ancestor of the label
        pop = page.get_by_text("People (over 6 years old):", exact=False).locator("xpath=ancestor::div[1]")
        # find numeric input (current count)
        try:
            count_input = pop.locator("input").first
            current_raw = count_input.input_value(timeout=3000)
            current = int("".join(c for c in current_raw if c.isdigit()) or "1")
        except Exception:
            current = 1

        # find + button (often the last button in that small control)
        plus = pop.locator("button").filter(has_text="+").first
        if adults > current:
            for _ in range(adults - current):
                plus.click(timeout=5000)

        # click Apply inside popover
        pop.get_by_role("button", name="Apply").click(timeout=10000)
        return True
    except Exception:
        # fallback: click any visible Apply button
        try:
            page.get_by_role("button", name="Apply").click(timeout=10000)
            return True
        except Exception:
            return False


def click_show_availability_for_campground(page) -> bool:
    """
    Click Show availability on the Campground Permits card (not Lodge).
    """
    try:
        title = page.get_by_text("Campground Permits", exact=False)
        title.wait_for(timeout=30000)
        card = title.locator("xpath=ancestor::div[1]")
        btn = card.get_by_text("Show availability", exact=False).first
        btn.click(timeout=15000)
        return True
    except Exception:
        # fallback: first visible Show availability
        try:
            page.get_by_text("Show availability", exact=False).first.click(timeout=15000)
            return True
        except Exception:
            return False


def detect_open_for_correct_filters(page) -> tuple[bool, str]:
    """
    Strong signals only:
    - "Add Booking" is best
    - If criteria error appears, it's closed
    """
    body = page.inner_text("body").lower()
    has_add_booking = "add booking" in body
    has_criteria_error = "do not meet the required criteria" in body
    has_no_avail = any(k in body for k in ["sold out", "unavailable", "no availability", "not available"])

    if has_add_booking and not has_criteria_error and not has_no_avail:
        return True, "Add Booking found; no error text."
    return False, f"Signals: add_booking={has_add_booking}, criteria_error={has_criteria_error}, no_avail={has_no_avail}"


def check_once():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        page.goto(BOOK_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(8000)

        arrival_ok, departure_ok = set_dates_via_datepicker(page, ARRIVAL, DEPARTURE)
        guests_ok = set_guests_and_apply(page, ADULTS)
        show_ok = click_show_availability_for_campground(page)

        # Wait for UI to refresh results
        page.wait_for_timeout(7000)

        note = f"arrival_ok={arrival_ok}, departure_ok={departure_ok}, guests_ok={guests_ok}, show_ok={show_ok}, arrival={ARRIVAL}, departure={DEPARTURE}, adults={ADULTS}"

        # HARD GATE: never alert unless we successfully set everything + clicked show availability
        if not (arrival_ok and departure_ok and guests_ok and show_ok):
            write_debug(page, "Filters not set; treating as CLOSED. " + note)
            browser.close()
            return False, "CLOSED (filters not set). " + note

        is_open, detail = detect_open_for_correct_filters(page)
        write_debug(page, f"Run completed. {detail}. {note}")
        browser.close()
        return is_open, f"{detail}. {note}"


def main():
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
            f"Go ASAP: {BOOK_URL}\n"
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
