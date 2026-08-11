# ponto_horas

Extração do ponto do **AtecSoft** (Firebird) para o **MariaDB** (`dbDrogamaisRH`),
o banco que o app do RH lê.

Um script só — `ponto_horas.py` — que copia os backups do AtecSoft, restaura,
transforma e publica.

```
.FBK do share  →  gbak restaura  →  bronze (fb_*)  →  silver (_stg)
                                          ↓
                        AFD dos relógios  →  enriquece a _stg  →  SWAP
```

## Instalar numa máquina nova

```powershell
git clone https://github.com/drogamais/ponto_horas.git
cd ponto_horas
.\instalar.ps1
notepad .env            # preencher as senhas
.\exec_ponto_horas.bat  # rodar
```

O `instalar.ps1` faz cinco passos:

| | |
|---|---|
| 1 | Confere o Python (não instala — avisa se faltar) |
| 2 | Instala o **Firebird** via winget, que traz o `gbak` |
| 3 | Cria o **venv** e instala o `requirements.txt` |
| 4 | Cria o **`.env`** a partir do exemplo e grava o caminho do `gbak` |
| 5 | Testa os acessos: backups no share, MariaDB e `gbak` |

O `.env` nasce **com as senhas em branco**. Preencha três antes de rodar:

```
MARIA_PASS=          senha do MariaDB
FB_PASS=             senha do Firebird (SYSDBA)
CONTROLID_PASS=      senha dos relógios Control iD
```

> O acesso aos backups usa o **login do Windows de quem executa** — não há senha
> guardada em arquivo. Rode com um usuário que enxergue `\\10.48.12.2\Ponto AtecSoft*`.

Deu problema depois? `venv\Scripts\python.exe testar_conexao.py` diz qual acesso
está quebrado.

## Como rodar

```powershell
.\exec_ponto_horas.bat                          # completo, com log em logs\
venv\Scripts\python.exe ponto_horas.py          # completo, sem log
venv\Scripts\python.exe ponto_horas.py --bronze # só a bronze (fb_*)
venv\Scripts\python.exe ponto_horas.py --silver # só a silver + swap
venv\Scripts\python.exe ponto_horas.py --sem-copia  # reaproveita os .FBK já copiados
venv\Scripts\python.exe ponto_horas.py --manter     # não apaga os temporários (depurar)
```

### Agendamento

Aponte a Tarefa Agendada do Windows para o `exec_ponto_horas.bat`, **depois das
17h**: os backups do dia só são gerados quando alguém abre o AtecSoft, à tarde.
Rodando antes, ele processa o backup do dia anterior.

A tarefa precisa rodar com um usuário que enxergue o share do AtecSoft.

## Por que lê do backup, e não do servidor

O Firebird do DC001 **sobe junto com o AtecSoft** (usado à tarde) e fica fora do
ar o resto do tempo. A porta 3050 não responde de madrugada, e o script antigo
falhava com `-902 Unable to complete network request`.

Os `.FBK` estão sempre no share. E o `gbak` resolve um segundo problema de
brinde: as bases são **ODS 10 (Firebird 1.x)** e as bibliotecas atuais não as
abririam direto — o restore converte o formato.

`FB_FBK_N` aponta para a **pasta** de backup, não para o arquivo: uma das fontes
gera nome com data (`PONTO_ATECSOFT_10082026.FBK`), então o script pega sempre o
`.FBK` mais recente.

## As três fontes

Uma base Firebird por empresa. O número em `FB_FBK_N` vira a coluna `ID_FONTE`,
que distingue a mesma pessoa cadastrada em empresas diferentes — é o que forma a
`CHAVE_FUNC_UNICA` (`<CODIGO>_<ID_FONTE>`).

| ID_FONTE | Empresa |
|---|---|
| 1 | ALTANEIRO |
| 2 | APF DROGAMAIS |
| 3 | DROGAMAIS COMERCIAL (CD) |

## As camadas

**Bronze** (`fb_*`) — espelho cru de cada tabela do Firebird, sem transformação
além da limpeza de encoding.

**Silver** (`dim_atec_*` / `fat_atec_*`) — as tabelas que o app do RH consome,
com `CHAVE_FUNC_UNICA` e justificativas padronizadas (o AtecSoft trunca o motivo
em 10 caracteres e escreve de várias formas; aqui viram nome canônico + ID
estável).

Gravadas primeiro em `_stg`, e só publicadas no swap.

## O enriquecimento (e por que ele vem antes do swap)

A silver é **full reload**. O AtecSoft não tem as batidas que ficaram só no
relógio, então a `_stg` recém-criada nasce com as colunas de horário vazias — não
é a linha que some, é o **ponto do dia que fica em branco**, o que é pior de
perceber.

Por isso o script baixa o AFD dos relógios e **enriquece a `_stg` antes do
swap**. Quando a troca acontece, a tabela nova já está completa. Se algo falhar
no meio, a produção nem foi tocada.

> Essa ordem veio de um erro real: na primeira versão o swap vinha antes e o
> enriquecimento depois. A reaplicação falhou e a produção ficou sem as batidas.

O `COALESCE` garante que só coluna **vazia** é preenchida — se o RH corrigiu a
batida no AtecSoft, a correção dele vence.

### O Henry do CD fica de fora

`HENRY_URLS` está vazio de propósito. O Henry aceita **uma sessão por vez**, e
quem cobre esse aparelho é o **agente coletor** (repo `agentes-rh`), que grava
direto na tabela viva. Dois coletores no mesmo Henry se bloqueiam.

Se o log disser *"não veio AFD — alguém está com a página do relógio aberta?"*,
é isso: o aparelho recusa **antes** de validar a senha, então parece erro de
credencial e não é.

## Proteções

- **Tabela vazia não gera staging** — se uma fonte falhar, a tabela boa não é
  trocada por uma vazia.
- **Fonte com problema não aborta as outras** — o pipeline segue com o que
  conseguiu ler.
- **Gravação em lotes** (5.000 linhas) — a `CP` tem ~75k linhas × 116 colunas e
  derrubava a conexão com `Server has gone away` num INSERT único.
- **Temporários descartados** no fim (`_trabalho/`), mesmo se der erro.

## Arquivos

| | |
|---|---|
| `ponto_horas.py` | o pipeline inteiro |
| `instalar.ps1` | prepara a máquina |
| `exec_ponto_horas.bat` | roda o fluxo completo, com log diário em `logs\` |
| `testar_conexao.py` | diagnóstico: `.env`, MariaDB, backups e `gbak` |
| `.env.example` | modelo de configuração (o `.env` real não vai pro git) |
| `gerar_mapa.py` | gera diagrama das tabelas `fb_*` em `mapa_atec/` (uso manual) |

## Dois detalhes de Windows

Se for editar os arquivos, evite tropeçar neles:

**Não use caractere não-ASCII em `.ps1`.** O PowerShell 5.1 lê arquivo sem BOM
como ANSI; um travessão no meio de um comentário quebra o parser inteiro, com o
erro apontando para uma linha 40 posições adiante.

**`Set-Content -Encoding utf8` põe BOM.** No `.env`, o BOM faz o `python-dotenv`
ler a primeira chave com o marcador colado no nome — ela simplesmente não existe
para o script. Grave com `UTF8Encoding($false)`.
