"""Velocidade da IA (CLAUDE.md 15.1, pedido de 30/09/2026): streaming, chat com uma nova tentativa, contexto compacto.

Eventos de streaming no formato documentado de cada API (Anthropic, Gemini ``alt=sse``, compatível com OpenAI).
"""

from __future__ import annotations

import json

import pytest

from msscan.core.executor import RespostaIA, validar_url_ia
from msscan.ia import fluxo
from msscan.ia.analise import solicitar_analise
from msscan.ia.chat import Conversa, resumo_compacto
from msscan.ia.provedores import CHAT, pedir_json, url_fluxo
from tests.conftest import ExecutorFalso
from tests.test_chat import relatorio_com_analise
from tests.test_ia import resposta, tres_provedores
from tests.test_ia_config import dois_provedores

JSON_CHAT = json.dumps({"resposta": "Troque a \"fonte\" 1 [E1].", "evidencias": [{"id": "E1", "componente": "fontes", "trecho": "PS1"}],
                        "acoes_catalogo": []}, ensure_ascii=False)  # fmt: skip


def pedacos(texto: str, n: int = 7) -> list[str]:
    return [texto[i:i + n] for i in range(0, len(texto), n)]


def eventos(tipo: str, texto: str) -> list[dict]:
    if tipo == "anthropic":
        return ([{"type": "message_start", "message": {"model": "claude-x", "usage": {"input_tokens": 5}}}]
                + [{"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": p}} for p in pedacos(texto)]
                + [{"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 9}}])  # fmt: skip
    if tipo == "gemini":
        return ([{"candidates": [{"content": {"parts": [{"text": "pensando", "thought": True}]}}]}]
                + [{"candidates": [{"content": {"parts": [{"text": p}], "role": "model"}}]} for p in pedacos(texto)]
                + [{"candidates": [{"content": {"parts": [{"text": ""}]}, "finishReason": "STOP"}], "modelVersion": "gemini-x"}])  # fmt: skip
    return ([{"model": "gpt-x", "choices": [{"delta": {"content": p}}]} for p in pedacos(texto)]
            + [{"model": "gpt-x", "choices": [{"delta": {}, "finish_reason": "stop"}]}])  # fmt: skip


class ExecutorFluxo(ExecutorFalso):
    """Entrega eventos gravados um a um (como o ``ExecutorReal`` faz com o SSE)."""

    def __init__(self, lotes):
        super().__init__()
        self.lotes = list(lotes)
        self.corpos = []

    def _post_ia_fluxo(self, url, cabecalhos, corpo, timeout, ao_evento):
        self.ia_urls.append(url)
        self.corpos.append(corpo)
        lote = self.lotes.pop(0)
        if isinstance(lote, RespostaIA):
            return lote
        for evento in lote:
            ao_evento(evento)
        return RespostaIA(200, {"_eventos": lote})


class TestFluxo:
    @pytest.mark.parametrize("tipo", ["anthropic", "gemini", "openai"])
    def test_juntar_da_o_mesmo_texto_da_resposta_inteira(self, tipo):
        corpo = fluxo.juntar(tipo, eventos(tipo, JSON_CHAT))
        from msscan.ia.provedores import Provedor, interpretar

        interp = interpretar(Provedor("x", tipo, "m", "https://x/chat/completions"), RespostaIA(200, corpo))
        assert interp.ok and json.loads(interp.texto)["resposta"] == 'Troque a "fonte" 1 [E1].'

    def test_texto_parcial_do_chat_e_da_analise(self):
        assert fluxo.texto_parcial_chat('{"resposta": "Troque a \\"fon') == 'Troque a "fon'
        assert fluxo.texto_parcial_chat('{"resp') == ""
        parcial = fluxo.texto_parcial_analise('{"resumo": "Uma fonte sem AC", "problemas": [{"titulo": "Fonte 1"}, {"titulo": "Mem')
        assert parcial == "Uma fonte sem AC\n• Fonte 1\n• Mem"

    def test_erro_no_meio_do_fluxo(self):
        assert fluxo.erro_no_fluxo([{"type": "error", "error": {"type": "overloaded_error"}}])
        assert not fluxo.erro_no_fluxo(eventos("anthropic", "x"))

    def test_url_de_streaming_do_gemini(self, tmp_path):
        gemini = tres_provedores(tmp_path)[0]
        url = url_fluxo(gemini)
        assert url.endswith(":streamGenerateContent?alt=sse") and validar_url_ia(url) == ""
        assert validar_url_ia(url.replace("alt=sse", "alt=json")) != ""
        assert validar_url_ia(gemini.url + "?alt=sse") != ""  # query só no método de streaming

    @pytest.mark.parametrize("indice", [0, 1, 2])
    def test_chat_em_streaming_mostra_o_texto_enquanto_chega(self, tmp_path, indice):
        provedor = tres_provedores(tmp_path)[indice]
        executor = ExecutorFluxo([eventos(provedor.tipo, JSON_CHAT)])
        parciais = []
        conversa = Conversa(relatorio_com_analise(tmp_path))
        resultado = conversa.conversar(executor, [provedor], "oi", ao_parcial=parciais.append)
        assert resultado.ok and resultado.mensagem_ia["texto"] == 'Troque a "fonte" 1 [E1].'
        assert len(parciais) > 2 and parciais[-1] == 'Troque a "fonte" 1 [E1].' and parciais[0] != parciais[-1]
        assert "segundos" in resultado.mensagem_ia and "segundos" in resultado.tentativas[0]
        if provedor.tipo != "gemini":
            assert executor.corpos[0]["stream"] is True

    def test_analise_em_streaming(self, tmp_path):
        provedor = tres_provedores(tmp_path)[1]
        bruto = resposta("openai_ok").corpo["choices"][0]["message"]["content"]
        executor = ExecutorFluxo([eventos("openai", bruto)])
        parciais = []
        resultado = solicitar_analise(executor, [provedor], relatorio_com_analise(tmp_path) and {"componentes": []},
                                      ao_parcial=parciais.append)  # fmt: skip
        assert resultado.ok and parciais and "segundos" in resultado.analise


class TestChatRapido:
    def test_uma_nova_tentativa_apos_2_s_sem_modelo_alternativo_e_direto_para_a_reserva(self, tmp_path, monkeypatch):
        import msscan.ia.provedores as prov

        esperas = []
        monkeypatch.setattr(prov, "_dormir", esperas.append)
        executor = ExecutorFalso()
        executor.respostas_ia = [RespostaIA(503, {}), RespostaIA(503, {}), resposta("chat_openai_ok")]
        conversa = Conversa(relatorio_com_analise(tmp_path))
        resultado = conversa.conversar(executor, dois_provedores(tmp_path), "oi")
        assert esperas == [2] and len(executor.ia_urls) == 3
        assert not any("gemini-3.8-flash" in u for u in executor.ia_urls) and "openrouter" in executor.ia_urls[2]
        assert resultado.ok and resultado.mensagem_ia["papel_ia"] == "reserva"

    def test_raciocinio_curto_no_chat(self, tmp_path):
        executor = ExecutorFalso()
        executor.respostas_ia = [resposta("chat_gemini_ok")]
        provedor = dois_provedores(tmp_path)[0]
        pedir_json(executor, provedor, lambda p, s: ({}, {"generationConfig": {}}), 10, perfil=CHAT)
        assert executor.ia_enviados[0][1]["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "low"}

    def test_contexto_compacto(self, tmp_path):
        conversa = Conversa(relatorio_com_analise(tmp_path))
        compacto = resumo_compacto(conversa.dados)
        oks = [c for c in compacto["componentes"] if c["status"] == "OK"]
        assert oks and all(set(c) == {"componente", "status"} for c in oks)
        assert len(conversa.contexto()) < len(json.dumps(conversa.dados, ensure_ascii=False, indent=1)) / 2
        assert "no máximo 120 palavras" in __import__("msscan.ia.chat", fromlist=["x"]).INSTRUCOES_CHAT
