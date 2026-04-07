"""
Standalone test for the AssetBrowser asset-switching pipeline.

Run inside Isaac Sim:
    ./python.sh tests/test_asset_swap.py

Creates a temp directory with 3 dummy USD files (colored cubes),
then tests the full swap cycle: scan → load → transform preservation
→ semantic labeling → next/prev navigation.
"""

import os
import sys
import tempfile
import shutil


def main():
    # ── Isaac Sim bootstrap ────────────────────────────────────────────
    from isaacsim import SimulationApp
    app = SimulationApp({"headless": False, "width": 1280, "height": 720})

    import omni.usd
    import omni.kit.commands
    from pxr import Usd, UsdGeom, Sdf, Gf, UsdShade

    # Wait for stage
    import asyncio
    async def wait_frames(n=5):
        for _ in range(n):
            await omni.kit.app.get_app().next_update_async()

    # ── Create dummy USD assets ────────────────────────────────────────
    tmp_dir = tempfile.mkdtemp(prefix="blv_test_assets_")
    print(f"\n=== Creating test assets in {tmp_dir} ===\n")

    colors = [
        ("red_cube", (1, 0, 0)),
        ("green_cube", (0, 1, 0)),
        ("blue_cube", (0, 0, 1)),
    ]
    for name, color in colors:
        path = os.path.join(tmp_dir, f"{name}.usd")
        s = Usd.Stage.CreateNew(path)
        cube = UsdGeom.Cube.Define(s, "/Root/Cube")
        s.SetDefaultPrim(s.GetPrimAtPath("/Root"))
        # Apply a display color so we can visually verify swaps
        cube.GetDisplayColorAttr().Set([Gf.Vec3f(*color)])
        cube.GetSizeAttr().Set(50.0)
        s.Save()
        print(f"  Created {path}")

    # ── Test setup ─────────────────────────────────────────────────────
    passed = 0
    failed = 0

    def check(name, condition):
        nonlocal passed, failed
        if condition:
            print(f"  ✓ {name}")
            passed += 1
        else:
            print(f"  ✗ FAIL: {name}")
            failed += 1

    # ── Open a blank stage ─────────────────────────────────────────────
    omni.usd.get_context().new_stage()
    stage = omni.usd.get_context().get_stage()

    # Create a ground plane for visual reference
    UsdGeom.Xform.Define(stage, "/World")

    # Let the stage settle
    loop = asyncio.get_event_loop()
    loop.run_until_complete(wait_frames(10))

    # ── Import and test AssetBrowser ───────────────────────────────────
    # Add the extension's source directory to sys.path
    ext_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if ext_root not in sys.path:
        sys.path.insert(0, ext_root)

    from blv.synth.data_collector.asset_browser import AssetBrowser

    print("\n=== Test 1: Scan folder ===")
    browser = AssetBrowser(target_prim_path="/World/TestAsset")
    count = browser.set_folder(tmp_dir, class_name="test_object")
    check("Found 3 USD files", count == 3)
    check("Current index is -1 (nothing loaded)", browser.current_index == -1)
    check("Current asset name is 'None'", browser.current_asset_name == "None")

    print("\n=== Test 2: Load first asset ===")
    success = browser.next_asset()
    loop.run_until_complete(wait_frames(5))
    check("next_asset() returned True", success)
    check("Current index is 0", browser.current_index == 0)
    check("Asset name contains 'blue_cube'", "blue_cube" in browser.current_asset_name)

    # Verify prim exists on stage
    prim = stage.GetPrimAtPath("/World/TestAsset")
    check("Target prim exists", prim.IsValid())

    # Check that it has a reference
    if prim.IsValid():
        refs = []
        for spec in prim.GetPrimStack():
            refs.extend(spec.referenceList.prependedItems)
        check("Prim has at least one reference", len(refs) > 0)

    print("\n=== Test 3: Transform preservation ===")
    # Set a transform on the prim
    if prim.IsValid():
        xf = UsdGeom.Xformable(prim)
        common = UsdGeom.XformCommonAPI(prim)
        if common:
            common.SetTranslate(Gf.Vec3d(100, 200, 300))
            common.SetRotate(Gf.Vec3f(10, 20, 30))
            common.SetScale(Gf.Vec3f(2, 2, 2))

    # Load next asset — transform should be preserved
    success = browser.next_asset()
    loop.run_until_complete(wait_frames(5))
    check("Second next_asset() returned True", success)
    check("Current index is 1", browser.current_index == 1)
    check("Asset name contains 'green_cube'", "green_cube" in browser.current_asset_name)

    prim2 = stage.GetPrimAtPath("/World/TestAsset")
    if prim2.IsValid():
        t, r, s = browser._read_xform(prim2)
        check(f"Translate preserved ~(100, 200, 300): {t}",
              abs(t[0] - 100) < 1 and abs(t[1] - 200) < 1 and abs(t[2] - 300) < 1)
        check(f"Scale preserved ~(2, 2, 2): {s}",
              abs(s[0] - 2) < 0.1 and abs(s[1] - 2) < 0.1 and abs(s[2] - 2) < 0.1)

    print("\n=== Test 4: Previous asset ===")
    success = browser.previous_asset()
    loop.run_until_complete(wait_frames(5))
    check("previous_asset() returned True", success)
    check("Current index is 0", browser.current_index == 0)

    print("\n=== Test 5: Wrap around ===")
    # Go backward from 0 → should wrap to 2
    success = browser.previous_asset()
    loop.run_until_complete(wait_frames(5))
    check("Wrap backward: index is 2", browser.current_index == 2)
    check("Asset name contains 'red_cube'", "red_cube" in browser.current_asset_name)

    print("\n=== Test 6: Semantic labeling ===")
    prim3 = stage.GetPrimAtPath("/World/TestAsset")
    if prim3.IsValid():
        # Check if the semantic label was applied
        try:
            from isaacsim.core.utils.semantics import get_semantics
            semantics = get_semantics(prim3)
            has_label = any("test_object" in str(v) for v in semantics.values())
            check("Semantic label 'test_object' applied", has_label)
        except ImportError:
            print("  (skipped — isaacsim.core.utils.semantics not available)")

    # ── Summary ────────────────────────────────────────────────────────
    print(f"\n{'='*50}")
    print(f"RESULTS: {passed} passed, {failed} failed out of {passed + failed}")
    print(f"{'='*50}\n")

    # Cleanup
    shutil.rmtree(tmp_dir, ignore_errors=True)

    # Keep the app open for 5 seconds so user can visually verify
    import time
    print("Visual inspection window open for 5 seconds...")
    time.sleep(5)

    app.close()
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
