@echo off
setlocal enabledelayedexpansion

REM --- MUDA PARA A PASTA DO SCRIPT ---
cd /d %~dp0

REM --- CAMINHO ABSOLUTO DO PYTHON DO VENV ---
set PYTHON_EXE=%~dp0venv\Scripts\python.exe

REM --- GARANTE QUE O PYTHON EXISTE ---
if not exist "!PYTHON_EXE!" (
    echo ERRO: Python do venv não encontrado: !PYTHON_EXE!
    pause
    exit /b 1
)

REM --- GERA DATA SEGURA ---
for /f "tokens=1-3 delims=/- " %%a in ("%date%") do (
    set dd=%%a
    set mm=%%b
    set yyyy=%%c
)

set SAFE_DATE=!yyyy!-!mm!-!dd!

echo === LOG INICIADO EM !SAFE_DATE! %time% === > app.log
echo. >> app.log

echo Executando main.py...
"!PYTHON_EXE!" main.py >> app.log 2>&1

echo Finalizado!
endlocal
