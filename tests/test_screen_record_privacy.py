"""录屏隐私排除验证（CM-TASK-020，呼应 CM-TASK-026 隐私校验）。

SCREEN_RECORD_SYNC §3：录屏属敏感 artifact——LLM 诊断 payload 只允许相对路径/
元数据出现，对比摘要（compare）永不包含录屏内容本身。
"""

from __future__ import annotations

import json
from pathlib import Path

from perfeval.core.compare.engine import compare_summaries
from perfeval.gui.models.llm import InterpretationSource, InterpretationDepth
from perfeval.gui.services.llm_artifacts import build_prompt

# fake mp4 内容中可识别的哨兵字节串（真实录屏画面内容的替身）
_VIDEO_SENTINEL = "SCREEN-FRAME-CONTENT-SENTINEL"
_FAKE_MP4 = b"\x00\x00\x00\x18ftypmp42" + _VIDEO_SENTINEL.encode("utf-8") * 8


def _run_dir_with_screen_record(tmp_path: Path) -> Path:
    root = tmp_path / "run-screen"
    artifact_dir = root / "artifacts" / "android"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "screen.mp4").write_bytes(_FAKE_MP4)
    (root / "summary.json").write_text(
        json.dumps(
            {
                "schemaVersion": "perfeval.summary.v1",
                "runId": "run-screen",
                "scenarioId": "screen_privacy",
                "status": "completed",
                "metrics": {
                    "startup.cold.ms": {
                        "value": 900,
                        "unit": "ms",
                        "confidence": "high",
                        "evidenceLevel": "L0",
                        "n": 3,
                    }
                },
                "artifacts": [
                    {
                        "artifactId": "screen",
                        "path": "artifacts/android/screen.mp4",
                        "collector": "screenrecord",
                        "sizeBytes": len(_FAKE_MP4),
                        "metricData": {"relatedMetrics": []},
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return root


def test_llm_payload_excludes_screen_record_content(tmp_path: Path) -> None:
    """LLM 诊断 payload：录屏内容（字节/哨兵串）不得出现，仅允许相对路径元数据。"""
    root = _run_dir_with_screen_record(tmp_path)
    source = InterpretationSource(
        "run-screen",
        "录屏隐私",
        root,
        data_paths=(root / "summary.json",),
    )

    bundle = build_prompt(source, InterpretationDepth.DEEP)

    assert _VIDEO_SENTINEL not in bundle.system_prompt
    assert _VIDEO_SENTINEL not in bundle.user_prompt
    # 路径字符串允许出现（仅引用，不内嵌内容）
    assert "artifacts/android/screen.mp4" in bundle.user_prompt


def test_compare_summary_never_contains_screen_record_content(tmp_path: Path) -> None:
    """对比摘要：录屏只保留相对路径引用（A/B 报告各自 run 目录内），内容不进对比。"""
    baseline_dir = tmp_path / "baseline"
    candidate_dir = tmp_path / "candidate"
    for directory, value in ((baseline_dir, 900.0), (candidate_dir, 1000.0)):
        artifact_dir = directory / "artifacts" / "android"
        artifact_dir.mkdir(parents=True)
        (artifact_dir / "screen.mp4").write_bytes(_FAKE_MP4)
        summary = {
            "schemaVersion": "perfeval.summary.v1",
            "runId": directory.name,
            "scenarioId": "screen_privacy",
            "status": "completed",
            "metrics": {
                "startup.cold.ms": {
                    "value": value,
                    "unit": "ms",
                    "confidence": "high",
                    "evidenceLevel": "L0",
                    "n": 3,
                }
            },
            "artifacts": [
                {
                    "artifactId": "screen",
                    "path": f"{directory.name}/artifacts/android/screen.mp4",
                    "collector": "screenrecord",
                    "metricData": {"relatedMetrics": []},
                }
            ],
        }
        (directory / "summary.json").write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")

    compare = compare_summaries(
        [json.loads((baseline_dir / "summary.json").read_text(encoding="utf-8"))],
        [json.loads((candidate_dir / "summary.json").read_text(encoding="utf-8"))],
    )

    dumped = json.dumps(compare, ensure_ascii=False)
    assert _VIDEO_SENTINEL not in dumped
    # 仅相对路径字符串可出现
    assert "artifacts/android/screen.mp4" in dumped
