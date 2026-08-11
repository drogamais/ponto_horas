"""
Extração completa do ponto (AtecSoft/Firebird → MariaDB dbDrogamaisRH).

Script ÚNICO — substitui main.py + extract_rh_full.py + config.py +
capturar_afd.py + enriquecer_ponto.py.

POR QUE LÊ DO BACKUP .FBK E NÃO DO SERVIDOR
O Firebird do DC001 sobe junto com o AtecSoft (que é usado só à tarde) e fica
fora do ar o resto do tempo — a porta 3050 não responde, e o script antigo
falhava com "-902 Unable to complete network request". Já o .FBK está sempre no
share, gerado quando o AtecSoft é aberto. Assim este script roda a QUALQUER HORA.

De brinde, o gbak resolve a versão: as bases são ODS 10 (Firebird 1.x) e as
bibliotecas atuais não as abririam direto — o restore converte o formato.

O QUE FAZ, EM ORDEM
  1. Copia os .FBK do share (o acesso usa o seu login do Windows)
  2. gbak restaura cada um num .FDB temporário local
  3. BRONZE — espelho cru de cada tabela em `fb_<tabela>`
  4. SILVER — tabelas do app com CHAVE_FUNC_UNICA, em `_stg`
  5. SWAP — `_stg` → live, preservando as batidas que vieram do AFD
  6. Descarta os temporários

As três bases (uma por empresa) são unidas pela coluna ID_FONTE — é o que
distingue a mesma pessoa cadastrada em empresas diferentes.

AS BATIDAS DO AFD NÃO VÊM DAQUI
Quem captura os relógios é o agente coletor (repo agentes-rh), continuamente.
O AtecSoft não tem essas batidas, então o swap as apagaria — por isso os passos
`guardar_batidas_afd` / `reaplicar_batidas_afd` em volta dele.

USO
    venv\\Scripts\\activate
    python ponto_horas.py                 # tudo (copia, bronze, silver, swap)
    python ponto_horas.py --bronze        # só a camada bronze (fb_*)
    python ponto_horas.py --silver        # só a silver + swap
    python ponto_horas.py --sem-copia     # reaproveita os .FBK já copiados
"""
import argparse
import os
import shutil
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote_plus

import firebird.driver as fb
import polars as pl
import requests
import urllib3
from dotenv import load_dotenv
from sqlalchemy import create_engine, inspect as sa_inspect, text

# os REP usam certificado self-signed; o aviso polui o log
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# ─────────────────────────────── configuração ────────────────────────────────

FB_USER = os.getenv("FB_USER", "SYSDBA")
FB_PASS = os.getenv("FB_PASS", "masterkey")

# Backups .FBK no share do AtecSoft. O número vira a coluna ID_FONTE (empresa).
#
# Cada FB_FBK_N pode ser a PASTA de backup (recomendado — pegamos o .FBK mais
# recente dela) ou um arquivo específico. A pasta é melhor porque uma das fontes
# gera nome com data (PONTO_ATECSOFT_10082026.FBK): com arquivo fixo o script
# pararia de achar no dia seguinte.
FONTE_FBK = {
    1: os.getenv("FB_FBK_1"),
    2: os.getenv("FB_FBK_2"),
    3: os.getenv("FB_FBK_3"),
}

# gbak: informe GBAK_EXE no .env se não estiver no PATH.
GBAK = os.getenv("GBAK_EXE", "gbak")

DIR_TRABALHO = Path(os.getenv("PONTO_DIR_TRABALHO", BASE_DIR / "_trabalho"))

# Avisa se o backup estiver velho — sinal de que ninguém abriu o AtecSoft.
MAX_IDADE_HORAS = int(os.getenv("MAX_IDADE_BACKUP_H", "48"))

MARIA = {
    "user": os.getenv("MARIA_USER"),
    "pass": os.getenv("MARIA_PASS"),
    "host": os.getenv("MARIA_HOST"),
    "port": os.getenv("MARIA_PORT"),
    "db": os.getenv("MARIA_DB"),
}

# Bronze: espelho cru. Silver: só o que o app do RH consome.
TABELAS_BRONZE = [
    "BH", "CP", "CP_T", "DEPARTAMENTO", "EMPRESA", "EQUIP", "EQUIP_EMP",
    "FERIADO", "FUNCAO", "FUNCIONARIO", "GRUPO", "HORARIOS_LINHAS",
    "JUST_BAT", "JUSTFICATIVA", "LOG", "NSR", "OBS_CP", "USUARIO",
]
TABELAS_SILVER = (os.getenv("TABELAS_ALVO")
                  or "BH,CP,CP_T,FERIADO,FUNCIONARIO,LOG,JUSTFICATIVA").split(",")

