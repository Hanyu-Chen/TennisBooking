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

import html
import json
import logging
import os
import re
import sys

from curl_cffi import requests
from dotenv import load_dotenv
from pydantic import ValidationError

from playbypoint.models import (
    DEFAULT_CARD_LAST4,
    DEFAULT_PROGRAM_SLUG,
    BookingEvent,
    ClinicBookingRequest,
    Payment,
    ProgramData,
    SavedCard,
)

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("log/booking.log")],
)

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


def find_open_session_id(program_data: ProgramData, date: str) -> int:
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
) -> dict:
    # clinic_id/plan_id are derived from the same fetch as the session lookup
    # (rather than taken as separate defaulted args) so they can never drift
    # out of sync with program_slug.
    program_page = get_program_page(session, program_slug)
    program_data = parse_program_data(program_page, program_slug)
    session_id = find_open_session_id(program_data, date)
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


def lambda_handler(event, context):
    session = requests.Session(impersonate="chrome124")
    try:
        login(session)
        session_cookie = session.cookies.get("_paybycourt_session", "")
        logging.info(
            {
                "msg": "Login successful",
                "email": os.environ.get("USER_EMAIL"),
                "logged_in_check": is_logged_in(session),
                "session_fingerprint": session_cookie[:8],
            }
        )
        try:
            booking_event = BookingEvent.model_validate(event)
        except ValidationError as e:
            raise RuntimeError(f"Invalid event: {e}") from e

        result = book_court(
            session,
            date=booking_event.date.isoformat(),
            program_slug=booking_event.program_slug,
            card_last4=booking_event.card_last4,
        )
    except (RuntimeError, NotImplementedError) as e:
        return {"status": "failed", "reason": str(e)}

    return {"status": "success", "result": result}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m playbypoint.book_court YYYY-MM-DD [program_slug] [card_last4]")
        sys.exit(1)

    event = {"date": sys.argv[1]}
    if len(sys.argv) > 2:
        event["program_slug"] = sys.argv[2]
    if len(sys.argv) > 3:
        event["card_last4"] = sys.argv[3]

    result = lambda_handler(event, None)
    print(result)
    sys.exit(0 if result["status"] == "success" else 1)
