"""Chave da IA enviada pelo celular (CLAUDE.md 10d, pedido de 28/09/2026).

A tela "Configurar IA" mostra um QR Code com ``https://<IP do servidor>:47443/p/<token>``. O celular abre uma página
do próprio MSscan (botão para o site de criação da chave, campo para colar e "Enviar"). A chave chega aqui e fica
PENDENTE: só é salva quando o técnico aceita na tela do servidor ("Chave recebida do celular (IP x). Aceitar?").

Segurança:
- a porta só abre enquanto a tela está aberta (sinal de vida a cada poucos segundos; sem sinal por 20 s, fecha), por
  no máximo 5 minutos, e fecha logo que a chave chega (antes mesmo da decisão do técnico);
- token aleatório longo (256 bits), de uso único, na URL do QR; qualquer caminho ou token errado conta como tentativa
  inválida; passou do limite, fecha;
- HTTPS com certificado gerado na hora (``openssl``, curva P-256, 1 dia, apagado ao fechar); código curto de
  verificação na tela do servidor e na página do celular;
- a porta só fica aberta no firewall do quiosque durante o pareamento (conjunto ``pareamento`` da tabela
  ``inet msscan_quiosque``, com timeout de 5 min no próprio nftables — fecha sozinha mesmo se o MSscan cair);
- nada da chave vai para log, relatório ou histórico; o relatório registra só que houve pareamento.

O ``openssl`` e o servidor HTTPS rodam fora do executor (infraestrutura da interface, como o servidor da própria
interface; não tocam o hardware). O ``nft`` passa pelo executor (fica no histórico) e só roda na ISO
(``--ui-firewall-pareamento``, passado pelo ``msscan-iniciar``).
"""

from __future__ import annotations

import hashlib
import secrets
import shutil
import socketserver
import ssl
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs

PORTA = 47443
DURACAO_S = 300
SINAL_S = 20
"""Sem sinal de vida da tela por este tempo, o pareamento fecha (a tela foi fechada)."""
MAX_TENTATIVAS = 10
MAX_CORPO = 4096
TABELA_NFT = ("inet", "msscan_quiosque")
CONJUNTO_NFT = "pareamento"

AGUARDANDO, RECEBIDA, ACEITA, RECUSADA = "aguardando", "recebida", "aceita", "recusada"
EXPIRADA, ENCERRADA, TENTATIVAS, ERRO = "expirada", "encerrada", "tentativas", "erro"
FINAIS = {ACEITA, RECUSADA, EXPIRADA, ENCERRADA, TENTATIVAS, ERRO}


def gerar_certificado(pasta: Path, ip: str) -> tuple[Path, Path, str]:
    """Certificado autoassinado novo (P-256, 1 dia) para ``ip``: ``(certificado, chave, SHA-256 do certificado)``.

    Levanta ``OSError``/``subprocess.SubprocessError`` se o ``openssl`` falhar.
    """
    certificado, chave = pasta / "cert.pem", pasta / "chave.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-days", "1",
         "-subj", "/CN=MSscan pareamento", "-addext", f"subjectAltName=IP:{ip}", "-keyout", str(chave), "-out", str(certificado)],
        check=True, capture_output=True, timeout=30,
    )  # fmt: skip
    chave.chmod(0o600)
    der = ssl.PEM_cert_to_DER_cert(certificado.read_text(encoding="ascii"))
    return certificado, chave, hashlib.sha256(der).hexdigest()


class _ServidorHTTPS(ThreadingHTTPServer):
    """Sem o ``socket.getfqdn(ip)`` do ``HTTPServer.server_bind``: sem DNS reverso na rede, ele esperava ~10 s."""

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = str(self.server_address[0]), int(self.server_address[1])


class FirewallQuiosque:
    """Abre/fecha a porta do pareamento no firewall do quiosque (só na ISO; fora dela, não mexe em nada)."""

    def __init__(self, executor: Any, ativo: bool) -> None:
        self.executor, self.ativo = executor, ativo

    def abrir(self, porta: int, segundos: int) -> str:
        """``""`` se abriu (ou se o firewall não é gerenciado aqui); senão, o motivo."""
        if not self.ativo:
            return ""
        rc, _, erro = self.executor.rodar(["nft", "add", "element", *TABELA_NFT, CONJUNTO_NFT, f"{{ {porta} timeout {segundos}s }}"], timeout=10)
        return "" if rc == 0 else f"não foi possível abrir a porta {porta} no firewall ({erro.strip() or rc})"

    def fechar(self, porta: int) -> None:
        if self.ativo:
            self.executor.rodar(["nft", "delete", "element", *TABELA_NFT, CONJUNTO_NFT, f"{{ {porta} }}"], timeout=10)


