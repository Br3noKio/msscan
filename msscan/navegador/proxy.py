"""Proxy local do navegador integrado (CLAUDE.md 11b): só HTTPS (``CONNECT`` na porta 443) para domínios oficiais.

O firewall do quiosque deixa o usuário do navegador (``msscan-web``) falar só com este proxy (e com a interface);
o proxy, que roda com o MSscan, só abre túneis para os domínios da lista. É a garantia de rede: a política do
Firefox (``WebsiteFilter``) é a segunda camada. HTTP sem TLS, outras portas, endereços IP e qualquer outro
domínio são recusados (403). O conteúdo do túnel é TLS de ponta a ponta: o proxy não vê páginas nem arquivos.
"""

from __future__ import annotations

import re
import select
import socket
import socketserver
import threading
import time
from pathlib import Path
from typing import Any, Callable

PORTA = 47480
DOMINIOS_OFICIAIS = ("dell.com", "dellcdn.com", "hpe.com", "lenovo.com", "www.google.com", "www.gstatic.com", "recaptcha.net")
"""Suporte e downloads: Dell (``www.dell.com``, ``dl.dell.com``, ``downloads.dell.com``; estáticos em ``dellcdn.com``),
HPE e Lenovo. Subdomínios valem; analytics e redes sociais das páginas ficam bloqueados (a página funciona sem eles).
Google só no necessário para a verificação "não sou um robô" (reCAPTCHA): ao abrir a página de drivers no R530 e no R620
(15/10/2026) o navegador ia direto para "Site bloqueado: www.google.com"; o usuário autorizou liberar. Só ``www.google.com``
(não ``google.com`` inteiro: nada de contas, Gmail etc.), ``www.gstatic.com`` e ``recaptcha.net``."""
_RE_HOST = re.compile(r"(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+")
LIMITE_CABECALHO = 16 * 1024
TEMPO_OCIOSO_S = 300
CACHE_DNS_S = 120.0
"""Cache de nomes do proxy (o sistema live não tem cache de DNS local): cada página da Dell abre dezenas de túneis."""
TENTATIVA_S = 4.0
"""Tempo por endereço antes de tentar o próximo (IPv6 sem rota ou endereço fora do ar não prende a página)."""
ULTIMA_TENTATIVA_S = 12.0


def tem_ipv6_global(ler: Callable[[str], str] = lambda c: Path(c).read_text()) -> bool:
    """Há endereço IPv6 global (escopo 00 em ``/proc/net/if_inet6``) e rota padrão IPv6 (``::/0`` em ``ipv6_route``)?

    Sem os dois, tentar IPv6 só atrasa: o proxy vai direto ao IPv4 (CLAUDE.md 15.1).
    """
    try:
        enderecos = ler("/proc/net/if_inet6").splitlines()
        rotas = ler("/proc/net/ipv6_route").splitlines()
    except OSError:
        return False
    global_ = any(len(c) >= 6 and c[3] == "00" and c[5] != "lo" for c in (l.split() for l in enderecos))
    padrao = any(l.split()[:2] == ["0" * 32, "00"] and not l.split()[-1] == "lo" for l in rotas if len(l.split()) >= 10)
    return global_ and padrao


def ordenar(enderecos: list[tuple[int, str]], ipv6: bool) -> list[tuple[int, str]]:
    """Sem IPv6 global: só IPv4. Com IPv6: intercala IPv6 e IPv4 (RFC 8305), começando pelo IPv6."""
    v4 = [e for e in enderecos if e[0] == socket.AF_INET]
    v6 = [e for e in enderecos if e[0] == socket.AF_INET6]
    if not ipv6:
        return v4 or v6
    ordem = []
    for i in range(max(len(v4), len(v6))):
        ordem += v6[i:i + 1] + v4[i:i + 1]
    return ordem


def permitido(host: str, porta: int) -> bool:
    """Só nomes (nunca IP) de um domínio oficial ou subdomínio dele, na porta 443."""
    host = host.lower().rstrip(".")
    if porta != 443 or not _RE_HOST.fullmatch(host) or re.fullmatch(r"[0-9.]+", host):
        return False
    return any(host == d or host.endswith("." + d) for d in DOMINIOS_OFICIAIS)


def ler_pedido(conexao: socket.socket) -> tuple[str, int] | None:
    """``CONNECT host:porta HTTP/1.1`` → ``(host, porta)``; qualquer outro pedido → ``None``."""
    dados = b""
    while b"\r\n\r\n" not in dados:
        parte = conexao.recv(4096)
        if not parte or len(dados) > LIMITE_CABECALHO:
            return None
        dados += parte
    linha = dados.split(b"\r\n", 1)[0].decode("latin-1")
    casado = re.fullmatch(r"CONNECT ([A-Za-z0-9.-]+):(\d{1,5}) HTTP/1\.[01]", linha)
    return (casado.group(1), int(casado.group(2))) if casado else None


