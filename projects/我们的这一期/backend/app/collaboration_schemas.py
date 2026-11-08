from typing import Literal
import unicodedata
from pydantic import Field, field_validator, model_validator
from .schemas import Strict


def literal_text(value):
    if any(unicodedata.category(character) in ("Cc", "Cf") for character in value):
        raise ValueError("可烧录的称呼或表达不能包含控制字符")
    return value


class ContextSpan(Strict):
    asset_id: str = Field(min_length=1, max_length=80)
    start: float = Field(ge=0)
    end: float = Field(gt=0)

    @model_validator(mode="after")
    def interval(self):
        if not 0 < self.end - self.start <= 30:
            raise ValueError("上下文必须在30秒内")
        return self


class InvitationIn(Strict):
    role: Literal["contribute", "interview", "review"]
    label: str = Field(min_length=1, max_length=40)
    context: ContextSpan | None = None
    questions: list[str] = Field(default_factory=list, max_length=8)

    _label = field_validator("label")(literal_text)

    @model_validator(mode="after")
    def valid(self):
        if self.role in ("interview", "review") and not self.context:
            raise ValueError("采访和核对需要明确上下文")
        if any(not text.strip() or len(text) > 200 for text in self.questions):
            raise ValueError("问题不能为空或超过200字")
        return self


class ParticipantLogin(Strict):
    invite_code: str = Field(min_length=1, max_length=200)
    display_name: str = Field(min_length=1, max_length=40)

    _name = field_validator("display_name")(literal_text)


class Question(Strict):
    id: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=200)
    skipped: bool = False


class QuestionsIn(Strict):
    questions: list[Question] = Field(max_length=8)


class DraftIn(Strict):
    text: str = Field(default="", max_length=100)
    question_id: str | None = Field(default=None, max_length=80)

    _text = field_validator("text")(literal_text)


class TextSubmissionIn(DraftIn):
    client_request_id: str = Field(min_length=1, max_length=80)


class ConfirmIn(Strict):
    confirmed: Literal[True]


class SubmissionConfirmIn(ConfirmIn):
    # Optional only for compatibility with the earlier local acceptance client.
    # The formal participant UI supplies the exact draft it displayed.
    expected_draft: DraftIn | None = None


class RevisionIn(Strict):
    text: str = Field(default="", max_length=100)
    start: float = Field(default=0, ge=0)
    end: float = Field(default=0, ge=0)

    _text = field_validator("text")(literal_text)


class RevisionDecision(Strict):
    decision: Literal["confirmed", "rejected"]


class ObjectionIn(Strict):
    submission_id: str | None = Field(default=None, max_length=80)
    start: float | None = Field(default=None, ge=0)
    end: float | None = Field(default=None, gt=0)
    reason: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def interval(self):
        if (self.start is None) != (self.end is None):
            raise ValueError("区间必须完整")
        if self.start is not None and self.end <= self.start:
            raise ValueError("区间无效")
        return self


class ParticipantAuthOut(Strict):
    participant_id: str
    invite_id: str
    display_name: str
    csrf_token: str


class InvitationOut(Strict):
    id: str
    role: Literal["contribute", "interview", "review"]
    label: str
    expires_at: float
    closed: bool
    accepting_new: bool
    context: ContextSpan | None
    questions: list[Question]
    context_job_id: str | None
    context_state: str
    context_error: dict | None
    context_preview_url: str | None
    created_at: str


class InvitationCreatedOut(InvitationOut):
    invite_code: str


class InvitationsOut(Strict):
    items: list[InvitationOut]


class RevisionOut(Strict):
    id: str
    submission_id: str
    text: str
    start: float
    end: float
    state: Literal["proposed", "confirmed", "rejected", "withdrawn"]
    created_at: str


class AdoptionOut(Strict):
    id: str
    revision_id: str
    state: Literal["pending", "ready", "revoked", "failed"]
    asset_id: str | None
    job_id: str | None
    error: dict | None


class SubmissionOut(Strict):
    id: str
    invite_id: str
    participant_id: str
    display_name: str
    kind: Literal["text", "audio", "video"]
    state: Literal["uploading", "draft", "submitted", "withdrawn", "failed"]
    text: str
    question_id: str | None
    filename: str | None
    duration: float
    size: int
    preview_state: str
    preview_job_id: str | None
    preview_error: dict | None
    media_url: str | None
    created_at: str
    revisions: list[RevisionOut]
    adoptions: list[AdoptionOut]
    can_delete_draft: bool
    deleted: bool
    deletion_pending: bool


class SubmissionsOut(Strict):
    items: list[SubmissionOut]


class ObjectionOut(Strict):
    id: str
    invite_id: str
    participant_id: str
    display_name: str
    submission_id: str | None
    asset_id: str | None
    start: float | None
    end: float | None
    reason: str
    state: Literal["open", "resolved"]
    created_at: str
    resolved_at: str | None


class ObjectionsOut(Strict):
    items: list[ObjectionOut]
