"""Compare built-in vs Better FBX import shading on a single FBX (background)."""

from __future__ import annotations

import json
import os
import sys


def _mesh_stats() -> list[dict]:
    import bpy

    rows = []
    for obj in bpy.context.scene.objects:
        if obj.type != "MESH" or obj.data is None:
            continue
        mesh = obj.data
        sharp_edges = sum(1 for e in mesh.edges if e.use_edge_sharp)
        smooth_faces = sum(1 for p in mesh.polygons if p.use_smooth)
        flat_faces = len(mesh.polygons) - smooth_faces
        rows.append(
            {
                "name": obj.name,
                "verts": len(mesh.vertices),
                "faces": len(mesh.polygons),
                "sharp_edges": sharp_edges,
                "smooth_faces": smooth_faces,
                "flat_faces": flat_faces,
                "custom_normals": bool(getattr(mesh, "has_custom_normals", False)),
            }
        )
    return rows


def _clear_scene() -> None:
    import bpy

    if bpy.context.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)


def _import_builtin(path: str) -> None:
    import bpy

    bpy.ops.import_scene.fbx(filepath=path)


def _import_better(path: str) -> None:
    import bpy

    bpy.ops.better_import.fbx(
        filepath=path,
        my_import_normal="Import",
        use_auto_smooth=True,
        my_angle=60,
        my_shade_mode="Smooth",
        my_edge_smoothing="Import",
        use_edge_crease=True,
        use_fix_attributes=True,
    )


def main() -> int:
    if len(sys.argv) < 3:
        print("Usage: blender --background --python compare_fbx_import.py -- <fbx> <builtin|better>")
        return 2

    fbx = os.path.normpath(sys.argv[-2])
    mode = sys.argv[-1].lower()
    if not os.path.isfile(fbx):
        print(json.dumps({"error": "file not found", "path": fbx}))
        return 1

    if mode == "builtin":
        _import_builtin(fbx)
    elif mode == "better":
        _import_better(fbx)
    else:
        print(json.dumps({"error": f"unknown mode: {mode}"}))
        return 2

    print(json.dumps({"mode": mode, "path": fbx, "meshes": _mesh_stats()}, indent=2))
    return 0


if __name__ == "__main__":
    import bpy

    if "--" in sys.argv:
        argv = sys.argv[sys.argv.index("--") + 1 :]
        if len(argv) >= 2:
            sys.argv = [sys.argv[0], argv[0], argv[1]]
    raise SystemExit(main())
