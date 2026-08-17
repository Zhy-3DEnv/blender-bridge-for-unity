import bpy
import importlib.util
import json
import math
import os
import queue
import socket
import sys
import threading
import time

from mathutils import Matrix

bpy.context.preferences.view.show_splash = False

CLOSE_AFTER_QUICK_SAVE = True
CLOSE_AFTER_MANUAL_SAVE = False

LOAD_TEXTURES = False
TEXTURE_PATH = r""
TEXTURE_EXTENSIONS = [".png", ".jpg", ".jpeg", ".tga", ".bmp"]

_PROFILE = os.environ.get("BLENDER_BRIDGE_PROFILE", "").lower() in ("1", "true", "yes")
_SKIP_VIEW_FRAME = os.environ.get("BRIDGE_SKIP_VIEW_FRAME", "").lower() in ("1", "true", "yes")
# Better FBX Importer & Exporter (mesh online): module folder name under scripts/addons/
_BETTER_FBX_MODULE = (os.environ.get("BRIDGE_BETTER_FBX_MODULE") or "better_fbx").strip() or "better_fbx"
_FORCE_BUILTIN_FBX = os.environ.get("BRIDGE_FORCE_BUILTIN_FBX", "").lower() in ("1", "true", "yes")
_FORCE_BETTER_FBX_IMPORT = os.environ.get("BRIDGE_FORCE_BETTER_FBX_IMPORT", "").lower() in (
    "1",
    "true",
    "yes",
)
# If set, Ctrl+S still uses bpy.ops.export_scene.fbx instead of better_export.fbx
_FORCE_BUILTIN_FBX_EXPORT = os.environ.get("BRIDGE_FORCE_BUILTIN_FBX_EXPORT", "").lower() in ("1", "true", "yes")
_VALID_BETTER_EXPORT_AXES = frozenset({"MayaZUp", "OpenGL", "Unity", "Unreal1", "Unreal2"})
_VALID_BETTER_IMPORT_EDGE_SMOOTHING = frozenset({"None", "Import", "FBXSDK", "Blender"})
_BRIDGE_SCRIPT_VERSION = "2.10"
_BRIDGE_MESH_SUFFIX = ".bridge-mesh"
_BRIDGE_NORMAL_BASELINE_SUFFIX = ".normal-baseline"
_BRIDGE_POSITION_EPSILON = 1e-5

# Unity connects here to import into this Blender instead of spawning a new process.
_bridge_cmd_queue: "queue.Queue[str]" = queue.Queue()
_bridge_server_started = False
_bridge_timer_registered = False


def _plog(msg: str) -> None:
    if _PROFILE:
        print(f"[BRIDGE_PROFILE {time.perf_counter():.4f}] {msg}")


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
    """Load Better FBX if user installed but disabled; no-op if already active."""
    if hasattr(bpy.ops, "better_import") and hasattr(bpy.ops.better_import, "fbx"):
        return True
    try:
        bpy.ops.preferences.addon_enable(module=_BETTER_FBX_MODULE)
    except Exception as ex:
        if _PROFILE:
            print(f"[BRIDGE_PROFILE] addon_enable({_BETTER_FBX_MODULE}): {ex}")
    return hasattr(bpy.ops, "better_import") and hasattr(bpy.ops.better_import, "fbx")