NOMES_SILVER = {
    "BH": "fat_atec_banco_horas",
    "CP": "fat_atec_ponto_diario",
    "CP_T": "fat_atec_ponto_temp",
    "FERIADO": "dim_atec_feriado",
    "FUNCIONARIO": "dim_atec_funcionarios",
    "JUSTFICATIVA": "dim_atec_justificativa",
    "LOG": "fat_atec_logs",
}

# Coluna que forma a chave sintética em cada tabela (o nome do campo muda).
CHAVE_POR_TABELA = {
    "FUNCIONARIO": ("CODIGO", "CHAVE_FUNC_UNICA"),
    "CP": ("IDENT", "CHAVE_FUNC_UNICA"),
    "CP_T": ("IDENT", "CHAVE_FUNC_UNICA"),
    "BH": ("ID_FUNC", "CHAVE_FUNC_UNICA"),
    "JUSTFICATIVA": ("CODIGO", "CHAVE_JUST_UNICA"),
}

# RENAME TABLE troca o objeto inteiro: índices criados à mão se perdem no swap.
# Como a bronze/silver grava TUDO como texto (ver ler_tabela), as colunas chegam
# no MariaDB como TEXT — e indexar TEXT exige prefixo de tamanho, senao da
# "Specified key was too long". Por isso todo campo textual leva (N) aqui.
INDICES_POS_SWAP = {
    "fat_atec_ponto_diario": {
        "idx_ponto_data_chave": "DATA_BATIDA(10), CHAVE_FUNC_UNICA(50)",
    },
    "dim_atec_justificativa": {
        "idx_justificativa_codigo_fonte": "CODIGO(20), ID_FONTE",
    },
}

# O AtecSoft trunca o motivo em 10 caracteres e escreve de várias formas. Aqui
# viram um nome canônico + ID estável, pro RH agrupar "Atestado" e
# "ATESTADO MEDICO" como a mesma coisa.
DE_PARA_JUSTIFICATIVAS = {
    "Atestado": "ATESTADO MEDICO", "INSS": "AFASTAMENTO INSS",
    "Afastament": "AFASTAMENTO", "Luto": "LICENCA NOJO (LUTO)",
    "LUTO": "LICENCA NOJO (LUTO)", "Falta": "FALTA",
    "falta desc": "FALTA DESCONTADA", "Falta desc": "FALTA DESCONTADA",
    "Aus. Just": "AUSENCIA JUSTIFICADA", "Aus. Justi": "AUSENCIA JUSTIFICADA",
    "Abono": "ABONO", "COMP. HORA": "COMPENSACAO DE HORAS",
    "comp. hrs": "COMPENSACAO DE HORAS", "Comp Horas": "COMPENSACAO DE HORAS",
    "Home Offic": "HOME OFFICE", "Dispensado": "DISPENSA",
    "Férias": "FERIAS", "Folga": "FOLGA", "Feriado": "FERIADO",
    "Lic.Patern": "LICENCA PATERNIDADE", "Lic.Matern": "LICENCA MATERNIDADE",
    "Casamento": "LICENCA GALA (CASAMENTO)", "Lic. Casam": "LICENCA GALA (CASAMENTO)",
    "Guarda M.": "GUARDA MIRIM", "Guarda Mir": "GUARDA MIRIM",
    "DAY OFF": "DAY OFF", "Day off": "DAY OFF",
    "Declaração": "DECLARACAO", "Dec. Acomp": "DECLARACAO ACOMPANHANTE",
    "Viagem Emp": "VIAGEM A TRABALHO", "Viagem emp": "VIAGEM A TRABALHO",
    "Aviso": "AVISO PREVIO", "JOGOS BR": "JOGOS DO BRASIL",
}

ID_PADRAO_JUSTIFICATIVA = {
    "ATESTADO MEDICO": "1", "FALTA": "2", "DISPENSA": "3", "FERIAS": "4",
    "FOLGA": "5", "LICENCA PATERNIDADE": "6", "LICENCA MATERNIDADE": "7",
    "LICENCA NOJO (LUTO)": "8", "AFASTAMENTO INSS": "9", "DAY OFF": "10",
    "COMPENSACAO DE HORAS": "11", "DECLARACAO": "12", "VIAGEM A TRABALHO": "13",
    "FALTA DESCONTADA": "14", "LICENCA GALA (CASAMENTO)": "15",
    "GUARDA MIRIM": "16", "ABONO": "17", "HOME OFFICE": "18",
    "AUSENCIA JUSTIFICADA": "19", "AVISO PREVIO": "20", "JOGOS DO BRASIL": "21",
    "FERIADO": "22", "DECLARACAO ACOMPANHANTE": "23", "AFASTAMENTO": "24",
}


