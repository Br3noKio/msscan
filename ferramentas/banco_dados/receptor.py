"""Receptor do banco de dados da MSServer (CLAUDE.md 33). Roda na máquina de desenvolvimento, fora da ISO.

Recebe por POST os relatórios ANONIMIZADOS que o MSscan envia sozinho (``msscan/envio_dados.py``) e guarda num SQLite.
Ouve só em 127.0.0.1; a internet chega pelo Tailscale Funnel (HTTPS), que repassa ``/msscan/...`` para cá.
Proteções: só ``POST /v1/coletas`` (com ou sem o prefixo ``/msscan``), JSON de no máximo 2 MB no formato
``msscan-coleta-1``, limite de envios por endereço de origem e nenhum dado devolvido pela rede.

Uso:  python3 receptor.py [--porta 8787] [--banco /var/lib/msscan-banco/coletas.db]
      python3 receptor.py --resumo     (contagem por modelo e versão, só local)
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

FORMATO = "msscan-coleta-1"
TAMANHO_MAXIMO = 2_000_000
LIMITE_POR_HORA = 60
CAMINHOS = {"/v1/coletas", "/msscan/v1/coletas"}
BANCO_PADRAO = "/var/lib/msscan-banco/coletas.db"

ESQUEMA = """
CREATE TABLE IF NOT EXISTS coletas (
    id TEXT PRIMARY KEY,
    recebido_em TEXT NOT NULL,
    enviado_em TEXT,
    versao_msscan TEXT,
    fabricante TEXT,
    modelo TEXT,
    veredito TEXT,
    dados TEXT NOT NULL
)
"""


def abrir(caminho: str) -> sqlite3.Connection:
    Path(caminho).parent.mkdir(parents=True, exist_ok=True)
    conexao = sqlite3.connect(caminho, check_same_thread=False)
    conexao.execute(ESQUEMA)
    conexao.commit()
    return conexao


def validar(corpo: Any) -> str:
    """``""`` se o envio é aceito; senão, o motivo."""
    if not isinstance(corpo, dict) or corpo.get("formato") != FORMATO:
        return "formato desconhecido"
    if not isinstance(corpo.get("dados"), dict) or not isinstance(corpo.get("id"), str) or not 8 <= len(corpo["id"]) <= 64:
        return "campos obrigatórios ausentes"
    return ""


def guardar(conexao: sqlite3.Connection, corpo: dict[str, Any]) -> None:
    servidor = corpo["dados"].get("servidor") or {}
    conexao.execute(
        "INSERT OR IGNORE INTO coletas VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (corpo["id"], time.strftime("%Y-%m-%dT%H:%M:%S%z"), str(corpo.get("enviado_em") or ""), str(corpo.get("versao_msscan") or ""),
         str(servidor.get("fabricante") or ""), str(servidor.get("modelo") or ""), str(corpo["dados"].get("veredito") or ""),
         json.dumps(corpo["dados"], ensure_ascii=False)),
    )  # fmt: skip
    conexao.commit()


class Receptor(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, endereco: tuple[str, int], banco: str) -> None:
        super().__init__(endereco, Tratador)
        self.conexao = abrir(banco)
        self.trava = threading.Lock()
        self.envios: dict[str, list[float]] = {}

    def permitido(self, origem: str) -> bool:
        agora = time.monotonic()
        with self.trava:
            recentes = [t for t in self.envios.get(origem, []) if agora - t < 3600]
            if len(recentes) >= LIMITE_POR_HORA:
                self.envios[origem] = recentes
                return False
            self.envios[origem] = recentes + [agora]
            return True


class Tratador(BaseHTTPRequestHandler):
    server: Receptor
    server_version = "msscan-banco"
    sys_version = ""

    def log_message(self, formato: str, *args: Any) -> None:  # sem endereços no log do systemd
        pass

    def _responder(self, status: HTTPStatus) -> None:
        corpo = json.dumps({"ok": status == HTTPStatus.CREATED}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)

    def do_GET(self) -> None:  # noqa: N802
        self._responder(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?")[0] not in CAMINHOS:
            return self._responder(HTTPStatus.NOT_FOUND)
        origem = (self.headers.get("X-Forwarded-For") or self.client_address[0]).split(",")[0].strip()
        if not self.server.permitido(origem):
            return self._responder(HTTPStatus.TOO_MANY_REQUESTS)
        try:
            tamanho = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            tamanho = -1
        if not 0 < tamanho <= TAMANHO_MAXIMO:
            return self._responder(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        try:
            corpo = json.loads(self.rfile.read(tamanho))
        except (ValueError, UnicodeDecodeError):
            return self._responder(HTTPStatus.BAD_REQUEST)
        if validar(corpo):
            return self._responder(HTTPStatus.BAD_REQUEST)
        with self.server.trava:
            guardar(self.server.conexao, corpo)
        self._responder(HTTPStatus.CREATED)


def resumo(banco: str) -> str:
    conexao = abrir(banco)
    linhas = conexao.execute("SELECT fabricante, modelo, versao_msscan, COUNT(*) FROM coletas GROUP BY 1, 2, 3 ORDER BY 4 DESC").fetchall()
    total = sum(n for *_, n in linhas)
    return "\n".join([f"{total} coletas"] + [f"{n:5d}  {fab} {mod} (MSscan {ver})" for fab, mod, ver, n in linhas])


def main() -> None:
    args = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    args.add_argument("--porta", type=int, default=8787)
    args.add_argument("--banco", default=BANCO_PADRAO)
    args.add_argument("--resumo", action="store_true")
    opcoes = args.parse_args()
    if opcoes.resumo:
        print(resumo(opcoes.banco))
        return
    Receptor(("127.0.0.1", opcoes.porta), opcoes.banco).serve_forever()


if __name__ == "__main__":
    main()
