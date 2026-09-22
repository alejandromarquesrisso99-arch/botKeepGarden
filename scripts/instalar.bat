@echo off
rem ===================================================================
rem Pone botKeepGarden en el escritorio.
rem
rem Doble clic y ya. Es un .bat que llama a PowerShell con
rem -ExecutionPolicy Bypass, asi que no hay que tocar la politica de
rem ejecucion de la maquina: mismo truco que setup.bat.
rem ===================================================================

cd /d "%~dp0.."

powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\crear_acceso_directo.ps1"

echo.
pause
