from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import Literal
from .services.editing import EditSegment, validate_transitions


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


DirectorFocus = Literal['auto', 'conversation', 'highlights', 'warm']


class DirectorMessageOut(Strict):
    id: str
    role: Literal['user', 'director']
    text: str
    created_at: str


class DirectorState(Strict):
    revision: int = Field(ge=0)
    focus: DirectorFocus | None
    notes: str
    messages: list[DirectorMessageOut]
    directions_current: bool


class DirectorMessageIn(Strict):
    revision: int = Field(ge=0)
    client_id: str = Field(min_length=8, max_length=80)
    text: str | None = Field(default=None, min_length=1, max_length=1000)
    choice: DirectorFocus | None = None

    @model_validator(mode='after')
    def one_message(self):
        if (self.text is None) == (self.choice is None):
            raise ValueError('请提交一条补充要求或一个选择')
        if self.text is not None and not self.text.strip():
            raise ValueError('补充要求不能为空')
        return self


class DirectorChatMessage(Strict):
    id: str
    role: Literal['user', 'assistant']
    text: str
    created_at: str
    source: Literal['user', 'model', 'demo']
    client_id: str | None = None


class DirectorChatProposal(Strict):
    id: str
    message_id: str
    kind: Literal['revise_directions', 'revise_narration', 'navigate']
    title: str
    explanation: str
    status: Literal['pending', 'applied', 'stale']
    requirement: str | None = None
    narration_text: str | None = None
    target_step: Literal['materials', 'direction', 'edit', 'preview'] | None = None


class DirectorChatState(Strict):
    messages: list[DirectorChatMessage]
    proposals: list[DirectorChatProposal]
    pending_job_id: str | None
    last_job_id: str | None
    last_error: dict | None
    can_chat: bool
    reason: str
    mode: Literal['cloud', 'demo']


class DirectorChatIn(Strict):
    client_id: str = Field(min_length=8, max_length=80)
    text: str = Field(min_length=1, max_length=2000)
    version_id: str | None = Field(default=None, pattern=r'^[a-f0-9]{32}$')
    step: Literal['home', 'materials', 'direction', 'edit', 'preview', 'collaboration', 'sharing'] = 'materials'
    confirm_send: bool = False


class DirectorActionIn(Strict):
    confirm: bool = False


class DirectorActionOut(Strict):
    ok: bool
    next_step: Literal['materials', 'direction', 'edit', 'preview'] | None = None
    version_id: str | None = None
    job_id: str | None = None


class Segment(EditSegment):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False, strict=False)
    # Legacy manual versions permit sub-0.1s spans; the new director plan does not.
    minimum_duration = 0.0
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
    packaging: Literal['standard', 'light', 'original', 'variety', 'variety_v2'] = 'standard'

    @model_validator(mode='after')
    def valid_transitions(self):
        validate_transitions(self.segments)
        if any(s.editorial for s in self.segments):
            if self.packaging != 'variety_v2':
                raise ValueError('编导版式需使用综艺节奏包装。')
            from .services.editorial_plan import compile_editorial
            compile_editorial([s.model_dump() for s in self.segments])
        if any(s.visual_beats for s in self.segments) and self.packaging != 'variety_v2':
            raise ValueError('人物与物件视觉强调需使用综艺节奏包装。')
        if sum(len(s.visual_beats) for s in self.segments) > 12:
            raise ValueError('一期最多12处视觉强调，请保留画面呼吸空间。')
        if any(s.visual_beats for s in self.segments):
            from .services.visual_beats import plan
            plan([s.model_dump() for s in self.segments])
        return self


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


class SubtitleRowOut(Strict):
    cue_id: str
    segment_index: int
    asset_id: str
    start: float
    end: float
    text: str
    original_text: str
    source_text: str
    hidden: bool
    reviewed: bool
    locked: bool
    flags: list[str]
    media_url: str


class SubtitleTrackOut(Strict):
    version_id: str
    rows: list[SubtitleRowOut]


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


class RetentionOut(Strict):
    expires_at: float | None
    deletion_requested: bool
    cleanup_pending: bool
    counts: dict[str, int]


class WorkflowAccessOut(Strict):
    enabled: bool
    reason: str
    analysis_cached: bool = False


class EpisodeOut(Strict):
    id: str
    title: str
    background: str
    created_at: str
    retention: RetentionOut
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
    director: DirectorState | None = None
    workflow: WorkflowAccessOut | None = None


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
    confirmation_mode: Literal['detailed', 'overview'] = 'detailed'


class DirectionSelect(Strict):
    direction_id: str = Field(min_length=1, max_length=80)
    revision: int = Field(ge=0)
    brief_revision: int | None = Field(default=None, ge=0)
    packaging: Literal['standard', 'light', 'original', 'variety', 'variety_v2'] = 'standard'
    auto_render_original: bool = False
    client_id: str | None = Field(default=None, pattern=r'^[a-zA-Z0-9_-]{16,80}$')


class NarrationConfirm(Strict):
    confirm_text: bool
