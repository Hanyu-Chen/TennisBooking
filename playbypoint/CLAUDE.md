# TennisBooking

A Python project for tennis court booking automation.

## Project structure

- `pyproject.toml` — project metadata, runtime deps, and the `dev` extra (pytest/ruff/mypy/tox), managed with `uv`
- `.venv/` — uv-managed virtual environment (do not commit)
- `README.md` — project overview

## Development setup

```bash
uv sync --all-extras   # or: make sync -- creates .venv with runtime + dev deps
```

## Conventions

- Use Python 3.13+ for all scripts and tooling
- Keep secrets (API keys, credentials, `.env`) out of git
- Only create git commits when explicitly requested
- Match existing code style in each file; keep changes minimal and focused
- Add comments only for non-obvious business logic

## Commands

| Task | Command |
|------|---------|
| Install/sync dev env | `make sync` (or `uv sync --all-extras`) |
| Run a script | `uv run python -m playbypoint.book_court ...` |
| Run tests | `make test` (or `uv run pytest`) |
| Lint | `make lint` (or `uv run ruff check .`) |
| Format | `make format` (or `uv run ruff format .`) |
| Type-check | `uv run mypy playbypoint` |
| Full matrix (lint + type + tests) | `uv run tox` |

## Goals

This project will automate finding and booking tennis courts. When adding features, prioritize reliability and clear error handling over clever abstractions.

## PlayByPoint API findings (reverse-engineered)

The target platform (`app.playbypoint.com`) is a Rails/Hotwire app behind Cloudflare, run by a real commercial vendor. It is not publicly documented — everything below was found by inspecting live responses and the site's own JS bundle, not from official docs.

- **Cloudflare blocks plain `requests`/`urllib3`** on TLS fingerprint alone — a bare `requests.get()` gets a `403` "Just a moment..." JS challenge page before ever reaching the app. Switching to **`curl_cffi`** (`requests.Session(impersonate="chrome124")`) gets straight through with no other changes needed. No browser automation (Playwright/claude-in-chrome) required for this site.
- **Login** is standard Rails Devise:
  - `GET /users/sign_in` → scrape `authenticity_token` from the hidden CSRF input.
  - `POST /users/sign_in.html` with `authenticity_token`, `user[email]`, `user[password]`, `user[remember_me]`.
  - Failure shows literal text `"Invalid Email or password."` in the response body.
  - To check if a session is still authenticated: re-GET `/users/sign_in` — Devise redirects an already-logged-in session away from that page, so `"/users/sign_in" not in response.url` means logged in.
- **Clinic/program data**: `GET /programs/{slug}` (e.g. `/programs/beginnerclinic`, `/programs/IntermediateClinic`) server-renders a React component whose props are embedded in the HTML as `data-react-props="{...html-entity-encoded JSON...}"`. Decode with `html.unescape()` then `json.loads()`. This blob contains everything needed to book, with no extra API calls:
  - `clinic_id` — the program's numeric id
  - `prices[]` — each has an `id` (this is the `plan_id` booking needs) and `price`
  - `sessions[]` — every session ever scheduled for this clinic, each with real `id`, `lesson_date` (`YYYY-MM-DD`), `hour_start`/`hour_end` (seconds since midnight), `capacity`, `player_count` (session is open iff `player_count < capacity`)
  - `on_success_url`, `schedulePath`, `userDefaultPaymentMethod`, `daysPriorBooking`, `hours_before_stop_booking`, etc.
