"""Pydantic models for PlayByPoint's (undocumented) API shapes.

These validate data we parse out of PlayByPoint's responses, and the
payloads we build to send back, so a shape mismatch raises a clear
validation error immediately instead of a confusing KeyError/TypeError
deep inside booking logic.
"""

from datetime import date as date_type

from pydantic import BaseModel, field_validator

DEFAULT_PROGRAM_SLUG = "IntermediateClinic"
DEFAULT_CARD_LAST4 = "1740"


class ClinicPrice(BaseModel):
    id: int
    price: float
    lessons: int
    lesson_unit: str
    hidden: bool


class ClinicSession(BaseModel):
    id: int
    clinic_id: int
    lesson_date: str
    hour_start: int
    hour_end: int
    capacity: int
    player_count: int
    status: str


class ProgramData(BaseModel):
    """Parsed from the `data-react-props` blob on GET /programs/{slug}."""

    clinic_id: int
    clinic_name: str
    prices: list[ClinicPrice]
    sessions: list[ClinicSession]
    on_success_url: str
    schedulePath: str
    userDefaultPaymentMethod: str


class Coupon(BaseModel):
    code: str = ""


class SavedCard(BaseModel):
    """A saved payment method, as returned by GET /api/cards."""

    id: int
    type: str
    brand: str
    is_default: bool
    exp_month: int
    exp_year: int
    last4: str
    tech_fee_exempt: bool


class Payment(BaseModel):
    method: str
    card_details: SavedCard | None = None
    coupon: Coupon = Coupon()
    payment_intent_id: str | None = None
    booking_package_purchase_id: int | None = None


class ClinicBookingRequest(BaseModel):
    """Body for POST /api/public/clinics/{clinic_id} (the `_bookClinic` request)."""

    plan_id: int
    user_child_id: int | None = None
    clinic_lesson_ids: list[int]
    free_passes: list = []
    apply_package_pass_to_lesson_ids: list = []
    payment: Payment
    notes: str = ""


class BookingEvent(BaseModel):
    """Input shape for lambda_handler. `date` is deliberately required with
    no default -- this triggers a real charge, so there's no sensible
    fallback for "which day did you mean?"."""

    date: date_type
    program_slug: str = DEFAULT_PROGRAM_SLUG
    card_last4: str = DEFAULT_CARD_LAST4

    @field_validator("card_last4")
    @classmethod
    def _validate_last4(cls, v: str) -> str:
        if not v.isdigit() or len(v) != 4:
            raise ValueError("card_last4 must be exactly 4 digits")
        return v
