import polars as pl
import pyodbc
from sqlalchemy import create_engine
from config import (
    FB_CAMINHOS, FONTE_MAP, get_firebird_conn_str, get_mariadb_uri,
    TABELAS_ALVO
)


def limpar_texto(valor):
    if valor is None:
        return None
    if isinstance(valor, str):
        try:
            return valor.encode('latin1').decode('cp1252')
        except:
            return valor
    elif isinstance(valor, bytes):
        try:
            return valor.decode('cp1252')
        except:
            return str(valor)
    return valor


def main():
    print(">>> Iniciando Migração com POLARS (Multi-Origem com Chave Única)")

    # Conectar ao MariaDB
    try:
        engine_dest = create_engine(get_mariadb_uri())
        print("Engine MariaDB configurada.")
    except Exception as e:
        print(f"Erro MariaDB: {e}")
        return

    # Loop nas tabelas
    for tabela in TABELAS_ALVO:
        tabela_upper = tabela.upper()
        print(f"\n--- Processando Tabela: {tabela} ---")

        dfs = []
        query = f"SELECT * FROM {tabela}"

        # Loop nas fontes Firebird
        for caminho, id_fonte in FONTE_MAP.items():
            print(f" > Lendo fonte {id_fonte}: {caminho}")

            try:
                conn_str = get_firebird_conn_str(caminho)
                conn_fb = pyodbc.connect(conn_str)

                df_temp = pl.read_database(query, connection=conn_fb)
                conn_fb.close()

                if not df_temp.is_empty():
                    df_temp = df_temp.with_columns(
                        pl.lit(id_fonte).alias("ID_FONTE").cast(pl.Int8)
                    )
                    dfs.append(df_temp)
            except Exception as e:
                print(f"Erro lendo {caminho}: {e}")
                continue

        if not dfs:
            print("Nenhuma fonte retornou dados. Pulando.")
            continue

        df = pl.concat(dfs)

        # Criar chave sintética
        if tabela_upper == "FUNCIONARIO":
            chave = "CODIGO"
        elif tabela_upper == "CP":
            chave = "IDENT"
        elif tabela_upper == "BH":
            chave = "ID_FUNC"
        else:
            chave = None

        if chave:
            df = df.with_columns(
                (pl.col(chave).cast(pl.Utf8) + "_" + pl.col("ID_FONTE").cast(pl.Utf8))
                .alias("CHAVE_FUNC_UNICA")
            )

        # Limpeza de texto
        colunas_str = [c for c, d in df.schema.items() if d == pl.String]

        if colunas_str:
            df = df.with_columns([
                pl.col(c)
                  .str.replace_all(r"^\(NULL\)$", "", literal=False)
                  .str.strip_chars()
                  .replace("", None)
                  .alias(c)
                for c in colunas_str
            ])

            df = df.with_columns([
                pl.col(c).map_elements(limpar_texto).alias(c)
                for c in colunas_str
            ])

        # Remover colunas totalmente nulas
        cols_nulas = [c for c in df.columns if df[c].null_count() == df.height]
        if cols_nulas:
            df = df.drop(cols_nulas)

        # Gravar no MariaDB
        df.write_database(
            table_name=tabela.lower(),
            connection=engine_dest,
            if_table_exists="replace",
            engine="sqlalchemy"
        )

        print("Sucesso.")


if __name__ == "__main__":
    main()
