"""Interface: aba Chat (CLAUDE.md 10e) e chave da IA pelo celular (10d) na sessão e nas rotas da API."""

from __future__ import annotations

import functools
import json
import threading
import time
import urllib.request
from types import SimpleNamespace

import pytest

from tests.conftest import com_textos
from msscan.interface import sessao as modulo_sessao
from msscan.interface.pareamento import AGUARDANDO, ENCERRADA, Pareamento
from msscan.interface.servidor import ServidorUI
from msscan.rede.verificacao import InterfaceRede
from tests.test_ia import CHAVE_ANTHROPIC, CHAVE_GEMINI, CHAVE_GROQ, TestSessaoIA, resposta
from tests.test_pareamento import CHAVE, pedir as pedir_https, porta_livre

CHAVES = (CHAVE_GEMINI, CHAVE_GROQ, CHAVE_ANTHROPIC, CHAVE)


def aguardar(condicao, segundos: float = 10.0) -> bool:
    fim = time.monotonic() + segundos
    while time.monotonic() < fim:
        if condicao():
            return True
        time.sleep(0.05)
    return condicao()


def sessao_com_analise(tmp_path, *respostas_chat):
    sessao = TestSessaoIA().sessao(tmp_path, [resposta("gemini_ok"), *respostas_chat])
    assert sessao.solicitar_ia()
    sessao.aguardar(30)
    assert sessao.estado()["ia"]["situacao"] == "concluida"
    return sessao


@pytest.fixture
def porta_do_pareamento(monkeypatch):
    porta = porta_livre()
    monkeypatch.setattr(modulo_sessao, "Pareamento", functools.partial(Pareamento, porta=porta))
    return porta


# --- Chat na sessão -------------------------------------------------------------------------------


class TestChatNaSessao:
    def test_antes_da_analise_indisponivel(self, tmp_path):
        sessao = TestSessaoIA().sessao(tmp_path, [])
        chat = sessao.estado()["chat"]
        assert not chat["disponivel"] and "após a análise por IA" in chat["motivo"] and chat["sugestoes"]
        assert sessao.enviar_chat("oi")[0] is False and sessao.mensagens_chat()["mensagens"] == []

    def test_primeira_mensagem_pergunta_e_relatorio(self, tmp_path):
        sessao = sessao_com_analise(tmp_path, resposta("chat_gemini_ok"))
        chat = sessao.estado()["chat"]
        assert chat["disponivel"] and chat["situacao"] == "pronto" and chat["quantidade"] == 1
        revisao = chat["revisao"]
        assert sessao.enviar_chat("") == (False, "Digite uma pergunta antes de enviar.")
        assert sessao.enviar_chat("O servidor srv-cliente 192.168.77.202 precisa de peça?") == (True, "enviado")
        assert aguardar(lambda: sessao.chat["situacao"] == "pronto" and sessao.chat["revisao"] > revisao + 1)
        mensagens = sessao.mensagens_chat()["mensagens"]
        assert [m["papel"] for m in mensagens] == ["ia", "tecnico", "ia"]
        assert "192.168.77.202" not in mensagens[1]["texto"] and mensagens[1]["avisos"]
        assert {e["id"]: e["confirmada"] for e in mensagens[2]["evidencias"]} == {"E1": True, "E2": False, "E3": False}
        pasta = sessao.gravacao["pasta"]
        with open(f"{pasta}/relatorio.json", encoding="utf-8") as arquivo:
            gravado = json.load(arquivo)
        assert [m["papel"] for m in gravado["chat"]["mensagens"]] == ["ia", "tecnico", "ia"]
        with open(f"{pasta}/relatorio.txt", encoding="utf-8") as arquivo:
            assert "Conversa com a IA\n" in arquivo.read()
        texto = json.dumps(sessao.estado()) + json.dumps(sessao.mensagens_chat())
        for chave in CHAVES:
            assert chave not in texto

    def test_uma_pergunta_por_vez(self, tmp_path):
        sessao = sessao_com_analise(tmp_path)
        sessao.chat["situacao"] = "digitando"
        assert sessao.enviar_chat("oi") == (False, "Aguarde a resposta anterior.")

    def test_sem_resposta_mostra_falha(self, tmp_path):
        sessao = sessao_com_analise(tmp_path)  # nenhuma resposta programada: todas as IAs falham
        assert sessao.enviar_chat("oi")[0]
        assert aguardar(lambda: sessao.chat["situacao"] == "falhou")
        assert sessao.chat["mensagem"].startswith("Sem resposta da IA") and sessao.mensagens_chat()["mensagens"][-1]["papel"] == "sistema"

    def test_tentar_novamente_reenvia_a_pergunta_sem_duplicar(self, tmp_path):
        sessao = sessao_com_analise(tmp_path)
        assert sessao.repetir_chat()[0] is False  # nada falhou ainda
        assert sessao.enviar_chat("Qual fonte está com defeito?")[0]
        assert aguardar(lambda: sessao.chat["situacao"] == "falhou")
        sessao.executor.respostas_ia.append(resposta("chat_gemini_ok"))
        assert sessao.repetir_chat() == (True, "enviado")
        assert aguardar(lambda: sessao.chat["situacao"] == "pronto")
        mensagens = sessao.mensagens_chat()["mensagens"]
        assert [m["papel"] for m in mensagens] == ["ia", "tecnico", "ia"] and mensagens[1]["texto"] == "Qual fonte está com defeito?"

    def test_diagnostico_refeito_encerra_o_chat(self, tmp_path):
        sessao = sessao_com_analise(tmp_path)
        assert sessao.refazer_diagnostico(None)
        sessao.aguardar(30)
        chat = sessao.estado()["chat"]
        assert not chat["disponivel"] and "Diagnóstico refeito" in chat["motivo"]


