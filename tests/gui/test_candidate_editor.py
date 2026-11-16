from __future__ import annotations

from pathlib import Path

from perfeval.gui.widgets.candidate_editor import CandidateEditor


def _apk(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.write_bytes(b"apk")
    return path


def test_candidate_editor_keeps_one_to_four_ordered_candidates(qtbot, tmp_path: Path) -> None:
    editor = CandidateEditor()
    qtbot.addWidget(editor)
    first_apk = _apk(tmp_path, "first.apk")
    second_apk = _apk(tmp_path, "second.apk")
    third_apk = _apk(tmp_path, "third.apk")
    fourth_apk = _apk(tmp_path, "fourth.apk")

    editor.rows[0].id_edit.setText("v1")
    editor.rows[0].apk_picker.set_path(str(first_apk))
    editor.add_candidate("v2", str(second_apk))
    editor.add_candidate("v3", str(third_apk))
    editor.add_candidate("v4", str(fourth_apk))

    assert editor.count == 4
    assert not editor.add_button.isEnabled()
    assert editor.add_candidate("v5") is None
    assert [value.candidate_id for value in editor.candidate_inputs()] == ["v1", "v2", "v3", "v4"]

    assert editor.move_candidate(3, -1)
    assert [value.candidate_id for value in editor.candidate_inputs()] == ["v1", "v2", "v4", "v3"]
    assert editor.rows[2].index_label.text() == "Candidate 3"

    assert editor.remove_candidate(1)
    assert editor.remove_candidate(1)
    assert editor.remove_candidate(1)
    assert editor.count == 1
    assert not editor.remove_candidate(0)
    assert not editor.rows[0].remove_button.isEnabled()


def test_candidate_editor_rejects_duplicate_and_sanitized_collisions(qtbot, tmp_path: Path) -> None:
    editor = CandidateEditor(baseline_id="baseline")
    qtbot.addWidget(editor)
    apk = _apk(tmp_path, "candidate.apk")
    second = _apk(tmp_path, "candidate-2.apk")

    editor.rows[0].id_edit.setText("BASELINE")
    editor.rows[0].apk_picker.set_path(str(apk))
    messages = editor.validation_messages()
    assert any("与 Baseline 重复" in message for message in messages)

    editor.rows[0].id_edit.setText("版本 一")
    editor.add_candidate("版本 二", str(second))
    messages = editor.validation_messages()
    assert any("清洗后的输出名" in message for message in messages)


def test_candidate_editor_exposes_output_slug_and_cross_platform_id_errors(qtbot, tmp_path: Path) -> None:
    editor = CandidateEditor()
    qtbot.addWidget(editor)
    apk = _apk(tmp_path, "candidate.apk")
    row = editor.rows[0]
    row.apk_picker.set_path(str(apk))

    row.id_edit.setText("release-v2")
    assert row.output_slug == "release-v2"
    assert "release-v2" in row.slug_label.text()

    row.id_edit.setText("CON")
    assert any("Windows 保留名称" in message for message in editor.validation_messages())

    row.id_edit.setText("candidate. ")
    assert any("不能以空格或点结尾" in message for message in editor.validation_messages())


def test_candidate_editor_emits_changes_for_id_path_and_reorder(qtbot, tmp_path: Path) -> None:
    editor = CandidateEditor()
    qtbot.addWidget(editor)
    apk = _apk(tmp_path, "candidate.apk")

    with qtbot.waitSignal(editor.changed):
        editor.rows[0].id_edit.setText("release")
    with qtbot.waitSignal(editor.changed):
        editor.rows[0].apk_picker.set_path(str(apk))
    editor.add_candidate("next", str(apk))
    with qtbot.waitSignal(editor.changed):
        editor.rows[1].move_up_button.click()

    assert [row.candidate_id for row in editor.rows] == ["next", "release"]
