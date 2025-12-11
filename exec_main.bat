@echo off
setlocal enableextensions enabledelayedexpansion

echo Ativando ambiente virtual...
call venv\Scripts\activate

echo Criando arquivo de log com data segura...

rem converte data local para YYYY-MM-DD (compatível com agendador)
for /f "tokens=1-3 delims=/" %%a in ("%date%") do (
    set yyyy=%%c
    set mm=%%b
    set dd=%%a
)

set SAFE_DATE=%yyyy%-%mm%-%dd%

echo === LOG INICIADO EM %SAFE_DATE% %time% === > app.log
echo. >> app.log

echo Executando main.py...
python main.py >> app.log 2>&1

echo.
echo Finalizado! O arquivo app.log foi criado/atualizado.

endlocal
