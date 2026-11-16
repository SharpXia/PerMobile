from __future__ import annotations

import sys
from pathlib import Path

from perfeval.gui.controllers.task_controller import TaskController
from perfeval.gui.controllers.task_session_controller import TaskSessionController
from perfeval.gui.models.task import TaskSpec, TaskType
from perfeval.gui.pages.task_page import TaskPage
from perfeval.gui.services.process_runner import ProcessRunner


FAKE_CLI = Path(__file__).with_name("fake_cli.py")


def test_task_page_scrolls_as_one_page_and_populates_realtime_log(qtbot, tmp_path: Path) -> None:
    runner = ProcessRunner(log_directory=tmp_path / "logs")
    controller = TaskController(runner=runner)
    session = TaskSessionController(controller=controller)
    page = TaskPage(session=session)
    qtbot.addWidget(page)
    page.resize(880, 600)
    page.show()
    task = TaskSpec.create(
        task_type=TaskType.RUN,
        program=sys.executable,
        arguments=(
            str(FAKE_CLI),
            "stream",
            "--delay",
            "0",
            "--output-root",
            str(tmp_path / "output"),
        ),
        working_directory=tmp_path,
        display_command="fake-cli stream",
        expected_output_root=tmp_path / "output",
        metadata={"scenario_title": "日志测试"},
        task_id="task-page-stream",
    )

    with qtbot.waitSignal(controller.finished, timeout=5000):
        session.start(task)

    qtbot.waitUntil(lambda: page.scroll.verticalScrollBar().maximum() > 0)
    visible_log = page.log_output.toPlainText()
    assert page.summary.results_scroll is None
    assert page.scroll.horizontalScrollBar().maximum() == 0
    assert page.log_group.isChecked()
    assert page.log_output.minimumHeight() == page.log_output.maximumHeight()
    assert "[阶段] 检查设备：完成" in visible_log
    assert "[stdout] first chunk" in visible_log
    assert "[stderr] stderr-one" in visible_log
    assert "[结果] completed，退出码：0" in visible_log
