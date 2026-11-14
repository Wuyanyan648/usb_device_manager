"""开机守卫结果叠加逻辑的单元测试（只测纯函数，不碰 sysfs）。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import usbdev_core  # noqa: E402


def test_failed_then_dock_present_becomes_recovered():
    state = {
        "result": "failed",
        "reason": "坞仍未出现，已重连 2 次，停止重试",
        "hub_present": False,
        "attempts": [{"attempt": 1}, {"attempt": 2}],
    }
    out = usbdev_core.overlay_boot_guard(state, True, "10:53:37")
    assert out["result"] == "recovered"
    assert out["hub_present"] is True
    assert out["recovered_at"] == "10:53:37"
    assert "10:53:37" in out["reason"]
    assert "自行恢复" in out["reason"]
    # 历史 attempts 必须原样保留，不能因为改展示而丢证据
    assert out["attempts"] == state["attempts"]
    # 不能改到落盘的那份
    assert state["result"] == "failed"


def test_failed_and_dock_still_missing_stays_failed():
    state = {"result": "failed", "reason": "坞仍未出现，已重连 2 次，停止重试"}
    out = usbdev_core.overlay_boot_guard(state, False, "10:53:37")
    assert out["result"] == "failed"
    assert out["hub_present"] is False
    assert out["reason"] == "坞仍未出现，已重连 2 次，停止重试"
    assert "recovered_at" not in out


def test_ok_result_is_not_rewritten():
    state = {"result": "ok", "reason": "扩展坞 3-1 已枚举，不需要干预"}
    out = usbdev_core.overlay_boot_guard(state, True, "10:53:37")
    assert out["result"] == "ok"
    assert out["hub_present"] is True


def test_overlay_accepts_empty_state():
    out = usbdev_core.overlay_boot_guard(None, True, "")
    assert out["hub_present"] is True
    assert "result" not in out