def _bring_window_to_front() -> None:
    """Raise this Blender instance when Unity hot-imports into an existing window."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        pid = os.getpid()
        found_hwnd = None

        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def _enum_cb(hwnd, _lparam):
            nonlocal found_hwnd
            if not user32.IsWindowVisible(hwnd):
                return True
            proc_id = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(proc_id))
            if proc_id.value != pid:
                return True
            if user32.GetParent(hwnd) == 0:
                found_hwnd = hwnd
                return False
            return True

        user32.EnumWindows(WNDENUMPROC(_enum_cb), 0)
        if not found_hwnd:
            return

        if user32.IsIconic(found_hwnd):
            user32.ShowWindow(found_hwnd, 9)
        else:
            user32.ShowWindow(found_hwnd, 5)

        user32.AllowSetForegroundWindow(pid)
        user32.SetForegroundWindow(found_hwnd)
        user32.BringWindowToTop(found_hwnd)
    except Exception as ex:
        if _PROFILE:
            print(f"[BRIDGE_PROFILE] bring_window_to_front: {ex}")


def _bridge_process_queue():
    """Runs on the main thread; one import per tick."""
    try:
        path = _bridge_cmd_queue.get_nowait()
    except queue.Empty:
        return 0.05
    try:
        _bring_window_to_front()
        _run_unity_load(path)
    except Exception as ex:
        print(f"BLENDER_BRIDGE_ERROR: queued import failed for {path!r}: {ex}")
    return 0.05


def _bridge_accept_loop(port: int):
    try:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", port))
        srv.listen(8)
        print(f"BLENDER_BRIDGE: reuse server listening on 127.0.0.1:{port}")
    except OSError as ex:
        print(f"BLENDER_BRIDGE_WARN: reuse server bind failed on port {port}: {ex}")
        return

    while True:
        try:
            conn, _ = srv.accept()
        except OSError:
            break
        try:
            conn.settimeout(60.0)
            buffer = b""
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line_raw, buffer = buffer.split(b"\n", 1)
                    line = line_raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    if line == "PING":
                        conn.sendall(b"PONG\n")
                    elif line.startswith("IMPORT|"):
                        raw = os.path.normpath(line[7:].strip())
                        if raw and os.path.isfile(raw):
                            _bridge_cmd_queue.put(raw)
                            conn.sendall(b"OK\n")
                        else:
                            conn.sendall(b"ERR|bad path\n")
                    else:
                        conn.sendall(b"ERR|unknown\n")
        except Exception as ex:
            try:
                conn.sendall(f"ERR|{ex}\n".encode("utf-8"))
            except Exception:
                pass
        finally:
            try:
                conn.close()
            except Exception:
                pass


def _ensure_bridge_server():
    global _bridge_server_started, _bridge_timer_registered
    if _bridge_server_started:
        return
    if bpy.app.background:
        return
    _bridge_server_started = True
    try:
        port = int(os.environ.get("BRIDGE_BLENDER_PORT", "35971"))
    except ValueError:
        port = 35971
    threading.Thread(target=_bridge_accept_loop, args=(port,), daemon=True).start()
    if not _bridge_timer_registered:
        bpy.app.timers.register(_bridge_process_queue, first_interval=0.05, persistent=True)
        _bridge_timer_registered = True


def _read_unity_meta_normal_settings(asset_path: str) -> tuple[str, float]:
    """
    Read Unity ModelImporter normal settings from the .meta next to the FBX.
    normalImportMode: 0=Import, 1=Calculate, 2=None
    """
    import_mode = "Import"
    smooth_angle = 60.0
    meta_path = asset_path + ".meta"
    if not os.path.isfile(meta_path):
        return import_mode, smooth_angle

    try:
        with open(meta_path, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if line.startswith("normalImportMode:"):
                    raw_mode = line.split(":", 1)[1].strip()
                    if raw_mode.isdigit():
                        mode_val = int(raw_mode)
                        if mode_val == 1:
                            import_mode = "Calculate"
                        elif mode_val == 2:
                            import_mode = "None"
                        else:
                            import_mode = "Import"
                    elif raw_mode in ("Import", "Calculate", "None"):
                        import_mode = raw_mode
                    else:
                        continue
                elif line.startswith("normalSmoothAngle:"):
                    try:
                        smooth_angle = float(line.split(":", 1)[1].strip())
                    except ValueError:
                        pass
    except OSError:
        pass

    return import_mode, max(0.0, min(180.0, smooth_angle))


def _read_unity_meta_export_settings(asset_path: str) -> dict:
    """
    Read Unity ModelImporter settings that affect FBX round-trip (axis / scale).
    bakeAxisConversion: 0 = keep file axis (typical game FBX), 1 = Unity bakes on import.
    """
    settings = {"bake_axis_conversion": False, "global_scale": 1.0}
    meta_path = asset_path + ".meta"
    if not os.path.isfile(meta_path):
        return settings

    try:
        with open(meta_path, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if line.startswith("bakeAxisConversion:"):
                    try:
                        settings["bake_axis_conversion"] = bool(
                            int(line.split(":", 1)[1].strip())
                        )
                    except ValueError:
                        pass
                elif line.startswith("globalScale:"):
                    try:
                        settings["global_scale"] = float(line.split(":", 1)[1].strip())
                    except ValueError:
                        pass
    except OSError:
        pass

    return settings


def _resolve_better_fbx_axis(asset_path: str | None) -> str:
    """
    Better FBX 'Unity' axis rotates 180 deg around Y (character-facing preset) and, with
    optimize_for_game_engine + only_root_empty_node, injects +/-90 deg child rotations.
    Static scene FBX round-trip needs MayaZUp (verified on CombatIsland M_r_* assets).
    """
    env_axis = (os.environ.get("BRIDGE_EXPORT_FBX_AXIS") or "").strip()
    if env_axis in _VALID_BETTER_EXPORT_AXES:
        return env_axis
    return "MayaZUp"


def _export_optimize_for_game_engine() -> bool:
    env = os.environ.get("BRIDGE_EXPORT_OPTIMIZE_GAME_ENGINE", "").strip().lower()
    if env in ("1", "true", "yes"):
        return True
    if env in ("0", "false", "no"):
        return False
    return False


def _export_only_root_empty_node() -> bool:
    env = os.environ.get("BRIDGE_EXPORT_ONLY_ROOT_EMPTY", "").strip().lower()
    if env in ("1", "true", "yes"):
        return True
    if env in ("0", "false", "no"):
        return False
    return False


def _export_move_to_origin(asset_path: str | None) -> bool:
    env = os.environ.get("BRIDGE_EXPORT_MOVE_TO_ORIGIN", "").strip().lower()
    if env in ("1", "true", "yes"):
        return True
    if env in ("0", "false", "no"):
        return False
    return False


def _matrix_to_rows(m: Matrix) -> list[list[float]]:
    return [list(row) for row in m]


def _capture_transform_baseline() -> None:
    """Snapshot local transforms after import (flat or hierarchical) for round-trip."""
    items = []
    for obj in bpy.context.scene.objects:
        items.append(
            {
                "name": obj.name,
                "parent": obj.parent.name if obj.parent else None,
                "matrix_local": _matrix_to_rows(obj.matrix_local),
                "matrix_world": _matrix_to_rows(obj.matrix_world),
            }
        )
    bpy.context.scene["unity_bridge_transform_baseline"] = json.dumps(items)
    _plog(f"transform baseline captured ({len(items)} objects)")


def _restore_transform_baseline() -> None:
    """Restore import baseline before export (mesh-only edits should not drift child local rot)."""
    raw = bpy.context.scene.get("unity_bridge_transform_baseline")
    if not raw:
        return
    try:
        items = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return
    by_name = {obj.name: obj for obj in bpy.context.scene.objects}

    # Re-link parents first (in case hierarchy changed).
    for item in items:
        obj = by_name.get(item.get("name", ""))
        if obj is None:
            continue
        parent_name = item.get("parent")
        obj.parent = by_name.get(parent_name) if parent_name else None

    restored = 0
    for item in items:
        obj = by_name.get(item.get("name", ""))
        if obj is None:
            continue
        matrix_local = item.get("matrix_local")
        if matrix_local:
            obj.matrix_local = Matrix(matrix_local)
            restored += 1
        elif item.get("matrix_world"):
            obj.matrix_world = Matrix(item["matrix_world"])
            restored += 1
    if restored:
        print(f"BLENDER_BRIDGE: restored transform baseline for {restored} object(s)")


def _resolve_unity_normal_import_mode(asset_path: str) -> str:
    env_mode = (os.environ.get("BRIDGE_IMPORT_NORMAL_MODE") or "").strip()
    if env_mode in ("Import", "Calculate", "None"):
        return env_mode
    meta_mode, _ = _read_unity_meta_normal_settings(asset_path)
    return meta_mode


def _unity_import_smooth_angle_deg(asset_path: str) -> float:
    env_angle = os.environ.get("BRIDGE_IMPORT_SMOOTH_ANGLE", "").strip()
    if env_angle:
        try:
            return max(0.0, min(180.0, float(env_angle)))
        except ValueError:
            pass
    _, meta_angle = _read_unity_meta_normal_settings(asset_path)
    return meta_angle


def _better_fbx_import_kwargs(path: str) -> dict:
    """
    Align Better FBX import with Unity ModelImporter (.meta).

    Unity Import mode stores per-corner normals in the FBX. FBXSDK edge smoothing can
    inject hundreds of sharp edges in Blender 5.x and break that shading; use Import
    edge smoothing instead and finalize meshes afterward.
    """
    smooth_angle = _unity_import_smooth_angle_deg(path)
    unity_mode = _resolve_unity_normal_import_mode(path)

    if unity_mode == "Import":
        import_normal = "Import"
        edge_smoothing = "Import"
    else:
        import_normal = "Calculate"
        edge_smoothing = "FBXSDK"

    kwargs = {
        "filepath": path,
        "my_import_normal": import_normal,
        "use_auto_smooth": True,
        "my_angle": smooth_angle,
        "my_shade_mode": "Smooth",
        "my_edge_smoothing": edge_smoothing,
        "use_edge_crease": True,
        "use_fix_attributes": True,
    }

    edge_mode = (os.environ.get("BRIDGE_IMPORT_EDGE_SMOOTHING") or "").strip()
    if edge_mode in _VALID_BETTER_IMPORT_EDGE_SMOOTHING:
        kwargs["my_edge_smoothing"] = edge_mode

    return kwargs


def _finalize_imported_normals_for_unity(asset_path: str) -> None:
    """
    Unity Import: keep FBX custom corner normals — remove stray sharp edges.
    Unity Calculate/None: angle-based smooth shading when custom normals are absent.
    """
    angle_deg = _unity_import_smooth_angle_deg(asset_path)
    unity_mode = _resolve_unity_normal_import_mode(asset_path)
    mesh_count = 0
    custom_count = 0
    cleared_sharp = 0

    view_layer = bpy.context.view_layer
    prev_active = view_layer.objects.active
    prev_mode = bpy.context.mode
    if prev_mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")

    for obj in bpy.context.scene.objects:
        if obj.type != "MESH" or obj.data is None or not obj.data.polygons:
            continue

        mesh = obj.data
        mesh_count += 1

        for poly in mesh.polygons:
            poly.use_smooth = True

        before_sharp = sum(1 for edge in mesh.edges if edge.use_edge_sharp)
        for edge in mesh.edges:
            edge.use_edge_sharp = False
        cleared_sharp += before_sharp

        has_custom = bool(getattr(mesh, "has_custom_normals", False))
        if has_custom:
            custom_count += 1
            mesh.update()
            continue

        if unity_mode in ("Calculate", "None") and hasattr(bpy.ops.object, "shade_smooth_by_angle"):
            view_layer.objects.active = obj
            obj.select_set(True)
            try:
                bpy.ops.object.shade_smooth_by_angle(angle=math.radians(angle_deg))
            except Exception as ex:
                print(f"BLENDER_BRIDGE_WARN: shade_smooth_by_angle failed on {obj.name}: {ex}")
            obj.select_set(False)

        mesh.update()

    if prev_active:
        view_layer.objects.active = prev_active

    print(
        f"BLENDER_BRIDGE: normals finalize v{_BRIDGE_SCRIPT_VERSION} "
        f"unity_mode={unity_mode} meshes={mesh_count} custom={custom_count} "
        f"cleared_sharp={cleared_sharp} angle={angle_deg:.1f}"
    )


def _prefer_builtin_fbx_import(path: str) -> bool:
    """
    Unity binary FBX usually matches Blender's built-in importer (and Unity viewport).
    Better FBX is kept for ASCII FBX or when BRIDGE_FORCE_BETTER_FBX_IMPORT=1.
    """
    if _FORCE_BUILTIN_FBX:
        return True
    if _FORCE_BETTER_FBX_IMPORT:
        return False
    if _fbx_is_ascii_text(path):
        return False
    return True


def _import_fbx_via_builtin(path: str) -> bool:
    if bpy.context.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    unity_mode = _resolve_unity_normal_import_mode(path)
    print(
        f"BLENDER_BRIDGE: builtin FBX import v{_BRIDGE_SCRIPT_VERSION} "
        f"unity_normals={unity_mode}"
    )
    bpy.ops.import_scene.fbx(filepath=path)
    _plog("import via builtin import_scene.fbx")
    return True


def _import_fbx_via_better(path: str) -> bool:
    if bpy.context.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    import_kwargs = _better_fbx_import_kwargs(path)
    ret = bpy.ops.better_import.fbx(**import_kwargs)
    if ret != {"FINISHED"}:
        print(f"BLENDER_BRIDGE_WARN: better_import.fbx returned {ret!r}")
        return False
    unity_mode = _resolve_unity_normal_import_mode(path)
    print(
        f"BLENDER_BRIDGE: Better FBX import v{_BRIDGE_SCRIPT_VERSION} "
        f"unity={unity_mode} better_normal={import_kwargs['my_import_normal']} "
        f"angle={import_kwargs['my_angle']} edge={import_kwargs['my_edge_smoothing']}"
    )
    _finalize_imported_normals_for_unity(path)
    _plog("import via Better FBX (better_import.fbx)")
    return True


def _import_fbx_better_or_builtin(path: str) -> bool:
    """
    Default: Blender built-in FBX for binary Unity assets (normals match Unity viewport).
    Better FBX: ASCII FBX, or opt-in via BRIDGE_FORCE_BETTER_FBX_IMPORT=1.
    Export still prefers Better FBX when available.
    Returns False if import could not be completed.
    """
    if _prefer_builtin_fbx_import(path):
        return _import_fbx_via_builtin(path)

    if _enable_better_fbx_addon():
        try:
            if _import_fbx_via_better(path):
                return True
        except Exception as ex:
            print(f"BLENDER_BRIDGE_WARN: Better FBX import failed, trying builtin: {ex}")

    if _fbx_is_ascii_text(path):
        msg = (
            "ASCII FBX needs the Better FBX Importer addon (better_fbx). "
            "Install/enable it in Blender, or set BRIDGE_BETTER_FBX_MODULE to your folder name."
        )
        print(f"BLENDER_BRIDGE_ERROR: {msg}")
        return False

    print("BLENDER_BRIDGE_WARN: Better FBX unavailable; falling back to builtin FBX import")
    return _import_fbx_via_builtin(path)


def _better_export_fbx_available() -> bool:
    return hasattr(bpy.ops, "better_export") and hasattr(bpy.ops.better_export, "fbx")


def _export_fbx_via_better_unity(filepath: str) -> bool:
    """
    Better FBX exporter preset aligned with Unity workflow.
    Axis: Unity .meta bakeAxisConversion=0 -> Unity axis (default for game FBX).
    Preserves object transforms: use_move_to_origin=False, baseline restore before export.
    """
    if _FORCE_BUILTIN_FBX_EXPORT:
        return False
    if not _enable_better_fbx_addon() or not _better_export_fbx_available():
        return False

    meta = _read_unity_meta_export_settings(filepath)
    axis = _resolve_better_fbx_axis(filepath)
    move_to_origin = _export_move_to_origin(filepath)
    optimize_ge = _export_optimize_for_game_engine()
    only_root_empty = _export_only_root_empty_node()
    global_scale = max(0.0001, float(meta.get("global_scale") or 1.0))

    try:
        if bpy.context.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        ret = bpy.ops.better_export.fbx(
            filepath=filepath,
            my_file_type=".fbx",
            my_fbx_format="binary",
            my_fbx_axis=axis,
            my_material_style="Unity",
            use_selection=False,
            use_active_collection=False,
            use_visible=False,
            use_optimize_for_game_engine=optimize_ge,
            use_reset_mesh_origin=False,
            use_reset_mesh_rotation=False,
            use_only_root_empty_node=only_root_empty,
            use_ignore_armature_node=True,
            use_apply_modifiers=True,
            use_include_armature_deform_modifier=False,
            use_triangulate=False,
            use_raw_normals_and_raw_tangents=False,
            my_edge_smoothing="FBXSDK",
            use_edge_crease=True,
            my_edge_crease_scale=1.0,
            my_separate_files=False,
            use_move_to_origin=move_to_origin,
            use_animation=True,
            use_embed_media=False,
            use_copy_texture=False,
        )
        if ret == {"FINISHED"}:
            print(
                f"BLENDER_BRIDGE: exported FBX v{_BRIDGE_SCRIPT_VERSION} "
                f"axis={axis} optimize_ge={optimize_ge} only_root_empty={only_root_empty} "
                f"move_to_origin={move_to_origin} globalScale={global_scale}"
            )
            return True
        print(f"BLENDER_BRIDGE_WARN: better_export.fbx returned {ret!r}, falling back to builtin")
    except Exception as ex:
        print(f"BLENDER_BRIDGE_WARN: Better FBX export failed, falling back to builtin: {ex}")
    return False


def _is_bridge_mesh_path(path: str) -> bool:
    return path.lower().endswith(_BRIDGE_MESH_SUFFIX)


def _resolve_model_format(path: str) -> str:
    if _is_bridge_mesh_path(path):
        return _BRIDGE_MESH_SUFFIX
    return os.path.splitext(path)[1].lower()


def _import_unity_bridge_mesh(path: str) -> bool:
    """Load Unity Mesh interchange JSON written by BlenderBridgeUnityMesh.cs."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as ex:
        print(f"BLENDER_BRIDGE_ERROR: failed to read bridge-mesh JSON: {ex}")
        return False

    verts_flat = data.get("vertices") or []
    if not verts_flat or len(verts_flat) % 3 != 0:
        print("BLENDER_BRIDGE_ERROR: bridge-mesh has no valid vertices")
        return False

    vertex_count = len(verts_flat) // 3
    vertices = [
        (verts_flat[i * 3], verts_flat[i * 3 + 1], verts_flat[i * 3 + 2])
        for i in range(vertex_count)
    ]

    tris = data.get("triangles") or []
    if len(tris) % 3 != 0:
        print("BLENDER_BRIDGE_ERROR: bridge-mesh triangles length is not a multiple of 3")
        return False
    for idx in tris:
        if idx < 0 or idx >= vertex_count:
            print(f"BLENDER_BRIDGE_ERROR: bridge-mesh triangle index out of range: {idx}")
            return False

    faces = [(tris[i], tris[i + 1], tris[i + 2]) for i in range(0, len(tris), 3)]
    name = (data.get("name") or os.path.splitext(os.path.basename(path))[0] or "UnityMesh").strip()
    # Avoid Blender treating "foo.bridge" as the object name stem of foo.bridge-mesh
    if name.lower().endswith(".bridge"):
        name = name[: -len(".bridge")]

    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(vertices, [], faces)
    mesh.update()

    normals_flat = data.get("normals") or []
    if len(normals_flat) == vertex_count * 3 and mesh.loops:
        try:
            loop_normals = []
            for loop in mesh.loops:
                vi = loop.vertex_index
                loop_normals.append(
                    (
                        normals_flat[vi * 3],
                        normals_flat[vi * 3 + 1],
                        normals_flat[vi * 3 + 2],
                    )
                )
            mesh.normals_split_custom_set(loop_normals)
        except Exception as ex:
            print(f"BLENDER_BRIDGE_WARN: custom normals failed, using Blender defaults: {ex}")

    uvs_flat = data.get("uvs") or []
    if len(uvs_flat) == vertex_count * 2 and mesh.polygons:
        _assign_bridge_uv_layer(mesh, "UVMap", uvs_flat, vertex_count)

        for key, layer_name in (
            ("uvs2", "UV2"),
            ("uvs3", "UV3"),
            ("uvs4", "UV4"),
            ("uvs5", "UV5"),
            ("uvs6", "UV6"),
            ("uvs7", "UV7"),
            ("uvs8", "UV8"),
        ):
            channel_flat = data.get(key) or []
            if len(channel_flat) == vertex_count * 2 and mesh.polygons:
                _assign_bridge_uv_layer(mesh, layer_name, channel_flat, vertex_count)
            elif channel_flat:
                print(
                    f"BLENDER_BRIDGE_WARN: skip {key} "
                    f"(len={len(channel_flat)}, expected {vertex_count * 2})"
                )

    for poly in mesh.polygons:
        poly.use_smooth = True

    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)

    unity_asset = data.get("unity_asset_path") or ""
    uv_names = [layer.name for layer in mesh.uv_layers]
    print(
        f"BLENDER_BRIDGE: bridge-mesh import v{_BRIDGE_SCRIPT_VERSION} "
        f"name={name!r} verts={vertex_count} tris={len(faces)} "
        f"uv_layers={uv_names} unity={unity_asset!r}"
    )
    _write_bridge_normal_baseline(path, data)
    return True


