"""Thread portability, attachment validation, and control API schemas."""

from typing import Optional

from pydantic import BaseModel, Field

from .chat import FileData


class AttachmentValidationRequest(BaseModel):
    """Request model for attachment preflight validation."""

    attachments: list[FileData] = Field(
        default_factory=list,
        description="Attachments to validate against the effective thread model",
    )


class AttachmentLimits(BaseModel):
    """Per-model attachment hard caps surfaced to the frontend.

    None means "no published limit for this model family"; the frontend
    should treat it as effectively uncapped (but still honor its own absolute
    ceilings on file size, e.g. 20 MB per document).
    """

    max_images_per_request: Optional[int] = None
    max_image_bytes: Optional[int] = None
    # Long edge in pixels. Unlike the others this one is never None in practice
    # (the table always answers), and it is the cap a client cannot infer from
    # file size: an image over it fails the whole REQUEST at the provider.
    max_image_dimension: Optional[int] = None
    max_pdf_pages: Optional[int] = None
    max_total_bytes: Optional[int] = None


class AttachmentValidationResponse(BaseModel):
    """Response model for attachment preflight validation."""

    compatible: bool
    effective_provider: str
    effective_model: str
    model_input_modalities: list[str]
    required_modalities: list[str]
    unsupported_modalities: list[str]
    warnings: list[str]
    can_force_send: bool = True
    limits: AttachmentLimits = Field(default_factory=AttachmentLimits)


class AttachmentLimitsResponse(BaseModel):
    """Response for the standalone limits-lookup endpoint."""

    effective_provider: str
    effective_model: str
    limits: AttachmentLimits


class ThreadRewindRequest(BaseModel):
    """Request model for rewinding thread state by count or by message id."""

    steps: int = Field(
        default=1,
        ge=1,
        le=100,
        description="Number of trailing exchanges to remove from thread state.",
    )
    to_message_id: str | None = Field(
        default=None,
        description=(
            "LangGraph id of the user message to rewind to (inclusive): that "
            "message and everything after it are removed. Takes precedence "
            "over steps when set; 404 when the id is not a user message in "
            "thread state."
        ),
    )


class ThreadRewindResponse(BaseModel):
    """Response model for the rewind endpoint."""

    status: str
    thread_id: str
    steps: int
    removed: int