def uri_mariadb() -> str:
    return (f"mariadb+mariadbconnector://{MARIA['user']}:{quote_plus(MARIA['pass'])}"
            f"@{MARIA['host']}:{MARIA['port']}/{MARIA['db']}")


# ────────────────────────── copiar e restaurar o .FBK ────────────────────────

def resolver_fbk(origem: str) -> Path | None:
    """Aceita pasta ou arquivo; devolve o .FBK a usar.

    Sendo pasta, escolhe o .FBK MAIS RECENTE — é o que torna o script imune ao
    nome com data que uma das fontes gera a cada dia.
    """
    p = Path(origem)
    if p.is_dir():
        candidatos = sorted(
            [f for f in p.glob("*") if f.suffix.upper() == ".FBK"],
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        return candidatos[0] if candidatos else None
    return p if p.exists() else None


def copiar_backups() -> dict[int, Path]:
    """Traz os .FBK do share pra uma pasta local.

    Copiamos antes de restaurar porque o gbak lendo direto do share é lento e
    sensível a oscilação de rede. A cópia usa o SEU login do Windows — é o que
    dá acesso ao compartilhamento, sem senha em arquivo nenhum.
    """
    DIR_TRABALHO.mkdir(parents=True, exist_ok=True)
    copiados: dict[int, Path] = {}

    for id_fonte, origem in sorted(FONTE_FBK.items()):
        if not origem:
            continue
        src = resolver_fbk(origem)
        if src is None:
            print(f"  [ERRO] fonte {id_fonte}: nenhum .FBK em {origem}")
            continue

        idade_h = (time.time() - src.stat().st_mtime) / 3600
        destino = DIR_TRABALHO / f"fonte_{id_fonte}.fbk"
        mb = src.stat().st_size / (1 << 20)
        try:
            shutil.copy2(src, destino)
            aviso = ""
            if idade_h > MAX_IDADE_HORAS:
                # Backup velho = ninguém abriu o AtecSoft. Os dados vão sair
                # defasados, e é melhor avisar do que gravar em silêncio.
                aviso = f"  [ATENÇÃO] backup com {idade_h:.0f}h — dados podem estar defasados"
            print(f"  fonte {id_fonte}: {src.name} ({mb:.1f} MB, {idade_h:.0f}h atrás){aviso}")
            copiados[id_fonte] = destino
        except Exception as e:
            print(f"  [ERRO] fonte {id_fonte}: falha ao copiar — {e}")
    return copiados


def restaurar(fbk: Path, fdb: Path) -> Path:
    """Restaura o .FBK num .FDB local com gbak (converte o ODS antigo)."""
    if fdb.exists():
        fdb.unlink()
    proc = subprocess.run(
        [GBAK, "-c", "-user", FB_USER, "-password", FB_PASS, str(fbk), str(fdb)],
        capture_output=True, text=True, timeout=1800,
    )
    if proc.returncode != 0 or not fdb.exists():
        saida = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"gbak falhou: {saida[:300]}")
    return fdb


def abrir_fontes(fbks: dict[int, Path]) -> dict[int, tuple[object, set[str]]]:
    """Restaura e abre cada base. Fonte com problema não aborta as outras."""
    fontes: dict[int, tuple[object, set[str]]] = {}
    for id_fonte, fbk in sorted(fbks.items()):
        try:
            fdb = restaurar(fbk, DIR_TRABALHO / f"fonte_{id_fonte}.fdb")
            con = fb.connect(str(fdb), user=FB_USER, password=FB_PASS, charset="NONE")
            existentes = tabelas_da_fonte(con)
            fontes[id_fonte] = (con, existentes)
            print(f"  fonte {id_fonte}: restaurada e aberta ({len(existentes)} tabelas)")
        except Exception as e:
            print(f"  [ERRO] fonte {id_fonte}: {str(e)[:200]}")
    return fontes


def tabelas_da_fonte(con) -> set[str]:
    """Tabelas de usuário da base (evita erro em fonte incompleta)."""
    cur = con.cursor()
    cur.execute("""
        SELECT RDB$RELATION_NAME FROM RDB$RELATIONS
         WHERE (RDB$SYSTEM_FLAG = 0 OR RDB$SYSTEM_FLAG IS NULL)
           AND RDB$VIEW_BLR IS NULL
    """)
    return {r[0].strip() for r in cur.fetchall()}


