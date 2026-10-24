"""Servidor Redfish falso (stdlib, só para testes): respostas DMTF gravadas + login que confere a senha.

O DMTF Redfish Mockup Server aceita qualquer senha; aqui o login só funciona com as
credenciais configuradas, para testar erro de senha, limite de tentativas e logout
por HTTP de verdade (``ExecutorReal``). Escuta só em 127.0.0.1.
"""

from __future__ import annotations

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

SESSOES = "/redfish/v1/SessionService/Sessions"


class ServidorRedfishFalso(ThreadingHTTPServer):
    """``with ServidorRedfishFalso(pasta_fixture) as srv: srv.base`` → ``http://127.0.0.1:<porta>``."""

    daemon_threads = True

    def __init__(
        self, fixture: Path, usuario: str = "tecnico", senha: str = "senha-certa", papeis: list[str] | None = None
    ) -> None:
        super().__init__(("127.0.0.1", 0), _Manipulador)
        gravado: dict[str, dict[str, Any]] = json.loads((Path(fixture) / "redfish.json").read_text(encoding="utf-8"))
        self.respostas = {k.split(" ", 1)[1]: v for k, v in gravado.items() if k.startswith("GET ")}
        self.usuario, self.senha, self.papeis = usuario, senha, papeis
        self.sessoes: dict[str, str] = {}  # token → caminho da sessão
        self.logins: list[tuple[str, int]] = []  # (usuário, status) — a senha não é guardada
        self.logouts = 0
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def __enter__(self) -> ServidorRedfishFalso:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.shutdown()
        self.server_close()


class _Manipulador(BaseHTTPRequestHandler):
    server: ServidorRedfishFalso

    def log_message(self, *_: Any) -> None:
        pass

    def _json(self, status: int, corpo: Any = None, cabecalhos: dict[str, str] | None = None) -> None:
        dados = json.dumps(corpo).encode() if corpo is not None else b""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(dados)))
        for chave, valor in (cabecalhos or {}).items():
            self.send_header(chave, valor)
        self.end_headers()
        self.wfile.write(dados)

    def _autenticado(self) -> bool:
        return self.headers.get("X-Auth-Token", "") in self.server.sessoes

    def do_GET(self) -> None:  # noqa: N802
        caminho = self.path.split("?", 1)[0].rstrip("/") or "/"
        if caminho == "/redfish/v1" or caminho == "/redfish/v1/":
            gravado = self.server.respostas.get("/redfish/v1") or self.server.respostas.get("/redfish/v1/")
            self._json(200, gravado["corpo"])
            return
        if not self._autenticado():
            self._json(401, {"error": {"message": "sem sessão"}})
            return
        if caminho.startswith(SESSOES + "/"):
            sessao = {"@odata.id": caminho, "Id": caminho.rsplit("/", 1)[1], "UserName": self.server.usuario}
            if self.server.papeis is not None:
                sessao["Roles"] = self.server.papeis
            self._json(200, sessao)
            return
        gravado = self.server.respostas.get(caminho)
        if gravado is None or gravado.get("status") != 200:
            self._json(404, {"error": {"message": "não existe"}})
            return
        self._json(200, gravado["corpo"])

    def do_POST(self) -> None:  # noqa: N802
        tamanho = int(self.headers.get("Content-Length") or 0)
        try:
            corpo = json.loads(self.rfile.read(tamanho) or b"{}")
        except json.JSONDecodeError:
            corpo = {}
        if self.path.rstrip("/") != SESSOES:
            self._json(405)
            return
        usuario = corpo.get("UserName", "")
        if usuario == self.server.usuario and corpo.get("Password") == self.server.senha:
            token = secrets.token_hex(8)
            caminho = f"{SESSOES}/{len(self.server.sessoes) + 1}"
            self.server.sessoes[token] = caminho
            self.server.logins.append((usuario, 201))
            self._json(201, {"@odata.id": caminho, "UserName": usuario}, {"X-Auth-Token": token, "Location": caminho})
        else:
            self.server.logins.append((usuario, 401))
            self._json(401, {"error": {"message": "credenciais inválidas"}})

    def do_DELETE(self) -> None:  # noqa: N802
        token = self.headers.get("X-Auth-Token", "")
        if self.server.sessoes.get(token) == self.path.rstrip("/"):
            del self.server.sessoes[token]
            self.server.logouts += 1
            self._json(204)
        else:
            self._json(401)
