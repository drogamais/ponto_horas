import os
from dotenv import load_dotenv
from urllib.parse import quote_plus

# Carrega .env automaticamente
load_dotenv()

# ---------------------------
# FIREBIRD (ORIGEM)
# ---------------------------

FB_IP = os.getenv("FB_IP")

FB_CAMINHOS = [
    os.getenv("FB_CAMINHO_1"),
    os.getenv("FB_CAMINHO_2"),
    os.getenv("FB_CAMINHO_3"),
]

FB_DRIVER = os.getenv("FB_DRIVER")
FB_USER = os.getenv("FB_USER")
FB_PASS = os.getenv("FB_PASS")

# Mapa para criar chave única (CHAVE = CODIGO + ID_FONTE)
FONTE_MAP = {
    FB_CAMINHOS[0]: 1,
    FB_CAMINHOS[1]: 2,
    FB_CAMINHOS[2]: 3
}

# ---------------------------
# MARIADB (DESTINO)
# ---------------------------

MARIA_USER = os.getenv("MARIA_USER")
MARIA_PASS = os.getenv("MARIA_PASS")
MARIA_HOST = os.getenv("MARIA_HOST")
MARIA_PORT = os.getenv("MARIA_PORT")
MARIA_DB = os.getenv("MARIA_DB")

# ---------------------------
# TABELAS PARA MIGRAR
# ---------------------------

TABELAS_ALVO = os.getenv("TABELAS_ALVO").split(",")

# ---------------------------
# FUNÇÕES DE CONFIG
# ---------------------------

def get_firebird_conn_str(caminho: str) -> str:
    """String de conexão Firebird por caminho."""
    return (
        f"DRIVER={{{FB_DRIVER}}};"
        f"DBNAME={FB_IP}:{caminho};"
        f"UID={FB_USER};PWD={FB_PASS};CHARSET=NONE;"
    )

def get_mariadb_uri():
    senha_segura = quote_plus(MARIA_PASS)
    return (
        f"mariadb+mariadbconnector://{MARIA_USER}:{senha_segura}"
        f"@{MARIA_HOST}:{MARIA_PORT}/{MARIA_DB}"
    )
