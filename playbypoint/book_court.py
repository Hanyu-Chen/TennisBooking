"""Automate finding/booking a tennis court slot on PlayByPoint.

Credentials are read from USER_EMAIL / USER_PASSWORD via .env — never hardcode them.

Requests go through curl_cffi (impersonating Chrome's TLS fingerprint) because
PlayByPoint sits behind Cloudflare, which blocks plain requests/urllib3 traffic
before it ever reaches the app.

Login targets the real Devise auth endpoint (POST /users/sign_in.html) and has
been verified against the live site.

Booking (clinics): the real submission endpoint and payload shape were
reverse-engineered from the site's own JS bundle and confirmed working against
a real booking (see CLAUDE.md for the full writeup, and models.py for the
validated shapes). `payment.card_details` is not a payment-gateway token —
it's simply one of the objects returned by GET /api/cards (the client just
passes through whatever the saved-card-selection UI received), keyed by that
card's PlayByPoint-internal `id`.
"""

import argparse
import html
import json
import logging
import os
import re
import sys
from datetime import UTC, date, datetime, timedelta

from curl_cffi import requests
from dotenv import load_dotenv
from pydantic import ValidationError

from playbypoint.models import (
    DEFAULT_CARD_LAST4,
    DEFAULT_PROGRAM_SLUG,
    SCHEDULED_CARD_LAST4,
    WEEKDAY_SCHEDULE,
    WEEKDAY_SCHEDULE_SAME_DAY_PROTECTION,
    BookingEvent,
    ClinicBookingRequest,
    Payment,
    ProgramData,
    SavedCard,
    ScheduledBookingResults,
    Weekday,
)

load_dotenv()

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

BASE_URL = "https://app.playbypoint.com"


def login(session: requests.Session) -> None:
    email = os.environ.get("USER_EMAIL")
    password = os.environ.get("USER_PASSWORD")
    if not email or not password:
        raise RuntimeError("USER_EMAIL and USER_PASSWORD must be set in the environment")

    sign_in_page = session.get(f"{BASE_URL}/users/sign_in")
    token_match = re.search(r'name="authenticity_token" value="([^"]+)"', sign_in_page.text)
    if not token_match:
        raise RuntimeError("Could not find CSRF token on sign-in page")

    res = session.post(
        f"{BASE_URL}/users/sign_in.html",
        data={
            "authenticity_token": token_match.group(1),
            "user[email]": email,
            "user[password]": password,
            "user[remember_me]": "0",
        },
    )

    if "Invalid Email or password" in res.text:
        raise RuntimeError("Login rejected: invalid email or password")
    if "/users/sign_in" in res.url:
        raise RuntimeError("Login did not succeed (still on sign-in page)")


def is_logged_in(session: requests.Session) -> bool:
    """Devise redirects an already-authenticated session away from /users/sign_in."""
    res = session.get(f"{BASE_URL}/users/sign_in")
    return "/users/sign_in" not in res.url


def get_saved_cards(session: requests.Session) -> list[SavedCard]:
    res = session.get(f"{BASE_URL}/api/cards")
    try:
        return [SavedCard.model_validate(c) for c in res.json()]
    except ValidationError as e:
        raise RuntimeError(f"GET /api/cards response no longer matches expected shape: {e}") from e


def find_card_by_last4(cards: list[SavedCard], last4: str) -> SavedCard:
    for card in cards:
        if card.last4 == last4:
            return card
    raise RuntimeError(f"No saved card found ending in {last4}")


def get_program_page(session: requests.Session, program_slug: str) -> requests.Response:
    return session.get(f"{BASE_URL}/programs/{program_slug}")


def parse_program_data(page: requests.Response, program_slug: str) -> ProgramData:
    """Programs pages embed a React props blob with the clinic id, price plans,
    and every session's real id/capacity -- including sessions created after
    this code was written, so this has to be fetched fresh, not hardcoded."""
    for m in re.finditer(r'data-react-props="(.*?)"\s', page.text, re.DOTALL):
        raw = html.unescape(m.group(1))
        if '"clinic_id"' in raw:
            try:
                return ProgramData.model_validate(json.loads(raw))
            except ValidationError as e:
                raise RuntimeError(
                    f"PlayByPoint's /programs/{program_slug} response no longer "
                    f"matches the expected shape: {e}"
                ) from e
    raise RuntimeError(f"Could not find clinic data on /programs/{program_slug}")


