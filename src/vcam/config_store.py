"""Comment-preserving configuration edits with optimistic concurrency."""

from __future__ import annotations

import errno
import hashlib
import io
import os
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML, YAMLError
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.tokens import AliasToken

from .errors import ConfigError


class ConfigRevisionConflict(ConfigError):
    def __init__(self, expected: str | None, actual: str | None) -> None:
        self.expected_revision = expected
        self.actual_revision = actual
        super().__init__("configuration changed on disk; read it again before editing")


class ConfigWriteError(ConfigError):
    def __init__(self, message: str, *, persisted: bool = False) -> None:
        self.config_persisted = persisted
        super().__init__(message)


class ConfigMigrationRequired(ConfigError):
    """A granular edit would affect other YAML nodes through an alias."""


def camera_nodes(data: CommentedMap) -> CommentedSeq:
    key = "streams" if "streams" in data and "cameras" not in data else "cameras"
    if data.get(key) is None:
        data[key] = CommentedSeq()
    nodes = data[key]
    if not isinstance(nodes, CommentedSeq) or any(not isinstance(n, CommentedMap) for n in nodes):
        raise ConfigMigrationRequired(
            f"migrate {key} to a sequence of camera mappings before editing"
        )
    return nodes


def camera_fields(data: CommentedMap, values: dict[str, Any]) -> CommentedMap:
    node = CommentedMap(values)
    if "streams" in data and "cameras" not in data and "start_offset" in node:
        node["offset_seconds"] = node.pop("start_offset")
    return node


def _yaml() -> YAML:
    parser = YAML(typ="rt")
    parser.preserve_quotes = True
    parser.indent(mapping=2, sequence=4, offset=2)
    return parser


@dataclass(frozen=True)
class ConfigDocument:
    revision: str
    text: str
    data: CommentedMap

    @classmethod
    def parse(cls, text: str) -> ConfigDocument:
        try:
            # Normalize for the round-trip parser only; otherwise its comment
            # tokens can retain a CR that is emitted again when writing CRLF.
            data = _yaml().load(text.replace("\r\n", "\n"))
        except YAMLError as exc:
            # Parser diagnostics can include the input line, including credentials.
            mark = getattr(exc, "problem_mark", None)
            where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
            raise ConfigError(f"invalid YAML{where}") from exc
        if data is None:
            data = CommentedMap()
        if not isinstance(data, CommentedMap):
            raise ConfigError("configuration root must be a mapping")
        return cls(hashlib.sha256(text.encode("utf-8")).hexdigest(), text, data)


@contextmanager
def _edit_lock(path: Path) -> Iterator[None]:
    # This file must outlive the replace: a lock on the config's old inode
    # would not serialize the next writer. Never unlink an active lock file.
    import fcntl

    flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise ConfigWriteError("config edit lock is not a private file owned by this user")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ConfigWriteError("another writer holds the config edit lock") from exc
        yield
    finally:
        os.close(fd)


def _sync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            os.fsync(fd)
        except OSError as exc:
            if exc.errno not in {errno.EINVAL, errno.ENOTSUP}:
                raise
    finally:
        os.close(fd)


class ConfigStore:
    """Persist already schema-validated UTF-8 YAML on macOS/Linux.

    Revisions hash exact on-disk bytes, including comments and newlines.
    Reads do not require a writable directory. Writers coordinate using a
    sibling lock and also check for edits made without that lock.
    """

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()

    def _read_bytes(self) -> tuple[bytes, os.stat_result]:
        fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ConfigError("configuration must be a regular file")
            return stream.read(), info

    def read(self) -> ConfigDocument:
        try:
            contents, _ = self._read_bytes()
            return ConfigDocument.parse(contents.decode("utf-8"))
        except UnicodeError as exc:
            raise ConfigError("configuration is not UTF-8") from exc
        except OSError as exc:
            raise ConfigError(f"could not read configuration: {exc.strerror}") from exc

    def disk_revision(self) -> str:
        """Read only the byte revision; do not parse the YAML to answer status queries."""
        try:
            data, _ = self._read_bytes()
        except OSError as exc:
            raise ConfigError(f"could not read configuration: {exc.strerror}") from exc
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def preview(document: ConfigDocument) -> str:
        """Render a granular edit without writing anything."""
        parser = _yaml()
        if any(isinstance(token, AliasToken) for token in parser.scan(document.text)):
            raise ConfigMigrationRequired(
                "YAML aliases require an explicit full-document edit, not a granular edit"
            )
        output = io.StringIO()
        parser.dump(document.data, output)
        text = output.getvalue()
        if "\r\n" in document.text and "\n" not in document.text.replace("\r\n", ""):
            text = text.replace("\n", "\r\n")
        return text

    def _check_revision(self, expected: str | None) -> os.stat_result | None:
        try:
            contents, info = self._read_bytes()
        except FileNotFoundError:
            info = None
            actual = None
        else:
            actual = hashlib.sha256(contents).hexdigest()
        if actual != expected:
            raise ConfigRevisionConflict(expected, actual)
        return info

    def commit(self, text: str, *, expected_revision: str | None) -> ConfigDocument:
        """Replace matching content; ``None`` means the file must not exist."""
        candidate = ConfigDocument.parse(text)
        persisted = False
        temporary: Path | None = None
        try:
            if expected_revision is None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            with _edit_lock(self.path.with_name(f".{self.path.name}.vcam-lock")):
                info = self._check_revision(expected_revision)
                if info and not (info.st_mode & 0o222 and os.access(self.path, os.W_OK)):
                    raise ConfigWriteError("configuration file is read-only")
                if candidate.revision == expected_revision:
                    return candidate
                with tempfile.NamedTemporaryFile(
                    dir=self.path.parent, prefix=f".{self.path.name}.", delete=False
                ) as stream:
                    temporary = Path(stream.name)
                    stream.write(text.encode("utf-8"))
                    stream.flush()
                    if info:
                        os.fchmod(stream.fileno(), stat.S_IMODE(info.st_mode) & 0o777)
                    os.fsync(stream.fileno())
                self._check_revision(expected_revision)
                os.replace(temporary, self.path)
                temporary = None
                persisted = True
                _sync_directory(self.path.parent)
        except OSError as exc:
            raise ConfigWriteError(
                f"could not commit configuration: {exc.strerror}", persisted=persisted
            ) from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return candidate
