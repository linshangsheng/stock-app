' Stock App launcher (Windows). Double-click to start the local backend and open the app.
' - starts "python -m server.main" hidden (log: data\server.log) if not already running
' - opens the animated splash screen (splash.html, a local file) right away; it waits for the backend
'   and then switches to the app, so there is no silent wait after double-clicking
' - creates the desktop shortcut "Stock App" on first run, and keeps its icon pointing at app.ico
' Keep this file ASCII-only: VBScript cannot read UTF-8 Chinese text.
Option Explicit
Dim sh, fso, dir, url, http, running, lnk, desktop, ico, lnkPath, splash, i
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir
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

' ---- backend ----
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
End If

' ---- window: splash first (an app window without tabs / address bar, maximized) ----
' the page goes fullscreen on first click, Esc exits
If fso.FileExists(dir & "\splash.html") Then
  splash = "file:///" & Replace(Replace(dir, "\", "/"), " ", "%20") & "/splash.html?url=" & url
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
sh.Run url
