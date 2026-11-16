from __future__ import annotations

from datetime import datetime, timezone

import pytest

from perfeval.core.schema import (
    MANIFEST_SCHEMA_VERSION,
    Manifest,
    ManifestApp,
    ManifestDevice,
    ManifestEnvironment,
    ManifestRun,
    ManifestScenario,
    ValidationError,
)


def test_manifest_round_trip_and_normalization() -> None:
    manifest = Manifest.from_run_context(
        run_id="run-001",
        created_at_utc=datetime(2026, 6, 24, 10, 30, 0, tzinfo=timezone.utc),
        mode="same_device_versions",
        scenario=ManifestScenario(scenario_id="startup_home", description="Cold start"),
        app=ManifestApp(
            platform="android",
            app_id="candidate",
            package_name="com.example.app",
            version_name="1.3.0",
            version_code="130",
            artifact_path="artifacts/app.apk",
            artifact_sha256="abc123",
        ),
        device=ManifestDevice(
            platform="android",
            device_id="pixel6_lab",
            serial_or_udid="R58R...",
            model="Pixel 6",
            api_level=34,
            refresh_rate_hz=60,
        ),
        environment=ManifestEnvironment(
            host_os="macOS",
            host_arch="arm64",
            tool_version="0.1.0",
            network_profile="wifi-lab",
            battery_level=82,
            charging=True,
            thermal_state="nominal",
        ),
        run=ManifestRun(
            repeat_index=1,
            warmup_index=0,
            started_at_utc="2026-06-24T10:30:02Z",
            ended_at_utc="2026-06-24T10:30:31Z",
            status="completed",
            evidence_level=("L0", "L1", "L2"),
        ),
    )

    payload = manifest.to_dict()
    assert payload["schemaVersion"] == MANIFEST_SCHEMA_VERSION
    assert payload["app"]["versionCode"] == "130"
    assert payload["run"]["evidenceLevel"] == ["L0", "L1", "L2"]

    reloaded = Manifest.from_dict(payload)
    assert reloaded == manifest


def test_manifest_validation_errors_are_actionable() -> None:
    with pytest.raises(ValidationError) as excinfo:
        Manifest.from_dict(
            {
                "schemaVersion": "wrong",
                "runId": "run-1",
                "createdAtUtc": "2026-06-24T10:30:00Z",
                "mode": "same_device_versions",
                "scenario": {"scenarioId": "startup", "description": "desc"},
                "app": {"platform": "android", "appId": "candidate"},
                "device": {"platform": "android", "deviceId": "d", "serialOrUdid": "s"},
                "environment": {"hostOs": "macOS", "hostArch": "arm64", "toolVersion": "0.1.0"},
                "run": {
                    "repeatIndex": 1,
                    "warmupIndex": 0,
                    "startedAtUtc": "2026-06-24T10:30:02Z",
                    "status": "completed",
                    "evidenceLevel": ["L0"],
                },
            }
        )

    assert "schemaVersion" in str(excinfo.value)


def test_manifest_environment_rejects_invalid_battery_and_charging_values() -> None:
    base = {"hostOs": "macOS", "hostArch": "arm64", "toolVersion": "0.1.0"}
    with pytest.raises(ValidationError, match="charging"):
        ManifestEnvironment.from_dict({**base, "charging": "yes"})
    with pytest.raises(ValidationError, match="batteryLevel"):
        ManifestEnvironment.from_dict({**base, "batteryLevel": 101})