def _assign_bridge_uv_layer(mesh, layer_name: str, uvs_flat: list, vertex_count: int) -> None:
    uv_layer = mesh.uv_layers.new(name=layer_name)
    for poly in mesh.polygons:
        for loop_idx in poly.loop_indices:
            vi = mesh.loops[loop_idx].vertex_index
            if vi < 0 or vi >= vertex_count:
                continue
            uv_layer.data[loop_idx].uv = (uvs_flat[vi * 2], uvs_flat[vi * 2 + 1])


def _bridge_normal_baseline_path(filepath: str) -> str:
    return filepath + _BRIDGE_NORMAL_BASELINE_SUFFIX


def _write_bridge_normal_baseline(filepath: str, data: dict) -> None:
    """Snapshot original Unity normals/vertices/triangles before Blender edits."""
    baseline = {
        "version": 1,
        "unity_asset_path": data.get("unity_asset_path") or "",
        "name": data.get("name") or "",
        "vertex_count": data.get("vertex_count") or 0,
        "vertices": data.get("vertices") or [],
        "normals": data.get("normals") or [],
        "triangles": data.get("triangles") or [],
    }
    baseline_path = _bridge_normal_baseline_path(filepath)
    tmp_path = baseline_path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(baseline, f, indent=2)
            f.write("\n")
        os.replace(tmp_path, baseline_path)
    except OSError as ex:
        print(f"BLENDER_BRIDGE_WARN: failed to write normal baseline: {ex}")


