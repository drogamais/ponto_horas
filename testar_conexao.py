"""Confere se o .env esta completo e se o MariaDB responde.

Chamado pelo instalar.ps1, mas util sozinho:
    venv\\Scripts\\python.exe testar_conexao.py
"""
import sys

try:
    import ponto_horas as ph
    from sqlalchemy import create_engine, text
except Exception as e:
    print(f"    X   nao consegui importar o projeto: {str(e)[:150]}")
    sys.exit(1)

faltam = [k for k, v in ph.MARIA.items() if not v]
if faltam:
    print(f"    !   .env incompleto, faltam: {faltam}")
    sys.exit(0)

try:
    with create_engine(ph.uri_mariadb()).connect() as c:
        c.execute(text("SELECT 1"))
    print("    OK  MariaDB respondeu")
except Exception as e:
    print(f"    X   MariaDB: {str(e)[:150]}")
    sys.exit(1)

if not any(ph.FONTE_FBK.values()):
    print("    !   FB_FBK_1/2/3 nao configurados no .env")
    sys.exit(0)

achou = 0
for id_fonte, origem in sorted(ph.FONTE_FBK.items()):
    if not origem:
        continue
    fbk = ph.resolver_fbk(origem)
    if fbk:
        achou += 1
    else:
        print(f"    !   fonte {id_fonte}: nenhum .FBK em {origem}")
print(f"    OK  {achou} backup(s) acessivel(is)")

if ph.shutil.which(ph.GBAK) is None and not ph.Path(ph.GBAK).exists():
    print(f"    X   gbak nao encontrado ({ph.GBAK})")
    sys.exit(1)
print("    OK  gbak disponivel")
