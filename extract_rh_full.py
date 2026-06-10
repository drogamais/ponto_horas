import polars as pl
import pyodbc
from sqlalchemy import create_engine, text
from config import FONTE_MAP, get_firebird_conn_str, get_mariadb_uri

PREFIXO = "fb_"

TABELAS_ALVO = [
    "BH", "CP", "CP_T", "DEPARTAMENTO", "EMPRESA",
    "FERIADO", "FUNCAO", "FUNCIONARIO", "GRUPO",
    "HORARIOS_LINHAS", "JUST_BAT", "JUSTFICATIVA",
    "LOG", "NSR", "OBS_CP", "USUARIO",
]


def tabelas_existentes_na_fonte(conn, alvo: list[str]) -> list[str]:
    """Retorna quais tabelas de `alvo` existem de fato na fonte Firebird."""
    cursor = conn.cursor()
    cursor.execute("""
        SELECT RDB$RELATION_NAME
        FROM RDB$RELATIONS
        WHERE (RDB$SYSTEM_FLAG = 0 OR RDB$SYSTEM_FLAG IS NULL)
          AND RDB$VIEW_BLR IS NULL
    """)
    existentes = {row[0].strip() for row in cursor.fetchall()}
    return [t for t in alvo if t in existentes]


def limpar_texto(valor):
    if valor is None:
        return None
    if isinstance(valor, str):
        try:
            return valor.encode('latin1').decode('cp1252')
        except Exception:
            return valor
    elif isinstance(valor, bytes):
        try:
            return valor.decode('cp1252')
        except Exception:
            return str(valor)
    return valor


def extrair_tabela(conn, tabela: str, id_fonte: int) -> pl.DataFrame | None:
    try:
        df = pl.read_database(f'SELECT * FROM "{tabela}"', connection=conn)
        if df.is_empty():
            return None
        return df.with_columns(pl.lit(id_fonte).alias("ID_FONTE").cast(pl.Int8))
    except Exception as e:
        print(f"  [ERRO] Fonte {id_fonte} / tabela {tabela}: {e}")
        return None


def limpar_df(df: pl.DataFrame) -> pl.DataFrame:
    colunas_str = [c for c, d in df.schema.items() if d == pl.String]
    if not colunas_str:
        return df
    df = df.with_columns([
        pl.col(c)
          .str.replace_all(r"^\(NULL\)$", "", literal=False)
          .str.strip_chars()
          .replace("", None)
          .alias(c)
        for c in colunas_str
    ])
    df = df.with_columns([
        pl.col(c).map_elements(limpar_texto, return_dtype=pl.String).alias(c)
        for c in colunas_str
    ])
    return df


def main():
    print(">>> Iniciando extração completa Firebird → MariaDB (dbDrogamaisRH)")

    engine_dest = create_engine(get_mariadb_uri())

    # --- Conecta e verifica quais tabelas alvo existem em cada fonte ---
    tabelas_por_fonte: dict[str, list[str]] = {}
    conexoes: dict[str, pyodbc.Connection] = {}

    for caminho, id_fonte in FONTE_MAP.items():
        try:
            conn = pyodbc.connect(get_firebird_conn_str(caminho))
            presentes = tabelas_existentes_na_fonte(conn, TABELAS_ALVO)
            tabelas_por_fonte[caminho] = presentes
            conexoes[caminho] = conn
            print(f"Fonte {id_fonte}: {len(presentes)} tabelas alvo encontradas.")
        except Exception as e:
            print(f"[ERRO] Não foi possível conectar à fonte {id_fonte}: {e}")

    if not conexoes:
        print("Nenhuma fonte disponível. Abortando.")
        return

    todas_tabelas: set[str] = set()
    for tabelas in tabelas_por_fonte.values():
        todas_tabelas.update(tabelas)

    print(f"\nTabelas a processar: {sorted(todas_tabelas)}")

    # --- Extrai e grava cada tabela ---
    ok, erros = 0, 0

    for tabela in sorted(todas_tabelas):
        nome_destino = f"{PREFIXO}{tabela.lower()}"
        print(f"\n--- {tabela} → {nome_destino} ---")

        dfs = []
        for caminho, id_fonte in FONTE_MAP.items():
            conn = conexoes.get(caminho)
            if conn is None:
                continue
            if tabela not in tabelas_por_fonte.get(caminho, []):
                continue
            df_parte = extrair_tabela(conn, tabela, id_fonte)
            if df_parte is not None:
                dfs.append(df_parte)

        if not dfs:
            print("  Sem dados em nenhuma fonte. Pulando.")
            continue

        df = pl.concat(dfs, how="diagonal")
        df = limpar_df(df)

        try:
            df.write_database(
                table_name=nome_destino,
                connection=engine_dest,
                if_table_exists="replace",
                engine="sqlalchemy",
            )
            print(f"  OK → {df.shape[0]} linhas, {df.shape[1]} colunas gravadas.")
            ok += 1
        except Exception as e:
            print(f"  [ERRO] Gravação falhou: {e}")
            erros += 1

    # Fecha conexões Firebird
    for conn in conexoes.values():
        try:
            conn.close()
        except Exception:
            pass

    print(f"\n>>> Concluído. Tabelas gravadas: {ok} | Erros: {erros}")


if __name__ == "__main__":
    main()
