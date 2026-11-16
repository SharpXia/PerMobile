from __future__ import annotations

import ast
import json
import re
import textwrap
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


NULL_VALUES = {"null", "Null", "NULL", "~"}
TRUE_VALUES = {"true", "True", "TRUE"}
FALSE_VALUES = {"false", "False", "FALSE"}


def ensure_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sanitize_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    slug = slug.strip("-.")
    return slug or "run"


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write_text(path: Path, text: str) -> Path:
    ensure_directory(path.parent)
    path.write_text(text, encoding="utf-8")
    return path


def write_json_document(path: Path, data: Any) -> Path:
    ensure_directory(path.parent)
    path.write_text(json.dumps(data, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    return path


def write_ndjson_document(path: Path, rows: list[dict[str, Any]]) -> Path:
    ensure_directory(path.parent)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, allow_nan=False))
            handle.write("\n")
    return path


def load_json_document(path: Path) -> Any:
    return json.loads(read_text(path))


def _split_inline_list(text: str) -> list[str]:
    items: list[str] = []
    buffer: list[str] = []
    quote: str | None = None
    escaped = False
    depth = 0
    for char in text:
        if quote is not None:
            buffer.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
            buffer.append(char)
            continue
        if char == "[":
            depth += 1
            buffer.append(char)
            continue
        if char == "]":
            depth -= 1
            buffer.append(char)
            continue
        if char == "," and depth == 0:
            items.append("".join(buffer).strip())
            buffer = []
            continue
        buffer.append(char)
    tail = "".join(buffer).strip()
    if tail:
        items.append(tail)
    return items


def parse_scalar(value: str) -> Any:
    value = value.strip()
    if value == "":
        return ""
    if value in NULL_VALUES:
        return None
    if value in TRUE_VALUES:
        return True
    if value in FALSE_VALUES:
        return False
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return ast.literal_eval(value)
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [parse_scalar(item) for item in _split_inline_list(inner)]
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    if re.fullmatch(r"-?\d+\.\d+", value):
        return float(value)
    return value


