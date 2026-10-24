"""Atualização manual na interface (fase 11): pacote na pasta do pendrive → SHA-256 → DUP -c → instalar (simulado)."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

import pytest

from msscan.atualizacao import aviso as modulo_aviso
from msscan.interface import manual as mod_manual
from msscan.interface.servidor import ServidorUI
from tests.test_atualizacao_manual import XML_BIOS, bin_falso
from tests.test_ia import TestSessaoIA
from tests.test_interface_chat_celular import aguardar, api

XML_R640 = XML_BIOS.replace("04CE".encode("utf-16-le"), "0716".encode("utf-16-le")).replace(
    'vendorVersion="2.9.0"'.encode("utf-16-le"), 'vendorVersion="2.99.1"'.encode("utf-16-le"))
"""package.xml real do BIOS do R620 com o systemID do R640 (0716) e versão 2.99.1: um BIOS "mais novo" para o R640."""


@pytest.fixture
def sessao(tmp_path):
    sessao = TestSessaoIA().sessao(tmp_path, [])
    yield sessao
    sessao.encerrar()


def colocar(sessao, nome: str, conteudo: bytes) -> tuple[Path, str]:
    pasta = Path(sessao.pasta_dados) / "firmware/manual"
    pasta.mkdir(parents=True, exist_ok=True)
    (pasta / nome).write_bytes(conteudo)
    return pasta / nome, hashlib.sha256(conteudo).hexdigest()


def pacote(sessao, indice=0):
    return sessao.estado()["atualizacoes"]["manual"]["pacotes"][indice]


class TestAssistenteNaSessao:
    def test_fluxo_completo_simulado(self, sessao, monkeypatch):
        chamadas = []
        monkeypatch.setattr(sessao.executor, "checar_dup", lambda caminho: chamadas.append(caminho) or (0, "pode ser aplicado", ""))
        arquivo, sha = colocar(sessao, "BIOS_W1WJW_LN_2.99.1.BIN", bin_falso(XML_R640))
        sessao.manual.procurar()
        sessao.manual.procurar()  # não duplica
        manual = sessao.estado()["atualizacoes"]["manual"]
        assert len(manual["pacotes"]) == 1 and manual["pasta"].endswith("firmware/manual")
        p = pacote(sessao)
        assert p["etapa"] == "sha" and p["modelo_ok"] is True and p["instalada"] == "2.21.2" and "arquivo" not in p
        ok, motivo = sessao.manual.informar_sha("p1", "0" * 10)
        assert not ok and "64 caracteres" in motivo and chamadas == []
        ok, _ = sessao.manual.informar_sha("p1", sha)
        assert ok and aguardar(lambda: pacote(sessao)["etapa"] == "pronto") and chamadas == [str(arquivo)]
        ok, mensagem = sessao.manual.instalar("p1", "ATUALIZAR", "SEM REDUNDANCIA")
        assert not ok and "plano de atualização" in mensagem  # 18.2: sem o plano, nada é instalado
        assert sessao.preparar_aviso("manual", ["p1"])[0]
        sessao.aguardar(30)
        aviso = sessao.aviso
        assert aviso["situacao"] == "pronto" and aviso["redundancia"], aviso  # R640 do replay: uma fonte sem AC, relida na hora
        assert [h["item"] for h in aviso["hardware"]] == ["Fontes", "Memória", "Configuração de memória", "RAID",
                                                     "Temperatura e sensores", "Ventoinhas", "Registro de eventos (eventos anteriores)"]
        # O SEL real do R640 tem "Uncorrectable ECC" no DIMM A2, sem confirmação agora (EDAC sem erro): HISTÓRICO, não
        # bloqueia (pedido de 01/10/2026), mas exige "Estou ciente de que é um evento antigo".
        assert aviso["historico"] and not any(b.startswith("Memória:") for b in aviso["bloqueios"])
        # 09/10/2026: a fixture tem 2 processadores e 2 pentes, ambos na CPU1 (A1, A2): a configuração de memória, calculada agora com os slots
        # lidos, fica fora do suportado — risco que o técnico pode assumir, nunca bloqueio fixo.
        montagem = next(h for h in aviso["hardware"] if h["item"] == "Configuração de memória")
        assert montagem["situacao"] == "bloqueia" and "CPU1: A1, A2 (16 GB)" in montagem["texto"] and "CPU2: nenhum pente" in montagem["texto"]
        assert not aviso["pode_seguir"] and aviso["pode_assumir"] and aviso["bloqueios_fixos"] == []
        assert [r["item"] for r in aviso["riscos"]] == ["Configuração de memória"] and "não iniciar" in aviso["riscos"][0]["risco"]
        ok, mensagem = sessao.manual.instalar("p1", "ATUALIZAR", "SEM REDUNDANCIA")
        assert not ok and "assumindo os riscos" in mensagem
        ok, mensagem = sessao.manual.instalar("p1", "ATUALIZAR", "SEM REDUNDANCIA", "ASSUMO OS RISCOS")
        assert not ok and "evento antigo" in mensagem
        assert sessao.preparar_aviso("manual", ["p1"])[0]
        sessao.aguardar(30)
        assert sessao.marcar_ciente(True)
        ok, mensagem = sessao.manual.instalar("p1", "ATUALIZAR", "SEM REDUNDANCIA", "ASSUMO OS RISCOS")
        assert ok, mensagem
        sessao.aguardar(30)
        assert sessao.aplicacao["situacao"] == "simulada" and sessao.relatorio["aplicacao"]["metodo"] == "manual"
        assert pacote(sessao)["etapa"] == "instalado"
        riscos = sessao.relatorio["aplicacao"]["riscos_assumidos"]
        assert riscos["assumidos"] and riscos["checagens"][0]["item"] == "Configuração de memória" and riscos["quando"]

    def test_confirmacao_errada_nao_instala(self, sessao, monkeypatch):
        monkeypatch.setattr(sessao.executor, "checar_dup", lambda caminho: (0, "", ""))
        _, sha = colocar(sessao, "BIOS_W1WJW_LN_2.99.1.BIN", bin_falso(XML_R640))
        sessao.manual.procurar()
        sessao.manual.informar_sha("p1", sha)
        aguardar(lambda: pacote(sessao)["etapa"] == "pronto")
        ok, motivo = sessao.manual.instalar("p1", "atualizar", "")
        assert not ok and "ATUALIZAR" in motivo and sessao.aplicacao["situacao"] == "nao_solicitada"

    def test_pacote_de_outro_modelo_recusado_sem_executar(self, sessao, monkeypatch):
        monkeypatch.setattr(sessao.executor, "checar_dup", lambda caminho: pytest.fail("executou o pacote"))
        colocar(sessao, "BIOS_W1WJW_LN_2.9.0.BIN", bin_falso(XML_BIOS))
        sessao.manual.procurar()
        p = pacote(sessao)
        assert p["etapa"] == "recusado" and "R620 (04CE)" in p["motivo"]
        assert sessao.manual.informar_sha("p1", "0" * 64)[0] is False

    def test_sha_divergente_apaga_do_pendrive(self, sessao):
        arquivo, _ = colocar(sessao, "BIOS_W1WJW_LN_2.99.1.BIN", bin_falso(XML_R640))
        sessao.manual.procurar()
        ok, motivo = sessao.manual.informar_sha("p1", "f" * 64)
        assert ok and "divergente" in motivo and not arquivo.exists() and pacote(sessao)["etapa"] == "recusado"

    def test_sem_navegador_fora_da_iso(self, sessao):
        ok, motivo = sessao.manual.abrir_navegador()
        assert not ok and "apenas no MSscan do pendrive" in motivo and "firmware/manual" in motivo

    def test_service_tag_so_na_url_e_nunca_no_estado(self, sessao, monkeypatch):
        abertos = []

        class NavegadorFalso:
            aberto = False

            def __init__(self, executor):
                pass

            def abrir(self, url, ao_baixar):
                abertos.append(url)
                return True, "ok"

            def estado(self):
                return {"aberto": True, "visivel": True, "url": "OCULTA"}

            def fechar(self):
                pass

        class ProxyFalso:
            def iniciar(self):
                pass

            def encerrar(self):
                pass

            def resumo(self):
                return {"permitidos": {}, "recusados": {}}

        monkeypatch.setattr(mod_manual, "Navegador", NavegadorFalso)
        monkeypatch.setattr(mod_manual, "ProxyOficial", ProxyFalso)
        sessao.manual.navegador_ativo = True
        monkeypatch.setattr(sessao.manual, "_service_tag", lambda: "ABC1234")
        ok, _ = sessao.manual.abrir_navegador()
        assert ok and abertos == ["https://www.dell.com/support/product-details/pt-br/servicetag/ABC1234/drivers"]
        assert "ABC1234" not in json.dumps(sessao.estado()) and "ABC1234" not in json.dumps(sessao.relatorio)


@pytest.fixture
def servidor(sessao):
    srv = ServidorUI(sessao, porta=0)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


class TestRotas:
    def test_rotas_exigem_token_e_campos(self, servidor):
        assert api(servidor, "manual/procurar", {}, token=None)[0] == 403
        assert api(servidor, "manual/sha", {"id": 5})[0] == 400
        assert api(servidor, "manual/navegador", {})[0] == 409
        assert api(servidor, "manual/sha", {"id": "p9", "sha": "x"})[0] == 409

    def test_voltar_sem_token_so_esconde(self, servidor, monkeypatch):
        import urllib.request

        chamado = []
        monkeypatch.setattr(servidor.sessao.manual, "voltar", lambda: chamado.append(1) or True)
        with urllib.request.urlopen(f"http://127.0.0.1:{servidor.porta}/voltar", timeout=5) as resposta:
            assert resposta.status == 204
        assert chamado == [1]

    def test_pagina_de_bloqueio_sem_token(self, servidor):
        import urllib.request

        with urllib.request.urlopen(f"http://127.0.0.1:{servidor.porta}/bloqueado?dominio=example.com", timeout=5) as resposta:
            corpo = resposta.read().decode()
            assert resposta.status == 200 and "script-src" not in resposta.headers["Content-Security-Policy"]
        assert "<title>Site bloqueado pelo MSscan: example.com</title>" in corpo

    def test_procurar_e_sha_pela_api(self, servidor, monkeypatch):
        sessao = servidor.sessao
        monkeypatch.setattr(sessao.executor, "checar_dup", lambda caminho: (0, "", ""))
        _, sha = colocar(sessao, "BIOS_W1WJW_LN_2.99.1.BIN", bin_falso(XML_R640))
        assert api(servidor, "manual/procurar", {})[0] in (200, 202)
        status, corpo = api(servidor, "manual/sha", {"id": "p1", "sha": sha.upper()})
        assert status == 200 and corpo["ok"]
        assert aguardar(lambda: pacote(sessao)["etapa"] == "pronto")
        time.sleep(0.05)


class TestPastaDeDownloads:
    """Pedido de 30/09/2026 (CLAUDE.md 15.2): "Atualizar" e "Abrir pasta de downloads", só em MSSCAN/firmware/manual/."""

    def test_lista_e_marca_o_que_ja_foi_usado(self, sessao):
        colocar(sessao, "BIOS_W1WJW_LN_2.99.1.BIN", bin_falso(XML_R640))
        colocar(sessao, "notas.txt", b"x" * 3000)
        sessao.manual.procurar()
        dados = sessao.manual.arquivos()
        por_nome = {a["nome"]: a for a in dados["arquivos"]}
        assert dados["pasta"].endswith("firmware/manual") and set(por_nome) == {"BIOS_W1WJW_LN_2.99.1.BIN", "notas.txt"}
        assert por_nome["notas.txt"]["tamanho"] == 3000 and por_nome["notas.txt"]["usado"] == "não"
        assert por_nome["BIOS_W1WJW_LN_2.99.1.BIN"]["usado"] == "verificado, não instalado" and "T" in por_nome["notas.txt"]["data"]

    @pytest.mark.parametrize("nome", ["../relatorios/x.json", "/etc/passwd", "..", ".", ".oculto", "sub/arq.BIN", "", "nao-existe.BIN"])
    def test_apagar_so_arquivo_da_pasta(self, sessao, nome):
        arquivo, _ = colocar(sessao, "BIOS_W1WJW_LN_2.99.1.BIN", b"abc")
        fora = Path(sessao.pasta_dados) / "relatorios" / "x.json"
        fora.parent.mkdir(parents=True, exist_ok=True)
        fora.write_text("{}")
        ok, _ = sessao.manual.apagar(nome, True)
        assert not ok and fora.exists() and arquivo.exists()

    def test_apagar_pede_confirmacao_e_nao_segue_link(self, sessao):
        arquivo, _ = colocar(sessao, "BIOS_W1WJW_LN_2.99.1.BIN", bin_falso(XML_R640))
        alvo = Path(sessao.pasta_dados) / "config.txt"
        alvo.write_text("importante")
        (arquivo.parent / "link.BIN").symlink_to(alvo)
        sessao.manual.procurar()
        assert sessao.manual.apagar("BIOS_W1WJW_LN_2.99.1.BIN", False) == (False, "Confirme para apagar.")
        assert not sessao.manual.apagar("link.BIN", True)[0] and alvo.exists()
        ok, mensagem = sessao.manual.apagar("BIOS_W1WJW_LN_2.99.1.BIN", True)
        assert ok and not arquivo.exists() and "apagado" in mensagem
        assert sessao.estado()["atualizacoes"]["manual"]["pacotes"] == []  # some da lista de pacotes também

    def test_atualizar_tira_da_lista_o_que_sumiu(self, sessao):
        arquivo, _ = colocar(sessao, "BIOS_W1WJW_LN_2.99.1.BIN", bin_falso(XML_R640))
        sessao.manual.procurar()
        assert len(sessao.estado()["atualizacoes"]["manual"]["pacotes"]) == 1
        arquivo.unlink()
        colocar(sessao, "BIOS_W1WJW_LN_2.99.2.BIN", bin_falso(XML_R640))
        sessao.manual.procurar()
        nomes = [p["nome"] for p in sessao.estado()["atualizacoes"]["manual"]["pacotes"]]
        assert len(nomes) == 1

    def test_rotas(self, servidor):
        colocar(servidor.sessao, "notas.txt", b"x")
        status, corpo = api(servidor, "manual/arquivos")
        assert status == 200 and corpo["arquivos"][0]["nome"] == "notas.txt"
        assert api(servidor, "manual/apagar", {"nome": "notas.txt"})[0] == 400  # sem confirmar
        assert api(servidor, "manual/apagar", {"nome": "../x", "confirmar": True})[0] == 409
        status, corpo = api(servidor, "manual/apagar", {"nome": "notas.txt", "confirmar": True})
        assert status == 200 and corpo["ok"]