def find_open_session_id(
    program_data: ProgramData, date: str, same_day_protection: bool = True
) -> int:
    # Circuit breaker: refuse to book a session on today's date (in UTC, matching
    # when this is run), regardless of what time that session starts -- avoids the
    # timezone guesswork of comparing hour_start (facility-local) against now(UTC).
    if same_day_protection and date == datetime.now(UTC).date().isoformat():
        raise RuntimeError(f"Refusing to book a session on today's date ({date})")

    for s in program_data.sessions:
        if s.lesson_date == date:
            if s.player_count >= s.capacity:
                raise RuntimeError(f"Session on {date} is full ({s.player_count}/{s.capacity})")
            return s.id
    raise RuntimeError(f"No session found on {date}")


def book_court(
    session: requests.Session,
    date: str,
    program_slug: str = DEFAULT_PROGRAM_SLUG,
    card_last4: str = DEFAULT_CARD_LAST4,
    same_day_protection: bool = True,
) -> dict:
    # clinic_id/plan_id are derived from the same fetch as the session lookup
    # (rather than taken as separate defaulted args) so they can never drift
    # out of sync with program_slug.
    program_page = get_program_page(session, program_slug)
    program_data = parse_program_data(program_page, program_slug)
    session_id = find_open_session_id(program_data, date, same_day_protection=same_day_protection)
    plan_id = program_data.prices[0].id

    cards = get_saved_cards(session)
    card = find_card_by_last4(cards, card_last4)

    try:
        booking_request = ClinicBookingRequest(
            plan_id=plan_id,
            clinic_lesson_ids=[session_id],
            payment=Payment(method="card", card_details=card),
        )
    except ValidationError as e:
        raise RuntimeError(f"Could not build a valid booking request: {e}") from e

    # Rails' CSRF protection covers this JSON endpoint too, so a fresh token
    # (matching the current session) is required as an X-CSRF-Token header.
    csrf_match = re.search(r'name="csrf-token" content="([^"]+)"', program_page.text)
    if not csrf_match:
        raise RuntimeError("Could not find a fresh CSRF token before booking")

    res = session.post(
        f"{BASE_URL}/api/public/clinics/{program_data.clinic_id}",
        data=booking_request.model_dump_json(),
        headers={
            "Content-Type": "application/json",
            "X-CSRF-Token": csrf_match.group(1),
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{BASE_URL}/programs/{program_slug}",
        },
    )
    if res.status_code != 200:
        raise RuntimeError(f"Booking request failed: {res.status_code} {res.text}")

    return {
        "clinic_id": program_data.clinic_id,
        "session_id": session_id,
        "card_last4": card_last4,
        "response": res.json(),
    }


def next_occurrences(weekday: Weekday, today: date | None = None) -> list[date]:
    """Every calendar date in the next 8 days (today counts) that falls on
    the given weekday. Usually a single date, but an 8-day window can catch
    the same weekday twice (today and 7 days out)."""
    today = today or date.today()
    # Weekday is declared Monday..Sunday, matching date.weekday()'s 0..6.
    weekdays_by_index = list(Weekday)
    dates = [
        candidate
        for offset in range(8)
        if weekdays_by_index[(candidate := today + timedelta(days=offset)).weekday()] == weekday
    ]
    if not dates:
        raise RuntimeError(f"No {weekday.value} found in the next 8 days")
    return dates


