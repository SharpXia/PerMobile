"""ux(fix-4) 回归：scenario manual 提示文案与 runner 实际时序一致。

runner 实际时序（perfeval/collectors/android/runner.py _execute_plan）：
launch（am start）在交互循环之前执行，采样/录屏也在 launch 前启动——第一条
manual 提示出现时应用已被工具拉起；reset_gfxinfo 之后的第二条 manual 提示，
Enter 的语义是"结束采集窗口"（停采样/录屏 → dumpsys 采集 → 生成报告），
而非"开始采集"。

本文件锁定文案与上述事实一致，防止回退。
"""

from __future__ import annotations

import re
from pathlib import Path

from perfeval.core.config import parse_yaml_subset, validate_scenario_config

SCENARIO_DIR = Path("scenarios")

MANUAL_SCENARIOS = (
    "android-current-page-manual-scroll.yaml",
    "android-fixture-scroll.yaml",
    "android-version-ab.example.yaml",
)

_STEP_MARKER_RE = re.compile(r"【人工步骤 (\d+)/(\d+)】")


def _flow_steps(scenario_name: str) -> list[dict]:
    data = parse_yaml_subset((SCENARIO_DIR / scenario_name).read_text(encoding="utf-8"))
    issues = validate_scenario_config(data)
    assert issues == [], f"{scenario_name} 配置校验失败：{issues}"
    steps = data.get("flow", {}).get("steps")
    assert isinstance(steps, list) and steps
    return steps


def _manual_prompts(steps: list[dict]) -> list[str]:
    return [step.get("prompt", "") for step in steps if step.get("type") == "manual"]


def test_manual_prompts_declare_step_order() -> None:
    for name in MANUAL_SCENARIOS:
        prompts = _manual_prompts(_flow_steps(name))
        assert prompts, name
        for index, prompt in enumerate(prompts, start=1):
            match = _STEP_MARKER_RE.search(prompt)
            assert match, f"{name}: 第 {index} 条提示缺少【人工步骤 k/N】标记：{prompt!r}"
            assert int(match.group(1)) == index, name
            assert int(match.group(2)) == len(prompts), name


def test_first_manual_prompt_states_app_already_launched() -> None:
    """runner 先 am start 再进入交互循环：第一条提示必须说明应用已被工具启动。"""
    for name in MANUAL_SCENARIOS:
        prompt = _manual_prompts(_flow_steps(name))[0]
        assert "应用已由工具自动启动" in prompt, f"{name}: {prompt!r}"


def test_manual_prompt_semantics_match_what_runner_does_after_enter() -> None:
    """按 runner 实际动作分派语义：
    - Enter 后仍有 reset_gfxinfo/交互步骤 → 文案说明工具将清空帧统计（并
      交代后续动作归属）；
    - Enter 后只剩收尾等待（idle）→ Enter = 结束采集窗口，文案必须说明
      停止采样/结束采集，不得写"开始采集"。
    """
    for name in MANUAL_SCENARIOS:
        steps = _flow_steps(name)
        for index, step in enumerate(steps):
            if step.get("type") != "manual":
                continue
            prompt = step.get("prompt", "")
            rest = [item.get("type") for item in steps[index + 1 :] if item.get("type") != "idle"]
            if not rest:
                assert "开始采集" not in prompt, f"{name}: Enter 语义写反：{prompt!r}"
                assert ("停止" in prompt) or ("结束" in prompt), f"{name}: {prompt!r}"
            elif "reset_gfxinfo" in rest:
                assert "清空帧统计" in prompt, f"{name}: {prompt!r}"


def test_all_manual_prompts_tell_user_to_press_enter() -> None:
    for name in MANUAL_SCENARIOS:
        for prompt in _manual_prompts(_flow_steps(name)):
            assert "Enter" in prompt, f"{name}: {prompt!r}"


def test_fixture_scroll_prompt_says_swipes_are_automated() -> None:
    steps = _flow_steps("android-fixture-scroll.yaml")
    prompt = _manual_prompts(steps)[0]
    # 滑动由工具自动执行（不是用户滑）：文案必须明确，避免歧义
    assert "自动执行" in prompt, prompt
    assert "请勿触碰" in prompt, prompt
