@echo off
title Compilador de AV1 Transcoder
echo ==============================================
echo GENERANDO EJECUTABLE (.EXE)
echo ==============================================
echo.

:: 1. Activar entorno virtual
if not exist "venv\" (
    echo [!] No se encuentra el entorno virtual. Ejecute iniciar.bat primero.
    pause
    exit
)
call venv\Scripts\activate.bat

:: 2. Instalar pyinstaller si no esta
echo [*] Verificando PyInstaller...
pip install pyinstaller -q

:: 3. Limpiar carpetas previas (evita errores de permisos con OneDrive)
echo [*] Limpiando carpetas anteriores...
if exist "build\" rd /s /q "build"
if exist "dist\AV1_Transcoder.exe" del /f /q "dist\AV1_Transcoder.exe"

:: 4. Compilar
:: --noconsole: Para que no abra ventana de comandos al abrir el exe
:: --onefile: Para que todo este en un solo archivo
:: --name: Nombre del ejecutable
echo [*] Iniciando proceso de compilacion (esto puede tardar unos minutos)...
pyinstaller --noconsole --onefile --name "AV1_Transcoder" transcode_av1.py

echo.
echo ==============================================
echo PROCESO FINALIZADO.
echo Revisa la carpeta "dist" para encontrar tu AV1_Transcoder.exe
echo.
echo IMPORTANTE: No olvides poner HandBrakeCLI.exe en la misma 
echo carpeta que el nuevo .exe generado.
echo ==============================================
pause
