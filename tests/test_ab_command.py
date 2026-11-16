from __future__ import annotations

import json
import subprocess
from pathlib import Path

from perfeval.cli.commands import ab as ab_command
from perfeval.cli.main import main
from perfeval.collectors.android import AndroidCollectionResult, AndroidPreflightResult
from perfeval.collectors.android._events import metric_event
from perfeval.core.artifacts import sha256_of_path
from perfeval.core.compare import compare_summaries
from perfeval.core.summary import aggregate_summary


def _write_ab_scenario(
    path: Path,
    *,
    baseline_apk: Path,
    candidate_apk: Path,
    required_metric: str = "startup.cold.ms",
) -> None:
    path.write_text(
        f"""
        schemaVersion: perfeval.scenario.v1
        project: PerMobile
        scenarioId: feed_scroll_perf
        description: Feed scroll performance comparison.
        apps:
          - id: baseline
            platform: android
            packageName: com.example.app
            artifact: {baseline_apk}
            versionName: 1.0.0
          - id: candidate
            platform: android
            packageName: com.example.app
            artifact: {candidate_apk}
            versionName: 1.0.0
        run:
          mode: same_device_versions
          repeats: 5
          install:
            clearData: true
            reinstall: true
        flow:
          driver: adb_script
          steps:
            - type: launch
              activity: .MainActivity
        metricContract:
          required:
            - {required_metric}
        budgets:
          startup.cold.ms:
            direction: lower_better
            unit: ms
            failRegressionPct: 0.15
            minDeltaAbs: 80
        """,
        encoding="utf-8",
    )


def _collection_result(run_dir: Path, run_id: str, value: int = 1000) -> AndroidCollectionResult:
    return AndroidCollectionResult(
        run_id=run_id,
        artifact_dir=run_dir / "artifacts" / "android",
        events=[
            metric_event(
                seq=1,
                run_id=run_id,
                name="startup.cold.ms",
                value=value,
                unit="ms",
                collector="fake",
            )
        ],
        artifacts=[],
        commands=(),
        diagnostics=(),
    )


def test_ab_command_runs_grouped_collection_and_report(tmp_path: Path, monkeypatch) -> None:
    baseline_apk = tmp_path / "baseline.apk"
    candidate_apk = tmp_path / "candidate.apk"
    baseline_apk.write_text("baseline", encoding="utf-8")
    candidate_apk.write_text("candidate", encoding="utf-8")
    scenario = tmp_path / "scenario.yaml"
    _write_ab_scenario(scenario, baseline_apk=baseline_apk, candidate_apk=candidate_apk)
    events: list[str] = []

    monkeypatch.setattr(
        ab_command,
        "run_preflight",
        lambda adb, serial=None: AndroidPreflightResult(
            ok=True,
            adb_available=True,
            selected_serial=serial or "emulator-5554",
        ),
    )

    def fake_runner(command, timeout):
        if "install" in command:
            events.append(f"install:{Path(command[-1]).name}")
        return subprocess.CompletedProcess(command, 0, "ok\n", "")

    def fake_collect_android_run(**kwargs):
        run_id = kwargs["run_id"]
        events.append(f"collect:{run_id}")
        value = 1200 if "-candidate-" in run_id else 1000
        return _collection_result(Path(kwargs["run_dir"]), run_id, value=value)

    monkeypatch.setattr(ab_command, "default_command_runner", fake_runner)
    monkeypatch.setattr(ab_command, "collect_android_run", fake_collect_android_run)

    code = main(
        [
            "ab",
            str(scenario),
            "--android-device-run",
            "--serial",
            "emulator-5554",
            "--out",
            str(tmp_path / "runs" / "feed-ab"),
        ]
    )

    root = tmp_path / "runs" / "feed-ab"
    compare = json.loads((root / "compare" / "compare.json").read_text(encoding="utf-8"))
    baseline_summary = json.loads((root / "baseline" / "summary.json").read_text(encoding="utf-8"))
    candidate_summary = json.loads((root / "candidate" / "summary.json").read_text(encoding="utf-8"))

    assert code == 1
    assert compare["status"] == "fail"
    assert compare["metrics"]["startup.cold.ms"]["judgement"] == "fail"
    assert (root / "report" / "report.html").exists()
    assert (root / "baseline" / "repeats" / "r001" / "summary.json").exists()
    assert (root / "candidate" / "repeats" / "r005" / "summary.json").exists()
    assert baseline_summary["app"]["artifactSha256"] == sha256_of_path(baseline_apk)
    assert candidate_summary["app"]["artifactSha256"] == sha256_of_path(candidate_apk)
    assert "errors" not in compare
    assert events[:6] == [
        "install:baseline.apk",
        "collect:feed_scroll_perf-baseline-r001",
        "collect:feed_scroll_perf-baseline-r002",
        "collect:feed_scroll_perf-baseline-r003",
        "collect:feed_scroll_perf-baseline-r004",
        "collect:feed_scroll_perf-baseline-r005",
    ]
    assert events[6:] == [
        "install:candidate.apk",
        "collect:feed_scroll_perf-candidate-r001",
        "collect:feed_scroll_perf-candidate-r002",
        "collect:feed_scroll_perf-candidate-r003",
        "collect:feed_scroll_perf-candidate-r004",
        "collect:feed_scroll_perf-candidate-r005",
    ]


