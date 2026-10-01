"""Private, content-addressed storage for generated resume PDFs.

Only a managed relative locator is persisted by the resume core. Absolute paths
never leave this repository, and callers must supply the independently persisted
SHA-256 when reading. Real-application and synthetic-research artifacts are stored
under disjoint roots so a locator cannot silently cross the trust boundary.

All traversal below the configured root uses directory descriptors and no-follow
opens. A private cross-process lock serializes publication, while a hard-link publish
acts as a portable no-clobber operation for immutable content addresses.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import re
import secrets
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, Optional, Tuple, Union


PDF_CONTENT_TYPE = "application/pdf"
MAX_PDF_BYTES = 20 * 1024 * 1024
_DIRECTORY_MODE = 0o700
_FILE_MODE = 0o600
_LOCK_NAME = ".write.lock"
_LOCATOR = re.compile(
    r"^(real|research)/([0-9a-f]{2})/([0-9a-f]{2})/([0-9a-f]{64})\.pdf$"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _directory_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _file_flags(base: int) -> int:
    flags = base
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


class ArtifactRepositoryError(RuntimeError):
    """Safe base error which never includes a filesystem path."""


class InvalidArtifactError(ValueError):
    """Artifact bytes, namespace, locator, or digest are invalid."""


class ArtifactNotFoundError(ArtifactRepositoryError):
    """A valid managed artifact reference has no stored file."""


class ArtifactIntegrityError(ArtifactRepositoryError):
    """Stored bytes do not match their content address."""


class ArtifactSecurityError(ArtifactRepositoryError):
    """A managed entry is a symlink, nonregular file, or not owner-only."""


class ArtifactNamespace(str, Enum):
    """The two physically separated artifact trust domains."""

    REAL = "real"
    RESEARCH = "research"


@dataclass(frozen=True)
class ManagedPdfWrite:
    """Persistence-only result; contains no absolute filesystem path."""

    managed_relative_path: str
    sha256: str
    size_bytes: int
    namespace: ArtifactNamespace
    content_type: str = PDF_CONTENT_TYPE

    @property
    def pdf_sha256(self) -> str:
        """Alias matching the resume core's artifact contract."""

        return self.sha256


@dataclass(frozen=True)
class VerifiedResumePdf:
    """Pathless bytes returned only after descriptor and digest verification."""

    content: bytes
    sha256: str
    size_bytes: int
    namespace: ArtifactNamespace
    content_type: str = PDF_CONTENT_TYPE


