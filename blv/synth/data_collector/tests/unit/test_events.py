"""Tests for :class:`blv.synth.data_collector.backend.events.EventBus`."""

from __future__ import annotations

from blv.synth.data_collector.backend.events import EventBus


def test_subscribe_and_emit_invokes_callback():
    bus = EventBus()
    calls = []
    bus.subscribe("tick", lambda **kw: calls.append(kw))

    bus.emit("tick", frame=1, delta=0.016)

    assert calls == [{"frame": 1, "delta": 0.016}]


def test_emit_runs_all_subscribers_in_order():
    bus = EventBus()
    order = []
    bus.subscribe("hi", lambda: order.append("a"))
    bus.subscribe("hi", lambda: order.append("b"))
    bus.subscribe("hi", lambda: order.append("c"))

    bus.emit("hi")

    assert order == ["a", "b", "c"]


def test_exception_in_subscriber_does_not_break_others():
    bus = EventBus()
    after = []

    def bad():
        raise RuntimeError("boom")

    bus.subscribe("x", bad)
    bus.subscribe("x", lambda: after.append(1))

    bus.emit("x")
    assert after == [1]


def test_unsubscribe_removes_only_that_callback():
    bus = EventBus()
    calls = []

    def first():
        calls.append("first")

    def second():
        calls.append("second")

    bus.subscribe("x", first)
    bus.subscribe("x", second)
    bus.unsubscribe("x", first)

    bus.emit("x")
    assert calls == ["second"]


def test_unsubscribe_missing_is_silent():
    bus = EventBus()
    bus.unsubscribe("nope", lambda: None)  # no error expected


def test_clear_all_drops_everything():
    bus = EventBus()
    calls = []
    bus.subscribe("a", lambda: calls.append("a"))
    bus.subscribe("b", lambda: calls.append("b"))
    bus.clear()

    bus.emit("a")
    bus.emit("b")
    assert calls == []


def test_clear_by_name_only_drops_that_event():
    bus = EventBus()
    calls = []
    bus.subscribe("a", lambda: calls.append("a"))
    bus.subscribe("b", lambda: calls.append("b"))
    bus.clear("a")

    bus.emit("a")
    bus.emit("b")
    assert calls == ["b"]


def test_emit_with_no_subscribers_is_noop():
    bus = EventBus()
    bus.emit("never-subscribed")