def _read_bridge_normal_baseline(filepath: str) -> dict:
    """Return the original mesh snapshot; fall back to the current interchange file."""
    candidates = [_bridge_normal_baseline_path(filepath), filepath]
    for path in candidates:
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue

        vertex_count = int(data.get("vertex_count") or 0)
        vertices = data.get("vertices") or []
        normals = data.get("normals") or []
        triangles = data.get("triangles") or []
        if not vertex_count:
            vertex_count = len(vertices) // 3 if isinstance(vertices, list) else 0
        if (
            isinstance(vertices, list)
            and isinstance(normals, list)
            and isinstance(triangles, list)
            and len(vertices) == vertex_count * 3
            and len(normals) == vertex_count * 3
            and len(triangles) % 3 == 0
        ):
            return {
                "vertex_count": vertex_count,
                "vertices": vertices,
                "normals": normals,
                "triangles": triangles,
            }
    return {}


def _tri_key(tri: tuple[int, int, int]) -> tuple[int, int, int]:
    return tuple(sorted(tri))


def _tri_orientation(tri: tuple[int, int, int]) -> int | None:
    """Return 0/1 winding sign relative to sorted vertex order; None if degenerate."""
    if len(tri) != 3 or len(set(tri)) != 3:
        return None
    inversions = 0
    for i in range(3):
        for j in range(i + 1, 3):
            if tri[i] > tri[j]:
                inversions += 1
    return inversions % 2


