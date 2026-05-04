"""Thread portability, attachment validation, and control API schemas."""

from pydantic import BaseModel, Field


class FileData(BaseModel):
    """Generic file attachment data for multimodal messages."""

    file_type: str = Field(..., description="File type: 'image' or 'document'")
    data_url: str = Field(..., description="Base64 data URL (data:mime/type;base64,...)")
    mime_type: str = Field(..., description="MIME type (image/jpeg, application/pdf, etc.)")
    file_name: str | None = Field(
        default=None,
        description="Original filename (used for MIME fallback when browser MIME type is missing)",
    )


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
