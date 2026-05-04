"""Thread portability, attachment validation, and control API schemas."""

from pydantic import BaseModel, Field

from .chat import FileData


class AttachmentValidationRequest(BaseModel):
    """Request model for attachment preflight validation."""

    attachments: list[FileData] = Field(
        default_factory=list,
        description="Attachments to validate against the effective thread model",
    )


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
