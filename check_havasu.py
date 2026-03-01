import os
import json
import smtplib
from email.message import EmailMessage
from datetime import datetime
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

STATE_FILE = "state.json"

BOOK_URL = os.getenv("BOOK_URL", "https://bookingsus.newbook.cloud/online/havasupai")

# Dates: YYYY-MM-DD
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


def to_mmddyyyy(date_yyyy_mm_dd: str) -> str:
    y, m, d = date_yyyy_mm_dd.split("-")
    return f"{m}/{d}/{y}"


def fill_input(page, locator, value: str) -> bool:
    try:
        locator.wait_for(state="attached", timeout=3000)
        locator.scroll_into_view_if_needed(timeout=3000)
        locator.click(timeout=3000)
        locator.fill(value, timeout=3000)
        # Trigger events that some JS frameworks rely on
        locator.press("Tab", timeout=1000)
        return True
    except Exception:
        return False


def try_fill_date(page, label_text: str, ymd: str) -> bool:
    candidates = [ymd, to_mmddyyyy(ymd)]

    # 1) Accessible label
    for v in candidates:
        try:
            if fill_input(page, page.get_by_label(label_text), v):
                return True
        except Exception:
            pass

    # 2) Nearby input after visible label text
    for v in candidates:
        try:
            lbl = page.get_by_text(label_text, exact=False)
            inp = lbl.locator("xpath=following::input[1]")
            if fill_input(page, inp, v):
                return True
        except Exception:
            pass

    # 3) Common input names/ids (best-effort)
    for v in candidates:
        for sel in ["input[name*='arrival' i]", "input[name*='start' i]", "input[id*='arrival' i]", "input[id*='start' i]"]:
            if "Departure" in label_text:
                sel = sel.replace("arrival", "departure").replace("start", "end")
            loc = page.locator(sel)
            if loc.count() > 0 and fill_input(page, loc.first, v):
                return True

    return False


def try_fill_people(page, people: str) -> bool:
    # 1) Accessible label
    for label in ["People (over 6 years old):", "People (over 6 years old)", "People"]:
        try:
            if fill_input(page, page.get_by_label(label), people):
                return True
        except Exception:
            pass

    # 2) Text then following input
    try:
        loc = page.get_by_text("People", exact=False).locator("xpath=following::input[1]")
        if fill_input(page, loc, people):
            return True
    except Exception:
        pass

    # 3) Any number input on page (last resort)
    try:
        num = page.locator("input[type='number']")
        if num.count() > 0 and fill_input(page, num.first, people):
            return True
    except Exception:
        pass

    return False


def click_apply_best_effort(page) -> bool:
    """
    Click an enabled Apply button. If none becomes enabled, return False (don't crash).
    """
    # Wait for any Apply button that is enabled (not disabled)
    try:
        page.wait_for_function(
            """
            () => {
              const btns = Array.from(document.querySelectorAll("button"));
              return btns.some(b => b && b.textContent && b.textContent.trim()==="Apply" && !b.disabled && b.offsetParent !== null);
            }
            """,
            timeout=30000,
        )
    except PWTimeout:
        return False

    # Click the first visible enabled Apply button
    btn = page.locator("button:has-text('Apply'):not([disabled])").first
    try:
        btn.scroll_into_view_if_needed(timeout=5000)
        btn.click(timeout=10000)
        return True
    except Exception:
        return False


def write_debug(page, note: str):
    # Always write debug; workflow will upload artifacts
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


def check_once():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        page.goto(BOOK_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(10000)  # give Newbook time to render fully

        arrival_ok = try_fill_date(page, "Arrival date:", ARRIVAL)
        departure_ok = try_fill_date(page, "Departure date:", DEPARTURE)
        people_ok = try_fill_people(page, str(PEOPLE))

        note = f"arrival_ok={arrival_ok}, departure_ok={departure_ok}, people_ok={people_ok}"

        # Try to click Apply; if it never enables, save debug + return CLOSED gracefully
        applied = click_apply_best_effort(page)
        if not applied:
            write_debug(page, "Apply never became enabled/visible. " + note)
            browser.close()
            return False, "CLOSED (could not click Apply). " + note

        # Wait results update
        page.wait_for_timeout(8000)

        body_text = page.inner_text("body").lower()

        has_add_booking = "add booking" in body_text
        has_checkout = "check out" in body_text or "checkout" in body_text
        has_loading = "loading availability" in body_text
        has_no_avail = any(k in body_text for k in ["no availability", "not available", "sold out", "unavailable"])

        if has_loading:
            page.wait_for_timeout(8000)
            body_text = page.inner_text("body").lower()
            has_add_booking = "add booking" in body_text
            has_checkout = "check out" in body_text or "checkout" in body_text
            has_no_avail = any(k in body_text for k in ["no availability", "not available", "sold out", "unavailable"])

        # Save debug every run (helps confirm what it saw)
        write_debug(page, "Run completed. " + note)

        browser.close()

        if (has_add_booking or has_checkout) and not has_no_avail:
            return True, "OPEN signals found. " + note
        return False, "CLOSED (no open signals after Apply). " + note


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
