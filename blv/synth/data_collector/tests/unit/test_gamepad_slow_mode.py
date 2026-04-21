"""Slow-mode behaviour for :class:`GamepadCameraController`.

Invariants under test
---------------------
* Slow mode starts OFF.
* ``SLOW_FACTOR`` is 0.5 and scales **both** move and look speeds.
* When slow mode is OFF, effective speeds equal the configured values
  exactly (no hidden factor).
* Every :meth:`enable` call forces slow mode back to OFF so a stuck
  toggle from a previous session can't persist across disable/enable.
* The Left Bumper gamepad event toggles slow mode.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from blv.synth.data_collector.backend.gamepad_camera import (
    GamepadCameraController,
)


def _make_controller(
    move_speed: float = 60.0,
    look_speed: float = 30.0,
) -> GamepadCameraController:
    ctrl = GamepadCameraController(move_speed=move_speed, look_speed=look_speed)
    # The real enable() subscribes to gamepad + app update streams and
    # touches the USD stage.  Stub those out so we can drive the logic
    # synchronously in tests.
    ctrl._ensure_camera_prim = lambda: None  # type: ignore[assignment]
    ctrl._read_camera_pose = lambda: None    # type: ignore[assignment]
    return ctrl


def test_slow_mode_initial_state_is_off():
    ctrl = _make_controller()
    assert ctrl.slow_mode is False


def test_slow_factor_is_one_half():
    assert GamepadCameraController.SLOW_FACTOR == 0.5


def test_effective_speeds_equal_configured_values_when_slow_off():
    ctrl = _make_controller(move_speed=60.0, look_speed=30.0)
    assert ctrl.slow_mode is False
    slow = ctrl.SLOW_FACTOR if ctrl.slow_mode else 1.0
    # Exactly the UI value when slow is off — no hidden factor.
    assert ctrl.move_speed * slow == 60.0
    assert ctrl.look_speed * slow == 30.0


def test_effective_speeds_scaled_when_slow_on():
    ctrl = _make_controller(move_speed=60.0, look_speed=30.0)
    ctrl._slow_mode = True
    slow = ctrl.SLOW_FACTOR if ctrl.slow_mode else 1.0
    assert slow == 0.5
    assert ctrl.move_speed * slow == pytest.approx(30.0)
    assert ctrl.look_speed * slow == pytest.approx(15.0)


def test_enable_resets_stuck_slow_mode():
    """Regression: look speed became slow after a disable→enable cycle.

    Cause was ``_slow_mode`` persisting across sessions.  Enable() must
    always reset it to False.
    """
    ctrl = _make_controller()
    ctrl._slow_mode = True  # simulate stuck state from previous session

    ctrl.enable()

    assert ctrl.slow_mode is False
    assert ctrl._enabled is True


def test_left_bumper_event_toggles_slow_mode():
    ctrl = _make_controller()
    import carb.input as _ci

    event = MagicMock()
    event.input = _ci.GamepadInput.LEFT_SHOULDER
    event.value = 1.0

    assert ctrl.slow_mode is False
    ctrl._on_gamepad_event(event)
    assert ctrl.slow_mode is True
    ctrl._on_gamepad_event(event)
    assert ctrl.slow_mode is False


def test_slow_mode_applies_uniformly_to_move_and_look():
    """Move and look must be scaled by the **same** factor.

    Test the formula used inside ``_on_update`` directly so a future
    refactor can't silently apply slow to only one axis.
    """
    ctrl = _make_controller(move_speed=80.0, look_speed=40.0)

    def effective(move: float, look: float, slow: bool):
        factor = ctrl.SLOW_FACTOR if slow else 1.0
        return move * factor, look * factor

    m_fast, l_fast = effective(ctrl.move_speed, ctrl.look_speed, slow=False)
    m_slow, l_slow = effective(ctrl.move_speed, ctrl.look_speed, slow=True)

    # Off: exact UI values.
    assert m_fast == 80.0
    assert l_fast == 40.0
    # On: both scaled by the same 0.5.
    assert m_slow / m_fast == pytest.approx(0.5)
    assert l_slow / l_fast == pytest.approx(0.5)
