' Stock App launcher (Windows). Double-click to start the local backend and open the app.
' - starts "python -m server.main" hidden (log: data\server.log) if not already running
' - creates a desktop shortcut on first run
Option Explicit
Dim sh, fso, dir, url, http, running, lnk, desktop
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir
url = "http://127.0.0.1:8000/"

desktop = sh.SpecialFolders("Desktop")
If Not fso.FileExists(desktop & "\Stock App.lnk") Then
  Set lnk = sh.CreateShortcut(desktop & "\Stock App.lnk")
  lnk.TargetPath = WScript.ScriptFullName
  lnk.WorkingDirectory = dir
  lnk.IconLocation = "shell32.dll,13"
  lnk.Save
End If

running = False
On Error Resume Next
Set http = CreateObject("MSXML2.XMLHTTP")
http.Open "GET", url & "api/ping", False
http.Send
If Err.Number = 0 Then
  If http.Status = 200 Then running = True
End If
Err.Clear
On Error GoTo 0

If Not running Then
  If Not fso.FolderExists(dir & "\data") Then fso.CreateFolder dir & "\data"
  sh.Run "cmd /c python -m server.main >> data\server.log 2>&1", 0, False
  ' wait until the backend answers (up to 60 s) instead of a fixed 3 s
  Dim i
  For i = 1 To 60
    WScript.Sleep 1000
    On Error Resume Next
    Set http = CreateObject("MSXML2.XMLHTTP")
    http.Open "GET", url & "api/ping", False
    http.Send
    If Err.Number = 0 Then
      If http.Status = 200 Then Exit For
    End If
    Err.Clear
    On Error GoTo 0
  Next
  On Error GoTo 0
End If

' open as an app window (no tabs / address bar), maximized; the page goes fullscreen on first click, Esc exits
On Error Resume Next
sh.Run "msedge --app=" & url & " --start-maximized", 1, False
If Err.Number <> 0 Then
  Err.Clear
  sh.Run "chrome --app=" & url & " --start-maximized", 1, False
  If Err.Number <> 0 Then
    Err.Clear
    sh.Run url
  End If
End If
On Error GoTo 0
