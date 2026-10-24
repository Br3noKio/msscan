"""Refazer diagnóstico (CLAUDE.md 7b): tudo ou um componente, comparação antes × depois, somente leitura."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from main import etapa_refazer
from msscan.core.executor import ExecutorReplay
from msscan.deteccao.detectores import detectar
from msscan.interface.sessao import CONCLUIDO, Sessao
from msscan.modulos.refazer import preparar_releitura
from msscan.modulos.registro import registro_padrao
from msscan.relatorio.comparacao import (
    IGUAL,
    MELHOROU,
    PIOROU,
    comparar,
    formatar_comparacao,
    html_comparacao,
    tendencia,
)
from msscan.relatorio.completo import Gravacao, montar_relatorio
from msscan.relatorio.html import gerar_html
from msscan.relatorio.simbolos import CONSOLE
from tests.conftest import R640
from tests.test_interface import pedir
from tests.test_rede import ReplayAlterado

FONTES = ("ipmitool", "sdr", "type", "Power Supply")
SEM_IPMI = (1, "", "Could not open device at /dev/ipmi0")


def relatorio(*medidos: tuple[str, str], inicio: str = "2026-09-28T10:00:00") -> dict:
    return {"inicio": inicio, "veredito": "v", "medido": [
        {"componente": c, "titulo": c.capitalize(), "status": s, "mensagem": f"{c} {s}"} for c, s in medidos]}  # fmt: skip


class TestComparacaoPura:
    @pytest.mark.parametrize(("antes", "depois", "esperado"), [
        ("ALERTA", "OK", MELHOROU), ("FALHA", "ALERTA", MELHOROU), ("NAO_VERIFICADO", "ALERTA", MELHOROU),
        ("OK", "ALERTA", PIOROU), ("ALERTA", "NAO_VERIFICADO", PIOROU), ("NAO_VERIFICADO", "FALHA", PIOROU),
        ("OK", "OK", IGUAL), ("OK", "NAO_APLICAVEL", IGUAL), (None, "OK", MELHOROU), ("OK", None, PIOROU),
    ])  # fmt: skip
    def test_tendencia(self, antes, depois, esperado):
        assert tendencia(antes, depois) == esperado

    def test_so_os_refeitos(self):
        antes = relatorio(("memoria", "ALERTA"), ("fontes", "ALERTA"))
        depois = relatorio(("memoria", "OK"), ("fontes", "ALERTA"), inicio="2026-09-28T10:30:00")
        comp = comparar(antes, depois, ["memoria"], "/dados/relatorios/r640_antes")
        assert [c["componente"] for c in comp["componentes"]] == ["memoria"]
        assert comp["resumo"] == {MELHOROU: 1, PIOROU: 0, IGUAL: 0} and comp["escopo"] == "componentes"
        assert comp["anterior"]["pasta"] == "/dados/relatorios/r640_antes"

    def test_tudo(self):
        comp = comparar(relatorio(("a", "OK"), ("b", "FALHA")), relatorio(("a", "ALERTA"), ("b", "FALHA")), None)
        assert comp["escopo"] == "tudo" and comp["resumo"] == {MELHOROU: 0, PIOROU: 1, IGUAL: 1}

    def test_texto_so_com_caracteres_do_console(self):
        comp = comparar(relatorio(("memoria", "ALERTA")), relatorio(("memoria", "OK")), ["memoria"])
        texto = formatar_comparacao(comp)
        assert "Comparação antes × depois (refeito só: Memoria)" in texto and "Alerta -> OK" in texto
        assert "MELHOROU" in texto and "1 melhorou, 0 piorou, 0 igual" in texto
        assert "→" not in texto and "✓" not in texto  # a fonte do tty1 não tem esses glyphs
        assert formatar_comparacao(None) == "" and html_comparacao(None) == ""

    def test_html_tendencia_com_simbolo_texto_e_cor(self):
        comp = comparar(relatorio(("memoria", "ALERTA")), relatorio(("memoria", "OK")), ["memoria"])
        html = gerar_html({**relatorio(("memoria", "OK")), "comparacao": comp})
        assert "Comparação antes × depois" in html and "▲ melhorou" in html
        comp["componentes"][0]["mensagem_depois"] = "<script>x</script>"
        assert "<script>x" not in html_comparacao(comp)


class TestReleitura:
    def test_descarta_leituras_guardadas(self):
        ctx = detectar(ExecutorReplay(R640))
        ctx.cache.update(sel=["velho"], redfish={"x": 1}, redfish_raiz={"r": 1})
        preparar_releitura(ctx, ("memoria",))
        assert "sel" not in ctx.cache and "redfish" not in ctx.cache
        assert ctx.cache["redfish_raiz"] == {"r": 1}  # detecção não muda

    def test_sensores_do_kernel_relidos_e_nunca_velhos(self):
        executor = ReplayAlterado()
        ctx = detectar(executor)
        assert ctx.sensores_kernel
        executor.troca_comandos[("sensors", "-j")] = (1, "", "No sensors found!")
        preparar_releitura(ctx, ("memoria",))
        assert ctx.sensores_kernel  # memória não usa lm-sensors: nada relido
        preparar_releitura(ctx, ("ventoinhas",))
        assert ctx.sensores_kernel == [] and ctx.erros_deteccao["sensores_kernel"] == "No sensors found!"


def sessao_concluida(executor) -> Sessao:
    sessao = Sessao(executor, None, replay=True)
    sessao.iniciar()
    sessao.aguardar(30)
    assert sessao.iniciar_diagnostico()
    sessao.aguardar(30)
    assert sessao.etapa == CONCLUIDO
    return sessao


class TestSessao:
    def test_um_componente_melhorou(self):
        executor = ReplayAlterado(comandos={FONTES: SEM_IPMI})
        sessao = sessao_concluida(executor)
        assert sessao.resultados["fontes"].status.value == "NAO_VERIFICADO"
        memoria_antes = sessao.resultados["memoria"]
        assert sessao.estado()["relatorio"]["estado_geral"]["nao_verificados"] == 1  # indicador da aba Diagnóstico
        del executor.troca_comandos[FONTES]  # técnico reencaixou o cabo / o BMC voltou a responder
        assert sessao.refazer_diagnostico("fontes")
        sessao.aguardar(30)
        comp = sessao.estado()["diagnostico"]["comparacao"]
        assert comp["refeitos"] == ["fontes"] and comp["componentes"][0]["tendencia"] == MELHOROU
        assert sessao.resultados["fontes"].status.value == "ALERTA"  # PS1 sem AC: redundância perdida
        assert sessao.estado()["relatorio"]["estado_geral"]["nao_verificados"] == 0  # o indicador muda na hora (02/10/2026)
        assert sessao.resultados["memoria"] is memoria_antes  # só o componente escolhido foi refeito
        assert "Comparação antes × depois" in sessao.texto_relatorio

    def test_tudo_e_invalida_ia_e_atualizacoes(self):
        executor = ReplayAlterado()
        sessao = sessao_concluida(executor)
        sessao.ia = {"situacao": "concluida", "mensagem": ""}
        sessao.atualizacoes = {"situacao": "concluidas", "mensagem": ""}
        executor.troca_comandos[FONTES] = SEM_IPMI
        assert sessao.refazer_diagnostico(None)
        sessao.aguardar(30)
        comp = sessao.relatorio["comparacao"]
        assert comp["escopo"] == "tudo" and len(comp["componentes"]) == len(registro_padrao().componentes())
        fontes = next(c for c in comp["componentes"] if c["componente"] == "fontes")
        assert fontes["tendencia"] == PIOROU and comp["resumo"][PIOROU] >= 1
        assert sessao.ia["situacao"] == "nao_solicitada" and sessao.atualizacoes["situacao"] == "nao_solicitadas"

    def test_recusa_fora_de_hora_e_componente_inexistente(self):
        sessao = Sessao(ExecutorReplay(R640), None, replay=True)
        assert not sessao.refazer_diagnostico(None)  # nada diagnosticado ainda
        sessao = sessao_concluida(ExecutorReplay(R640))
        assert not sessao.refazer_diagnostico("disco_voador")

    def test_pasta_nova_e_anterior_guardada(self, tmp_path):
        sessao = Sessao(ExecutorReplay(R640), str(tmp_path), replay=True)
        sessao.iniciar()
        sessao.aguardar(30)
        sessao.iniciar_diagnostico()
        sessao.aguardar(30)
        primeira = sessao.gravacao["pasta"]
        import time

        time.sleep(1.1)  # nome da pasta tem segundos
        assert sessao.refazer_diagnostico("memoria")
        sessao.aguardar(30)
        assert sessao.gravacao["pasta"] != primeira and Path(primeira, "relatorio.json").exists()
        gravado = json.loads(Path(sessao.gravacao["pasta"], "relatorio.json").read_text(encoding="utf-8"))
        assert gravado["comparacao"]["anterior"]["pasta"] == primeira
        assert "Comparação antes × depois" in Path(sessao.gravacao["pasta"], "relatorio.html").read_text(encoding="utf-8")

    def test_pela_api(self):
        from msscan.interface.servidor import ServidorUI
        import threading
        import urllib.request

        sessao = sessao_concluida(ExecutorReplay(R640))
        srv = ServidorUI(sessao, porta=0)
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        try:
            def post(corpo: bytes):
                req = urllib.request.Request(f"http://127.0.0.1:{srv.porta}/api/diagnostico/refazer", data=corpo,
                                             method="POST", headers={"X-MSscan-Token": srv.token})  # fmt: skip
                try:
                    with urllib.request.urlopen(req, timeout=10) as r:
                        return r.status
                except urllib.error.HTTPError as erro:
                    return erro.code

            assert post(b'{"componente": 5}') == 400
            assert post(b'{"componente": "disco_voador"}') == 409
            assert post(b'{"componente": "memoria"}') == 200
            sessao.aguardar(30)
            assert pedir(srv, "/api/estado")[0] == 200
            assert json.loads(pedir(srv, "/api/estado")[2])["diagnostico"]["comparacao"]["refeitos"] == ["memoria"]
        finally:
            srv.shutdown()
            srv.server_close()


class TestModoTexto:
    def preparar(self, executor):
        ctx = detectar(executor)
        registro = registro_padrao()
        resultados = registro.diagnosticar(ctx)
        rel = montar_relatorio(ctx, resultados, None, executor, __import__("datetime").datetime.now(),
                               __import__("datetime").datetime.now(), registro.titulo)  # fmt: skip
        args = argparse.Namespace(replay=R640)
        return ctx, registro, resultados, rel, args

    def test_padrao_nao(self, capsys):
        executor = ExecutorReplay(R640)
        ctx, registro, resultados, rel, args = self.preparar(executor)
        etapa_refazer(args, executor, ctx, registro, None, resultados, rel, "", None, Gravacao(None, False, ""),
                      CONSOLE, entrada=lambda _: "")  # fmt: skip
        assert "Refazendo" not in capsys.readouterr().out

    def test_um_componente(self, capsys):
        executor = ReplayAlterado(comandos={FONTES: SEM_IPMI})
        ctx, registro, resultados, rel, args = self.preparar(executor)
        del executor.troca_comandos[FONTES]
        indice = [c.nome for c in registro.componentes()].index("fontes") + 1
        respostas = iter(["s", str(indice), "n"])
        etapa_refazer(args, executor, ctx, registro, None, resultados, rel, "", None, Gravacao(None, False, ""),
                      CONSOLE, entrada=lambda _: next(respostas))  # fmt: skip
        saida = capsys.readouterr().out
        assert "Refazendo o diagnóstico (somente leitura)" in saida
        assert "refeito só: Fontes de alimentação" in saida and "MELHOROU" in saida
        assert "não se aplicam ao diagnóstico refeito" in saida

    def test_escolha_invalida_nao_refaz(self, capsys):
        executor = ExecutorReplay(R640)
        ctx, registro, resultados, rel, args = self.preparar(executor)
        respostas = iter(["s", "99", ""])
        etapa_refazer(args, executor, ctx, registro, None, resultados, rel, "", None, Gravacao(None, False, ""),
                      CONSOLE, entrada=lambda _: next(respostas))  # fmt: skip
        saida = capsys.readouterr().out
        assert "Nada refeito." in saida and "Refazendo" not in saida
