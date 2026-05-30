# Run from repo root (or anywhere):
#   blender --background --python Tools/BlenderBridgeProfile/profile_fbx_import.py -- "<abs path to .fbx>" [optional_report.json]
# Mirrors blender-bridge-injector load_model hot path for timing (no addon register).
# FBX: prefers Better FBX (better_fbx) like the bridge, then builtin import_scene.fbx.

import json
import os
import sys
import time

import bpy

_BETTER_FBX_MODULE = (os.environ.get("BRIDGE_BETTER_FBX_MODULE") or "better_fbx").strip() or "better_fbx"
_FORCE_BUILTIN_FBX = os.environ.get("BRIDGE_FORCE_BUILTIN_FBX", "").lower() in ("1", "true", "yes")


def _fbx_is_ascii_text(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            h = f.read(48)
    except OSError:
        return False
    if h.startswith(b"Kaydara FBX Binary"):
        return False
    if h.lstrip().startswith(b"; FBX"):
        return True
    return b";" in h[:8] and b"FBX" in h[:40] and b"Binary" not in h[:40]


def _enable_better_fbx_addon() -> bool:
    if hasattr(bpy.ops, "better_import") and hasattr(bpy.ops.better_import, "fbx"):
        return True
    try:
        bpy.ops.preferences.addon_enable(module=_BETTER_FBX_MODULE)
    except Exception:
        pass
    return hasattr(bpy.ops, "better_import") and hasattr(bpy.ops.better_import, "fbx")


def _import_fbx_better_or_builtin(path: str) -> None:
    if not _FORCE_BUILTIN_FBX:
        if _enable_better_fbx_addon():
            try:
                if bpy.context.mode != "OBJECT":
                    bpy.ops.object.mode_set(mode="OBJECT")
                ret = bpy.ops.better_import.fbx(filepath=path)
                if ret == {"FINISHED"}:
                    return
            except Exception:
                pass
    if _fbx_is_ascii_text(path):
        raise RuntimeError(
            "ASCII FBX requires Better FBX addon (better_fbx). "
            "Install to scripts/addons and enable, or set BRIDGE_BETTER_FBX_MODULE."
        )
    bpy.ops.import_scene.fbx(filepath=path)


def _mark(t0: float) -> float:
    return time.perf_counter() - t0


def main() -> int:
    if "--" not in sys.argv:
        print(
            "Usage: blender --background --python profile_fbx_import.py -- <model.fbx> [report.json]",
            file=sys.stderr,
        )
        return 2

    argv = sys.argv[sys.argv.index("--") + 1 :]
    if not argv:
        print("Missing model path after --", file=sys.stderr)
        return 2

    model_path = argv[0]
    out_json = argv[1] if len(argv) > 1 else None

    t0 = time.perf_counter()
    phases: list[tuple[str, float]] = []

    def phase(name: str) -> None:
        phases.append((name, _mark(t0)))

    phase("script_start")

    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    phase("after_startup_delete")

    ext = os.path.splitext(model_path)[1].lower()
    if ext == ".fbx":
        _import_fbx_better_or_builtin(model_path)
    elif ext == ".obj":
        bpy.ops.wm.obj_import(filepath=model_path)
    elif ext == ".dae":
        bpy.ops.wm.collada_import(filepath=model_path)
    else:
        print(json.dumps({"error": f"unsupported extension {ext}"}))
        return 1
    phase("after_import")

    verts = edges = faces = mesh_objs = 0
    for o in bpy.context.scene.objects:
        if o.type == "MESH" and o.data:
            mesh_objs += 1
            m = o.data
            verts += len(m.vertices)
            edges += len(m.edges)
            faces += len(m.polygons)
    phase("after_mesh_stats")

    bpy.context.tool_settings.mesh_select_mode = (False, False, True)
    bpy.ops.object.select_all(action="SELECT")
    phase("after_select_all")

    view3d_runs = 0
    screen = bpy.context.screen
    if screen:
        for area in screen.areas:
            if area.type != "VIEW_3D":
                continue
            for space in area.spaces:
                if space.type == "VIEW_3D":
                    space.shading.type = "SOLID"
            region = area.regions[-1] if area.regions else None
            if region is None:
                continue
            override = {"area": area, "region": region}
            with bpy.context.temp_override(**override):
                bpy.ops.view3d.view_selected()
                view3d_runs += 1
    phase("after_view_selected")

    bpy.ops.object.select_all(action="DESELECT")
    phase("after_deselect")

    # Blender 5.x may remove ed.undo_history_clear; injector must match this behavior.
    try:
        bpy.ops.ed.undo_history_clear()
        phase("after_undo_history_clear")
    except Exception:
        phase("after_undo_history_clear_skipped")

    # pairwise deltas
    deltas: dict[str, float] = {}
    for i in range(1, len(phases)):
        n0, t0_ = phases[i - 1]
        n1, t1_ = phases[i]
        deltas[f"{n0} -> {n1}"] = round(t1_ - t0_, 4)

    report = {
        "model": model_path,
        "extension": ext,
        "total_seconds": round(phases[-1][1], 4),
        "phases": {n: round(t, 4) for n, t in phases},
        "deltas_seconds": deltas,
        "mesh_summary": {
            "mesh_objects": mesh_objs,
            "vertices": verts,
            "edges": edges,
            "faces": faces,
        },
        "view3d_view_selected_calls": view3d_runs,
    }

    text = json.dumps(report, indent=2, ensure_ascii=False)
    print(text)
    if out_json:
        with open(out_json, "w", encoding="utf-8") as f:
            f.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