class _Tratador(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        proxy: ProxyOficial = self.server.proxy  # type: ignore[attr-defined]
        cliente = self.request
        cliente.settimeout(30)
        try:
            pedido = ler_pedido(cliente)
        except OSError:
            return
        if pedido is None or not permitido(*pedido):
            proxy.registrar(pedido[0] if pedido else "(pedido que não é CONNECT)", False)
            cliente.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            return
        proxy.registrar(pedido[0], True)
        destino = proxy.conectar(*pedido)
        if destino is None:
            cliente.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            return
        with destino:
            cliente.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            _encaminhar(cliente, destino)


def _encaminhar(a: socket.socket, b: socket.socket) -> None:
    a.settimeout(None)
    b.settimeout(None)
    pares = {a: b, b: a}
    while True:
        prontos, _, _ = select.select(list(pares), [], [], TEMPO_OCIOSO_S)
        if not prontos:
            return
        for origem in prontos:
            try:
                dados = origem.recv(65536)
                if not dados:
                    return
                pares[origem].sendall(dados)
            except OSError:
                return


class _Servidor(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


class ProxyOficial:
    """Proxy em ``127.0.0.1:porta`` numa thread. ``acessos``: domínios permitidos e recusados (sem caminhos)."""

    def __init__(self, porta: int = PORTA, resolver: Callable[..., Any] = socket.getaddrinfo,
                 ipv6: Callable[[], bool] = tem_ipv6_global, relogio: Callable[[], float] = time.monotonic) -> None:  # fmt: skip
        self.porta = porta
        self.permitidos: dict[str, int] = {}
        self.recusados: dict[str, int] = {}
        self.falhas: dict[str, str] = {}
        """Último motivo de falha ao conectar, por domínio (aparece na tela: "sem resposta de dl.dell.com")."""
        self._trava = threading.Lock()
        self._servidor: _Servidor | None = None
        self._resolver, self._ipv6, self._relogio = resolver, ipv6, relogio
        self._dns: dict[str, tuple[float, list[tuple[int, str]]]] = {}

    def resolver(self, host: str, porta: int) -> list[tuple[int, str]]:
        """Endereços do nome (cache de ``CACHE_DNS_S``). Erro de DNS → lista vazia."""
        agora = self._relogio()
        with self._trava:
            guardado = self._dns.get(host)
        if guardado and agora - guardado[0] < CACHE_DNS_S:
            return guardado[1]
        try:
            infos = self._resolver(host, porta, type=socket.SOCK_STREAM)
        except OSError:
            return []
        enderecos = list(dict.fromkeys((familia, str(sockaddr[0])) for familia, _, _, _, sockaddr in infos))
        with self._trava:
            self._dns[host] = (agora, enderecos)
        return enderecos

    def conectar(self, host: str, porta: int) -> socket.socket | None:
        """Conecta ao destino (IPv4 direto sem IPv6 global; tentativa curta por endereço). Falha → ``None`` + motivo."""
        enderecos = ordenar(self.resolver(host, porta), self._ipv6())
        if not enderecos:
            self._falhou(host, "nome não resolvido (DNS)")
            return None
        motivo = ""
        for indice, (familia, endereco) in enumerate(enderecos):
            limite = ULTIMA_TENTATIVA_S if indice == len(enderecos) - 1 else TENTATIVA_S
            try:
                conexao = socket.socket(familia, socket.SOCK_STREAM)
                conexao.settimeout(limite)
                conexao.connect((endereco, porta))
                return conexao
            except OSError as erro:
                conexao.close()
                motivo = "sem resposta (tempo esgotado)" if isinstance(erro, TimeoutError) else (erro.strerror or str(erro))
        self._falhou(host, motivo)
        return None

    def _falhou(self, host: str, motivo: str) -> None:
        with self._trava:
            self.falhas[host] = motivo

    def registrar(self, host: str, ok: bool) -> None:
        with self._trava:
            contagem = self.permitidos if ok else self.recusados
            contagem[host] = contagem.get(host, 0) + 1

    def iniciar(self) -> None:
        servidor = _Servidor(("127.0.0.1", self.porta), _Tratador)
        servidor.proxy = self  # type: ignore[attr-defined]
        self._servidor = servidor
        threading.Thread(target=servidor.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True, name="msscan-proxy").start()

    def encerrar(self) -> None:
        if self._servidor is not None:
            self._servidor.shutdown()
            self._servidor.server_close()
            self._servidor = None

    def resumo(self) -> dict[str, Any]:
        with self._trava:
            return {"permitidos": dict(self.permitidos), "recusados": dict(self.recusados), "falhas": dict(self.falhas)}
