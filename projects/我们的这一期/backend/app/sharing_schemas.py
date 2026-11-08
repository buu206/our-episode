from typing import Literal
from uuid import UUID
from pydantic import Field, field_validator
from .schemas import Strict


class ShareCreate(Strict):
    label: str = Field(min_length=1, max_length=40)
    expires_days: Literal[1, 3, 7] = 7
    confirmed: Literal[True]
    client_request_id: UUID

    @field_validator("label")
    @classmethod
    def label_not_blank(cls, value):
        if not value.strip():
            raise ValueError("观看称呼不能为空")
        return value.strip()


class ShareUpdate(Strict):
    version_id: str = Field(min_length=1, max_length=80)
    expected_revision: int = Field(ge=1)
    confirmed: Literal[True]
    client_request_id: UUID


class ShareClose(Strict):
    expected_revision: int = Field(ge=1)
    confirmed: Literal[True]


class RestrictedReason(Strict):
    code: str
    message: str


class ShareOut(Strict):
    id: str
    episode_id: str
    version_id: str
    label: str
    title: str
    state: Literal["active", "restricted", "closed", "expired"]
    revision: int
    created_at: str
    updated_at: str
    expires_at: float
    closed_at: float | None
    restricted_reason: RestrictedReason | None
    confirmed_at: str
    watch_path: str


class ShareCreatedOut(ShareOut):
    share_code: str | None
    watch_url: str | None


class SharesOut(Strict):
    items: list[ShareOut]


class ViewerLogin(Strict):
    share_id: str = Field(min_length=1, max_length=80)
    share_code: str = Field(min_length=1, max_length=200)


class ViewerAuthOut(Strict):
    share_id: str
    label: str
    csrf_token: str


class ViewerShareOut(Strict):
    id: str
    label: str
    title: str
    version_id: str
    revision: int
    expires_at: float
    confirmed_at: str
    media_url: str
