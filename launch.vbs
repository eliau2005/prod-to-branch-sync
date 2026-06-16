' launch.vbs — starts the app's Node launcher with no visible console window.
' The Chrome app window it opens is the only UI; closing that window stops the server.
Set sh = CreateObject("WScript.Shell")
appDir = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))
sh.CurrentDirectory = appDir
' 0 = hidden window, False = don't wait for it to finish
sh.Run """" & appDir & "node.exe"" """ & appDir & "launcher.mjs""", 0, False