class Pareamento:
    """Uma sessão de pareamento (um QR, um envio). Nunca levanta exceção para quem chama."""

    def __init__(self, ia: str, ia_nome: str, link_chave: str, ip: str, firewall: FirewallQuiosque, porta: int = PORTA,
                 certificado: Callable[[Path, str], tuple[Path, Path, str]] = gerar_certificado,
                 relogio: Callable[[], float] = time.monotonic, ao_terminar: Callable[["Pareamento"], None] | None = None) -> None:  # fmt: skip
        self.ia, self.ia_nome, self.link_chave, self.ip, self.porta = ia, ia_nome, link_chave, ip, porta
        self.firewall, self._certificado, self._relogio, self.ao_terminar = firewall, certificado, relogio, ao_terminar
        self.token = secrets.token_urlsafe(32)
        self.codigo = f"{secrets.randbelow(1_000_000):06d}"
        self.situacao = "iniciando"
        self.mensagem = ""
        self.impressao = ""
        self.tentativas_invalidas = 0
        self.ip_celular = ""
        self.inicio = datetime.now().isoformat(timespec="seconds")
        self.fim = ""
        self._chave: str | None = None
        self._trava = threading.Lock()
        self._servidor: ThreadingHTTPServer | None = None
        self._pasta: Path | None = None
        self._expira = 0.0
        self._sinal = 0.0
        self._fim_avisado = False

    @property
    def url(self) -> str:
        return f"https://{self.ip}:{self.porta}/p/{self.token}"

    # --- ciclo de vida -------------------------------------------------------------------------

    def iniciar(self) -> tuple[bool, str]:
        """Gera o certificado, abre o servidor HTTPS e a porta no firewall. ``(ok, mensagem)``."""
        try:
            self._pasta = Path(tempfile.mkdtemp(prefix="msscan-pareamento-"))
            certificado, chave, self.impressao = self._certificado(self._pasta, self.ip)
            contexto = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            contexto.minimum_version = ssl.TLSVersion.TLSv1_2
            contexto.load_cert_chain(certificado, chave)
            servidor = _ServidorHTTPS((self.ip, self.porta), _fabricar_handler(self))
            servidor.daemon_threads = True
            servidor.socket = contexto.wrap_socket(servidor.socket, server_side=True, do_handshake_on_connect=False)
        except (OSError, ssl.SSLError, subprocess.SubprocessError, ValueError) as erro:
            self._limpar_pasta()
            return self._falhou(f"não foi possível abrir o pareamento neste endereço ({self.ip}:{self.porta}): {erro}")
        motivo = self.firewall.abrir(self.porta, DURACAO_S)
        if motivo:
            servidor.server_close()
            self._limpar_pasta()
            return self._falhou(motivo)
        agora = self._relogio()
        with self._trava:
            self._servidor, self._expira, self._sinal, self.situacao = servidor, agora + DURACAO_S, agora, AGUARDANDO
        threading.Thread(target=servidor.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True, name="msscan-pareamento").start()
        threading.Thread(target=self._vigiar, daemon=True, name="msscan-pareamento-vigia").start()
        return True, "aguardando o celular"

    def _falhou(self, motivo: str) -> tuple[bool, str]:
        with self._trava:
            self.situacao, self.mensagem = ERRO, motivo
            self.fim = datetime.now().isoformat(timespec="seconds")
        self._avisar_fim()
        return False, motivo

    def manter(self) -> bool:
        """Sinal de vida da tela "Configurar IA". ``False`` se o pareamento já terminou."""
        with self._trava:
            if self.situacao in FINAIS:
                return False
            self._sinal = self._relogio()
            return True

    def _vigiar(self) -> None:
        while True:
            time.sleep(0.5)
            with self._trava:
                if self.situacao in FINAIS:
                    return
                agora = self._relogio()
                sem_sinal = agora - self._sinal > SINAL_S
                expirou = self.situacao == AGUARDANDO and agora > self._expira
            if sem_sinal:
                self.encerrar(ENCERRADA, "a tela Configurar IA foi fechada")
                return
            if expirou:
                self.encerrar(EXPIRADA, "passaram 5 minutos sem envio")
                return

    def _fechar_porta(self) -> None:
        """Fecha o servidor HTTPS e a porta no firewall, e apaga o certificado. Idempotente."""
        with self._trava:
            servidor, self._servidor = self._servidor, None
        if servidor is not None:
            # Espera o laço do servidor parar (≤ 0,1 s) antes de fechar: ao voltar, a porta está livre de fato (um QR
            # novo reabre na mesma porta). Nunca é chamado pela thread do ``serve_forever`` (só por handlers e vigia).
            servidor.shutdown()
            servidor.server_close()
            self.firewall.fechar(self.porta)
        self._limpar_pasta()

    def _limpar_pasta(self) -> None:
        if self._pasta is not None:
            shutil.rmtree(self._pasta, ignore_errors=True)
            self._pasta = None

    def encerrar(self, situacao: str = ENCERRADA, mensagem: str = "") -> None:
        """Fecha tudo e descarta a chave pendente (se houver)."""
        with self._trava:
            if self.situacao in FINAIS:
                return
            self.situacao, self.mensagem, self._chave = situacao, mensagem, None
            self.fim = datetime.now().isoformat(timespec="seconds")
        self._fechar_porta()
        self._avisar_fim()

    def _avisar_fim(self) -> None:
        if self.ao_terminar is not None and not self._fim_avisado:
            self._fim_avisado = True
            try:
                self.ao_terminar(self)
            except Exception:  # noqa: BLE001 - registrar o fim nunca derruba nada
                pass

    # --- decisão do técnico --------------------------------------------------------------------

    def aceitar(self) -> str | None:
        """Chave recebida (e a esquece aqui), ou ``None`` se não houver chave pendente."""
        with self._trava:
            if self.situacao != RECEBIDA or not self._chave:
                return None
            chave, self._chave = self._chave, None
            self.situacao, self.mensagem = ACEITA, "chave aceita pelo técnico"
            self.fim = datetime.now().isoformat(timespec="seconds")
        self._fechar_porta()
        self._avisar_fim()
        return chave

    def recusar(self) -> None:
        self.encerrar(RECUSADA, "chave recusada pelo técnico")

    # --- chamado pelo servidor HTTPS -----------------------------------------------------------

    def _token_valido(self, token: str) -> bool:
        return secrets.compare_digest(token.encode(), self.token.encode())

    def _invalida(self) -> None:
        with self._trava:
            self.tentativas_invalidas += 1
            passou = self.tentativas_invalidas > MAX_TENTATIVAS
        if passou:
            threading.Thread(target=self.encerrar, args=(TENTATIVAS, "tentativas inválidas demais"), daemon=True).start()

    def _receber(self, chave: str, ip: str) -> str:
        """``""`` se a chave foi aceita para a decisão do técnico; senão, o motivo (mostrado no celular)."""
        chave = "".join(chave.split())
        if not chave:
            return "Cole a chave antes de enviar."
        if len(chave) > 512 or not chave.isascii() or not chave.isprintable():
            return "Essa chave tem caracteres inválidos. Copie de novo do site do provedor."
        with self._trava:
            if self.situacao != AGUARDANDO:
                return "Este pareamento já foi usado ou encerrado. Gere um QR Code novo na tela do servidor."
            self._chave, self.ip_celular, self.situacao = chave, ip, RECEBIDA
            self.mensagem = "chave recebida; aguardando a confirmação na tela do servidor"
        # Uso único: a porta fecha já (a decisão do técnico é na tela do servidor).
        threading.Thread(target=self._fechar_porta, daemon=True).start()
        return ""

    # --- estado --------------------------------------------------------------------------------

    def estado(self) -> dict[str, Any]:
        """Para a tela do servidor (nunca inclui a chave)."""
        with self._trava:
            restante = max(0, int(self._expira - self._relogio())) if self.situacao == AGUARDANDO else 0
            return {"situacao": self.situacao, "mensagem": self.mensagem, "ia": self.ia, "ia_nome": self.ia_nome,
                    "url": self.url if self.situacao == AGUARDANDO else "", "codigo": self.codigo, "ip": self.ip,
                    "porta": self.porta, "ip_celular": self.ip_celular, "restante_s": restante,
                    "impressao": ":".join(self.impressao[i:i + 2] for i in range(0, min(len(self.impressao), 16), 2)).upper(),
                    "tentativas_invalidas": self.tentativas_invalidas}  # fmt: skip

    def registro(self) -> dict[str, Any]:
        """Para o relatório: que houve pareamento, sem a chave."""
        with self._trava:
            return {"ia": self.ia, "ia_nome": self.ia_nome, "inicio": self.inicio, "fim": self.fim or datetime.now().isoformat(timespec="seconds"),
                    "resultado": self.situacao, "ip_celular": self.ip_celular, "tentativas_invalidas": self.tentativas_invalidas}  # fmt: skip


