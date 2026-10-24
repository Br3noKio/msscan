"""Análise por IA: sanitização, provedores (Gemini, compatível com OpenAI, Anthropic), fallback, plano
de solução, catálogo de ações, fluxo e interface.

Nenhum teste chama API real: as respostas estão em ``tests/fixtures/ia/`` no formato documentado de cada API.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import pytest

from msscan.core.executor import URL_IA, ExecutorReplay, RespostaIA
from msscan.deteccao.detectores import detectar
from msscan.ia.analise import (
    AVISO_TREINAMENTO,
    ESQUEMA,
    INSTRUCOES,
    NAO_CONFIRMADA,
    disponibilidade,
    solicitar_analise,
    validar_analise,
)
from msscan.ia.provedores import Provedor, carregar_provedores, montar
from msscan.ia.sanitizar import Sanitizador, montar_dados_ia, preparar_envio
from msscan.modulos.registro import registro_padrao
from msscan.relatorio.completo import montar_relatorio
from msscan.relatorio.html import gerar_html
from msscan.relatorio.ia import formatar_analise_ia, secao_ia
from tests.conftest import PASTA_FIXTURES, R640, ExecutorFalso, com_textos

IA = PASTA_FIXTURES / "ia"
CHAVE_GEMINI = "AIza-CHAVE-GEMINI-DE-TESTE"
CHAVE_GROQ = "gsk-CHAVE-GROQ-DE-TESTE"
CHAVE_ANTHROPIC = "sk-ant-CHAVE-ANTHROPIC-DE-TESTE"
URL_GEMINI = "https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent"
URL_GROQ = "https://api.groq.com/openai/v1/chat/completions"


def resposta(nome: str) -> RespostaIA:
    """Resposta de provedor gravada em fixture."""
    dados = json.loads((IA / f"{nome}.json").read_text())
    return RespostaIA(dados["status"], dados["corpo"])


def relatorio_r640() -> dict:
    executor = ExecutorReplay(R640)
    ctx = detectar(executor)
    registro = registro_padrao()
    resultados = registro.diagnosticar(ctx)
    return montar_relatorio(ctx, resultados, None, executor, datetime(2026, 9, 25, 10), datetime(2026, 9, 25, 10, 1), registro.titulo)


def pasta_config(tmp_path: Path, conf: str | None = None, **chaves: str) -> str:
    """Pasta de dados com config/ia.conf (opcional) e arquivos de chave."""
    config = tmp_path / "config"
    config.mkdir(exist_ok=True)
    if conf is not None:
        (config / "ia.conf").write_text(conf)
    for nome, valor in chaves.items():
        (config / nome.replace("_", ".")).write_text(valor + "\n")
    return str(tmp_path)


CONF_TRES = """
[gemini]
provedor = gemini
chave = gemini.key

[groq]
provedor = openai
url = https://api.groq.com/openai/v1
modelo = llama-3.3-70b-versatile
chave = groq.key