def test_ab_command_accepts_reusable_template_cli_overrides(tmp_path: Path, monkeypatch) -> None:
    baseline_apk = tmp_path / "baseline.apk"
    candidate_apk = tmp_path / "candidate.apk"
    baseline_apk.write_text("baseline", encoding="utf-8")
    candidate_apk.write_text("candidate", encoding="utf-8")
    scenario = tmp_path / "generic.yaml"
    scenario.write_text(
        """
        schemaVersion: perfeval.scenario.v1
        project: PerMobile
        scenarioId: generic_ab
        apps:
          - id: baseline
            platform: android
          - id: candidate
            platform: android
        run:
          mode: same_device_versions
          repeats: 1
          install:
            clearData: true
        """,
        encoding="utf-8",
    )
    collection_calls: list[dict] = []

    monkeypatch.setattr(
        ab_command,
        "run_preflight",
        lambda adb, serial=None: AndroidPreflightResult(
            ok=True,
            adb_available=True,
            selected_serial=serial or "emulator-5554",
        ),
    )
    monkeypatch.setattr(
        ab_command,
        "default_command_runner",
        lambda command, timeout: subprocess.CompletedProcess(command, 0, "ok\n", ""),
    )

    def fake_collect_android_run(**kwargs):
        collection_calls.append(kwargs)
        return _collection_result(Path(kwargs["run_dir"]), kwargs["run_id"], value=1000)

    monkeypatch.setattr(ab_command, "collect_android_run", fake_collect_android_run)

    output_root = tmp_path / "runs" / "generic-ab"
    stale_repeat = output_root / "baseline" / "repeats" / "r999" / "summary.json"
    stale_repeat.parent.mkdir(parents=True)
    stale_repeat.write_text("stale", encoding="utf-8")
    stale_report = output_root / "report" / "stale.html"
    stale_report.parent.mkdir(parents=True)
    stale_report.write_text("stale", encoding="utf-8")
    stale_candidate = output_root / "candidate-old" / "summary.json"
    stale_candidate.parent.mkdir(parents=True)
    stale_candidate.write_text("stale", encoding="utf-8")
    (output_root / "ab.json").write_text(
        json.dumps({"candidates": {"candidate-old": str(stale_candidate.parent)}}),
        encoding="utf-8",
    )
    code = main(
        [
            "ab",
            str(scenario),
            "--android-device-run",
            "--baseline-apk",
            str(baseline_apk),
            "--candidate-apk",
            str(candidate_apk),
            "--package-name",
            "com.example.app",
            "--activity",
            ".MainActivity",
            "--no-clear-data",
            "--out",
            str(output_root),
        ]
    )

    metadata = json.loads((output_root / "ab.json").read_text(encoding="utf-8"))
    assert code == 0
    assert metadata["clearData"] is False
    assert len(collection_calls) == 2
    assert all(call["package_name"] == "com.example.app" for call in collection_calls)
    assert all(call["activity"] == ".MainActivity" for call in collection_calls)
    assert all(call["auto_resolve_activity"] is False for call in collection_calls)
    assert all(call["clear_data"] is False for call in collection_calls)
    assert not stale_repeat.exists()
    assert not stale_report.exists()
    assert not stale_candidate.exists()


