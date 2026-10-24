"""Chat com a IA sobre o diagnóstico (CLAUDE.md 10e): primeira mensagem, sanitização das perguntas, evidências
marcadas, só ações do catálogo, principal → reserva, espera da rede, relatório e modo texto.

Nenhum teste chama API real: as respostas estão em ``tests/fixtures/ia/chat_*.json`` (formato documentado de cada API).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from msscan.core.executor import RespostaIA
from msscan.ia.analise import NAO_CONFIRMADA, solicitar_analise
from msscan.ia.chat import (
    MAX_PERGUNTA,
    MAX_TURNOS_ENVIADOS,
    SUGESTOES,
    Conversa,
    primeira_mensagem,
    texto_com_marcas,
    validar_resposta,
)
from msscan.ia.provedores import montar_conversa
from msscan.ia.sanitizar import preparar_envio
from msscan.relatorio.chat import formatar_chat, formatar_mensagem, formatar_pareamentos, html_chat, html_pareamentos
from msscan.relatorio.html import gerar_html
from msscan.relatorio.ia import secao_ia
from tests.conftest import ExecutorFalso, com_textos
from tests.test_ia import CHAVE_ANTHROPIC, CHAVE_GEMINI, CHAVE_GROQ, relatorio_r640, resposta, tres_provedores
from tests.test_rede_continua import MonitorFalso

CHAVES = (CHAVE_GEMINI, CHAVE_GROQ, CHAVE_ANTHROPIC)

JS_APP = com_textos((Path(__file__).parent.parent / "msscan/interface/estaticos/app.js").read_text(encoding="utf-8"))


def relatorio_com_analise(tmp_path) -> dict:
    """Relatório do R640 com a análise da fixture ``openai_ok`` (3 problemas: real, ação do catálogo, inventado)."""
    relatorio = relatorio_r640()
    dados, removidos = preparar_envio(relatorio)
    executor = ExecutorFalso()
    executor.respostas_ia.append(resposta("openai_ok"))
    resultado = solicitar_analise(executor, [tres_provedores(tmp_path)[1]], dados)
    assert resultado.ok
    relatorio["interpretacao_ia"] = secao_ia(resultado.analise, removidos, resultado.tentativas)
    return relatorio


# --- Primeira mensagem ---------------------------------------------------------------------------


class TestPrimeiraMensagem:
    def test_diagnostico_e_solucoes_em_ordem_de_prioridade(self, tmp_path):
        mensagem = Conversa(relatorio_com_analise(tmp_path)).mensagens[0]
        assert mensagem["papel"] == "ia" and mensagem["origem"] == "analise"
        texto = mensagem["texto"]
        assert texto.startswith("Diagnóstico: Servidor operando com uma fonte sem energia")
        # prioridade imediata (disco inventado) → alta (fontes) → baixa (SEL)
        assert texto.index("Disco com setores realocados") < texto.index("Redundância de energia perdida") < texto.index("Eventos antigos")
        assert "provável troca de peça" in texto and "exige ir até o servidor" in texto and f"({NAO_CONFIRMADA})" in texto
        assert "Ficou sem verificação: RAID de hardware não verificado." in texto and "não executo nada" in texto
        assert [e["id"] for e in mensagem["evidencias"]] == ["E1", "E2", "E3"]
        assert [e["confirmada"] for e in mensagem["evidencias"]] == [False, True, True]
        # só a ação do catálogo; a inexistente já foi descartada na análise
        assert mensagem["acoes"] == [{"id": "bmc.sel_exportar_limpar", "titulo": mensagem["acoes"][0]["titulo"]}]

    def test_sem_problemas(self):
        mensagem = primeira_mensagem({"resumo": "Tudo certo.", "problemas": []})
        assert "Não encontrei problemas" in mensagem["texto"] and mensagem["evidencias"] == [] and mensagem["acoes"] == []

    def test_campos_fora_do_padrao_nao_quebram(self):
        mensagem = primeira_mensagem({"problemas": [{"plano": {"prioridade": "urgentissima"}, "severidade": 7}, "lixo"]})
        assert "1. Problema (prioridade urgentissima; severidade 7)" in mensagem["texto"]


# --- Validação da resposta -----------------------------------------------------------------------


class TestValidarResposta:
    TEXTOS = {"fontes": "power supply ac lost", "servidor": "dell poweredge r640"}

    def test_evidencias_marcadas_e_acoes_so_do_catalogo(self):
        bruta = json.loads(resposta("chat_gemini_ok").corpo["candidates"][0]["content"]["parts"][0]["text"])
        validada = validar_resposta(bruta, self.TEXTOS)
        assert {e["id"]: e["confirmada"] for e in validada["evidencias"]} == {"E1": True, "E2": False, "E3": False}
        assert validada["acoes"] == [{"id": "bmc.sel_exportar_limpar", "titulo": validada["acoes"][0]["titulo"]}]
        assert any("disco.formatar_tudo" in a and "não existe no catálogo" in a for a in validada["avisos"])
        assert any(NAO_CONFIRMADA in a for a in validada["avisos"])

    def test_acao_repetida_e_vazia(self):
        validada = validar_resposta({"resposta": "ok", "acoes_catalogo": ["bmc.sel_exportar_limpar", " bmc.sel_exportar_limpar ", ""]}, {})
        assert len(validada["acoes"]) == 1 and validada["avisos"] == []

    @pytest.mark.parametrize("bruta", [None, [], {"resposta": ""}, {"resposta": 3}, {"evidencias": []}])
    def test_formato_invalido(self, bruta):
        with pytest.raises(ValueError):
            validar_resposta(bruta, {})

    def test_trecho_curto_nao_confirma(self):
        validada = validar_resposta({"resposta": "[E1]", "evidencias": [{"id": "[E1]", "componente": "fontes", "trecho": "ac"}]}, self.TEXTOS)
        assert validada["evidencias"] == [{"id": "E1", "componente": "fontes", "trecho": "ac", "confirmada": False}]

    def test_texto_com_marcas(self):
        mensagem = {"texto": "A [E1] e B [E2] e C [E9]", "evidencias": [{"id": "E1", "confirmada": True}, {"id": "E2", "confirmada": False}]}
        assert texto_com_marcas(mensagem) == "A [E1] e B [E2](NÃO CONFIRMADA) e C [E9](NÃO CONFIRMADA)"


# --- Perguntas do técnico: sanitização ----------------------------------------------------------


class TestPergunta:
    def test_contexto_sem_identificadores(self, tmp_path):
        conversa = Conversa(relatorio_com_analise(tmp_path), ["srv-cliente"])
        contexto = conversa.contexto()
        assert not re.search(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", contexto, re.I)
        assert "srv-cliente" not in contexto and '"componentes"' in contexto and '"problemas"' in contexto

    def test_identificadores_colados_saem_com_aviso(self, tmp_path):
        conversa = Conversa(relatorio_com_analise(tmp_path), ["srv-cliente"])
        limpo, avisos = conversa.preparar_pergunta(
            "O srv-cliente (192.168.77.202, MAC 02:00:5e:10:00:05, db01.corp) tem serial WD-WX12345678?\r\nE agora?")
        for segredo in ("srv-cliente", "192.168.77.202", "02:00:5e:10:00:05", "db01.corp"):
            assert segredo not in limpo, segredo
        assert "[HOSTNAME-" in limpo and "[IP-" in limpo and "[MAC-" in limpo and "\r" not in limpo
        assert len(avisos) == 1 and "Removido antes do envio" in avisos[0]
        assert all(c in avisos[0] for c in ("hostname", "IP", "MAC"))

    def test_pergunta_limpa_sem_aviso(self, tmp_path):
        conversa = Conversa(relatorio_com_analise(tmp_path))
        assert conversa.preparar_pergunta("  O que resolvo primeiro?  ") == ("O que resolvo primeiro?", [])

    def test_pergunta_longa_cortada(self, tmp_path):
        conversa = Conversa(relatorio_com_analise(tmp_path))
        limpo, avisos = conversa.preparar_pergunta("a" * (MAX_PERGUNTA + 50))
        assert len(limpo) == MAX_PERGUNTA and avisos == [f"Mensagem cortada em {MAX_PERGUNTA} caracteres."]


# --- Envio aos provedores ------------------------------------------------------------------------


class TestProvedores:
    @pytest.mark.parametrize(("indice", "fixture", "modelo"), [
        (0, "chat_gemini_ok", "gemini-3.8-flash"), (1, "chat_openai_ok", "llama-3.3-70b-versatile"),
        (2, "chat_anthropic_ok", "claude-sonnet-5"),
    ])  # fmt: skip
    def test_resposta_de_cada_provedor(self, tmp_path, indice, fixture, modelo):
        provedor = tres_provedores(tmp_path)[indice]
        conversa = Conversa(relatorio_com_analise(tmp_path))
        executor = ExecutorFalso()
        executor.respostas_ia.append(resposta(fixture))
        resultado = conversa.conversar(executor, [provedor], "O que resolvo primeiro?")
        assert resultado.ok and executor.ia_urls == [provedor.url]
        ia = conversa.mensagens[-1]
        assert ia is resultado.mensagem_ia and ia["modelo"] == modelo and ia["origem"] == "chat"
        assert {e["id"]: e["confirmada"] for e in ia["evidencias"]} == {"E1": True, "E2": False, "E3": False}
        assert [a["id"] for a in ia["acoes"]] == ["bmc.sel_exportar_limpar"]
        assert [m["papel"] for m in conversa.mensagens] == ["ia", "tecnico", "ia"]
        texto = json.dumps(conversa.para_relatorio(), ensure_ascii=False) + json.dumps(executor.historico_dict())
        for chave in CHAVES:
            assert chave not in texto

    def test_papeis_alternados_em_cada_api(self, tmp_path):
        gemini, groq, anthropic = tres_provedores(tmp_path)
        mensagens = [("usuario", "contexto"), ("ia", "diagnóstico"), ("usuario", "pergunta")]
        _, corpo = montar_conversa(gemini, "instr", mensagens, {"type": "object"})
        assert [c["role"] for c in corpo["contents"]] == ["user", "model", "user"]
        _, corpo = montar_conversa(groq, "instr", mensagens, {"type": "object"})
        assert [m["role"] for m in corpo["messages"]] == ["system", "user", "assistant", "user"]
        _, corpo = montar_conversa(anthropic, "instr", mensagens, {"type": "object"})
        assert [m["role"] for m in corpo["messages"]] == ["user", "assistant", "user"] and corpo["system"] == "instr"

    def test_principal_com_limite_vai_para_a_reserva(self, tmp_path):
        conversa = Conversa(relatorio_com_analise(tmp_path))
        executor = ExecutorFalso()
        executor.respostas_ia += [resposta("gemini_429"), resposta("chat_openai_ok")]
        resultado = conversa.conversar(executor, tres_provedores(tmp_path)[:2], "Preciso trocar alguma peça?")
        assert resultado.ok and [t["resultado"] for t in resultado.tentativas] == ["falhou", "ok"]
        assert resultado.mensagem_ia["avisos"][0].startswith("Resposta da IA reserva (gemini: Limite de uso atingido)")

    def test_esquema_recusado_tenta_sem_ele(self, tmp_path):
        conversa = Conversa(relatorio_com_analise(tmp_path))
        executor = ExecutorFalso()
        executor.respostas_ia += [resposta("gemini_esquema_recusado"), resposta("chat_gemini_ok")]
        assert conversa.conversar(executor, [tres_provedores(tmp_path)[0]], "oi").ok
        assert "responseJsonSchema" in executor.ia_enviados[0][1]["generationConfig"]
        assert "responseJsonSchema" not in executor.ia_enviados[1][1]["generationConfig"]

    def test_resposta_fora_do_formato_passa_para_a_reserva(self, tmp_path):
        conversa = Conversa(relatorio_com_analise(tmp_path))
        ruim = resposta("chat_gemini_ok")
        ruim.corpo["candidates"][0]["content"]["parts"][0]["text"] = "não é JSON"
        executor = ExecutorFalso()
        executor.respostas_ia += [ruim, resposta("chat_anthropic_ok")]
        resultado = conversa.conversar(executor, [tres_provedores(tmp_path)[0], tres_provedores(tmp_path)[2]], "oi")
        assert resultado.ok and resultado.tentativas[0]["detalhe"].startswith("resposta inválida")

    def test_nenhuma_ia_responde(self, tmp_path):
        conversa = Conversa(relatorio_com_analise(tmp_path))
        resultado = conversa.conversar(ExecutorFalso(), tres_provedores(tmp_path), "oi")
        assert not resultado.ok and resultado.mensagem.startswith("nenhuma IA respondeu")
        assert conversa.mensagens[-1]["papel"] == "sistema" and conversa.mensagens[-2]["texto"] == "oi"
        assert not conversa.conversar(ExecutorFalso(), [], "oi").ok

    def test_mensagem_vazia_nao_envia(self, tmp_path):
        conversa = Conversa(relatorio_com_analise(tmp_path))
        executor = ExecutorFalso()
        assert conversa.conversar(executor, tres_provedores(tmp_path), "   ").mensagem == "mensagem vazia"
        assert executor.ia_enviados == [] and len(conversa.mensagens) == 1


class TestHistorico:
    def test_envio_comeca_pelo_contexto_e_junta_pergunta_sem_resposta(self, tmp_path):
        conversa = Conversa(relatorio_com_analise(tmp_path))
        conversa.mensagens += [{"papel": "tecnico", "texto": "p1"}, {"papel": "sistema", "texto": "Sem resposta"},
                               {"papel": "tecnico", "texto": "p2"}, {"papel": "ia", "texto": "r2"}]  # fmt: skip
        envio = conversa.para_envio("p3")
        assert envio[0][0] == "usuario" and envio[0][1].startswith("Dados do diagnóstico")
        assert envio[1] == ("ia", conversa.mensagens[0]["texto"])
        assert envio[2:] == [("usuario", "p1\n\np2"), ("ia", "r2"), ("usuario", "p3")]

    def test_so_os_turnos_recentes(self, tmp_path):
        conversa = Conversa(relatorio_com_analise(tmp_path))
        for n in range(MAX_TURNOS_ENVIADOS + 5):
            conversa.mensagens += [{"papel": "tecnico", "texto": f"p{n}"}, {"papel": "ia", "texto": f"r{n}"}]
        envio = conversa.para_envio("nova")
        papeis = [p for p, _ in envio]
        assert len(envio) == 2 + 2 * MAX_TURNOS_ENVIADOS + 1 and envio[2] == ("usuario", "p5")
        assert all(a != b for a, b in zip(papeis, papeis[1:]))  # sempre alternados


class TestRede:
    def test_sem_rede_espera_e_reenvia(self, tmp_path):
        conversa = Conversa(relatorio_com_analise(tmp_path))
        executor = ExecutorFalso()
        executor.respostas_ia += [RespostaIA(None, erro_tipo="conexao", erro="x"), resposta("chat_gemini_ok")]
        monitor, avisos = MonitorFalso(), []
        resultado = conversa.conversar(executor, [tres_provedores(tmp_path)[0]], "oi", monitor, avisos.append)
        assert resultado.ok and monitor.esperas == 1 and "Rede de volta: enviando a mensagem..." in avisos
        assert [m["papel"] for m in conversa.mensagens] == ["ia", "tecnico", "ia"]

    def test_erro_do_provedor_nao_espera_rede(self, tmp_path):
        conversa = Conversa(relatorio_com_analise(tmp_path))
        executor = ExecutorFalso()
        executor.respostas_ia += [resposta("openai_500")]
        monitor = MonitorFalso()
        assert not conversa.conversar(executor, [tres_provedores(tmp_path)[1]], "oi", monitor).ok
        assert monitor.esperas == 0 and len(executor.ia_enviados) == 1


# --- Relatório -----------------------------------------------------------------------------------


def conversa_respondida(tmp_path) -> Conversa:
    conversa = Conversa(relatorio_com_analise(tmp_path))
    executor = ExecutorFalso()
    executor.respostas_ia.append(resposta("chat_gemini_ok"))
    assert conversa.conversar(executor, [tres_provedores(tmp_path)[0]], "O que resolvo primeiro? <script>alert(1)</script>").ok
    return conversa


class TestRelatorio:
    def test_sem_conversa_nao_ha_secao(self, tmp_path):
        chat = Conversa(relatorio_com_analise(tmp_path)).para_relatorio()
        assert formatar_chat(chat) == "" and html_chat(chat) == "" and formatar_chat(None) == ""

    def test_texto(self, tmp_path):
        texto = formatar_chat(conversa_respondida(tmp_path).para_relatorio())
        assert "Conversa com a IA\n" in texto and "] Técnico:" in texto and "IA (gemini, gemini-3.8-flash):" in texto
        resposta_ia = texto[texto.index("IA (gemini"):]  # a primeira mensagem (da análise) tem outra numeração
        assert "[E2](NÃO CONFIRMADA)" in resposta_ia and "[E1](NÃO" not in resposta_ia
        assert "Ação do catálogo recomendada: bmc.sel_exportar_limpar" in texto and "disco.formatar_tudo" in texto
        assert all(len(linha) <= 78 for linha in texto.splitlines())

    def test_html_escapado_e_marcado(self, tmp_path):
        html = html_chat(conversa_respondida(tmp_path).para_relatorio())
        assert "<script>" not in html and "&lt;script&gt;" in html
        assert '<span class="ev-ok">[E1]</span>' in html and '<span class="ev-falha">[E2 — não confirmada]</span>' in html

    def test_relatorio_html_completo_com_chat_e_pareamento(self, tmp_path):
        relatorio = relatorio_com_analise(tmp_path)
        relatorio["chat"] = conversa_respondida(tmp_path).para_relatorio()
        relatorio["pareamentos_ia"] = [{"ia": "gemini", "ia_nome": "Google Gemini", "inicio": "2026-09-29T10:00:00",
                                        "fim": "2026-09-29T10:01:00", "resultado": "aceita", "ip_celular": "192.168.0.50",
                                        "tentativas_invalidas": 2}]  # fmt: skip
        html = gerar_html(relatorio)
        assert "Conversa com a IA<" in html and "Chave da IA enviada pelo celular" in html and "tentativas inválidas: 2" in html

    def test_formatar_mensagem_largura(self):
        linhas = formatar_mensagem({"texto": "palavra " * 40, "avisos": ["aviso " * 30]}, 40)
        assert all(len(linha) <= 40 for linha in linhas) and linhas[0].startswith("  palavra")

    def test_evidencia_citada_que_nao_veio_nos_dados(self):
        mensagem = {"texto": "Veja [E3].", "evidencias": [{"id": "E3", "componente": "", "trecho": "", "confirmada": False}]}
        esperado = "[E3] citada pela IA, mas não existe nos dados — NÃO CONFIRMADA"
        assert any(esperado in linha for linha in formatar_mensagem(mensagem))
        assert "?: " not in html_chat({"mensagens": [mensagem, mensagem]}) and "citada pela IA" in html_chat({"mensagens": [mensagem, mensagem]})
        assert "citada pela IA, mas não consta dos dados" in JS_APP

    def test_pareamentos(self):
        registro = [{"ia": "groq", "ia_nome": "", "inicio": "i", "fim": "f", "resultado": "expirada", "ip_celular": "",
                     "tentativas_invalidas": 0}]  # fmt: skip
        assert "groq — expirou sem envio (5 minutos)" in formatar_pareamentos(registro)
        assert "Celular:" not in formatar_pareamentos(registro)
        assert formatar_pareamentos([]) == "" and html_pareamentos(None) == ""
        assert "a chave nunca vai para o relatório" in html_pareamentos(registro)


# --- Modo texto ----------------------------------------------------------------------------------


class TestModoTexto:
    def test_padrao_nao_nao_conversa(self, tmp_path):
        from main import etapa_chat

        relatorio = relatorio_com_analise(tmp_path)
        executor = ExecutorFalso()
        assert etapa_chat(executor, relatorio, tres_provedores(tmp_path), entrada=lambda _: "") == ""
        assert executor.ia_enviados == [] and len(relatorio["chat"]["mensagens"]) == 1

    def test_sugestao_por_numero_e_enter_encerra(self, tmp_path, capsys):
        from main import etapa_chat

        relatorio = relatorio_com_analise(tmp_path)
        executor = ExecutorFalso()
        executor.respostas_ia.append(resposta("chat_gemini_ok"))
        respostas = iter(["s", "1", "O srv-cliente 192.168.77.202 importa?", ""])
        secao = etapa_chat(executor, relatorio, tres_provedores(tmp_path)[:1], entrada=lambda _: next(respostas))
        saida = capsys.readouterr().out
        assert "Diagnóstico: Servidor operando" in saida and "Sugestões: 1) " + SUGESTOES[0] in saida
        assert "IA (gemini):" in saida and "[E2](NÃO CONFIRMADA)" in saida and "NÃO CONFIRMADA pelos dados" in saida
        assert "AVISO: Removido antes do envio" in saida and "Sem resposta da IA. Sem conexão" in saida
        tecnicas = [m["texto"] for m in relatorio["chat"]["mensagens"] if m["papel"] == "tecnico"]
        assert tecnicas[0] == SUGESTOES[0] and "192.168.77.202" not in tecnicas[1]
        assert "Conversa com a IA\n" in secao

    def test_chat_salvo_no_relatorio(self, tmp_path):
        from main import etapa_ia
        from tests.test_ia import preparar_main

        relatorio, gravacao, args, local, rede = preparar_main(tmp_path)
        executor = ExecutorFalso()
        executor.respostas_ia += [resposta("gemini_ok"), resposta("chat_gemini_ok")]
        respostas = iter(["s", "n", "s", "O que resolvo primeiro?", ""])
        etapa_ia(args, executor, relatorio, "t", local, rede, gravacao, entrada=lambda _: next(respostas))
        gravado = json.loads((gravacao.pasta / "relatorio.json").read_text())
        assert [m["papel"] for m in gravado["chat"]["mensagens"]] == ["ia", "tecnico", "ia"]
        todos = "".join(p.read_text() for p in gravacao.pasta.iterdir())
        assert "Conversa com a IA\n" in (gravacao.pasta / "relatorio.txt").read_text()
        assert "Conversa com a IA<" in (gravacao.pasta / "relatorio.html").read_text()
        for chave in CHAVES:
            assert chave not in todos
