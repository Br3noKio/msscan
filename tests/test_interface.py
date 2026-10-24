"""Testes da interface gráfica (fase 5): sessão, servidor local, frontend e regras de segurança.

Tudo roda sobre o replay do R640 real; o servidor sobe numa porta livre de 127.0.0.1.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from main import criar_executor, criar_parser
from msscan.core.executor import ExecutorReplay
from msscan.core.modelo import Resultado, Status
from msscan.deteccao.contexto import Contexto
from msscan.deteccao.detectores import detectar
from msscan.deteccao.resumo import capacidades
from msscan.interface import sessao as modulo_sessao
from msscan.interface.servidor import ENDERECO, ESTATICOS, PASTA_ESTATICOS, ServidorUI
from msscan.interface.sessao import CONCLUIDO, PRONTO, REDE, Sessao
from msscan.modulos.registro import Registro, registro_padrao
from msscan.rede.verificacao import niveis_rede, verificar_rede
from msscan.relatorio.completo import gravar_relatorios
from msscan.relatorio.terminal import veredito
from tests.conftest import R640, ExecutorFalso, com_textos
from tests.test_rede import CARRIER_ENO3, ReplayAlterado

ESTATICOS_TEXTO = {nome: com_textos((PASTA_ESTATICOS / nome).read_text(encoding="utf-8")) for nome in ESTATICOS}


def sessao_concluida(**kwargs) -> Sessao:
    sessao = Sessao(ExecutorReplay(R640), None, replay=True, **kwargs)
    sessao.iniciar()
    sessao.aguardar(30)
    assert sessao.iniciar_diagnostico()
    sessao.aguardar(30)
    return sessao


# --- Núcleo reaproveitado --------------------------------------------------------------


class TestNucleoCompartilhado:
    def test_progresso_do_registro(self):
        eventos: list[tuple[str, str | None]] = []
        executor = ExecutorReplay(R640)
        registro_padrao().diagnosticar(
            detectar(executor, None), lambda nome, r: eventos.append((nome, r.status.value if r else None))
        )
        nomes = [c.nome for c in registro_padrao().componentes()]
        assert [e[0] for e in eventos[::2]] == nomes
        assert all(e[1] is None for e in eventos[::2]) and all(e[1] for e in eventos[1::2])

    def test_callback_com_erro_nao_para_o_diagnostico(self):
        def explode(nome: str, resultado: Resultado | None) -> None:
            raise RuntimeError("bug na interface")

        resultados = registro_padrao().diagnosticar(detectar(ExecutorReplay(R640), None), explode)
        assert len(resultados) == len(registro_padrao().componentes())

    def test_niveis_rede_ok(self):
        niveis = niveis_rede(verificar_rede(ExecutorReplay(R640)))
        assert [(n["chave"], n["situacao"]) for n in niveis] == [("cabo", "ok"), ("ip", "ok"), ("internet", "ok")]
        assert niveis[1]["detalhe"].startswith("eno3 192.0.2.2/24")

    def test_niveis_rede_sem_cabo(self):
        niveis = niveis_rede(verificar_rede(ReplayAlterado(arquivos={CARRIER_ENO3: "0\n"})))
        assert [n["situacao"] for n in niveis] == ["falha", "nao_testado", "nao_testado"]
        assert all(n["detalhe"] == "" for n in niveis)

    def test_capacidades_r640_so_com_evidencia(self):
        caps = {c["chave"]: c for c in capacidades(detectar(ExecutorReplay(R640), None))}
        assert caps["ipmi"]["disponivel"] and caps["ipmi"]["detalhe"] == "IPMI 2.0"
        assert not caps["redfish"]["disponivel"]  # SMBIOS 42 ausente no R640 coletado
        assert not caps["raid"]["disponivel"] and not caps["ferramentas"]["disponivel"]
        assert not caps["pendrive"]["disponivel"]

    def test_capacidades_sem_nada_detectado(self):
        caps = capacidades(Contexto(executor=ExecutorFalso()))
        assert not any(c["disponivel"] for c in caps)

    def test_gravar_relatorios(self, tmp_path):
        relatorio = {"equipamento": {"modelo": "PowerEdge R640"}, "veredito": "x"}
        assert gravar_relatorios(relatorio, "t", None, replay=True).pasta is None
        gravado = gravar_relatorios(relatorio, "t", str(tmp_path), replay=True)
        assert gravado.no_pendrive and (gravado.pasta / "relatorio.html").is_file()
        assert gravado.mensagem.startswith("Relatório gravado no pendrive")


# --- Sessão ------------------------------------------------------------------------


class TestSessao:
    def test_fluxo_completo_igual_ao_modo_texto(self):
        sessao = Sessao(ExecutorReplay(R640), None, replay=True)
        assert sessao.estado()["etapa"] == REDE
        sessao.iniciar()
        sessao.aguardar(30)
        estado = sessao.estado()
        assert estado["etapa"] == PRONTO and estado["rede"]["situacao"] == "ok"
        assert estado["equipamento"]["modelo"] == "PowerEdge R640"
        assert estado["diagnostico"]["concluidos"] == 0

        assert sessao.iniciar_diagnostico()
        assert not sessao.iniciar_diagnostico()  # nunca dois diagnósticos ao mesmo tempo
        sessao.aguardar(30)
        estado = sessao.estado()
        assert estado["etapa"] == CONCLUIDO
        esperado = veredito(registro_padrao().diagnosticar(detectar(ExecutorReplay(R640), None)))
        assert estado["diagnostico"]["veredito"] == esperado == estado["relatorio"]["veredito"]
        assert "Resultado: 4 alertas e nenhuma falha." in esperado
        assert estado["relatorio"]["pasta"] is None
        assert "não gravado" in estado["relatorio"]["mensagem"]
        json.dumps(estado)  # serializável

    def test_detalhe_do_componente(self):
        sessao = sessao_concluida()
        fontes = sessao.componente("fontes")
        assert fontes["status"] == "ALERTA" and fontes["titulo"] == "Fontes de alimentação"
        assert fontes["dados"]["redundancia_perdida"] and not fontes["dados"]["defeito"]
        assert [c["comando"] for c in fontes["comandos"]] == [["ipmitool", "sdr", "type", "Power Supply"]]
        tentativas = {t["provedor"]: t["resultado"] for t in fontes["dados"]["tentativas"]}
        assert tentativas["Redfish (Power)"] == "indisponível"  # sem credenciais do BMC
        assert tentativas["IPMI (ipmitool sdr type Power Supply)"] == "sucesso"
        assert sessao.componente("inexistente") is None

    def test_html_do_relatorio_e_o_mesmo_do_pendrive(self, tmp_path):
        sessao = Sessao(ExecutorReplay(R640), str(tmp_path), replay=True)
        assert sessao.html_relatorio() is None
        sessao.iniciar()
        sessao.aguardar(30)
        sessao.iniciar_diagnostico()
        sessao.aguardar(30)
        estado = sessao.estado()["relatorio"]
        assert estado["no_pendrive"]
        gravado = (Path(estado["pasta"]) / "relatorio.html").read_text(encoding="utf-8")
        assert gravado == sessao.html_relatorio()

    def test_sem_rede_segue_e_tentar_novamente_reconecta(self):
        """28.1: a rede não bloqueia mais; a faixa mostra o motivo e "Tentar novamente" refaz tudo."""
        executor = ReplayAlterado(arquivos={CARRIER_ENO3: "0\n"})
        esperas: list[float] = []
        sessao = Sessao(executor, None, replay=False, espera_rede=6, dormir=esperas.append, monitorar_rede=False)
        sessao.iniciar()
        sessao.aguardar(30)
        estado = sessao.estado()
        assert estado["etapa"] == PRONTO and estado["rede"]["situacao"] == "sem_internet"
        assert estado["rede"]["titulo"].startswith("SEM CABO") and "cabo" in estado["rede"]["causa"]
        assert [n["situacao"] for n in estado["rede"]["niveis"]] == ["falha", "nao_testado", "nao_testado"]
        assert esperas == [3, 3]  # esperou o link por até 6 s antes de seguir
        assert estado["equipamento"] is not None  # detecção feita sem internet
        assert sessao.iniciar_diagnostico()
        sessao.aguardar(30)

        executor.troca_arquivos.clear()  # "conectou o cabo"
        assert sessao.tentar_rede_novamente()
        for _ in range(100):
            if sessao.estado()["rede"]["situacao"] == "ok":
                break
            time.sleep(0.05)
        assert sessao.estado()["rede"]["situacao"] == "ok" and sessao.rede.ok

    def test_rede_que_cai_e_volta_varias_vezes_nunca_fica_presa(self):
        """A4: a faixa "Sem internet" some em TODA volta da rede, inclusive depois de um "Tentar novamente" que falhou."""
        executor = ReplayAlterado(arquivos={CARRIER_ENO3: "0\n"})
        sessao = Sessao(executor, None, replay=False, espera_rede=0, dormir=lambda _s: None)
        sessao.iniciar()
        sessao.aguardar(30)
        monitor = sessao.monitor
        monitor.parar()
        assert sessao.estado()["rede"]["situacao"] == "sem_internet"
        executor.troca_arquivos.clear()
        monitor.verificar_agora()
        assert sessao.estado()["rede"]["situacao"] == "ok" and sessao.rede.ok
        executor.troca_arquivos[CARRIER_ENO3] = "0\n"  # caiu de novo
        monitor.verificar_agora()
        monitor.verificar_agora()
        assert not monitor.ok
        sessao._repetir_rede()  # "Tentar novamente" com a rede fora
        assert sessao.estado()["rede"]["situacao"] == "sem_internet"
        executor.troca_arquivos.clear()  # voltou
        monitor.verificar_agora()
        assert sessao.estado()["rede"]["situacao"] == "ok" and sessao.rede.ok

    def test_proxy_conf_criado_com_o_msscan_aberto_vale_no_tentar_novamente(self, tmp_path, monkeypatch):
        """28.1: na interface (modo da ISO) o proxy.conf é lido no início e relido em "Tentar novamente"."""
        for variavel in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY"):
            monkeypatch.setenv(variavel, "")  # registra o valor original: o carregar_proxy grava no os.environ
            monkeypatch.delenv(variavel)
        executor = ReplayAlterado(arquivos={CARRIER_ENO3: "0\n"})
        sessao = Sessao(executor, str(tmp_path), replay=False, espera_rede=0, dormir=lambda _s: None, monitorar_rede=False)
        sessao.iniciar()
        sessao.aguardar(30)
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "proxy.conf").write_text("isto não é um proxy\n", encoding="utf-8")
        sessao._repetir_rede()
        assert any("proxy.conf ignorado" in d for d in sessao.rede.detalhes)
        (tmp_path / "config" / "proxy.conf").write_text("http://proxy.exemplo:3128\n", encoding="utf-8")
        sessao._repetir_rede()
        assert os.environ["https_proxy"] == "http://proxy.exemplo:3128"
        assert not any("proxy.conf ignorado" in d for d in sessao.rede.detalhes)

    def test_relogio_atrasado_dias_aparece_no_inicio(self):
        """28.1 (QEMU, 14/10/2026): o aviso de corrigir a data no BIOS aparece no Início também com a rede OK."""
        from msscan.rede.verificacao import ResultadoRede

        sessao = Sessao(ExecutorReplay(R640), None, replay=True)
        mensagem = "O relógio do servidor estava atrasado 183 dias e foi ajustado (NTP). Recomenda-se corrigir a data no BIOS."
        sessao.rede = ResultadoRede(True, relogio={"ajustado": True, "diferenca_s": 183 * 86400, "mensagem": mensagem})
        assert sessao._estado_rede()["aviso_relogio"] == mensagem
        sessao.rede = ResultadoRede(True, relogio={"ajustado": True, "diferenca_s": 40, "mensagem": "Relógio do sistema ajustado"})
        assert sessao._estado_rede()["aviso_relogio"] == ""
        assert 'id="inicio-aviso-relogio"' in (PASTA_ESTATICOS / "index.html").read_text(encoding="utf-8")

    def test_hora_pelo_tecnico_na_interface(self):
        sessao = Sessao(ExecutorReplay(R640), None, replay=True)
        ok, mensagem = sessao.ajustar_hora("13/10/2026 10:00")
        assert not ok and "não é alterado" in mensagem

    def test_tentar_novamente_so_quando_falhou(self):
        sessao = Sessao(ExecutorReplay(R640), None, replay=True)
        sessao.iniciar()
        sessao.aguardar(30)
        assert not sessao.tentar_rede_novamente()

    def test_parametro_semrede(self):
        executor = ReplayAlterado()
        cmdline = (R640 / "arquivos/proc/cmdline").read_text().strip()
        executor.troca_arquivos["/proc/cmdline"] = cmdline + " msscan.semrede=1\n"
        sessao = Sessao(executor, None, replay=False)
        sessao.iniciar()
        sessao.aguardar(30)
        estado = sessao.estado()
        assert estado["rede"]["situacao"] == "desativada" and estado["rede"]["sem_rede_por_parametro"]
        assert estado["etapa"] == PRONTO
        assert not any(c[0] == "GET" for c in (e.comando for e in executor.historico))

    def test_erro_inesperado_vira_etapa_erro(self, monkeypatch):
        def explode(*_args, **_kwargs):
            raise RuntimeError("bug")

        monkeypatch.setattr(modulo_sessao, "detectar", explode)
        sessao = Sessao(ExecutorReplay(R640), None, replay=True)
        sessao.iniciar()
        sessao.aguardar(30)
        assert sessao.estado()["etapa"] == "erro" and "RuntimeError: bug" in sessao.estado()["erro"]

    def test_progresso_ao_vivo(self):
        vistos: list[tuple[int, str]] = []

        class Espiao(Registro):
            def diagnosticar(self, ctx, progresso=None, somente=None):
                def espiar(nome, resultado):
                    progresso(nome, resultado)
                    d = sessao.estado()["diagnostico"]
                    atual = [c["componente"] for c in d["componentes"] if c["em_andamento"]]
                    vistos.append((d["concluidos"], atual[0] if atual else ""))

                return registro_padrao().diagnosticar(ctx, espiar, somente)

        registro = Espiao()
        for comp in registro_padrao().componentes():
            registro.registrar_componente(comp.nome, comp.titulo)
        sessao = Sessao(ExecutorReplay(R640), None, replay=True, registro=registro)
        sessao.iniciar()
        sessao.aguardar(30)
        assert sessao.iniciar_diagnostico()
        sessao.aguardar(30)
        assert vistos[0] == (0, "sensores") and vistos[1] == (1, "")
        assert vistos[-1] == (10, "")
        assert sessao.estado()["diagnostico"]["concluidos"] == 10


# --- Servidor HTTP -------------------------------------------------------------------


@pytest.fixture
def servidor():
    sessao = sessao_concluida()
    srv = ServidorUI(sessao, porta=0)
    thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def pedir(srv: ServidorUI, caminho: str, metodo: str = "GET", token: str | None = "", host: str | None = None):
    headers = {}
    if token is not None:
        headers["X-MSscan-Token"] = token or srv.token
    if host:
        headers["Host"] = host
    req = urllib.request.Request(f"http://127.0.0.1:{srv.porta}{caminho}", method=metodo, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resposta:
            return resposta.status, dict(resposta.headers), resposta.read()
    except urllib.error.HTTPError as erro:
        return erro.code, dict(erro.headers), erro.read()


class TestServidor:
    def test_so_escuta_em_127_0_0_1(self, servidor):
        assert ENDERECO == "127.0.0.1"
        assert servidor.server_address[0] == "127.0.0.1"
        assert servidor.url.startswith(f"http://127.0.0.1:{servidor.porta}/#t=")

    def test_token_aleatorio_por_sessao(self):
        sessao = Sessao(ExecutorReplay(R640), None, replay=True)
        a, b = ServidorUI(sessao, porta=0), ServidorUI(sessao, porta=0)
        try:
            assert a.token != b.token and len(a.token) >= 40
        finally:
            a.server_close()
            b.server_close()

    def test_api_exige_token(self, servidor):
        assert pedir(servidor, "/api/estado", token=None)[0] == 403
        assert pedir(servidor, "/api/estado", token="errado")[0] == 403
        assert pedir(servidor, "/api/diagnostico/iniciar", "POST", token=None)[0] == 403
        status, _, corpo = pedir(servidor, "/api/estado")
        assert status == 200 and json.loads(corpo)["etapa"] == CONCLUIDO

    def test_host_estranho_bloqueado(self, servidor):
        # DNS rebinding: um site com DNS apontando para 127.0.0.1 manda o próprio Host.
        assert pedir(servidor, "/", token=None, host="atacante.example")[0] == 403
        assert pedir(servidor, "/api/estado", host="atacante.example")[0] == 403
        assert pedir(servidor, "/", token=None, host=f"localhost:{servidor.porta}")[0] == 200

    def test_estaticos_so_da_lista(self, servidor):
        status, headers, corpo = pedir(servidor, "/", token=None)
        assert status == 200 and b"MSscan" in corpo and headers["Content-Type"].startswith("text/html")
        for nome in ESTATICOS:
            assert pedir(servidor, f"/estaticos/{nome}", token=None)[0] == 200
        for caminho in ("/estaticos/../sessao.py", "/estaticos/sessao.py", "/estaticos/%2e%2e/servidor.py", "/etc/passwd"):
            assert pedir(servidor, caminho, token=None)[0] == 404

    def test_cabecalhos_de_seguranca(self, servidor):
        _, headers, _ = pedir(servidor, "/", token=None)
        csp = headers["Content-Security-Policy"]
        assert "default-src 'none'" in csp and "script-src 'self'" in csp
        assert "http" not in csp and "unsafe" not in csp
        assert headers["X-Content-Type-Options"] == "nosniff" and headers["Cache-Control"] == "no-store"

    def test_detalhe_e_acoes(self, servidor):
        status, _, corpo = pedir(servidor, "/api/componente/memoria")
        assert status == 200 and json.loads(corpo)["titulo"] == "Memória"
        assert pedir(servidor, "/api/componente/nada")[0] == 404
        assert pedir(servidor, "/api/diagnostico/iniciar", "POST")[0] == 409  # já concluído
        assert pedir(servidor, "/api/rede/tentar", "POST")[0] == 409
        assert pedir(servidor, "/api/pronto", "POST")[0] == 200 and servidor.sessao.pagina_pronta
        assert pedir(servidor, "/api/apagar_tudo", "POST")[0] == 404

    def test_relatorio_html(self, servidor):
        assert pedir(servidor, "/relatorio.html", token=None)[0] == 403
        assert pedir(servidor, "/relatorio.html?t=errado", token=None)[0] == 403
        status, headers, corpo = pedir(servidor, f"/relatorio.html?t={servidor.token}", token=None)
        assert status == 200 and corpo.decode() == servidor.sessao.html_relatorio()
        assert "script" not in headers["Content-Security-Policy"]

    def test_diagnostico_pela_api(self):
        sessao = Sessao(ExecutorReplay(R640), None, replay=True)
        srv = ServidorUI(sessao, porta=0)
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        try:
            sessao.iniciar()
            sessao.aguardar(30)
            assert pedir(srv, "/api/diagnostico/iniciar", "POST")[0] == 200
            sessao.aguardar(30)
            assert json.loads(pedir(srv, "/api/estado")[2])["etapa"] == CONCLUIDO
        finally:
            srv.shutdown()
            srv.server_close()


# --- Frontend (estático) ----------------------------------------------------------------


class TestFrontend:
    def test_nenhum_recurso_externo(self):
        for nome, texto in ESTATICOS_TEXTO.items():
            sem_comentarios = re.sub(r"/\*.*?\*/", "", texto, flags=re.DOTALL)
            assert not re.search(r"""(src|href)\s*=\s*["']?(https?:)?//""", sem_comentarios), nome
            assert not re.search(r"url\(\s*['\"]?(https?:)?//", sem_comentarios), nome
            assert "@import" not in sem_comentarios, nome
            assert not re.search(r"fetch\(\s*['\"]https?:", sem_comentarios), nome

    def test_sem_script_nem_estilo_inline(self):
        html = ESTATICOS_TEXTO["index.html"]
        assert re.findall(r"<script\b[^>]*>", html) == [
            '<script src="/estaticos/textos.js">',
            '<script src="/estaticos/icones.js">',
            '<script src="/estaticos/modelo3d.js">',
            '<script src="/estaticos/app.js">',
        ]
        assert "<style" not in html and " style=" not in html and " onclick=" not in html

    def test_cores_so_no_tema(self):
        cor = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(")
        for nome in ("app.css", "app.js", "index.html"):
            assert not cor.search(re.sub(r"/\*.*?\*/", "", ESTATICOS_TEXTO[nome], flags=re.DOTALL)), nome

    def test_paleta_do_tema(self):
        tema = ESTATICOS_TEXTO["tema.css"]
        paleta = {
            "preto-0": "#010409", "preto-1": "#0d1117", "preto-2": "#161b22", "borda": "#30363d",
            "borda-suave": "#21262d", "texto": "#e6edf3", "texto-sec": "#b1bac4", "azul-0": "#0a1a33",
            "azul-1": "#0d2d62", "azul-2": "#1760d2", "azul-link": "#58a6ff", "ok": "#3fb950",
            "alerta": "#d29922", "falha": "#f85149",
        }  # fmt: skip
        for nome, valor in paleta.items():
            assert f"--ms-{nome}: {valor};" in tema
        assert "linear-gradient(180deg, #010409, #0d1117)" in tema
        assert "linear-gradient(135deg, #0a1a33, #0d2d62 55%, #1760d2)" in tema
        assert "Primer" in tema and "MIT" in tema

    def test_licenca_dos_icones_e_sem_marca(self):
        icones = ESTATICOS_TEXTO["icones.js"]
        # Nova interface (1.0.0): ícones Lucide, licença ISC (aviso de copyright obrigatório no arquivo).
        assert "Lucide" in icones and "ISC License" in icones and "Permission to use, copy, modify" in icones
        # Fora do aviso de licença, nenhuma menção à marca.
        sem_licenca = re.sub(r'<details class="licenca">.*?</details>', "", ESTATICOS_TEXTO["index.html"], flags=re.DOTALL)
        for nome, texto in (("index.html", sem_licenca), ("app.js", ESTATICOS_TEXTO["app.js"]), ("app.css", ESTATICOS_TEXTO["app.css"])):
            assert "github" not in texto.lower(), nome
        assert "mark-github" not in icones

    def test_icones_usados_existem(self):
        definidos = set(re.findall(r'^  "([a-z-]+)":', ESTATICOS_TEXTO["icones.js"], flags=re.MULTILINE))
        usados = set(re.findall(r'data-icone="([a-z-]+)"', ESTATICOS_TEXTO["index.html"]))
        usados |= set(re.findall(r'icone\("([a-z-]+)"', ESTATICOS_TEXTO["app.js"]))
        usados |= set(re.findall(r'icone: "([a-z-]+)"', ESTATICOS_TEXTO["app.js"]))
        assert usados and usados <= definidos, usados - definidos

    def test_dados_da_api_nunca_via_innerhtml(self):
        assert "innerHTML" not in ESTATICOS_TEXTO["app.js"].replace("(nunca innerHTML)", "")
        assert "insertAdjacentHTML" not in ESTATICOS_TEXTO["app.js"] and "eval(" not in ESTATICOS_TEXTO["app.js"]

    def test_foco_visivel(self):
        css = ESTATICOS_TEXTO["app.css"]
        assert re.search(r":focus-visible[^{]*\{\s*outline: 2px solid var\(--ms-foco\)", css)
        assert "--ms-foco: #1f6feb;" in ESTATICOS_TEXTO["tema.css"]

    def test_setas_nas_abas_mantem_o_foco_na_aba(self):
        # Achado no QEMU (ISO v0.8.2): a seta trocava a tela e o foco ia para o <main>; as setas seguintes paravam.
        js = ESTATICOS_TEXTO["app.js"]
        assert 'aplicarRota(!(ativo && ativo.closest && ativo.closest("#abas")));' in js
        assert 'window.addEventListener("hashchange", () => aplicarRota(true));' not in js

    def test_atalhos_documentados(self):
        js = ESTATICOS_TEXTO["app.js"]
        for teclas in ('[["?"]', '[["/"]', '[["g", "i"]', '[["g", "d"]', '[["g", "h"]', '[["g", "r"]', '[["g", "c"]', '[["g", "a"]', '[["g", "t"]', '[["g", "s"]', '[["Ctrl", "+"]'):
            assert teclas in js
        assert '{ i: "inicio", d: "diagnostico", h: "discos", r: "relatorios", c: "chat", a: "atualizacoes", t: "terminal", s: "sobre" }' in js


class TestRegrasDeSegurancaDaInterface:
    """Regras que valem para todas as fases (CLAUDE.md, seção 12b)."""

    def test_nada_com_foco_inicial(self):
        html = ESTATICOS_TEXTO["index.html"]
        assert "autofocus" not in html
        assert ".autofocus" not in ESTATICOS_TEXTO["app.js"]

    def test_perguntas_opcionais_com_nao_como_padrao(self):
        html = ESTATICOS_TEXTO["index.html"]
        for grupo in ("ia-enviar", "atu-verificar"):
            radios = re.findall(rf'<input type="radio" name="{grupo}"[^>]*>', html)
            marcados = [r for r in radios if " checked" in r]
            assert len(marcados) == 1 and 'value="nao"' in marcados[0], grupo

    def test_checkboxes_de_atualizacao_desmarcados(self):
        tela = ESTATICOS_TEXTO["index.html"].split('id="tela-atualizacoes"')[1].split("</section>")[0]
        checkboxes = re.findall(r'<input type="checkbox"[^>]*>', tela)
        assert checkboxes and not any(" checked" in c for c in checkboxes)

    def test_confirmacao_digitada_e_sem_atualizar_tudo(self):
        html = ESTATICOS_TEXTO["index.html"]
        assert "ATUALIZAR" in html and 'id="atu-confirmacao"' in html
        texto = (html + ESTATICOS_TEXTO["app.js"]).lower()
        assert not re.search(r">\s*atualizar tudo\s*<", texto)
        assert "selecionar tudo" not in texto and "select all" not in texto

    def test_botao_de_perigo_nunca_e_padrao(self):
        html = ESTATICOS_TEXTO["index.html"]
        for botao in re.findall(r"<button[^>]*botao-perigo[^>]*>", html):
            assert 'type="button"' in botao  # nunca submit (Enter num campo não aciona)
        assert "<form" not in html  # sem formulário, Enter num campo não envia nada
        assert 'type="submit"' not in html


# --- Linha de comando ------------------------------------------------------------------


class TestLinhaDeComando:
    def test_flags_da_interface(self):
        args = criar_parser().parse_args(["--ui", "--replay", str(R640)])
        assert args.ui and args.ui_porta == 8765

    def test_replay_em_tempo_real_so_na_interface(self):
        assert criar_executor(criar_parser().parse_args(["--ui", "--replay", str(R640)])).tempo_real
        assert not criar_executor(criar_parser().parse_args(["--replay", str(R640)])).tempo_real

    def test_porta_ocupada_nao_quebra(self, capsys):
        from main import main

        sessao = Sessao(ExecutorReplay(R640), None, replay=True)
        ocupada = ServidorUI(sessao, porta=0)
        try:
            assert main(["--ui", "--replay", str(R640), "--ui-porta", str(ocupada.porta)]) == 2
        finally:
            ocupada.server_close()
        assert "não foi possível abrir a porta" in capsys.readouterr().out

    def test_status_permanece_o_do_nucleo(self):
        assert {s.value for s in Status} == {"OK", "ALERTA", "FALHA", "NAO_VERIFICADO", "NAO_APLICAVEL"}
        js = ESTATICOS_TEXTO["app.js"]
        for status in Status:
            assert f"  {status.value}: {{" in js


class TestIntegracaoComALancadorDaISO:
    """Flags usadas pelo msscan-iniciar: URL com token em arquivo privado e aviso de "página pronta"."""

    def test_url_em_arquivo_privado_e_aviso_de_pronto(self, tmp_path):
        import os
        import subprocess
        import sys
        import time

        url_arq, pronto = tmp_path / "url", tmp_path / "pronto"
        proc = subprocess.Popen(
            [sys.executable, "main.py", "--ui", "--replay", str(R640), "--ui-porta", "0",
             "--ui-arquivo-url", str(url_arq), "--ui-arquivo-pronto", str(pronto)],
            cwd=Path(__file__).parent.parent, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )  # fmt: skip
        try:
            for _ in range(100):
                if url_arq.exists() and url_arq.read_text().endswith("\n"):
                    break
                time.sleep(0.1)
            assert url_arq.stat().st_mode & 0o777 == 0o600
            url = url_arq.read_text().strip()
            assert re.fullmatch(r"http://127\.0\.0\.1:\d+/#t=[A-Za-z0-9_-]{40,}", url)
            base, token = url.split("/#t=")
            assert not pronto.exists()
            req = urllib.request.Request(f"{base}/api/pronto", method="POST", headers={"X-MSscan-Token": token})
            with urllib.request.urlopen(req, timeout=10) as resposta:
                assert resposta.status == 200
            assert pronto.exists()
        finally:
            proc.terminate()
            proc.wait(10)
        assert os.path.exists(url_arq)

    def test_aviso_de_pronto_com_erro_nao_derruba_a_api(self, servidor):
        def falha() -> None:
            raise OSError("somente leitura")

        servidor.ao_pronto = falha
        assert pedir(servidor, "/api/pronto", "POST")[0] == 200

    def test_arquivo_de_url_inacessivel(self, tmp_path, capsys):
        from main import main

        destino = tmp_path / "nao-existe" / "url"
        assert main(["--ui", "--replay", str(R640), "--ui-porta", "0", "--ui-arquivo-url", str(destino)]) == 2
        assert "não foi possível gravar" in capsys.readouterr().out
