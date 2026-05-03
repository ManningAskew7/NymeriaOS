"""System and health-check routes."""

from fastapi import APIRouter

from ..schemas.system import HealthResponse

router = APIRouter(tags=["System"])


@router.get("/health", response_model=HealthResponse)
async def health_check():
    """Health check endpoint."""
    return HealthResponse()
