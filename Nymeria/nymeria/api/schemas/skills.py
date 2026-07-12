"""Agent Skills API schemas."""

from typing import List, Optional

from pydantic import BaseModel, Field


class ThreadTemplateSummary(BaseModel):
    name: str
    description: str


class SkillMetadataResponse(BaseModel):
    name: str
    description: str
    scope: str
    allowed_tools: List[str] = Field(default_factory=list)
    required_tools: List[str] = Field(default_factory=list)
    required_skills: List[str] = Field(default_factory=list)
    thread_templates: List[ThreadTemplateSummary] = Field(default_factory=list)
    tool_ttl: str = "2h"
    is_skill_kit: bool = False
    default_active: bool = False
    has_scripts: bool = False
    has_references: bool = False
    has_assets: bool = False


class SkillDetailResponse(SkillMetadataResponse):
    body: str
    path: str
    license: Optional[str] = None
    scripts: List[str] = Field(default_factory=list)
    references: List[str] = Field(default_factory=list)


class SkillInstallRequest(BaseModel):
    name: str
    source: str = Field(
        "anthropic",
        description="Marketplace source: 'anthropic' (Phase 1).",
    )
    scope: str = Field("user", description="Install scope: 'user' or 'global'.")


class GlobalSkillsUpdateRequest(BaseModel):
    skill_names: List[str]
