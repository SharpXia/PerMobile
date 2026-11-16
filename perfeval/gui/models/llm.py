from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .result import ResultDescriptor, ResultType, ResultViewer


class LlmApiFormat(str, Enum):
    OPENAI_CHAT = "openai_chat"
    ANTHROPIC_MESSAGES = "anthropic_messages"
    GEMINI_GENERATE_CONTENT = "gemini_generate_content"


LLM_API_FORMAT_LABELS: Mapping[LlmApiFormat, str] = MappingProxyType(
    {
        LlmApiFormat.OPENAI_CHAT: "OpenAI-compatible Chat Completions",
        LlmApiFormat.ANTHROPIC_MESSAGES: "Anthropic Messages",
        LlmApiFormat.GEMINI_GENERATE_CONTENT: "Gemini generateContent",
    }
)


class InterpretationState(str, Enum):
    NOT_GENERATED = "not_generated"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class InterpretationDepth(str, Enum):
    BRIEF = "brief"
    STANDARD = "standard"
    DEEP = "deep"


INTERPRETATION_DEPTH_LABELS: Mapping[InterpretationDepth, str] = MappingProxyType(
    {
        InterpretationDepth.BRIEF: "精简",
        InterpretationDepth.STANDARD: "标准",
        InterpretationDepth.DEEP: "深度",
    }
)


INTERPRETATION_DEPTH_DESCRIPTIONS: Mapping[InterpretationDepth, str] = MappingProxyType(
    {
        InterpretationDepth.BRIEF: "聚焦最关键结论，适合快速浏览。",
        InterpretationDepth.STANDARD: "平衡问题、证据和可执行建议。",
        InterpretationDepth.DEEP: "增加跨指标分析、根因假设、验证步骤和更多日志上下文。",
    }
)


INTERPRETATION_DEPTH_MAX_OUTPUT_TOKENS: Mapping[InterpretationDepth, int] = MappingProxyType(
    {
        InterpretationDepth.BRIEF: 1_800,
        InterpretationDepth.STANDARD: 3_500,
        InterpretationDepth.DEEP: 6_000,
    }
)


@dataclass(frozen=True)
class LlmProfile:
    profile_id: str
    name: str
    api_format: LlmApiFormat
    base_url: str
    model: str

    def __post_init__(self) -> None:
        profile_id = str(self.profile_id).strip()
        name = str(self.name).strip()
        base_url = str(self.base_url).strip().rstrip("/")
        model = str(self.model).strip()
        if not profile_id:
            raise ValueError("profile_id 不能为空")
        if not name:
            raise ValueError("配置名称不能为空")
        if not base_url:
            raise ValueError("LLM Base URL 不能为空")
        if not model:
            raise ValueError("LLM 模型名称不能为空")
        object.__setattr__(self, "profile_id", profile_id)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "base_url", base_url)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "api_format", LlmApiFormat(self.api_format))

    def to_mapping(self) -> dict[str, str]:
        return {
            "id": self.profile_id,
            "name": self.name,
            "apiFormat": self.api_format.value,
            "baseUrl": self.base_url,
            "model": self.model,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> LlmProfile:
        return cls(
            profile_id=str(value.get("id") or ""),
            name=str(value.get("name") or ""),
            api_format=LlmApiFormat(str(value.get("apiFormat") or "")),
            base_url=str(value.get("baseUrl") or ""),
            model=str(value.get("model") or ""),
        )


@dataclass(frozen=True)
class InterpretationSource:
    source_id: str
    display_name: str
    root: Path
    report_paths: tuple[Path, ...] = ()
    data_paths: tuple[Path, ...] = ()
    log_paths: tuple[Path, ...] = ()
    artifact_paths: tuple[Path, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        root = Path(self.root).expanduser().resolve(strict=False)
        source_id = str(self.source_id).strip() or os.path.normcase(str(root))
        display_name = str(self.display_name).strip() or root.name or "性能报告"
        object.__setattr__(self, "source_id", source_id)
        object.__setattr__(self, "display_name", display_name)
        object.__setattr__(self, "root", root)
        object.__setattr__(self, "report_paths", _safe_paths(root, self.report_paths))
        object.__setattr__(self, "data_paths", _safe_paths(root, self.data_paths))
        object.__setattr__(self, "log_paths", _safe_paths(root, self.log_paths))
        object.__setattr__(self, "artifact_paths", _safe_paths(root, self.artifact_paths))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def source_key(self) -> str:
        return os.path.normcase(str(self.root))

    @property
    def llm_directory(self) -> Path:
        return self.root / "llm"

    @property
    def status_path(self) -> Path:
        return self.llm_directory / "status.json"

    @property
    def interpretation_json_path(self) -> Path:
        return self.llm_directory / "interpretation.json"

    @property
    def interpretation_html_path(self) -> Path:
        return self.llm_directory / "interpretation.html"

    @property
    def interpretation_descriptor(self) -> ResultDescriptor:
        path = self.interpretation_html_path
        return ResultDescriptor(
            type=ResultType.INTERPRETATION,
            label="LLM 解读报告",
            location=str(path),
            exists=path.is_file() and not path.is_symlink(),
            viewer=ResultViewer.REPORT,
            related_task_id=self.source_id,
        )


@dataclass(frozen=True)
class InterpretationSnapshot:
    state: InterpretationState
    html_path: Path | None = None
    json_path: Path | None = None
    message: str = ""
    profile_id: str | None = None
    model: str | None = None
    depth: InterpretationDepth | None = None
    updated_at_utc: str | None = None


def _safe_paths(root: Path, values: Sequence[Path]) -> tuple[Path, ...]:
    paths: list[Path] = []
    seen: set[str] = set()
    for value in values:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = root / path
        if path.is_symlink():
            continue
        resolved = path.resolve(strict=False)
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        key = os.path.normcase(str(resolved))
        if key in seen:
            continue
        seen.add(key)
        paths.append(resolved)
    return tuple(paths)


__all__ = [
    "InterpretationSnapshot",
    "InterpretationDepth",
    "InterpretationSource",
    "InterpretationState",
    "INTERPRETATION_DEPTH_DESCRIPTIONS",
    "INTERPRETATION_DEPTH_LABELS",
    "INTERPRETATION_DEPTH_MAX_OUTPUT_TOKENS",
    "LLM_API_FORMAT_LABELS",
    "LlmApiFormat",
    "LlmProfile",
]
