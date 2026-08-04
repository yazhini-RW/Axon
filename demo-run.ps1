# Starts the reminder daemon against the same demo sandbox as demo.ps1, so a
# reminder note (like "buy milk at 5pm") actually fires as a real Windows
# notification. Run this in a SEPARATE terminal alongside demo.ps1.
$env:AXON_DATA_DIR = "$PSScriptRoot\demo\data"
$env:AXON_PROJECTS_DIR = "$PSScriptRoot\demo\projects"
$env:AXON_DOCUMENTS_DIR = "$PSScriptRoot\demo\documents"

Write-Host "Axon reminder daemon (demo mode) - watching for due reminders..." -ForegroundColor Green

& "$PSScriptRoot\.venv\Scripts\python.exe" -m axon.cli run