# ──────────────────────────────── leitura ────────────────────────────────────

def limpar_texto(v):
    """Corrige o encoding do Firebird (bytes latin1 que na verdade são cp1252)."""
    if isinstance(v, bytes):
        try:
            return v.decode("cp1252")
        except Exception:
            return v.decode("latin1", errors="replace")
    return v


def _tipo_polars(tipo_py):
    """Traduz o tipo que o cursor do Firebird informa para o tipo do polars.

    Sem isso o polars infere pela primeira linha e estoura na seguinte
    ("could not append value: 16:19:00 of type: time"). E gravar tudo como texto
    tambem nao serve: as colunas chegam no MariaDB como TEXT e o app do RH
    quebra — o espelho de ponto usa WEEKDAY(DATA_BATIDA) e TIMESTAMPDIFF, que
    precisam de DATE/TIME de verdade.
    """
    import datetime
    import decimal

    return {
        datetime.datetime: pl.Datetime,
        datetime.date: pl.Date,
        datetime.time: pl.Time,
        decimal.Decimal: pl.Float64,
        int: pl.Int64,
        float: pl.Float64,
        bool: pl.Boolean,
        str: pl.String,
        bytes: pl.String,  # decodificamos em limpar_texto()
    }.get(tipo_py, pl.String)


def ler_tabela(con, tabela: str, id_fonte: int) -> pl.DataFrame | None:
    """Lê a tabela inteira e devolve DataFrame já com o ID_FONTE.

    Não usamos pl.read_database: com charset=NONE o driver entrega texto como
    `bytes` e o polars recusa montar o DataFrame ("value ... of type binary").
    O pyodbc convertia sozinho; o firebird-driver não.

    Os tipos vêm do próprio cursor (ver _tipo_polars) para chegarem no MariaDB
    como DATE/TIME/INT de verdade — o app do RH depende disso.
    """
    try:
        cur = con.cursor()
        cur.execute(f'SELECT * FROM "{tabela}"')
        colunas = [d[0] for d in cur.description]
        esquema = {d[0]: _tipo_polars(d[1]) for d in cur.description}

        linhas = [
            [limpar_texto(v) if isinstance(v, bytes) else v for v in linha]
            for linha in cur.fetchall()
        ]
        if not linhas:
            return None

        try:
            df = pl.DataFrame(linhas, schema=esquema, orient="row")
        except Exception:
            # Coluna com tipo misto (acontece em base antiga): cai pra texto só
            # nessa tabela, em vez de perder a tabela inteira.
            df = pl.DataFrame(
                [[None if v is None else str(v) for v in linha] for linha in linhas],
                schema={c: pl.String for c in colunas}, orient="row",
            )
            print(f"    [AVISO] {tabela}: tipo misto, gravando como texto")

        return df.with_columns(pl.lit(id_fonte).alias("ID_FONTE").cast(pl.Int8))
    except Exception as e:
        print(f"    [ERRO] fonte {id_fonte} / {tabela}: {str(e)[:150]}")
        return None


def juntar_fontes(fontes, tabela: str) -> pl.DataFrame | None:
    """Lê a mesma tabela nas 3 bases e empilha, marcando o ID_FONTE."""
    partes = []
    for id_fonte, (con, existentes) in sorted(fontes.items()):
        if tabela not in existentes:
            continue
        df = ler_tabela(con, tabela, id_fonte)
        if df is not None:
            partes.append(df)
    if not partes:
        return None
    # diagonal: as bases podem ter colunas ligeiramente diferentes entre si.
    return pl.concat(partes, how="diagonal")


# ──────────────────────────────── bronze ─────────────────────────────────────

# Tabelas grandes (a CP tem ~75k linhas x 116 colunas) estouram o
# max_allowed_packet do MariaDB num INSERT unico — o servidor derruba a conexao
# com "Server has gone away". Por isso gravamos em lotes.
LOTE_LINHAS = int(os.getenv("PONTO_LOTE_LINHAS", "5000"))


def gravar(df: pl.DataFrame, tabela: str, engine) -> None:
    """Grava o DataFrame na tabela, em lotes se for grande.

    O primeiro lote recria a tabela (replace); os seguintes acrescentam (append).
    """
    if df.height <= LOTE_LINHAS:
        df.write_database(table_name=tabela, connection=engine,
                          if_table_exists="replace", engine="sqlalchemy")
        return
    for inicio in range(0, df.height, LOTE_LINHAS):
        df.slice(inicio, LOTE_LINHAS).write_database(
            table_name=tabela, connection=engine,
            if_table_exists="replace" if inicio == 0 else "append",
            engine="sqlalchemy",
        )


