"""
Lê os AFDs REP-C de todos os dispositivos, extrai batidas tipo 3 dos
últimos 30 dias, cruza CPF com dim_atec_funcionarios e faz UPDATE/INSERT
direto em fat_atec_ponto_diario via tabela temporária.
"""
import sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from pathlib import Path
from datetime import datetime, date, timedelta

import polars as pl
from sqlalchemy import create_engine, text
from config import get_mariadb_uri

PASTA_AFD  = Path("base_equipamento")
TAB_FUNC   = "dim_atec_funcionarios"
TAB_PONTO  = "fat_atec_ponto_diario"
TAB_TMP    = "tmp_afd_batidas"
PARES      = ["E1","S1","E2","S2","E3","S3","E4","S4","E5","S5","E6","S6"]
JANELA_DIAS = 30


def afds_por_dispositivo() -> list[Path]:
    """
    Retorna o arquivo REP_C mais recente de cada dispositivo.
    Nomenclatura nova:  AFD_<octet>_<octet>_<YYYYMMDD>_<HHMMSS>_REP_C.txt
                        serial = octet_octet  (ex: 21_249)
    Nomenclatura legada: AFD_<serial_longo>_<YYYYMMDD>_...
                        serial = serial_longo (ex: 00014003750346237)
    """
    todos = sorted(
        list(PASTA_AFD.glob("*_LEGADO.txt")) + list(PASTA_AFD.glob("*_REP_C.txt")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    vistos: set[str] = set()
    selecionados: list[Path] = []
    for p in todos:
        partes = p.stem.split("_")   # ['AFD', A, B, date, time, 'REP', 'C']
        # Formato novo: partes[1] e partes[2] são octetos curtos (1–3 dígitos)
        if len(partes) >= 3 and partes[1].isdigit() and len(partes[1]) <= 3:
            serial = f"{partes[1]}_{partes[2]}"
        else:
            serial = partes[1]       # serial longo ou 'DESCONHECIDO'
        if serial not in vistos:
            vistos.add(serial)
            selecionados.append(p)
    if not selecionados:
        raise FileNotFoundError("Nenhum arquivo AFD (LEGADO ou REP_C) em base_equipamento/.")
    return selecionados
    return arquivos[0]


def parsear_batidas(caminhos: list[Path]) -> pl.DataFrame:
    """
    Extrai linhas tipo 3 de todos os arquivos fornecidos,
    filtra pelos últimos JANELA_DIAS dias e remove duplicatas.
    """
    corte = date.today() - timedelta(days=JANELA_DIAS)
    registros = []
    for caminho in caminhos:
        for raw in caminho.read_bytes().splitlines():
            try:
                l = raw.decode("latin1")
                if len(l) < 34 or l[9] != "3":
                    continue
                if len(l) >= 50 and l[13] == "-":
                    # REP-C (Portaria 671): NSR(9)+tipo(1)+ISO8601(24)+CPF(12)+CRC(4)
                    dt  = datetime.fromisoformat(l[10:34])
                    cpf = (l[34:46].lstrip("0") or "0").zfill(11)
                else:
                    # Legado (Portaria 1510): NSR(9)+tipo(1)+DDMMYYYY(8)+HHMM(4)+CPF(12)+CRC(4)
                    dt = datetime(
                        int(l[14:18]), int(l[12:14]), int(l[10:12]),
                        int(l[18:20]), int(l[20:22]),
                    )
                    cpf = (l[22:34].lstrip("0") or "0").zfill(11)
                if dt.date() < corte:
                    continue
                registros.append({
                    "CPF":  cpf,
                    "DATA": dt.date(),
                    "HORA": dt.replace(tzinfo=None).time(),
                })
            except Exception:
                continue

    if not registros:
        return pl.DataFrame(schema={"CPF": pl.String, "DATA": pl.Date, "HORA": pl.Time})

    return (
        pl.DataFrame(registros, schema={"CPF": pl.String, "DATA": pl.Date, "HORA": pl.Time})
        .unique(subset=["CPF", "DATA", "HORA"])   # dedup entre dispositivos
    )


def pivotar_batidas(df: pl.DataFrame) -> pl.DataFrame:
    """
    Agrupa (CPF, DATA), ordena por HORA e distribui em E1->S1->...->E6->S6.
    Retorna strings 'HH:MM:SS' para compatibilidade com TIME do MariaDB.
    """
    linhas = []
    for (cpf, data), grupo in df.sort("HORA").group_by(["CPF", "DATA"]):
        horas = grupo["HORA"].to_list()
        row: dict = {"CPF": cpf, "DATA": data}
        for i, h in enumerate(horas[: len(PARES)]):
            row[PARES[i]] = f"{h.hour:02d}:{h.minute:02d}:{h.second:02d}"
        linhas.append(row)

    schema: dict = {"CPF": pl.String, "DATA": pl.Date}
    schema.update({c: pl.String for c in PARES})
    return pl.DataFrame(linhas, schema=schema)


def enriquecer(
    caminho_afd: Path | None = None,
    tab_ponto: str | None = None,
    tab_func: str | None = None,
) -> None:
    _tab_ponto = tab_ponto or TAB_PONTO
    _tab_func  = tab_func  or TAB_FUNC

    if caminho_afd is not None:
        arquivos = [caminho_afd]
    else:
        arquivos = afds_por_dispositivo()

    for f in arquivos:
        print(f"  AFD: {f.name}")

    # 1. Parse + pivot (todos os dispositivos, ultimos 30 dias)
    df_batidas = parsear_batidas(arquivos)
    if df_batidas.is_empty():
        print("  Nenhuma batida tipo 3 no AFD.")
        return
    print(f"  Batidas tipo 3: {len(df_batidas)}")

    df_pivot = pivotar_batidas(df_batidas)
    print(f"  Registros (funcionario x dia): {len(df_pivot)}")

    engine = create_engine(get_mariadb_uri())

    # 2. Mapeamento ID_AFD -> CHAVE_FUNC_UNICA + IDENT + ID_FONTE
    #    Dispositivos Control iD 249 gravam CPF; 248 e Henry gravam PIS.
    #    Unifica os dois em uma tabela de lookup com coluna ID_AFD.
    def _normalizar(col: str) -> pl.Expr:
        return (
            pl.col(col).cast(pl.String)
              .str.replace_all(r"[.\-/\s]", "")
              .str.zfill(11)
        )

    df_por_cpf = pl.read_database(
        f"SELECT CPF AS ID_AFD, CODIGO, ID_FONTE, CHAVE_FUNC_UNICA FROM {_tab_func} "
        f"WHERE CPF IS NOT NULL AND CPF <> '' AND SITUACAO = 'ATIVO'",
        connection=engine,
    ).with_columns(_normalizar("ID_AFD").alias("ID_AFD")).rename({"CODIGO": "IDENT"})

    df_por_pis = pl.read_database(
        f"SELECT PIS AS ID_AFD, CODIGO, ID_FONTE, CHAVE_FUNC_UNICA FROM {_tab_func} "
        f"WHERE PIS IS NOT NULL AND PIS <> '' AND SITUACAO = 'ATIVO'",
        connection=engine,
    ).with_columns(_normalizar("ID_AFD").alias("ID_AFD")).rename({"CODIGO": "IDENT"})

    df_func = (
        pl.concat([df_por_cpf, df_por_pis])
        .unique(subset=["ID_AFD", "CHAVE_FUNC_UNICA"])
    )

    # 3. Join pivot x funcionarios (CPF do AFD pode ser CPF ou PIS)
    df_afd = (
        df_pivot
        .rename({"CPF": "ID_AFD"})
        .join(df_func, on="ID_AFD", how="inner")
        .drop("ID_AFD")
        .rename({"DATA": "DATA_BATIDA"})
    )

    if df_afd.is_empty():
        print("  Nenhum ID do AFD cruzou com dim_atec_funcionarios.")
        return

    # Deduplicação: mesmo funcionário pode aparecer via CPF (249) e PIS (248/Henry).
    # Mantém a linha com mais batidas preenchidas para evitar INSERT duplicado.
    pares_presentes = [c for c in PARES if c in df_afd.columns]
    df_afd = (
        df_afd
        .with_columns(
            pl.sum_horizontal([pl.col(c).is_not_null() for c in pares_presentes])
            .alias("_n")
        )
        .sort("_n", descending=True)
        .unique(subset=["CHAVE_FUNC_UNICA", "DATA_BATIDA"], keep="first")
        .drop("_n")
    )
    print(f"  Cruzamentos (func x dia): {len(df_afd)}  |  pares: {pares_presentes}")

    # 4. Garante coluna FONTE_HORARIO na tabela destino
    with engine.connect() as conn:
        conn.execute(text(
            f"ALTER TABLE `{_tab_ponto}` "
            f"ADD COLUMN IF NOT EXISTS `FONTE_HORARIO` VARCHAR(20) NULL"
        ))
        conn.commit()

    # 5. Grava tabela temporária com os dados do AFD
    df_afd.write_database(
        table_name=TAB_TMP,
        connection=engine,
        if_table_exists="replace",
        engine="sqlalchemy",
    )

    # 6. UPDATE JOIN: atualiza E1..S6 e DATA_E1..DATA_S6 onde AFD tem valor
    set_partes = []
    for col in pares_presentes:
        set_partes.append(
            f"f.`{col}` = IF(a.`{col}` IS NOT NULL, TIME(a.`{col}`), f.`{col}`)"
        )
        data_col = f"DATA_{col}"
        set_partes.append(
            f"f.`{data_col}` = IF(a.`{col}` IS NOT NULL, f.`DATA_BATIDA`, f.`{data_col}`)"
        )
    set_partes.append("f.`FONTE_HORARIO` = 'AFD'")

    sql_update = f"""
        UPDATE `{_tab_ponto}` f
        JOIN `{TAB_TMP}` a
          ON  a.CHAVE_FUNC_UNICA = f.CHAVE_FUNC_UNICA
          AND a.DATA_BATIDA      = f.DATA_BATIDA
        SET {', '.join(set_partes)}
    """

    # Colunas de fuso: '-0300' para cada batida presente
    fuso_partes_insert = ", ".join(
        f"IF(a.`{c}` IS NOT NULL, '-0300', NULL) AS `FUSO_{c}`"
        for c in pares_presentes
    )

    # INSERT das linhas que ainda não existem na fat
    colunas_batida_insert = ", ".join(
        f"IF(a.`{c}` IS NOT NULL, TIME(a.`{c}`), NULL) AS `{c}`"
        for c in pares_presentes
    )
    colunas_data_insert = ", ".join(
        f"IF(a.`{c}` IS NOT NULL, a.`DATA_BATIDA`, NULL) AS `DATA_{c}`"
        for c in pares_presentes
    )
    cols_fuso_names  = ", ".join(f"`FUSO_{c}`"  for c in pares_presentes)
    cols_pares_names = ", ".join(f"`{c}`"       for c in pares_presentes)
    cols_data_names  = ", ".join(f"`DATA_{c}`"  for c in pares_presentes)

    sql_insert = f"""
        INSERT INTO `{_tab_ponto}`
          (`IDENT`, `DATA_BATIDA`, `ID_FONTE`, `CHAVE_FUNC_UNICA`, `FONTE_HORARIO`,
           {cols_pares_names},
           {cols_data_names},
           {cols_fuso_names})
        SELECT
          a.`IDENT`,
          a.`DATA_BATIDA`,
          a.`ID_FONTE`,
          a.`CHAVE_FUNC_UNICA`,
          'AFD',
          {colunas_batida_insert},
          {colunas_data_insert},
          {fuso_partes_insert}
        FROM `{TAB_TMP}` a
        LEFT JOIN `{_tab_ponto}` p
          ON  p.`CHAVE_FUNC_UNICA` = a.`CHAVE_FUNC_UNICA`
          AND p.`DATA_BATIDA`      = a.`DATA_BATIDA`
        WHERE p.`CHAVE_FUNC_UNICA` IS NULL
    """

    with engine.connect() as conn:
        res_upd = conn.execute(text(sql_update))
        res_ins = conn.execute(text(sql_insert))
        conn.execute(text(f"DROP TABLE IF EXISTS `{TAB_TMP}`"))
        conn.commit()

    print(f"  Linhas atualizadas : {res_upd.rowcount}")
    print(f"  Linhas inseridas   : {res_ins.rowcount}")


if __name__ == "__main__":
    enriquecer()