# --- Página do celular -----------------------------------------------------------------------------

CSP = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"


def pagina_celular(p: Pareamento, erro: str = "", enviada: bool = False) -> str:
    """HTML autocontido (sem JavaScript) da página do celular."""
    corpo = (
        f"<p class='ok'>Chave enviada. Confirme na tela do servidor (código <strong>{escape(p.codigo[:3])} {escape(p.codigo[3:])}</strong>).</p>"
        "<p>Pode fechar esta página. A chave só é salva depois que você aceitar no servidor.</p>"
        if enviada else
        f"<p>Código de verificação: <strong class='codigo'>{escape(p.codigo[:3])} {escape(p.codigo[3:])}</strong><br>"
        "<small>Confira que é o mesmo código mostrado na tela do servidor.</small></p>"
        f"<p><a class='botao' href='{escape(p.link_chave)}' target='_blank' rel='noreferrer noopener'>1. Criar a chave no site: "
        f"{escape(p.ia_nome)}</a></p>"
        + (f"<p class='erro'>{escape(erro)}</p>" if erro else "")
        + f"<form method='post' action='/p/{escape(p.token)}' autocomplete='off'>"
        "<label for='chave'>2. Cole a chave aqui</label>"
        "<input id='chave' name='chave' type='password' autocomplete='off' autocapitalize='off' spellcheck='false' required>"
        "<button type='submit'>3. Enviar para o servidor</button></form>"
        "<p><small>A chave vai direto para o servidor pela rede local, criptografada (HTTPS). O navegador pode avisar que o "
        "certificado não é confiável: é esperado, ele foi gerado agora pelo MSscan.</small></p>"
    )  # fmt: skip
    return f"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="referrer" content="no-referrer">