def bronze(fontes, engine) -> int:
    """Espelho cru do Firebird em fb_<tabela>. Sem chave sintética."""
    print("\n>>> BRONZE — espelho cru (fb_*)")
    ok = 0
    for tabela in sorted(TABELAS_BRONZE):
        df = juntar_fontes(fontes, tabela)
        if df is None:
            print(f"  {tabela}: sem dados, pulando")
            continue
        destino = f"fb_{tabela.lower()}"
        try:
            gravar(df, destino, engine)
            print(f"  {tabela} -> {destino} ({df.height} linhas)")
            ok += 1
        except Exception as e:
            print(f"  [ERRO] {destino}: {str(e)[:150]}")
    return ok


# ──────────────────────────────── silver ─────────────────────────────────────

def aplicar_justificativas(df: pl.DataFrame) -> pl.DataFrame:
    """Padroniza o motivo e dá um ID estável a cada um."""
    return df.with_columns(
        pl.col("NOME").replace_strict(DE_PARA_JUSTIFICATIVAS,
                                      default=pl.col("NOME")).alias("NOME_PADRAO")
    ).with_columns(
        pl.col("NOME_PADRAO").replace_strict(ID_PADRAO_JUSTIFICATIVA,
                                             default="999").alias("ID_FIXO")
    ).with_columns([
        (pl.col("NOME_PADRAO").str.slice(0, 3).str.to_uppercase() + "_" +
         pl.col("ID_FIXO")).alias("CHAVE_MAP_UNICA"),
        pl.col("ID_FIXO").cast(pl.Int32).alias("ID_MAP_INT"),
    ]).drop("ID_FIXO")


def silver(fontes, engine) -> list[str]:
    """Grava as tabelas do app em _stg (o swap vem depois)."""
    print("\n>>> SILVER — tabelas do app com CHAVE_FUNC_UNICA")
    gravadas = []
    for tabela in TABELAS_SILVER:
        tabela = tabela.strip().upper()
        if not tabela:
            continue
        df = juntar_fontes(fontes, tabela)
        if df is None:
            print(f"  {tabela}: sem dados, pulando (staging não criado)")
            continue

        if tabela == "JUSTFICATIVA":
            df = aplicar_justificativas(df)

        if tabela in CHAVE_POR_TABELA:
            coluna, alias = CHAVE_POR_TABELA[tabela]
            if coluna in df.columns:
                df = df.with_columns(
                    (pl.col(coluna).cast(pl.Utf8) + "_" +
                     pl.col("ID_FONTE").cast(pl.Utf8)).alias(alias)
                )
            else:
                print(f"  [AVISO] {tabela}: coluna {coluna} ausente, sem {alias}")

        destino = NOMES_SILVER.get(tabela, tabela.lower())
        try:
            gravar(df, f"{destino}_stg", engine)
            gravadas.append(destino)
            print(f"  {tabela} -> {destino}_stg ({df.height} linhas)")
        except Exception as e:
            print(f"  [ERRO] {destino}_stg: {str(e)[:150]}")
    return gravadas


# ──────────────────── captura do AFD e enriquecimento ────────────────────────
#
# O AtecSoft não tem as batidas que ficaram só no relógio, então a _stg recém
# criada nasce com as colunas de horário vazias. Baixamos o AFD direto dos
# aparelhos e preenchemos a _stg ANTES do swap — quando a troca acontece, a
# tabela nova já está completa. Se algo aqui falhar, a produção nem foi tocada.

TAB_PONTO = "fat_atec_ponto_diario"
PARES = [f"{p}{i}" for i in range(1, 7) for p in ("E", "S")]
JANELA_DIAS = int(os.getenv("JANELA_DIAS", "30"))

CID_DEVICES = [u.strip() for u in os.getenv("CONTROLID_URLS", "").split(",") if u.strip()]
CID_USER = os.getenv("CONTROLID_USER", "admin")
CID_PASS = os.getenv("CONTROLID_PASS", "admin")

HENRY_DEVICES = [u.strip() for u in os.getenv("HENRY_URLS", "").split(",") if u.strip()]
HENRY_USER = os.getenv("HENRY_USER", "rep")
HENRY_PASS = os.getenv("HENRY_PASS", "")


