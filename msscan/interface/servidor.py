"""Servidor HTTP local da interface gráfica (somente biblioteca padrão).

Segurança:

- escuta SOMENTE em 127.0.0.1 (constante, sem opção para mudar); o acesso de
  outro computador só é possível por túnel SSH, que exige login no servidor;
- token aleatório por sessão: toda rota ``/api/`` exige o header ``X-MSscan-Token``.
  Um header próprio não pode ser enviado por outro site sem permissão de CORS
  (que este servidor nunca concede), então não há CSRF;
- o header ``Host`` precisa ser 127.0.0.1 ou localhost (bloqueia DNS rebinding);
- arquivos estáticos vêm de uma lista fixa (sem caminhos arbitrários) e todas as
  respostas levam Content-Security-Policy sem nenhuma origem externa.

O token chega ao navegador no fragmento da URL (``/#t=...``), que o navegador
nunca envia ao servidor; o JavaScript guarda o token e limpa a barra de endereço.
"""

from __future__ import annotations

from msscan import textos

import base64
import hmac
import json
import secrets
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from msscan import __version__
from msscan.interface.sessao import Sessao
from msscan.interface.terminal import ARQUIVOS_XTERM, PASTA_XTERM, Terminal

ENDERECO = "127.0.0.1"
PORTA_PADRAO = 8765
HOSTS_PERMITIDOS = {"127.0.0.1", "localhost"}
PASTA_ESTATICOS = Path(__file__).parent / "estaticos"
ESTATICOS = {
    "index.html": "text/html; charset=utf-8",
    "tema.css": "text/css; charset=utf-8",
    "app.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "icones.js": "text/javascript; charset=utf-8",
    "modelo3d.js": "text/javascript; charset=utf-8",
}
PASTA_THREE = Path("/usr/share/javascript/three")
ARQUIVOS_THREE = {"three.min.js": "build/three.min.js", "OrbitControls.js": "examples/js/controls/OrbitControls.js"}
"""three.js r111 do pacote Debian ``libjs-three`` (MIT): modelo 3D do chassi (CLAUDE.md 18.1). Lista fixa."""
CSP_APP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
# O relatório HTML é autocontido com <style> embutido e sem nenhum script.
CSP_RELATORIO = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; frame-ancestors 'self'"
TAMANHO_MAX_CORPO = 4096


