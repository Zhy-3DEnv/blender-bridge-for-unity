import bpy
import os
import queue
import socket
import sys
import threading
import time

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
# If set, Ctrl+S still uses bpy.ops.export_scene.fbx instead of better_export.fbx
_FORCE_BUILTIN_FBX_EXPORT = os.environ.get("BRIDGE_FORCE_BUILTIN_FBX_EXPORT", "").lower() in ("1", "true", "yes")
_VALID_BETTER_EXPORT_AXES = frozenset({"MayaZUp", "OpenGL", "Unity", "Unreal1", "Unreal2"})

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


def _bridge_process_queue():
    """Runs on the main thread; one import per tick."""
    try:
        path = _bridge_cmd_queue.get_nowait()
    except queue.Empty:
        return 0.05
    try:
        UnityModelExporter(path).load_model()
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
            data = b""
            while b"\n" not in data:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
            line = data.decode("utf-8", errors="replace").split("\n", 1)[0].strip()
            if line == "PING":
                conn.sendall(b"PONG\n")
            elif line.startswith("IMPORT|"):
                raw = line[7:].strip()
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


def _import_fbx_better_or_builtin(path: str) -> bool:
    """
    Prefer Better FBX (ASCII + binary); fall back to bpy.ops.import_scene.fbx for binary only.
    Returns False if import could not be completed.
    """
    if not _FORCE_BUILTIN_FBX:
        if _enable_better_fbx_addon():
            try:
                if bpy.context.mode != "OBJECT":
                    bpy.ops.object.mode_set(mode="OBJECT")
                ret = bpy.ops.better_import.fbx(filepath=path)
                if ret == {"FINISHED"}:
                    _plog("import via Better FBX (better_import.fbx)")
                    return True
                print(f"BLENDER_BRIDGE_WARN: better_import.fbx returned {ret!r}, trying builtin importer")
            except Exception as ex:
                print(f"BLENDER_BRIDGE_WARN: Better FBX import failed, trying builtin: {ex}")

    if _fbx_is_ascii_text(path):
        msg = (
            "ASCII FBX needs the Better FBX Importer addon (better_fbx). "
            "Install/enable it in Blender, or set BRIDGE_BETTER_FBX_MODULE to your folder name, "
            "or use BRIDGE_FORCE_BUILTIN_FBX=1 with FBX Binary assets only."
        )
        print(f"BLENDER_BRIDGE_ERROR: {msg}")
        return False

    bpy.ops.import_scene.fbx(filepath=path)
    _plog("import via builtin import_scene.fbx")
    return True


def _better_export_fbx_available() -> bool:
    return hasattr(bpy.ops, "better_export") and hasattr(bpy.ops.better_export, "fbx")


def _export_fbx_via_better_unity(filepath: str) -> bool:
    """
    Better FBX exporter preset aligned with Unity workflow (see user Game Engine / Mesh / Edge / Batch options).
    Axis preset: BRIDGE_EXPORT_FBX_AXIS (default MayaZUp); use 'Unity' if you need Better's Unity-facing rotation.
    """
    if _FORCE_BUILTIN_FBX_EXPORT:
        return False
    if not _enable_better_fbx_addon() or not _better_export_fbx_available():
        return False
    axis = (os.environ.get("BRIDGE_EXPORT_FBX_AXIS") or "MayaZUp").strip()
    if axis not in _VALID_BETTER_EXPORT_AXES:
        axis = "MayaZUp"
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
            use_optimize_for_game_engine=True,
            use_reset_mesh_origin=False,
            use_reset_mesh_rotation=False,
            use_only_root_empty_node=True,
            use_ignore_armature_node=True,
            use_apply_modifiers=True,
            use_include_armature_deform_modifier=False,
            use_triangulate=False,
            use_raw_normals_and_raw_tangents=False,
            my_edge_smoothing="FBXSDK",
            use_edge_crease=True,
            my_edge_crease_scale=1.0,
            my_separate_files=False,
            use_move_to_origin=True,
            use_animation=True,
            use_embed_media=False,
            use_copy_texture=False,
        )
        if ret == {"FINISHED"}:
            print("BLENDER_BRIDGE: exported FBX via Better FBX (Unity-oriented preset)")
            return True
        print(f"BLENDER_BRIDGE_WARN: better_export.fbx returned {ret!r}, falling back to builtin")
    except Exception as ex:
        print(f"BLENDER_BRIDGE_WARN: Better FBX export failed, falling back to builtin: {ex}")
    return False


class UnityModelExporter:
    def __init__(self, model_path):
        self.model_path = model_path
        self.filename = os.path.basename(model_path)
        self.extension = os.path.splitext(model_path)[1].lower()

    def load_model(self):
        _plog(f"load_model begin {self.model_path}")

        if bpy.context.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")

        bpy.ops.object.select_all(action="SELECT")
        bpy.ops.object.delete(use_global=False)
        _plog("startup scene cleared")

        if self.extension == ".fbx":
            if not _import_fbx_better_or_builtin(self.model_path):
                return
        elif self.extension == ".obj":
            bpy.ops.wm.obj_import(filepath=self.model_path)
        elif self.extension == ".dae":
            bpy.ops.wm.collada_import(filepath=self.model_path)
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
        if file_format == ".fbx":
            if _export_fbx_via_better_unity(filepath):
                return
            bpy.ops.export_scene.fbx(
                filepath=filepath,
                global_scale=1.0,
                apply_unit_scale=True,
                apply_scale_options="FBX_SCALE_UNITS",
                object_types={"ARMATURE", "MESH", "EMPTY"},
                add_leaf_bones=False,
                primary_bone_axis="Y",
                secondary_bone_axis="X",
                armature_nodetype="NULL",
                bake_anim=True,
                axis_forward="-Z",
                axis_up="Y",
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
        file_format = context.scene.get("unity_model_format", ".fbx").upper()
        self.layout.operator(
            WM_OT_save_unity_model.bl_idname,
            text=f"{file_format[1:]} (back to original Unity asset)",
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


def _run_unity_load(model_path: str):
    UnityModelExporter(model_path).load_model()
    return None


if __name__ == "__main__":
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
