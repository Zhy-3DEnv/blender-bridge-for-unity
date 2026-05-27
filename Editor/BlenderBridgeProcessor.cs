// original script is by FleshMobProductions (thank you!): https://gist.github.com/FleshMobProductions/f598096b705f6a9c96beb58e284303f1

using System;
using System.IO;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using UnityEditor;
using UnityEditor.Callbacks;
using UnityEngine;

public static class BlenderBridgeProcessor
{
    private static readonly bool DEBUG = false; // If false it'll only log errors

    private static readonly string BLENDER_PATH = @"C:\Program Files\Blender Foundation\Blender 5.1\blender.exe";
    private static readonly string[] SUPPORTED_EXTENSIONS = { ".fbx", ".obj", ".dae" };

    /// <summary>Must match blender-bridge-injector default BRIDGE_BLENDER_PORT.</summary>
    private const int BridgeListenPort = 35971;

    /// <summary>Per attempt; first Blender may need several seconds before Python binds the port.</summary>
    private const int BridgeConnectTimeoutMs = 350;

    private const int BridgeIoTimeoutMs = 8000;

    /// <summary>After spawning Blender, keep trying to reuse while it boots (seconds).</summary>
    private const float BlenderSpawnReuseWindowSec = 55f;

    private const int BlenderReuseRoundsAfterSpawn = 90;

    private const int BlenderReuseRoundsCold = 3;

    private const int BridgeReuseDelayMs = 120;

    private static float _lastBlenderSpawnRealtime = -1f;

    private static string _cachedPythonScriptPath;
    private static string PYTHON_SCRIPT_PATH
    {
        get
        {
            if (_cachedPythonScriptPath == null)
            {
                string[] guids = AssetDatabase.FindAssets("blender-bridge-injector t:DefaultAsset");
                if (guids.Length > 0)
                {
                    string assetPath = AssetDatabase.GUIDToAssetPath(guids[0]);
                    _cachedPythonScriptPath = Path.GetFullPath(assetPath);
                }
            }
            return _cachedPythonScriptPath;
        }
    }

    [OnOpenAsset]
    public static bool OnOpenAsset(int instanceId, int line)
    {
        UnityEngine.Object obj = EditorUtility.InstanceIDToObject(instanceId);
        string assetPath = AssetDatabase.GetAssetPath(instanceId);
        string extension = Path.GetExtension(assetPath).ToLowerInvariant();

        if (Array.Exists(SUPPORTED_EXTENSIONS, ext => ext.Equals(extension, StringComparison.OrdinalIgnoreCase))
            && obj is GameObject)
        {
            if (DEBUG)
            {
                Debug.Log($"Opening {extension.ToUpper()} '{assetPath}' in Blender");
            }

            OpenInBlender(assetPath);
            return true;
        }
        return false;
    }

    private static void OpenInBlender(string assetPath)
    {
        string modelFullPath = Path.GetFullPath(assetPath);

        if (TrySendImportToRunningBlenderWithRetries(modelFullPath))
        {
            if (DEBUG)
            {
                Debug.Log($"Blender bridge: imported into existing Blender — '{assetPath}'");
            }
            return;
        }

        if (DEBUG)
        {
            string py = PYTHON_SCRIPT_PATH ?? "(injector not found)";
            Debug.LogWarning(
                $"[BlenderBridge] 未连上 127.0.0.1:{BridgeListenPort} 的复用监听，将启动新 Blender。injector: {py}");
        }

        if (!File.Exists(BLENDER_PATH))
        {
            Debug.LogError($"Blender not found at {BLENDER_PATH}.");
            return;
        }

        string pythonScript = PYTHON_SCRIPT_PATH;
        if (pythonScript == null)
        {
            Debug.LogError("Python script not found");
            return;
        }

        string arguments = $"--python \"{pythonScript}\" -- \"{modelFullPath}\"";
        StartBlenderWithArguments(arguments, pythonScript);
    }

