' Hidden launcher for Hermes Connector Hub autostart.
' Usage: wscript.exe run-hidden.vbs "<path-to-cmd>"
Set objArgs = WScript.Arguments
If objArgs.Count < 1 Then WScript.Quit 1
Set ws = CreateObject("WScript.Shell")
ws.Run """" & objArgs(0) & """", 0, False