[anthropic]
provedor = anthropic
modelo = claude-sonnet-5
chave = ia.key
"""


def tres_provedores(tmp_path: Path) -> list[Provedor]:
    pasta = pasta_config(tmp_path, CONF_TRES, gemini_key=CHAVE_GEMINI, groq_key=CHAVE_GROQ, ia_key=CHAVE_ANTHROPIC)
    provedores, avisos = carregar_provedores(pasta)
    assert avisos == []
    return provedores


# --- Sanitização -------------------------------------------------------------------------------


class TestSanitizacao:
    def test_identificadores_removidos_e_marcadores_consistentes(self):
        dados = {
            "componentes": [{"componente": "discos", "dados": {
                "serial_number": "WD-WX12345678", "wwn": "5000c500a1b2c3d4",
                "texto": "Disco WD-WX12345678 (wwn 0x5000c500a1b2c3d4) em srv-cliente",
                "mac": "02:00:5e:10:00:05", "log": "eno1 02:00:5e:10:00:05 192.168.77.202/24 gw 192.168.77.200 masc 255.255.255.0",
                "ipv6": "2001:db8:78b0:9d5e::11e2", "hora": "13:45:58", "uuid": "4c4c4544-0042-4310-8032-b3c04f313233",
                "ServiceTag": "ABC1234", "nota": "service tag ABC1234 na etiqueta", "loopback": "127.0.0.1",
            }}],
        }  # fmt: skip
        sanitizador = Sanitizador({"srv-cliente": "HOSTNAME"})
        sanitizador.coletar(dados)
        saida = json.dumps(sanitizador.dados(dados), ensure_ascii=False)
        for segredo in ("WD-WX12345678", "5000c500a1b2c3d4", "srv-cliente", "02:00:5e:10:00:05", "192.168.77.202",
                        "192.168.77.200", "2001:db8", "4c4c4544", "ABC1234"):  # fmt: skip
            assert segredo not in saida, segredo
        assert "255.255.255.0" in saida and "13:45:58" in saida and "127.0.0.1" in saida
        assert saida.count("[SERIAL-1]") == 2 and saida.count("[SERVICE-TAG-1]") == 2 and saida.count("[MAC-1]") == 2
        assert sanitizador.contagem()["IP"] == 3 and sanitizador.contagem()["HOSTNAME"] == 1

    def test_hostname_generico_nao_vira_marcador(self):
        sanitizador = Sanitizador({"debian": "HOSTNAME", "localhost": "HOSTNAME"})
        assert sanitizador.dados({"x": "debian live"}) == {"x": "debian live"} and sanitizador.contagem() == {}

    def test_recorte_sem_historico_nem_pendrive(self):
        relatorio = relatorio_r640()
        relatorio["pendrive"] = {"pasta": "/run/live/medium/MSSCAN"}
        recorte = montar_dados_ia(relatorio)
        assert set(recorte) == {"servidor", "veredito", "contagem_por_status", "rede", "acesso_bmc", "componentes"}
        assert "historico_comandos" not in json.dumps(recorte) and "/run/live/medium" not in json.dumps(recorte)

    def test_relatorio_real_sem_mac_nem_ip(self):
        import re

        dados, _ = preparar_envio(relatorio_r640(), ["srv-cliente"])
        texto = json.dumps(dados, ensure_ascii=False)
        assert not re.search(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", texto, re.I)
        assert not re.search(r"(?<![\d.])(?:192\.0\.2|10\.|172\.16)\.", texto)
        assert len(texto) < 300_000

    def test_listas_longas_encurtadas_e_avisadas(self, monkeypatch):
        from msscan.ia import sanitizar

        monkeypatch.setattr(sanitizar, "TAMANHO_MAXIMO", 1000)
        relatorio = {"medido": [{"componente": "log_bmc", "dados": {"eventos": [{"id": i} for i in range(500)]}}]}
        dados, _ = preparar_envio(relatorio)
        assert len(dados["componentes"][0]["dados"]["eventos"]) == 100
        assert dados["listas_encurtadas"] == ["componentes.dados.eventos: 500 itens, enviados só os 100 últimos"]


# --- Configuração dos provedores ---------------------------------------------------------------


class TestConfiguracao:
    def test_padrao_gemini_sem_ia_conf(self, tmp_path):
        provedores, _ = carregar_provedores(pasta_config(tmp_path, gemini_key=CHAVE_GEMINI, ia_key=CHAVE_ANTHROPIC))
        # v0.8.4: sem ia.conf, o padrão vem do catálogo (modelos conferidos na documentação oficial)
        assert [(p.nome, p.tipo, p.modelo, p.papel) for p in provedores] == [
            ("gemini_gratuito", "gemini", "gemini-3.5-flash-lite", "principal"),
            ("anthropic", "anthropic", "claude-sonnet-5", "reserva")]
        assert provedores[0].url == URL_GEMINI.replace("gemini-flash-latest", "gemini-3.5-flash-lite")
        assert provedores[0].chave == CHAVE_GEMINI

    def test_compativel_com_v07_so_anthropic(self, tmp_path):
        provedores, _ = carregar_provedores(pasta_config(tmp_path, ia_key=CHAVE_ANTHROPIC))
        assert [p.tipo for p in provedores] == ["anthropic"] and provedores[0].url == URL_IA

    def test_ia_conf_ordem_e_ollama_sem_chave(self, tmp_path):
        conf = CONF_TRES + "\n[ollama]\nprovedor = openai\nurl = http://192.168.0.10:11434/v1\nmodelo = llama3.1\n"
        provedores, avisos = carregar_provedores(pasta_config(tmp_path, conf, gemini_key=CHAVE_GEMINI, groq_key=CHAVE_GROQ, ia_key=CHAVE_ANTHROPIC))
        assert [p.nome for p in provedores] == ["gemini", "groq", "anthropic", "ollama"] and avisos == []
        assert provedores[1].url == URL_GROQ and provedores[3].url == "http://192.168.0.10:11434/v1/chat/completions"
        assert provedores[3].chave == ""

    @pytest.mark.parametrize(("conf", "aviso"), [
        ("[x]\nprovedor = gpt\n", "desconhecido"),
        ("[x]\nprovedor = gemini\nchave = ../../etc/shadow\n", "sem caminho"),
        ("[x]\nprovedor = gemini\nchave = /root/.ssh/id_rsa\n", "sem caminho"),
        ("[x]\nprovedor = openai\nurl = http://evil.example/v1\nmodelo = m\n", "HTTP sem TLS"),
        ("[x]\nprovedor = openai\nurl = https://api.groq.com/openai/v1\nchave = groq.key\n", "modelo ausente"),
        ("[x]\nprovedor = gemini\nchave = falta.key\n", "sem chave em config/falta.key"),
        ("[x]\nprovedor = gemini\nmodelo = ../x\n", "modelo ausente ou inválido"),
    ])  # fmt: skip
    def test_configuracao_invalida_e_pulada(self, tmp_path, conf, aviso):
        provedores, avisos = carregar_provedores(pasta_config(tmp_path, conf, groq_key=CHAVE_GROQ))
        assert provedores == [] and any(aviso in a for a in avisos), avisos

    def test_chave_nunca_de_variavel_de_ambiente(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "da-variavel")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "da-variavel")
        assert carregar_provedores(pasta_config(tmp_path))[0] == []

    def test_chave_nao_aparece_em_repr_nem_descricao(self, tmp_path):
        provedor = tres_provedores(tmp_path)[0]
        assert CHAVE_GEMINI not in repr(provedor) and CHAVE_GEMINI not in json.dumps(provedor.descrever())


# --- Requisições de cada provedor ----------------------------------------------------------------


class TestRequisicoes:
    def test_gemini(self, tmp_path):
        cabecalhos, corpo = montar(tres_provedores(tmp_path)[0], INSTRUCOES, "dados", ESQUEMA)
        assert cabecalhos == {"x-goog-api-key": CHAVE_GEMINI, "content-type": "application/json"}
        assert corpo["generationConfig"]["responseMimeType"] == "application/json"
        assert corpo["generationConfig"]["responseJsonSchema"] == ESQUEMA
        assert corpo["contents"] == [{"role": "user", "parts": [{"text": "dados"}]}]
        assert "SOMENTE com um objeto JSON" in corpo["systemInstruction"]["parts"][0]["text"]
        assert "responseJsonSchema" not in montar(tres_provedores(tmp_path)[0], INSTRUCOES, "d", ESQUEMA, simples=True)[1]["generationConfig"]

    def test_openai(self, tmp_path):
        cabecalhos, corpo = montar(tres_provedores(tmp_path)[1], INSTRUCOES, "dados", ESQUEMA)
        assert cabecalhos["authorization"] == f"Bearer {CHAVE_GROQ}"
        assert corpo["model"] == "llama-3.3-70b-versatile" and corpo["response_format"] == {"type": "json_object"}
        assert [m["role"] for m in corpo["messages"]] == ["system", "user"]

    def test_anthropic(self, tmp_path):
        cabecalhos, corpo = montar(tres_provedores(tmp_path)[2], INSTRUCOES, "dados", ESQUEMA)
        assert cabecalhos["x-api-key"] == CHAVE_ANTHROPIC and cabecalhos["anthropic-version"] == "2023-06-01"
        assert corpo["output_config"]["format"] == {"type": "json_schema", "schema": ESQUEMA}

    def test_instrucoes_catalogo_e_plano(self):
        assert "bmc.relogio_ntp" in INSTRUCOES and "bmc.sel_exportar_limpar" in INSTRUCOES
        assert "NÃO gera comandos" in INSTRUCOES and "Nunca invente identificadores" in INSTRUCOES
        plano = ESQUEMA["properties"]["problemas"]["items"]["properties"]["plano"]
        assert set(plano["required"]) == {"passos", "prioridade", "intervencao_fisica", "troca_de_peca", "acao_catalogo"}

    def test_esquema_dentro_das_limitacoes(self):
        def objetos(no):
            if isinstance(no, dict):
                if no.get("type") == "object":
                    yield no
                for v in no.values():
                    yield from objetos(v)
            elif isinstance(no, list):
                for v in no:
                    yield from objetos(v)
        for objeto in objetos(ESQUEMA):
            assert objeto["additionalProperties"] is False and set(objeto["required"]) == set(objeto["properties"])
        for proibido in ("minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems"):
            assert proibido not in json.dumps(ESQUEMA)


# --- Respostas gravadas, fallback e validação ------------------------------------------------------


@pytest.mark.parametrize(("provedor", "fixture", "modelo"), [
    (0, "gemini_ok", "gemini-3.8-flash"), (1, "openai_ok", "llama-3.3-70b-versatile"), (2, "anthropic_ok", "claude-sonnet-5"),
])  # fmt: skip
def test_resposta_de_cada_provedor(tmp_path, provedor, fixture, modelo):
    escolhido = tres_provedores(tmp_path)[provedor]
    executor = ExecutorFalso()
    executor.respostas_ia.append(resposta(fixture))
    dados, _ = preparar_envio(relatorio_r640())
    resultado = solicitar_analise(executor, [escolhido], dados)
    assert resultado.ok and resultado.analise["modelo"] == modelo and resultado.analise["provedor"] == escolhido.tipo
    assert executor.ia_urls == [escolhido.url]
    fontes, sel, inventado = resultado.analise["problemas"]
    assert fontes["confirmado"] and fontes["plano"]["passos"][0] == "Conferir o LED da PS1"
    assert fontes["plano"]["intervencao_fisica"] is True and fontes["plano"]["troca_de_peca"] is False
    assert sel["plano"]["acao"]["id"] == "bmc.sel_exportar_limpar" and sel["aviso_acao"] == ""
    assert not inventado["confirmado"] and inventado["aviso"] == NAO_CONFIRMADA
    # Ação inexistente: ignorada e sinalizada, nunca repassada
    assert inventado["plano"]["acao_catalogo"] is None and inventado["plano"]["acao"] is None
    assert inventado["plano"]["acao_ignorada"] == "disco.formatar_tudo" and "não existe no catálogo" in inventado["aviso_acao"]
    assert resultado.analise["acoes_ignoradas"] == ["disco.formatar_tudo"]
    historico = json.dumps(executor.historico_dict())
    for chave in (CHAVE_GEMINI, CHAVE_GROQ, CHAVE_ANTHROPIC):
        assert chave not in historico and chave not in json.dumps(resultado.analise)


def test_fallback_429_erro_e_timeout(tmp_path):
    executor = ExecutorFalso()
    executor.respostas_ia += [resposta("gemini_429"), resposta("openai_500"), resposta("anthropic_ok")]
    resultado = solicitar_analise(executor, tres_provedores(tmp_path), {"componentes": []})
    assert resultado.ok and resultado.analise["provedor_nome"] == "anthropic"
    assert [(t["nome"], t["resultado"]) for t in resultado.tentativas] == [("gemini", "falhou"), ("groq", "falhou"), ("anthropic", "ok")]
    assert resultado.tentativas[0]["detalhe"] == "Limite de uso atingido" and resultado.tentativas[1]["detalhe"] == "HTTP 500"
    assert executor.ia_urls == [URL_GEMINI, URL_GROQ, URL_IA]

    executor = ExecutorFalso()
    executor.respostas_ia += [RespostaIA(None, erro_tipo="timeout", erro="lido")] * 4
    resultado = solicitar_analise(executor, tres_provedores(tmp_path)[:2], {"componentes": []})
    assert not resultado.ok and len(executor.ia_urls) == 5  # timeout: 1 + 3 novas tentativas no Gemini, depois o Groq
    assert resultado.tentativas[0]["detalhe"] == "não respondeu a tempo"


@pytest.mark.parametrize(("fixture", "mensagem"), [
    ("gemini_bloqueado", "bloqueou"), ("gemini_max_tokens", "incompleta"), ("anthropic_recusa", "recusou"),
    ("openai_length", "incompleta"), ("anthropic_429", "Limite de uso atingido"),
])  # fmt: skip
def test_falhas_passam_para_o_proximo(tmp_path, fixture, mensagem):
    executor = ExecutorFalso()
    executor.respostas_ia += [resposta(fixture)]
    provedor = {"gemini": 0, "openai": 1, "anthropic": 2}[fixture.split("_")[0]]
    resultado = solicitar_analise(executor, [tres_provedores(tmp_path)[provedor]], {"componentes": []})
    assert not resultado.ok and mensagem in resultado.mensagem and resultado.analise is None


def test_esquema_recusado_tenta_sem_ele(tmp_path):
    executor = ExecutorFalso()
    executor.respostas_ia += [resposta("gemini_esquema_recusado"), resposta("gemini_ok")]
    resultado = solicitar_analise(executor, [tres_provedores(tmp_path)[0]], {"componentes": []})
    assert resultado.ok and len(executor.ia_enviados) == 2
    assert "responseJsonSchema" in executor.ia_enviados[0][1]["generationConfig"]
    assert "responseJsonSchema" not in executor.ia_enviados[1][1]["generationConfig"]


def test_todos_falham(tmp_path):
    executor = ExecutorFalso()
    resultado = solicitar_analise(executor, tres_provedores(tmp_path), {"componentes": []})
    assert not resultado.ok and resultado.mensagem.startswith("nenhum provedor respondeu") and len(resultado.tentativas) == 3
    assert not solicitar_analise(ExecutorFalso(), [], {}).ok


def test_url_fora_da_lista_bloqueada():
    resposta_ = ExecutorFalso().post_ia({}, {}, url="https://evil.example/v1/messages")
    assert resposta_.erro_tipo == "bloqueado"


def test_plano_invalido_e_normalizado():
    analise = validar_analise({"resumo": "x", "problemas": [{"titulo": "t", "evidencias": [], "plano": "texto"}], "limitacoes": []}, {})
    plano = analise["problemas"][0]["plano"]
    assert plano == {"passos": [], "prioridade": "media", "intervencao_fisica": None, "troca_de_peca": None,
                     "acao_catalogo": None, "acao": None, "acao_ignorada": None}  # fmt: skip


class TestDisponibilidade:
    def test_motivos(self, tmp_path):
        assert "--sem-ia" in disponibilidade(str(tmp_path), True, False, True, False)
        assert "semrede=1" in disponibilidade(str(tmp_path), False, True, False, False)
        assert "replay" in disponibilidade(str(tmp_path), True, False, False, True)
        assert "sem acesso à internet" in disponibilidade(str(tmp_path), False, False, False, False)
        motivo = disponibilidade(str(tmp_path), True, False, False, False)
        assert "Configurar IA" in motivo and "ia.conf" in motivo  # v0.8.4: orienta para a tela e o ia.conf
        assert disponibilidade(pasta_config(tmp_path, gemini_key=CHAVE_GEMINI), True, False, False, False) == ""


# --- Relatório ---------------------------------------------------------------------------------


class TestRelatorioIA:
    def concluida(self, tmp_path):
        executor = ExecutorFalso()
        executor.respostas_ia += [resposta("gemini_429"), resposta("openai_ok")]
        dados, removidos = preparar_envio(relatorio_r640())
        resultado = solicitar_analise(executor, tres_provedores(tmp_path), dados)
        return secao_ia(resultado.analise, removidos, resultado.tentativas)

    def test_texto(self, tmp_path):
        texto = " ".join(formatar_analise_ia(self.concluida(tmp_path)).split())
        assert "Gerado por: groq (openai, modelo llama-3.3-70b-versatile)" in texto
        assert "antes, sem sucesso: gemini: Limite de uso atingido" in texto
        assert "Plano (prioridade alta; intervenção física: sim; troca de peça: não):" in texto
        assert "1) Conferir o LED da PS1" in texto
        assert "Ação do catálogo recomendada: bmc.sel_exportar_limpar" in texto
        assert "'disco.formatar_tudo', que não existe no catálogo" in texto and f"ATENÇÃO: {NAO_CONFIRMADA}" in texto

    def test_html(self, tmp_path):
        html = gerar_html({"interpretacao_ia": self.concluida(tmp_path), "medido": []})
        assert "Plano de solução" in html and "bmc.sel_exportar_limpar" in html and "disco.formatar_tudo" in html
        assert "modelo llama-3.3-70b-versatile" in html

    def test_falha_sem_secao(self):
        html = gerar_html({"interpretacao_ia": {"situacao": "falhou", "motivo": "x"}, "medido": []})
        assert "Interpretação (IA)" not in html and formatar_analise_ia({"situacao": "falhou"}) == ""
        assert "Análise por IA não solicitada." in gerar_html({"interpretacao_ia": {"situacao": "nao_solicitada"}, "medido": []})


# --- Fluxo do modo texto -----------------------------------------------------------------------


def preparar_main(tmp_path):
    from msscan.pendrive.local import local_manual
    from msscan.rede.verificacao import ResultadoRede
    from msscan.relatorio.completo import gravar_relatorios

    pasta_config(tmp_path, CONF_TRES, gemini_key=CHAVE_GEMINI, groq_key=CHAVE_GROQ, ia_key=CHAVE_ANTHROPIC)
    relatorio = relatorio_r640()
    gravacao = gravar_relatorios(relatorio, "texto do terminal", str(tmp_path), replay=False)
    args = argparse.Namespace(sem_ia=False, replay=None)
    return relatorio, gravacao, args, local_manual(str(tmp_path)), ResultadoRede(True)


class TestFluxoTexto:
    def test_padrao_e_nao_com_aviso_de_treinamento(self, tmp_path, capsys):
        from main import etapa_ia

        relatorio, gravacao, args, local, rede = preparar_main(tmp_path)
        executor = ExecutorFalso()
        etapa_ia(args, executor, relatorio, "t", local, rede, gravacao, entrada=lambda _: "")
        assert executor.ia_enviados == [] and relatorio["interpretacao_ia"]["situacao"] == "nao_solicitada"
        saida = " ".join(capsys.readouterr().out.split())
        assert "podem usar os dados enviados para treinar os modelos" in saida
        assert "Provedores, na ordem de tentativa: gemini (gemini-flash-latest), groq (llama-3.3-70b-versatile)" in saida

    def test_ver_antes_e_desistir(self, tmp_path, capsys):
        from main import etapa_ia

        relatorio, gravacao, args, local, rede = preparar_main(tmp_path)
        respostas = iter(["s", "s", "n"])
        executor = ExecutorFalso()
        etapa_ia(args, executor, relatorio, "t", local, rede, gravacao, entrada=lambda _: next(respostas))
        assert executor.ia_enviados == [] and '"componentes"' in capsys.readouterr().out

    def test_fallback_e_relatorio(self, tmp_path, capsys):
        from main import etapa_ia

        relatorio, gravacao, args, local, rede = preparar_main(tmp_path)
        respostas = iter(["s", "n", ""])
        executor = ExecutorFalso()
        executor.respostas_ia += [resposta("gemini_429"), resposta("openai_ok")]
        etapa_ia(args, executor, relatorio, "t", local, rede, gravacao, entrada=lambda _: next(respostas))
        gravado = json.loads((gravacao.pasta / "relatorio.json").read_text())
        assert gravado["interpretacao_ia"]["provedor_nome"] == "groq" and gravado["interpretacao_ia"]["modelo"] == "llama-3.3-70b-versatile"
        assert [t["resultado"] for t in gravado["interpretacao_ia"]["tentativas"]] == ["falhou", "ok"]
        saida = capsys.readouterr().out
        todos = "".join(p.read_text() for p in gravacao.pasta.iterdir()) + saida
        for chave in (CHAVE_GEMINI, CHAVE_GROQ, CHAVE_ANTHROPIC):
            assert chave not in todos

    def test_todos_falham_segue_sem_ia(self, tmp_path, capsys):
        from main import etapa_ia

        relatorio, gravacao, args, local, rede = preparar_main(tmp_path)
        respostas = iter(["s", "n", "s", "n"])  # enviar, sem prévia, tentar de novo uma vez, não tentar mais
        executor = ExecutorFalso()
        etapa_ia(args, executor, relatorio, "t", local, rede, gravacao, entrada=lambda _: next(respostas))
        saida = capsys.readouterr().out
        assert "O relatório segue sem a seção de IA" in saida
        assert len(executor.ia_urls) == 6  # 3 IAs x 2 rodadas: "Tentar novamente" reenvia sem refazer o diagnóstico
        assert relatorio["interpretacao_ia"]["situacao"] == "falhou" and len(relatorio["interpretacao_ia"]["tentativas"]) == 3
        assert "Interpretação (IA)" not in (gravacao.pasta / "relatorio.html").read_text()

    def test_sem_ia_nao_pergunta(self, tmp_path):
        from main import etapa_ia

        relatorio, gravacao, args, local, rede = preparar_main(tmp_path)
        args.sem_ia = True
        etapa_ia(args, ExecutorFalso(), relatorio, "t", local, rede, gravacao, entrada=lambda _: pytest.fail("perguntou"))


# --- Interface -------------------------------------------------------------------------------


class ExecutorReplayComIA(ExecutorReplay):
    """Replay do R640 com as APIs de IA simuladas (respostas gravadas)."""

    def __init__(self, *args, respostas: list[RespostaIA], **kwargs):
        super().__init__(*args, **kwargs)
        self.respostas_ia = respostas
        self.ia_enviados: list = []

    def _post_ia(self, url, cabecalhos, corpo, timeout):
        self.ia_enviados.append((url, corpo))
        return self.respostas_ia.pop(0) if self.respostas_ia else RespostaIA(None, erro_tipo="conexao", erro="sem resposta")


class TestSessaoIA:
    def sessao(self, tmp_path, respostas):
        from msscan.interface.sessao import Sessao
        from msscan.rede.verificacao import ResultadoRede

        pasta = pasta_config(tmp_path, CONF_TRES, gemini_key=CHAVE_GEMINI, groq_key=CHAVE_GROQ, ia_key=CHAVE_ANTHROPIC)
        sessao = Sessao(ExecutorReplayComIA(R640, respostas=respostas), pasta, replay=False)
        sessao.iniciar()
        sessao.aguardar(30)
        sessao.rede = ResultadoRede(True)
        assert sessao.iniciar_diagnostico()
        sessao.aguardar(30)
        return sessao

    def test_estado_previa_envio_e_relatorio(self, tmp_path):
        sessao = self.sessao(tmp_path, [resposta("gemini_ok")])
        estado = sessao.estado()["ia"]
        assert estado["disponivel"] and estado["aviso_treinamento"] == AVISO_TREINAMENTO
        assert [p["nome"] for p in estado["provedores"]] == ["gemini", "groq", "anthropic"]
        assert sessao.previa_ia() and sessao.executor.ia_enviados == []
        assert sessao.solicitar_ia()
        sessao.aguardar(30)
        estado = sessao.estado()["ia"]
        assert estado["situacao"] == "concluida" and estado["analise"]["modelo"] == "gemini-3.8-flash"
        assert estado["analise"]["acoes_ignoradas"] == ["disco.formatar_tudo"]
        texto = json.dumps(sessao.estado())
        for chave in (CHAVE_GEMINI, CHAVE_GROQ, CHAVE_ANTHROPIC):
            assert chave not in texto

    def test_falha(self, tmp_path):
        sessao = self.sessao(tmp_path, [resposta("gemini_429"), resposta("openai_500"), resposta("anthropic_429")])
        assert sessao.solicitar_ia()
        sessao.aguardar(30)
        estado = sessao.estado()["ia"]
        assert estado["situacao"] == "falhou" and estado["mensagem"].startswith("Análise não concluída.") and estado["analise"] is None
        assert estado["sem_reserva"] is False  # três IAs configuradas: sem dica de reserva

    def test_tentar_novamente_sem_refazer_o_diagnostico(self, tmp_path):
        sessao = self.sessao(tmp_path, [RespostaIA(401, {})] * 3)
        relatorio = sessao.relatorio
        assert sessao.solicitar_ia()
        sessao.aguardar(30)
        assert sessao.estado()["ia"]["situacao"] == "falhou" and len(sessao.executor.ia_enviados) == 3  # 401: sem repetir
        sessao.executor.respostas_ia.append(resposta("gemini_ok"))
        assert sessao.solicitar_ia()
        sessao.aguardar(30)
        assert sessao.estado()["ia"]["situacao"] == "concluida" and sessao.relatorio is relatorio

    def test_so_uma_ia_mostra_a_dica_de_reserva(self, tmp_path):
        from msscan.interface.sessao import Sessao
        from msscan.rede.verificacao import ResultadoRede

        pasta = pasta_config(tmp_path, "[ia]\nprincipal = gemini_gratuito\n", gemini_key=CHAVE_GEMINI)
        sessao = Sessao(ExecutorReplayComIA(R640, respostas=[RespostaIA(503, {})] * 8), pasta, replay=False)
        sessao.iniciar()
        sessao.aguardar(30)
        sessao.rede = ResultadoRede(True)
        assert sessao.iniciar_diagnostico()
        sessao.aguardar(30)
        assert sessao.estado()["ia"]["sem_reserva"] is True
        assert sessao.solicitar_ia()
        sessao.aguardar(30)
        estado = sessao.estado()["ia"]
        assert estado["mensagem"] == ("Análise não concluída. O serviço da IA está sobrecarregado agora. Tente de novo em alguns "
                                      "minutos ou configure uma IA reserva.")  # fmt: skip


def test_tela_de_ia():
    html = com_textos((Path(__file__).parent.parent / "msscan/interface/estaticos/index.html").read_text())
    assert 'id="ia-nao" value="nao" checked' in html and 'id="ia-sim" value="sim" disabled' in html
    assert 'type="button" class="botao botao-primario" id="ia-solicitar" disabled' in html
    assert 'id="ia-aviso-treinamento"' in html and "Plano de solução" in html


def test_413_repete_uma_vez_com_os_dados_enxutos(tmp_path):
    """Caso real (R530, Groq gratuita): SEL com 1024 eventos → 413 três vezes. Agora repete enxuto na mesma IA."""
    executor = ExecutorFalso()
    executor.respostas_ia += [RespostaIA(413, {"error": {"message": "Request too large"}}), resposta("openai_ok")]
    dados = {"componentes": [{"componente": "log_bmc", "eventos": [{"n": i} for i in range(1024)]}]}
    resultado = solicitar_analise(executor, [tres_provedores(tmp_path)[1]], dados)
    assert resultado.ok
    assert [t["detalhe"].split(":")[0] for t in resultado.tentativas] == ["dados grandes demais", "dados enxutos"]
    enviado = json.dumps(executor.ia_enviados[-1][1], ensure_ascii=False)
    assert "1024 itens, enviados só os 15 últimos" in enviado and '\\"n\\": 1008' not in enviado
