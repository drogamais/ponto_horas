# =====================================================================
#  Prepara esta maquina para rodar o ponto_horas.
#
#  Faz tudo o que o script precisa:
#    1. confere o Python
#    2. instala o Firebird (traz o gbak, que restaura os backups .FBK)
#    3. cria o venv e instala as dependencias
#    4. cria o .env a partir do exemplo e grava o caminho do gbak
#    5. testa: acesso aos backups no share + conexao com o MariaDB
#
#  Uso (PowerShell na pasta do projeto):
#      .\instalar.ps1
#
#  O acesso aos backups usa o SEU login do Windows - nao ha senha guardada.
#  Rode com um usuario que enxergue \\10.48.12.2\Ponto AtecSoft*.
# =====================================================================
[CmdletBinding()]
param(
    # Pula a instalacao do Firebird (use se ja tiver o gbak na maquina).
    [switch]$SemFirebird,
    # Versao do Firebird a instalar. A 4 foi a testada com estas bases (ODS 10).
    [ValidateSet('3', '4', '5')]
    [string]$VersaoFirebird = '4'
)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

function Passo($n, $texto) { Write-Host "`n[$n] $texto" -ForegroundColor Cyan }
function Ok($texto)        { Write-Host "    OK  $texto" -ForegroundColor Green }
function Aviso($texto)     { Write-Host "    !   $texto" -ForegroundColor Yellow }
function Falha($texto)     { Write-Host "    X   $texto" -ForegroundColor Red }

Write-Host "=== Instalacao do ponto_horas ===" -ForegroundColor White

# --- 1. Python -------------------------------------------------------
Passo 1 "Conferindo o Python"
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
    Falha "Python nao encontrado no PATH. Instale o Python 3.11+ e rode de novo."
    Write-Host "        winget install Python.Python.3.12" -ForegroundColor DarkGray
    exit 1
}
Ok ((& python --version) 2>&1)

# --- 2. Firebird (gbak) ----------------------------------------------
Passo 2 "Firebird / gbak (restaura os backups .FBK)"
function Find-Gbak {
    $c = Get-Command gbak -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    foreach ($base in @("C:\Program Files\Firebird", "C:\Program Files (x86)\Firebird")) {
        if (Test-Path $base) {
            $f = Get-ChildItem $base -Recurse -Filter gbak.exe -ErrorAction SilentlyContinue |
                 Sort-Object FullName -Descending | Select-Object -First 1
            if ($f) { return $f.FullName }
        }
    }
    return $null
}

$gbak = Find-Gbak
if ($gbak) {
    Ok "ja instalado: $gbak"
} elseif ($SemFirebird) {
    Aviso "gbak nao encontrado e -SemFirebird foi passado; o script nao vai rodar sem ele"
} else {
    $pacote = "FirebirdProject.Firebird.$VersaoFirebird"
    Write-Host "    instalando $pacote (via winget)..." -ForegroundColor DarkGray
    winget install --id $pacote -e --silent --accept-source-agreements --accept-package-agreements | Out-Null
    $gbak = Find-Gbak
    if ($gbak) { Ok "instalado: $gbak" }
    else {
        Falha "nao consegui instalar o Firebird. Baixe de https://firebirdsql.org/en/downloads/"
        exit 1
    }
}

# --- 3. venv + dependencias ------------------------------------------
Passo 3 "Ambiente virtual e dependencias"
if (-not (Test-Path "venv")) {
    & python -m venv venv
    Ok "venv criado"
} else {
    Ok "venv ja existe"
}
$pyVenv = ".\venv\Scripts\python.exe"
# python -m pip: atualizar o pip pelo proprio pip.exe falha no Windows, ele nao
# consegue sobrescrever o executavel em uso.
& $pyVenv -m pip install --quiet --upgrade pip 2>&1 | Out-Null
& $pyVenv -m pip install --quiet -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    Falha "falha ao instalar as dependencias (veja o erro acima)"
    exit 1
}
Ok "dependencias instaladas"

# --- 4. .env ---------------------------------------------------------
Passo 4 "Arquivo de configuracao (.env)"
if (-not (Test-Path ".env")) {
    if (Test-Path ".env.example") {
        Copy-Item ".env.example" ".env"
        Aviso ".env criado a partir do exemplo - PREENCHA as senhas antes de rodar"
    } else {
        Falha "nao achei .env nem .env.example"
        exit 1
    }
} else {
    Ok ".env ja existe"
}

# grava o caminho do gbak (o script usa GBAK_EXE quando nao esta no PATH)
if ($gbak) {
    $linhas = Get-Content ".env"
    if ($linhas -match '^GBAK_EXE=') {
        $linhas = $linhas -replace '^GBAK_EXE=.*$', "GBAK_EXE=$gbak"
    } else {
        $linhas += "GBAK_EXE=$gbak"
    }
    # UTF8Encoding($false) = sem BOM. Com BOM, o python-dotenv le a primeira
    # chave com o marcador colado no nome e ela simplesmente nao existe pro script.
    [System.IO.File]::WriteAllLines(
        (Join-Path $PSScriptRoot ".env"), $linhas,
        (New-Object System.Text.UTF8Encoding $false))
    Ok "GBAK_EXE gravado no .env"
}

# --- 5. testes -------------------------------------------------------
Passo 5 "Testando os acessos"

# 5a. backups no share
$faltando = @()
foreach ($linha in Get-Content ".env") {
    if ($linha -match '^FB_FBK_(\d)=(.+)$') {
        $n = $Matches[1]; $caminho = $Matches[2].Trim()
        if (Test-Path $caminho) {
            $fbk = Get-ChildItem $caminho -Filter *.FBK -ErrorAction SilentlyContinue |
                   Sort-Object LastWriteTime -Descending | Select-Object -First 1
            if ($fbk) {
                $h = [math]::Round(((Get-Date) - $fbk.LastWriteTime).TotalHours)
                $idade = [string]$h + 'h atras'
                Ok "fonte $n -> $($fbk.Name) ($idade)"
            } else { Aviso "fonte $n -> pasta acessivel, mas sem .FBK dentro" }
        } else {
            Falha "fonte $n -> sem acesso a $caminho"
            $faltando += $n
        }
    }
}
if ($faltando.Count -gt 0) {
    Aviso "sem acesso ao share: rode com um usuario do dominio que enxergue as pastas"
}

# 5b. MariaDB, backups e gbak (script proprio, ver testar_conexao.py)
& .\venv\Scripts\python.exe testar_conexao.py

# --- fim -------------------------------------------------------------
Write-Host "`n=== Pronto ===" -ForegroundColor White
Write-Host @"
Para rodar:
    .\exec_ponto_horas.bat              (fluxo completo, com log em logs\)
    venv\Scripts\python.exe ponto_horas.py --bronze    (so a bronze)

Agendamento: aponte a Tarefa Agendada do Windows para o exec_ponto_horas.bat,
DEPOIS das 17h - antes disso os backups do dia ainda nao foram gerados (eles
saem quando alguem abre o AtecSoft, a tarde). A tarefa precisa rodar com um
usuario que enxergue o share do AtecSoft.
"@ -ForegroundColor Gray
