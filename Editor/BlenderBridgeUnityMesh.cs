using System;
using System.Collections.Generic;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using Unity.Collections;
using UnityEditor;
using UnityEngine;
using UnityEngine.Rendering;

/// <summary>
/// Round-trip Unity Mesh (.mesh or .asset) assets through Blender via a JSON interchange file
/// (Library/BlenderBridge/*.bridge-mesh). Unity decompresses Mesh API data; Blender never
/// parses Unity's CompressedMesh YAML.
/// </summary>
[InitializeOnLoad]
public static class BlenderBridgeUnityMesh
{
    public const string InterchangeExtension = ".bridge-mesh";

    private const int BridgeMeshVersion = 3;
    private static readonly Dictionary<string, string> PendingByInterchange =
        new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
    private static readonly Dictionary<string, long> PendingLocalIdByInterchange =
        new Dictionary<string, long>(StringComparer.OrdinalIgnoreCase);

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
        return TryOpen(
            AssetDatabase.LoadAssetAtPath<Mesh>(assetPath),
            assetPath,
            out interchangeFullPath,
            out error);
    }

    public static bool TryOpen(
        Mesh mesh,
        string assetPath,
        out string interchangeFullPath,
        out string error)
    {
        interchangeFullPath = null;
        error = null;

        if (mesh == null)
        {
            error = $"Not a Mesh asset: '{assetPath}'";
            return false;
        }

        if (!AssetDatabase.TryGetGUIDAndLocalFileIdentifier(mesh, out string _, out long localId))
        {
            error = $"Could not identify Mesh '{mesh.name}' in asset '{assetPath}'.";
            return false;
        }

        try
        {
            BridgeMeshData data = CaptureMesh(mesh, assetPath, localId);
            interchangeFullPath = GetInterchangePath(assetPath, mesh.name, localId);
            Directory.CreateDirectory(Path.GetDirectoryName(interchangeFullPath));

            WriteInterchange(interchangeFullPath, data);

            string normalized = NormalizePath(interchangeFullPath);
            PendingByInterchange[normalized] = assetPath;
            PendingLocalIdByInterchange[normalized] = localId;
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

    private static BridgeMeshData CaptureMesh(Mesh mesh, string assetPath, long localId)
    {
        using (Mesh.MeshDataArray meshDataArray = MeshUtility.AcquireReadOnlyMeshData(mesh))
        {
            Mesh.MeshData meshData = meshDataArray[0];
            int vertexCount = meshData.vertexCount;
            if (vertexCount == 0)
            {
                throw new InvalidOperationException($"Mesh '{assetPath}' has no vertices.");
            }

            Vector3[] vertices = CaptureVertices(meshData);
            Vector3[] normals = CaptureNormals(meshData, vertexCount);
            int[] submeshIndexCounts;
            int[] triangles = CaptureTriangles(meshData, out submeshIndexCounts);

            return new BridgeMeshData
            {
                version = BridgeMeshVersion,
                name = string.IsNullOrEmpty(mesh.name) ? Path.GetFileNameWithoutExtension(assetPath) : mesh.name,
                unity_asset_path = assetPath.Replace('\\', '/'),
                unity_mesh_local_id = localId,
                vertex_count = vertexCount,
                vertices = Flatten(vertices),
                normals = normals.Length == vertexCount ? Flatten(normals) : Array.Empty<float>(),
                uvs = CaptureUvChannel(meshData, 0, vertexCount),
                uvs2 = CaptureUvChannel(meshData, 1, vertexCount),
                uvs3 = CaptureUvChannel(meshData, 2, vertexCount),
                uvs4 = CaptureUvChannel(meshData, 3, vertexCount),
                uvs5 = CaptureUvChannel(meshData, 4, vertexCount),
                uvs6 = CaptureUvChannel(meshData, 5, vertexCount),
                uvs7 = CaptureUvChannel(meshData, 6, vertexCount),
                uvs8 = CaptureUvChannel(meshData, 7, vertexCount),
                triangles = triangles,
                submesh_count = meshData.subMeshCount,
                submesh_index_counts = submeshIndexCounts
            };
        }
    }

    private static Vector3[] CaptureVertices(Mesh.MeshData meshData)
    {
        using (var vertices = new NativeArray<Vector3>(meshData.vertexCount, Allocator.Temp))
        {
            meshData.GetVertices(vertices);
            return vertices.ToArray();
        }
    }

    private static Vector3[] CaptureNormals(Mesh.MeshData meshData, int vertexCount)
    {
        if (!meshData.HasVertexAttribute(VertexAttribute.Normal))
        {
            return Array.Empty<Vector3>();
        }

        using (var normals = new NativeArray<Vector3>(vertexCount, Allocator.Temp))
        {
            meshData.GetNormals(normals);
            return normals.ToArray();
        }
    }

    private static int[] CaptureTriangles(Mesh.MeshData meshData, out int[] submeshIndexCounts)
    {
        submeshIndexCounts = new int[meshData.subMeshCount];
        int totalIndexCount = 0;
        for (int i = 0; i < meshData.subMeshCount; i++)
        {
            SubMeshDescriptor submesh = meshData.GetSubMesh(i);
            if (submesh.topology != MeshTopology.Triangles)
            {
                throw new InvalidOperationException(
                    $"Submesh {i} uses unsupported topology '{submesh.topology}'. Only triangles are supported.");
            }

            submeshIndexCounts[i] = submesh.indexCount;
            totalIndexCount += submesh.indexCount;
        }

        int[] triangles = new int[totalIndexCount];
        int offset = 0;
        for (int i = 0; i < meshData.subMeshCount; i++)
        {
            int count = submeshIndexCounts[i];
            using (var indices = new NativeArray<int>(count, Allocator.Temp))
            {
                meshData.GetIndices(indices, i);
                for (int j = 0; j < count; j++)
                {
                    triangles[offset + j] = indices[j];
                }
            }
            offset += count;
        }

        return triangles;
    }

    private static float[] CaptureUvChannel(Mesh.MeshData meshData, int channel, int vertexCount)
    {
        VertexAttribute attribute = (VertexAttribute)((int)VertexAttribute.TexCoord0 + channel);
        if (!meshData.HasVertexAttribute(attribute))
        {
            return Array.Empty<float>();
        }

        using (var uvs = new NativeArray<Vector2>(vertexCount, Allocator.Temp))
        {
            meshData.GetUVs(channel, uvs);
            return Flatten(uvs.ToArray());
        }
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

    private static string GetInterchangePath(string assetPath, string meshName, long localId)
    {
        string projectRoot = Path.GetDirectoryName(Application.dataPath);
        string dir = Path.Combine(projectRoot, "Library", "BlenderBridge");
        string hash;
        using (SHA1 sha = SHA1.Create())
        {
            string meshIdentity = assetPath.Replace('\\', '/') + "|" + localId;
            byte[] bytes = sha.ComputeHash(Encoding.UTF8.GetBytes(meshIdentity));
            hash = BitConverter.ToString(bytes).Replace("-", "").Substring(0, 12).ToLowerInvariant();
        }

        string safeName = string.IsNullOrEmpty(meshName)
            ? Path.GetFileNameWithoutExtension(assetPath)
            : meshName;
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

            long localId = data.unity_mesh_local_id;
            if (localId == 0)
            {
                PendingLocalIdByInterchange.TryGetValue(normalized, out localId);
            }

            Mesh mesh = ResolveMesh(assetPath, localId);
            if (mesh == null)
            {
                Debug.LogError(
                    $"[BlenderBridge] Mesh asset missing: '{assetPath}' " +
                    $"(local file ID {localId})");
                return;
            }

            // In Edit Mode Unity permits Mesh writes even when the importer's
            // Read/Write option is disabled. The old explicit isReadable guard
            // was what prevented these assets from round-tripping.
            ApplyBridgeDataToMesh(mesh, data);

            EditorUtility.SetDirty(mesh);
            AssetDatabase.SaveAssets();
            Debug.Log(
                $"[BlenderBridge] Wrote Blender edits back to Mesh '{assetPath}' " +
                $"({data.vertex_count} verts, {data.triangles?.Length / 3 ?? 0} tris, " +
                $"uvs={HasUv(data.uvs, data.vertex_count)} " +
                $"uvs2={HasUv(data.uvs2, data.vertex_count)})");
        }
        catch (Exception ex)
        {
            Debug.LogError($"[BlenderBridge] Mesh write-back failed for '{interchangeFullPath}': {ex.Message}");
        }
    }

    private static bool HasUv(float[] uvs, int vertexCount)
    {
        return uvs != null && vertexCount > 0 && uvs.Length == vertexCount * 2;
    }

    private static Mesh ResolveMesh(string assetPath, long localId)
    {
        if (localId != 0)
        {
            UnityEngine.Object[] assets = AssetDatabase.LoadAllAssetsAtPath(assetPath);
            foreach (UnityEngine.Object asset in assets)
            {
                if (!(asset is Mesh candidate))
                {
                    continue;
                }

                if (AssetDatabase.TryGetGUIDAndLocalFileIdentifier(
                        candidate,
                        out string _,
                        out long candidateLocalId)
                    && candidateLocalId == localId)
                {
                    return candidate;
                }
            }
        }

        return AssetDatabase.LoadAssetAtPath<Mesh>(assetPath);
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

        ApplyUvChannel(mesh, 0, data.uvs, vertexCount);
        ApplyUvChannel(mesh, 1, data.uvs2, vertexCount);
        ApplyUvChannel(mesh, 2, data.uvs3, vertexCount);
        ApplyUvChannel(mesh, 3, data.uvs4, vertexCount);
        ApplyUvChannel(mesh, 4, data.uvs5, vertexCount);
        ApplyUvChannel(mesh, 5, data.uvs6, vertexCount);
        ApplyUvChannel(mesh, 6, data.uvs7, vertexCount);
        ApplyUvChannel(mesh, 7, data.uvs8, vertexCount);

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

    private static void ApplyUvChannel(Mesh mesh, int channel, float[] flat, int vertexCount)
    {
        if (flat == null || flat.Length != vertexCount * 2)
        {
            return;
        }

        mesh.SetUVs(channel, Unflatten2(flat, vertexCount));
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
        public long unity_mesh_local_id;
        public int vertex_count;
        public float[] vertices;
        public float[] normals;
        public float[] uvs;
        public float[] uvs2;
        public float[] uvs3;
        public float[] uvs4;
        public float[] uvs5;
        public float[] uvs6;
        public float[] uvs7;
        public float[] uvs8;
        public int[] triangles;
        public int submesh_count;
        public int[] submesh_index_counts;
    }
}
