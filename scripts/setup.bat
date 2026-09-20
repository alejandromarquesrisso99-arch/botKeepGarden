@echo off
setlocal enabledelayedexpansion
rem ===================================================================
rem Prepara el entorno de botKeepGarden.
rem
rem Se usa un .bat y no un .ps1 a proposito: los .bat no dependen de la
rem politica de ejecucion de PowerShell, asi que funcionan sin tocar
rem ningun permiso.
rem
rem Todo lo que ocurre se vuelca a state\logs\setup.log para poder
rem diagnosticar sin depender de copiar texto de la consola.
rem ===================================================================

cd /d "%~dp0.."
if not exist "state\logs" mkdir "state\logs"
set "LOG=state\logs\setup.log"

echo botKeepGarden - preparando entorno
echo.

> "%LOG%" echo === botKeepGarden setup ===
>> "%LOG%" echo fecha: %DATE% %TIME%
>> "%LOG%" echo carpeta: %CD%
>> "%LOG%" echo.

rem --- 1. Elegir interprete -----------------------------------------
>> "%LOG%" echo === versiones que conoce el lanzador py ===
py -0 >> "%LOG%" 2>&1

set "PYEXE=py"
set "PYARG=-3.13"
py -3.13 -c "print('ok')" >nul 2>&1
if errorlevel 1 (
    >> "%LOG%" echo el lanzador no sirve para 3.13; se usa la ruta directa
    set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
    set "PYARG="
)

>> "%LOG%" echo.
>> "%LOG%" echo === interprete elegido: !PYEXE! !PYARG! ===
"!PYEXE!" !PYARG! -c "import sys; print(sys.version); print(sys.executable)" >> "%LOG%" 2>&1
if errorlevel 1 goto fail

rem --- 2. Entorno virtual -------------------------------------------
echo   creando entorno virtual...
>> "%LOG%" echo.
>> "%LOG%" echo === creando entorno virtual ===
if exist ".venv" rmdir /s /q ".venv"
"!PYEXE!" !PYARG! -m venv .venv >> "%LOG%" 2>&1
if errorlevel 1 goto fail

set "VPY=.venv\Scripts\python.exe"
if not exist "%VPY%" set "VPY=.venv\bin\python.exe"
if not exist "%VPY%" (
    >> "%LOG%" echo ERROR: no se encuentra python dentro de .venv
    goto fail
)
>> "%LOG%" echo python del entorno: %VPY%

rem --- 3. Dependencias ----------------------------------------------
echo   actualizando pip...
>> "%LOG%" echo.
>> "%LOG%" echo === pip ===
"%VPY%" -m pip install --upgrade pip >> "%LOG%" 2>&1

echo   instalando dependencias (varios minutos, no cierres la ventana)...
>> "%LOG%" echo.
>> "%LOG%" echo === dependencias ===
"%VPY%" -m pip install -e ".[dev]" >> "%LOG%" 2>&1
if errorlevel 1 goto fail

rem --- 4. Carpetas de estado ----------------------------------------
for %%d in (cache db logs reports) do if not exist "state\%%d" mkdir "state\%%d"

rem --- 5. Comprobaciones --------------------------------------------
echo   comprobando...
>> "%LOG%" echo.
>> "%LOG%" echo === keepgarden config ===
"%VPY%" -m keepgarden.cli config >> "%LOG%" 2>&1

>> "%LOG%" echo.
>> "%LOG%" echo === pytest ===
"%VPY%" -m pytest -q >> "%LOG%" 2>&1

>> "%LOG%" echo.
>> "%LOG%" echo RESULTADO: LISTO
echo.
echo ================================================
echo  Listo. El detalle esta en state\logs\setup.log
echo ================================================
goto end

:fail
>> "%LOG%" echo.
>> "%LOG%" echo RESULTADO: FALLO
echo.
echo ================================================
echo  Ha fallado algo. Dile a Claude que lea el log:
echo  state\logs\setup.log
echo ================================================

:end
echo.
pause
