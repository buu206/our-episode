from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import Literal


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class Login(Strict):
    invite_code: str = Field(min_length=1, max_length=200)


class AuthOut(Strict):
    user_id: str
    csrf_token: str


class EpisodeIn(Strict):
    title: str = Field(min_length=1, max_length=120)
    background: str = Field(default='', max_length=4000)


class Segment(Strict):
    asset_id: str = Field(min_length=1, max_length=80)
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    caption: str = Field(default='', max_length=100)

    @model_validator(mode='after')
    def ordered(self):
        if self.end <= self.start:
            raise ValueError('片段结束必须晚于开始')
        return self


class VersionIn(Strict):
    title: str = Field(min_length=1, max_length=120)
    segments: list[Segment] = Field(min_length=1, max_length=30)
    base_version_id: str | None = None
    narration_text: str = Field(default='', max_length=120)
    packaging: Literal['standard', 'light', 'original'] = 'standard'


class VersionOut(Strict):
    id: str
    title: str
    state: str
    origin: str
    segments: list[Segment]
    media_url: str | None
    job_id: str | None
    error: dict | None
    base_version_id: str | None
    created_at: str
    narration_text: str = ''
    narration_approved: bool = False
    narration_ready: bool = False
    packaging: str = 'standard'
    evidence_revision: int | None = None


class AssetOut(Strict):
    id: str
    filename: str
    duration: float
    width: int
    height: int
    has_audio: bool
    size: int
    media_url: str
    source_kind: str = 'original'
    contributor: str | None = None
    interview_text: str | None = None
    fixed_segment: dict | None = None


class JobOut(Strict):
    id: str
    kind: str
    state: str
    stage: str
    error: dict | None
    version_id: str | None
    result: dict | None


class QuotaOut(Strict):
    original_count: int = Field(ge=0)
    total_seconds: float = Field(ge=0)
    total_bytes: int = Field(ge=0)
    max_count: int = 8
    max_seconds: float = 600
    max_bytes: int = 1073741824


class PendingCleanupOut(Strict):
    asset_id: str
    filename: str


class EpisodeOut(Strict):
    id: str
    title: str
    background: str
    created_at: str
    assets: list[AssetOut]
    versions: list[VersionOut]
    jobs: list[JobOut]
    directions: list[dict]
    budget: dict
    quota: QuotaOut
    pending_cleanup: list[PendingCleanupOut] = Field(default_factory=list)
    evidence: list[dict] = Field(default_factory=list)
    evidence_revision: int = 0
    evidence_confirmed_revision: int | None = None
    analysis_state: str = 'empty'
    analysis_notes: list[str] = Field(default_factory=list)
    analysis_coverage: list[dict] = Field(default_factory=list)
    directions_revision: int | None = None
    selected_direction: str | None = None


class EpisodesOut(Strict):
    items: list[EpisodeOut]


class StatusOut(Strict):
    cloud_enabled: bool
    cloud_reason: str
    media_ready: bool
    stage: str
    workflow_enabled: bool = False
    model_mode: str = 'cloud'


class Ok(Strict):
    ok: bool = True


class ShareImpact(Strict):
    id: str
    label: str
    version_id: str
    state: str


class ImpactOut(Strict):
    asset_id: str
    versions: list[str]
    shares: list[ShareImpact] = Field(default_factory=list)
    message: str


class EvidenceItem(Strict):
    id: str = Field(min_length=1, max_length=80)
    asset_id: str = Field(min_length=1, max_length=80)
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    note: str = Field(min_length=1, max_length=2000)
    kind: Literal['visual', 'speech']

    @model_validator(mode='after')
    def check(self):
        if self.end <= self.start or not self.note.strip():
            raise ValueError('证据区间与文字不能为空')
        return self


class EvidenceReview(Strict):
    revision: int = Field(ge=0)
    items: list[EvidenceItem] = Field(max_length=100)
    confirmed: bool


class DirectionSelect(Strict):
    direction_id: str = Field(min_length=1, max_length=80)
    revision: int = Field(ge=0)
    packaging: Literal['standard', 'light', 'original'] = 'standard'


class NarrationConfirm(Strict):
    confirm_text: bool
