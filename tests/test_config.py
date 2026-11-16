from pathlib import Path

import pytest

from perfeval.core.config import load_scenario, parse_yaml_subset, validate_scenario_config


def test_parse_yaml_subset_supports_document_examples() -> None:
    data = parse_yaml_subset(
        """
        schemaVersion: perfeval.scenario.v1
        project: PerMobile
        scenarioId: startup_home
        run:
          repeats: 10
          mode: same_device_versions
        devices:
          - id: pixel6_lab
            platform: android
            tags: [high, android14, 60hz]
        collectors:
          android:
            logcat: true
        """
    )

    assert data["run"]["repeats"] == 10
    assert data["devices"][0]["tags"] == ["high", "android14", "60hz"]
    assert data["collectors"]["android"]["logcat"] is True


def test_load_scenario_reads_example() -> None:
    scenario = load_scenario(Path("scenarios/startup.synthetic.yaml"))

    assert scenario.schema_version == "perfeval.scenario.v1"
    assert scenario.scenario_id == "startup_home"
    assert scenario.repeats == 5
    assert scenario.mode == "same_device_versions"
    assert "startup.cold.ms" in scenario.budgets


def test_parse_yaml_subset_supports_list_item_continuations() -> None:
    data = parse_yaml_subset(
        """
        apps:
          - id: baseline
            platform: android
            packageName: com.perfeval.fixture
          - id: candidate
            platform: android
            versionCode: 101
        """
    )

    assert data["apps"][0]["id"] == "baseline"
    assert data["apps"][0]["platform"] == "android"
    assert data["apps"][1]["versionCode"] == 101


def test_parse_yaml_subset_handles_escaped_quotes_in_inline_lists() -> None:
    data = parse_yaml_subset(r'tags: ["a\"b", "c,d"]')

    assert data["tags"] == ['a"b', "c,d"]


def test_parse_yaml_subset_rejects_duplicate_keys() -> None:
    with pytest.raises(ValueError, match="Duplicate mapping key"):
        parse_yaml_subset("run:\n  repeats: 5\n  repeats: 10\n")


def test_scenario_validation_rejects_boolean_repeats_and_invalid_direction() -> None:
    issues = validate_scenario_config(
        {
            "schemaVersion": "perfeval.scenario.v1",
            "scenarioId": "invalid",
            "run": {"repeats": True},
            "budgets": {"throughput": {"direction": "up_is_good"}},
        }
    )

    assert "run.repeats must be a positive integer when provided" in issues
    assert "budgets.throughput.direction is invalid" in issues


def test_scenario_validation_rejects_invalid_quick_start_visibility() -> None:
    issues = validate_scenario_config(
        {
            "schemaVersion": "perfeval.scenario.v1",
            "scenarioId": "invalid_gui_visibility",
            "gui": {"quickStart": "false"},
        }
    )

    assert "gui.quickStart must be a boolean when provided" in issues
