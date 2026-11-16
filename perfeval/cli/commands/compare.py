from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from perfeval.cli.protocol import add_gui_events_argument, emit_result, gui_events_enabled
from perfeval.core.compare import compare_summaries
from perfeval.core.config import load_document, load_run_summary, write_json_document


def register_parser(subparsers: Any) -> argparse.ArgumentParser:
    parser = subparsers.add_parser("compare", help="Compare baseline and candidate run groups.")
    parser.add_argument("baseline", help="Baseline run dir, summary.json, or run group directory.")
    parser.add_argument("candidate", help="Candidate run dir, summary.json, or run group directory.")
    parser.add_argument("--mode", default="same_device_versions")
    parser.add_argument("--budget", help="Budget YAML/JSON file.")
    parser.add_argument("--out", default="compare", help="Output directory.")
    add_gui_events_argument(parser)
    parser.set_defaults(func=handle)
    return parser


def _load_budget(path: str | None) -> dict[str, Any] | None:
    if path is None:
        return None
    document = load_document(Path(path))
    if not isinstance(document, dict):
        raise ValueError(f"Budget document must be a mapping: {path}")
    if "metrics" in document:
        return document
    if "budgets" in document and isinstance(document["budgets"], dict):
        return {"metrics": document["budgets"]}
    return document


def _load_summary_group(path: str) -> list[dict[str, Any]]:
    root = Path(path)
    if root.is_file():
        return [load_run_summary(root)]
    repeat_summaries = sorted((root / "repeats").glob("*/summary.json"))
    if repeat_summaries:
        return [load_run_summary(summary) for summary in repeat_summaries]
    summaries = sorted(root.glob("*/summary.json"))
    if summaries:
        return [load_run_summary(summary) for summary in summaries]
    if (root / "summary.json").exists():
        return [load_run_summary(root)]
    raise ValueError(f"No summary.json files found under {path}")


def handle(args: argparse.Namespace) -> int:
    baseline = _load_summary_group(args.baseline)
    candidate = _load_summary_group(args.candidate)
    compare = compare_summaries(
        baseline,
        candidate,
        mode=args.mode,
        budget=_load_budget(args.budget),
    )
    output_dir = Path(args.out)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = write_json_document(output_dir / "compare.json", compare)
    status = str(compare["status"])
    exit_code = 1 if status == "fail" else 4 if status == "invalid" else 0
    if gui_events_enabled(args):
        emit_result(
            status=status,
            exit_code=exit_code,
            outputRoot=str(output_dir),
            runDir=None,
            report=None,
            summary=None,
            artifacts=[],
            compare=str(output_path),
        )
    else:
        print(json.dumps({"status": compare["status"], "compare": str(output_path)}, sort_keys=True))
    return exit_code
