from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ParserFailure:
    error_code: str
    detail: str


@dataclass(frozen=True)
class ParsedMediaUpload:
    ordinal: int
    media_role: str
    media_type: str
    path: Path
    file_name: str


@dataclass(frozen=True)
class GeneratedArtifact:
    ordinal: int
    content: bytes
    file_name: str
    replace_token: str
    replacement_template: str
    fallback_text: str


@dataclass
class ElementResult:
    text: str
    media_reference: dict[str, Any] | None = None
    uploads: list[ParsedMediaUpload] = field(default_factory=list)
    artifacts: list[GeneratedArtifact] = field(default_factory=list)
    failures: list[ParserFailure] = field(default_factory=list)


@dataclass(frozen=True)
class ParsedMessage:
    item: dict[str, Any]
    uploads: tuple[ParsedMediaUpload, ...]
    artifacts: tuple[GeneratedArtifact, ...]
    failures: tuple[ParserFailure, ...]