<title>MSscan — chave da IA</title><style>
body{{font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;background:#0d1117;color:#e6edf3;margin:0;padding:24px 16px;line-height:1.5}}
h1{{font-size:1.25rem;margin:0 0 16px;background:linear-gradient(90deg,#1f6feb,#8957e5);-webkit-background-clip:text;background-clip:text;color:transparent}}
.codigo{{font-size:1.5rem;letter-spacing:.1em}}a.botao,button{{display:block;width:100%;box-sizing:border-box;padding:12px 16px;border-radius:8px;
font-size:1rem;text-align:center;text-decoration:none;border:0;margin:8px 0}}a.botao{{background:#21262d;color:#e6edf3}}
button{{background:#238636;color:#fff;font-weight:600}}input{{width:100%;box-sizing:border-box;padding:12px;font-size:1rem;border-radius:8px;
border:1px solid #30363d;background:#161b22;color:#e6edf3}}label{{display:block;margin-top:16px}}.erro{{color:#ff7b72}}.ok{{color:#3fb950}}
small{{color:#8b949e}}</style></head><body><main><h1>MSscan — enviar a chave da IA</h1>{corpo}</main></body></html>"""


def _fabricar_handler(pareamento: Pareamento) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "MSscan"
        sys_version = ""
        timeout = 15

        def log_message(self, formato: str, *args: Any) -> None:  # nunca registra a URL (tem o token)
            pass

        def _responder(self, status: int, html: str) -> None:
            dados = html.encode("utf-8")
            self.send_response(status)
            for nome, valor in (("Content-Type", "text/html; charset=utf-8"), ("Content-Length", str(len(dados))),
                                ("Cache-Control", "no-store"), ("Referrer-Policy", "no-referrer"),
                                ("X-Content-Type-Options", "nosniff"), ("Content-Security-Policy", CSP)):  # fmt: skip
                self.send_header(nome, valor)
            self.end_headers()
            self.wfile.write(dados)

        def _token(self) -> str | None:
            partes = self.path.split("?")[0].split("/")
            if len(partes) == 3 and partes[1] == "p" and pareamento._token_valido(partes[2]):
                return partes[2]
            return None

        def _nao_encontrado(self) -> None:
            pareamento._invalida()
            self._responder(HTTPStatus.NOT_FOUND, "<!doctype html><title>404</title><p>Não encontrado.</p>")

        def do_GET(self) -> None:  # noqa: N802
            if self._token() is None or pareamento.situacao != AGUARDANDO:
                self._nao_encontrado()
                return
            self._responder(HTTPStatus.OK, pagina_celular(pareamento))

        def do_POST(self) -> None:  # noqa: N802
            if self._token() is None:
                self._nao_encontrado()
                return
            try:
                tamanho = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                tamanho = -1
            if not 0 < tamanho <= MAX_CORPO:
                pareamento._invalida()
                self._responder(HTTPStatus.BAD_REQUEST, pagina_celular(pareamento, "Envio inválido."))
                return
            campos = parse_qs(self.rfile.read(tamanho).decode("utf-8", "replace"), max_num_fields=4)
            motivo = pareamento._receber((campos.get("chave") or [""])[0], self.client_address[0])
            del campos
            self._responder(HTTPStatus.OK if not motivo else HTTPStatus.BAD_REQUEST,
                            pagina_celular(pareamento, motivo) if motivo else pagina_celular(pareamento, enviada=True))  # fmt: skip

    return Handler
