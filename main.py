import sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from pathlib import Path
import polars as pl
import pyodbc
from sqlalchemy import create_engine, text, inspect as sa_inspect
from config import (
    FB_CAMINHOS, FONTE_MAP, get_firebird_conn_str, get_mariadb_uri,
    TABELAS_ALVO
)
from capturar_afd import capturar as capturar_afd

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
    print(">>> Iniciando Migracao Camada Silver (Normalizacao e Chaves Unicas)")

    try:
        engine_dest = create_engine(get_mariadb_uri())
        print("Engine MariaDB configurada.")
    except Exception as e:
        print(f"Erro MariaDB: {e}")
        return

    # Dicionário de Normalização (De/Para)
    DE_PARA_JUSTFICATIVAS = {
        # Atestados e Afastamentos
        "Atestado": "ATESTADO MEDICO",
        "INSS": "AFASTAMENTO INSS",
        "Afastament": "AFASTAMENTO",
        "Luto": "LICENCA NOJO (LUTO)",
        "LUTO": "LICENCA NOJO (LUTO)",

        # Faltas e Ausências
        "Falta": "FALTA",
        "falta desc": "FALTA DESCONTADA",
        "Falta desc": "FALTA DESCONTADA",
        "Aus. Just": "AUSENCIA JUSTIFICADA",
        "Aus. Justi": "AUSENCIA JUSTIFICADA",
        "Abono": "ABONO",

        # Compensações e Horas
        "COMP. HORA": "COMPENSACAO DE HORAS",
        "comp. hrs": "COMPENSACAO DE HORAS",
        "Comp Horas": "COMPENSACAO DE HORAS",
        "Home Offic": "HOME OFFICE",
        "Dispensado": "DISPENSA",

        # Licenças e Especiais
        "Férias": "FERIAS",
        "Folga": "FOLGA",
        "Feriado": "FERIADO",
        "Lic.Patern": "LICENCA PATERNIDADE",
        "Lic.Matern": "LICENCA MATERNIDADE",
        "Casamento": "LICENCA GALA (CASAMENTO)",
        "Lic. Casam": "LICENCA GALA (CASAMENTO)",
        "Guarda M.": "GUARDA MIRIM",
        "Guarda Mir": "GUARDA MIRIM",
        "DAY OFF": "DAY OFF",
        "Day off": "DAY OFF",

        # Outros
        "Declaração": "DECLARACAO",
        "Dec. Acomp": "DECLARACAO ACOMPANHANTE",
        "Viagem Emp": "VIAGEM A TRABALHO",
        "Viagem emp": "VIAGEM A TRABALHO",
        "Aviso": "AVISO PREVIO",
        "JOGOS BR": "JOGOS DO BRASIL"
    }

    ID_PADRAO_JUSTIFICATIVA = {
        "ATESTADO MEDICO": "1",
        "FALTA": "2",
        "DISPENSA": "3",
        "FERIAS": "4",
        "FOLGA": "5",
        "LICENCA PATERNIDADE": "6",
        "LICENCA MATERNIDADE": "7",
        "LICENCA NOJO (LUTO)": "8",
        "AFASTAMENTO INSS": "9",
        "DAY OFF": "10",
        "COMPENSACAO DE HORAS": "11",
        "DECLARACAO": "12",
        "VIAGEM A TRABALHO": "13",
        "FALTA DESCONTADA": "14",
        "LICENCA GALA (CASAMENTO)": "15",
        "GUARDA MIRIM": "16",
        "ABONO": "17",
        "HOME OFFICE": "18",
        "AUSENCIA JUSTIFICADA": "19",
        "AVISO PREVIO": "20",
        "JOGOS DO BRASIL": "21",
        "FERIADO": "22",
        "DECLARACAO ACOMPANHANTE": "23",
        "AFASTAMENTO": "24"
    }

    tabelas_gravadas: list[str] = []

    for tabela in TABELAS_ALVO:
        tabela_upper = tabela.upper()
        print(f"\n--- Processando Tabela: {tabela} ---")

        dfs = []
        query = f"SELECT * FROM {tabela}"

        for caminho, id_fonte in FONTE_MAP.items():
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

        # --- 1. Definição de Chaves (Estrutura Unificada) ---
        chave = None
        alias_chave = "CHAVE_FUNC_UNICA"

        if tabela_upper == "FUNCIONARIO":
            chave = "CODIGO"
        elif tabela_upper in ["CP", "CP_T"]:
            chave = "IDENT"
        elif tabela_upper == "BH":
            chave = "ID_FUNC"
        elif tabela_upper == "JUSTFICATIVA":
            chave = "CODIGO"
            alias_chave = "CHAVE_JUST_UNICA"
            
            # 1. Normaliza o Nome
            df = df.with_columns(
                pl.col("NOME").replace_strict(DE_PARA_JUSTFICATIVAS, default=pl.col("NOME")).alias("NOME_PADRAO")
            )

            # 2. Atribui o ID Fixo (Temporário como String para concatenar na chave)
            df = df.with_columns(
                pl.col("NOME_PADRAO").replace_strict(ID_PADRAO_JUSTIFICATIVA, default="999").alias("ID_FIXO")
            )

            # 3. Cria a CHAVE_MAP_UNICA e a nova coluna ID_MAP_INT como Inteiro
            df = df.with_columns([
                (pl.col("NOME_PADRAO").str.slice(0, 3).str.to_uppercase() + "_" + pl.col("ID_FIXO"))
                .alias("CHAVE_MAP_UNICA"),
                pl.col("ID_FIXO").cast(pl.Int32).alias("ID_MAP_INT") # <-- Nova coluna INT
            ]).drop("ID_FIXO")

        # Aplica a criação da Chave Sintética se houver uma chave definida
        if chave:
            df = df.with_columns(
                (pl.col(chave).cast(pl.Utf8) + "_" + pl.col("ID_FONTE").cast(pl.Utf8))
                .alias(alias_chave)
            )

        # --- 2. Limpeza de Texto ---
        colunas_str = [c for c, d in df.schema.items() if d == pl.String]
        if colunas_str:
            df = df.with_columns([
                pl.col(c).str.replace_all(r"^\(NULL\)$", "", literal=False).str.strip_chars().replace("", None).alias(c)
                for c in colunas_str
            ])
            df = df.with_columns([
                pl.col(c).map_elements(limpar_texto, return_dtype=pl.String).alias(c)
                for c in colunas_str
            ])

        # --- 3. Mapeamento de Nomes e Gravação ---
        MAPEAMENTO_NOMES = {
            "BH": "fat_atec_banco_horas",
            "CP": "fat_atec_ponto_diario",
            "CP_T": "fat_atec_ponto_temp",
            "FERIADO": "dim_atec_feriado",
            "FUNCIONARIO": "dim_atec_funcionarios",
            "JUSTFICATIVA": "dim_atec_justificativa",
            "LOG": "fat_atec_logs"
        }

        nome_final = MAPEAMENTO_NOMES.get(tabela_upper, tabela.lower())

        if not df.is_empty():
            df.write_database(
                table_name=f"{nome_final}_stg",
                connection=engine_dest,
                if_table_exists="replace",
                engine="sqlalchemy"
            )
            tabelas_gravadas.append(nome_final)
            print(f"Sucesso: {nome_final}_stg gravada.")
        else:
            print(f"Aviso: {nome_final} vazia. Staging ignorado para proteger o banco.")

    # --- AFD: captura e insere direto nas tabelas _stg (antes do swap) ---
    print("\n>>> Capturando AFD e enriquecendo staging...")
    try:
        capturar_afd(
            tab_ponto="fat_atec_ponto_diario_stg",
            tab_func="dim_atec_funcionarios_stg",
        )
    except Exception as e:
        print(f"[AVISO] Captura AFD falhou: {e}")

    # --- Swap: _stg -> live, live -> _old, drop _old ---
    if tabelas_gravadas:
        print("\n>>> Realizando swap das tabelas staging -> producao...")
        existing = set(sa_inspect(engine_dest).get_table_names())
        with engine_dest.connect() as conn:
            for nome_final in tabelas_gravadas:
                stg = f"{nome_final}_stg"
                old = f"{nome_final}_old"
                conn.execute(text(f"DROP TABLE IF EXISTS `{old}`"))
                if nome_final in existing:
                    conn.execute(text(
                        f"RENAME TABLE `{nome_final}` TO `{old}`, `{stg}` TO `{nome_final}`"
                    ))
                    conn.execute(text(f"DROP TABLE IF EXISTS `{old}`"))
                else:
                    conn.execute(text(f"RENAME TABLE `{stg}` TO `{nome_final}`"))
                print(f"  {stg} -> {nome_final}")
            conn.commit()

    # --- Limpeza dos .txt baixados ---
    pasta_afd = Path("base_equipamento")
    removidos = 0
    for txt in pasta_afd.glob("*.txt"):
        try:
            txt.unlink()
            removidos += 1
        except Exception as e:
            print(f"[AVISO] Nao foi possivel remover {txt.name}: {e}")
    if removidos:
        print(f"\n>>> {removidos} arquivo(s) AFD removido(s) de base_equipamento/")

if __name__ == "__main__":
    main()