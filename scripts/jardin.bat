@echo off
setlocal
rem ===================================================================
rem La aplicacion: arranca el jardin y abre el visor en el navegador.
rem
rem Doble clic y ya. Es un .bat y no un .ps1 a proposito, igual que
rem setup.bat: no depende de la politica de ejecucion de PowerShell.
rem
rem   scripts\jardin.bat              jardin vivo, una vela por hora
rem   scripts\jardin.bat --dry-run    recorre historico acelerado
rem ===================================================================

cd /d "%~dp0.."

set "VPY=.venv\Scripts\python.exe"
if not exist "%VPY%" set "VPY=.venv\bin\python.exe"
if not exist "%VPY%" (
    echo No hay entorno preparado. Ejecuta primero scripts\setup.bat
    echo.
    pause
    exit /b 1
)

if not exist "state\db\garden.db" (
    echo No hay ningun jardin todavia. Siembralo con:
    echo.
    echo     keepgarden data backfill --symbol BTC/USDT --timeframe 1h --since 2019-01-01
    echo     keepgarden garden seed --size 60
    echo.
    pause
    exit /b 1
)

echo botKeepGarden - abriendo la aplicacion
echo Cierra esta ventana o pulsa Ctrl+C para parar el jardin.
echo.

"%VPY%" -m keepgarden app %*

rem Si ha fallado, la ventana no se cierra sola: si no, el error no se lee.
if errorlevel 1 pause
