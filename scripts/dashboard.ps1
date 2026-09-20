# Abre el dashboard del jardin.
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
& .\.venv\Scripts\Activate.ps1
keepgarden dashboard
