# Blender Bridge

[English](#english) · [中文](#中文)

---

Open `.FBX`, `.OBJ`, `.DAE`, and Unity Mesh (`.mesh`) assets in Blender from Unity, then export straight back to the original asset. Ctrl+S in Blender is overridden so saving writes to the Unity project file instead of a `.blend`.

**Version:** 0.1.9 · **Unity:** 2022.3+ · **Platform:** Windows

## Installing

1. Open Unity and go to **Window → Package Manager**
2. Click the **+** button in the top-left
3. Choose **Add package from git URL**
4. Enter:
  ```
   https://github.com/Zhy-3DEnv/blender-bridge-for-unity.git
  ```
5. Done — configure your Blender path (see [Configuration](#configuration)).

## Quick start

1. Set your Blender executable path in `Editor/BlenderBridgeProcessor.cs` (see [Configuration](#configuration)).
2. Double-click an `.fbx`, `.obj`, `.dae`, or `.mesh` asset in the Unity Project window.
3. Edit the model in Blender.
4. Press **Ctrl+S** or use **File → Export → … (back to original Unity asset)** to write back to Unity.

## Features

- **Double-click to edit** — open supported 3D assets directly from the Unity Project window
- **Unity Mesh (.mesh)** — opens readable Mesh assets via a lossless JSON interchange under `Library/BlenderBridge/` (handles Unity CompressedMesh by reading through the Mesh API)
- **Hot reuse** — if Blender is already running, the next double-click imports into the same window instead of launching another instance
- **Auto focus** — when hot-reusing Blender, the window is brought to the foreground automatically
- **Save back to Unity** — Ctrl+S and the File → Export menu export to the original asset path
- **Format preserved** — round-trip stays in the same format (FBX / OBJ / DAE / Mesh)
- **Unity-aware FBX** — reads Unity `.meta` importer settings (normals, smooth angle, axis) for better round-trip fidelity
- **Better FBX support** — uses the [Better FBX Importer & Exporter](https://blendermarket.com/products/better-fbx-importer-exporter) addon when installed; falls back to Blender's built-in FBX importer/exporter
- **Transform preservation** — restores object transforms before export and bakes static FBX geometric pivots so Unity root rotation/position stay unchanged
- **Viewport framing** — zooms the 3D view to the imported model
- **Face select mode** — mesh edit mode defaults to face selection
- **No splash screen** — Blender opens without the splash
- **Optional texture loading** — can auto-apply textures from a folder (off by default)

[https://github.com/user-attachments/assets/c8879a20-0098-4138-a847-a047c0887f8a](https://github.com/user-attachments/assets/c8879a20-0098-4138-a847-a047c0887f8a)

## Requirements


| Requirement          | Notes                                                                                                     |
| -------------------- | --------------------------------------------------------------------------------------------------------- |
| **Windows**          | Editor integration uses Windows process and window APIs                                                   |
| **Blender 5.x**      | Default path targets Blender 5.1; other versions work if you update the path                              |
| **Better FBX addon** | Strongly recommended for FBX (especially ASCII FBX and correct normals). Built-in FBX is used as fallback |


## Configuration

There is no Unity Preferences panel yet. Edit the source files below.

### Blender executable path

In `Editor/BlenderBridgeProcessor.cs`, set `BLENDER_PATH` to your Blender install:

```csharp
private static readonly string BLENDER_PATH = @"C:\Program Files\Blender Foundation\Blender 5.1\blender.exe";
```

Common locations:

- Standard install: `C:\Program Files\Blender Foundation\Blender X.X\blender.exe`
- Steam: `C:\Program Files (x86)\Steam\steamapps\common\Blender\blender.exe`

### Close Blender after save

In `Editor/blender-bridge-injector.py`:

```python
CLOSE_AFTER_QUICK_SAVE = True   # Ctrl+S
CLOSE_AFTER_MANUAL_SAVE = False # File → Export menu
```

### Texture loading

In `Editor/blender-bridge-injector.py`:

```python
LOAD_TEXTURES = True
TEXTURE_PATH = r"C:\path\to\your\textures"
```

Textures are matched by material name + extension (`.png`, `.jpg`, etc.).

### Unity Mesh (`.mesh`) requirements

- The Mesh asset must be **Read/Write Enabled** (`isReadable`).
- Double-clicking a `.mesh` writes a temporary `Library/BlenderBridge/*.bridge-mesh` interchange file, opens it in Blender, and on Ctrl+S writes geometry back into the original Mesh asset.
- Unity CompressedMesh YAML is never parsed by Blender; Unity decompresses via the Mesh API.
- When a face's normal is flipped in Blender, unchanged vertices keep their original Unity normals; only the flipped face's shared vertices are split so adjacent faces keep their previous shading.

### Advanced (environment variables)


| Variable                          | Purpose                                             |
| --------------------------------- | --------------------------------------------------- |
| `BRIDGE_BLENDER_PORT`             | TCP port for hot reuse (default `35971`)            |
| `BRIDGE_BETTER_FBX_MODULE`        | Better FBX addon folder name (default `better_fbx`) |
| `BRIDGE_FORCE_BUILTIN_FBX`        | Force built-in FBX import                           |
| `BRIDGE_FORCE_BUILTIN_FBX_EXPORT` | Force built-in FBX export                           |
| `BRIDGE_EXPORT_FBX_AXIS`          | Better FBX export axis (`MayaZUp`, `Unity`, etc.)   |
| `BLENDER_BRIDGE_PROFILE`          | Enable import timing logs                           |


## How it works

```
Unity double-click
    │
    ├─ Blender already listening on localhost:35971?
    │       └─ Yes → TCP IMPORT → load model + focus window
    │
    └─ No → launch Blender with blender-bridge-injector.py
                └─ script registers Ctrl+S, starts TCP server for reuse
```

## Development tools

`Tools/BlenderBridgeProfile/` contains PowerShell/Python scripts for batch testing, TCP flow checks, and import profiling. These are for development only and are not required for normal use.

## Credits

Based on work by [FleshMobProductions](https://gist.github.com/FleshMobProductions/f598096b705f6a9c96beb58e284303f1). Extended with hot reuse, Unity meta sync, Better FBX integration, transform baseline restore, and profiling tools.

## License

MIT

---

# Blender Bridge（中文）

在 Unity 中双击 `.FBX`、`.OBJ`、`.DAE`、Unity Mesh（`.mesh`）资产即可用 Blender 打开编辑；保存时直接写回 Unity 项目里的原文件，而不是存成 `.blend`。Blender 中的 **Ctrl+S** 已被桥接脚本接管。

**版本：** 0.1.9 · **Unity：** 2022.3+ · **平台：** Windows

## 安装

1. 打开 Unity，进入 **Window → Package Manager**（窗口 → 包管理器）
2. 点击左上角 **+** 按钮
3. 选择 **Add package from git URL**（从 Git URL 添加包）
4. 填入：
  ```
   https://github.com/Zhy-3DEnv/blender-bridge-for-unity.git
  ```
5. 完成。记得配置 Blender 路径（见下方 [配置说明](#配置说明)）。

## 快速上手

1. 在 `Editor/BlenderBridgeProcessor.cs` 中设置 Blender 可执行文件路径（见 [配置说明](#配置说明)）。
2. 在 Unity **Project** 窗口双击 `.fbx`、`.obj`、`.dae` 或 `.mesh` 资产。
3. 在 Blender 中编辑模型。
4. 按 **Ctrl+S**，或使用 **File → Export → … (back to original Unity asset)** 导出回 Unity。

## 功能特性

- **双击编辑** — 从 Unity 工程窗口直接打开支持的 3D 资产
- **Unity Mesh（.mesh）** — 支持可读 Mesh 资产；通过 `Library/BlenderBridge/` 下的 JSON 中转文件往返（Unity CompressedMesh 由 Mesh API 解压，Blender 不直接解析 YAML）
- **热复用** — Blender 已在运行时，再次双击会导入到同一窗口，不会重复启动新实例
- **自动置前** — 热复用导入时，Blender 窗口会自动弹到最前面
- **保存回 Unity** — Ctrl+S 和导出菜单都会写回原资产路径
- **格式保持不变** — 往返编辑保持原格式（FBX / OBJ / DAE / Mesh）
- **读取 Unity 导入设置** — 从 `.meta` 读取法线、平滑角度、轴向等，提升往返一致性
- **Better FBX 支持** — 已安装 [Better FBX Importer & Exporter](https://blendermarket.com/products/better-fbx-importer-exporter) 时优先使用；否则回退到 Blender 内置 FBX 导入/导出
- **变换保持** — 导出前恢复物体变换，并烘焙静态 FBX 的几何枢轴，避免 Unity 根节点产生旋转和位移
- **视口自动聚焦** — 导入后自动缩放到模型
- **默认面选择模式** — 进入网格编辑时默认为面选择
- **无启动闪屏** — 打开 Blender 时不显示 Splash
- **可选纹理加载** — 可从指定文件夹自动匹配贴图（默认关闭）

## 环境要求


| 要求                | 说明                                        |
| ----------------- | ----------------------------------------- |
| **Windows**       | 编辑器集成依赖 Windows 进程与窗口 API                 |
| **Blender 5.x**   | 默认路径为 Blender 5.1；其他版本需手动修改路径             |
| **Better FBX 插件** | 强烈建议安装（ASCII FBX 与法线一致性尤其需要）；未安装时使用内置 FBX |


## 配置说明

目前没有 Unity 偏好设置面板，需直接修改源码。

### Blender 可执行文件路径

在 `Editor/BlenderBridgeProcessor.cs` 中修改 `BLENDER_PATH`：

```csharp
private static readonly string BLENDER_PATH = @"C:\Program Files\Blender Foundation\Blender 5.1\blender.exe";
```

常见路径：

- 官方安装：`C:\Program Files\Blender Foundation\Blender X.X\blender.exe`
- Steam 版：`C:\Program Files (x86)\Steam\steamapps\common\Blender\blender.exe`

### 保存后是否关闭 Blender

在 `Editor/blender-bridge-injector.py` 中：

```python
CLOSE_AFTER_QUICK_SAVE = True   # Ctrl+S 快捷保存
CLOSE_AFTER_MANUAL_SAVE = False # File → Export 菜单导出
```

### 纹理加载

在 `Editor/blender-bridge-injector.py` 中：

```python
LOAD_TEXTURES = True
TEXTURE_PATH = r"C:\path\to\your\textures"
```

贴图按 **材质名 + 扩展名**（`.png`、`.jpg` 等）匹配。

### Unity Mesh（`.mesh`）要求

- Mesh 资产必须开启 **Read/Write Enabled**（`isReadable`）。
- 双击 `.mesh` 时，会在 `Library/BlenderBridge/` 生成 JSON 中转文件 `*.bridge-mesh` 并在 Blender 中打开；Ctrl+S 后把几何写回原 Mesh 资产。
- Blender 不直接解析 Unity CompressedMesh YAML，而是由 Unity Mesh API 解压后再中转。
- 在 Blender 中翻转某个面的法线时，未改动的顶点会保留原有 Unity 法线；仅被翻转面共用的顶点会被拆分，从而让相邻面的原有光照保持不变。

### 高级选项（环境变量）

在 Blender 进程上设置，用于调试或覆盖默认行为：


| 变量                                | 用途                                   |
| --------------------------------- | ------------------------------------ |
| `BRIDGE_BLENDER_PORT`             | 热复用 TCP 端口（默认 `35971`）               |
| `BRIDGE_BETTER_FBX_MODULE`        | Better FBX 插件文件夹名（默认 `better_fbx`）   |
| `BRIDGE_FORCE_BUILTIN_FBX`        | 强制使用内置 FBX 导入                        |
| `BRIDGE_FORCE_BUILTIN_FBX_EXPORT` | 强制使用内置 FBX 导出                        |
| `BRIDGE_EXPORT_FBX_AXIS`          | Better FBX 导出轴向（`MayaZUp`、`Unity` 等） |
| `BLENDER_BRIDGE_PROFILE`          | 开启导入耗时日志                             |


## 工作原理

```
Unity 双击资产
    │
    ├─ Blender 是否已在 localhost:35971 监听？
    │       └─ 是 → TCP 发送 IMPORT → 加载模型并置前窗口
    │
    └─ 否 → 启动 Blender 并执行 blender-bridge-injector.py
                └─ 注册 Ctrl+S、启动 TCP 服务供后续热复用
```

## 开发工具

`Tools/BlenderBridgeProfile/` 目录下有 PowerShell / Python 脚本，用于批量测试、TCP 流程检查和导入性能分析。仅供开发调试，日常使用无需关心。

## 致谢

基于 [FleshMobProductions](https://gist.github.com/FleshMobProductions/f598096b705f6a9c96beb58e284303f1) 的原始脚本。本分支扩展了热复用、Unity meta 同步、Better FBX 集成、变换基线恢复与性能分析工具。

## 许可证

MIT