def test_ab_command_auto_resolves_launcher_activity_when_launch_has_no_activity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    baseline_apk = tmp_path / "baseline.apk"
    candidate_apk = tmp_path / "candidate.apk"
    baseline_apk.write_text("baseline", encoding="utf-8")
    candidate_apk.write_text("candidate", encoding="utf-8")
    scenario = tmp_path / "scenario.yaml"
    _write_ab_scenario(scenario, baseline_apk=baseline_apk, candidate_apk=candidate_apk)
    scenario.write_text(
        scenario.read_text(encoding="utf-8").replace(
            "              activity: .MainActivity\n",
            "",
        ),
        encoding="utf-8",
    )
    collection_calls: list[dict] = []

    monkeypatch.setattr(
        ab_command,
        "run_preflight",
        lambda adb, serial=None: AndroidPreflightResult(
            ok=True,
            adb_available=True,
            selected_serial=serial or "emulator-5554",
        ),
    )
    monkeypatch.setattr(
        ab_command,
        "default_command_runner",
        lambda command, timeout: subprocess.CompletedProcess(command, 0, "ok\n", ""),
    )

    def fake_collect_android_run(**kwargs):
        collection_calls.append(kwargs)
        return _collection_result(Path(kwargs["run_dir"]), kwargs["run_id"], value=1000)

    monkeypatch.setattr(ab_command, "collect_android_run", fake_collect_android_run)

    code = main(
        [
            "ab",
            str(scenario),
            "--android-device-run",
            "--repeat",
            "1",
            "--out",
            str(tmp_path / "runs" / "auto-activity-ab"),
        ]
    )

    assert code == 0
    assert len(collection_calls) == 2
    assert all(call["activity"] is None for call in collection_calls)
    assert all(call["auto_resolve_activity"] is True for call in collection_calls)