def _match_triangle_orientations(
    current_tris: list[tuple[int, int, int]],
    baseline_tris: list[tuple[int, int, int]],
) -> list[bool] | None:
    """Return True=same winding, False=flipped, or None if topology differs."""
    if len(current_tris) != len(baseline_tris):
        return None

    baseline_by_key: dict[tuple[int, int, int], list[tuple[int, int, int]]] = {}
    for tri in baseline_tris:
        baseline_by_key.setdefault(_tri_key(tri), []).append(tri)

    flags: list[bool] = []
    for tri in current_tris:
        key = _tri_key(tri)
        pool = baseline_by_key.get(key)
        if not pool:
            return None
        baseline_tri = pool.pop(0)
        cur_orient = _tri_orientation(tri)
        base_orient = _tri_orientation(baseline_tri)
        if cur_orient is None or base_orient is None:
            return None
        flags.append(cur_orient == base_orient)
    return flags


def _normalize_vector3(values: tuple[float, float, float]) -> tuple[float, float, float]:
    length = math.sqrt(values[0] * values[0] + values[1] * values[1] + values[2] * values[2]) or 1.0
    return (values[0] / length, values[1] / length, values[2] / length)


def _negate_vector3(values: tuple[float, float, float]) -> tuple[float, float, float]:
    return (-values[0], -values[1], -values[2])


def _vectors_close(a: list[float], b: list[float], epsilon: float = _BRIDGE_POSITION_EPSILON) -> bool:
    if len(a) != 3 or len(b) != 3:
        return False
    return (
        abs(a[0] - b[0]) <= epsilon
        and abs(a[1] - b[1]) <= epsilon
        and abs(a[2] - b[2]) <= epsilon
    )


def _average_loop_normals_per_vertex(mesh) -> list[tuple[float, float, float]]:
    accum = [[0.0, 0.0, 0.0] for _ in range(len(mesh.vertices))]
    counts = [0] * len(mesh.vertices)
    for loop in mesh.loops:
        n = loop.normal
        idx = loop.vertex_index
        accum[idx][0] += float(n.x)
        accum[idx][1] += float(n.y)
        accum[idx][2] += float(n.z)
        counts[idx] += 1

    result = []
    for i, (ax, ay, az) in enumerate(accum):
        c = counts[i] or 1
        result.append(_normalize_vector3((ax / c, ay / c, az / c)))
    return result


def _flatten_vec3_list(values: list[tuple[float, float, float]]) -> list[float]:
    flat: list[float] = []
    for x, y, z in values:
        flat.extend((float(x), float(y), float(z)))
    return flat


