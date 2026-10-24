"""Cliente Redfish sobre o ``Executor``: raiz sem autenticação, sessão (login/logout) e GET.

Regras (CLAUDE.md, seção 6b): sessão Redfish com ``X-Auth-Token`` e logout ao terminar;
o token e a senha ficam só na memória (nunca em histórico, relatório ou coleta).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from msscan.core.executor import CAMINHO_SESSOES, CAMINHOS_SESSOES, Executor, RespostaRedfish, caminho_url
from msscan.core.fallback import FalhaColeta
from msscan.parsers import ErroParser
from msscan.parsers.redfish import link, membros, parse_conta, parse_raiz, parse_sessao

MAX_CONTAS = 32
"""Limite de contas lidas para descobrir o papel do usuário (iDRAC tem 16)."""


class FalhaRedfish(FalhaColeta):
    """Requisição Redfish sem sucesso (a mensagem vai para as tentativas do relatório).

    ``status``: código HTTP quando o BMC respondeu (ex.: 404 = o recurso não existe); ``None`` = sem resposta (timeout,
    conexão cortada, sem sessão) — "não sei", nunca "não existe"."""

    def __init__(self, mensagem: str, status: int | None = None) -> None:
        super().__init__(mensagem)
        self.status = status


@dataclass
class ResultadoLogin:
    """Resultado de uma tentativa de login (nunca contém a senha nem o token)."""

    ok: bool
    mensagem: str
    status: int | None = None


def descrever_falha(resposta: RespostaRedfish) -> str:
    """Texto curto para uma resposta sem sucesso."""
    if resposta.status is None:
        return {
            "timeout": "o BMC não respondeu a tempo",
            "tls": f"erro de TLS com o BMC ({resposta.erro})",
            "conexao": "sem conexão com o BMC",
            "bloqueado": resposta.erro,
        }.get(resposta.erro_tipo, resposta.erro or "sem resposta do BMC")
    if resposta.status == 401:
        return "acesso negado (HTTP 401)"
    if resposta.status == 403:
        return "sem permissão (HTTP 403)"
    if resposta.status == 404:
        return "recurso não existe neste BMC (HTTP 404)"
    return f"HTTP {resposta.status}"


class ClienteRedfish:
    """Conversa com um serviço Redfish num endereço (``https://<ip>``)."""

    def __init__(self, executor: Executor, base: str) -> None:
        self.executor = executor
        self.base = base.rstrip("/")
        self.caminho_sessoes = CAMINHO_SESSOES
        self._token = ""
        self._sessao = ""
        self.__credenciais: tuple[str, str] | None = None
        """Só na memória deste objeto (6b), para reabrir a sessão depois de o iDRAC reiniciar (CLAUDE.md 21.3/21.4)."""

    @property
    def autenticado(self) -> bool:
        return bool(self._token)

    def url(self, caminho: str) -> str:
        """URL completa de um caminho ``/redfish/v1/...`` (links absolutos são reduzidos ao caminho)."""
        if caminho.startswith(("http://", "https://")):
            caminho = caminho_url(caminho)
        return self.base + (caminho if caminho.startswith("/") else "/" + caminho)

    def raiz(self) -> dict[str, Any]:
        """``GET /redfish/v1`` sem autenticação (permitido pela especificação). Confirma o serviço."""
        resposta = self.executor.redfish("GET", self.url("/redfish/v1"), timeout=10)
        if not resposta.ok:
            raise FalhaRedfish(f"raiz Redfish em {self.base}: {descrever_falha(resposta)}", resposta.status)
        try:
            raiz = parse_raiz(resposta.corpo)
        except ErroParser as erro:
            raise FalhaRedfish(f"{self.base}/redfish/v1: {erro}") from erro
        if raiz["sessoes"].rstrip("/") in CAMINHOS_SESSOES:
            self.caminho_sessoes = raiz["sessoes"].rstrip("/")
        return raiz

    def login(self, usuario: str, senha: str) -> ResultadoLogin:
        """Abre uma sessão Redfish. A senha fica só na memória (nunca em disco, log ou relatório), para ``reconectar``."""
        self.logout()
        corpo = {"UserName": usuario, "Password": senha}
        resposta = self.executor.redfish("POST", self.url(self.caminho_sessoes), corpo=corpo, timeout=20)
        if resposta.status in (404, 405):
            # Coleção errada (a senha nem foi conferida, não conta para bloqueio): tenta a outra (iDRAC7/8 × norma).
            outro = next(c for c in CAMINHOS_SESSOES if c != self.caminho_sessoes.rstrip("/"))
            segunda = self.executor.redfish("POST", self.url(outro), corpo=corpo, timeout=20)
            if segunda.status not in (404, 405, None):
                self.caminho_sessoes, resposta = outro, segunda
        if resposta.ok and resposta.token:
            self._token = resposta.token
            self._sessao = caminho_url(resposta.local) if resposta.local else ""
            self.__credenciais = (usuario, senha)
            return ResultadoLogin(True, "sessão Redfish aberta", resposta.status)
        if resposta.ok:
            return ResultadoLogin(False, "o BMC aceitou o login, mas não devolveu o token da sessão", resposta.status)
        if resposta.status in (400, 401, 403):
            return ResultadoLogin(False, "usuário ou senha não aceitos pelo BMC", resposta.status)
        return ResultadoLogin(False, descrever_falha(resposta), resposta.status)

    def get(self, caminho: str, timeout: float = 30) -> Any:
        """GET autenticado; devolve o JSON ou levanta ``FalhaRedfish``."""
        if not self._token:
            raise FalhaRedfish("sem sessão Redfish (credenciais do BMC não validadas)")
        resposta = self.executor.redfish("GET", self.url(caminho), token=self._token, timeout=timeout)
        if resposta.status == 401:
            self._token = ""
            raise FalhaRedfish(f"{caminho}: a sessão Redfish expirou ou foi encerrada pelo BMC")
        if not resposta.ok:
            raise FalhaRedfish(f"{caminho}: {descrever_falha(resposta)}", resposta.status)
        if resposta.corpo is None:
            raise FalhaRedfish(f"{caminho}: resposta sem JSON")
        return resposta.corpo

    def escrever(self, operacao: str, caminho: str, corpo: Any, arquivo: str | None = None, timeout: float = 900,
                 progresso: Any = None):  # fmt: skip
        """Escrita no BMC para atualizar firmware (``Executor.bmc_escrita``: salvaguardas + trava ``GRAVACAO_REAL_LIBERADA``).
        ``progresso(enviados, total)``: bytes do arquivo enviados (CLAUDE.md 21.1)."""
        if not self._token:
            raise FalhaRedfish("sem sessão Redfish (credenciais do BMC não validadas)")
        return self.executor.bmc_escrita(operacao, self.url(caminho), self._token, corpo, arquivo, timeout, progresso)

    def acao(self, operacao: str, caminho: str, corpo: Any, timeout: float = 60):
        """Ação corretiva no BMC (``Executor.acao_bmc``: salvaguardas + trava das ações corretivas)."""
        if not self._token:
            raise FalhaRedfish("sem sessão Redfish (credenciais do BMC não validadas)")
        return self.executor.acao_bmc(operacao, self.url(caminho), self._token, corpo, timeout)

    def reconectar(self) -> bool:
        """Depois de o iDRAC reiniciar (as sessões somem): confirma a raiz e abre outra sessão com as mesmas credenciais
        já validadas (nunca outras). ``False`` se o iDRAC ainda não responde ou recusou."""
        if self.__credenciais is None:
            return False
        try:
            self.raiz()
        except FalhaRedfish:
            return False
        usuario, senha = self.__credenciais
        self.descartar_sessao()
        return self.login(usuario, senha).ok

    def descartar_sessao(self) -> None:
        """Esquece a sessão sem DELETE: ela morreu com o reinício do iDRAC (o próximo ``reconectar`` abre outra)."""
        self._token = self._sessao = ""

    def logout(self) -> bool:
        """Encerra a sessão (DELETE), para não ocupar o limite de sessões do BMC."""
        if not self._token:
            return False
        token, sessao = self._token, self._sessao
        self._token, self._sessao = "", ""
        if not sessao:
            return False
        resposta = self.executor.redfish("DELETE", self.url(sessao), token=token, timeout=15)
        return resposta.ok

    def descobrir_papel(self, usuario: str, contas: str | None) -> tuple[str | None, list[str]]:
        """Papel do usuário (``RoleId``) e privilégios, se o BMC informar. Nunca levanta exceção."""
        papel: str | None = None
        try:
            if self._sessao:
                papeis = parse_sessao(self.get(self._sessao))["papeis"]
                papel = papeis[0] if papeis else None
            if papel is None and contas:
                colecao = link(self.get(contas), "Accounts")
                for membro in membros(self.get(colecao))[:MAX_CONTAS] if colecao else []:
                    try:
                        conta = parse_conta(self.get(membro))
                    except (FalhaRedfish, ErroParser):
                        continue  # conta de outro usuário sem permissão de leitura
                    if conta["usuario"] == usuario:
                        papel = conta["papel"]
                        break
        except (FalhaRedfish, ErroParser):
            return papel, []
        privilegios: list[str] = []
        if papel and contas:
            try:
                privilegios = list(self.get(f"{contas.rstrip('/')}/Roles/{papel}").get("AssignedPrivileges") or [])
            except (FalhaRedfish, ErroParser, AttributeError):
                privilegios = []
        return papel, [p for p in privilegios if isinstance(p, str)]