def _afd_controlid(url: str) -> bytes:
    """Baixa o AFD de um Control iD (login + get_afd)."""
    ses = requests.Session()
    ses.verify = False
    r = ses.post(f"{url}/login.fcgi",
                 json={"login": CID_USER, "password": CID_PASS}, timeout=15)
    sessao = r.json().get("session", "")
    if not sessao:
        raise RuntimeError("login recusado")
    inicio = date.today() - timedelta(days=JANELA_DIAS)
    r = ses.post(f"{url}/get_afd.fcgi?session={sessao}",
                 json={"initial_date": {"day": inicio.day, "month": inicio.month,
                                        "year": inicio.year}}, timeout=120)
    r.raise_for_status()
    return r.content


def _afd_henry(url: str) -> bytes:
    """Baixa o AFD de um Henry (rep.html: login pgCode=7, AFD pgCode=8).

    O Henry aceita UMA sessão por vez: se alguém estiver com a página web do
    relógio aberta, ele devolve a página de login em vez do AFD — e faz isso
    ANTES de validar a senha, então parece erro de credencial e não é.
    """
    ses = requests.Session()
    inicio = date.today() - timedelta(days=JANELA_DIAS)

    def fmt(d):
        return f"{d.day:02d}/{d.month:02d}/{d.year % 100:02d}"

    ses.get(f"{url}/rep.html?pgCode=7&opType=1&lblId=0"
            f"&lblLogin={HENRY_USER}&lblPass={HENRY_PASS}", timeout=15)
    r = ses.get(f"{url}/rep.html?pgCode=8&opType=5&lblId=2&visibleDiv=communication"
                f"&lblNsrI=000000001&lblNsrF=999999999"
                f"&lblDataI={fmt(inicio)}+00:00&lblDataF={fmt(date.today())}+23:59",
                timeout=180)
    r.raise_for_status()
    primeira = r.content.split(b"\n", 1)[0].strip()
    if not (len(primeira) >= 9 and primeira[:9].isdigit()):
        raise RuntimeError("não veio AFD — alguém está com a página do relógio aberta?")
    return r.content


def capturar_afd() -> list[bytes]:
    """Baixa o AFD de todos os aparelhos. Aparelho que falhar não aborta os outros."""
    conteudos = []
    for url in CID_DEVICES:
        try:
            c = _afd_controlid(url)
            conteudos.append(c)
            print(f"  Control iD {url}: {len(c.splitlines())} linhas")
        except Exception as e:
            print(f"  [AVISO] Control iD {url}: {str(e)[:120]}")
    for url in HENRY_DEVICES:
        try:
            c = _afd_henry(url)
            conteudos.append(c)
            print(f"  Henry {url}: {len(c.splitlines())} linhas")
        except Exception as e:
            print(f"  [AVISO] Henry {url}: {str(e)[:120]}")
    return conteudos


def parsear_batidas(conteudos: list[bytes]) -> pl.DataFrame:
    """Extrai as marcações (registro tipo 3) dos AFDs, dos últimos JANELA_DIAS."""
    corte = date.today() - timedelta(days=JANELA_DIAS)
    registros = []
    for conteudo in conteudos:
        for raw in conteudo.splitlines():
            try:
                linha = raw.decode("latin1")
                if len(linha) < 34 or linha[9] != "3":
                    continue
                if len(linha) >= 50 and linha[13] == "-":
                    # REP-C (Portaria 671): NSR(9)+tipo(1)+ISO8601(24)+CPF(12)
                    dt = datetime.fromisoformat(linha[10:34])
                    ident = (linha[34:46].lstrip("0") or "0").zfill(11)
                else:
                    # Legado (1510): NSR(9)+tipo(1)+DDMMYYYY(8)+HHMM(4)+CPF(12)
                    dt = datetime(int(linha[14:18]), int(linha[12:14]), int(linha[10:12]),
                                  int(linha[18:20]), int(linha[20:22]))
                    ident = (linha[22:34].lstrip("0") or "0").zfill(11)
                if dt.date() < corte:
                    continue
                registros.append({"IDENT_AFD": ident, "DATA": dt.date(), "HORA": dt.time()})
            except Exception:
                continue
    esquema = {"IDENT_AFD": pl.String, "DATA": pl.Date, "HORA": pl.Time}
    if not registros:
        return pl.DataFrame(schema=esquema)
    # dedup entre aparelhos: a mesma batida pode vir de mais de um relógio
    return pl.DataFrame(registros, schema=esquema).unique(
        subset=["IDENT_AFD", "DATA", "HORA"])


