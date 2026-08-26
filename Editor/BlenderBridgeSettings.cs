using System.Collections.Generic;
using System.IO;
using UnityEditor;
using UnityEngine;

[FilePath("ProjectSettings/BlenderBridgeSettings.asset", FilePathAttribute.Location.ProjectFolder)]
internal sealed class BlenderBridgeSettings : ScriptableSingleton<BlenderBridgeSettings>
{
#if UNITY_EDITOR_WIN
    internal const string DefaultBlenderExecutablePath =
        @"C:\Program Files\Blender Foundation\Blender 5.1\blender.exe";
#elif UNITY_EDITOR_OSX
    internal const string DefaultBlenderExecutablePath =
        "/Applications/Blender.app/Contents/MacOS/Blender";
#else
    internal const string DefaultBlenderExecutablePath = "/usr/bin/blender";
#endif

    [SerializeField]
    private string blenderExecutablePath = DefaultBlenderExecutablePath;

    internal string BlenderExecutablePath
    {
        get
        {
            return string.IsNullOrWhiteSpace(blenderExecutablePath)
                ? DefaultBlenderExecutablePath
                : blenderExecutablePath.Trim();
        }
        set
        {
            blenderExecutablePath = value;
            Save(true);
        }
    }

    internal void ResetToDefault()
    {
        blenderExecutablePath = DefaultBlenderExecutablePath;
        Save(true);
    }
}

internal sealed class BlenderBridgeSettingsProvider : SettingsProvider
{
    private BlenderBridgeSettingsProvider(string path, SettingsScope scope)
        : base(path, scope)
    {
        keywords = new HashSet<string>(new[] { "Blender", "Bridge", "Executable", "Path" });
    }

    [SettingsProvider]
    public static SettingsProvider CreateProvider()
    {
        return new BlenderBridgeSettingsProvider("Project/Blender Bridge", SettingsScope.Project);
    }

    public override void OnGUI(string searchContext)
    {
        BlenderBridgeSettings settings = BlenderBridgeSettings.instance;

        EditorGUILayout.LabelField("Blender", EditorStyles.boldLabel);
        EditorGUILayout.HelpBox(
            "Select the Blender executable used by this Unity project.",
            MessageType.Info);

        EditorGUILayout.BeginHorizontal();
        EditorGUI.BeginChangeCheck();
        string path = EditorGUILayout.TextField(
            new GUIContent("Executable Path", "Full path to the Blender executable."),
            settings.BlenderExecutablePath);
        if (EditorGUI.EndChangeCheck())
        {
            settings.BlenderExecutablePath = path;
        }

        if (GUILayout.Button("Browse...", GUILayout.Width(85f)))
        {
            string selectedPath = EditorUtility.OpenFilePanel(
                "Select Blender Executable",
                GetInitialDirectory(settings.BlenderExecutablePath),
#if UNITY_EDITOR_WIN
                "exe"
#else
                string.Empty
#endif
            );
            if (!string.IsNullOrEmpty(selectedPath))
            {
                settings.BlenderExecutablePath = selectedPath;
                GUI.FocusControl(null);
            }
        }
        EditorGUILayout.EndHorizontal();

        string executablePath = settings.BlenderExecutablePath;
        if (File.Exists(executablePath))
        {
            EditorGUILayout.HelpBox("Blender executable found.", MessageType.None);
        }
        else
        {
            EditorGUILayout.HelpBox(
                $"Blender executable was not found at:\n{executablePath}",
                MessageType.Warning);
        }

        if (GUILayout.Button("Reset to Default", GUILayout.Width(120f)))
        {
            settings.ResetToDefault();
            GUI.FocusControl(null);
        }
    }

    private static string GetInitialDirectory(string executablePath)
    {
        if (!string.IsNullOrEmpty(executablePath))
        {
            try
            {
                string directory = Path.GetDirectoryName(executablePath);
                if (!string.IsNullOrEmpty(directory) && Directory.Exists(directory))
                {
                    return directory;
                }
            }
            catch (System.ArgumentException)
            {
                // Let the settings warning report malformed paths instead of breaking the GUI.
            }
        }

        return string.Empty;
    }
}