def _export_uv_channel_flat(mesh, uv_layer) -> list:
    if uv_layer is None:
        return []
    uv_per_vert = [(0.0, 0.0)] * len(mesh.vertices)
    seen = [False] * len(mesh.vertices)
    for loop in mesh.loops:
        vi = loop.vertex_index
        if seen[vi]:
            continue
        uv = uv_layer.data[loop.index].uv
        uv_per_vert[vi] = (float(uv.x), float(uv.y))
        seen[vi] = True
    flat = []
    for u, v in uv_per_vert:
        flat.extend((u, v))
    return flat


def _export_unity_bridge_mesh(filepath: str) -> None:
    """Write edited scene meshes back to Unity Mesh interchange JSON."""
    mesh_objects = [obj for obj in bpy.context.scene.objects if obj.type == "MESH" and obj.data]
    if not mesh_objects:
        raise RuntimeError("No mesh objects to export for Unity .mesh write-back")

    # Prefer the active mesh; otherwise export the first mesh object.
    active = bpy.context.view_layer.objects.active
    if active is not None and active.type == "MESH" and active in mesh_objects:
        target = active
    else:
        target = mesh_objects[0]

    depsgraph = bpy.context.evaluated_depsgraph_get()
    eval_obj = target.evaluated_get(depsgraph)
    mesh = eval_obj.to_mesh()
    try:
        mesh.calc_loop_triangles()

        # Preserve Unity coordinates: use object-local mesh data (no axis conversion).
        current_vertices = []
        for v in mesh.vertices:
            co = v.co
            current_vertices.append((float(co.x), float(co.y), float(co.z)))

        # Preserve UV layer order: UVMap/first -> uvs, UV2/second -> uvs2, ...
        current_uv_flats = []
        for layer in mesh.uv_layers:
            current_uv_flats.append(_export_uv_channel_flat(mesh, layer))
        while len(current_uv_flats) < 8:
            current_uv_flats.append([])

        current_tri_objects = list(mesh.loop_triangles)
        current_tri_tuples = [tuple(int(v) for v in tri.vertices) for tri in current_tri_objects]
        current_avg_normals = _average_loop_normals_per_vertex(mesh)

        baseline = _read_bridge_normal_baseline(filepath)
        orientation_flags = None
        baseline_vertices_flat = baseline.get("vertices") or []
        baseline_normals_flat = baseline.get("normals") or []
        baseline_tris_flat = baseline.get("triangles") or []
        baseline_vertex_count = int(baseline.get("vertex_count") or 0)
        if baseline_vertex_count != len(current_vertices):
            baseline_vertex_count = len(baseline_vertices_flat) // 3

        baseline_tri_tuples = [
            tuple(int(x) for x in baseline_tris_flat[i : i + 3])
            for i in range(0, len(baseline_tris_flat) - 2, 3)
        ]
        positions_match = (
            baseline_vertex_count == len(current_vertices)
            and len(baseline_vertices_flat) == baseline_vertex_count * 3
            and len(baseline_normals_flat) == baseline_vertex_count * 3
            and len(baseline_tri_tuples) == len(current_tri_tuples)
        )
        if positions_match:
            positions_match = all(
                _vectors_close(
                    current_vertices[i],
                    [
                        float(baseline_vertices_flat[i * 3]),
                        float(baseline_vertices_flat[i * 3 + 1]),
                        float(baseline_vertices_flat[i * 3 + 2]),
                    ],
                )
                for i in range(len(current_vertices))
            )

        if positions_match:
            orientation_flags = _match_triangle_orientations(
                current_tri_tuples, baseline_tri_tuples
            )

        has_winding_changes = orientation_flags is not None and not all(orientation_flags)
        if not has_winding_changes:
            # No flipped faces (or topology/positions changed): keep Blender's current normals.
            normal_mode = "blender-current"
            out_vertex_tuples = list(current_vertices)
            out_normal_tuples = list(current_avg_normals)
            out_triangles = []
            for tri in current_tri_tuples:
                out_triangles.extend(tri)
            out_uv_flats = [list(flat) for flat in current_uv_flats]
        else:
            normal_mode = "preserve-winding"
            baseline_normals = []
            for i in range(baseline_vertex_count):
                baseline_normals.append(
                    _normalize_vector3(
                        (
                            float(baseline_normals_flat[i * 3]),
                            float(baseline_normals_flat[i * 3 + 1]),
                            float(baseline_normals_flat[i * 3 + 2]),
                        )
                    )
                )

            unflipped_count = [0] * len(current_vertices)
            flipped_count = [0] * len(current_vertices)
            for tri, is_same in zip(current_tri_tuples, orientation_flags):
                for vi in tri:
                    if is_same:
                        unflipped_count[vi] += 1
                    else:
                        flipped_count[vi] += 1

            out_vertex_tuples = list(current_vertices)
            out_normal_tuples = []
            for vi in range(len(current_vertices)):
                if unflipped_count[vi] > 0:
                    out_normal_tuples.append(baseline_normals[vi])
                elif flipped_count[vi] > 0:
                    # Every adjacent face was flipped; use Blender's resulting normal.
                    out_normal_tuples.append(_negate_vector3(baseline_normals[vi]))
                else:
                    out_normal_tuples.append(baseline_normals[vi])

            out_triangles = []
            out_uv_flats = [list(flat) for flat in current_uv_flats]
            original_vertex_count = len(current_vertices)
            channel_present = [
                len(flat) == original_vertex_count * 2 for flat in out_uv_flats
            ]

            for tri, is_same in zip(current_tri_tuples, orientation_flags):
                new_tri = []
                if is_same:
                    new_tri.extend(tri)
                else:
                    for vi in tri:
                        mixed = unflipped_count[vi] > 0 and flipped_count[vi] > 0
                        if not mixed:
                            new_tri.append(vi)
                            continue

                        new_index = len(out_vertex_tuples)
                        out_vertex_tuples.append(current_vertices[vi])
                        out_normal_tuples.append(_negate_vector3(baseline_normals[vi]))
                        for channel_index, flat in enumerate(out_uv_flats):
                            if channel_present[channel_index]:
                                flat.extend(
                                    (
                                        flat[vi * 2],
                                        flat[vi * 2 + 1],
                                    )
                                )
                        new_tri.append(new_index)
                out_triangles.extend(new_tri)

        vertices = _flatten_vec3_list(out_vertex_tuples)
        normals = _flatten_vec3_list(out_normal_tuples)
        triangles = out_triangles

        existing = {}
        if os.path.isfile(filepath):
            try:
                with open(filepath, encoding="utf-8") as f:
                    existing = json.load(f)
            except Exception:
                existing = {}

        payload = {
            "version": 2,
            "name": existing.get("name") or target.name,
            "unity_asset_path": existing.get("unity_asset_path") or "",
            "vertex_count": len(out_vertex_tuples),
            "vertices": vertices,
            "normals": normals,
            "uvs": out_uv_flats[0],
            "uvs2": out_uv_flats[1],
            "uvs3": out_uv_flats[2],
            "uvs4": out_uv_flats[3],
            "uvs5": out_uv_flats[4],
            "uvs6": out_uv_flats[5],
            "uvs7": out_uv_flats[6],
            "uvs8": out_uv_flats[7],
            "triangles": triangles,
            "submesh_count": int(existing.get("submesh_count") or 1),
            "submesh_index_counts": existing.get("submesh_index_counts")
            or [len(triangles)],
        }
        # If triangle count no longer matches preserved submesh layout, fall back to one submesh.
        sub_counts = payload["submesh_index_counts"] or []
        if not isinstance(sub_counts, list) or sum(int(c) for c in sub_counts) != len(triangles):
            payload["submesh_count"] = 1
            payload["submesh_index_counts"] = [len(triangles)]

        # Atomic replace so Unity's FileSystemWatcher sees a complete file.
        tmp_path = filepath + ".tmp"
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(payload, f, indent=2)
            f.write("\n")
        os.replace(tmp_path, filepath)
        present = [
            i + 1
            for i, flat in enumerate(out_uv_flats)
            if len(flat) == len(out_vertex_tuples) * 2
        ]
        print(
            f"BLENDER_BRIDGE: bridge-mesh export v{_BRIDGE_SCRIPT_VERSION} "
            f"verts={payload['vertex_count']} tris={len(triangles) // 3} "
            f"normals={normal_mode} uv_channels={present} -> {filepath}"
        )
    finally:
        eval_obj.to_mesh_clear()


