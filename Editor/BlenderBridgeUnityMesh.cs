using System;
using System.Collections.Generic;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using UnityEditor;
using UnityEngine;

/// <summary>
/// Round-trip Unity Mesh (.mesh) assets through Blender via a lossless JSON interchange file
/// (Library/BlenderBridge/*.bridge-mesh). Unity decompresses Mesh API data; Blender never
/// parses Unity's CompressedMesh YAML.
/// </summary>
[InitializeOnLoad]
public static class BlenderBridgeUnityMesh
{
    public const string InterchangeExtension = ".bridge-mesh";

    private const int BridgeMeshVersion = 1;
    private static readonly Dictionary<string, string> PendingByInterchange =
        new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);

    private static FileSystemWatcher _watcher;
    private static readonly object WatcherLock = new object();
    private static readonly HashSet<string> DirtyInterchangePaths =
        new HashSet<string>(StringComparer.OrdinalIgnoreCase);
    private static readonly Dictionary<string, double> IgnoreWriteBackUntil =
        new Dictionary<string, double>(StringComparer.OrdinalIgnoreCase);

    static BlenderBridgeUnityMesh()
    {
        EditorApplication.update += FlushDirtyInterchanges;
        AssemblyReloadEvents.beforeAssemblyReload += DisposeWatcher;
    }

    public static bool TryOpen(string assetPath, out string interchangeFullPath, out string error)
    {
        interchangeFullPath = null;
        error = null;

        Mesh mesh = AssetDatabase.LoadAssetAtPath<Mesh>(assetPath);
        if (mesh == null)
        {
            error = $"Not a Mesh asset: '{assetPath}'";
            return false;
        }

        if (!mesh.isReadable)
        {
            error =
                $"Mesh '{assetPath}' is not readable (Read/Write Off). " +
                "Enable Read/Write on the mesh asset before editing in Blender.";
            return false;
        }

        try
        {
            Vector3[] vertices = mesh.vertices;
            if (vertices == null || vertices.Length == 0)
            {
                error = $"Mesh '{assetPath}' has no vertices.";
                return false;
            }

            interchangeFullPath = GetInterchangePath(assetPath);
            Directory.CreateDirectory(Path.GetDirectoryName(interchangeFullPath));

            BridgeMeshData data = CaptureMesh(mesh, assetPath);
            WriteInterchange(interchangeFullPath, data);

            string normalized = NormalizePath(interchangeFullPath);
            PendingByInterchange[normalized] = assetPath;
            // Ignore watcher events from this export for a short window.
            IgnoreWriteBackUntil[normalized] = EditorApplication.timeSinceStartup + 1.5;
            EnsureWatcher(Path.GetDirectoryName(interchangeFullPath));
            return true;
        }
        catch (Exception ex)
        {
            error = ex.Message;
            return false;
        }
    }

    public static bool IsInterchangePath(string path)
    {
        return !string.IsNullOrEmpty(path)
            && path.EndsWith(InterchangeExtension, StringComparison.OrdinalIgnoreCase);
    }

    private static BridgeMeshData CaptureMesh(Mesh mesh, string assetPath)
    {
        Vector3[] vertices = mesh.vertices;
        Vector3[] normals = mesh.normals;
        Vector2[] uvs = mesh.uv;
        int[] triangles = mesh.triangles;

        var data = new BridgeMeshData
        {
            version = BridgeMeshVersion,
            name = string.IsNullOrEmpty(mesh.name) ? Path.GetFileNameWithoutExtension(assetPath) : mesh.name,
            unity_asset_path = assetPath.Replace('\\', '/'),
            vertex_count = vertices.Length,
            vertices = Flatten(vertices),
            normals = normals != null && normals.Length == vertices.Length ? Flatten(normals) : Array.Empty<float>(),
            uvs = uvs != null && uvs.Length == vertices.Length ? Flatten(uvs) : Array.Empty<float>(),
            triangles = triangles ?? Array.Empty<int>(),
            submesh_count = mesh.subMeshCount,
            submesh_index_counts = new int[mesh.subMeshCount]
        };

        for (int i = 0; i < mesh.subMeshCount; i++)
        {
            data.submesh_index_counts[i] = (int)mesh.GetIndexCount(i);
        }

        return data;
    }

    private static void WriteInterchange(string path, BridgeMeshData data)
    {
        string json = JsonUtility.ToJson(data, true);
        File.WriteAllText(path, json, new UTF8Encoding(encoderShouldEmitUTF8Identifier: false));
    }

    private static BridgeMeshData ReadInterchange(string path)
    {
        string json = File.ReadAllText(path, Encoding.UTF8);
        return JsonUtility.FromJson<BridgeMeshData>(json);
    }

    private static string GetInterchangePath(string assetPath)
    {
        string projectRoot = Path.GetDirectoryName(Application.dataPath);
        string dir = Path.Combine(projectRoot, "Library", "BlenderBridge");
        string hash;
        using (SHA1 sha = SHA1.Create())
        {
            byte[] bytes = sha.ComputeHash(Encoding.UTF8.GetBytes(assetPath.Replace('\\', '/')));
            hash = BitConverter.ToString(bytes).Replace("-", "").Substring(0, 12).ToLowerInvariant();
        }

        string safeName = Path.GetFileNameWithoutExtension(assetPath);
        foreach (char c in Path.GetInvalidFileNameChars())
        {
            safeName = safeName.Replace(c, '_');
        }

        return Path.GetFullPath(Path.Combine(dir, $"{safeName}_{hash}{InterchangeExtension}"));
    }

    private static void EnsureWatcher(string directory)
    {
        lock (WatcherLock)
        {
            if (_watcher != null)
            {
                return;
            }

            _watcher = new FileSystemWatcher(directory, "*" + InterchangeExtension)
            {
                NotifyFilter = NotifyFilters.LastWrite | NotifyFilters.Size | NotifyFilters.FileName,
                IncludeSubdirectories = false,
                EnableRaisingEvents = true
            };
            _watcher.Changed += OnInterchangeFileEvent;
            _watcher.Created += OnInterchangeFileEvent;
            _watcher.Renamed += OnInterchangeRenamed;
        }
    }

    private static void DisposeWatcher()
    {
        lock (WatcherLock)
        {
            if (_watcher == null)
            {
                return;
            }

            _watcher.EnableRaisingEvents = false;
            _watcher.Changed -= OnInterchangeFileEvent;
            _watcher.Created -= OnInterchangeFileEvent;
            _watcher.Renamed -= OnInterchangeRenamed;
            _watcher.Dispose();
            _watcher = null;
        }
    }

    private static void OnInterchangeRenamed(object sender, RenamedEventArgs e)
    {
        QueueDirty(e.FullPath);
    }

    private static void OnInterchangeFileEvent(object sender, FileSystemEventArgs e)
    {
        QueueDirty(e.FullPath);
    }

    private static void QueueDirty(string fullPath)
    {
        if (!IsInterchangePath(fullPath))
        {
            return;
        }

        lock (DirtyInterchangePaths)
        {
            DirtyInterchangePaths.Add(NormalizePath(fullPath));
        }
    }

    private static void FlushDirtyInterchanges()
    {
        List<string> batch;
        lock (DirtyInterchangePaths)
        {
            if (DirtyInterchangePaths.Count == 0)
            {
                return;
            }

            batch = new List<string>(DirtyInterchangePaths);
            DirtyInterchangePaths.Clear();
        }

        foreach (string path in batch)
        {
            TryWriteBackToUnityMesh(path);
        }
    }

    private static void TryWriteBackToUnityMesh(string interchangeFullPath)
    {
        try
        {
            if (!File.Exists(interchangeFullPath))
            {
                return;
            }

            string normalized = NormalizePath(interchangeFullPath);
            if (IgnoreWriteBackUntil.TryGetValue(normalized, out double until)
                && EditorApplication.timeSinceStartup < until)
            {
                return;
            }

            // FileSystemWatcher can fire while Blender is still writing.
            if (!WaitForFileReady(interchangeFullPath, 20, 50))
            {
                QueueDirty(interchangeFullPath);
                return;
            }

            BridgeMeshData data = ReadInterchange(interchangeFullPath);
            if (data == null || data.version < 1)
            {
                Debug.LogError($"[BlenderBridge] Invalid interchange file: {interchangeFullPath}");
                return;
            }

            string assetPath = data.unity_asset_path;
            if (string.IsNullOrEmpty(assetPath))
            {
                PendingByInterchange.TryGetValue(NormalizePath(interchangeFullPath), out assetPath);
            }

            if (string.IsNullOrEmpty(assetPath))
            {
                Debug.LogError($"[BlenderBridge] No unity_asset_path in '{interchangeFullPath}'");
                return;
            }

            Mesh mesh = AssetDatabase.LoadAssetAtPath<Mesh>(assetPath);
            if (mesh == null)
            {
                Debug.LogError($"[BlenderBridge] Mesh asset missing: '{assetPath}'");
                return;
            }

            if (!mesh.isReadable)
            {
                Debug.LogError($"[BlenderBridge] Cannot write back; mesh not readable: '{assetPath}'");
                return;
            }

            ApplyBridgeDataToMesh(mesh, data);
            EditorUtility.SetDirty(mesh);
            AssetDatabase.SaveAssets();
            Debug.Log(
                $"[BlenderBridge] Wrote Blender edits back to Mesh '{assetPath}' " +
                $"({data.vertex_count} verts, {data.triangles?.Length / 3 ?? 0} tris)");
        }
        catch (Exception ex)
        {
            Debug.LogError($"[BlenderBridge] Mesh write-back failed for '{interchangeFullPath}': {ex.Message}");
        }
    }

    private static void ApplyBridgeDataToMesh(Mesh mesh, BridgeMeshData data)
    {
        if (data.vertices == null || data.vertices.Length < 3 || data.vertices.Length % 3 != 0)
        {
            throw new InvalidOperationException("vertices array is empty or not a multiple of 3");
        }

        int vertexCount = data.vertices.Length / 3;
        Vector3[] vertices = Unflatten3(data.vertices, vertexCount);
        int[] triangles = data.triangles ?? Array.Empty<int>();
        if (triangles.Length % 3 != 0)
        {
            throw new InvalidOperationException("triangles length is not a multiple of 3");
        }

        foreach (int index in triangles)
        {
            if (index < 0 || index >= vertexCount)
            {
                throw new InvalidOperationException($"triangle index {index} out of range 0..{vertexCount - 1}");
            }
        }

        mesh.Clear();
        mesh.name = string.IsNullOrEmpty(data.name) ? mesh.name : data.name;
        mesh.vertices = vertices;

        if (data.normals != null && data.normals.Length == vertexCount * 3)
        {
            mesh.normals = Unflatten3(data.normals, vertexCount);
        }

        if (data.uvs != null && data.uvs.Length == vertexCount * 2)
        {
            mesh.uv = Unflatten2(data.uvs, vertexCount);
        }

        int submeshCount = data.submesh_count > 0 ? data.submesh_count : 1;
        if (data.submesh_index_counts != null
            && data.submesh_index_counts.Length == submeshCount
            && Sum(data.submesh_index_counts) == triangles.Length)
        {
            mesh.subMeshCount = submeshCount;
            int offset = 0;
            for (int i = 0; i < submeshCount; i++)
            {
                int count = data.submesh_index_counts[i];
                int[] slice = new int[count];
                Array.Copy(triangles, offset, slice, 0, count);
                mesh.SetTriangles(slice, i, true);
                offset += count;
            }
        }
        else
        {
            mesh.subMeshCount = 1;
            mesh.triangles = triangles;
        }

        if (mesh.normals == null || mesh.normals.Length != vertexCount)
        {
            mesh.RecalculateNormals();
        }

        mesh.RecalculateBounds();
        mesh.RecalculateTangents();
    }

    private static bool WaitForFileReady(string path, int attempts, int delayMs)
    {
        for (int i = 0; i < attempts; i++)
        {
            try
            {
                using (FileStream stream = File.Open(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
                {
                    return stream.Length > 0;
                }
            }
            catch (IOException)
            {
                System.Threading.Thread.Sleep(delayMs);
            }
        }

        return false;
    }

    private static float[] Flatten(Vector3[] values)
    {
        float[] flat = new float[values.Length * 3];
        for (int i = 0; i < values.Length; i++)
        {
            flat[i * 3] = values[i].x;
            flat[i * 3 + 1] = values[i].y;
            flat[i * 3 + 2] = values[i].z;
        }

        return flat;
    }

    private static float[] Flatten(Vector2[] values)
    {
        float[] flat = new float[values.Length * 2];
        for (int i = 0; i < values.Length; i++)
        {
            flat[i * 2] = values[i].x;
            flat[i * 2 + 1] = values[i].y;
        }

        return flat;
    }

    private static Vector3[] Unflatten3(float[] flat, int count)
    {
        Vector3[] values = new Vector3[count];
        for (int i = 0; i < count; i++)
        {
            values[i] = new Vector3(flat[i * 3], flat[i * 3 + 1], flat[i * 3 + 2]);
        }

        return values;
    }

    private static Vector2[] Unflatten2(float[] flat, int count)
    {
        Vector2[] values = new Vector2[count];
        for (int i = 0; i < count; i++)
        {
            values[i] = new Vector2(flat[i * 2], flat[i * 2 + 1]);
        }

        return values;
    }

    private static int Sum(int[] values)
    {
        int total = 0;
        for (int i = 0; i < values.Length; i++)
        {
            total += values[i];
        }

        return total;
    }

    private static string NormalizePath(string path)
    {
        return Path.GetFullPath(path).Replace('\\', '/');
    }

    [Serializable]
    private class BridgeMeshData
    {
        public int version;
        public string name;
        public string unity_asset_path;
        public int vertex_count;
        public float[] vertices;
        public float[] normals;
        public float[] uvs;
        public int[] triangles;
        public int submesh_count;
        public int[] submesh_index_counts;
    }
}
