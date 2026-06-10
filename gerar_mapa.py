"""
Lê o schema das tabelas fb_* no MariaDB e gera:
  mapa_atec/mapa.md   — diagrama Mermaid erDiagram
  mapa_atec/mapa.html — HTML com Mermaid.js para abrir no browser
"""
import os
from sqlalchemy import create_engine, text
from config import get_mariadb_uri

PASTA = "mapa_atec"

TABELAS = [
    "fb_bh", "fb_cp", "fb_cp_t", "fb_departamento", "fb_empresa",
    "fb_feriado", "fb_funcao", "fb_funcionario", "fb_grupo",
    "fb_horarios_linhas", "fb_just_bat", "fb_justficativa",
    "fb_log", "fb_nsr", "fb_obs_cp", "fb_usuario",
]

# (tabela_origem, coluna_fk) → (tabela_destino, coluna_pk)
RELACOES = [
    ("fb_cp",             "IDENT",         "fb_funcionario",  "CODIGO"),
    ("fb_cp_t",           "IDENT",         "fb_funcionario",  "CODIGO"),
    ("fb_bh",             "ID_FUNC",       "fb_funcionario",  "CODIGO"),
    ("fb_nsr",            "IDENT",         "fb_funcionario",  "CODIGO"),
    ("fb_just_bat",       "IDENT",         "fb_funcionario",  "CODIGO"),
    ("fb_obs_cp",         "IDENT",         "fb_funcionario",  "CODIGO"),
    ("fb_funcionario",    "EMPRESA",       "fb_empresa",      "CODIGO"),
    ("fb_funcionario",    "DEPARTAMENTO",  "fb_departamento", "CODIGO"),
    ("fb_funcionario",    "FUNCAO",        "fb_funcao",       "CODIGO"),
    ("fb_funcionario",    "GRUPO",         "fb_grupo",        "CODIGO"),
    ("fb_horarios_linhas","GRUPO",         "fb_grupo",        "CODIGO"),
    ("fb_departamento",   "EMPRESA",       "fb_empresa",      "CODIGO"),
    ("fb_cp",             "JUSTIFICATIVA", "fb_justficativa", "CODIGO"),
    ("fb_just_bat",       "JUSTIFICATIVA", "fb_justficativa", "CODIGO"),
    ("fb_log",            "USUARIO",       "fb_usuario",      "CODIGO"),
]

PKS = {
    "fb_funcionario":    "CODIGO",
    "fb_empresa":        "CODIGO",
    "fb_departamento":   "CODIGO",
    "fb_funcao":         "CODIGO",
    "fb_grupo":          "CODIGO",
    "fb_feriado":        "CODIGO",
    "fb_justficativa":   "CODIGO",
    "fb_usuario":        "CODIGO",
}

TYPE_MAP = {
    "int": "int", "bigint": "int", "smallint": "int", "tinyint": "int",
    "mediumint": "int", "varchar": "string", "char": "string",
    "text": "string", "longtext": "string", "mediumtext": "string",
    "date": "date", "datetime": "date", "timestamp": "date",
    "float": "float", "double": "float", "decimal": "float",
    "blob": "bytes", "longblob": "bytes",
}

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <title>Mapa AtecSoft Ponto - RH</title>
  <script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: #1e1e2e;
      color: #cdd6f4;
      font-family: 'Segoe UI', sans-serif;
      padding: 24px;
    }}
    h1 {{
      text-align: center;
      color: #89b4fa;
      margin-bottom: 8px;
      font-size: 1.4rem;
      letter-spacing: .05em;
    }}
    p.sub {{
      text-align: center;
      color: #6c7086;
      font-size: .85rem;
      margin-bottom: 24px;
    }}
    .mermaid {{
      background: #181825;
      border: 1px solid #313244;
      border-radius: 10px;
      padding: 32px;
      overflow: auto;
    }}
  </style>
</head>
<body>
  <h1>Mapa de Tabelas — AtecSoft Ponto Eletrônico</h1>
  <p class="sub">Gerado automaticamente a partir do schema MariaDB (dbDrogamaisRH)</p>
  <div class="mermaid">
{mermaid}
  </div>
  <script>
    mermaid.initialize({{
      startOnLoad: true,
      theme: 'dark',
      er: {{ diagramPadding: 30, layoutDirection: 'TB', minEntityWidth: 100, entityPadding: 15 }}
    }});
  </script>
</body>
</html>
"""


def obter_schema(engine, db_name: str) -> dict[str, list[tuple]]:
    schema = {}
    with engine.connect() as conn:
        for tabela in TABELAS:
            rows = conn.execute(text("""
                SELECT COLUMN_NAME, DATA_TYPE
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = :db AND TABLE_NAME = :t
                ORDER BY ORDINAL_POSITION
            """), {"db": db_name, "t": tabela}).fetchall()
            if rows:
                schema[tabela] = [(r[0], r[1]) for r in rows]
    return schema


def gerar_mermaid(schema: dict) -> str:
    existentes = set(schema.keys())

    fk_por_tabela: dict[str, set] = {}
    for orig, col, dest, _ in RELACOES:
        if orig in existentes and dest in existentes:
            fk_por_tabela.setdefault(orig, set()).add(col.upper())

    linhas = ["erDiagram"]

    for tabela in TABELAS:
        cols = schema.get(tabela)
        if not cols:
            continue
        pk_col = PKS.get(tabela, "").upper()
        fks = fk_por_tabela.get(tabela, set())
        linhas.append(f"    {tabela} {{")
        for col_name, data_type in cols:
            tipo = TYPE_MAP.get(data_type.lower(), "string")
            safe = col_name.replace(" ", "_").replace("-", "_")
            marcador = ""
            if col_name.upper() == pk_col:
                marcador = " PK"
            elif col_name.upper() in fks:
                marcador = " FK"
            linhas.append(f"        {tipo} {safe}{marcador}")
        linhas.append("    }")

    linhas.append("")

    for orig, col, dest, _ in RELACOES:
        if orig not in existentes or dest not in existentes:
            continue
        cols_orig = {c[0].upper() for c in schema[orig]}
        if col.upper() not in cols_orig:
            continue
        label = col.lower()
        linhas.append(f'    {orig} }}o--|| {dest} : "{label}"')

    return "\n".join(linhas)


def main():
    engine = create_engine(get_mariadb_uri())
    db_name = engine.url.database
    print(f"Lendo schema de '{db_name}'...")

    schema = obter_schema(engine, db_name)

    encontradas = list(schema.keys())
    ausentes = [t for t in TABELAS if t not in schema]

    print(f"  Tabelas encontradas : {len(encontradas)}")
    if ausentes:
        print(f"  Ausentes (rode extract_rh_full.py): {ausentes}")

    if not encontradas:
        print("Nenhuma tabela encontrada. Rode extract_rh_full.py primeiro.")
        return

    mermaid = gerar_mermaid(schema)

    os.makedirs(PASTA, exist_ok=True)

    md_path = os.path.join(PASTA, "mapa.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("# Mapa de Tabelas — AtecSoft Ponto Eletrônico\n\n")
        f.write("```mermaid\n")
        f.write(mermaid)
        f.write("\n```\n")
    print(f"\nSalvo: {md_path}")

    html_path = os.path.join(PASTA, "mapa.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(HTML_TEMPLATE.format(mermaid=mermaid))
    print(f"Salvo: {html_path}")
    print(f"\nAbra '{html_path}' no browser para ver o diagrama.")


if __name__ == "__main__":
    main()