class ResumePdfArtifactRepository:
    """Store immutable PDFs beneath private ``real/`` and ``research/`` roots."""

    __slots__ = ("_root", "_root_identity")

    def __init__(self, root: Union[str, os.PathLike[str]]) -> None:
        try:
            supplied = Path(root).expanduser()
            if not supplied.is_absolute():
                supplied = Path.cwd() / supplied
            # abspath is lexical: unlike resolve(), it does not follow a malicious
            # repository-root symlink before we can reject it with lstat().
            self._root = Path(os.path.abspath(os.fspath(supplied)))
        except (OSError, TypeError, ValueError):
            raise InvalidArtifactError("artifact repository root is invalid") from None
        self._root_identity: Optional[Tuple[int, int]] = None
        self._prepare_root()
        root_descriptor = self._open_root()
        real_descriptor = research_descriptor = -1
        try:
            root_info = os.fstat(root_descriptor)
            self._root_identity = (root_info.st_dev, root_info.st_ino)
            self._assert_root_descriptor(root_descriptor)
            real_descriptor = self._ensure_directory_at(
                root_descriptor, ArtifactNamespace.REAL.value, repair_mode=True
            )
            research_descriptor = self._ensure_directory_at(
                root_descriptor, ArtifactNamespace.RESEARCH.value, repair_mode=True
            )
            real_info = os.fstat(real_descriptor)
            research_info = os.fstat(research_descriptor)
            if (real_info.st_dev, real_info.st_ino) == (
                research_info.st_dev,
                research_info.st_ino,
            ):
                raise ArtifactSecurityError("artifact namespaces are not distinct")
            self._prepare_lock_file(root_descriptor)
            self._sync_directory(root_descriptor)
            self._assert_root_descriptor(root_descriptor)
        finally:
            self._close(real_descriptor)
            self._close(research_descriptor)
            self._close(root_descriptor)

    def __repr__(self) -> str:
        return "ResumePdfArtifactRepository(<private>)"

    def write_pdf(
        self,
        content: Union[bytes, bytearray, memoryview],
        namespace: Union[ArtifactNamespace, str],
    ) -> ManagedPdfWrite:
        """Atomically persist one PDF and return its internal relative locator."""

        pdf = self._validate_pdf(content)
        scope = self._namespace(namespace)
        digest = hashlib.sha256(pdf).hexdigest()
        with self._locked_root(exclusive=True) as root_descriptor:
            result = self._write_pdf_locked(root_descriptor, pdf, scope, digest)
            self._assert_root_descriptor(root_descriptor)
            return result

    def read_pdf(
        self, managed_relative_path: str, expected_sha256: str
    ) -> VerifiedResumePdf:
        """Read through a validated locator and verify the separately stored hash."""

        scope, digest = self._parse_locator(managed_relative_path)
        expected = self._digest(expected_sha256)
        if digest != expected:
            raise ArtifactIntegrityError("artifact reference digest does not match")
        with self._locked_root(exclusive=False) as root_descriptor:
            scope_descriptor = first_descriptor = parent_descriptor = -1
            try:
                scope_descriptor = self._open_directory_at(root_descriptor, scope.value)
                first_descriptor = self._open_directory_at(scope_descriptor, digest[:2])
                parent_descriptor = self._open_directory_at(
                    first_descriptor, digest[2:4]
                )
                content = self._read_file_at(
                    parent_descriptor, f"{digest}.pdf", expected
                )
                self._assert_directory_entry(
                    first_descriptor, digest[2:4], parent_descriptor
                )
                self._assert_directory_entry(
                    scope_descriptor, digest[:2], first_descriptor
                )
                self._assert_directory_entry(
                    root_descriptor, scope.value, scope_descriptor
                )
                self._assert_root_descriptor(root_descriptor)
            finally:
                self._close(parent_descriptor)
                self._close(first_descriptor)
                self._close(scope_descriptor)
        return VerifiedResumePdf(content, expected, len(content), scope)

    def _write_pdf_locked(
        self,
        root_descriptor: int,
        pdf: bytes,
        scope: ArtifactNamespace,
        digest: str,
    ) -> ManagedPdfWrite:
        locator = self._locator(scope, digest)
        scope_descriptor = first_descriptor = parent_descriptor = -1
        try:
            scope_descriptor = self._open_directory_at(root_descriptor, scope.value)
            first_descriptor = self._ensure_directory_at(scope_descriptor, digest[:2])
            parent_descriptor = self._ensure_directory_at(first_descriptor, digest[2:4])
            target_name = f"{digest}.pdf"
            if self._entry_info(parent_descriptor, target_name) is not None:
                self._read_file_at(parent_descriptor, target_name, digest)
            else:
                self._publish_pdf(parent_descriptor, target_name, pdf, digest)
            self._assert_directory_entry(
                first_descriptor, digest[2:4], parent_descriptor
            )
            self._assert_directory_entry(scope_descriptor, digest[:2], first_descriptor)
            self._assert_directory_entry(root_descriptor, scope.value, scope_descriptor)
            return ManagedPdfWrite(locator, digest, len(pdf), scope)
        finally:
            self._close(parent_descriptor)
            self._close(first_descriptor)
            self._close(scope_descriptor)

    def _publish_pdf(
        self, parent_descriptor: int, target_name: str, pdf: bytes, digest: str
    ) -> None:
        descriptor = -1
        temporary: Optional[str] = None
        try:
            descriptor, temporary = self._create_temporary(parent_descriptor)
            view = memoryview(pdf)
            offset = 0
            while offset < len(view):
                written = os.write(descriptor, view[offset:])
                if written <= 0:
                    raise OSError(errno.EIO, "short artifact write")
                offset += written
            os.fsync(descriptor)
            info = os.fstat(descriptor)
            self._validate_file_info(info)
            if info.st_size != len(pdf):
                raise ArtifactIntegrityError("temporary artifact size is invalid")
            self._close(descriptor)
            descriptor = -1

            try:
                os.link(
                    temporary,
                    target_name,
                    src_dir_fd=parent_descriptor,
                    dst_dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except FileExistsError:
                # Another cooperating writer published the same content address.
                self._read_file_at(parent_descriptor, target_name, digest)
                return
            os.unlink(temporary, dir_fd=parent_descriptor)
            temporary = None
            self._read_file_at(parent_descriptor, target_name, digest)
            self._sync_directory(parent_descriptor)
        except ArtifactRepositoryError:
            raise
        except OSError:
            raise ArtifactRepositoryError("resume PDF could not be persisted") from None
        finally:
            self._close(descriptor)
            if temporary is not None:
                try:
                    os.unlink(temporary, dir_fd=parent_descriptor)
                except OSError:
                    pass

    @staticmethod
    def _create_temporary(parent_descriptor: int) -> Tuple[int, str]:
        flags = _file_flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        for _attempt in range(32):
            name = f".resume-pdf-{secrets.token_hex(16)}.tmp"
            try:
                descriptor = os.open(name, flags, _FILE_MODE, dir_fd=parent_descriptor)
                os.fchmod(descriptor, _FILE_MODE)
                return descriptor, name
            except FileExistsError:
                continue
            except OSError:
                raise ArtifactSecurityError(
                    "temporary artifact could not be created"
                ) from None
        raise ArtifactSecurityError("temporary artifact name could not be allocated")

    def _prepare_root(self) -> None:
        try:
            self._root.mkdir(mode=_DIRECTORY_MODE, parents=True, exist_ok=True)
            info = os.lstat(self._root)
            self._validate_directory_info(info, allow_repair=True)
            os.chmod(self._root, _DIRECTORY_MODE, follow_symlinks=False)
            self._validate_directory_info(os.lstat(self._root))
        except ArtifactRepositoryError:
            raise
        except (NotImplementedError, OSError):
            raise ArtifactSecurityError(
                "artifact repository root is unavailable"
            ) from None

    def _open_root(self) -> int:
        descriptor = -1
        try:
            before = os.lstat(self._root)
            self._validate_directory_info(before)
            descriptor = os.open(self._root, _directory_flags())
            opened = os.fstat(descriptor)
            self._validate_directory_info(opened)
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                raise ArtifactSecurityError("artifact repository root changed")
            self._assert_root_descriptor(descriptor)
            return descriptor
        except ArtifactRepositoryError:
            self._close(descriptor)
            raise
        except OSError as exc:
            self._close(descriptor)
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise ArtifactSecurityError(
                    "artifact repository root is unsafe"
                ) from None
            raise ArtifactSecurityError(
                "artifact repository root is inaccessible"
            ) from None

    def _assert_root_descriptor(self, descriptor: int) -> None:
        try:
            named = os.lstat(self._root)
            opened = os.fstat(descriptor)
        except OSError:
            raise ArtifactSecurityError(
                "artifact repository root is inaccessible"
            ) from None
        self._validate_directory_info(named)
        self._validate_directory_info(opened)
        if (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
            raise ArtifactSecurityError("artifact repository root changed")
        identity = (opened.st_dev, opened.st_ino)
        if self._root_identity is not None and identity != self._root_identity:
            raise ArtifactSecurityError("artifact repository root changed")

    @staticmethod
    def _validate_directory_info(
        info: os.stat_result, *, allow_repair: bool = False
    ) -> None:
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ArtifactSecurityError("managed artifact directory is unsafe")
        if not ResumePdfArtifactRepository._owned(info):
            raise ArtifactSecurityError(
                "managed artifact directory has the wrong owner"
            )
        if not allow_repair and stat.S_IMODE(info.st_mode) != _DIRECTORY_MODE:
            raise ArtifactSecurityError("managed artifact directory is not owner-only")

    @classmethod
    def _ensure_directory_at(
        cls, parent_descriptor: int, name: str, *, repair_mode: bool = False
    ) -> int:
        created = False
        try:
            os.mkdir(name, _DIRECTORY_MODE, dir_fd=parent_descriptor)
            created = True
        except FileExistsError:
            pass
        except OSError:
            raise ArtifactSecurityError(
                "managed artifact directory is unavailable"
            ) from None
        descriptor = -1
        try:
            before = cls._entry_info(parent_descriptor, name)
            if before is None:
                raise ArtifactSecurityError("managed artifact directory is unavailable")
            cls._validate_directory_info(before, allow_repair=created or repair_mode)
            descriptor = os.open(name, _directory_flags(), dir_fd=parent_descriptor)
            opened = os.fstat(descriptor)
            cls._validate_directory_info(opened, allow_repair=created or repair_mode)
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                raise ArtifactSecurityError("managed artifact directory changed")
            if created or repair_mode:
                os.fchmod(descriptor, _DIRECTORY_MODE)
            cls._validate_directory_info(os.fstat(descriptor))
            cls._assert_directory_entry(parent_descriptor, name, descriptor)
            if created:
                cls._sync_directory(parent_descriptor)
            return descriptor
        except ArtifactRepositoryError:
            cls._close(descriptor)
            raise
        except OSError as exc:
            cls._close(descriptor)
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise ArtifactSecurityError(
                    "managed artifact directory is unsafe"
                ) from None
            raise ArtifactSecurityError(
                "managed artifact directory is inaccessible"
            ) from None

    @classmethod
    def _open_directory_at(cls, parent_descriptor: int, name: str) -> int:
        before = cls._entry_info(parent_descriptor, name)
        if before is None:
            raise ArtifactNotFoundError("managed artifact directory is missing")
        cls._validate_directory_info(before)
        descriptor = -1
        try:
            descriptor = os.open(name, _directory_flags(), dir_fd=parent_descriptor)
            opened = os.fstat(descriptor)
            cls._validate_directory_info(opened)
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                raise ArtifactSecurityError("managed artifact directory changed")
            cls._assert_directory_entry(parent_descriptor, name, descriptor)
            return descriptor
        except ArtifactRepositoryError:
            cls._close(descriptor)
            raise
        except OSError as exc:
            cls._close(descriptor)
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise ArtifactSecurityError(
                    "managed artifact directory is unsafe"
                ) from None
            raise ArtifactSecurityError(
                "managed artifact directory is inaccessible"
            ) from None

    @classmethod
    def _assert_directory_entry(
        cls, parent_descriptor: int, name: str, descriptor: int
    ) -> None:
        named = cls._entry_info(parent_descriptor, name)
        if named is None:
            raise ArtifactSecurityError("managed artifact directory changed")
        opened = os.fstat(descriptor)
        cls._validate_directory_info(named)
        cls._validate_directory_info(opened)
        if (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
            raise ArtifactSecurityError("managed artifact directory changed")

    def _prepare_lock_file(self, root_descriptor: int) -> None:
        descriptor = -1
        try:
            descriptor = os.open(
                _LOCK_NAME,
                _file_flags(os.O_RDWR | os.O_CREAT),
                _FILE_MODE,
                dir_fd=root_descriptor,
            )
            info = os.fstat(descriptor)
            self._validate_lock_info(info, allow_repair=True)
            os.fchmod(descriptor, _FILE_MODE)
            self._validate_lock_info(os.fstat(descriptor))
            self._assert_file_entry(root_descriptor, _LOCK_NAME, descriptor, lock=True)
        except ArtifactRepositoryError:
            raise
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise ArtifactSecurityError("artifact write lock is unsafe") from None
            raise ArtifactSecurityError("artifact write lock is unavailable") from None
        finally:
            self._close(descriptor)

    @contextmanager
    def _locked_root(self, *, exclusive: bool) -> Iterator[int]:
        root_descriptor = self._open_root()
        lock_descriptor = -1
        try:
            lock_descriptor = os.open(
                _LOCK_NAME,
                _file_flags(os.O_RDWR),
                dir_fd=root_descriptor,
            )
            self._validate_lock_info(os.fstat(lock_descriptor))
            self._assert_file_entry(
                root_descriptor, _LOCK_NAME, lock_descriptor, lock=True
            )
            operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            fcntl.flock(lock_descriptor, operation)
            self._assert_file_entry(
                root_descriptor, _LOCK_NAME, lock_descriptor, lock=True
            )
            yield root_descriptor
            self._assert_file_entry(
                root_descriptor, _LOCK_NAME, lock_descriptor, lock=True
            )
        except ArtifactRepositoryError:
            raise
        except OSError:
            raise ArtifactSecurityError("artifact write lock is unavailable") from None
        finally:
            if lock_descriptor >= 0:
                try:
                    fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
                except OSError:
                    pass
            self._close(lock_descriptor)
            self._close(root_descriptor)

    @staticmethod
    def _validate_lock_info(
        info: os.stat_result, *, allow_repair: bool = False
    ) -> None:
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ArtifactSecurityError("artifact write lock is unsafe")
        if not ResumePdfArtifactRepository._owned(info) or info.st_nlink != 1:
            raise ArtifactSecurityError("artifact write lock is unsafe")
        if not allow_repair and stat.S_IMODE(info.st_mode) != _FILE_MODE:
            raise ArtifactSecurityError("artifact write lock is not owner-only")

    @classmethod
    def _read_file_at(
        cls, parent_descriptor: int, name: str, expected_sha256: str
    ) -> bytes:
        before = cls._entry_info(parent_descriptor, name)
        if before is None:
            raise ArtifactNotFoundError("resume PDF artifact was not found")
        cls._validate_file_info(before)
        descriptor = -1
        try:
            descriptor = os.open(
                name, _file_flags(os.O_RDONLY), dir_fd=parent_descriptor
            )
            opened = os.fstat(descriptor)
            cls._validate_file_info(opened)
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                raise ArtifactSecurityError("managed artifact changed while opening")
            chunks = []
            remaining = opened.st_size
            while remaining:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            after = os.fstat(descriptor)
            cls._validate_file_info(after)
            cls._assert_file_entry(parent_descriptor, name, descriptor)
        except ArtifactRepositoryError:
            raise
        except FileNotFoundError:
            raise ArtifactNotFoundError("resume PDF artifact was not found") from None
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise ArtifactSecurityError(
                    "managed artifact entry is unsafe"
                ) from None
            raise ArtifactSecurityError(
                "managed artifact entry is inaccessible"
            ) from None
        finally:
            cls._close(descriptor)
        if (opened.st_dev, opened.st_ino, opened.st_size) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
        ) or remaining != 0:
            raise ArtifactIntegrityError("managed artifact changed while reading")
        content = b"".join(chunks)
        if b"%PDF-" not in content[:1024]:
            raise ArtifactIntegrityError("managed artifact is not a PDF")
        if hashlib.sha256(content).hexdigest() != expected_sha256:
            raise ArtifactIntegrityError("managed artifact SHA-256 verification failed")
        return content

    @classmethod
    def _assert_file_entry(
        cls,
        parent_descriptor: int,
        name: str,
        descriptor: int,
        *,
        lock: bool = False,
    ) -> None:
        named = cls._entry_info(parent_descriptor, name)
        if named is None:
            raise ArtifactSecurityError("managed artifact entry changed")
        opened = os.fstat(descriptor)
        if lock:
            cls._validate_lock_info(named)
            cls._validate_lock_info(opened)
        else:
            cls._validate_file_info(named)
            cls._validate_file_info(opened)
        if (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
            raise ArtifactSecurityError("managed artifact entry changed")

    @staticmethod
    def _entry_info(parent_descriptor: int, name: str) -> Optional[os.stat_result]:
        try:
            return os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError:
            raise ArtifactSecurityError(
                "managed artifact entry is inaccessible"
            ) from None

    @staticmethod
    def _validate_file_info(info: os.stat_result) -> None:
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ArtifactSecurityError("managed artifact entry is not a regular file")
        if not ResumePdfArtifactRepository._owned(info):
            raise ArtifactSecurityError("managed artifact file has the wrong owner")
        if stat.S_IMODE(info.st_mode) != _FILE_MODE:
            raise ArtifactSecurityError("managed artifact file is not owner-only")
        if info.st_nlink != 1:
            raise ArtifactSecurityError("managed artifact file has unexpected links")
        if info.st_size <= 0 or info.st_size > MAX_PDF_BYTES:
            raise ArtifactIntegrityError("managed artifact size is invalid")

    @staticmethod
    def _owned(info: os.stat_result) -> bool:
        return not hasattr(os, "getuid") or info.st_uid == os.getuid()

    @staticmethod
    def _sync_directory(descriptor: int) -> None:
        try:
            os.fsync(descriptor)
        except OSError as exc:
            unsupported = {errno.EINVAL, errno.EBADF}
            if hasattr(errno, "ENOTSUP"):
                unsupported.add(errno.ENOTSUP)
            if exc.errno not in unsupported:
                raise ArtifactRepositoryError(
                    "artifact directory could not be synchronized"
                ) from None

    @staticmethod
    def _close(descriptor: int) -> None:
        if descriptor < 0:
            return
        try:
            os.close(descriptor)
        except OSError:
            pass

    @staticmethod
    def _namespace(value: Union[ArtifactNamespace, str]) -> ArtifactNamespace:
        try:
            return (
                value
                if isinstance(value, ArtifactNamespace)
                else ArtifactNamespace(value)
            )
        except (TypeError, ValueError):
            raise InvalidArtifactError("artifact namespace is invalid") from None

    @staticmethod
    def _digest(value: str) -> str:
        if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
            raise InvalidArtifactError("expected SHA-256 must be lowercase hexadecimal")
        return value

    @staticmethod
    def _validate_pdf(value: Any) -> bytes:
        if not isinstance(value, (bytes, bytearray, memoryview)):
            raise InvalidArtifactError("resume PDF must be bytes")
        content = bytes(value)
        if not content or len(content) > MAX_PDF_BYTES:
            raise InvalidArtifactError("resume PDF size is invalid")
        if b"%PDF-" not in content[:1024]:
            raise InvalidArtifactError("resume artifact is not a PDF")
        return content

    @staticmethod
    def _locator(scope: ArtifactNamespace, digest: str) -> str:
        return f"{scope.value}/{digest[:2]}/{digest[2:4]}/{digest}.pdf"

    @staticmethod
    def _parse_locator(value: str) -> Tuple[ArtifactNamespace, str]:
        if not isinstance(value, str) or len(value) > 128:
            raise InvalidArtifactError("managed artifact locator is invalid")
        match = _LOCATOR.fullmatch(value)
        if match is None:
            raise InvalidArtifactError("managed artifact locator is invalid")
        scope_text, first, second, digest = match.groups()
        if first != digest[:2] or second != digest[2:4]:
            raise InvalidArtifactError("managed artifact locator is invalid")
        return ArtifactNamespace(scope_text), digest


__all__ = [
    "ArtifactIntegrityError",
    "ArtifactNamespace",
    "ArtifactNotFoundError",
    "ArtifactRepositoryError",
    "ArtifactSecurityError",
    "InvalidArtifactError",
    "MAX_PDF_BYTES",
    "ManagedPdfWrite",
    "PDF_CONTENT_TYPE",
    "ResumePdfArtifactRepository",
    "VerifiedResumePdf",
]