class UnityModelExporter:
    def __init__(self, model_path):
        self.model_path = model_path
        self.filename = os.path.basename(model_path)
        self.extension = _resolve_model_format(model_path)

    def load_model(self):
        self.model_path = os.path.normpath(self.model_path)
        self.extension = _resolve_model_format(self.model_path)
        print(f"BLENDER_BRIDGE: load_model v{_BRIDGE_SCRIPT_VERSION} -> {self.filename}")
        _plog(f"load_model begin {self.model_path}")

        if not os.path.isfile(self.model_path):
            print(f"BLENDER_BRIDGE_ERROR: model file not found: {self.model_path!r}")
            return

        if bpy.context.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")

        bpy.ops.object.select_all(action="SELECT")
        bpy.ops.object.delete(use_global=False)
        _plog("startup scene cleared")

        if self.extension == ".fbx":
            if not _import_fbx_better_or_builtin(self.model_path):
                print(f"BLENDER_BRIDGE_ERROR: FBX import failed for '{self.filename}'")
                return
        elif self.extension == ".obj":
            bpy.ops.wm.obj_import(filepath=self.model_path)
        elif self.extension == ".dae":
            bpy.ops.wm.collada_import(filepath=self.model_path)
        elif self.extension == _BRIDGE_MESH_SUFFIX:
            if not _import_unity_bridge_mesh(self.model_path):
                print(f"BLENDER_BRIDGE_ERROR: bridge-mesh import failed for '{self.filename}'")
                return
        else:
            print(f"Unsupported format '{self.extension}'")
            return

        _plog("import finished")

        bpy.context.scene["unity_model_path"] = self.model_path
        bpy.context.scene["unity_model_format"] = self.extension

        bpy.context.tool_settings.mesh_select_mode = (False, False, True)
        bpy.ops.object.select_all(action="SELECT")

        if LOAD_TEXTURES:
            self.apply_textures()

        if not _SKIP_VIEW_FRAME:
            screen = bpy.context.screen
            if screen:
                for area in screen.areas:
                    if area.type != "VIEW_3D":
                        continue
                    for space in area.spaces:
                        if space.type == "VIEW_3D":
                            space.shading.type = "SOLID"
                            if LOAD_TEXTURES:
                                space.shading.color_type = "TEXTURE"
                    region = area.regions[-1] if area.regions else None
                    if region is None:
                        continue
                    override = {"area": area, "region": region}
                    try:
                        with bpy.context.temp_override(**override):
                            bpy.ops.view3d.view_selected()
                    except Exception as ex:
                        print(f"BLENDER_BRIDGE_WARN: view_selected failed: {ex}")

        bpy.ops.object.select_all(action="DESELECT")

        try:
            bpy.ops.ed.undo_history_clear()
        except Exception as ex:
            # Blender 5.x may omit this operator; --background also often cannot run it.
            if _PROFILE:
                print(f"[BRIDGE_PROFILE] undo_history_clear skipped: {ex}")

        _plog("load_model end")
        _capture_transform_baseline()
        print(f"Loaded '{self.filename}'")

    def apply_textures(self):
        search_dir = os.path.normpath(TEXTURE_PATH)

        if not os.path.exists(search_dir):
            print(f"Texture path not found: {search_dir}")
            return

        for obj in bpy.context.selected_objects:
            if obj.type == "MESH":
                for slot in obj.material_slots:
                    if slot.material:
                        self.setup_material_node(slot.material, search_dir)

    def setup_material_node(self, mat, search_dir):
        mat.use_nodes = True
        nodes = mat.node_tree.nodes
        links = mat.node_tree.links

        principled = next((n for n in nodes if n.type == "BSDF_PRINCIPLED"), None)
        if not principled:
            return

        if "Roughness" in principled.inputs:
            principled.inputs["Roughness"].default_value = 1.0

        for ext in TEXTURE_EXTENSIONS:
            image_name = f"{mat.name}{ext}"
            full_image_path = os.path.join(search_dir, image_name)

            if os.path.exists(full_image_path):
                try:
                    img = bpy.data.images.load(full_image_path)
                    tex_node = next((n for n in nodes if n.type == "TEX_IMAGE"), None)

                    if not tex_node:
                        tex_node = nodes.new("ShaderNodeTexImage")
                        tex_node.location = (-300, 300)

                    tex_node.image = img

                    if "Base Color" in principled.inputs:
                        links.new(tex_node.outputs["Color"], principled.inputs["Base Color"])

                    print(f"Applied texture '{image_name}'")
                    break
                except Exception as e:
                    print(f"Failed to load '{image_name}': {e}")

    @staticmethod
    def export_to_unity(filepath, file_format):
        """Export scene back to Unity in the original format"""
        if bpy.context.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        _restore_transform_baseline()

        if file_format == ".fbx":
            if _export_fbx_via_better_unity(filepath):
                return
            meta = _read_unity_meta_export_settings(filepath)
            global_scale = max(0.0001, float(meta.get("global_scale") or 1.0))
            bpy.ops.export_scene.fbx(
                filepath=filepath,
                global_scale=global_scale,
                apply_unit_scale=True,
                apply_scale_options="FBX_SCALE_UNITS",
                object_types={"ARMATURE", "MESH", "EMPTY"},
                add_leaf_bones=False,
                primary_bone_axis="Y",
                secondary_bone_axis="X",
                armature_nodetype="NULL",
                bake_anim=True,
                bake_space_transform=False,
                axis_forward="-Z",
                axis_up="Y",
            )
            print(
                f"BLENDER_BRIDGE: exported FBX via builtin "
                f"bakeAxisConversion={meta['bake_axis_conversion']} globalScale={global_scale}"
            )
        elif file_format == ".obj":
            bpy.ops.wm.obj_export(
                filepath=filepath,
                export_animation=False,
                apply_modifiers=True,
                forward_axis="NEGATIVE_Z",
                up_axis="Y",
            )
        elif file_format == ".dae":
            bpy.ops.wm.collada_export(filepath=filepath, apply_modifiers=True)
        elif file_format == _BRIDGE_MESH_SUFFIX or _is_bridge_mesh_path(filepath):
            _export_unity_bridge_mesh(filepath)
        else:
            raise ValueError(f"Unsupported export format '{file_format}'")


