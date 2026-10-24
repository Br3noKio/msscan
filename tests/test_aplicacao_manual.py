"""Assistente do pacote manual (fase 11): SHA-256 antes de executar, DUP -c, instalar com travas e iDRAC7/8."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

import pytest

from msscan.atualizacao import aplicacao_manual as fluxo
from msscan.atualizacao import manual
from msscan.atualizacao.aplicacao import MODO_APLICAR, MODO_SIMULAR
from msscan.core.executor import (
    CAMINHO_PUSH_LEGADO,
    CAMINHO_SIMPLE_UPDATE,
    ENVIAR_FIRMWARE_LEGADO,
    MOTIVO_TRAVA,
    SIMPLE_UPDATE,
    ExecutorReal,
    RespostaRedfish,
    corpo_simple_update,
    validar_escrita_bmc,
)
from tests.conftest import ExecutorFalso, hardware_saudavel
from tests.test_aplicacao import BASE, TAREFA, BMCFalso, contexto
from tests.test_atualizacao_manual import SKU_R620, XML_BIOS, bin_falso, bios

PRE_OK = {"bloqueios": [], "redundancia_perdida": False}
BMC_LEGADO = {"redfish": True, "multipart": None, "simple_update": CAMINHO_SIMPLE_UPDATE, "administrador": True,
              "sistema": "/redfish/v1/Systems/437XR1138R2", "boot_usb": True}  # fmt: skip


def pacote_bin(tmp_path: Path, nome: str = "BIOS_W1WJW_LN_2.9.0.BIN") -> tuple[Path, str]:
    arquivo = tmp_path / nome
    arquivo.write_bytes(bin_falso(XML_BIOS))
    return arquivo, hashlib.sha256(arquivo.read_bytes()).hexdigest()


class DupFalso(ExecutorFalso):
    """Executor que registra as execuções do DUP; ``alvo_simulado`` libera a trava só nos testes."""

    alvo_simulado = True

    def __init__(self, rc_checar=0, rc_aplicar=2):
        super().__init__()
        self.rc_checar, self.rc_aplicar, self.execucoes = rc_checar, rc_aplicar, []

    def _rodar(self, comando, timeout):
        self.execucoes.append(list(comando))
        if comando == ["systemctl", "reboot"]:
            return 0, "", "", 0.0
        return (self.rc_checar if comando[-1] == "-c" else self.rc_aplicar), "saída do DUP", "", 0.0


INSTALADA_MAIS_VELHA = (bios("2.7.0"),)
"""Inventário com a BIOS mais velha que a do pacote (2.9.0): versão incerta é recusada (CLAUDE.md 11.4)."""


def pronto(tmp_path, executor, inventario=INSTALADA_MAIS_VELHA, sku=SKU_R620):
    arquivo, sha = pacote_bin(tmp_path)
    analise = fluxo.analisar(arquivo, sku, list(inventario), None)
    return fluxo.checar(fluxo.conferir_sha(analise, sha), executor)


class TestAssistente:
    def test_nada_executa_antes_do_sha(self, tmp_path):
        executor = DupFalso()
        arquivo, sha = pacote_bin(tmp_path)
        analise = fluxo.analisar(arquivo, SKU_R620, [bios("2.7.0")], None)
        assert analise["etapa"] == "sha" and analise["metodo"] == manual.DUP_LINUX and analise["disponivel"] == "2.9.0"
        assert fluxo.checar(analise, executor) is analise and executor.execucoes == []  # sem SHA, sem -c
        analise = fluxo.conferir_sha(analise, "texto qualquer")
        assert analise["etapa"] == "sha" and "64 caracteres" in analise["motivo"] and arquivo.exists()
        analise = fluxo.checar(fluxo.conferir_sha(analise, f"SHA-256: {sha.upper()}"), executor)
        assert analise["etapa"] == "pronto" and executor.execucoes == [["bash", str(arquivo), "-c"]]

    def test_sha_divergente_apaga_e_recusa(self, tmp_path):
        arquivo, _ = pacote_bin(tmp_path)
        analise = fluxo.conferir_sha(fluxo.analisar(arquivo, SKU_R620, list(INSTALADA_MAIS_VELHA), None), "0" * 64)
        assert analise["etapa"] == "recusado" and "apagado" in analise["motivo"] and not arquivo.exists()

    @pytest.mark.parametrize("rc, trecho", [(3, "igual ou mais antiga"), (5, "não se aplica"), (4, "pré-requisito"), (1, "erro")])
    def test_dup_c_recusa_pela_tabela_oficial(self, tmp_path, rc, trecho):
        analise = pronto(tmp_path, DupFalso(rc_checar=rc))
        assert analise["etapa"] == "recusado" and trecho in analise["motivo"] and analise["checagem"]["codigo"] == rc

    def test_recusas_sem_executar(self, tmp_path):
        arquivo, _ = pacote_bin(tmp_path)
        assert "R620 (04CE)" in fluxo.analisar(arquivo, "SKU=0716;ModelName=PowerEdge R640", [], None)["motivo"]
        assert "downgrade" in fluxo.analisar(arquivo, SKU_R620, [bios("2.9.0")], None)["motivo"]
        outro = tmp_path / "manual.pdf"
        outro.write_bytes(b"%PDF")
        assert "não reconhecido" in fluxo.analisar(outro, SKU_R620, [], None)["motivo"]
        exe = tmp_path / "BIOS_W1WJW_WN64_2.9.0.EXE"
        exe.write_bytes(b"MZ")
        assert "não confirmada" in fluxo.analisar(exe, SKU_R620, [], {"redfish": True})["motivo"]


class TestInstalarDup:
    def test_simulacao_nao_executa_o_pacote(self, tmp_path):
        executor = DupFalso()
        analise = pronto(tmp_path, executor)
        resultado = fluxo.instalar(analise, None, executor, PRE_OK, "ATUALIZAR", "", str(tmp_path), MODO_SIMULAR, [], {})
        assert resultado["situacao"] == "simulada" and all(c[-1] == "-c" for c in executor.execucoes)
        assert "-q" in json.dumps(resultado["passos"])

    @pytest.mark.parametrize("confirmacao, pre, trecho", [
        ("atualizar", PRE_OK, "ATUALIZAR"),
        ("ATUALIZAR", {"bloqueios": ["RAID degradado"]}, "RAID degradado"),
        ("ATUALIZAR", {"bloqueios": [], "redundancia_perdida": True}, "SEM REDUNDANCIA"),
    ])  # fmt: skip
    def test_confirmacoes_e_bloqueios(self, tmp_path, confirmacao, pre, trecho):
        executor = DupFalso()
        resultado = fluxo.instalar(pronto(tmp_path, executor), None, executor, pre, confirmacao, "", str(tmp_path), MODO_APLICAR, [], {})
        assert resultado["situacao"] == "bloqueada" and trecho in resultado["mensagem"]
        assert all(c[-1] == "-c" for c in executor.execucoes)

    def test_aplicar_reinicio_e_retomada(self, tmp_path):
        executor = DupFalso(rc_aplicar=2)
        resultado = fluxo.instalar(pronto(tmp_path, executor), None, executor, PRE_OK, "ATUALIZAR", "", str(tmp_path), MODO_APLICAR, [], {})
        assert resultado["situacao"] == "aguardando_reinicio" and resultado["itens"][0]["codigo_dup"]["nome"] == "REBOOT_REQUIRED"
        assert executor.execucoes[-2][-1] == "-q" and executor.execucoes[-1] == ["systemctl", "reboot"]
        assert json.loads((tmp_path / "estado/atualizacao_pendente.json").read_text())["metodo"] == "manual"

    def test_dup_falhou(self, tmp_path):
        executor = DupFalso(rc_aplicar=1)
        resultado = fluxo.instalar(pronto(tmp_path, executor), None, executor, PRE_OK, "ATUALIZAR", "", str(tmp_path), MODO_APLICAR, [], {})
        assert resultado["situacao"] == "falhou" and ["systemctl", "reboot"] not in executor.execucoes


    def test_pacote_alterado_depois_do_sha_nao_executa(self, tmp_path):
        """Revisão independente (MÉDIO 3): SHA-256 recalculado logo antes do -q."""
        executor = DupFalso(rc_aplicar=2)
        analise = pronto(tmp_path, executor)
        with open(analise["arquivo"], "ab") as arquivo:
            arquivo.write(b"trocado")
        resultado = fluxo.instalar(analise, None, executor, PRE_OK, "ATUALIZAR", "", str(tmp_path), MODO_APLICAR, [], {})
        assert resultado["situacao"] == "falhou" and "mudou depois da verificação de integridade" in resultado["mensagem"]
        assert all(c[-1] != "-q" for c in executor.execucoes)


class TestTravaDoDup:
    def test_executor_real_nunca_aplica(self, tmp_path, monkeypatch):
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("executaria o pacote"))
        executor = ExecutorReal()
        assert executor.aplicar_dup(str(tmp_path / "x.BIN")) == (-1, "", MOTIVO_TRAVA)
        analise = {**pronto(tmp_path, DupFalso()), "etapa": "pronto"}
        resultado = fluxo.instalar(analise, None, executor, PRE_OK, "ATUALIZAR", "", str(tmp_path), MODO_APLICAR, [], {})
        assert resultado["situacao"] == "bloqueada_trava" and MOTIVO_TRAVA in resultado["mensagem"]

    @pytest.mark.parametrize("comando", [
        ["bash", "/p/BIOS.BIN", "-q"], ["sh", "/p/BIOS.BIN", "-q", "-f"], ["/p/BIOS.BIN", "-r", "-q"], ["/p/BIOS.BIN"],
        ["bash", "/p/bios.bin", "-c", "-q"], ["bash", "-x", "/p/BIOS.BIN", "-q"], ["bash", "-x", "/p/BIOS.BIN", "-c"],
    ])  # fmt: skip
    def test_rodar_recusa_dup_que_grava(self, comando, monkeypatch):
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("executaria o pacote"))
        rc, _, erro = ExecutorReal().rodar(comando)
        assert rc == -1 and "DUP que grava" in erro

    def test_rodar_permite_leitura_do_dup(self):
        executor = ExecutorFalso(comandos={("bash", "/p/BIOS.BIN", "-c"): (0, "ok", ""), ("bash", "/p/x.bin", "--version"): (0, "", "")})
        assert executor.checar_dup("/p/BIOS.BIN") == (0, "ok", "")
        assert executor.rodar(["bash", "/p/x.bin", "--version"])[0] == 0
        assert executor.rodar(["grep", "-q", "algo", "/p/x.bin"])[0] == -1  # não é execução de DUP: só não programado


# --- iDRAC7/8: upload + SimpleUpdate ---------------------------------------------------------------


class BMCLegado(BMCFalso):
    """iDRAC7/8 falso: upload devolve 201 + Id "Available-…"; o pacote enviado tem versão e SoftwareId."""

    def __init__(self, versao_enviada="2.9.0"):
        super().__init__()
        disponivel = CAMINHO_PUSH_LEGADO + "/Available-159-2.9.0"
        self.respostas_redfish[("GET", BASE + disponivel)] = RespostaRedfish(
            BASE + disponivel, 200, corpo={"Id": "Available-159-2.9.0", "SoftwareId": "159", "Version": versao_enviada})

    def _bmc_escrita(self, operacao, metodo, url, token, corpo, arquivo, timeout):
        self.escritas.append((operacao, url, corpo, arquivo))
        if operacao == ENVIAR_FIRMWARE_LEGADO:
            return RespostaRedfish(url, 201, corpo={"Id": "Available-159-2.9.0"})
        return RespostaRedfish(url, 202 if operacao == SIMPLE_UPDATE else 200, local=TAREFA if operacao == SIMPLE_UPDATE else "")


def exe_pronto(tmp_path):
    exe = tmp_path / "BIOS_W1WJW_WN64_2.9.0.EXE"
    exe.write_bytes(b"MZ pacote de teste")
    analise = fluxo.analisar(exe, SKU_R620, [], BMC_LEGADO)
    return fluxo.checar(fluxo.conferir_sha(analise, hashlib.sha256(exe.read_bytes()).hexdigest()), None)


class TestIdracLegado:
    def test_salvaguardas(self):
        imagem = CAMINHO_PUSH_LEGADO + "/Available-159-2.9.0"
        assert validar_escrita_bmc(ENVIAR_FIRMWARE_LEGADO, BASE + CAMINHO_PUSH_LEGADO, None, "f.EXE") == ""
        assert "sem corpo" in validar_escrita_bmc(ENVIAR_FIRMWARE_LEGADO, BASE + CAMINHO_PUSH_LEGADO, {"x": 1}, "f")
        assert "só para" in validar_escrita_bmc(ENVIAR_FIRMWARE_LEGADO, BASE + "/redfish/v1/Systems/1", None, "f")
        assert validar_escrita_bmc(SIMPLE_UPDATE, BASE + CAMINHO_SIMPLE_UPDATE, corpo_simple_update(imagem)) == ""
        for corpo in ({"ImageURI": imagem, "@Redfish.OperationApplyTime": "Immediate"},
                      corpo_simple_update("http://10.0.0.9/BIOS.EXE"), corpo_simple_update(CAMINHO_PUSH_LEGADO + "/Installed-159")):
            assert "OnReset" in validar_escrita_bmc(SIMPLE_UPDATE, BASE + CAMINHO_SIMPLE_UPDATE, corpo)

    def test_executor_real_nunca_envia(self, monkeypatch):
        import requests

        monkeypatch.setattr(requests.Session, "post", lambda *a, **k: pytest.fail("chegou à rede"))
        monkeypatch.setattr(requests.Session, "get", lambda *a, **k: pytest.fail("chegou à rede"))
        resposta = ExecutorReal().bmc_escrita(ENVIAR_FIRMWARE_LEGADO, BASE + CAMINHO_PUSH_LEGADO, "tok", None, "/tmp/x.EXE")
        assert resposta.erro_tipo == "trava"

    def test_simulacao_descreve(self, tmp_path):
        analise = exe_pronto(tmp_path)
        assert analise["etapa"] == "pronto" and analise["metodo"] == manual.REDFISH_IDRAC_LEGADO
        executor = BMCLegado()
        resultado = fluxo.instalar(analise, contexto(executor), executor, PRE_OK, "ATUALIZAR", "", str(tmp_path), MODO_SIMULAR,
                                   [bios("2.7.0", component_id="159")], BMC_LEGADO)  # fmt: skip
        assert resultado["situacao"] == "simulada" and executor.escritas == []
        assert "SimpleUpdate" in json.dumps(resultado["passos"])

    def test_aplicar_confere_a_versao_antes_do_simple_update(self, tmp_path):
        executor = BMCLegado()
        resultado = fluxo.instalar(exe_pronto(tmp_path), contexto(executor), executor, PRE_OK, "ATUALIZAR", "", str(tmp_path),
                                   MODO_APLICAR, [bios("2.7.0", component_id="159")], BMC_LEGADO, dormir=lambda _: None)  # fmt: skip
        assert [e[0] for e in executor.escritas] == [ENVIAR_FIRMWARE_LEGADO, SIMPLE_UPDATE, "boot_unico_usb"]
        assert executor.escritas[1][2] == corpo_simple_update(CAMINHO_PUSH_LEGADO + "/Available-159-2.9.0")
        assert resultado["situacao"] == "aguardando_reinicio" and resultado["itens"][0]["disponivel"] == "2.9.0"

    def test_simple_update_aceito_e_sem_resposta_fica_possivelmente_agendado(self, tmp_path):
        """Revisão independente (ALTO 2): SimpleUpdate aceito e tarefa sem resposta → estado salvo, sem reinício."""

        class SemResposta(BMCLegado):
            def _redfish(self, metodo, url, token, corpo, timeout):
                if metodo == "GET" and url == BASE + TAREFA:
                    return RespostaRedfish(url, 503, erro_tipo="http", erro="ocupado")
                return super()._redfish(metodo, url, token, corpo, timeout)

        executor = SemResposta()
        resultado = fluxo.instalar(exe_pronto(tmp_path), contexto(executor), executor, PRE_OK, "ATUALIZAR", "", str(tmp_path),
                                   MODO_APLICAR, [bios("2.7.0", component_id="159")], BMC_LEGADO, dormir=lambda _: None)  # fmt: skip
        assert resultado["situacao"] == "possivelmente_agendado" and "Job Queue" in resultado["mensagem"]
        assert (tmp_path / "estado" / "atualizacao_pendente.json").exists()
        assert "boot_unico_usb" not in [e[0] for e in executor.escritas] and ["systemctl", "reboot"] not in executor.comandos_rodados()

    @pytest.mark.parametrize("instalada", ["2.9.0", "3.0.0", None])
    def test_versao_nao_mais_nova_nao_aplica(self, tmp_path, instalada):
        executor = BMCLegado()
        inventario = [bios(instalada, component_id="159")] if instalada else []
        resultado = fluxo.instalar(exe_pronto(tmp_path), contexto(executor), executor, PRE_OK, "ATUALIZAR", "", str(tmp_path),
                                   MODO_APLICAR, inventario, BMC_LEGADO, dormir=lambda _: None)  # fmt: skip
        assert [e[0] for e in executor.escritas] == [ENVIAR_FIRMWARE_LEGADO] and resultado["situacao"] == "bloqueada"
        assert re.search("nada foi aplicado", resultado["mensagem"])


class TestModoTexto:
    @pytest.fixture(autouse=True)
    def inventario_com_bios_velha(self, monkeypatch):
        import main

        monkeypatch.setattr(main, "coletar_inventario", lambda _ctx: (list(INSTALADA_MAIS_VELHA), "teste"))

    def rodar(self, tmp_path, respostas, capsys):
        import argparse

        from main import etapa_manual
        from msscan.deteccao.contexto import Contexto
        from msscan.pendrive.local import local_manual
        from msscan.relatorio.completo import Gravacao

        executor = DupFalso()
        pasta = tmp_path / "firmware/manual"
        pasta.mkdir(parents=True)
        (pasta / "BIOS_W1WJW_LN_2.9.0.BIN").write_bytes(bin_falso(XML_BIOS))
        ctx = Contexto(executor=executor, fabricante="Dell Inc.", modelo="PowerEdge R620", sku=SKU_R620)
        relatorio: dict = {}
        fila = iter(respostas)
        etapa_manual(argparse.Namespace(replay=None, aplicar=False, simular=True), executor, ctx, relatorio, "t", local_manual(str(tmp_path)),
                     Gravacao(None, False, ""), entrada=lambda _: next(fila))  # fmt: skip
        return executor, relatorio, capsys.readouterr().out

    def test_padrao_nao(self, tmp_path, capsys):
        executor, relatorio, saida = self.rodar(tmp_path, [""], capsys)
        assert "aplicacao" not in relatorio and "firmware/manual/" in saida and "product/poweredge-r620/drivers" in saida
        assert not any(c[-1] in ("-c", "-q") for c in executor.execucoes)

    def test_simula_com_sha_e_atualizar(self, tmp_path, capsys):
        sha = hashlib.sha256(bin_falso(XML_BIOS)).hexdigest()
        executor, relatorio, saida = self.rodar(tmp_path, ["s", sha, "ATUALIZAR"], capsys)  # plano + ATUALIZAR (18.2)
        assert relatorio["aplicacao"]["situacao"] == "simulada" and "SHA-256 confere" in saida
        assert [c[-1] for c in executor.execucoes if c[0] == "bash"] == ["-c"]

    def test_sha_errado_nao_executa(self, tmp_path, capsys):
        executor, relatorio, saida = self.rodar(tmp_path, ["s", "f" * 64], capsys)
        assert "divergente" in saida and not any(c[0] == "bash" for c in executor.execucoes) and "aplicacao" not in relatorio


def test_dup_com_versao_incerta_so_fica_pronto_se_o_pacote_confirmar(tmp_path):
    """Pedido de 01/10/2026 (3b; antes MÉDIO 6 recusava logo): sem a BIOS no inventário, a versão fica incerta e NÃO
    é instalável; o SHA-256 e a checagem do próprio pacote (DUP -c) decidem: aplicável → confirmada; código 3 → recusado."""
    arquivo, sha = pacote_bin(tmp_path)
    analise = fluxo.analisar(arquivo, SKU_R620, [], None)
    assert analise["etapa"] == "sha" and analise["versao"] == "incerta"
    pronta = fluxo.checar(fluxo.conferir_sha(analise, sha), DupFalso(rc_checar=0))
    assert pronta["etapa"] == "pronto" and pronta["versao"] == "atualizacao" and pronta["versao_confirmada_por"] == "DUP -c"
    igual = fluxo.checar(fluxo.conferir_sha(analise, sha), DupFalso(rc_checar=3))
    assert igual["etapa"] == "recusado" and "igual ou mais antiga" in igual["motivo"]


def test_bin_sem_package_xml_usa_a_versao_do_catalogo(tmp_path):
    """3b: sem package.xml, a entrada do catálogo com o mesmo release ID dá a versão (o nome do arquivo só apoia)."""
    arquivo = tmp_path / "BIOS_W1WJW_LN64_2.9.0.BIN"
    arquivo.write_bytes(b"#!/bin/sh\nexit 0\n")
    catalogo = {"componentes": [{"caminho": "FOLDER1/BIOS_W1WJW_WN64_2.9.0.EXE", "versao": "2.9.0", "tipo": "BIOS",
                                 "nome": "BIOS", "sistemas": [{"system_id": "04CE", "modelo": "R620"}], "dispositivos": [],
                                 "reinicio": True, "data": "2026-01-01"}]}  # fmt: skip
    com = fluxo.analisar(arquivo, SKU_R620, list(INSTALADA_MAIS_VELHA), None, catalogo)
    assert com["versao"] == "atualizacao" and "catálogo oficial" in com["motivo_versao"] and com["modelo_ok"] is True
    sem = fluxo.analisar(arquivo, SKU_R620, list(INSTALADA_MAIS_VELHA), None, None)
    assert sem["versao"] == "incerta" and "só apoio" in sem["motivo_versao"] and sem["etapa"] == "sha"


@pytest.fixture(autouse=True)
def _hardware_saudavel(monkeypatch):
    """Tela de aviso: hardware do BMC falso tratado como OK (a lógica do aviso é testada em test_aviso.py)."""
    hardware_saudavel(monkeypatch)