def pivotar(df: pl.DataFrame) -> pl.DataFrame:
    """Agrupa (identificador, dia), ordena as horas e distribui em E1->S1->...->S6.

    As horas saem como pl.Time (nao string): assim a tabela auxiliar nasce com
    coluna TIME no MariaDB e o UPDATE nao depende de conversao implicita.
    """
    linhas = []
    for (ident, data), grupo in df.sort("HORA").group_by(["IDENT_AFD", "DATA"]):
        row = {"IDENT_AFD": ident, "DATA": data}
        for i, h in enumerate(grupo["HORA"].to_list()[:len(PARES)]):
            row[PARES[i]] = h
        linhas.append(row)
    esquema = {"IDENT_AFD": pl.String, "DATA": pl.Date}
    esquema.update({c: pl.Time for c in PARES})
    return pl.DataFrame(linhas, schema=esquema)


def enriquecer_staging(engine) -> int:
    """Preenche as colunas de batida na _stg com o AFD baixado dos relógios.

    Roda ANTES do swap: a staging fica completa e só então vira produção.

    O AFD identifica a pessoa só por CPF/PIS (não traz empresa), e o cruzamento é
    feito contra a própria staging de funcionários — que já tem a
    CHAVE_FUNC_UNICA com o ID_FONTE embutido.
    """
    stg_ponto = f"{TAB_PONTO}_stg"
    stg_func = "dim_atec_funcionarios_stg"

    print("\n>>> CAPTURANDO o AFD dos relógios")
    conteudos = capturar_afd()
    if not conteudos:
        print("  [AVISO] nenhum aparelho respondeu — staging segue sem as batidas")
        return 0

    batidas = parsear_batidas(conteudos)
    if batidas.is_empty():
        print("  [AVISO] nenhuma marcação no AFD")
        return 0
    print(f"  marcações tipo 3: {batidas.height}")

    pivot = pivotar(batidas)
    print(f"  funcionário x dia: {pivot.height}")

    # O AFD grava CPF em uns aparelhos e PIS em outros — unificamos os dois.
    def norm(coluna):
        return (pl.col(coluna).cast(pl.String)
                  .str.replace_all(r"[.\-/\s]", "").str.zfill(11))

    partes = []
    for campo in ("CPF", "PIS"):
        partes.append(
            pl.read_database(
                f"SELECT {campo} AS IDENT_AFD, CODIGO, ID_FONTE, CHAVE_FUNC_UNICA "
                f"  FROM {stg_func} "
                f" WHERE {campo} IS NOT NULL AND {campo} <> '' AND SITUACAO = 'ATIVO'",
                connection=engine,
            ).with_columns(norm("IDENT_AFD").alias("IDENT_AFD"))
        )
    func = pl.concat(partes).unique(subset=["IDENT_AFD", "CHAVE_FUNC_UNICA"])

    afd = (pivot.join(func, on="IDENT_AFD", how="inner")
                .drop("IDENT_AFD").rename({"DATA": "DATA_BATIDA"}))
    if afd.is_empty():
        print("  [AVISO] nenhum identificador do AFD cruzou com os funcionários")
        return 0

    # O mesmo funcionário pode vir por CPF (um aparelho) e por PIS (outro).
    # Fica a linha com mais batidas preenchidas.
    presentes = [c for c in PARES if c in afd.columns]
    afd = (afd.with_columns(
               pl.sum_horizontal([pl.col(c).is_not_null() for c in presentes]).alias("_n"))
              .sort("_n", descending=True)
              .unique(subset=["CHAVE_FUNC_UNICA", "DATA_BATIDA"], keep="first")
              .drop("_n"))
    print(f"  cruzamentos (funcionário x dia): {afd.height}")

    aux = "_afd_tmp"
    afd.write_database(table_name=aux, connection=engine,
                       if_table_exists="replace", engine="sqlalchemy")

    sets = ", ".join(f"d.`{c}` = COALESCE(d.`{c}`, s.`{c}`)" for c in presentes)
    with engine.connect() as conn:
        # FONTE_HORARIO é nossa (marca a origem da batida) — o AtecSoft não gera,
        # então a _stg nasce sem ela.
        conn.execute(text(f"ALTER TABLE `{stg_ponto}` "
                          f"ADD COLUMN `FONTE_HORARIO` VARCHAR(10) NULL"))
        # COALESCE: só preenche coluna VAZIA. Se o AtecSoft já trouxe a batida
        # (alguém corrigiu no sistema), o valor dele vence.
        r = conn.execute(text(
            f"UPDATE `{stg_ponto}` d "
            f"  JOIN `{aux}` s ON s.CHAVE_FUNC_UNICA = d.CHAVE_FUNC_UNICA "
            f"   AND s.DATA_BATIDA = d.DATA_BATIDA "
            f"   SET {sets}, d.FONTE_HORARIO = 'AFD'"))
        conn.execute(text(f"DROP TABLE IF EXISTS `{aux}`"))
        conn.commit()
    print(f"  staging enriquecida: {r.rowcount} linha(s)")
    return r.rowcount


