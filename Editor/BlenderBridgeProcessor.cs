// original script is by FleshMobProductions (thank you!): https://gist.github.com/FleshMobProductions/f598096b705f6a9c96beb58e284303f1

using System;
using System.IO;
using System.Net.Sockets;
using System.Runtime.InteropServices;
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
                _cachedPythonScriptPath = ResolveInjectorScriptPath();
            }
            return _cachedPythonScriptPath;
        }
    }

    /// <summary>Resolve injector next to BlenderBridgeProcessor.cs (Assets, embedded Packages, PackageCache).</summary>
    private static string ResolveInjectorScriptPath()
    {
        string[] guids = AssetDatabase.FindAssets("BlenderBridgeProcessor t:MonoScript");
        if (guids.Length == 0)
        {
            return null;
        }

        string processorAssetPath = AssetDatabase.GUIDToAssetPath(guids[0]);
        string processorDir = Path.GetDirectoryName(processorAssetPath);
        if (string.IsNullOrEmpty(processorDir))
        {
            return null;
        }

        string injectorAssetPath = Path.Combine(processorDir, "blender-bridge-injector.py")
            .Replace('\\', '/');

        string projectRoot = Path.GetDirectoryName(Application.dataPath);
        string projectRelativePath = Path.GetFullPath(Path.Combine(projectRoot, injectorAssetPath));
        if (File.Exists(projectRelativePath))
        {
            return projectRelativePath;
        }

        UnityEditor.PackageManager.PackageInfo packageInfo =
            UnityEditor.PackageManager.PackageInfo.FindForAssetPath(processorAssetPath);
        if (packageInfo != null && !string.IsNullOrEmpty(packageInfo.resolvedPath))
        {
            string packageCachePath = Path.GetFullPath(
                Path.Combine(packageInfo.resolvedPath, "Editor", "blender-bridge-injector.py"));
            if (File.Exists(packageCachePath))
            {
                return packageCachePath;
            }
        }

        return null;
    }

    /// <summary>Turn a Unity asset path (Assets/...) into an absolute filesystem path.</summary>
    private static string ResolveAssetFullPath(string assetPath)
    {
        if (Path.IsPathRooted(assetPath))
        {
            return Path.GetFullPath(assetPath);
        }

        string projectRoot = Path.GetDirectoryName(Application.dataPath);
        return Path.GetFullPath(Path.Combine(projectRoot, assetPath));
    }

    [OnOpenAsset]
    public static bool OnOpenAsset(int instanceId, int line)
    {
        UnityEngine.Object obj = EditorUtility.InstanceIDToObject(instanceId);
        string assetPath = AssetDatabase.GetAssetPath(instanceId);
        string extension = Path.GetExtension(assetPath).ToLowerInvariant();

        if (!Array.Exists(SUPPORTED_EXTENSIONS, ext => ext.Equals(extension, StringComparison.OrdinalIgnoreCase)))
        {
            return false;
        }

        if (string.IsNullOrEmpty(assetPath))
        {
            return false;
        }

        string modelFullPath = ResolveAssetFullPath(assetPath);
        if (!File.Exists(modelFullPath))
        {
            Debug.LogError($"[BlenderBridge] Model file not found: '{modelFullPath}' (asset: '{assetPath}')");
            return false;
        }

        if (DEBUG)
        {
            string objType = obj != null ? obj.GetType().Name : "null";
            Debug.Log($"Opening {extension.ToUpper()} '{assetPath}' in Blender (object: {objType})");
        }

        OpenInBlender(assetPath, modelFullPath);
        return true;
    }

    private static void OpenInBlender(string assetPath, string modelFullPath)
    {
        string importResult = TrySendImportToRunningBlenderWithRetries(modelFullPath);
        if (importResult == "OK")
        {
            TryBringBlenderToForeground();
            if (DEBUG)
            {
                Debug.Log($"Blender bridge: imported into existing Blender — '{assetPath}'");
            }
            return;
        }

        if (!string.IsNullOrEmpty(importResult) && importResult != "TIMEOUT")
        {
            Debug.LogWarning($"[BlenderBridge] Hot reuse failed ({importResult}). Launching Blender. Path: '{modelFullPath}'");
        }
        else if (DEBUG)
        {
            string py = PYTHON_SCRIPT_PATH ?? "(injector not found)";
            Debug.LogWarning(
                $"[BlenderBridge] No listener on 127.0.0.1:{BridgeListenPort}; launching Blender. Injector: {py}");
        }

        if (!File.Exists(BLENDER_PATH))
        {
            Debug.LogError($"Blender not found at {BLENDER_PATH}.");
            return;
        }

        string pythonScript = PYTHON_SCRIPT_PATH;
        if (pythonScript == null)
        {
            Debug.LogError(
                "[BlenderBridge] Python injector not found. Reimport the package or check that " +
                "Editor/blender-bridge-injector.py exists next to BlenderBridgeProcessor.cs.");
            return;
        }

        string arguments = $"--python \"{pythonScript}\" -- \"{modelFullPath}\"";
        if (DEBUG)
        {
            Debug.Log($"[BlenderBridge] Launching Blender with model: '{modelFullPath}'");
        }
        StartBlenderWithArguments(arguments, pythonScript);
    }

    /// <summary>
    /// If a Blender window started by this bridge is still running, it listens on 127.0.0.1:BridgeListenPort.
    /// Retries so a second double-click while the first Blender is still starting does not open a second window.
    /// </summary>
    /// <returns>"OK", null/empty when no listener, or an error token such as "ERR|bad path".</returns>
    private static string TrySendImportToRunningBlenderWithRetries(string modelFullPath)
    {
        float now = Time.realtimeSinceStartup;
        bool aggressive = _lastBlenderSpawnRealtime >= 0f
            && (now - _lastBlenderSpawnRealtime) < BlenderSpawnReuseWindowSec;
        int maxRounds = aggressive ? BlenderReuseRoundsAfterSpawn : BlenderReuseRoundsCold;
        string lastError = null;

        for (int round = 0; round < maxRounds; round++)
        {
            string result = TrySendImportToRunningBlenderOnce(modelFullPath);
            if (result == "OK")
            {
                return "OK";
            }

            if (!string.IsNullOrEmpty(result))
            {
                lastError = result;
            }

            if (round < maxRounds - 1)
            {
                Thread.Sleep(BridgeReuseDelayMs);
            }
        }

        return lastError ?? "TIMEOUT";
    }

    private static string TrySendImportToRunningBlenderOnce(string modelFullPath)
    {
        try
        {
            using (TcpClient client = new TcpClient())
            {
                IAsyncResult ar = client.BeginConnect("127.0.0.1", BridgeListenPort, null, null);
                if (!ar.AsyncWaitHandle.WaitOne(TimeSpan.FromMilliseconds(BridgeConnectTimeoutMs)))
                {
                    return null;
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
                        return $"unexpected pong: {pong}";
                    }

                    WriteUtf8Line(stream, "IMPORT|" + modelFullPath);
                    string ack = ReadUtf8Line(stream).Trim();
                    return ack == "OK" ? "OK" : ack;
                }
            }
        }
        catch (Exception ex)
        {
            if (DEBUG)
            {
                Debug.Log($"Blender bridge: listener not ready ({ex.GetType().Name})");
            }
            return null;
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

    /// <summary>
    /// After a hot-reuse import, raise the Blender window (Windows only).
    /// Called from Unity right after the user double-clicked an asset.
    /// </summary>
    private static void TryBringBlenderToForeground()
    {
#if UNITY_EDITOR_WIN
        try
        {
            System.Diagnostics.Process[] processes = System.Diagnostics.Process.GetProcessesByName("blender");
            IntPtr targetHwnd = IntPtr.Zero;
            int targetPid = -1;

            foreach (System.Diagnostics.Process process in processes)
            {
                try
                {
                    IntPtr hwnd = process.MainWindowHandle;
                    if (hwnd == IntPtr.Zero)
                    {
                        continue;
                    }

                    targetHwnd = hwnd;
                    targetPid = process.Id;
                    break;
                }
                catch
                {
                    // Process may exit while enumerating.
                }
            }

            if (targetHwnd == IntPtr.Zero)
            {
                return;
            }

            if (IsIconic(targetHwnd))
            {
                ShowWindow(targetHwnd, SwRestore);
            }
            else
            {
                ShowWindow(targetHwnd, SwShow);
            }

            if (targetPid > 0)
            {
                AllowSetForegroundWindow(targetPid);
            }

            IntPtr foregroundHwnd = GetForegroundWindow();
            uint foregroundThreadId = GetWindowThreadProcessId(foregroundHwnd, out _);
            uint targetThreadId = GetWindowThreadProcessId(targetHwnd, out _);
            uint currentThreadId = GetCurrentThreadId();

            bool attachedToForeground = false;
            bool attachedToTarget = false;
            if (foregroundThreadId != currentThreadId)
            {
                attachedToForeground = AttachThreadInput(currentThreadId, foregroundThreadId, true);
            }

            if (targetThreadId != currentThreadId)
            {
                attachedToTarget = AttachThreadInput(currentThreadId, targetThreadId, true);
            }

            SetForegroundWindow(targetHwnd);
            BringWindowToTop(targetHwnd);

            if (attachedToTarget)
            {
                AttachThreadInput(currentThreadId, targetThreadId, false);
            }

            if (attachedToForeground)
            {
                AttachThreadInput(currentThreadId, foregroundThreadId, false);
            }
        }
        catch (Exception ex)
        {
            if (DEBUG)
            {
                Debug.Log($"Blender bridge: could not focus Blender ({ex.GetType().Name})");
            }
        }
#endif
    }

#if UNITY_EDITOR_WIN
    private const int SwShow = 5;
    private const int SwRestore = 9;

    [DllImport("user32.dll")]
    private static extern bool SetForegroundWindow(IntPtr hWnd);

    [DllImport("user32.dll")]
    private static extern bool BringWindowToTop(IntPtr hWnd);

    [DllImport("user32.dll")]
    private static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);

    [DllImport("user32.dll")]
    private static extern bool IsIconic(IntPtr hWnd);

    [DllImport("user32.dll")]
    private static extern bool AllowSetForegroundWindow(int dwProcessId);

    [DllImport("user32.dll")]
    private static extern IntPtr GetForegroundWindow();

    [DllImport("user32.dll")]
    private static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint lpdwProcessId);

    [DllImport("user32.dll")]
    private static extern bool AttachThreadInput(uint idAttach, uint idAttachTo, bool fAttach);

    [DllImport("kernel32.dll")]
    private static extern uint GetCurrentThreadId();
#endif

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