# --- Celular na sessão ----------------------------------------------------------------------------


class TestCelularNaSessao:
    def test_ip_da_placa_fisica_com_link(self, tmp_path):
        sessao = TestSessaoIA().sessao(tmp_path, [])
        sessao.rede = SimpleNamespace(interfaces=[
            InterfaceRede("idrac", "cdc_ether", True, True, True, ["169.254.0.2/24"]),
            InterfaceRede("eno1", "tg3", True, False, False, ["10.0.0.9/24"]),
            InterfaceRede("eno2", "tg3", True, False, True, ["fe80::1/64", "192.168.77.202/24"]),
        ])  # fmt: skip
        assert sessao._ip_local() == "192.168.77.202"
        sessao.rede = SimpleNamespace(interfaces=[])
        assert sessao._ip_local() is None
        ok, mensagem = sessao.iniciar_pareamento("gemini_gratuito")
        assert not ok and "não tem endereço IP" in mensagem and "config/ia.conf" in mensagem

    def test_sem_pasta_de_dados_ou_ia_desconhecida(self, tmp_path):
        sessao = TestSessaoIA().sessao(tmp_path, [])
        assert sessao.iniciar_pareamento("nao-existe") == (False, "Serviço de IA desconhecido")
        sessao.pasta_dados = None
        assert "Sem local de dados" in sessao.iniciar_pareamento("gemini_gratuito")[1]

    def test_receber_aceitar_salvar_e_testar(self, tmp_path, porta_do_pareamento, monkeypatch):
        sessao = TestSessaoIA().sessao(tmp_path, [resposta("gemini_ok")])
        monkeypatch.setattr(sessao, "_ip_local", lambda: "127.0.0.1")
        assert sessao.iniciar_pareamento("gemini_gratuito") == (True, "aguardando o celular")
        pareamento = sessao.pareamento
        assert sessao.estado()["ia_celular"]["situacao"] == AGUARDANDO and sessao.manter_pareamento()
        assert sessao.aceitar_chave_celular()[0] is False  # nada recebido ainda
        assert pedir_https(pareamento, "POST", f"/p/{pareamento.token}", f"chave={CHAVE}")[0] == 200
        ok, mensagem, teste = sessao.aceitar_chave_celular()
        assert ok and teste is not None
        with open(f"{sessao.pasta_dados}/config/ia.conf", encoding="utf-8") as arquivo:
            assert CHAVE in arquivo.read()  # salva só no ia.conf (pedido do usuário, 10c)
        assert aguardar(lambda: len(sessao.pareamentos_ia) == 1)
        assert sessao.pareamentos_ia[0]["resultado"] == "aceita" and sessao.relatorio["pareamentos_ia"] == sessao.pareamentos_ia
        assert CHAVE not in json.dumps(sessao.estado()) + json.dumps(sessao.relatorio, default=str)
        assert aguardar(lambda: "Chave da IA enviada pelo celular" in open(f"{sessao.gravacao['pasta']}/relatorio.txt", encoding="utf-8").read())

    def test_novo_pareamento_encerra_o_anterior_e_parar(self, tmp_path, porta_do_pareamento, monkeypatch):
        sessao = TestSessaoIA().sessao(tmp_path, [])
        monkeypatch.setattr(sessao, "_ip_local", lambda: "127.0.0.1")
        assert sessao.iniciar_pareamento("groq")[0]
        primeiro = sessao.pareamento
        assert aguardar(lambda: primeiro.situacao == AGUARDANDO)
        # QR novo: encerra o anterior e reabre na MESMA porta, na hora
        assert sessao.iniciar_pareamento("groq")[0] and sessao.pareamento is not primeiro and primeiro.situacao == ENCERRADA
        assert sessao.parar_pareamento() and sessao.pareamento.situacao == ENCERRADA
        assert not sessao.manter_pareamento()

    def test_encerrar_a_sessao_fecha_o_pareamento(self, tmp_path, porta_do_pareamento, monkeypatch):
        sessao = TestSessaoIA().sessao(tmp_path, [])
        monkeypatch.setattr(sessao, "_ip_local", lambda: "127.0.0.1")
        assert sessao.iniciar_pareamento("groq")[0]
        sessao.encerrar()
        assert sessao.pareamento.situacao == ENCERRADA


