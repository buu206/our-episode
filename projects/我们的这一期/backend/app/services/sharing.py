"""Durable, fixed-version viewing authorization. No cloud or external delivery."""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time

from sqlalchemy import select

from ..models import Version, now
from ..repository import DomainError
from ..sharing_models import Share, ShareOperation


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def request_fingerprint(kind, target, body):
    return digest(json.dumps([kind, target, body], ensure_ascii=False, sort_keys=True, separators=(",", ":")))


@dataclass
class MediaHandle:
    fd: int
    size: int
    sha256: str

    def close(self):
        if self.fd >= 0:
            fd, self.fd = self.fd, -1
            os.close(fd)

    def __del__(self):
        try:
            self.close()
        except OSError:
            pass


def open_media(repo, relative):
    """Resolve only a generated output through directory FDs, never symlinks.

    The same opened descriptor is hashed and eventually streamed. Replacing the
    pathname with a symlink after validation therefore cannot change the bytes.
    """
    if not isinstance(relative, str) or not re.fullmatch(r"outputs/[0-9a-f]{32}\.mp4", relative):
        raise DomainError("SHARE_MEDIA_UNAVAILABLE", "成片副本不可用于分享。", 409)
    root_fd = directory_fd = fd = -1
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        root_fd = os.open(repo.data_dir, flags)
        directory_fd = os.open("outputs", flags, dir_fd=root_fd)
        # Reject FIFOs/device files after opening without ever blocking on them.
        fd = os.open(Path(relative).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size <= 0:
            raise OSError("not a regular output")
        checksum, remaining = hashlib.sha256(), before.st_size
        while remaining:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                raise OSError("output changed during inspection")
            checksum.update(chunk)
            remaining -= len(chunk)
        after = os.fstat(fd)
        if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
            raise OSError("output changed during inspection")
        os.lseek(fd, 0, os.SEEK_SET)
        result = MediaHandle(fd, before.st_size, checksum.hexdigest())
        fd = -1
        return result
    except OSError:
        raise DomainError("SHARE_MEDIA_UNAVAILABLE", "成片副本缺失、已变化或不是受控普通文件。", 409) from None
    finally:
        for descriptor in (fd, directory_fd, root_fd):
            if descriptor >= 0:
                os.close(descriptor)


def candidate_snapshot(repo, db, version):
    if version.state != "confirmed" or not version.confirmed_at or not version.output_path:
        raise DomainError("VERSION_NOT_CONFIRMED", "请先预览并确认要分享的具体版本。", 409)
    # Runtime import avoids a cycle: worker/collaboration lifecycle hooks import
    # suspend_version_shares, while their existing source guard remains canonical.
    from ..worker import source_segments
    source_segments(repo, db, version)
    handle = open_media(repo, version.output_path)
    content = {key: getattr(version, key) for key in (
        "id", "owner_id", "episode_id", "title", "origin", "segments", "output_path", "confirmed_at",
        "evidence_revision", "direction_id", "narration_text", "narration_approved", "narration_path", "packaging")}
    content.update(media_sha256=handle.sha256, media_size=handle.size)
    fingerprint = digest(json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return {"version_id": version.id, "title": version.title, "output_path": version.output_path,
            "confirmed_at": version.confirmed_at, "fingerprint": fingerprint,
            "media_sha256": handle.sha256, "media_size": handle.size}, handle


def restrict_share(share, reason="CONTENT_CHANGED"):
    if share.state == "active":
        share.state, share.updated_at = "restricted", now()
        share.restricted_reason = {"code": reason,
            "message": "关联内容或确认已变化；需要发起人重新核对并明确更新分享。"}


def suspend_version_shares(db, version_ids, reason="CONTENT_CHANGED"):
    """Call inside the transaction that invalidates the referenced versions."""
    ids = [version_ids] if isinstance(version_ids, str) else list(version_ids)
    if not ids:
        return 0
    changed = 0
    for share in db.scalars(select(Share).where(Share.version_id.in_(ids), Share.state == "active")):
        restrict_share(share, reason)
        changed += 1
    return changed


def touch_expiry(share):
    if share.state not in ("closed", "expired") and share.expires_at <= time.time():
        share.state, share.updated_at = "expired", now()


def state_error(share):
    if share.state == "closed":
        return DomainError("SHARE_CLOSED", "该分享已关闭，需要发起人另建分享。", 410)
    if share.state == "expired":
        return DomainError("SHARE_EXPIRED", "该分享已过期，需要发起人另建分享。", 410)
    if share.state == "restricted":
        return DomainError("SHARE_RESTRICTED", "该分享因内容或确认变化暂停，等待发起人重新核对。", 409)
    return None


def inspect_share(db, repo, share, *, expected_revision=None, keep_media=False):
    """Return a fault rather than raising, so callers commit security changes.

    Every API caller must exit its transaction before raising the returned error.
    An invalid snapshot is a durable latch, never an automatic temporary check.
    """
    touch_expiry(share)
    fault = state_error(share)
    if fault:
        return None, fault
    version = db.get(Version, share.version_id)
    if not version or version.owner_id != share.owner_id or version.episode_id != share.episode_id:
        restrict_share(share, "SOURCE_REMOVED")
        return None, state_error(share)
    try:
        snapshot, handle = candidate_snapshot(repo, db, version)
    except DomainError as exc:
        restrict_share(share, exc.code)
        return None, state_error(share)
    if any(snapshot[key] != getattr(share, key) for key in (
            "version_id", "title", "output_path", "confirmed_at", "fingerprint", "media_sha256", "media_size")):
        handle.close()
        restrict_share(share, "SNAPSHOT_CHANGED")
        return None, state_error(share)
    if expected_revision is not None and expected_revision != share.revision:
        handle.close()
        return None, DomainError("SHARE_REVISION_CHANGED", "分享已明确更新，旧媒体地址已失效，请刷新观看页。", 409)
    if not keep_media:
        handle.close()
        handle = None
    return handle, None


def share_out(share):
    return {**{key: getattr(share, key) for key in ("id", "episode_id", "version_id", "label", "title", "state",
        "revision", "created_at", "updated_at", "expires_at", "closed_at", "restricted_reason", "confirmed_at")},
        "watch_path": "/watch/" + share.id}


def find_operation(db, owner_id, client_request_id, kind, fingerprint):
    row = db.scalars(select(ShareOperation).where(ShareOperation.owner_id == owner_id,
                     ShareOperation.client_request_id == str(client_request_id))).first()
    if row and (row.kind != kind or row.request_hash != fingerprint):
        raise DomainError("IDEMPOTENCY_CONFLICT", "此请求编号已经用于不同的分享操作。", 409)
    return row


def record_operation(db, owner_id, client_request_id, kind, fingerprint, share, result):
    db.add(ShareOperation(owner_id=owner_id, client_request_id=str(client_request_id), kind=kind,
                         request_hash=fingerprint, share_id=share.id, result=result))


def byte_range(value, size):
    if value is None:
        return 0, size - 1, 200
    if len(value) > 100:
        raise DomainError("INVALID_RANGE", "字节区间无效。", 416)
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value.strip())
    if not match or not any(match.groups()):
        raise DomainError("INVALID_RANGE", "仅支持一个有效的字节区间。", 416)
    left, right = match.groups()
    if not left:
        suffix = int(right)
        if suffix <= 0:
            raise DomainError("INVALID_RANGE", "字节区间无效。", 416)
        start, end = max(0, size - suffix), size - 1
    else:
        start, end = int(left), int(right) if right else size - 1
        if start >= size or end < start:
            raise DomainError("INVALID_RANGE", "字节区间超出成片范围。", 416)
        end = min(end, size - 1)
    return start, end, 206


def stream_is_active(repo, share_id, revision):
    with repo.factory() as db:
        share = db.get(Share, share_id)
        return bool(share and share.state == "active" and share.revision == revision and share.expires_at > time.time())


def media_chunks(repo, share_id, revision, handle, start, end):
    try:
        os.lseek(handle.fd, start, os.SEEK_SET)
        remaining = end - start + 1
        while remaining and stream_is_active(repo, share_id, revision):
            chunk = os.read(handle.fd, min(64 * 1024, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
    finally:
        handle.close()