- **Saved payment methods**: `GET /api/cards` returns the user's saved cards, e.g. `{"id": 1753810, "type": "card", "brand": "Mastercard", "is_default": false, "exp_month": 9, "exp_year": 30, "last4": "1740", "tech_fee_exempt": false}`. This `id` is PlayByPoint's own internal reference to an already-tokenized card — not a raw PAN, not a Stripe token.
- **Booking submission** (found in `application-*.js`, React method `_bookClinic`) — **confirmed working with a real, successful test booking**:
  - `POST /api/public/clinics/{clinic_id}`, JSON body:
    ```json
    {
      "plan_id": <price id>,
      "user_child_id": null,
      "clinic_lesson_ids": [<session id>],
      "free_passes": [],
      "apply_package_pass_to_lesson_ids": [],
      "payment": {
        "method": "card",
        "card_details": <full object from GET /api/cards for the chosen card>,
        "coupon": {"code": ""},
        "payment_intent_id": null,
        "booking_package_purchase_id": null
      },
      "notes": ""
    }
    ```
  - **`payment.card_details` is simply one of the objects from `GET /api/cards`**, passed through verbatim — the client-side UI just forwards whatever the saved-card list handed it (`onCardSelected(this.props.card)`). No Stripe.js tokenization or PaymentIntent creation needed for a saved card. (The generic `payment_intent_id`/3D-Secure handling in the client JS is a fallback path for *new* cards or step-up authentication, not required for the common saved-card case.)
  - **CSRF**: this JSON `/api/` endpoint still enforces Rails CSRF — a fresh `authenticity_token` (scraped from any page's `<meta name="csrf-token">`) must be sent as the `X-CSRF-Token` request header, not a form field.
  - Success returns `200` with an empty `{}` body — there's no useful confirmation in the response itself. Verify success by re-fetching `/programs/{slug}` and checking `user_clinics` for a new entry with today's `created_at` and `payment.status == "captured"`.
  - There is a separate `/api/clinics/{id}/book_with_bolt` endpoint — this is the **staff/admin** in-person card-reader flow (takes `device_id`, allows `custom_price` overrides), not the customer-facing booking path. Don't confuse the two.
- **Raw court reservations** (as opposed to clinic/program bookings) have not been investigated — the `/api/public/clinics/{id}` flow above is specific to clinics/programs; a plain court-slot reservation likely has a different endpoint under the same app.
- **`program_slug` is just a URL path segment**, not a query param or JSON field — it's substituted directly into `GET /programs/{program_slug}` (see `get_program_page()`). It's the site's human-readable clinic identifier (React-router-friendly, not the numeric `clinic_id`), case-sensitive as PlayByPoint stores it (mixed case for most, all-lowercase for `beginnerclinic`).
- **Discovering all valid slugs**: the `/programs` listing page itself is a React SPA (`ClinicsList` component in `application-*.js`) that renders no slugs server-side — it populates client-side via `GET /api/public/clinics` (optionally filtered with `category`/`search`/`date`/`date_range` query params; the facility's category ids come from the `data-react-props` blob on `/programs` itself, e.g. `facilityCategories: [[1143, "Clinic"], [1145, "Academy"]]`). Calling it with no params returns every clinic across all categories for the logged-in facility, each with a `url` field giving its `/programs/{slug}` path directly — no need to enumerate categories separately. As of 2026-08-04, for facility_id 265 (Tennis Prime Independence Harbor) this returned all 5 current program slugs:
  - `AdvancedBeginner` — Advanced Beginner (Clinic)
  - `beginnerclinic` — Beginner Clinic (Clinic)
  - `IntermediateClinic` — Intermediate Clinic (Clinic) — the current `DEFAULT_PROGRAM_SLUG`
  - `private-lesson-da085204-2cc6-474d-adce-0f477daddab7` — Private Lesson (Academy)
  - `private-group-class` — Semi Private Lesson (Academy)

  This list is a live snapshot, not a fixed enum — programs get added/removed, so re-query `/api/public/clinics` rather than trusting this list to stay current.
- **Safety note**: `book_court()` charges a real card immediately on a successful call, with no dry-run/confirmation step and no idempotency guard against double-booking the same session. `lambda_handler` requires `clinic_id`/`plan_id`/`session_id`/`card_last4` explicitly in the event with no defaults, specifically so an accidental no-args invocation fails loudly instead of silently re-charging.

Implementation lives in `playbypoint/` (a package, not a flat script):
- `playbypoint/models.py` — pydantic models for every PlayByPoint shape we depend on (`SavedCard`, `Payment`, `ClinicBookingRequest`, plus `ProgramData`/`ClinicPrice`/`ClinicSession` for the `/programs/{slug}` props blob). Responses are validated against these on parse, raising a clear error on any shape drift instead of a bare `KeyError`.
- `playbypoint/book_court.py` — `login()`, `is_logged_in()`, `get_saved_cards()`, `find_card_by_last4()`, `book_court(session, clinic_id, plan_id, session_id, card_last4)`, and `lambda_handler`.