# --- Rotas da API ---------------------------------------------------------------------------------


@pytest.fixture
def servidor(tmp_path):
    sessao = sessao_com_analise(tmp_path, resposta("chat_gemini_ok"))
    srv = ServidorUI(sessao, porta=0)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()
    sessao.encerrar()


def api(srv: ServidorUI, caminho: str, corpo: dict | None = None, token: str | None = ""):
    dados = json.dumps(corpo).encode() if corpo is not None else None
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["X-MSscan-Token"] = token or srv.token
    req = urllib.request.Request(f"http://127.0.0.1:{srv.porta}/api/{caminho}", data=dados, headers=headers,
                                 method="POST" if corpo is not None else "GET")  # fmt: skip
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as erro:
        return erro.code, json.loads(erro.read() or b"null")


class TestRotas:
    def test_chat_exige_token(self, servidor):
        assert api(servidor, "chat", token=None)[0] == 403
        assert api(servidor, "chat/enviar", {"texto": "oi"}, token="errado")[0] == 403

    def test_chat_enviar_e_ler(self, servidor):
        assert api(servidor, "chat/enviar", {})[0] == 400
        assert api(servidor, "chat/enviar", {"texto": "x" * 20001})[0] == 413  # corpo acima do limite da API
        status, _ = api(servidor, "chat/enviar", {"texto": "O que resolvo primeiro?"})
        assert status in (200, 202)
        sessao = servidor.sessao
        assert aguardar(lambda: sessao.chat["situacao"] == "pronto" and len(sessao.conversa.mensagens) == 3)
        status, dados = api(servidor, "chat")
        assert status == 200 and [m["papel"] for m in dados["mensagens"]] == ["ia", "tecnico", "ia"]
        sessao.chat["situacao"] = "digitando"
        assert api(servidor, "chat/enviar", {"texto": "outra"})[0] == 409

    def test_celular_rotas(self, servidor, porta_do_pareamento, monkeypatch):
        monkeypatch.setattr(servidor.sessao, "_ip_local", lambda: "127.0.0.1")
        assert api(servidor, "ia/celular/iniciar", {})[0] == 400
        assert api(servidor, "ia/celular/iniciar", {"id": "nao-existe"})[0] == 409
        assert api(servidor, "ia/celular/iniciar", {"id": "groq"})[0] in (200, 202)
        assert api(servidor, "ia/celular/manter", {})[0] in (200, 202)
        assert api(servidor, "ia/celular/aceitar", {})[0] == 409  # nada recebido
        assert api(servidor, "ia/celular/recusar", {})[0] in (200, 202)
        assert api(servidor, "ia/celular/parar", {})[0] in (200, 202)
        assert servidor.sessao.pareamento.situacao != AGUARDANDO


# --- Frontend -------------------------------------------------------------------------------------

import re  # noqa: E402
from pathlib import Path  # noqa: E402

ESTATICOS = Path(__file__).parent.parent / "msscan/interface/estaticos"
HTML = com_textos((ESTATICOS / "index.html").read_text(encoding="utf-8"))
JS = com_textos((ESTATICOS / "app.js").read_text(encoding="utf-8"))
CSS = (ESTATICOS / "app.css").read_text(encoding="utf-8")


