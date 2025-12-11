@echo off
echo Ativando ambiente virtual...
call venv\Scripts\activate

echo Executando migracao...
python main.py

echo.
echo Finalizado!
