"""
Captura o AFD de todos os dispositivos REP configurados:
  - Control iD (CONTROLID_URLS): formato legado Portaria 1510, via REST API
  - Henry SuperFácil (HENRY_URLS): formato legado Portaria 1510, via HTTP form

Uso:
  python capturar_afd.py              # baixa uma vez de todos os dispositivos
  python capturar_afd.py --watch 5   # repete a cada 5 minutos
"""
import sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import argparse
import os
import socket as _socket
import time
import urllib3
from datetime import datetime, date, timedelta
from pathlib import Path
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from enriquecer_ponto import enriquecer

load_dotenv()
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# -- Control iD --
_cid_raw    = os.getenv("CONTROLID_URLS") or os.getenv("CONTROLID_URL", "")
CID_DEVICES = [u.strip() for u in _cid_raw.split(",") if u.strip()]
CID_USER    = os.getenv("CONTROLID_USER", "admin")
CID_PASS    = os.getenv("CONTROLID_PASS", "admin")

# -- Henry SuperFácil --
_hen_raw     = os.getenv("HENRY_URLS", "")
HENRY_DEVICES = [u.strip() for u in _hen_raw.split(",") if u.strip()]
HENRY_USER   = os.getenv("HENRY_USER", "rep")
HENRY_PASS   = os.getenv("HENRY_PASS", "")

PASTA_SAIDA  = Path("base_equipamento")
JANELA_DIAS  = 30


# ---------------------------------------------------------------------------
# Utilitários comuns
# ---------------------------------------------------------------------------

def serial_da_url(url: str) -> str:
    """Usa os últimos dois octetos do IP como identificador do dispositivo."""
    host = url.rstrip("/").split("//")[-1].split(":")[0]
    partes = host.split(".")
    if len(partes) >= 2:
        return f"{partes[-2]}_{partes[-1]}"
    return host.replace(".", "_")


def ultimo_nsr(conteudo: bytes) -> int:
    maior = 0
    for linha in conteudo.splitlines():
        try:
            nsr = int(linha[:9])
            if 0 < nsr < 999999999 and nsr > maior:
                maior = nsr
        except (ValueError, IndexError):
            continue
    return maior


def salvar(conteudo: bytes, serial: str) -> Path:
    PASTA_SAIDA.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    caminho = PASTA_SAIDA / f"AFD_{serial}_{ts}_LEGADO.txt"
    caminho.write_bytes(conteudo)
    return caminho


# ---------------------------------------------------------------------------
# Control iD
# ---------------------------------------------------------------------------