    /// <summary>
    /// If a Blender window started by this bridge is still running, it listens on 127.0.0.1:BridgeListenPort.
    /// Retries so a second double-click while the first Blender is still starting does not open a second window.
    /// </summary>
    private static bool TrySendImportToRunningBlenderWithRetries(string modelFullPath)
    {
        float now = Time.realtimeSinceStartup;
        bool aggressive = _lastBlenderSpawnRealtime >= 0f
            && (now - _lastBlenderSpawnRealtime) < BlenderSpawnReuseWindowSec;
        int maxRounds = aggressive ? BlenderReuseRoundsAfterSpawn : BlenderReuseRoundsCold;

        for (int round = 0; round < maxRounds; round++)
        {
            if (TrySendImportToRunningBlenderOnce(modelFullPath))
            {
                return true;
            }

            if (round < maxRounds - 1)
            {
                Thread.Sleep(BridgeReuseDelayMs);
            }
        }

        return false;
    }

    private static bool TrySendImportToRunningBlenderOnce(string modelFullPath)
    {
        try
        {
            using (TcpClient client = new TcpClient())
            {
                IAsyncResult ar = client.BeginConnect("127.0.0.1", BridgeListenPort, null, null);
                if (!ar.AsyncWaitHandle.WaitOne(TimeSpan.FromMilliseconds(BridgeConnectTimeoutMs)))
                {
                    return false;
                }

                client.EndConnect(ar);
                client.ReceiveTimeout = BridgeIoTimeoutMs;
                client.SendTimeout = BridgeIoTimeoutMs;

                using (NetworkStream stream = client.GetStream())
                {
                    WriteUtf8Line(stream, "PING");
                    string pong = ReadUtf8Line(stream).Trim();
                    if (pong != "PONG")
                    {
                        return false;
                    }

                    WriteUtf8Line(stream, "IMPORT|" + modelFullPath);
                    string ok = ReadUtf8Line(stream).Trim();
                    return ok == "OK";
                }
            }
        }
        catch (Exception ex)
        {
            if (DEBUG)
            {
                Debug.Log($"Blender bridge: listener not ready ({ex.GetType().Name})");
            }
            return false;
        }
    }

    private static void WriteUtf8Line(NetworkStream stream, string line)
    {
        byte[] buf = Encoding.UTF8.GetBytes(line + "\n");
        stream.Write(buf, 0, buf.Length);
        stream.Flush();
    }

    private static string ReadUtf8Line(NetworkStream stream)
    {
        using (MemoryStream ms = new MemoryStream())
        {
            int n = 0;
            const int maxBytes = 65536;
            while (n++ < maxBytes)
            {
                int b = stream.ReadByte();
                if (b < 0)
                {
                    break;
                }

                if (b == '\n')
                {
                    break;
                }

                if (b != '\r')
                {
                    ms.WriteByte((byte)b);
                }
            }

            return Encoding.UTF8.GetString(ms.ToArray());
        }
    }

    private static void StartBlenderWithArguments(string arguments, string injectorScriptPath)
    {
        System.Diagnostics.Process process = new System.Diagnostics.Process();
        System.Diagnostics.ProcessStartInfo startInfo = new System.Diagnostics.ProcessStartInfo
        {
            FileName = BLENDER_PATH,
            UseShellExecute = false,
            // Never redirect stdout/stderr without draining: Blender + Python can block when the pipe buffer fills,
            // which prevents the reuse TCP server from starting.
            RedirectStandardOutput = false,
            RedirectStandardError = false,
            CreateNoWindow = true,
            Arguments = arguments
        };
        startInfo.EnvironmentVariables["BRIDGE_BLENDER_PORT"] = BridgeListenPort.ToString();
        if (!string.IsNullOrEmpty(injectorScriptPath))
        {
            startInfo.EnvironmentVariables["BLENDER_BRIDGE_INJECTOR"] = injectorScriptPath;
        }
        if (DEBUG)
        {
            startInfo.EnvironmentVariables["BLENDER_BRIDGE_PROFILE"] = "1";
        }
        process.StartInfo = startInfo;
        process.Start();
        _lastBlenderSpawnRealtime = Time.realtimeSinceStartup;
    }
}
