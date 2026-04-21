"""StageController — safe USD stage swap orchestration.

The single responsibility of this module is to switch the active USD
stage from one file to another without leaking state.  Stage swaps
touch a lot of moving parts: the Replicator orchestrator, its attached
writer, the render product + its hydra texture, the gamepad update
subscription, the trajectory player / recorder update subscriptions,
and the asset-browser's cached prim paths.  Getting the ordering
wrong leaves stale Carb subscriptions firing on deleted prims or a
dangling hydra texture handle that crashes the next orchestrator tick.

:class:`StageController` centralises that ordering behind an
extensible pre-close / post-open hook registry.  The UI / CLI /
headless driver just calls ``await controller.switch_to(usd_path)``
and the registered hooks run in the right order.

Hooks registered by :class:`Session` on construction:

**Pre-close (order matters):**

1. ``TrajectoryPlayer.stop(fire_on_complete=False)`` — tear down the
   update subscription before the prims it references are destroyed.
2. ``TrajectoryRecorder.stop_recording()`` if recording.
3. ``GamepadCameraController.disable_async()`` — unsubscribe the
   per-frame update, yield one tick to drain any in-flight gamepad
   callback.
4. ``AssetBrowser.clear_stage_state()`` — forget prim paths that are
   about to become invalid.
5. ``DataRecorder.prepare_for_stage_change_async()`` — drain,
   detach, disable hydra updates, destroy render product.

**Post-open:**

1. ``GamepadCameraController.enable()`` (if it was enabled before
   the swap).

**Not in the hooks**: rebuilding the render product.  The capture
subsystem picks that back up lazily via
``DataRecorder.ensure_setup()`` on the next capture — so the new
render product is guaranteed to bind to the camera prim that lives on
the just-opened stage, not to one scavenged from the old stage.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, List

import carb
import omni.kit.app
import omni.replicator.core as rep
import omni.usd

from .events import EventBus


# Number of render frames to wait after open_stage_async returns.  Kit
# 107.3 fires STAGE_EVENT_OPENED before Hydra is ready; without this
# pause the next capture can reference a half-built scene.  Values
# 10-30 work well empirically.
_WARMUP_FRAMES: int = 20

AsyncHook = Callable[[], Awaitable[None]]
SyncHook = Callable[[], None]


class StageController:
    """Owns the teardown / swap / rebuild dance for USD stage changes."""

    def __init__(self, bus: EventBus) -> None:
        self._bus: EventBus = bus
        self._pre_close_hooks: List[AsyncHook] = []
        self._post_open_hooks: List[AsyncHook] = []
        self._is_switching: bool = False
        self._stage_sub = None
        self._attach_stage_events()

    # ------------------------------------------------------------------ #
    #  Lifecycle                                                          #
    # ------------------------------------------------------------------ #

    def _attach_stage_events(self) -> None:
        try:
            ctx = omni.usd.get_context()
            self._stage_sub = ctx.get_stage_event_stream().create_subscription_to_pop(
                self._on_stage_event, name="blv.stage_controller"
            )
        except Exception as exc:  # noqa: BLE001
            carb.log_warn(
                f"[BLV] StageController: could not subscribe to stage events: {exc}"
            )

    def destroy(self) -> None:
        if self._stage_sub is not None:
            try:
                self._stage_sub.unsubscribe()
            except Exception:  # noqa: BLE001
                pass
            self._stage_sub = None
        self._pre_close_hooks.clear()
        self._post_open_hooks.clear()

    # ------------------------------------------------------------------ #
    #  Hook registration                                                  #
    # ------------------------------------------------------------------ #

    def add_pre_close_hook(self, hook: AsyncHook) -> None:
        """Register a coroutine to run before the stage is closed.

        Hooks run in registration order.  Exceptions are caught and
        emitted as ``"hook_error"`` events — one bad hook must not
        prevent the rest of the teardown from continuing.
        """
        self._pre_close_hooks.append(hook)

    def add_post_open_hook(self, hook: AsyncHook) -> None:
        """Register a coroutine to run after the new stage has opened."""
        self._post_open_hooks.append(hook)

    # ------------------------------------------------------------------ #
    #  Properties                                                         #
    # ------------------------------------------------------------------ #

    @property
    def is_switching(self) -> bool:
        return self._is_switching

    # ------------------------------------------------------------------ #
    #  Core switch                                                        #
    # ------------------------------------------------------------------ #

    async def switch_to(self, usd_path: str) -> bool:
        """Close the current stage, open *usd_path*, run all hooks.

        Returns ``True`` on success, ``False`` if ``open_stage_async``
        failed.  The caller should treat ``False`` as a terminal error
        and decide whether to abort the wider workflow (e.g. collect-all
        may want to skip the failing environment and continue).

        ``is_switching`` is set to ``True`` for the entire duration of
        this method — the guard is cleared in the ``finally`` so it
        can't get stuck on after an exception.
        """
        self._is_switching = True
        self._bus.emit("stage_will_close", usd_path=usd_path)
        try:
            # 1. Ask the Replicator orchestrator to stop so no new work
            #    queues behind the in-flight items.
            try:
                rep.orchestrator.stop()
            except Exception as exc:  # noqa: BLE001
                carb.log_warn(f"[BLV] rep.orchestrator.stop warning: {exc}")

            # 2. Run all registered pre-close hooks.  They must NOT
            #    raise through this layer — a failing hook for one
            #    subsystem should not stop the others from unwinding.
            for hook in list(self._pre_close_hooks):
                try:
                    await hook()
                except Exception as exc:  # noqa: BLE001
                    carb.log_error(f"[BLV] pre-close hook raised: {exc}")
                    self._bus.emit(
                        "hook_error", phase="pre_close", exc=exc
                    )

            # 3. Let any final orchestrator work drain.  The recorder
            #    hook already called wait_until_complete_async, but we
            #    call it again here so non-recorder orchestrator work
            #    (e.g. other extensions) is also quiesced.
            try:
                await rep.orchestrator.wait_until_complete_async()
            except Exception as exc:  # noqa: BLE001
                carb.log_warn(
                    f"[BLV] wait_until_complete_async warning: {exc}"
                )

            # 4. Yield a frame so destructors fire cleanly before we
            #    close the stage they hold references into.
            await omni.kit.app.get_app().next_update_async()

            ctx = omni.usd.get_context()

            # 5. Close the old stage.  Using the async variant is
            #    important: the sync version can re-enter the update
            #    loop while hooks are still settling, which has been
            #    observed to leave render-graph nodes in a half-alive
            #    state.
            try:
                await ctx.close_stage_async()
            except Exception as exc:  # noqa: BLE001
                carb.log_warn(f"[BLV] close_stage_async warning: {exc}")

            # 6. Let STAGE_EVENT_CLOSING callbacks fan out.
            await asyncio.sleep(0)
            await omni.kit.app.get_app().next_update_async()

            self._bus.emit("stage_closed")

            # 7. Open the new stage.  Skip the rest of the pipeline if
            #    this fails — returning False lets the caller decide
            #    whether to abort or continue with the next env.
            try:
                ok, err = await ctx.open_stage_async(usd_path)
            except Exception as exc:  # noqa: BLE001
                carb.log_error(f"[BLV] open_stage_async threw: {exc}")
                return False
            if not ok:
                carb.log_error(
                    f"[BLV] open_stage_async failed: {err}"
                )
                return False

            # 8. Warm up Hydra.  STAGE_EVENT_OPENED fires before the
            #    renderer is fully back, so capturing immediately can
            #    reference a half-built scene.
            for _ in range(_WARMUP_FRAMES):
                await omni.kit.app.get_app().next_update_async()

            # 9. Run all post-open hooks.
            for hook in list(self._post_open_hooks):
                try:
                    await hook()
                except Exception as exc:  # noqa: BLE001
                    carb.log_error(f"[BLV] post-open hook raised: {exc}")
                    self._bus.emit(
                        "hook_error", phase="post_open", exc=exc
                    )

            self._bus.emit("stage_opened", usd_path=usd_path)
            return True
        finally:
            self._is_switching = False

    # ------------------------------------------------------------------ #
    #  Event handler                                                     #
    # ------------------------------------------------------------------ #

    def _on_stage_event(self, evt) -> None:
        """Bridge USD stage events onto the bus.

        The controller only emits on the bus here; the actual cleanup
        runs inside :meth:`switch_to` for stage changes we initiated.
        External stage changes (user loads a USD from File menu) will
        still fire these events so downstream subscribers can react.
        """
        try:
            event_type = evt.type
        except Exception:  # noqa: BLE001
            return

        # Avoid import-time dep on carb enums; fall back on integer values.
        try:
            from omni.usd import StageEventType  # type: ignore

            if event_type == int(StageEventType.CLOSING):
                if not self._is_switching:
                    self._bus.emit("stage_will_close")
            elif event_type == int(StageEventType.OPENED):
                if not self._is_switching:
                    self._bus.emit("stage_opened")
        except Exception:  # noqa: BLE001
            pass