def trocar_tabelas(engine, tabelas: list[str]) -> None:
    """Swap atômico _stg -> live, com a staging JÁ enriquecida.

    Tabela vazia não gera staging (ver silver), então nunca trocamos uma tabela
    boa por uma vazia se a origem falhar — é a proteção contra apagar produção.
    """
    if not tabelas:
        print("\n[AVISO] nenhuma staging gravada — swap não executado")
        return

    if TAB_PONTO in tabelas:
        enriquecer_staging(engine)

    print("\n>>> SWAP — staging para produção")
    existentes = set(sa_inspect(engine).get_table_names())
    with engine.connect() as conn:
        for nome in tabelas:
            stg, old = f"{nome}_stg", f"{nome}_old"
            conn.execute(text(f"DROP TABLE IF EXISTS `{old}`"))
            if nome in existentes:
                conn.execute(text(
                    f"RENAME TABLE `{nome}` TO `{old}`, `{stg}` TO `{nome}`"))
                conn.execute(text(f"DROP TABLE IF EXISTS `{old}`"))
            else:
                conn.execute(text(f"RENAME TABLE `{stg}` TO `{nome}`"))
            print(f"  {stg} -> {nome}")

            for idx, colunas in INDICES_POS_SWAP.get(nome, {}).items():
                try:
                    conn.execute(text(f"CREATE INDEX `{idx}` ON `{nome}` ({colunas})"))
                    print(f"    índice {idx} recriado")
                except Exception as e:
                    print(f"    [AVISO] índice {idx}: {str(e)[:100]}")
        conn.commit()


# ───────────────────────────────── entrada ───────────────────────────────────

def limpar_trabalho() -> None:
    """As bases restauradas e os .FBK copiados são descartáveis."""
    shutil.rmtree(DIR_TRABALHO, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Extração do ponto (AtecSoft .FBK → MariaDB)")
    ap.add_argument("--bronze", action="store_true", help="só a camada bronze (fb_*)")
    ap.add_argument("--silver", action="store_true", help="só a silver + swap")
    ap.add_argument("--sem-copia", action="store_true",
                    help="reaproveita os .FBK já copiados em _trabalho/")
    ap.add_argument("--manter", action="store_true",
                    help="não apaga os temporários no fim (para depurar)")
    args = ap.parse_args()

    fazer_bronze = args.bronze or not args.silver
    fazer_silver = args.silver or not args.bronze

    faltando = [k for k, v in MARIA.items() if not v]
    if faltando:
        print(f"[ERRO] faltam variáveis do MariaDB no .env: {faltando}")
        return 1
    if not any(FONTE_FBK.values()):
        print("[ERRO] nenhum backup configurado — defina FB_FBK_1/2/3 no .env")
        return 1
    if shutil.which(GBAK) is None and not Path(GBAK).exists():
        print(f"[ERRO] gbak não encontrado ({GBAK!r}). Instale o Firebird ou "
              f"aponte GBAK_EXE no .env para o gbak.exe")
        return 1

    print(">>> COPIANDO os backups do share")
    if args.sem_copia:
        fbks = {i: DIR_TRABALHO / f"fonte_{i}.fbk" for i in FONTE_FBK
                if (DIR_TRABALHO / f"fonte_{i}.fbk").exists()}
        print(f"  reaproveitando {len(fbks)} backup(s) já copiado(s)")
    else:
        fbks = copiar_backups()
    if not fbks:
        print("[ERRO] nenhum backup disponível. Abortando.")
        return 1

    print("\n>>> RESTAURANDO com gbak")
    fontes = abrir_fontes(fbks)
    if not fontes:
        print("[ERRO] nenhuma fonte pôde ser aberta. Abortando.")
        return 1

    engine = create_engine(uri_mariadb())
    try:
        if fazer_bronze:
            bronze(fontes, engine)
        if fazer_silver:
            trocar_tabelas(engine, silver(fontes, engine))
    finally:
        for con, _ in fontes.values():
            try:
                con.close()
            except Exception:
                pass
        if not args.manter:
            limpar_trabalho()

    print("\n>>> Concluído.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
