from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from perfeval.core.config import load_json_document
from perfeval.core.report import write_report_bundle, write_summary_report_bundle


def register_parser(subparsers: Any) -> argparse.ArgumentParser:
    parser = subparsers.add_parser("report", help="Render a static HTML report from compare.json or summary.json.")
    parser.add_argument("input_json", help="Path to compare.json or summary.json.")
    parser.add_argument("--out", default="reports/latest", help="Output report directory.")
    parser.set_defaults(func=handle)
    return parser


def handle(args: argparse.Namespace) -> int:
    payload = load_json_document(Path(args.input_json))
    if not isinstance(payload, dict):
        raise ValueError("input_json must contain a JSON object")

    output_dir = Path(args.out)
    if "candidateGroup" in payload or "baselineGroup" in payload:
        outputs = write_report_bundle(payload, output_dir)
        print(f"Report: {outputs['report_html']}")
        print(f"Compare JSON: {outputs['compare_json']}")
        print(f"Metrics CSV: {outputs['metrics_csv']}")
        return 0

    diagnosis = _load_sibling_diagnosis(Path(args.input_json))
    outputs = write_summary_report_bundle(payload, output_dir, diagnosis=diagnosis)
    print(f"Report: {outputs['report_html']}")
    print(f"Summary JSON: {outputs['summary_json']}")
    return 0


def _load_sibling_diagnosis(input_path: Path) -> dict[str, Any] | None:
    diagnosis_path = input_path.parent / "diagnosis.json"
    if not diagnosis_path.exists():
        return None
    payload = load_json_document(diagnosis_path)
    return payload if isinstance(payload, dict) else None
