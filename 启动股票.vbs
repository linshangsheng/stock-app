' Stock App launcher (Windows). Double-click to start the local backend and open the app.
' - starts "python -m server.main" hidden (log: data\server.log) if not already running;
'   on a new PC without Python / packages it opens the installer instead
' - opens the animated splash screen (splash.html, a local file) right away; it waits for the backend
'   and then switches to the app, so there is no silent wait after double-clicking
' - port 8000 by default; if Windows has reserved it (Hyper-V / WSL, WinError 10013) or another program
'   uses it, the backend switches to a fallback port and the splash screen finds it automatically
' - creates the desktop shortcut "Stock App" on first run, and keeps its icon pointing at app.ico
' Keep this file ASCII-only: VBScript cannot read UTF-8 Chinese text.
Option Explicit
Dim sh, fso, dir, url, running, lnk, desktop, ico, lnkPath, splash, i, p, ports, altPorts, rc, inst
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir
' keep in sync with server.port / server.fallback_ports in server/config.yaml
ports = Array(8000, 18000, 28000)
altPorts = "18000,28000"
url = "http://127.0.0.1:8000/"
ico = dir & "\app.ico"

' ---- desktop shortcut (create once; update the icon if it still uses the old one) ----
desktop = sh.SpecialFolders("Desktop")
lnkPath = desktop & "\Stock App.lnk"
On Error Resume Next
Set lnk = sh.CreateShortcut(lnkPath)
If Not fso.FileExists(lnkPath) Then
  lnk.TargetPath = WScript.ScriptFullName
  lnk.WorkingDirectory = dir
  lnk.Description = "Stock App - trend and swing"
  If fso.FileExists(ico) Then lnk.IconLocation = ico & ",0" Else lnk.IconLocation = "shell32.dll,13"
  lnk.Save
ElseIf fso.FileExists(ico) And LCase(lnk.IconLocation) <> LCase(ico & ",0") Then
  lnk.IconLocation = ico & ",0"
  lnk.Save
End If
Err.Clear
On Error GoTo 0

' True if this app's backend answers at base (checks the "build" field of /api/ping)
Function IsOurs(base)
  Dim x
  IsOurs = False
  On Error Resume Next
  Set x = CreateObject("MSXML2.XMLHTTP")
  x.Open "GET", base & "api/ping", False
  x.Send
  If Err.Number = 0 Then
    If x.Status = 200 Then
      If InStr(x.responseText, """build""") > 0 Then IsOurs = True
    End If
  End If
  Err.Clear
  On Error GoTo 0
End Function

' ---- backend: already running on any of the ports? ----
running = False
For Each p In ports
  If IsOurs("http://127.0.0.1:" & p & "/") Then
    url = "http://127.0.0.1:" & p & "/"
    running = True
    Exit For
  End If
Next

If Not running Then
  If Not fso.FolderExists(dir & "\data") Then fso.CreateFolder dir & "\data"
  ' first run on a new PC: Python missing or packages not installed -> open the installer instead of
  ' starting a backend that would exit silently (the splash screen would just wait). server.deps exits 0 when complete.
  rc = sh.Run("cmd /c python -m server.deps >nul 2>nul", 0, True)
  If rc <> 0 Then
    inst = dir & "\" & ChrW(&H5B89) & ChrW(&H88C5) & ".bat"   ' the installer file (Chinese name; VBScript source must stay ASCII)
    If fso.FileExists(inst) Then
      sh.Run Chr(34) & inst & Chr(34), 1, False
    Else
      MsgBox "Python or the required packages are missing." & vbCrLf & "Run in this folder:  python -m pip install -r server/requirements.txt", 48, "Stock App"
    End If
    WScript.Quit
  End If
  sh.Run "cmd /c python -m server.main >> data\server.log 2>&1", 0, False
End If

' ---- window: splash first (an app window without tabs / address bar, maximized) ----
' the page goes fullscreen on first click, Esc exits
If fso.FileExists(dir & "\splash.html") Then
  splash = "file:///" & Replace(Replace(dir, "\", "/"), " ", "%20") & "/splash.html?url=" & url & "&alt=" & altPorts
  On Error Resume Next
  sh.Run "msedge --app=""" & splash & """ --start-maximized", 1, False
  If Err.Number = 0 Then WScript.Quit
  Err.Clear
  sh.Run "chrome --app=""" & splash & """ --start-maximized", 1, False
  If Err.Number = 0 Then WScript.Quit
  Err.Clear
  On Error GoTo 0
End If

' fallback (no Edge / Chrome, or no splash.html): wait for the backend (up to 60 s), then open the default browser
If Not running Then
  For i = 1 To 60
    WScript.Sleep 1000
    For Each p In ports
      If IsOurs("http://127.0.0.1:" & p & "/") Then
        url = "http://127.0.0.1:" & p & "/"
        running = True
        Exit For
      End If
    Next
    If running Then Exit For
  Next
End If
sh.Run url
