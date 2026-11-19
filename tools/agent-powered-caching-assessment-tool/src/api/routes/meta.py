"""Routes that describe the server itself."""

from fastapi import APIRouter

from src.api.capabilities import build_capabilities
from src.contracts.api_models import Capabilities

router = APIRouter()


@router.get("/capabilities", response_model=Capabilities)
def get_capabilities() -> Capabilities:
    """What the server can do and the values its inputs accept.

    A UX reads this once to draw the submit form and the what-if sliders without
    hardcoding anything. It makes no AWS call, so it says nothing about whether the
    model, the Pricing API or a database is reachable. Use preflight for the database.
    """
    return build_capabilities()