def _corpo_json(corpo: bytes) -> dict[str, Any] | None:
    """Corpo de um POST como objeto JSON, ou ``None`` se inválido."""
    try:
        dados = json.loads(corpo.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return dados if isinstance(dados, dict) else None


CSP_BLOQUEIO = "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"


def pagina_bloqueio(dominio: str) -> str:
    """Página "Site bloqueado pelo MSscan: <domínio>" (sem script; domínio validado e escapado)."""
    import html
    import re

    dominio = dominio.strip().lower()[:253]
    if not re.fullmatch(r"[a-z0-9.:-]+", dominio):
        dominio = textos.t("py_servidor.endereco_nao_permitido")
    dominio = html.escape(dominio)
    return textos.t("py_servidor.doctype_html_html_lang_pt", dominio, dominio)


class ServidorUI(ThreadingHTTPServer):
    """Servidor HTTP da interface, preso a 127.0.0.1."""

    daemon_threads = True

    def __init__(self, sessao: Sessao, porta: int = PORTA_PADRAO, token: str | None = None,
                 terminal: Terminal | None = None) -> None:
        self.sessao = sessao
        self.terminal = terminal
        """Terminal embutido (CLAUDE.md 15.5); ``None`` = indisponível (ex.: ``--replay``). Fica aqui, fora da sessão
        (e portanto fora do alcance da IA)."""
        self.token = token or secrets.token_urlsafe(32)
        self.ao_pronto: Callable[[], None] | None = None
        """Chamado quando o navegador carrega a interface (a ISO usa para saber que o modo gráfico subiu)."""
        super().__init__((ENDERECO, porta), ManipuladorUI)

    @property
    def porta(self) -> int:
        """Porta efetivamente em uso (útil com porta 0 nos testes)."""
        return int(self.server_address[1])

    @property
    def url(self) -> str:
        """URL completa para abrir a interface (com o token no fragmento)."""
        return f"http://{ENDERECO}:{self.porta}/#t={self.token}"


class ManipuladorUI(BaseHTTPRequestHandler):
    """Rotas da interface: estáticos, API JSON e o relatório HTML."""

    server: ServidorUI
    server_version = "MSscan"
    sys_version = ""

    def log_message(self, formato: str, *args: Any) -> None:  # noqa: D401
        """Sem log de acesso (a tela é do técnico; nada de tokens em logs)."""

    # Despacho ----------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - nome exigido pela http.server
        if not self._host_valido():
            return
        caminho = urlsplit(self.path).path
        if caminho == "/":
            self._estatico("index.html")
        elif caminho.startswith("/estaticos/three/"):
            nome = caminho.removeprefix("/estaticos/three/")
            try:
                corpo = (PASTA_THREE / ARQUIVOS_THREE[nome]).read_bytes() if nome in ARQUIVOS_THREE else None
            except OSError:
                corpo = None
            if corpo is None:
                self._erro(HTTPStatus.NOT_FOUND, textos.t("py_servidor.biblioteca_3d_nao_instalada_pacote"))
            else:
                self._enviar(HTTPStatus.OK, corpo, "text/javascript; charset=utf-8")
        elif caminho.startswith("/estaticos/xterm/"):
            self._xterm(caminho.removeprefix("/estaticos/xterm/"))
        elif caminho.startswith("/estaticos/"):
            self._estatico(caminho.removeprefix("/estaticos/"))
        elif caminho == "/relatorio.html":
            self._relatorio_html()
        elif caminho == "/voltar":
            # "Voltar ao MSscan" da barra do navegador integrado (sem token: só esconde a janela do navegador).
            self.server.sessao.manual.voltar()
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
        elif caminho == "/bloqueado":
            # Navegador integrado: site fora da lista oficial (sem token; só mostra o domínio recusado, CLAUDE.md 15.1).
            dominio = (parse_qs(urlsplit(self.path).query).get("dominio") or [""])[0]
            self._enviar(HTTPStatus.OK, pagina_bloqueio(dominio).encode("utf-8"), "text/html; charset=utf-8", CSP_BLOQUEIO)
        elif caminho.startswith("/api/"):
            if self._autorizado():
                self._api_get(caminho.removeprefix("/api/"))
        else:
            self._erro(HTTPStatus.NOT_FOUND, textos.t("py_servidor.rota_inexistente"))

    def do_POST(self) -> None:  # noqa: N802
        if not self._host_valido():
            return
        caminho = urlsplit(self.path).path
        if not caminho.startswith("/api/"):
            self._erro(HTTPStatus.NOT_FOUND, textos.t("py_servidor.rota_inexistente"))
            return
        if not self._autorizado():
            return
        tamanho = int(self.headers.get("Content-Length") or 0)
        if tamanho > TAMANHO_MAX_CORPO:
            self._erro(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, textos.t("py_servidor.corpo_grande_demais"))
            return
        corpo = self.rfile.read(tamanho) if tamanho else b""
        self._api_post(caminho.removeprefix("/api/"), corpo)

    # API ---------------------------------------------------------------------

    def _api_get(self, rota: str) -> None:
        sessao = self.server.sessao
        if rota == "estado":
            self._json(sessao.estado())
        elif rota == "visao":  # nova interface (09/10/2026): visão do servidor e sensores ao vivo
            self._json(sessao.visao())
        elif rota == "tela":
            tela = sessao.tela
            self._json(tela.estado() if tela else {"disponivel": False, "motivo": textos.t("py_servidor.resolucao_indisponivel_neste_modo")})
        elif rota == "ia/config":
            self._json(sessao.configuracao_ia())
        elif rota.startswith("ia/qr/"):
            qr = sessao.qr_ia(rota.removeprefix("ia/qr/"))
            if qr is None:
                self._erro(HTTPStatus.NOT_FOUND, textos.t("py_servidor.ia_desconhecida"))
            else:
                self._json(qr)
        elif rota == "chat":
            self._json(sessao.mensagens_chat())
        elif rota == "manual/arquivos":
            self._json(sessao.manual.arquivos())
        elif rota == "terminal/ler":
            terminal = self.server.terminal
            if terminal is None:
                self._erro(HTTPStatus.CONFLICT, textos.t("py_servidor.terminal_indisponivel_neste_modo"))
                return
            try:
                desde = max(0, int((parse_qs(urlsplit(self.path).query).get("desde") or ["0"])[0]))
            except ValueError:
                desde = 0
            dados, fim, ativo = terminal.ler(desde)
            self._json({"dados": base64.b64encode(dados).decode("ascii"), "fim": fim, "ativo": ativo})
        elif rota == "ia/previa":
            previa = sessao.previa_ia()
            if previa is None:
                self._erro(HTTPStatus.CONFLICT, textos.t("py_servidor.analise_por_ia_indisponivel_agora"))
            else:
                self._json(previa)
        elif rota.startswith("componente/"):
            detalhes = sessao.componente(rota.removeprefix("componente/"))
            if detalhes is None:
                self._erro(HTTPStatus.NOT_FOUND, textos.t("py_servidor.componente_ainda_nao_diagnosticado"))
            else:
                self._json(detalhes)
        else:
            self._erro(HTTPStatus.NOT_FOUND, textos.t("py_servidor.rota_inexistente"))

    def _api_post(self, rota: str, corpo: bytes = b"") -> None:
        sessao = self.server.sessao
        if rota == "bmc/credenciais":
            # Usuário e senha do BMC: só em memória, repassados à sessão e descartados (nunca em log).
            try:
                dados = json.loads(corpo.decode("utf-8") or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                dados = None
            if not isinstance(dados, dict) or not isinstance(dados.get("usuario"), str) or not isinstance(dados.get("senha"), str):
                self._erro(HTTPStatus.BAD_REQUEST, textos.t("py_servidor.informe_usuario_e_senha"))
                return
            aceito = sessao.informar_credenciais(dados["usuario"], dados["senha"])
            dados.clear()
            del corpo
        elif rota in ("tela/modo", "tela/manter", "tela/voltar"):
            tela = sessao.tela
            if tela is None:
                self._erro(HTTPStatus.CONFLICT, textos.t("py_servidor.resolucao_indisponivel_neste_modo"))
                return
            if rota == "tela/modo":
                try:
                    dados = json.loads(corpo.decode("utf-8") or "{}")
                except (UnicodeDecodeError, json.JSONDecodeError):
                    dados = {}
                ok, mensagem = tela.trocar(str(dados.get("modo") or ""))
                if not ok:
                    self._erro(HTTPStatus.CONFLICT, mensagem)
                    return
                self._json({"ok": True, "mensagem": mensagem})
                return
            aceito = tela.confirmar() if rota == "tela/manter" else tela.reverter()
        elif rota in ("ia/testar", "ia/salvar"):
            # Chaves de IA: só em memória, repassadas à sessão e descartadas (nunca em log nem na resposta).
            dados = _corpo_json(corpo)
            del corpo
            if dados is None:
                self._erro(HTTPStatus.BAD_REQUEST, textos.t("py_servidor.corpo_json_invalido"))
                return
            if rota == "ia/testar":
                if not isinstance(dados.get("id"), str) or not isinstance(dados.get("chave", ""), str):
                    self._erro(HTTPStatus.BAD_REQUEST, textos.t("py_servidor.informe_id_e_opcionalmente_chave"))
                    return
                resultado = sessao.testar_ia(dados["id"], dados.get("chave", ""))
                dados.clear()
                if resultado is None:
                    self._erro(HTTPStatus.NOT_FOUND, textos.t("py_servidor.ia_desconhecida"))
                else:
                    self._json(resultado)
                return
            chaves = dados.get("chaves") or {}
            principal, reserva = dados.get("principal"), dados.get("reserva")
            if not isinstance(chaves, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in chaves.items()) \
                    or not all(v is None or isinstance(v, str) for v in (principal, reserva)):  # fmt: skip
                self._erro(HTTPStatus.BAD_REQUEST, textos.t("py_servidor.formato_invalido"))
                return
            ok, mensagem = sessao.salvar_ia(principal, reserva, chaves)
            chaves.clear()
            dados.clear()
            if ok:
                self._json({"ok": True, "mensagem": mensagem})
            else:
                self._erro(HTTPStatus.CONFLICT, mensagem)
            return
        elif rota in ("manual/navegador", "manual/voltar", "manual/fechar", "manual/procurar"):
            manual = sessao.manual
            if rota == "manual/navegador":
                ok, mensagem = manual.abrir_navegador()
                if not ok:
                    self._erro(HTTPStatus.CONFLICT, mensagem)
                    return
                aceito = True
            elif rota == "manual/voltar":
                aceito = manual.voltar()
            elif rota == "manual/fechar":
                aceito = manual.fechar_navegador()
            else:
                manual.procurar()
                aceito = True
        elif rota == "manual/apagar":
            dados = _corpo_json(corpo) or {}
            if not isinstance(dados.get("nome"), str) or dados.get("confirmar") is not True:
                self._erro(HTTPStatus.BAD_REQUEST, textos.t("py_servidor.informe_nome_e_confirmar_true"))
                return
            ok, mensagem = sessao.manual.apagar(dados["nome"], True)
            if not ok:
                self._erro(HTTPStatus.CONFLICT, mensagem)
                return
            self._json({"ok": True, "mensagem": mensagem})
            return
        elif rota in ("manual/sha", "manual/instalar"):
            dados = _corpo_json(corpo) or {}
            campos = ("id", "sha") if rota == "manual/sha" else ("id", "confirmacao", "confirmacao_redundancia")
            if not all(isinstance(dados.get(c, ""), str) for c in campos) or not dados.get("id"):
                self._erro(HTTPStatus.BAD_REQUEST, "informe " + ", ".join(campos))
                return
            if rota == "manual/sha":
                ok, mensagem = sessao.manual.informar_sha(dados["id"], dados.get("sha", ""))
            else:
                ok, mensagem = sessao.manual.instalar(dados["id"], dados.get("confirmacao", ""), dados.get("confirmacao_redundancia", ""),
                                                       dados.get("confirmacao_riscos", ""))
            if not ok:
                self._erro(HTTPStatus.CONFLICT, mensagem)
                return
            self._json({"ok": True, "mensagem": mensagem})
            return
        elif rota == "atualizacoes/aviso":
            # Plano de atualização (18.2): relê o hardware e o BMC (só leitura) antes de qualquer instalação.
            dados = _corpo_json(corpo) or {}
            itens = dados.get("itens")
            if dados.get("origem") not in ("catalogo", "manual") or not isinstance(itens, list) \
                    or not all(isinstance(i, str) for i in itens):  # fmt: skip
                self._erro(HTTPStatus.BAD_REQUEST, textos.t("py_servidor.informe_origem_catalogo_ou_manual"))
                return
            ok, mensagem = sessao.preparar_aviso(dados["origem"], itens)
            if not ok:
                self._erro(HTTPStatus.CONFLICT, mensagem)
                return
            aceito = True
        elif rota == "log/exportar":
            gravado, onde, conteudo = sessao.exportar_log()
            self._json({"ok": True, "gravado": gravado, "onde": onde, "conteudo": conteudo, "nome": "msscan_log_tecnico.json"})
            return
        elif rota == "coleta/iniciar":
            ok, mensagem = sessao.coletar_dados()
            if not ok:
                self._erro(HTTPStatus.CONFLICT, mensagem)
                return
            aceito = True
        elif rota == "atualizacoes/aviso/cancelar":
            aceito = sessao.cancelar_aviso()
        elif rota == "atualizacoes/baixar":
            dados = _corpo_json(corpo) or {}
            itens = dados.get("itens")
            if not isinstance(itens, list) or not all(isinstance(i, str) for i in itens):
                self._erro(HTTPStatus.BAD_REQUEST, textos.t("py_servidor.informe_itens"))
                return
            ok, mensagem = sessao.baixar_escolhidos(itens)
            if not ok:
                self._erro(HTTPStatus.CONFLICT, mensagem)
                return
            aceito = True
        elif rota == "atualizacoes/aviso/ciente":
            dados = _corpo_json(corpo) or {}
            if not isinstance(dados.get("ciente"), bool):
                self._erro(HTTPStatus.BAD_REQUEST, textos.t("py_servidor.informe_ciente_true_ou_false"))
                return
            aceito = sessao.marcar_ciente(dados["ciente"])
        elif rota == "atualizacoes/verificar":
            aceito = sessao.verificar_atualizacoes()
        elif rota in ("atualizacoes/parar", "atualizacoes/continuar"):
            # 21.2: "Parar depois do componente atual" e "Continuar de onde parou" (nunca continua sozinho).
            ok, mensagem = sessao.parar_depois_do_atual() if rota == "atualizacoes/parar" else sessao.continuar_parado()
            if not ok:
                self._erro(HTTPStatus.CONFLICT, mensagem)
                return
            self._json({"ok": True, "mensagem": mensagem})
            return
        elif rota == "jobs/limpar":
            # 09/10/2026: job órfão na fila do iDRAC; "forcar" só depois do aviso de que a fila inteira será apagada.
            dados = _corpo_json(corpo) or {}
            if not isinstance(dados.get("id"), str) or not isinstance(dados.get("forcar", False), bool):
                self._erro(HTTPStatus.BAD_REQUEST, textos.t("py_servidor.informe_id_e_forcar_true"))
                return
            ok, mensagem = sessao.limpar_job_orfao(dados["id"], dados.get("forcar", False))
            if not ok:
                self._erro(HTTPStatus.CONFLICT, mensagem)
                return
            aceito = True
        elif rota == "jobs/reler":
            aceito = sessao.reler_jobs_idrac()
        elif rota == "atualizacoes/aplicar":
            dados = _corpo_json(corpo)
            itens = (dados or {}).get("itens")
            if dados is None or not isinstance(itens, list) or not all(isinstance(i, str) for i in itens) \
                    or not isinstance(dados.get("confirmacao", ""), str) or not isinstance(dados.get("confirmacao_redundancia", ""), str):  # fmt: skip
                self._erro(HTTPStatus.BAD_REQUEST, textos.t("py_servidor.informe_itens_confirmacao_e_se"))
                return
            ok, mensagem = sessao.aplicar_atualizacoes(itens, dados.get("confirmacao", ""), dados.get("confirmacao_redundancia", ""),
                                                       dados.get("confirmacao_riscos", ""))
            if not ok:
                self._erro(HTTPStatus.CONFLICT, mensagem)
                return
            aceito = True
        elif rota in ("acoes/prechecar", "acoes/executar"):
            dados = _corpo_json(corpo)
            identificador = (dados or {}).get("id")
            parametros = (dados or {}).get("parametros") or {}
            if dados is None or not isinstance(identificador, str) or not isinstance(parametros, dict) \
                    or not all(isinstance(v, str) for v in parametros.values()) or not isinstance(dados.get("confirmacao", ""), str):  # fmt: skip
                self._erro(HTTPStatus.BAD_REQUEST, textos.t("py_servidor.informe_id_parametros_texto_e"))
                return
            if rota == "acoes/prechecar":
                ok, mensagem = sessao.prechecar_acao(identificador, parametros)
            else:
                ok, mensagem = sessao.executar_acao(identificador, dados.get("confirmacao", ""))
            if not ok:
                self._erro(HTTPStatus.CONFLICT, mensagem)
                return
            aceito = True
        elif rota == "chat/repetir":
            ok, mensagem = sessao.repetir_chat()
            if not ok:
                self._erro(HTTPStatus.CONFLICT, mensagem)
                return
            aceito = True
        elif rota == "chat/enviar":
            dados = _corpo_json(corpo)
            if dados is None or not isinstance(dados.get("texto"), str) or len(dados["texto"]) > 20000:
                self._erro(HTTPStatus.BAD_REQUEST, textos.t("py_servidor.informe_texto"))
                return
            ok, mensagem = sessao.enviar_chat(dados["texto"])
            if not ok:
                self._erro(HTTPStatus.CONFLICT, mensagem)
                return
            aceito = True
        elif rota in ("ia/celular/iniciar", "ia/celular/manter", "ia/celular/parar", "ia/celular/aceitar", "ia/celular/recusar"):
            dados = _corpo_json(corpo) or {}
            if rota == "ia/celular/iniciar":
                if not isinstance(dados.get("id"), str):
                    self._erro(HTTPStatus.BAD_REQUEST, textos.t("py_servidor.informe_id"))
                    return
                ok, mensagem = sessao.iniciar_pareamento(dados["id"])
                if not ok:
                    self._erro(HTTPStatus.CONFLICT, mensagem)
                    return
                aceito = True
            elif rota == "ia/celular/aceitar":
                ok, mensagem, teste = sessao.aceitar_chave_celular()
                if not ok:
                    self._erro(HTTPStatus.CONFLICT, mensagem)
                    return
                self._json({"ok": True, "mensagem": mensagem, "teste": teste})
                return
            elif rota == "ia/celular/manter":
                aceito = sessao.manter_pareamento()
            elif rota == "ia/celular/parar":
                aceito = sessao.parar_pareamento()
            else:
                aceito = sessao.recusar_chave_celular()
        elif rota == "acoes/ntp_sugestoes":
            aceito = sessao.ler_sugestoes_ntp()
        elif rota in ("acoes/desfazer/prechecar", "acoes/desfazer"):
            dados = _corpo_json(corpo)
            indice = (dados or {}).get("indice")
            if dados is None or not isinstance(indice, int) or isinstance(indice, bool) or not isinstance(dados.get("confirmacao", ""), str):
                self._erro(HTTPStatus.BAD_REQUEST, textos.t("py_servidor.informe_indice_numero_e_para"))
                return
            if rota == "acoes/desfazer/prechecar":
                ok, mensagem = sessao.prechecar_desfazer(indice)
            else:
                ok, mensagem = sessao.desfazer_acao(indice, dados.get("confirmacao", ""))
            if not ok:
                self._erro(HTTPStatus.CONFLICT, mensagem)
                return
            aceito = True
        elif rota in ("terminal/abrir", "terminal/escrever", "terminal/tamanho", "terminal/ativar"):
            terminal = self.server.terminal
            if terminal is None:
                self._erro(HTTPStatus.CONFLICT, textos.t("py_servidor.terminal_indisponivel_neste_modo"))
                return
            dados = _corpo_json(corpo) or {}
            if rota == "terminal/ativar":
                terminal.liberar()
                aceito = True
            elif rota == "terminal/abrir":
                ok, mensagem = terminal.abrir()
                if not ok:
                    self._erro(HTTPStatus.LOCKED if not terminal.liberado else HTTPStatus.CONFLICT, mensagem)
                    return
                aceito = True
            elif rota == "terminal/escrever":
                if not isinstance(dados.get("dados"), str):
                    self._erro(HTTPStatus.BAD_REQUEST, textos.t("py_servidor.informe_dados"))
                    return
                aceito = terminal.escrever(dados["dados"])
            else:
                linhas, colunas = dados.get("linhas"), dados.get("colunas")
                if not all(isinstance(v, int) and not isinstance(v, bool) for v in (linhas, colunas)):
                    self._erro(HTTPStatus.BAD_REQUEST, textos.t("py_servidor.informe_linhas_e_colunas"))
                    return
                aceito = terminal.redimensionar(linhas, colunas)
        elif rota in ("ssh/ativar", "ssh/desativar"):
            ok, mensagem = sessao.ativar_ssh() if rota == "ssh/ativar" else sessao.desativar_ssh()
            if not ok:
                self._erro(HTTPStatus.CONFLICT, mensagem)
                return
            self._json({"ok": True, "mensagem": mensagem})
            return
        elif rota == "energia":
            dados = _corpo_json(corpo) or {}
            if dados.get("acao") not in ("desligar", "reiniciar") or dados.get("confirmar") is not True:
                self._erro(HTTPStatus.BAD_REQUEST, textos.t("py_servidor.informe_acao_desligar_ou_reiniciar"))
                return
            ok, mensagem = sessao.energia(dados["acao"])
            if not ok:
                self._erro(HTTPStatus.CONFLICT, mensagem)
                return
            self._json({"ok": True, "mensagem": mensagem})
            return
        elif rota == "ia/solicitar":
            aceito = sessao.solicitar_ia()
        elif rota == "ia/remover_chave":
            dados = _corpo_json(corpo) or {}
            if not isinstance(dados.get("id"), str) or dados.get("confirmar") is not True:
                self._erro(HTTPStatus.BAD_REQUEST, textos.t("py_servidor.informe_id_e_confirmar_true"))
                return
            ok, mensagem = sessao.remover_chave_ia(dados["id"])
            if not ok:
                self._erro(HTTPStatus.CONFLICT, mensagem)
                return
            self._json({"ok": True, "mensagem": mensagem})
            return
        elif rota == "ia/cancelar":
            aceito = sessao.cancelar_envio_ia()
        elif rota == "bmc/pular":
            aceito = sessao.pular_credenciais()
        elif rota == "idrac/tentar":
            aceito = sessao.tentar_acesso_idrac()
        elif rota == "rede/tentar":
            aceito = sessao.tentar_rede_novamente()
        elif rota == "rede/hora":
            ok, mensagem = sessao.ajustar_hora(str((_corpo_json(corpo) or {}).get("texto", "")))
            if not ok:
                self._erro(HTTPStatus.CONFLICT, mensagem)
                return
            self._json({"ok": True, "mensagem": mensagem})
            return
        elif rota == "diagnostico/iniciar":
            aceito = sessao.iniciar_diagnostico()
        elif rota == "diagnostico/refazer":
            dados = _corpo_json(corpo)
            componente = (dados or {}).get("componente")
            if dados is None or not (componente is None or isinstance(componente, str)):
                self._erro(HTTPStatus.BAD_REQUEST, textos.t("py_servidor.informe_componente_ou_null_para"))
                return
            aceito = sessao.refazer_diagnostico(componente or None)
        elif rota == "pronto":
            sessao.pagina_pronta = True
            if self.server.ao_pronto is not None:
                try:
                    self.server.ao_pronto()
                except OSError:
                    pass  # sem o aviso, a ISO só cai para o modo texto; a interface segue funcionando
            aceito = True
        else:
            self._erro(HTTPStatus.NOT_FOUND, textos.t("py_servidor.rota_inexistente"))
            return
        if aceito:
            self._json({"ok": True})
        else:
            self._erro(HTTPStatus.CONFLICT, textos.t("py_servidor.acao_indisponivel_nesta_etapa"))

    def _relatorio_html(self) -> None:
        # Carregado num <iframe>, que não envia headers próprios: token na query só nesta rota.
        token = (parse_qs(urlsplit(self.path).query).get("t") or [""])[0]
        if not hmac.compare_digest(token.encode(), self.server.token.encode()):
            self._erro(HTTPStatus.FORBIDDEN, textos.t("py_servidor.token_invalido"))
            return
        html = self.server.sessao.html_relatorio()
        if html is None:
            self._erro(HTTPStatus.NOT_FOUND, textos.t("py_servidor.relatorio_ainda_nao_gerado"))
            return
        self._enviar(HTTPStatus.OK, html.encode("utf-8"), "text/html; charset=utf-8", CSP_RELATORIO)

    # Apoio -------------------------------------------------------------------

    def _host_valido(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        nome = host.rsplit(":", 1)[0] if not host.startswith("[") else host
        if nome in HOSTS_PERMITIDOS:
            return True
        self._erro(HTTPStatus.FORBIDDEN, textos.t("py_servidor.host_nao_permitido"))
        return False

    def _autorizado(self) -> bool:
        token = self.headers.get("X-MSscan-Token") or ""
        if hmac.compare_digest(token.encode(), self.server.token.encode()):
            return True
        self._erro(HTTPStatus.FORBIDDEN, textos.t("py_servidor.token_invalido_ou_ausente"))
        return False

    def _estatico(self, nome: str) -> None:
        if nome == "textos.js":  # arquivo central de textos (msscan/textos/pt_BR.json), 09/10/2026
            self._enviar(HTTPStatus.OK, textos.script_js().encode("utf-8"), "text/javascript; charset=utf-8")
            return
        tipo = ESTATICOS.get(nome)
        if tipo is None:
            self._erro(HTTPStatus.NOT_FOUND, textos.t("py_servidor.arquivo_inexistente"))
            return
        self._enviar(HTTPStatus.OK, (PASTA_ESTATICOS / nome).read_bytes(), tipo)

    def _xterm(self, nome: str) -> None:
        """Arquivos do xterm.js do pacote Debian (lista fixa, sem caminhos arbitrários)."""
        arquivo = ARQUIVOS_XTERM.get(nome)
        try:
            corpo = (PASTA_XTERM / arquivo[0]).read_bytes() if arquivo else None
        except OSError:
            corpo = None
        if corpo is None:
            self._erro(HTTPStatus.NOT_FOUND, textos.t("py_servidor.biblioteca_do_terminal_nao_instalada"))
            return
        self._enviar(HTTPStatus.OK, corpo, arquivo[1])

    def _json(self, dados: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        corpo = json.dumps(dados, ensure_ascii=False, default=str).encode("utf-8")
        self._enviar(status, corpo, "application/json; charset=utf-8")

    def _erro(self, status: HTTPStatus, mensagem: str) -> None:
        self._json({"erro": mensagem}, status)

    def _enviar(self, status: HTTPStatus, corpo: bytes, tipo: str, csp: str = CSP_APP) -> None:
        self.send_response(status)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(corpo)))
        self.send_header("Content-Security-Policy", csp)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(corpo)