def _line_indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _is_meaningful(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith("#")


def _skip_ignorable(lines: list[str], index: int) -> int:
    while index < len(lines) and not _is_meaningful(lines[index]):
        index += 1
    return index


def _parse_block(lines: list[str], index: int, indent: int) -> tuple[Any, int]:
    index = _skip_ignorable(lines, index)
    if index >= len(lines):
        return {}, index

    line = lines[index]
    line_indent = _line_indent(line)
    if line_indent < indent:
        return {}, index
    if line_indent != indent:
        raise ValueError(f"Unexpected indentation near line {index + 1}: {line!r}")

    if line.lstrip().startswith("- "):
        return _parse_list(lines, index, indent)
    return _parse_mapping(lines, index, indent)


def _parse_mapping(lines: list[str], index: int, indent: int) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {}
    while index < len(lines):
        index = _skip_ignorable(lines, index)
        if index >= len(lines):
            break
        line = lines[index]
        line_indent = _line_indent(line)
        if line_indent < indent:
            break
        if line_indent > indent:
            raise ValueError(f"Unexpected indentation near line {index + 1}: {line!r}")
        stripped = line[indent:]
        if stripped.startswith("- "):
            break
        key, sep, rest = stripped.partition(":")
        if not sep:
            raise ValueError(f"Missing ':' near line {index + 1}: {line!r}")
        key = key.strip()
        if not key:
            raise ValueError(f"Empty mapping key near line {index}: {line!r}")
        if key in result:
            raise ValueError(f"Duplicate mapping key {key!r} near line {index}: {line!r}")
        remainder = rest.strip()
        index += 1
        if remainder:
            result[key] = parse_scalar(remainder)
            continue
        child, index = _parse_block(lines, index, indent + 2)
        result[key] = child
    return result, index


def _parse_list(lines: list[str], index: int, indent: int) -> tuple[list[Any], int]:
    result: list[Any] = []
    while index < len(lines):
        index = _skip_ignorable(lines, index)
        if index >= len(lines):
            break
        line = lines[index]
        line_indent = _line_indent(line)
        if line_indent < indent:
            break
        if line_indent > indent:
            raise ValueError(f"Unexpected indentation near line {index + 1}: {line!r}")
        stripped = line[indent:]
        if not stripped.startswith("- "):
            break
        item_text = stripped[2:].strip()
        index += 1
        if not item_text:
            child, index = _parse_block(lines, index, indent + 2)
            result.append(child)
            continue
        if ":" in item_text and not item_text.startswith("{"):
            key, sep, remainder = item_text.partition(":")
            if sep and remainder.strip():
                item: dict[str, Any] = {key.strip(): parse_scalar(remainder.strip())}
                if index < len(lines) and _is_meaningful(lines[index]) and _line_indent(lines[index]) > indent:
                    child, index = _parse_block(lines, index, indent + 2)
                    if isinstance(child, dict):
                        duplicate_keys = set(item).intersection(child)
                        if duplicate_keys:
                            duplicate = sorted(duplicate_keys)[0]
                            raise ValueError(f"Duplicate mapping key {duplicate!r} in list item")
                        item.update(child)
                    else:
                        item[key.strip()] = child
                result.append(item)
                continue
            child, index = _parse_block(lines, index, indent + 2)
            result.append({key.strip(): child})
            continue
        result.append(parse_scalar(item_text))
    return result, index


def load_yaml_document(path: Path) -> Any:
    text = textwrap.dedent(read_text(path))
    value, index = _parse_yaml_text(text)
    lines = text.splitlines()
    index = _skip_ignorable(lines, index)
    if index != len(lines):
        raise ValueError(f"Could not parse trailing content near line {index + 1} in {path}")
    return value


def _parse_yaml_text(text: str) -> tuple[Any, int]:
    lines = text.splitlines()
    return _parse_block(lines, 0, 0)


def parse_yaml_subset(text: str) -> Any:
    text = textwrap.dedent(text)
    value, index = _parse_yaml_text(text)
    lines = text.splitlines()
    index = _skip_ignorable(lines, index)
    if index != len(lines):
        raise ValueError(f"Could not parse trailing content near line {index + 1}")
    return value


def load_document(path: Path) -> Any:
    if path.suffix.lower() == ".json":
        return load_json_document(path)
    return load_yaml_document(path)


def load_scenario_config(path: Path | str) -> dict[str, Any]:
    resolved = Path(path)
    document = load_document(resolved)
    if not isinstance(document, dict):
        raise ValueError(f"Scenario config must be a mapping: {resolved}")
    return document


@dataclass(frozen=True)
class ScenarioConfig:
    schema_version: str
    project: str
    scenario_id: str
    description: str
    run: dict[str, Any]
    budgets: dict[str, Any]
    raw: dict[str, Any]

    @property
    def repeats(self) -> int:
        return read_scenario_defaults(self.raw)[1]

    @property
    def mode(self) -> str:
        return read_scenario_defaults(self.raw)[0]


def load_scenario(path: Path | str) -> ScenarioConfig:
    config = load_scenario_config(path)
    issues = validate_scenario_config(config)
    if issues:
        raise ValueError("; ".join(issues))
    run = config.get("run") if isinstance(config.get("run"), dict) else {}
    budgets = config.get("budgets") if isinstance(config.get("budgets"), dict) else {}
    return ScenarioConfig(
        schema_version=str(config.get("schemaVersion")),
        project=str(config.get("project", "")),
        scenario_id=str(config.get("scenarioId")),
        description=str(config.get("description", "")),
        run=dict(run),
        budgets=dict(budgets),
        raw=config,
    )


def validate_scenario_config(config: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    if not isinstance(config, dict):
        return ["Scenario config must be a mapping."]

    schema_version = config.get("schemaVersion")
    if not isinstance(schema_version, str) or not schema_version.startswith("perfeval.scenario."):
        issues.append("schemaVersion should start with 'perfeval.scenario.'")

    scenario_id = config.get("scenarioId")
    if not isinstance(scenario_id, str) or not scenario_id.strip():
        issues.append("scenarioId must be a non-empty string")

    run_config = config.get("run")
    if run_config is not None and not isinstance(run_config, dict):
        issues.append("run must be a mapping when provided")
    elif isinstance(run_config, dict):
        repeats = run_config.get("repeats")
        if repeats is not None and (isinstance(repeats, bool) or not isinstance(repeats, int) or repeats < 1):
            issues.append("run.repeats must be a positive integer when provided")
        mode = run_config.get("mode")
        if mode is not None and mode not in {"same_device_versions", "same_app_devices"}:
            issues.append("run.mode must be same_device_versions or same_app_devices")

    gui_config = config.get("gui")
    if gui_config is not None and not isinstance(gui_config, dict):
        issues.append("gui must be a mapping when provided")
    elif isinstance(gui_config, dict):
        quick_start = gui_config.get("quickStart")
        if quick_start is not None and not isinstance(quick_start, bool):
            issues.append("gui.quickStart must be a boolean when provided")

    budgets = config.get("budgets")
    if budgets is not None and not isinstance(budgets, dict):
        issues.append("budgets must be a mapping when provided")
    elif isinstance(budgets, dict):
        for metric_id, budget in budgets.items():
            if not isinstance(metric_id, str) or not isinstance(budget, dict):
                issues.append("each budgets entry must map a metric id to a mapping")
                continue
            direction = budget.get("direction")
            if direction is not None and direction not in {"lower_better", "higher_better", "neutral"}:
                issues.append(f"budgets.{metric_id}.direction is invalid")

    apps = config.get("apps")
    if apps is not None:
        if not isinstance(apps, list):
            issues.append("apps must be a list when provided")
        else:
            seen_app_ids: set[str] = set()
            for index, app in enumerate(apps):
                if not isinstance(app, dict):
                    issues.append(f"apps[{index}] must be a mapping")
                    continue
                app_id = app.get("id")
                if not isinstance(app_id, str) or not app_id.strip():
                    issues.append(f"apps[{index}].id must be a non-empty string")
                elif app_id in seen_app_ids:
                    issues.append(f"apps contains duplicate id {app_id!r}")
                else:
                    seen_app_ids.add(app_id)
                if not isinstance(app.get("platform"), str) or not app.get("platform"):
                    issues.append(f"apps[{index}].platform must be a non-empty string")

    return issues


def load_run_summary(path: Path | str) -> dict[str, Any]:
    resolved = Path(path)
    if resolved.is_dir():
        resolved = resolved / "summary.json"
    document = load_json_document(resolved)
    if not isinstance(document, dict):
        raise ValueError(f"Summary document must be a mapping: {resolved}")
    return document


def resolve_input_document(path: Path | str) -> Path:
    resolved = Path(path)
    if resolved.is_dir():
        summary = resolved / "summary.json"
        if summary.exists():
            return summary
    return resolved


def read_scenario_defaults(config: dict[str, Any]) -> tuple[str, int]:
    run_config = config.get("run") if isinstance(config, dict) else None
    if not isinstance(run_config, dict):
        return "same_device_versions", 1
    mode = run_config.get("mode") if isinstance(run_config.get("mode"), str) else "same_device_versions"
    repeats = run_config.get("repeats")
    if not isinstance(repeats, int) or repeats < 1:
        repeats = 1
    return mode, repeats


def load_budget_mapping(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if not isinstance(document, dict):
        return {}
    budgets = document.get("budgets")
    if isinstance(budgets, dict):
        return {
            key: value
            for key, value in budgets.items()
            if isinstance(key, str) and isinstance(value, dict)
        }
    metrics = document.get("metrics")
    if isinstance(metrics, dict):
        return {
            key: value
            for key, value in metrics.items()
            if isinstance(key, str) and isinstance(value, dict)
        }
    return {}


def metric_value(entry: Any) -> float | int | None:
    if isinstance(entry, dict):
        value = entry.get("value")
        if isinstance(value, (int, float)):
            return value
    if isinstance(entry, (int, float)):
        return entry
    return None


def metric_unit(entry: Any) -> str:
    if isinstance(entry, dict):
        unit = entry.get("unit")
        if isinstance(unit, str):
            return unit
    return ""


def metric_direction(entry: Any) -> str | None:
    if isinstance(entry, dict):
        direction = entry.get("direction")
        if isinstance(direction, str):
            return direction
    return None


def load_metric_map(summary: dict[str, Any]) -> dict[str, Any]:
    metrics = summary.get("metrics")
    if isinstance(metrics, dict):
        return metrics
    metric_results = summary.get("metricResults")
    if isinstance(metric_results, list):
        normalized: dict[str, Any] = {}
        for item in metric_results:
            if not isinstance(item, dict):
                continue
            name = item.get("metric") or item.get("name")
            if isinstance(name, str):
                normalized[name] = item
        return normalized
    return {}
