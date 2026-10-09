"""Scoped immutable byte artifacts; PostgreSQL stores only their keys and digests."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from uuid import UUID

from ragos.core.errors import ConflictError, NotFoundError, ValidationError
from ragos.core.tenant import TenantContext


def require_artifact_ref(value: str, tenant_id: UUID) -> None:
    prefix = f"tenant/{tenant_id}/"
    if (
        not isinstance(value, str)
        or not value.startswith(prefix)
        or len(value) > 1024
        or any(char in value for char in ("%", "\\", "?", "#", "\x00"))
        or any(ord(char) < 32 for char in value)
        or ":" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or str(PurePosixPath(value)) != value
        or len(value) == len(prefix)
    ):
        raise ValidationError("artifact reference must be a canonical tenant storage key")


def canonical_json(value) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValidationError("artifact must contain finite JSON values") from exc


class ArtifactStore:
    def __init__(self, root: Path | str, *, max_bytes: int = 64 * 1024 * 1024) -> None:
        if isinstance(max_bytes, bool) or max_bytes <= 0:
            raise ValidationError("artifact byte budget must be positive")
        self.root = Path(root).resolve()
        self.max_bytes = max_bytes

    @contextmanager
    def _directory(self, tenant_ctx: TenantContext, reference: str, *, create: bool):
        tenant_ctx.require_scope_key()
        require_artifact_ref(reference, tenant_ctx.tenant_id)
        if create:
            # Configured root is trusted; all tenant-relative traversal uses descriptor anchors.
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptors = []
        try:
            descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            descriptors.append(descriptor)
            parts = PurePosixPath(reference).parts
            for part in parts[:-1]:
                if create:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=descriptor)
                        os.fsync(descriptor)
                    except FileExistsError:
                        pass
                descriptor = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
                )
                descriptors.append(descriptor)
            yield descriptor, parts[-1]
        except FileNotFoundError as exc:
            raise NotFoundError("immutable artifact is unavailable") from exc
        except OSError as exc:
            raise ValidationError("artifact path is not a safe tenant directory") from exc
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def _read_at(self, directory: int, name: str, digest: str) -> bytes:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(descriptor, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ValidationError("artifact must be a regular file")
            data = handle.read(self.max_bytes + 1)
        if len(data) > self.max_bytes:
            raise ValidationError("artifact exceeds its byte budget")
        if hashlib.sha256(data).hexdigest() != digest:
            raise ConflictError("immutable artifact digest does not match")
        return data

    def read_bytes(self, *, tenant_ctx: TenantContext, reference: str, digest: str) -> bytes:
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValidationError("artifact requires a lowercase SHA-256 digest")
        with self._directory(tenant_ctx, reference, create=False) as (directory, name):
            return self._read_at(directory, name, digest)

    def put_bytes(self, *, tenant_ctx: TenantContext, kind: str, data: bytes) -> tuple[str, str]:
        if not isinstance(data, bytes) or len(data) > self.max_bytes:
            raise ValidationError("artifact exceeds its byte budget or is not bytes")
        if re.fullmatch(r"[a-z][a-z0-9-]{0,63}", kind) is None:
            raise ValidationError("unknown artifact kind")
        digest = hashlib.sha256(data).hexdigest()
        reference = f"tenant/{tenant_ctx.tenant_id}/{kind}/{digest}.bin"
        with self._directory(tenant_ctx, reference, create=True) as (directory, name):
            temporary = ".pending-" + os.urandom(16).hex()
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.link(
                        temporary,
                        name,
                        src_dir_fd=directory,
                        dst_dir_fd=directory,
                        follow_symlinks=False,
                    )
                except FileExistsError:
                    if self._read_at(directory, name, digest) != data:
                        raise ConflictError("artifact address is already occupied") from None
                os.fsync(directory)
            finally:
                # Remove only this operation's temporary file, using the same anchored directory.
                os.unlink(temporary, dir_fd=directory)
        return reference, digest

    def read_json(self, *, tenant_ctx: TenantContext, reference: str, digest: str):
        data = self.read_bytes(tenant_ctx=tenant_ctx, reference=reference, digest=digest)
        try:

            def reject_nonfinite(value):
                raise ValueError("non-finite JSON value")

            return json.loads(data, parse_constant=reject_nonfinite)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValidationError("artifact is not valid JSON") from exc