class WM_OT_save_unity_model(bpy.types.Operator):
    bl_idname = "wm.save_unity_model"
    bl_label = "Save Unity Model"
    bl_description = "Export back to Unity"
    bl_options = {"REGISTER"}

    from_shortcut: bpy.props.BoolProperty(default=False)  # type: ignore

    def execute(self, context):
        if "unity_model_path" not in context.scene:
            bpy.ops.wm.save_mainfile("INVOKE_DEFAULT")
            return {"FINISHED"}

        path = context.scene["unity_model_path"]
        file_format = context.scene.get("unity_model_format", ".fbx")

        try:
            UnityModelExporter.export_to_unity(path, file_format)
            self.report({"INFO"}, f"Saved '{os.path.basename(path)}' to Unity")

            if self.from_shortcut and CLOSE_AFTER_QUICK_SAVE:
                bpy.ops.wm.quit_blender()
            elif not self.from_shortcut and CLOSE_AFTER_MANUAL_SAVE:
                bpy.ops.wm.quit_blender()

        except Exception as e:
            self.report({"ERROR"}, f"Save failed '{str(e)}'")
            return {"CANCELLED"}

        return {"FINISHED"}


def menu_func_export(self, context):
    if "unity_model_path" in context.scene:
        file_format = context.scene.get("unity_model_format", ".fbx")
        if file_format == _BRIDGE_MESH_SUFFIX:
            label = "Unity Mesh (back to original .mesh asset)"
        else:
            label = f"{str(file_format)[1:].upper()} (back to original Unity asset)"
        self.layout.operator(
            WM_OT_save_unity_model.bl_idname,
            text=label,
            icon="EXPORT",
        )


addon_keymaps = []


def register():
    bpy.utils.register_class(WM_OT_save_unity_model)
    bpy.types.TOPBAR_MT_file_export.append(menu_func_export)

    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if kc:
        km = kc.keymaps.new(name="Window", space_type="EMPTY")
        kmi = km.keymap_items.new(WM_OT_save_unity_model.bl_idname, "S", "PRESS", ctrl=True)
        kmi.properties.from_shortcut = True
        addon_keymaps.append((km, kmi))


def unregister():
    for km, kmi in addon_keymaps:
        km.keymap_items.remove(kmi)
    addon_keymaps.clear()

    bpy.types.TOPBAR_MT_file_export.remove(menu_func_export)
    bpy.utils.unregister_class(WM_OT_save_unity_model)


def _injector_script_path() -> str | None:
    path = os.environ.get("BLENDER_BRIDGE_INJECTOR", "").strip()
    if path and os.path.isfile(path):
        return path
    try:
        here = os.path.abspath(__file__)
        if os.path.isfile(here):
            return here
    except NameError:
        pass
    return None


def _load_injector_module_from_disk():
    path = _injector_script_path()
    if not path:
        return None
    module_name = f"blender_bridge_injector_{time.time_ns()}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_unity_load(model_path: str):
    model_path = os.path.normpath(model_path)
    hot_reload = os.environ.get("BLENDER_BRIDGE_HOT_RELOAD", "").lower() in ("1", "true", "yes")
    if hot_reload:
        mod = _load_injector_module_from_disk()
        if mod is not None and hasattr(mod, "UnityModelExporter"):
            mod.UnityModelExporter(model_path).load_model()
            return None
    UnityModelExporter(model_path).load_model()
    return None


if __name__ == "__main__":
    if __file__:
        os.environ.setdefault("BLENDER_BRIDGE_INJECTOR", os.path.abspath(__file__))
    register()
    _ensure_bridge_server()

    if "--" in sys.argv:
        argv = sys.argv[sys.argv.index("--") + 1 :]
        if argv:
            model_path = argv[0]
            # Let the UI appear before heavy import when launched from Unity (non-background).
            if bpy.app.background:
                _run_unity_load(model_path)
            else:
                bpy.app.timers.register(lambda: _run_unity_load(model_path), first_interval=0.05)