def test_ab_command_supports_five_apk_variants(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    apk_paths = {
        variant_id: tmp_path / f"{variant_id}.apk"
        for variant_id in ("baseline", "v431", "v432", "v433", "v434")
    }
    for variant_id, apk_path in apk_paths.items():
        apk_path.write_text(variant_id, encoding="utf-8")
    scenario = tmp_path / "multi.yaml"
    scenario.write_text(
        """
        schemaVersion: perfeval.scenario.v1
        project: PerMobile
        scenarioId: multi_android_ab
        apps:
          - id: baseline
            platform: android
          - id: candidate
            platform: android
        run:
          mode: same_device_versions
          repeats: 1
        budgets:
          startup.cold.ms:
            direction: lower_better
            unit: ms
            minRuns: 1
            recommendedRuns: 1
            warnRegressionPct: 0.05
            failRegressionPct: 0.15
            minDeltaAbs: 50
        """,
        encoding="utf-8",
    )
    events: list[str] = []
    values = {
        "baseline": 1000,
        "v431": 1050,
        "v432": 1200,
        "v433": 900,
        "v434": 1000,
    }

    monkeypatch.setattr(
        ab_command,
        "run_preflight",
        lambda adb, serial=None: AndroidPreflightResult(
            ok=True,
            adb_available=True,
            selected_serial=serial or "emulator-5554",
        ),
    )

    def fake_runner(command, timeout):
        if "install" in command:
            events.append(f"install:{Path(command[-1]).stem}")
        return subprocess.CompletedProcess(command, 0, "ok\n", "")

    def fake_collect_android_run(**kwargs):
        run_id = kwargs["run_id"]
        variant_id = next(
            variant for variant in values if f"-{variant}-r" in run_id
        )
        events.append(f"collect:{variant_id}")
        return _collection_result(
            Path(kwargs["run_dir"]),
            run_id,
            value=values[variant_id],
        )

    monkeypatch.setattr(ab_command, "default_command_runner", fake_runner)
    monkeypatch.setattr(ab_command, "collect_android_run", fake_collect_android_run)

    output_root = tmp_path / "runs" / "multi-ab"
    code = main(
        [
            "ab",
            str(scenario),
            "--android-device-run",
            "--baseline-apk",
            str(apk_paths["baseline"]),
            "--candidate-apk",
            f"v431={apk_paths['v431']}",
            "--candidate-apk",
            f"v432={apk_paths['v432']}",
            "--candidate-apk",
            f"v433={apk_paths['v433']}",
            "--candidate-apk",
            f"v434={apk_paths['v434']}",
            "--package-name",
            "com.example.app",
            "--activity",
            ".MainActivity",
            "--out",
            str(output_root),
        ]
    )

    captured = json.loads(capsys.readouterr().out)
    aggregate = json.loads((output_root / "compare" / "compare.json").read_text(encoding="utf-8"))
    metadata = json.loads((output_root / "ab.json").read_text(encoding="utf-8"))
    overview = (output_root / "report" / "report.html").read_text(encoding="utf-8")

    assert code == 1
    assert captured["status"] == "fail"
    assert set(captured["comparisons"]) == {"v431", "v432", "v433", "v434"}
    assert aggregate["schemaVersion"] == "perfeval.multi_compare.v1"
    assert aggregate["status"] == "fail"
    assert aggregate["candidateGroups"] == ["v431", "v432", "v433", "v434"]
    assert metadata["schemaVersion"] == "perfeval.ab.v2"
    assert metadata["variantCount"] == 5
    assert set(metadata["candidates"]) == {"v431", "v432", "v433", "v434"}
    assert events == [
        "install:baseline",
        "collect:baseline",
        "install:v431",
        "collect:v431",
        "install:v432",
        "collect:v432",
        "install:v433",
        "collect:v433",
        "install:v434",
        "collect:v434",
    ]
    assert json.loads((output_root / "compare" / "v431" / "compare.json").read_text(encoding="utf-8"))["status"] == "warn"
    assert json.loads((output_root / "compare" / "v432" / "compare.json").read_text(encoding="utf-8"))["status"] == "fail"
    assert json.loads((output_root / "compare" / "v433" / "compare.json").read_text(encoding="utf-8"))["status"] == "pass"
    assert json.loads((output_root / "compare" / "v434" / "compare.json").read_text(encoding="utf-8"))["status"] == "pass"
    assert (output_root / "report" / "v431" / "report.html").exists()
    assert (output_root / "report" / "v434" / "report.html").exists()
    assert "PerfEval 多 APK A/B 总览" in overview
    assert "v431/report.html" in overview
    assert "../compare/v432/compare.json" in overview


def test_candidate_specs_support_scenario_variants_and_auto_ids(tmp_path: Path) -> None:
    scenario_apks = {
        variant_id: tmp_path / f"{variant_id}.apk"
        for variant_id in ("baseline", "candidate", "candidate-next")
    }
    config = {
        "apps": [
            {
                "id": variant_id,
                "platform": "android",
                "packageName": "com.example.app",
                "artifact": str(apk_path),
            }
            for variant_id, apk_path in scenario_apks.items()
        ]
    }

    scenario_specs = ab_command._resolve_candidate_specs(
        config,
        baseline_id="baseline",
        legacy_candidate_id="candidate",
        candidate_ids=None,
        candidate_apk_override=None,
        candidate_apk_overrides=None,
    )
    auto_specs = ab_command._resolve_candidate_specs(
        config,
        baseline_id="baseline",
        legacy_candidate_id="candidate",
        candidate_ids=None,
        candidate_apk_override=None,
        candidate_apk_overrides=["/tmp/a.apk", "/tmp/b.apk", "/tmp/c.apk"],
    )

    assert [spec.group_id for spec in scenario_specs] == ["candidate", "candidate-next"]
    assert [spec.group_id for spec in auto_specs] == ["candidate", "candidate-2", "candidate-3"]


def test_compare_accepts_same_package_same_version_when_apk_hash_differs() -> None:
    device = {"serialOrUdid": "emulator-5554"}
    baseline = aggregate_summary(
        "baseline-r001",
        {"startup.cold.ms": [{"value": 1000, "unit": "ms", "direction": "lower_better"}]},
        context={
            "scenarioId": "startup",
            "app": {"packageName": "com.example.app", "versionName": "1.0.0", "artifactSha256": "a" * 64},
            "device": device,
        },
    )
    candidate = aggregate_summary(
        "candidate-r001",
        {"startup.cold.ms": [{"value": 1010, "unit": "ms", "direction": "lower_better"}]},
        context={
            "scenarioId": "startup",
            "app": {"packageName": "com.example.app", "versionName": "1.0.0", "artifactSha256": "b" * 64},
            "device": device,
        },
    )

    compare = compare_summaries([baseline], [candidate], mode="same_device_versions")

    assert compare["status"] == "unknown"
    assert "errors" not in compare


def test_ab_required_metric_missing_marks_compare_unknown(tmp_path: Path, monkeypatch) -> None:
    baseline_apk = tmp_path / "baseline.apk"
    candidate_apk = tmp_path / "candidate.apk"
    baseline_apk.write_text("baseline", encoding="utf-8")
    candidate_apk.write_text("candidate", encoding="utf-8")
    scenario = tmp_path / "scenario.yaml"
    _write_ab_scenario(
        scenario,
        baseline_apk=baseline_apk,
        candidate_apk=candidate_apk,
        required_metric="frame.jank_rate",
    )

    monkeypatch.setattr(
        ab_command,
        "run_preflight",
        lambda adb, serial=None: AndroidPreflightResult(
            ok=True,
            adb_available=True,
            selected_serial="emulator-5554",
        ),
    )
    monkeypatch.setattr(
        ab_command,
        "default_command_runner",
        lambda command, timeout: subprocess.CompletedProcess(command, 0, "ok\n", ""),
    )
    monkeypatch.setattr(
        ab_command,
        "collect_android_run",
        lambda **kwargs: _collection_result(Path(kwargs["run_dir"]), kwargs["run_id"], value=1000),
    )

    code = main(
        [
            "ab",
            str(scenario),
            "--android-device-run",
            "--out",
            str(tmp_path / "runs" / "missing-required"),
        ]
    )

    compare = json.loads((tmp_path / "runs" / "missing-required" / "compare" / "compare.json").read_text(encoding="utf-8"))
    assert code == 0
    assert compare["status"] == "unknown"
    assert compare["metrics"]["frame.jank_rate"]["judgement"] == "unknown"
    assert compare["summary"]["missingRequiredMetrics"] == ["frame.jank_rate"]


def test_ab_install_failure_writes_diagnostic(tmp_path: Path, monkeypatch) -> None:
    baseline_apk = tmp_path / "baseline.apk"
    candidate_apk = tmp_path / "candidate.apk"
    baseline_apk.write_text("baseline", encoding="utf-8")
    candidate_apk.write_text("candidate", encoding="utf-8")
    scenario = tmp_path / "scenario.yaml"
    _write_ab_scenario(scenario, baseline_apk=baseline_apk, candidate_apk=candidate_apk)

    monkeypatch.setattr(
        ab_command,
        "run_preflight",
        lambda adb, serial=None: AndroidPreflightResult(
            ok=True,
            adb_available=True,
            selected_serial="emulator-5554",
        ),
    )
    monkeypatch.setattr(
        ab_command,
        "default_command_runner",
        lambda command, timeout: subprocess.CompletedProcess(command, 1, "", "INSTALL_FAILED_UPDATE_INCOMPATIBLE"),
    )

    code = main(
        [
            "ab",
            str(scenario),
            "--android-device-run",
            "--out",
            str(tmp_path / "runs" / "install-fail"),
        ]
    )

    diagnostics_path = tmp_path / "runs" / "install-fail" / "diagnostics.ndjson"
    assert code == 10
    assert diagnostics_path.exists()
    assert "INSTALL_FAILED_UPDATE_INCOMPATIBLE" in diagnostics_path.read_text(encoding="utf-8")


def test_ab_allow_uninstall_retries_install(tmp_path: Path, monkeypatch) -> None:
    baseline_apk = tmp_path / "baseline.apk"
    candidate_apk = tmp_path / "candidate.apk"
    baseline_apk.write_text("baseline", encoding="utf-8")
    candidate_apk.write_text("candidate", encoding="utf-8")
    scenario = tmp_path / "scenario.yaml"
    _write_ab_scenario(scenario, baseline_apk=baseline_apk, candidate_apk=candidate_apk)
    commands: list[list[str]] = []
    failed_once = {"value": False}

    monkeypatch.setattr(
        ab_command,
        "run_preflight",
        lambda adb, serial=None: AndroidPreflightResult(
            ok=True,
            adb_available=True,
            selected_serial="emulator-5554",
        ),
    )

    def fake_runner(command, timeout):
        commands.append(command)
        if "install" in command and not failed_once["value"]:
            failed_once["value"] = True
            return subprocess.CompletedProcess(command, 1, "", "INSTALL_FAILED_UPDATE_INCOMPATIBLE")
        return subprocess.CompletedProcess(command, 0, "ok\n", "")

    monkeypatch.setattr(ab_command, "default_command_runner", fake_runner)
    monkeypatch.setattr(
        ab_command,
        "collect_android_run",
        lambda **kwargs: _collection_result(Path(kwargs["run_dir"]), kwargs["run_id"], value=1000),
    )

    code = main(
        [
            "ab",
            str(scenario),
            "--android-device-run",
            "--allow-uninstall",
            "--out",
            str(tmp_path / "runs" / "install-retry"),
        ]
    )

    assert code == 0
    assert any("uninstall" in command for command in commands)
    assert sum(1 for command in commands if "install" in command) == 3
    assert (tmp_path / "runs" / "install-retry" / "compare" / "compare.json").exists()


def test_ab_rejects_group_ids_that_collide_after_sanitizing(tmp_path: Path) -> None:
    baseline_apk = tmp_path / "baseline.apk"
    candidate_apk = tmp_path / "candidate.apk"
    baseline_apk.write_text("baseline", encoding="utf-8")
    candidate_apk.write_text("candidate", encoding="utf-8")
    scenario = tmp_path / "scenario.yaml"
    scenario.write_text(
        f"""
        schemaVersion: perfeval.scenario.v1
        project: PerMobile
        scenarioId: collision
        apps:
          - id: a/b
            platform: android
            packageName: com.example.app
            artifact: {baseline_apk}
          - id: a-b
            platform: android
            packageName: com.example.app
            artifact: {candidate_apk}
        run:
          repeats: 1
        """,
        encoding="utf-8",
    )

    try:
        ab_command.collect_android_ab(
            scenario_path=scenario,
            output_root=tmp_path / "runs",
            android_device_run=True,
            baseline_id="a/b",
            candidate_id="a-b",
        )
    except ValueError as exc:
        assert "same output directory" in str(exc)
    else:
        raise AssertionError("expected colliding group ids to be rejected")
