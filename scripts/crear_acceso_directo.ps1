# Crea los accesos directos de botKeepGarden en el escritorio.
#
# No se invoca a mano: lo lanza scripts\instalar.bat, que le pasa
# -ExecutionPolicy Bypass. Asi no hay que tocar la politica de PowerShell
# de la maquina, igual que hace setup.bat.

$ErrorActionPreference = "Stop"

$raiz = Split-Path -Parent $PSScriptRoot
$lanzador = Join-Path $raiz "scripts\jardin.bat"
$icono = Join-Path $raiz "scripts\botkeepgarden.ico"

if (-not (Test-Path $lanzador)) {
    throw "No encuentro $lanzador. Esta completo el repositorio?"
}

# GetFolderPath resuelve el escritorio de verdad, incluso si OneDrive lo ha
# redirigido: dar por hecho "$HOME\Desktop" falla en medio Windows.
$escritorio = [Environment]::GetFolderPath('Desktop')
if (-not $escritorio -or -not (Test-Path $escritorio)) {
    throw "No se encuentra la carpeta del escritorio."
}

$shell = New-Object -ComObject WScript.Shell

function Nuevo-Acceso {
    param([string]$Nombre, [string]$Argumentos, [string]$Descripcion)

    $ruta = Join-Path $escritorio "$Nombre.lnk"
    $lnk = $shell.CreateShortcut($ruta)
    $lnk.TargetPath = $lanzador
    $lnk.Arguments = $Argumentos
    $lnk.WorkingDirectory = $raiz
    $lnk.Description = $Descripcion
    if (Test-Path $icono) { $lnk.IconLocation = "$icono,0" }
    $lnk.Save()
    Write-Host "  creado: $ruta" -ForegroundColor Green
}

Write-Host "botKeepGarden - instalando accesos directos" -ForegroundColor Green
Write-Host ""

Nuevo-Acceso -Nombre "botKeepGarden" -Argumentos "" `
    -Descripcion "El jardin vivo: una vela por hora, con su visor."

Nuevo-Acceso -Nombre "botKeepGarden (acelerado)" -Argumentos "--dry-run" `
    -Descripcion "Recorre historico acelerado: para ver evolucionar el jardin en minutos."

Write-Host ""
Write-Host "Listo. Tienes dos iconos en el escritorio:" -ForegroundColor Green
Write-Host "  botKeepGarden              el jardin de verdad, una vela por hora"
Write-Host "  botKeepGarden (acelerado)  para verlo evolucionar ya"
Write-Host ""
if (-not (Test-Path (Join-Path $raiz "state\db\garden.db"))) {
    Write-Host "Aviso: todavia no hay ningun jardin sembrado." -ForegroundColor Yellow
    Write-Host "Los iconos te lo diran al abrirlos, con los comandos que faltan."
}
