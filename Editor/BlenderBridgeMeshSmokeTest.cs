using System.IO;
using UnityEditor;
using UnityEngine;

/// <summary>Dev-only smoke test for Unity Mesh (.mesh) interchange.</summary>
public static class BlenderBridgeMeshSmokeTest
{
    private const string DefaultMeshAsset =
        "Assets/Scenes/HLOD/DreamIsland/JiChang/Mesh/NearShell/N008.mesh";

    [MenuItem("Tools/Blender Bridge/Smoke Test Export N008.mesh")]
    public static void ExportN008()
    {
        ExportMeshAsset(DefaultMeshAsset);
    }

    public static void ExportMeshAsset(string meshAsset)
    {
        if (!BlenderBridgeUnityMesh.TryOpen(meshAsset, out string interchange, out string error))
        {
            Debug.LogError("[BlenderBridge SmokeTest] " + error);
            return;
        }

        Debug.Log("[BlenderBridge SmokeTest] interchange=" + interchange);
        if (!File.Exists(interchange))
        {
            Debug.LogError("[BlenderBridge SmokeTest] interchange missing on disk");
            return;
        }

        string json = File.ReadAllText(interchange);
        int headLen = Mathf.Min(200, json.Length);
        Debug.Log(
            "[BlenderBridge SmokeTest] bytes=" + new FileInfo(interchange).Length +
            " head=" + json.Substring(0, headLen));
        EditorGUIUtility.systemCopyBuffer = interchange;
    }
}
