# Prepara el entorno de desarrollo en Windows.
#   .\scripts\bootstrap.ps1
#   .\scripts\bootstrap.ps1 -PythonExe "C:\ruta\a\python.exe"   (fuerza un interprete concreto)

param(
    [string]$PythonExe = ""
)

$ErrorActionPreference = "Stop"
# Desactiva, donde exista (PowerShell 7.3+), que un exit code distinto de 0 de
# un programa nativo se convierta el solo en un error terminante: se comprueba
# $LASTEXITCODE a mano tras cada paso critico, con un mensaje claro en espanol,
# en vez de fiarse de este mecanismo (que ademas varia de comportamiento entre
# Windows PowerShell 5.1 y PowerShell 7).
$PSNativeCommandUseErrorActionPreference = $false

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host "botKeepGarden - preparando entorno" -ForegroundColor Green

function Resolve-Python {
    param([string]$Override)

    if ($Override) {
        if (-not (Test-Path $Override)) { throw "No existe el interprete indicado: $Override" }
        return @($Override)
    }

    # Se prefiere el Python Launcher (py.exe) con una version concreta, en ese
    # orden, en vez de fiarse de a que resuelve "python" en el PATH: un
    # "python" generico puede venir de una instalacion no estandar (p.ej.
    # MSYS2/mingw, o una version demasiado nueva) sin ruedas precompiladas
    # para pandas/numpy/pyarrow, lo que obliga a compilar desde el codigo
    # fuente y falla sin Visual Studio Build Tools instalados.
    #
    # Se usa "py -0" (lista las versiones instaladas, sin lanzar ninguna) en
    # vez de invocar cada version a ver si responde: invocar una version que
    # no existe escribe en stderr, y bajo $ErrorActionPreference = "Stop" con
    # una redireccion de esa salida (2>...), PowerShell convierte esa escritura
    # en un error terminante aunque el exit code no importe. Listar evita el
    # problema de raiz.
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        $prevEap = $ErrorActionPreference
        $ErrorActionPreference = "SilentlyContinue"
        $installed = & py -0 2>$null
        $ErrorActionPreference = $prevEap
        foreach ($v in @("3.13", "3.12", "3.11")) {
            if ($installed -match [regex]::Escape($v)) { return @("py", "-$v") }
        }
    }

    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { return @("python") }

    throw "No se encuentra un Python 3.11-3.13 utilizable. Instala Python desde " +
          "https://www.python.org/downloads/ marcando 'Add python.exe to PATH' " +
          "y vuelve a ejecutar este script (o pasa -PythonExe con la ruta exacta)."
}

$pyBase = Resolve-Python -Override $PythonExe

function Invoke-Python {
    $extra = if ($pyBase.Count -gt 1) { $pyBase[1..($pyBase.Count - 1)] } else { @() }
    & $pyBase[0] @extra @args
    if ($LASTEXITCODE -ne 0) { throw "Fallo '$($pyBase -join ' ') $args' (codigo $LASTEXITCODE)." }
}

$pyLabel = $pyBase -join " "
$version = (Invoke-Python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
Write-Host "  usando '$pyLabel' (Python $version)" -ForegroundColor DarkGray
if ([version]$version -lt [version]"3.11") {
    throw "Hace falta Python 3.11 o superior. Encontrado: $version"
}
if ([version]$version -ge [version]"3.14") {
    Write-Host "  aviso: Python $version es muy reciente; pandas/numpy pueden no " -ForegroundColor Yellow
    Write-Host "  tener ruedas precompiladas todavia. Si falla la instalacion," -ForegroundColor Yellow
    Write-Host "  usa -PythonExe o instala una 3.11-3.13 (ver README)." -ForegroundColor Yellow
}

if (-not (Test-Path ".venv")) {
    Write-Host "  creando entorno virtual..." -ForegroundColor DarkGray
    Invoke-Python -m venv .venv
}

# El venv puede quedar en Scripts\ (Python nativo de Windows) o en bin\
# (Python de diseno POSIX, p.ej. via MSYS2/mingw). Se admite cualquiera.
$activate = ".venv\Scripts\Activate.ps1"
if (-not (Test-Path $activate)) { $activate = ".venv\bin\Activate.ps1" }
if (-not (Test-Path $activate)) { throw "No se encuentra el script de activacion del entorno virtual." }
& $activate

python -m pip install --upgrade pip --quiet
if ($LASTEXITCODE -ne 0) { throw "No se pudo actualizar pip (codigo $LASTEXITCODE)." }

Write-Host "  instalando dependencias..." -ForegroundColor DarkGray
pip install -e ".[dev]" --quiet
if ($LASTEXITCODE -ne 0) {
    throw "La instalacion de dependencias fallo (codigo $LASTEXITCODE). Revisa el " +
          "error de arriba -- normalmente es un paquete sin rueda precompilada " +
          "para esta version de Python. No sigas: el entorno no esta listo."
}

New-Item -ItemType Directory -Force -Path state\cache, state\db, state\logs, state\reports | Out-Null

Write-Host "`nListo." -ForegroundColor Green
Write-Host "  keepgarden config    valida la configuracion"
Write-Host "  keepgarden catalog   lista el catalogo de genes"
Write-Host "  pytest               corre los tests"