def book_scheduled_programs(
    session: requests.Session,
    card_last4: str = SCHEDULED_CARD_LAST4,
    same_day_protection: bool = WEEKDAY_SCHEDULE_SAME_DAY_PROTECTION,
) -> ScheduledBookingResults:
    """Book every program WEEKDAY_SCHEDULE (weekday_schedule.yaml) assigns to
    each configured weekday, for every occurrence of that weekday in the next
    8 days. One program's failure is logged and doesn't stop the rest; once
    every program has been attempted, a RuntimeError is raised summarizing
    any failures."""
    results = ScheduledBookingResults()
    for weekday, programs in WEEKDAY_SCHEDULE.items():
        target_dates = next_occurrences(weekday)
        for target_date in target_dates:
            for program in programs:
                try:
                    book_court(
                        session,
                        date=target_date.isoformat(),
                        program_slug=program.value,
                        card_last4=card_last4,
                        same_day_protection=same_day_protection,
                    )
                    results.success_count += 1
                    logger.info(
                        {
                            "msg": "Booking succeeded",
                            "weekday": weekday.value,
                            "date": target_date.isoformat(),
                            "program_slug": program.value,
                        }
                    )
                except RuntimeError as e:
                    logger.error(
                        {
                            "weekday": weekday.value,
                            "date": target_date.isoformat(),
                            "program_slug": program.value,
                            "reason": str(e),
                        }
                    )
                    results.failed_count += 1
                    continue

    if results.failed_count:
        total = results.success_count + results.failed_count
        raise RuntimeError(f"{results.failed_count}/{total} scheduled bookings failed")

    return results


def _login_and_log(session: requests.Session) -> None:
    login(session)
    session_cookie = session.cookies.get("_paybycourt_session", "")
    logger.info(
        {
            "msg": "Login successful",
            "email": os.environ.get("USER_EMAIL"),
            "logged_in_check": is_logged_in(session),
            "session_fingerprint": session_cookie[:8],
        }
    )


def _create_session() -> requests.Session:
    return requests.Session(impersonate="chrome124", proxy=os.environ.get("PROXY_URL"))


def lambda_handler(event, context):
    """Book a single explicit date/program_slug. See BookingEvent for the
    required event shape."""

    try:
        booking_event: BookingEvent = BookingEvent.model_validate(event)
    except ValidationError as e:
        return {"status": "failed", "reason": f"Invalid event: {e}"}

    session = _create_session()
    try:
        _login_and_log(session)

        result = book_court(
            session,
            date=booking_event.date.isoformat(),
            program_slug=booking_event.program_slug,
            card_last4=booking_event.card_last4,
        )
    except (RuntimeError, NotImplementedError) as e:
        logger.error(
            {
                "date": booking_event.date.isoformat(),
                "program_slug": booking_event.program_slug,
                "reason": str(e),
            }
        )
        return {"status": "failed", "reason": str(e)}

    logger.info(
        {
            "msg": "Booking succeeded",
            "date": booking_event.date.isoformat(),
            "program_slug": booking_event.program_slug,
        }
    )
    return {"status": "success", "result": result}


def scheduled_lambda_handler(event, context):
    """Book every program WEEKDAY_SCHEDULE (weekday_schedule.yaml) assigns to
    each configured weekday, for every occurrence of that weekday in the next
    8 days, using the card from that same YAML file. This is a distinct
    Lambda entry point from lambda_handler --
    point the EventBridge rule driving the recurring cron booking at this
    handler specifically, so it always runs the full schedule; event content
    is ignored."""
    session = _create_session()
    try:
        _login_and_log(session)
        results = book_scheduled_programs(session)
    except (RuntimeError, NotImplementedError) as e:
        return {"status": "failed", "reason": str(e)}

    return {"status": "success", "results": results.model_dump(mode="json")}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Book a PlayByPoint clinic/lesson.")
    parser.add_argument("date", nargs="?", help="YYYY-MM-DD -- book a single specific date/program")
    parser.add_argument("program_slug", nargs="?", default=DEFAULT_PROGRAM_SLUG)
    parser.add_argument("card_last4", nargs="?", default=DEFAULT_CARD_LAST4)
    parser.add_argument(
        "--scheduled",
        action="store_true",
        help=(
            "Run the full weekday_schedule.yaml schedule (every weekday it defines, "
            "at its next occurrence) instead of a single date/program_slug."
        ),
    )
    args = parser.parse_args()

    if args.scheduled:
        result = scheduled_lambda_handler({}, None)
    else:
        if not args.date:
            parser.error("date is required unless --scheduled is given")
        event = {
            "date": args.date,
            "program_slug": args.program_slug,
            "card_last4": args.card_last4,
        }
        result = lambda_handler(event, None)

    print(result)
    sys.exit(0 if result["status"] == "success" else 1)
