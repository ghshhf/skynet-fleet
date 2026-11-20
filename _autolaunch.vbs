' ============================================================
'  _autolaunch.vbs  -  adaptive headless launcher
'  Invoked by Windows Run:
'     wscript.exe //B //NoLogo _autolaunch.vbs <target.py>
'
'  NO python interpreter path is hardcoded. Candidates are discovered
'  at runtime (live python processes, common locations, PATH) and each
'  one is probed before use. The first candidate that can import the
'  modules the target needs wins. Stale paths simply self-skip.
' ============================================================
Option Explicit

Dim fso, sh, target, workDir, baseDir, nfDir, logFile
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")

If WScript.Arguments.Count < 1 Then WScript.Quit 1
target = WScript.Arguments(0)
If Not fso.FileExists(target) Then WScript.Quit 2

workDir = fso.GetParentFolderName(target)
baseDir = fso.GetParentFolderName(workDir)
logFile = fso.BuildPath(workDir, "_autolaunch.log")

' --- locate netforce root (for the import probe) ---
Dim nf
nf = sh.ExpandEnvironmentStrings("%NETFORCE_HOME%")
If nf = "" Or Not fso.FolderExists(nf) Then
  If LCase(fso.GetFileName(workDir)) = "skynet-fleet" Then
    nf = fso.BuildPath(baseDir, "netforce")
  ElseIf LCase(fso.GetFileName(baseDir)) = "netforce" Then
    nf = baseDir
  Else
    nf = fso.BuildPath(baseDir, "netforce")
  End If
End If
nfDir = nf

LogIt "=== request " & target & " | netforce=" & nfDir

Dim probeCode
probeCode = "import sys;sys.path.insert(0,r'" & Replace(workDir,"'","''") & "');sys.path.insert(0,r'" & Replace(nfDir,"'","''") & "');import selfproxy"

Dim cand(), n, i
ReDim cand(96)
n = 0

' --- 1) stable runtimes first (probe-gated: stale ones self-skip) ---
'     dsh primary runtime is WorkBuddy's core runtime = most durable.
'     Derive from %USERPROFILE% (no hardcoded account name) so it works on any machine.
AddCand sh.ExpandEnvironmentStrings("%USERPROFILE%") & "\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\pythonw.exe"
AddCand sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python313\pythonw.exe"
AddCand sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python312\pythonw.exe"
AddCand "C:\Python313\pythonw.exe"
AddCand "C:\Python312\pythonw.exe"
AddCand "C:\Python311\pythonw.exe"
AddCand "D:\Python312\pythonw.exe"
AddCand "E:\Python312\pythonw.exe"

' --- 2) live python processes as fallback (proven runnable but may be
'     short-lived MCP venvs that get cleaned up) ---
On Error Resume Next
Dim wmi, procs, p
Set wmi = GetObject("winmgmts:\\.\root\cimv2")
Set procs = wmi.ExecQuery("SELECT ExecutablePath FROM Win32_Process WHERE Name='pythonw.exe' OR Name='python.exe'")
For Each p In procs
  If Not IsNull(p.ExecutablePath) Then AddCand CStr(p.ExecutablePath)
Next
On Error GoTo 0

' --- 3) PATH ---
Dim pe, ds, d
pe = sh.ExpandEnvironmentStrings("%PATH%")
ds = Split(pe, ";")
For Each d In ds
  If Trim(d) <> "" Then AddCand Trim(d) & "\pythonw.exe"
Next

Dim chosen, rc, cmd
chosen = ""
For i = 0 To n - 1
  If fso.FileExists(cand(i)) Then
    cmd = """" & cand(i) & """ -c """ & probeCode & """"
    rc = sh.Run(cmd, 0, True)
    LogIt "probe " & cand(i) & " => " & rc
    If rc = 0 Then
      chosen = cand(i)
      Exit For
    End If
  End If
Next

If chosen = "" Then
  LogIt "FATAL: no usable pythonw found"
  WScript.Quit 3
End If

cmd = """" & chosen & """ """ & target & """"
sh.CurrentDirectory = workDir
rc = sh.Run(cmd, 0, False)
LogIt "STARTED " & chosen & " -> " & target & " (rc=" & rc & ")"

Sub AddCand(path)
  Dim j, k
  If path = "" Then Exit Sub
  k = LCase(path)
  For j = 0 To n - 1
    If LCase(cand(j)) = k Then Exit Sub
  Next
  If n <= UBound(cand) Then
    cand(n) = path
    n = n + 1
  End If
End Sub

Sub LogIt(msg)
  On Error Resume Next
  Dim f
  Set f = fso.OpenTextFile(logFile, 8, True)
  f.WriteLine Now & "  " & msg
  f.Close
  On Error GoTo 0
End Sub
