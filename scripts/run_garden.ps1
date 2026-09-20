# Arranca el jardin vivo y lo mantiene en pie.
#   .\scripts\run_garden.ps1
#
# El jardin esta pensado para correr indefinidamente. Si el proceso muere,
# este script lo relanza: el estado vive en SQLite, asi que reanudar es seguro
# y las velas perdidas se recuperan solas (catch-up).

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# El venv puede quedar en Scripts\ (Python nativo de Windows) o en bin\
# (Python de diseno POSIX, p.ej. via MSYS2/mingw). Se admite cualquiera.
$activate = ".venv\Scripts\Activate.ps1"
if (-not (Test-Path $activate)) { $activate = ".venv\bin\Activate.ps1" }
if (-not (Test-Path $activate)) { throw "No se encuentra el script de activacion del entorno virtual." }
& $activate

$restarts = 0
while ($true) {
    Write-Host "[$(Get-Date -Format 'u')] arrancando el jardin (reinicios: $restarts)" -ForegroundColor Green
    keepgarden run
    $code = $LASTEXITCODE
    if ($code -eq 0) {
        Write-Host "parada limpia." -ForegroundColor Green
        break
    }
    $restarts++
    Write-Host "el jardin salio con codigo $code; reintentando en 60 s" -ForegroundColor Yellow
    Start-Sleep -Seconds 60
}
