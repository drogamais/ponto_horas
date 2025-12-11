@echo off
echo Ativando ambiente virtual...
call venv\Scripts\activate

echo Criando arquivo de log com data e hora...

rem sobrescreve o arquivo e coloca apenas a data e hora no topo
echo === LOG INICIADO EM %date% %time% === > app.log
echo. >> app.log

echo Executando main.py...
python main.py >> app.log 2>&1

echo.
echo Finalizado! O arquivo app.log foi criado/atualizado.
