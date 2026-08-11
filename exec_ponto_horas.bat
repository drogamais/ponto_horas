@echo off
REM ===================================================================
REM  Executa o ponto_horas.py COMPLETO (copia .FBK -> bronze -> silver
REM  -> swap). Use este .bat na Tarefa Agendada do Windows.
REM
REM  Precisa rodar com um usuario que enxergue o share do AtecSoft
REM  (\\10.48.12.2\...) — o acesso e pelo login do Windows, nao ha senha
REM  guardada em lugar nenhum.
REM ===================================================================
setlocal enabledelayedexpansion

cd /d %~dp0

set PYTHON_EXE=%~dp0venv\Scripts\python.exe
if not exist "!PYTHON_EXE!" (
    echo ERRO: Python do venv nao encontrado em !PYTHON_EXE!
    echo Crie o venv com:  python -m venv venv ^&^& venv\Scripts\pip install -r requirements.txt
    exit /b 1
)

REM --- data no formato AAAA-MM-DD, independente do locale ---
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set SAFE_DATE=%%i

REM --- mantem um log por dia, em logs\ ---
if not exist "logs" mkdir logs
set LOG=logs\ponto_horas_!SAFE_DATE!.log

echo === EXECUCAO INICIADA EM !SAFE_DATE! %time% === >> "!LOG!"
echo. >> "!LOG!"

echo Executando ponto_horas.py (completo)...
"!PYTHON_EXE!" ponto_horas.py %* >> "!LOG!" 2>&1
set RC=!ERRORLEVEL!

echo. >> "!LOG!"
echo === FIM (codigo !RC!) em %time% === >> "!LOG!"

if !RC! NEQ 0 (
    echo FALHOU ^(codigo !RC!^). Veja o log: !LOG!
) else (
    echo Concluido com sucesso. Log: !LOG!
)

endlocal & exit /b %RC%