def _cid_login(url: str) -> tuple[requests.Session, str]:
    s = requests.Session()
    resp = s.post(
        f"{url}/login.fcgi",
        json={"login": CID_USER, "password": CID_PASS},
        verify=False,
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    token = data.get("session")
    if not token:
        raise RuntimeError(f"Login falhou: {data}")
    return s, token


def _cid_baixar(s: requests.Session, url: str, token: str) -> bytes:
    # Formato legado (Portaria 1510) sem mode=671
    # initial_date = {day, month, year} conforme modal afd_data_legado do firmware
    inicio = date.today() - timedelta(days=JANELA_DIAS)
    resp = s.post(
        f"{url}/get_afd.fcgi",
        params={"session": token},
        json={"initial_date": {"day": inicio.day, "month": inicio.month, "year": inicio.year}},
        verify=False,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.content


def capturar_controlid(url: str) -> Path | None:
    try:
        s, token = _cid_login(url)
        conteudo  = _cid_baixar(s, url, token)
        s.close()
        serial  = serial_da_url(url)
        caminho = salvar(conteudo, serial)
        nsr     = ultimo_nsr(conteudo)
        linhas  = len(conteudo.splitlines())
        print(f"  [ControlID {url}] {linhas} linhas | NSR max: {nsr} -> {caminho.name}")
        return caminho
    except requests.exceptions.ConnectionError:
        print(f"  [ControlID {url}] ERRO: inacessivel")
    except requests.exceptions.HTTPError as e:
        print(f"  [ControlID {url}] ERRO HTTP {e.response.status_code}")
    except Exception as e:
        print(f"  [ControlID {url}] ERRO: {e}")
    return None


# ---------------------------------------------------------------------------
# Henry SuperFácil
# ---------------------------------------------------------------------------

def _henry_raw_get(host: str, port: int, path: str, timeout: int = 30) -> bytes:
    """HTTP/1.0 GET via socket raw (servidor Henry não usa keep-alive padrão)."""
    s = _socket.socket()
    s.settimeout(timeout)
    s.connect((host, port))
    req = f"GET {path} HTTP/1.0\r\nHost: {host}\r\nConnection: close\r\n\r\n"
    s.sendall(req.encode())
    data = b""
    while True:
        try:
            chunk = s.recv(8192)
            if not chunk:
                break
            data += chunk
        except _socket.timeout:
            break
    s.close()
    # Remove cabeçalho HTTP se presente
    if data.startswith(b"HTTP/"):
        hend = data.find(b"\r\n\r\n")
        if hend > 0:
            data = data[hend + 4:]
    return data


def _henry_baixar(url: str) -> bytes:
    parsed = urlparse(url)
    host   = parsed.hostname
    port   = parsed.port or 80

    # 1. Login — estabelece sessão IP no servidor
    _henry_raw_get(
        host, port,
        f"/rep.html?pgCode=7&opType=1&lblId=0&lblLogin={HENRY_USER}&lblPass={HENRY_PASS}",
        timeout=10,
    )

    # 2. Download filtrado pelos últimos JANELA_DIAS dias
    # Formato de data: DD/MM/YY HH:MM (ano 2 dígitos, conforme formulário do dispositivo)
    inicio = date.today() - timedelta(days=JANELA_DIAS)
    di = inicio.strftime("%d/%m/%y") + "+00:00"
    df = date.today().strftime("%d/%m/%y") + "+23:59"

    path = (
        f"/rep.html?pgCode=8&opType=5&lblId=2"
        f"&visibleDiv=communication"
        f"&lblNsrI=000000001&lblNsrF=999999999"
        f"&lblDataI={di}&lblDataF={df}"
    )
    return _henry_raw_get(host, port, path, timeout=60)


def capturar_henry(url: str) -> Path | None:
    try:
        conteudo = _henry_baixar(url)
        # Valida: primeira linha deve começar com dígitos (NSR), não com '<' (HTML)
        primeira = conteudo.split(b"\n")[0].strip()
        if not primeira or not primeira[:9].isdigit():
            raise RuntimeError(f"Resposta não é AFD (inicio: {primeira[:30]})")
        serial  = serial_da_url(url)
        caminho = salvar(conteudo, serial)
        nsr     = ultimo_nsr(conteudo)
        linhas  = len(conteudo.splitlines())
        print(f"  [Henry {url}] {linhas} linhas | NSR max: {nsr} -> {caminho.name}")
        return caminho
    except ConnectionResetError:
        print(f"  [Henry {url}] ERRO: conexao resetada (servidor ocupado?)")
    except Exception as e:
        print(f"  [Henry {url}] ERRO: {e}")
    return None


# ---------------------------------------------------------------------------
# Orquestração
# ---------------------------------------------------------------------------

def capturar() -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    total = len(CID_DEVICES) + len(HENRY_DEVICES)
    print(f"[{ts}] Capturando {total} dispositivo(s)...")

    salvos = []
    for url in CID_DEVICES:
        r = capturar_controlid(url)
        if r:
            salvos.append(r)
    for url in HENRY_DEVICES:
        r = capturar_henry(url)
        if r:
            salvos.append(r)

    if not salvos:
        print("  Nenhum dispositivo respondeu. Enriquecimento cancelado.")
        return

    print("  Enriquecendo fat_atec_ponto_diario...")
    try:
        enriquecer()
    except Exception as e:
        print(f"  [AVISO] Enriquecimento falhou: {e}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Captura AFD dos dispositivos REP")
    parser.add_argument(
        "--watch", type=int, metavar="MINUTOS",
        help="Modo continuo: repete a cada N minutos",
    )
    args = parser.parse_args()

    if args.watch:
        print(f"Modo watch ativo - capturando a cada {args.watch} min. Ctrl+C para parar.")
        while True:
            capturar()
            time.sleep(args.watch * 60)
    else:
        capturar()


if __name__ == "__main__":
    main()
