"""Testes do catálogo de IAs (ia_provedores.yaml) e do ia.conf (principal + reserva, Bloco de Notas)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from msscan.ia.catalogo import (
    ARQUIVO_EMBUTIDO,
    CatalogoInvalido,
    carregar_catalogo,
    interpretar_catalogo,
)
from msscan.ia.config import (
    atualizar_texto,
    criar_modelo_se_faltar,
    interpretar,
    ler_config,
    mascarar,
    modelo_ia_conf,
    normalizar_chave,
    texto_atual_ou_modelo,
)
from msscan.ia.provedores import carregar_provedores, resolver_configuracao

CHAVE = "AIzaSyTESTE-chave-ficticia-0123456789"


def pasta(tmp_path: Path, ia_conf: bytes | str | None = None, **arquivos: str) -> str:
    config = tmp_path / "config"
    config.mkdir(parents=True, exist_ok=True)
    if ia_conf is not None:
        (config / "ia.conf").write_bytes(ia_conf if isinstance(ia_conf, bytes) else ia_conf.encode())
    for nome, valor in arquivos.items():
        (config / nome.replace("_key", ".key")).write_text(valor + "\n")
    return str(tmp_path)


# --- Catálogo ----------------------------------------------------------------------------------


class TestCatalogo:
    def test_embutido_tem_as_opcoes_pedidas(self):
        catalogo = carregar_catalogo()
        assert catalogo.origem == "embutido" and catalogo.avisos == []
        gratuitas = [e.id for e in catalogo.por_categoria("gratuita")]
        pagas = [e.id for e in catalogo.por_categoria("paga")]
        assert gratuitas == ["gemini_gratuito", "groq", "openrouter_gratuito", "mistral_gratuito"]
        assert pagas == ["anthropic", "openai", "gemini_pago", "deepseek", "openrouter_pago"]
        assert {e.tipo_api for e in catalogo.entradas} == {"gemini", "openai", "anthropic"}

    def test_cada_entrada_tem_todos_os_campos_e_data_de_verificacao(self):
        for entrada in carregar_catalogo().entradas:
            assert entrada.link_chave.startswith("https://") and entrada.fontes, entrada.id
            assert entrada.limites and entrada.privacidade and entrada.verificado_em in ("2026-09-28", "2026-10-01"), entrada.id
            assert all(f.startswith("https://") for f in entrada.fontes)

    def test_avisos_de_privacidade(self):
        catalogo = carregar_catalogo()
        assert "china" in catalogo.obter("deepseek").avisos
        assert "treinamento" in catalogo.obter("gemini_gratuito").avisos
        assert "treinamento" not in catalogo.obter("gemini_pago").avisos
        assert "limite_pequeno" in catalogo.obter("groq").avisos  # 8K tokens/min < ~19K de um diagnóstico

    def test_urls_de_chamada_passam_pela_validacao_do_executor(self):
        urls = {e.id: e.url_chamada() for e in carregar_catalogo().entradas}
        assert urls["anthropic"] == "https://api.anthropic.com/v1/messages"
        assert urls["gemini_gratuito"].endswith("/v1beta/models/gemini-3.5-flash-lite:generateContent")
        assert urls["deepseek"] == "https://api.deepseek.com/chat/completions"
        assert urls["mistral_gratuito"] == "https://api.mistral.ai/v1/chat/completions"

    def test_para_dict_nao_tem_chave_nem_arquivo(self):
        dados = json.dumps([e.para_dict() for e in carregar_catalogo().entradas])
        assert "arquivo_chave" not in dados and ".key" not in dados

    @pytest.mark.parametrize(("trecho", "motivo"), [
        ("categoria: gratuita", "categoria deve ser"),
        ("tipo_api: gemini", "tipo_api deve ser"),
        ("url: https://generativelanguage.googleapis.com/v1beta", "url"),
        ("link_chave: https://aistudio.google.com/apikey", "link_chave deve ser HTTPS"),
        ("arquivo_chave: gemini.key", "sem caminho"),
        ("avisos: [treinamento]", "avisos devem estar entre"),
    ])  # fmt: skip
    def test_catalogo_invalido_e_recusado(self, trecho, motivo):
        troca = {
            "categoria: gratuita": "categoria: talvez",
            "tipo_api: gemini": "tipo_api: gpt",
            "url: https://generativelanguage.googleapis.com/v1beta": "url: http://atacante.example/v1",
            "link_chave: https://aistudio.google.com/apikey": "link_chave: http://aistudio.google.com/apikey",
            "arquivo_chave: gemini.key": "arquivo_chave: ../../etc/shadow",
            "avisos: [treinamento]": "avisos: [inventado]",
        }[trecho]
        texto = ARQUIVO_EMBUTIDO.read_text().replace(trecho, troca, 1)
        with pytest.raises(CatalogoInvalido, match=motivo):
            interpretar_catalogo(texto)

    def test_ids_repetidos_recusados(self):
        texto = ARQUIVO_EMBUTIDO.read_text().replace("id: groq", "id: gemini_gratuito", 1)
        with pytest.raises(CatalogoInvalido, match="repetidos"):
            interpretar_catalogo(texto)

    def test_catalogo_do_pendrive_tem_prioridade(self, tmp_path):
        config = tmp_path / "config"
        config.mkdir()
        texto = ARQUIVO_EMBUTIDO.read_text().replace("modelo_padrao: deepseek-flash", "modelo_padrao: deepseek-v4-pro")
        (config / "ia_provedores.yaml").write_text(texto)
        catalogo = carregar_catalogo(str(tmp_path))
        assert catalogo.origem.startswith("pendrive") and catalogo.obter("deepseek").modelo_padrao == "deepseek-v4-pro"

    def test_catalogo_invalido_no_pendrive_volta_ao_embutido(self, tmp_path):
        config = tmp_path / "config"
        config.mkdir()
        (config / "ia_provedores.yaml").write_text("versao: 1\nprovedores: [ {id: x} ]\n")
        catalogo = carregar_catalogo(str(tmp_path))
        assert catalogo.origem == "embutido" and len(catalogo.entradas) == 9
        assert "ignorado" in catalogo.avisos[0]


# --- ia.conf ---------------------------------------------------------------------------------


class TestIaConf:
    def test_arquivo_do_bloco_de_notas_crlf_bom_e_espacos(self, tmp_path):
        texto = f"﻿[ia]\r\nprincipal = Gemini_Gratuito \r\nreserva=groq\r\n\r\n[gemini_gratuito]\r\nchave =   {CHAVE}   \r\n"
        provedores, avisos = carregar_provedores(pasta(tmp_path, texto.encode("utf-8")))
        assert [(p.nome, p.papel) for p in provedores] == [("gemini_gratuito", "principal")]
        assert provedores[0].chave == CHAVE
        assert any("[groq]: sem chave" in a for a in avisos)

    def test_ansi_do_bloco_de_notas_antigo(self, tmp_path):
        texto = f"; configuração\r\n[ia]\r\nprincipal = groq\r\n[groq]\r\nchave = {CHAVE}\r\n".encode("cp1252")
        provedores, _ = carregar_provedores(pasta(tmp_path, texto))
        assert provedores[0].nome == "groq" and provedores[0].chave == CHAVE

    def test_chave_colada_na_linha_de_baixo_e_com_aspas(self, tmp_path):
        texto = f'[ia]\nprincipal = deepseek\n[deepseek]\nchave =\n   "{CHAVE}"\n'
        provedores, _ = carregar_provedores(pasta(tmp_path, texto))
        assert provedores[0].chave == CHAVE and provedores[0].url == "https://api.deepseek.com/chat/completions"

    @pytest.mark.parametrize("colada", [f"  {CHAVE}\n", f"{CHAVE}\r\n", f"'{CHAVE}'", f"​{CHAVE} ", f"{CHAVE[:10]}\n{CHAVE[10:]}"])
    def test_normalizar_chave(self, colada):
        assert normalizar_chave(colada) == CHAVE

    def test_mascara_no_maximo_4_caracteres(self):
        assert mascarar(CHAVE) == "••••••••" + CHAVE[-4:]
        assert CHAVE[:-4] not in mascarar(CHAVE)
        assert mascarar("curta123") == "••••••••"  # chave curta: nada visível
        assert mascarar("") == ""

    def test_principal_e_reserva_na_ordem(self, tmp_path):
        texto = f"[ia]\nprincipal = openrouter_gratuito\nreserva = anthropic\n[openrouter_gratuito]\nchave = {CHAVE}\n[anthropic]\nchave = sk-ant-{CHAVE}\n"
        provedores, avisos = carregar_provedores(pasta(tmp_path, texto))
        assert [(p.nome, p.papel, p.modelo) for p in provedores] == [
            ("openrouter_gratuito", "principal", "openrouter/free"), ("anthropic", "reserva", "claude-sonnet-5")]
        assert avisos == []

    def test_modelo_trocado_no_ia_conf(self, tmp_path):
        texto = f"[ia]\nprincipal = gemini_pago\n[gemini_pago]\nchave = {CHAVE}\nmodelo = gemini-3.8-flash\n"
        [provedor], _ = carregar_provedores(pasta(tmp_path, texto))
        assert provedor.url.endswith("/models/gemini-3.8-flash:generateContent")

    def test_chave_no_arquivo_antigo_quando_o_ia_conf_nao_tem(self, tmp_path):
        texto = "[ia]\nprincipal = gemini_gratuito\nreserva = anthropic\n[gemini_gratuito]\nchave =\n"
        provedores, _ = carregar_provedores(pasta(tmp_path, texto, gemini_key=CHAVE, ia_key="sk-ant-x" * 3))
        assert [p.nome for p in provedores] == ["gemini_gratuito", "anthropic"] and provedores[0].chave == CHAVE

    def test_provedor_proprio_fora_do_catalogo(self, tmp_path):
        texto = "[ia]\nprincipal = ollama\n[ollama]\nprovedor = openai\nurl = http://192.168.0.10:11434/v1\nmodelo = llama3.1\n"
        [provedor], avisos = carregar_provedores(pasta(tmp_path, texto))
        assert provedor.url == "http://192.168.0.10:11434/v1/chat/completions" and provedor.chave == "" and avisos == []

    @pytest.mark.parametrize(("texto", "aviso"), [
        ("[ia]\nprincipal = inventada\n", "não está no catálogo"),
        ("[ia]\nprincipal = x\n[x]\nprovedor = openai\nurl = http://evil.example/v1\nmodelo = m\n", "HTTP sem TLS"),
        ("[ia]\nreserva = groq\n", "escolha a IA principal"),
        ("[ia]\nprincipal = groq\nlixo sem igual\n", "linha 3 ignorada"),
    ])  # fmt: skip
    def test_erros_viram_aviso(self, tmp_path, texto, aviso):
        _, avisos = carregar_provedores(pasta(tmp_path, texto))
        assert any(aviso in a for a in avisos), avisos

    def test_resolver_mostra_quem_esta_sem_chave(self, tmp_path):
        configurados, _ = resolver_configuracao(pasta(tmp_path, "[ia]\nprincipal = groq\nreserva = openai\n"))
        assert [(p.nome, p.papel, p.chave) for p in configurados] == [("groq", "principal", ""), ("openai", "reserva", "")]

    def test_chave_nunca_de_variavel_de_ambiente(self, tmp_path, monkeypatch):
        for variavel in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GROQ_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
            monkeypatch.setenv(variavel, "da-variavel")
        assert carregar_provedores(pasta(tmp_path, "[ia]\nprincipal = groq\nreserva = openai\n"))[0] == []

    def test_chave_nao_aparece_em_repr_nem_descricao(self, tmp_path):
        [provedor], _ = carregar_provedores(pasta(tmp_path, f"[ia]\nprincipal = groq\n[groq]\nchave = {CHAVE}\n"))
        assert CHAVE not in repr(provedor) and CHAVE not in json.dumps(provedor.descrever())


class TestModeloEGravacao:
    def test_modelo_comentado_em_portugues_com_crlf(self):
        texto = modelo_ia_conf(carregar_catalogo())
        assert "\r\n" in texto and "\n" not in texto.replace("\r\n", "")
        assert "PASSO A PASSO" in texto and "Bloco de Notas" in texto
        assert "https://aistudio.google.com/apikey" in texto and "China" in texto
        config = interpretar(texto)
        assert config.principal == "gemini_gratuito" and config.reserva == "" and config.avisos == []
        assert all(not v.get("chave") for v in config.secoes.values())
        assert texto.index("IAs GRATUITAS") < texto.index("[groq]") < texto.index("IAs PAGAS") < texto.index("[openai]")

    def test_primeira_execucao_cria_modelo_e_nunca_sobrescreve(self, tmp_path):
        local = pasta(tmp_path)
        assert criar_modelo_se_faltar(local, carregar_catalogo())
        arquivo = tmp_path / "config" / "ia.conf"
        arquivo.write_text("[ia]\nprincipal = groq\n")
        assert not criar_modelo_se_faltar(local, carregar_catalogo())
        assert arquivo.read_text() == "[ia]\nprincipal = groq\n"

    def test_primeira_execucao_aponta_para_chaves_antigas(self, tmp_path):
        local = pasta(tmp_path, ia_key="sk-ant-" + CHAVE)  # usuário da v0.7: só a chave da Anthropic
        criar_modelo_se_faltar(local, carregar_catalogo())
        assert ler_config(local).principal == "anthropic"
        [provedor], _ = carregar_provedores(local)
        assert provedor.nome == "anthropic"

    def test_gravar_preserva_comentarios_e_linhas_do_tecnico(self):
        original = "; meu comentário\r\n[ia]\r\nprincipal = groq ; a mais rápida\r\n\r\n[groq]\r\n; chave da empresa\r\nchave = antiga\r\nmodelo = openai/gpt-oss-20b\r\n"
        novo = atualizar_texto(original, principal="deepseek", reserva="groq", chaves={"groq": f"  {CHAVE} \n", "deepseek": CHAVE})
        assert "; meu comentário" in novo and "; chave da empresa" in novo and "modelo = openai/gpt-oss-20b" in novo
        config = interpretar(novo)
        assert (config.principal, config.reserva) == ("deepseek", "groq")
        assert config.secoes["groq"]["chave"] == CHAVE and config.secoes["deepseek"]["chave"] == CHAVE
        assert "antiga" not in novo and novo.count("\r\n") == len(novo.split("\r\n")) - 1

    def test_formato_antigo_vira_backup(self, tmp_path):
        local = pasta(tmp_path, "[gemini]\nprovedor = gemini\nchave = gemini.key\n")
        texto, aviso = texto_atual_ou_modelo(local, carregar_catalogo())
        assert "ia.conf.antigo" in aviso and (tmp_path / "config" / "ia.conf.antigo").read_text().startswith("[gemini]")
        assert interpretar(texto).principal == "gemini_gratuito"

    def test_formato_antigo_segue_funcionando_sem_mexer(self, tmp_path):
        local = pasta(tmp_path, "[gemini]\nprovedor = gemini\nchave = gemini.key\n", gemini_key=CHAVE)
        [provedor], _ = carregar_provedores(local)
        assert provedor.nome == "gemini" and provedor.chave == CHAVE and provedor.papel == ""


def test_coleta_nunca_leva_o_ia_conf_nem_chaves(tmp_path):
    """Guarda: uma coleta completa com o ia.conf cheio de chaves no local de dados não contém nenhuma delas."""
    import tarfile

    from msscan.coleta.coletor import coletar
    from msscan.core.executor import ExecutorReplay
    from tests.conftest import R640

    dados = tmp_path / "dados"
    local = pasta(dados, f"[ia]\nprincipal = groq\n[groq]\nchave = {CHAVE}\n", gemini_key="AIzaOutraChaveSecreta123")
    arquivo = coletar(tmp_path / "saida", base=ExecutorReplay(R640), pasta_dados=local)
    with tarfile.open(arquivo) as pacote:
        for membro in pacote.getmembers():
            assert "ia.conf" not in membro.name and not membro.name.endswith(".key")
            if membro.isfile():
                conteudo = pacote.extractfile(membro).read()
                assert CHAVE.encode() not in conteudo and b"AIzaOutraChaveSecreta123" not in conteudo, membro.name


# --- "Testar" e indicador de estado ------------------------------------------------------------

from msscan.core.executor import RespostaIA  # noqa: E402
from msscan.ia.analise import solicitar_analise  # noqa: E402
from msscan.ia import estado as modulo_estado  # noqa: E402
from msscan.ia.estado import EstadoIA, indicador  # noqa: E402
from tests.conftest import ExecutorFalso  # noqa: E402


def dois_provedores(tmp_path: Path):
    texto = f"[ia]\nprincipal = gemini_gratuito\nreserva = openrouter_gratuito\n[gemini_gratuito]\nchave = {CHAVE}\n[openrouter_gratuito]\nchave = sk-or-{CHAVE}\n"
    configurados, _ = resolver_configuracao(pasta(tmp_path, texto))
    return configurados


class TestTestarEIndicador:
    @pytest.mark.parametrize(("status", "situacao", "trecho"), [
        (200, "ok", "Chave aceita"), (401, "sem_acesso", "recusada"), (403, "sem_acesso", "recusada"),
        (429, "limite", "limite de uso"), (500, "erro", "erro"),
    ])  # fmt: skip
    def test_testar_classifica_pelo_http(self, tmp_path, status, situacao, trecho):
        executor = ExecutorFalso()
        executor.respostas_ia = [RespostaIA(status, {"candidates": [{"content": {"parts": [{"text": "{}"}]}}]} if status == 200 else {"error": {"message": "x"}})]
        resultado = modulo_estado.testar_chave(executor, dois_provedores(tmp_path)[0])
        assert (resultado.situacao, resultado.status) == (situacao, status) and trecho in resultado.mensagem

    def test_testar_nao_envia_nada_do_servidor(self, tmp_path):
        executor = ExecutorFalso()
        executor.respostas_ia = [RespostaIA(200, {"choices": [{"message": {"content": "{}"}}]})]
        modulo_estado.testar_chave(executor, dois_provedores(tmp_path)[1])
        [(cabecalhos, corpo)] = executor.ia_enviados
        assert corpo["messages"][-1] == {"role": "user", "content": "teste"}
        assert "PowerEdge" not in json.dumps(corpo) and "componentes" not in json.dumps(corpo)
        assert executor.historico[-1].comando[-1] == "(dados e chave omitidos)"
        assert CHAVE not in json.dumps(executor.historico_dict())

    def test_testar_sem_rede_e_sem_chave(self, tmp_path):
        assert modulo_estado.testar_chave(ExecutorFalso(), dois_provedores(tmp_path)[0]).situacao == "sem_rede"
        [sem_chave], _ = resolver_configuracao(pasta(tmp_path, "[ia]\nprincipal = groq\n"))
        executor = ExecutorFalso()
        assert modulo_estado.testar_chave(executor, sem_chave).situacao == "sem_chave" and executor.ia_enviados == []

    def test_indicador_quatro_estados(self, tmp_path):
        estado = EstadoIA()
        configurados = dois_provedores(tmp_path)
        assert indicador([], estado)["situacao"] == "sem_chave"
        assert indicador(configurados, estado)["situacao"] == "disponivel"
        estado.registrar("gemini_gratuito", "limite")
        estado.registrar("openrouter_gratuito", "sem_acesso")
        assert indicador(configurados, estado)["situacao"] == "limite"
        estado.registrar("gemini_gratuito", "sem_acesso")
        assert indicador(configurados, estado)["situacao"] == "sem_acesso"
        assert indicador(configurados, estado, "sem acesso à internet")["situacao"] == "indisponivel"

    def test_indicador_usa_a_reserva_quando_a_principal_esta_no_limite(self, tmp_path):
        estado = EstadoIA()
        estado.registrar("gemini_gratuito", "limite")
        resultado = indicador(dois_provedores(tmp_path), estado)
        assert resultado["situacao"] == "disponivel" and "reserva" in resultado["detalhe"]

    def test_analise_real_atualiza_o_estado_e_cai_para_a_reserva(self, tmp_path):
        from tests.test_ia import resposta  # respostas no formato documentado de cada provedor

        executor = ExecutorFalso()
        executor.respostas_ia = [resposta("gemini_429"), resposta("gemini_429"), resposta("openai_ok")]  # 2º: modelo alternativo
        resultado = solicitar_analise(executor, dois_provedores(tmp_path), {"componentes": []})
        estado = EstadoIA()
        estado.registrar_tentativas(resultado.tentativas)
        assert [t["situacao"] for t in resultado.tentativas] == ["limite", "ok"]
        assert resultado.ok and resultado.analise["provedor_nome"] == "openrouter_gratuito"
        assert estado.situacao("gemini_gratuito") == "limite"


class TestOrigemDaAnalise:
    """Relatório diz qual IA gerou a análise e se foi a principal ou a reserva (CLAUDE.md 10c)."""

    def secao(self, tmp_path, respostas):
        from msscan.relatorio.ia import formatar_analise_ia, secao_ia
        from tests.test_ia import resposta

        executor = ExecutorFalso()
        executor.respostas_ia = [resposta(r) for r in respostas]
        resultado = solicitar_analise(executor, dois_provedores(tmp_path), {"componentes": []})
        assert resultado.ok
        secao = secao_ia(resultado.analise, {}, resultado.tentativas)
        return secao, " ".join(formatar_analise_ia(secao).split())

    def test_principal(self, tmp_path):
        secao, texto = self.secao(tmp_path, ["gemini_ok"])
        assert secao["papel"] == "principal" and ", IA principal" in secao["origem"]
        assert "sem sucesso" not in secao["origem"] and secao["origem"] in texto

    def test_reserva_depois_do_limite_da_principal(self, tmp_path):
        secao, texto = self.secao(tmp_path, ["gemini_429", "gemini_429", "openai_ok"])
        assert secao["papel"] == "reserva" and secao["provedor_nome"] == "openrouter_gratuito"
        assert "[openrouter_gratuito]" in secao["origem"] and ", IA reserva" in secao["origem"]
        assert "sem sucesso: gemini_gratuito (IA principal): Limite do plano gratuito atingido" in secao["origem"]
        assert CHAVE not in json.dumps(secao)


class TestIndicadorModoTexto:
    def args(self, **kw):
        from argparse import Namespace

        return Namespace(**{"sem_ia": False, "replay": None, **kw})

    def test_linha(self, tmp_path):
        from main import linha_estado_ia
        from msscan.rede.verificacao import ResultadoRede

        texto = f"[ia]\nprincipal = gemini_gratuito\n[gemini_gratuito]\nchave = {CHAVE}\n"
        linha = linha_estado_ia(self.args(), ResultadoRede(True), pasta(tmp_path, texto))
        assert linha.startswith("IA disponível: ") and CHAVE not in linha
        assert linha_estado_ia(self.args(), ResultadoRede(True), None).startswith("IA sem chave")
        assert "--sem-ia" in linha_estado_ia(self.args(sem_ia=True), ResultadoRede(True), None)
        sem_rede = linha_estado_ia(self.args(), ResultadoRede(False, sem_rede_por_parametro=True), None)
        assert sem_rede.startswith("IA indisponível") and "semrede=1" in sem_rede


class TestErrosTemporarios:
    """HTTP 503 no teste real (29/09/2026); esperas curtas pedidas em 30/09/2026 (CLAUDE.md 15.1): análise 2 s e 5 s,
    teto ~20 s, outro modelo e só então a reserva."""

    def rodar(self, tmp_path, monkeypatch, respostas, provedores=None):
        import msscan.ia.provedores as prov

        esperas, avisos = [], []
        monkeypatch.setattr(prov, "_dormir", esperas.append)
        executor = ExecutorFalso()
        executor.respostas_ia = list(respostas)
        resultado = solicitar_analise(executor, provedores or dois_provedores(tmp_path)[:1], {"componentes": []}, avisos.append)
        return resultado, executor, esperas, avisos

    def test_503_repete_com_espera_crescente_e_avisa(self, tmp_path, monkeypatch):
        from tests.test_ia import resposta

        resultado, executor, esperas, avisos = self.rodar(tmp_path, monkeypatch, [RespostaIA(503, {}), RespostaIA(502, {}), resposta("gemini_ok")])
        assert resultado.ok and len(executor.ia_urls) == 3 and esperas == [2, 5]
        assert avisos == ["Serviço da IA ocupado, tentando novamente (1/2)...", "Serviço da IA ocupado, tentando novamente (2/2)..."]

    def test_retry_after_manda_so_se_couber_no_teto(self, tmp_path, monkeypatch):
        from tests.test_ia import resposta

        respostas = [RespostaIA(503, {}, espera_s=7), RespostaIA(504, {}, espera_s=6), resposta("gemini_ok")]
        resultado, _, esperas, _ = self.rodar(tmp_path, monkeypatch, respostas)
        assert resultado.ok and esperas == [7, 6]
        # Retry-After maior que o teto (~20 s): não espera; vai para o outro modelo / a reserva.
        resultado, executor, esperas, _ = self.rodar(tmp_path, monkeypatch, [RespostaIA(503, {}, espera_s=600), resposta("gemini_ok")])
        assert esperas == [] and resultado.ok and "gemini-3.8-flash" in executor.ia_urls[1]

    def test_teto_conta_o_tempo_das_respostas_que_falharam(self, tmp_path, monkeypatch):
        import msscan.ia.provedores as prov

        relogio = [0.0]
        monkeypatch.setattr(prov, "_relogio", lambda: relogio[0])
        monkeypatch.setattr(prov, "_dormir", lambda s: None)
        executor = ExecutorFalso()
        executor.respostas_ia = [RespostaIA(503, {})] * 4
        original = executor._post_ia

        def demorado(*args):  # cada resposta 503 leva 19 s: 19 + 2 > ~20 → não repete, vai direto ao outro modelo
            relogio[0] += 19
            return original(*args)

        executor._post_ia = demorado
        resultado = solicitar_analise(executor, dois_provedores(tmp_path)[:1], {"componentes": []})
        assert not resultado.ok and len(executor.ia_urls) == 2 and "gemini-3.8-flash" in executor.ia_urls[1]
        assert resultado.tentativas[0]["segundos"] == 38

    def test_continua_503_tenta_o_modelo_lite_com_a_mesma_chave_e_depois_a_reserva(self, tmp_path, monkeypatch):
        from tests.test_ia import resposta

        respostas = [RespostaIA(503, {})] * 6 + [resposta("openai_ok")]
        resultado, executor, esperas, avisos = self.rodar(tmp_path, monkeypatch, respostas, dois_provedores(tmp_path))
        assert resultado.ok and resultado.analise["provedor_nome"] == "openrouter_gratuito"
        assert [u.split("/models/")[-1] for u in executor.ia_urls[:6]] == \
            ["gemini-3.5-flash-lite:generateContent"] * 3 + ["gemini-3.8-flash:generateContent"] * 3  # fmt: skip
        assert esperas == [2, 5] * 2 and any("gemini-3.8-flash" in a for a in avisos)
        assert resultado.tentativas[0]["detalhe"].startswith("O serviço da IA está sobrecarregado agora")

    def test_so_a_principal_mensagem_clara_para_a_tela(self, tmp_path, monkeypatch):
        from msscan.ia.provedores import resumo_falhas

        resultado, _, _, _ = self.rodar(tmp_path, monkeypatch, [RespostaIA(503, {})] * 8)
        assert resumo_falhas(resultado.tentativas) == ("O serviço da IA está sobrecarregado agora. Tente de novo em alguns minutos "
                                                       "ou configure uma IA reserva")  # fmt: skip
