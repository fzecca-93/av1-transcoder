@echo off
title Sistema de Transcodificacion AV1 para tu NAS
echo ==============================================
echo MOTOR DE AV1 - LANZADOR AUTOMATIZADO
echo ==============================================
echo.

:: 1. Verificación del Entorno Virtual (VENV)
if not exist "venv\" (
    echo [*] Detectada primera ejecucion. Construyendo entorno de aislamiento o virtual environment ^(Esto solo pasa 1 vez^)...
    python -m venv venv
) else (
    echo [*] Entorno virtual ya montado.
)

:: 2. Activación e instalación transparente de dependencias 
echo [*] Cargando paquetes de datos en el entorno virtual activo...
call venv\Scripts\activate.bat
pip install -r requirements.txt -q
echo [*] Dependencias resueltas de forma exitosa.
echo.

:: 3. Ejecutar y abrir seleccion visual
echo [*] Inicializando modulo visual de seleccion de carpetas de origen/destino...
python transcode_av1.py

:: 4. Evitar que se cierre abruptamente
echo.
echo ==============================================
echo Proceso de conversiones masivo finalizado.
pause