class TestFrontendChatCelular:
    def test_aba_chat_entre_analise_e_atualizacoes(self):
        abas = re.findall(r'data-tela="([a-z]+)"', HTML.split('id="abas"')[1].split("</ul>")[0])
        assert abas.index("chat") == abas.index("ia") + 1 and abas.index("atualizacoes") == abas.index("chat") + 1
        assert '"chat"' in JS.split("const TELAS")[1].split(";")[0]

    def test_chat_acessivel_enter_envia_shift_enter_quebra(self):
        assert 'id="chat-mensagens" role="log"' in HTML and 'id="chat-digitando" role="status" aria-live="polite"' in HTML
        assert 'ev.key === "Enter" && !ev.shiftKey' in JS and "<textarea" in HTML and "<form" not in HTML
        assert 'anunciar(T("Resposta da IA: "' in JS  # respostas anunciadas a leitores de tela

    def test_vai_sozinha_para_o_chat_quando_a_analise_termina(self):
        assert 'iaSituacaoAnterior === "enviando" && ia.situacao === "concluida"' in JS and 'ir("chat")' in JS

    def test_evidencia_nao_confirmada_marcada_com_icone_e_texto(self):
        assert '" — não confirmada"' in JS and 'icone("alert")' in JS.split("function textoComMarcas")[1].split("\n}\n")[0]

    def test_celular_sem_foco_inicial_e_aceitar_nao_e_primario(self):
        botao = re.search(r'<button[^>]*id="ia-celular-aceitar"[^>]*>', HTML).group(0)
        assert "botao-secundario" in botao and "autofocus" not in HTML
        assert 'ia/celular/manter' in JS and 'window.addEventListener("pagehide", pararCelular)' in JS
        assert "config/ia.conf" in HTML.split('id="ia-celular"')[1].split("</div>")[0]

    def test_uma_acao_principal_por_tela(self):
        telas = re.split(r'<section class="tela" id="tela-', HTML)[1:]
        for tela in telas:
            nome = tela.split('"')[0]
            if nome == "ia":  # análise e "Configurar IA" são duas telas na mesma seção
                analise, config = tela.split('id="ia-config-area"')
                partes = {"ia": analise, "ia/configurar": config}
            else:
                partes = {nome: tela}
            for rotulo, trecho in partes.items():
                # "Tentar novamente" (hidden) só aparece NO LUGAR de "Enviar para análise"
                visiveis = [b for b in re.findall(r"<button[^>]*botao-primario[^>]*>", trecho) if " hidden" not in b]
                assert len(visiveis) <= 1, rotulo

    def test_visual_limpo_gradiente_so_no_cabecalho_e_destaques(self):
        sem_comentarios = re.sub(r"/\*.*?\*/", "", CSS, flags=re.DOTALL)
        usos = re.findall(r"^([^{}]+)\{[^}]*var\(--ms-grad-", sem_comentarios, flags=re.MULTILINE)
        # 27 (09/10/2026) prevalece sobre a 15.4: fundo é o papel de parede e a barra substituiu o cabeçalho. A borda ativa do
        # cartão usa --ms-borda-ativa (a regra antiga com --ms-grad-borda era sobrescrita e saiu na consolidação de 14/10).
        assert sorted({u.strip() for u in usos}) == [".faixa"]
        espacos = re.findall(r"(?:gap|padding|margin)[a-z-]*:\s*([^;]+);", sem_comentarios)
        fora = {v for valor in espacos for v in re.findall(r"(\d+)px", valor) if int(v) % 8 and int(v) not in (1, 2, 4)}  # 1 px: .sr-only
        assert not fora, fora

    def test_mostrar_menos_mais_e_modo_leve(self):
        """15.4: cartões e listas recolhíveis (lembrado na sessão), confirmações nunca recolhem; fps baixo → leve."""
        assert '.cartao:not(.cartao-perigo):not(.chat)' in JS and "sessionStorage.setItem(\"msscan-\" + chave" in JS
        assert "!i.classList.contains(\"alem-limite\")" in JS  # setas não param em item escondido
        assert '!item.querySelector("input:checked")' in JS  # item marcado para atualizar nunca fica escondido
        assert "fps < FPS_MINIMO" in JS and "prefers-reduced-motion: reduce" in JS
        assert "body.leve *" in CSS and 'id="sobre-fps"' in HTML

    def test_barra_de_etapas(self):
        assert 'id="etapas" aria-label="Etapas"' in HTML
        nomes = re.findall(r'\["[a-z]+", T\("([^"]+)"\), "[a-z]+"\]', JS.split("const ETAPAS")[1].split(";")[0])
        assert nomes == ["Rede", "Diagnóstico", "Análise por IA", "Conversa", "Atualizações", "Relatório"]
        assert '"aria-current": id === atual ? "step" : null' in JS and "(concluída)" in JS
