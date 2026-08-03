# Wipes the demo folder back to empty, so you can practice the demo again from a
# clean slate. Never touches your real notes in .\data\.
Remove-Item -Recurse -Force "$PSScriptRoot\demo\data" -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force "$PSScriptRoot\demo\projects" -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force "$PSScriptRoot\demo\documents" -ErrorAction SilentlyContinue
Write-Host "Demo data reset." -ForegroundColor Green
