"""Pure Python: FBX header + size (no Blender). Run: python analyze_fbx_file.py <path.fbx> [out.json]"""

from __future__ import annotations

import json
import os
import sys


def classify_fbx(path: str) -> dict:
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        head = f.read(64)
    if head.startswith(b"Kaydara FBX Binary"):
        enc = "binary"
        preview = "Kaydara FBX Binary"
    elif head.lstrip().startswith(b"; FBX") or (b";" in head[:4] and b"FBX" in head[:32]):
        enc = "ascii_text"
        preview = head[:48].decode("ascii", errors="replace")
    else:
        enc = "unknown"
        preview = head[:24].hex()
    note = None
    if enc == "ascii_text":
        note = (
            "Blender 3D 5.x built-in FBX importer rejects ASCII FBX "
            "(RuntimeError: ASCII FBX files are not supported). "
            "Re-export from Unity or DCC as FBX Binary, or use Blender < 4.x with legacy importer."
        )
    return {
        "path": os.path.abspath(path),
        "size_bytes": size,
        "fbx_encoding": enc,
        "header_preview": preview,
        "note": note,
    }


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python analyze_fbx_file.py <file.fbx> [out.json]", file=sys.stderr)
        return 2
    path = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else None
    if not os.path.isfile(path):
        print(json.dumps({"error": "file not found", "path": path}))
        return 1
    data = classify_fbx(path)
    text = json.dumps(data, indent=2, ensure_ascii=False)
    if out:
        with open(out, "w", encoding="utf-8") as f:
            f.write(text)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        text = json.dumps(data, indent=2, ensure_ascii=True)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
