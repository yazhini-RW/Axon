# Launches Axon's web UI against a separate, persistent demo folder - never mixes
# with your real notes in .\data. Safe to run over and over for practice or a live demo.
$env:AXON_DATA_DIR = "$PSScriptRoot\demo\data"
$env:AXON_PROJECTS_DIR = "$PSScriptRoot\demo\projects"
$env:AXON_DOCUMENTS_DIR = "$PSScriptRoot\demo\documents"

Write-Host "Axon demo mode - data stays in .\demo\, your real notes are untouched." -ForegroundColor Green
Write-Host "Opening http://127.0.0.1:8420 once the server is ready..." -ForegroundColor Green

& "$PSScriptRoot\.venv\Scripts\python.exe" -m axon.cli serve
