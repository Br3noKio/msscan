"""Fase 9: aplicação de firmware — trava da rodada final, salvaguardas, plano, simulação, hash, retomada.

Nenhum teste grava firmware: o caminho real só roda contra o BMC falso deste arquivo (``alvo_simulado = True``).
Respostas do BMC: arquivos oficiais da DMTF e o MOCKUP(rodada final) no formato da Dell (ver LEIAME da fixture).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

import pytest

from msscan.atualizacao import aplicacao as mod
from msscan.atualizacao.aplicacao import (
    CONFIRMACAO,
    CONFIRMACAO_REDUNDANCIA,
    MODO_APLICAR,
    MODO_SIMULAR,
    conferir_confirmacoes,
    executar_plano,
    montar_plano,
)
from msscan.atualizacao.retomada import APLICADA, NAO_APLICADA, NAO_VERIFICADA, arquivar_estado, conferir, ler_estado
from msscan.core import executor as modulo_executor
from msscan.core.executor import (
    BOOT_UNICO_USB,
    CORPO_BOOT_USB,
    ENVIAR_FIRMWARE,
    EXPORTAR_CONFIGURACAO,
    MOTIVO_TRAVA,
    PARAMETROS_UPDATE,
    ExecutorReal,
    RespostaDownload,
    RespostaRedfish,
    validar_escrita_bmc,
)
from msscan.deteccao.contexto import Contexto
from msscan.parsers.redfish import parse_boot, parse_tarefa, parse_update_service
from msscan.redfish.acesso import AcessoBMC
from msscan.redfish.cliente import ClienteRedfish
from msscan.relatorio.atualizacoes import formatar_aplicacao, formatar_retomada, html_aplicacao
from tests.conftest import PASTA_FIXTURES, ExecutorFalso, hardware_saudavel

DMTF = PASTA_FIXTURES / "redfish" / "dmtf" / "public_rackmount1_atualizacao"
DELL = PASTA_FIXTURES / "redfish" / "dell_idrac9_mockup"
SISTEMA_DMTF = json.loads((PASTA_FIXTURES / "redfish" / "dmtf" / "public_rackmount1" / "redfish.json").read_text())[
    "GET /redfish/v1/Systems/437XR1138R2"]["corpo"]  # fmt: skip
BASE = "https://bmc.teste"
TAREFA = "/redfish/v1/TaskService/Tasks/JID_000000000001"
CONTEUDO_NIC, CONTEUDO_BIOS = b"pacote de rede (teste)" * 100, b"pacote de BIOS (teste)" * 100


def ler(pasta: Path, nome: str):
    return json.loads((pasta / nome).read_text())


# --- BMC falso (só para testes; alvo_simulado) ----------------------------------------------------


class BMCFalso(ExecutorFalso):
    """Responde GETs do Redfish a partir das fixtures e registra as escritas. ``alvo_simulado``: passa pela trava."""

    alvo_simulado = True

    def __init__(self, tarefas=("Task_agendada.json",), com_exportacao=False, com_anterior=True):
        super().__init__(comandos={("systemctl", "reboot"): (0, "", "")})
        self.escritas: list[tuple[str, str, object, str | None]] = []
        self.tarefas = [ler(DELL, t) for t in tarefas]
        get = {
            "/redfish/v1/UpdateService": ler(DELL, "UpdateService.json"),
            "/redfish/v1/UpdateService/FirmwareInventory": {"Members": [
                {"@odata.id": "/redfish/v1/UpdateService/FirmwareInventory/Installed-25237-22.5.0"}]
                + ([{"@odata.id": "/redfish/v1/UpdateService/FirmwareInventory/Previous-25237-21.5.9"}] if com_anterior else [])},
            "/redfish/v1/UpdateService/FirmwareInventory/Installed-25237-22.5.0": ler(DELL, "FirmwareInventory_instalado.json"),
            "/redfish/v1/UpdateService/FirmwareInventory/Previous-25237-21.5.9": ler(DELL, "FirmwareInventory_anterior.json"),
            "/redfish/v1/Systems": {"Members": [{"@odata.id": "/redfish/v1/Systems/437XR1138R2"}]},
            "/redfish/v1/Systems/437XR1138R2": SISTEMA_DMTF,
            "/redfish/v1/Managers": {"Members": [{"@odata.id": "/redfish/v1/Managers/BMC"}]},
            "/redfish/v1/Managers/BMC": {"Id": "BMC", "Actions": {"Oem": {
                "#OemManager.v1_4_0.OemManager#OemManager.ExportSystemConfiguration": {
                    "target": "/redfish/v1/Managers/BMC/Actions/Oem/OemManager.ExportSystemConfiguration"}}} if com_exportacao else {}},
        }  # fmt: skip
        for caminho, corpo in get.items():
            self.respostas_redfish[("GET", BASE + caminho)] = RespostaRedfish(BASE + caminho, 200, corpo=corpo)
        sessao = BASE + "/redfish/v1/SessionService/Sessions"
        self.respostas_redfish[("POST", sessao)] = RespostaRedfish(sessao, 201, token="tok", local="/redfish/v1/SessionService/Sessions/1")

    def _redfish(self, metodo, url, token, corpo, timeout):
        if metodo == "GET" and url == BASE + TAREFA:
            corpo_tarefa = self.tarefas.pop(0) if len(self.tarefas) > 1 else self.tarefas[0]
            return RespostaRedfish(url, 200, corpo=corpo_tarefa)
        return super()._redfish(metodo, url, token, corpo, timeout)

    def _bmc_escrita(self, operacao, metodo, url, token, corpo, arquivo, timeout):
        self.escritas.append((operacao, url, corpo, arquivo))
        return RespostaRedfish(url, 202 if operacao != BOOT_UNICO_USB else 200, local=TAREFA if operacao != BOOT_UNICO_USB else "")

    def _baixar(self, url, destino, limite, timeout):
        conteudo = CONTEUDO_BIOS if "BIOS" in url else CONTEUDO_NIC
        Path(destino).parent.mkdir(parents=True, exist_ok=True)
        Path(destino).write_bytes(conteudo)
        return RespostaDownload(url, 200, destino, len(conteudo), hashlib.sha256(conteudo).hexdigest())


class BMCReal(BMCFalso):
    """Mesmo BMC, mas como um executor real: a trava da rodada final precisa segurar."""

    alvo_simulado = False


def contexto(executor, administrador=True):
    cliente = ClienteRedfish(executor, BASE)
    ctx = Contexto(executor=executor, fabricante="Dell Inc.", modelo="PowerEdge R640")
    ctx.bmc = AcessoBMC(cliente)
    ctx.bmc.tentar("tecnico", "senha")
    ctx.bmc.administrador = administrador
    return ctx


def item(identificador, componente, categoria, instalada, disponivel, conteudo, situacao="atualizacao", **pacote):
    return {"id": identificador, "componente": componente, "component_id": "25237" if categoria == "Network" else "159",
            "instalada": instalada, "disponivel": disponivel, "situacao": situacao, "texto_situacao": situacao,
            "pacote": {"url": f"https://downloads.dell.com/F/1/{componente.replace(' ', '_')}.EXE", "tamanho": len(conteudo),
                       "sha256": hashlib.sha256(conteudo).hexdigest(), "md5": hashlib.md5(conteudo).hexdigest(),
                       "categoria": categoria, "reinicio": True, **pacote}}  # fmt: skip


def verificacao(*itens, bloqueios=(), redundancia=False):
    return {"situacao": "verificada", "itens": list(itens), "bloqueios": list(bloqueios), "redundancia_perdida": redundancia}


NIC = item("item1", "Rede X710", "Network", "22.5.0", "22.5.7", CONTEUDO_NIC)
BIOS = item("item2", "BIOS", "BIOS", "2.21.2", "2.28.1", CONTEUDO_BIOS)
LIVRE = lambda _: 10 * 2**30  # noqa: E731


# --- Trava da rodada final e salvaguardas ---------------------------------------------------------


class TestTrava:
    def test_trava_liberada_no_codigo_e_desligada_nos_testes(self):
        """Liberada em 30/09/2026 por ordem do usuário (15.6); nos testes, sempre forçada a False (conftest)."""
        assert modulo_executor.GRAVACAO_REAL_LIBERADA is False
        fonte = Path(modulo_executor.__file__).read_text(encoding="utf-8")
        assert re.search(r"^GRAVACAO_REAL_LIBERADA = True$", fonte, re.M)

    def test_executor_real_nunca_envia(self, monkeypatch):
        import requests

        monkeypatch.setattr(requests.Session, "request", lambda *a, **k: pytest.fail("chegou à rede"))
        monkeypatch.setattr(requests.Session, "post", lambda *a, **k: pytest.fail("chegou à rede"))
        executor = ExecutorReal()
        for operacao, caminho, corpo, arquivo in (
            (ENVIAR_FIRMWARE, "/redfish/v1/UpdateService/MultipartUpload", PARAMETROS_UPDATE, "/tmp/x.EXE"),
            (BOOT_UNICO_USB, "/redfish/v1/Systems/System.Embedded.1", CORPO_BOOT_USB, None),
        ):
            resposta = executor.bmc_escrita(operacao, BASE + caminho, "tok", corpo, arquivo)
            assert resposta.erro_tipo == "trava" and resposta.erro == MOTIVO_TRAVA and resposta.status is None
        assert executor.historico[-1].comando[-1] == f"({BOOT_UNICO_USB})" and "tok" not in json.dumps(executor.historico_dict())

    def test_reinicio_para_aplicar_travado(self, monkeypatch):
        import subprocess

        monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("reiniciaria o servidor"))
        assert ExecutorReal().reiniciar_para_aplicar() == (False, MOTIVO_TRAVA)

    def test_marcar_executor_real_como_simulado_nao_libera(self):
        class Disfarcado(ExecutorReal):
            alvo_simulado = True

        assert not Disfarcado().escrita_permitida()
        assert BMCFalso().escrita_permitida() and not BMCReal().escrita_permitida()

    @pytest.mark.parametrize(("operacao", "caminho", "corpo", "arquivo", "trecho"), [
        (ENVIAR_FIRMWARE, "/redfish/v1/UpdateService/MultipartUpload", {**PARAMETROS_UPDATE, "@Redfish.OperationApplyTime": "Immediate"}, "f", "OnReset"),
        (ENVIAR_FIRMWARE, "/redfish/v1/UpdateService/MultipartUpload", {**PARAMETROS_UPDATE, "Targets": ["/redfish/v1/x"]}, "f", "OnReset"),
        (ENVIAR_FIRMWARE, "/redfish/v1/Systems/1", PARAMETROS_UPDATE, "f", "MultipartHttpPushUri"),
        (ENVIAR_FIRMWARE, "/redfish/v1/UpdateService/MultipartUpload", PARAMETROS_UPDATE, None, "sem arquivo"),
        (BOOT_UNICO_USB, "/redfish/v1/Systems/1", {"Boot": {"BootSourceOverrideTarget": "Pxe", "BootSourceOverrideEnabled": "Once"}}, None, "USB"),
        (BOOT_UNICO_USB, "/redfish/v1/Systems/1", {**CORPO_BOOT_USB, "AssetTag": "x"}, None, "USB"),
        (BOOT_UNICO_USB, "/redfish/v1/Managers/1", CORPO_BOOT_USB, None, "Systems"),
        (EXPORTAR_CONFIGURACAO, "/redfish/v1/Managers/1/Actions/Oem/OemManager.ExportSystemConfiguration",
         {"ExportFormat": "JSON", "ShareParameters": {"Target": ["ALL"], "IPAddress": "10.0.0.9", "ShareType": "CIFS"}}, None, "local"),
        (EXPORTAR_CONFIGURACAO, "/redfish/v1/Managers/1/Actions/Oem/OemManager.ImportSystemConfiguration", {}, None, "ExportSystemConfiguration"),
        ("apagar_bios", "/redfish/v1/Systems/1", {}, None, "não permitida"),
    ])  # fmt: skip
    def test_salvaguardas(self, operacao, caminho, corpo, arquivo, trecho):
        assert trecho in validar_escrita_bmc(operacao, BASE + caminho, corpo, arquivo)
        resposta = BMCFalso().bmc_escrita(operacao, BASE + caminho, "tok", corpo, arquivo)
        assert resposta.erro_tipo == "bloqueado"

    def test_so_https_e_formatos_permitidos(self):
        assert "HTTPS" in validar_escrita_bmc(ENVIAR_FIRMWARE, "http://bmc/redfish/v1/UpdateService/M", PARAMETROS_UPDATE, "f")
        assert validar_escrita_bmc(ENVIAR_FIRMWARE, BASE + "/redfish/v1/UpdateService/MultipartUpload", PARAMETROS_UPDATE, "f") == ""
        assert validar_escrita_bmc(BOOT_UNICO_USB, BASE + "/redfish/v1/Systems/System.Embedded.1", CORPO_BOOT_USB) == ""
        assert validar_escrita_bmc(EXPORTAR_CONFIGURACAO, BASE + "/redfish/v1/Managers/iDRAC.Embedded.1/Actions/Oem/"
                                   "OemManager.ExportSystemConfiguration", {"ExportFormat": "JSON", "ShareParameters": {"Target": ["ALL"]}}) == ""  # fmt: skip


# --- Parsers --------------------------------------------------------------------------------------


class TestParsers:
    def test_update_service_dmtf_sem_multipart(self):
        servico = parse_update_service(ler(DMTF, "UpdateService.json"))
        assert servico["multipart"] is None and servico["http_push"] == "/FWUpdate" and "OnReset" in servico["tempos_aplicacao"]

    def test_update_service_dell_mockup(self):
        assert parse_update_service(ler(DELL, "UpdateService.json"))["multipart"] == "/redfish/v1/UpdateService/MultipartUpload"

    @pytest.mark.parametrize(("pasta", "arquivo", "esperado"), [
        (DMTF, "Task_545.json", "concluida"), (DELL, "Task_agendada.json", "agendada"),
        (DELL, "Task_concluida.json", "concluida"), (DELL, "Task_falhou.json", "falhou"),
    ])  # fmt: skip
    def test_tarefa(self, pasta, arquivo, esperado):
        tarefa = parse_tarefa(ler(pasta, arquivo))
        assert tarefa[esperado] and tarefa["terminou"]
        assert sum(tarefa[k] for k in ("concluida", "agendada", "falhou")) == 1

    def test_tarefa_em_andamento(self):
        tarefa = parse_tarefa({"Id": "1", "TaskState": "Running", "PercentComplete": 10, "Messages": []})
        assert not tarefa["terminou"] and tarefa["percentual"] == 10

    def test_boot_dmtf(self):
        boot = parse_boot(SISTEMA_DMTF)
        assert "Usb" in boot["alvos"] and boot["alvo"] == "Pxe"


# --- Plano e confirmações -------------------------------------------------------------------------


class TestPlano:
    def test_selecao_vazia_bloqueia(self):
        plano = montar_plano(verificacao(NIC), [])
        assert not plano["pode_seguir"] and "Nenhum item selecionado" in plano["bloqueios"][0]

    @pytest.mark.parametrize("situacao", ["igual", "incerta", "instalada_mais_nova", "sem_pacote"])
    def test_so_atualizacao_disponivel(self, situacao):
        plano = montar_plano(verificacao({**NIC, "situacao": situacao, "texto_situacao": situacao}), ["item1"])
        assert not plano["pode_seguir"] and "nunca downgrade" in plano["recusados"][0]

    def test_bloqueios_globais_e_por_item(self):
        assert not montar_plano(verificacao(NIC, bloqueios=["Fonte com DEFEITO"]), ["item1"])["pode_seguir"]
        plano = montar_plano(verificacao({**NIC, "bloqueios": ["RAID desconhecido"]}), ["item1"])
        assert not plano["pode_seguir"] and "RAID desconhecido" in plano["recusados"][0]
        assert not montar_plano({"situacao": "modelo_nao_confirmado", "itens": [NIC]}, ["item1"])["pode_seguir"]

    def test_sem_hash_oficial(self):
        sem_hash = {**NIC, "pacote": {**NIC["pacote"], "sha256": "", "md5": ""}}
        assert "sem SHA-256 oficial" in montar_plano(verificacao(sem_hash), ["item1"])["recusados"][0]

    def test_item_desconhecido_bloqueia_tudo(self):
        plano = montar_plano(verificacao(NIC), ["item1", "item99"])
        assert not plano["pode_seguir"] and "item99" in plano["recusados"][0]

    def test_ordem_rede_demais_idrac_e_bios_por_ultimo(self):
        """09/10/2026: placas de rede, demais, iDRAC (sozinho) e BIOS sempre por último."""
        idrac = item("item3", "iDRAC", "iDRAC with Lifecycle Controller", "6.1", "7.0", b"x")
        plano = montar_plano(verificacao(BIOS, idrac, NIC), ["item2", "item3", "item1"])
        assert [i["componente"] for i in plano["itens"]] == ["Rede X710", "iDRAC", "BIOS"]
        assert plano["tamanho_total"] == len(CONTEUDO_NIC) + len(CONTEUDO_BIOS) + 1

    def test_confirmacoes(self):
        plano = montar_plano(verificacao(NIC), ["item1"])
        assert conferir_confirmacoes(plano, "  ATUALIZAR ") == ""
        assert "ATUALIZAR" in conferir_confirmacoes(plano, "atualizar")
        assert conferir_confirmacoes(plano, "")
        com_redundancia = montar_plano(verificacao(NIC, redundancia=True), ["item1"])
        assert "SEM REDUNDANCIA" in conferir_confirmacoes(com_redundancia, CONFIRMACAO)
        assert conferir_confirmacoes(com_redundancia, CONFIRMACAO, "sem redundancia")
        assert conferir_confirmacoes(com_redundancia, CONFIRMACAO, CONFIRMACAO_REDUNDANCIA) == ""


# --- Simulação (padrão) ---------------------------------------------------------------------------


class TestSimulacao:
    def test_simula_sem_gravar_nada_no_bmc(self, tmp_path):
        executor = BMCFalso()
        plano = montar_plano(verificacao(NIC, BIOS), ["item1", "item2"])
        resultado = executar_plano(contexto(executor), plano, executor, str(tmp_path), MODO_SIMULAR, espaco_livre=LIVRE)
        assert resultado["situacao"] == "simulada" and executor.escritas == []
        assert not any("reboot" in " ".join(c.comando) for c in executor.historico)
        assert [i["componente"] for i in resultado["itens"]] == ["Rede X710", "BIOS"]
        assert all(i["hash"] == "SHA-256 confere" for i in resultado["itens"])
        acoes = [p["acao"] for p in resultado["passos"]]
        assert acoes.count("enviar (simulado)") == 2 and "boot único (simulado)" in acoes
        envio = next(p for p in resultado["passos"] if p["acao"] == "enviar (simulado)")
        assert "/redfish/v1/UpdateService/MultipartUpload" in envio["detalhe"] and "OnReset" in envio["detalhe"]
        nic = next(i for i in resultado["itens"] if i["componente"] == "Rede X710")
        assert nic["rollback"] == "21.5.9"  # item Previous- do FirmwareInventory
        assert (tmp_path / "firmware" / "pacotes" / "Rede_X710.EXE").read_bytes() == CONTEUDO_NIC

    def test_sem_versao_anterior_avisa(self, tmp_path):
        executor = BMCFalso(com_anterior=False)
        resultado = executar_plano(contexto(executor), montar_plano(verificacao(NIC), ["item1"]), executor, str(tmp_path),
                                   espaco_livre=LIVRE)  # fmt: skip
        assert any("sem versão anterior" in p["detalhe"] for p in resultado["passos"])

    def test_espaco_insuficiente(self, tmp_path):
        executor = BMCFalso()
        resultado = executar_plano(contexto(executor), montar_plano(verificacao(NIC), ["item1"]), executor, str(tmp_path),
                                   espaco_livre=lambda _: 10)  # fmt: skip
        assert resultado["situacao"] == "bloqueada" and "Espaço insuficiente" in resultado["mensagem"]
        assert not any("dell.com" in " ".join(c.comando) for c in executor.historico)  # nenhum download começou

    def test_hash_divergente_apaga_e_aborta(self, tmp_path):
        errado = {**BIOS, "pacote": {**BIOS["pacote"], "sha256": "0" * 64}}
        executor = BMCFalso()
        plano = montar_plano(verificacao(NIC, errado), ["item1", "item2"])
        resultado = executar_plano(contexto(executor), plano, executor, str(tmp_path), espaco_livre=LIVRE)
        assert resultado["situacao"] == "falhou" and "SHA-256 divergente na verificação de integridade" in resultado["mensagem"]
        assert not (tmp_path / "firmware" / "pacotes" / "BIOS.EXE").exists()
        itens = resultado["itens"]
        assert [i["componente"] for i in itens] == ["Rede X710", "BIOS"] and itens[-1]["situacao"] == "abortado"  # nada segue

    def test_md5_so_como_alternativa(self, tmp_path):
        so_md5 = {**NIC, "pacote": {**NIC["pacote"], "sha256": ""}}
        executor = BMCFalso()
        resultado = executar_plano(contexto(executor), montar_plano(verificacao(so_md5), ["item1"]), executor, str(tmp_path),
                                   espaco_livre=LIVRE)  # fmt: skip
        assert resultado["itens"][0]["hash"] == "MD5 confere"

    def test_sem_credenciais_do_bmc(self, tmp_path):
        executor = BMCFalso()
        ctx = Contexto(executor=executor)
        resultado = executar_plano(ctx, montar_plano(verificacao(NIC), ["item1"]), executor, str(tmp_path), espaco_livre=LIVRE)
        assert resultado["situacao"] == "simulada"
        assert any("administrador" in p["detalhe"] for p in resultado["passos"] if p["acao"] == "BMC")

    def test_plano_bloqueado_nao_faz_nada(self, tmp_path):
        executor = BMCFalso()
        ctx = contexto(executor)
        antes = len(executor.historico)
        resultado = executar_plano(ctx, montar_plano(verificacao(NIC), []), executor, str(tmp_path))
        assert resultado["situacao"] == "bloqueada" and len(executor.historico) == antes


# --- Caminho real (só contra o BMC falso) ---------------------------------------------------------


class TestAplicacao:
    def test_trava_segura_o_primeiro_envio(self, tmp_path):
        executor = BMCReal(com_exportacao=True)
        resultado = executar_plano(contexto(executor), montar_plano(verificacao(NIC), ["item1"]), executor, str(tmp_path),
                                   MODO_APLICAR, espaco_livre=LIVRE)  # fmt: skip
        assert resultado["situacao"] == "bloqueada_trava" and resultado["mensagem"] == MOTIVO_TRAVA
        assert executor.escritas == [] and not (tmp_path / "estado").exists()
        assert not any(c.comando[:2] == ["systemctl", "reboot"] and c.rc == 0 for c in executor.historico)

    def test_agendado_boot_usb_estado_e_reinicio(self, tmp_path):
        executor = BMCFalso(tarefas=("Task_agendada.json",), com_exportacao=True)
        plano = montar_plano(verificacao(NIC, BIOS), ["item1", "item2"])
        resultado = executar_plano(contexto(executor), plano, executor, str(tmp_path), MODO_APLICAR, espaco_livre=LIVRE,
                                   dormir=lambda _: None)  # fmt: skip
        assert resultado["situacao"] == "aguardando_reinicio"
        operacoes = [e[0] for e in executor.escritas]
        # 09/10/2026: a BIOS vai sozinha — a rede é agendada e aplicada num reinício; a BIOS fica pendente para a volta.
        assert operacoes == [EXPORTAR_CONFIGURACAO, ENVIAR_FIRMWARE, BOOT_UNICO_USB]
        assert executor.escritas[1][3].endswith("Rede_X710.EXE") and executor.escritas[2][2] == CORPO_BOOT_USB
        estado = json.loads((tmp_path / "estado" / "atualizacao_pendente.json").read_text())
        assert [i["componente"] for i in estado["enviados"]] == ["Rede X710"]
        assert [i["componente"] for i in estado["pendentes"]] == ["BIOS"]
        assert executor.comandos_rodados().count(["systemctl", "reboot"]) == 1
        assert "tok" not in json.dumps(estado) and "senha" not in json.dumps(estado)
        assert ["systemctl", "reboot"] in executor.comandos_rodados()
        assert list((tmp_path / "firmware" / "backup").iterdir())

    def test_tarefa_falhou_nao_reinicia(self, tmp_path):
        executor = BMCFalso(tarefas=("Task_falhou.json",))
        resultado = executar_plano(contexto(executor), montar_plano(verificacao(NIC), ["item1"]), executor, str(tmp_path),
                                   MODO_APLICAR, espaco_livre=LIVRE, dormir=lambda _: None)  # fmt: skip
        assert resultado["situacao"] == "falhou" and "signature" in resultado["mensagem"]
        assert BOOT_UNICO_USB not in [e[0] for e in executor.escritas] and ["systemctl", "reboot"] not in executor.comandos_rodados()

    def test_tarefa_sem_resposta_depois_do_202_fica_possivelmente_agendado(self, tmp_path):
        """Revisão independente (ALTO 2): aceito pelo iDRAC e depois sem resposta → nunca só "falhou"."""

        class SemResposta(BMCFalso):
            consultas = 0

            def _redfish(self, metodo, url, token, corpo, timeout):
                if metodo == "GET" and url == BASE + TAREFA:
                    SemResposta.consultas += 1
                    return RespostaRedfish(url, 503, erro_tipo="http", erro="serviço ocupado")
                return super()._redfish(metodo, url, token, corpo, timeout)

        executor = SemResposta()
        resultado = executar_plano(contexto(executor), montar_plano(verificacao(NIC), ["item1"]), executor, str(tmp_path),
                                   MODO_APLICAR, espaco_livre=LIVRE, dormir=lambda _: None)  # fmt: skip
        assert resultado["situacao"] == "possivelmente_agendado" and "Job Queue" in resultado["mensagem"]
        assert SemResposta.consultas == mod.FALHAS_SEGUIDAS_TAREFA  # repetiu antes de desistir
        estado = json.loads((tmp_path / "estado" / "atualizacao_pendente.json").read_text())  # retomada no próximo boot
        assert [i["componente"] for i in estado["enviados"]] == ["Rede X710"]
        assert ["systemctl", "reboot"] not in executor.comandos_rodados()

    def test_pacote_alterado_depois_do_download_nao_e_enviado(self, tmp_path):
        """Revisão independente (MÉDIO 3): hash recalculado logo antes do envio ao BMC."""

        class Troca(BMCFalso):
            def _bmc_escrita(self, operacao, metodo, url, token, corpo, arquivo, timeout):
                if operacao == EXPORTAR_CONFIGURACAO:  # acontece entre o download e o envio
                    for pacote in (tmp_path / "firmware" / "pacotes").iterdir():
                        pacote.write_bytes(b"trocado")
                return super()._bmc_escrita(operacao, metodo, url, token, corpo, arquivo, timeout)

        executor = Troca(com_exportacao=True)
        resultado = executar_plano(contexto(executor), montar_plano(verificacao(NIC), ["item1"]), executor, str(tmp_path),
                                   MODO_APLICAR, espaco_livre=LIVRE, dormir=lambda _: None)  # fmt: skip
        assert resultado["situacao"] == "falhou" and "mudou depois da verificação de integridade" in resultado["mensagem"]
        assert ENVIAR_FIRMWARE not in [e[0] for e in executor.escritas]

    def test_recusa_do_bmc_nao_deixa_estado(self, tmp_path):
        class Recusa(BMCFalso):
            def _bmc_escrita(self, operacao, metodo, url, token, corpo, arquivo, timeout):
                self.escritas.append((operacao, url, corpo, arquivo))
                return RespostaRedfish(url, 400, erro_tipo="http", erro="pacote inválido")

        executor = Recusa()
        resultado = executar_plano(contexto(executor), montar_plano(verificacao(NIC), ["item1"]), executor, str(tmp_path),
                                   MODO_APLICAR, espaco_livre=LIVRE, dormir=lambda _: None)  # fmt: skip
        assert resultado["situacao"] == "falhou" and not (tmp_path / "estado" / "atualizacao_pendente.json").exists()

    def test_concluido_sem_reinicio_segue_para_o_proximo(self, tmp_path):
        executor = BMCFalso(tarefas=("Task_concluida.json",))
        plano = montar_plano(verificacao(NIC, BIOS), ["item1", "item2"])
        resultado = executar_plano(contexto(executor), plano, executor, str(tmp_path), MODO_APLICAR, espaco_livre=LIVRE,
                                   dormir=lambda _: None)  # fmt: skip
        assert resultado["situacao"] == "aplicada" and [e[0] for e in executor.escritas] == [ENVIAR_FIRMWARE] * 2

    def test_usuario_somente_leitura_nada_e_perguntado(self, tmp_path):
        """18.2/18.3: sem administrador e sem IPMI local, nada é perguntado; o caminho g explica o que faltou."""
        executor = BMCFalso()
        ctx = contexto(executor, administrador=False)
        resultado = executar_plano(ctx, montar_plano(verificacao(NIC), ["item1"]), executor, str(tmp_path), MODO_APLICAR,
                                   espaco_livre=LIVRE)  # fmt: skip
        assert resultado["situacao"] == "falhou" and executor.escritas == []
        assert "Precisa de um usuário administrador" in resultado["mensagem"] and "racadm" in resultado["mensagem"]
        assert [t["caminho"] for t in resultado["tentativas"]][-1] == "g"

    def test_bmc_so_com_simple_update_tenta_o_caminho_do_idrac8_e_explica(self, tmp_path):
        executor = BMCFalso()
        url = BASE + "/redfish/v1/UpdateService"
        executor.respostas_redfish[("GET", url)] = RespostaRedfish(url, 200, corpo=ler(DMTF, "UpdateService.json"))
        resultado = executar_plano(contexto(executor), montar_plano(verificacao(NIC), ["item1"]), executor, str(tmp_path),
                                   MODO_APLICAR, espaco_livre=LIVRE)  # fmt: skip
        assert resultado["situacao"] == "falhou" and "g) Nenhum caminho funcionou" in resultado["mensagem"]
        assert all(e[0] != "enviar_firmware" for e in executor.escritas)


# --- Retomada ----------------------------------------------------------------------------------------


class TestRetomada:
    ESTADO = {"versao": 1, "criado": "2026-09-28T12:00:00",
              "enviados": [{"componente": "BIOS", "component_id": "159", "instalada": "2.21.2", "disponivel": "2.28.1"}],
              "pendentes": [{"componente": "Rede X710"}]}  # fmt: skip

    @pytest.mark.parametrize(("versao_agora", "esperado"), [("2.28.1", APLICADA), ("2.21.2", NAO_APLICADA), (None, NAO_VERIFICADA)])
    def test_antes_depois(self, versao_agora, esperado):
        inventario = [{"nome": "BIOS", "versao": versao_agora, "component_id": "159"}] if versao_agora else []
        resultado = conferir(self.ESTADO, inventario, "Redfish (FirmwareInventory)")
        assert resultado["itens"][0]["resultado"] == esperado and resultado["pendentes"] == ["Rede X710"]

    def test_ler_e_arquivar(self, tmp_path):
        executor = ExecutorFalso()
        caminho = tmp_path / "estado" / "atualizacao_pendente.json"
        assert ler_estado(executor, str(tmp_path)) is None
        executor.arquivos[str(caminho)] = json.dumps(self.ESTADO)
        assert ler_estado(executor, str(tmp_path))["enviados"][0]["componente"] == "BIOS"
        executor.arquivos[str(caminho)] = '{"versao": 2}'
        assert ler_estado(executor, str(tmp_path)) is None
        caminho.parent.mkdir(parents=True)
        caminho.write_text(json.dumps(self.ESTADO))
        arquivado = arquivar_estado(str(tmp_path))
        assert not caminho.exists() and arquivado.name.startswith("atualizacao_concluida_")

    def test_textos(self):
        retomada = conferir(self.ESTADO, [{"nome": "BIOS", "versao": "2.28.1", "component_id": "159"}], "Redfish")
        assert "BIOS: 2.21.2 -> 2.28.1 — aplicada" in formatar_retomada(retomada)
        assert "Retomada da atualização" in html_aplicacao(None, retomada)


# --- Relatório e modo texto ---------------------------------------------------------------------------


class TestRelatorioEModoTexto:
    def test_relatorio(self, tmp_path):
        executor = BMCFalso()
        resultado = executar_plano(contexto(executor), montar_plano(verificacao(NIC), ["item1"]), executor, str(tmp_path),
                                   espaco_livre=LIVRE)  # fmt: skip
        texto = formatar_aplicacao(resultado)
        assert "modo interno de teste (nada foi gravado no servidor)" in texto and "enviar (simulado)" in texto
        assert "Mudanças temporárias no iDRAC: nenhuma" in texto
        resultado["passos"][0]["detalhe"] = "<script>x</script>"
        assert "<script>x" not in html_aplicacao(resultado)

    def preparar(self, tmp_path, redundancia=False):
        from msscan.pendrive.local import local_manual
        from msscan.relatorio.completo import Gravacao

        executor = BMCFalso()
        return executor, contexto(executor), {"atualizacoes": verificacao(NIC, BIOS, redundancia=redundancia)}, \
            local_manual(str(tmp_path)), Gravacao(None, False, "")  # fmt: skip

    def rodar(self, tmp_path, respostas, redundancia=False, aplicar=False, monkeypatch=None):
        from main import etapa_aplicacao

        executor, ctx, relatorio, local, gravacao = self.preparar(tmp_path, redundancia)
        fila = iter(respostas)
        etapa_aplicacao(argparse.Namespace(replay=None, aplicar=aplicar, simular=not aplicar), executor, ctx, relatorio, "t", local, gravacao,
                        entrada=lambda _: next(fila))  # fmt: skip
        return executor, relatorio

    def test_padrao_nao(self, tmp_path):
        _, relatorio = self.rodar(tmp_path, [""])
        assert "aplicacao" not in relatorio

    def test_selecao_vazia_e_invalida(self, tmp_path, capsys):
        _, relatorio = self.rodar(tmp_path, ["s", ""])
        assert "aplicacao" not in relatorio and "Nenhum item escolhido" in capsys.readouterr().out
        _, relatorio = self.rodar(tmp_path, ["s", "1, 9"])
        assert "aplicacao" not in relatorio and "Escolha inválida" in capsys.readouterr().out

    def test_confirmacao_errada_nao_faz_nada(self, tmp_path):
        executor, relatorio = self.rodar(tmp_path, ["s", "1", "atualizar"])
        assert "aplicacao" not in relatorio and not any(c.comando[0] == "GET" and "dell.com" in c.comando[1] for c in executor.historico)

    def test_simulacao_pelo_modo_texto(self, tmp_path, capsys):
        executor, relatorio = self.rodar(tmp_path, ["s", "1", CONFIRMACAO])
        assert relatorio["aplicacao"]["situacao"] == "simulada" and executor.escritas == []
        saida = capsys.readouterr().out
        assert "nenhum vem selecionado" in saida and "Aplicação de firmware — modo interno de teste" in saida
        assert "PLANO DE ATUALIZAÇÃO" in saida and "Hardware conferido agora" in saida and "nobreak" in saida
        assert "Mudanças TEMPORÁRIAS no iDRAC" in saida and "Li os avisos" not in saida and "SIMULA" not in saida.split("PLANO")[1].split("Digite")[0]

    def test_aviso_vencido_no_modo_texto(self, tmp_path, capsys, monkeypatch):
        """Revisão independente (MÉDIO 5): ATUALIZAR digitado mais de 10 min depois do aviso → nada é feito."""
        import types

        from main import etapa_aplicacao
        from msscan.atualizacao import aviso as modulo_aviso

        relogio = [1000.0]
        monkeypatch.setattr(modulo_aviso, "time", types.SimpleNamespace(time=lambda: relogio[0]))
        executor, ctx, relatorio, local, gravacao = self.preparar(tmp_path, False)
        respostas = iter(["s", "1", CONFIRMACAO])

        def entrada(pergunta=""):
            if "ATUALIZAR" in pergunta:
                relogio[0] += modulo_aviso.VALIDADE_S + 1
            return next(respostas)

        etapa_aplicacao(argparse.Namespace(replay=None, aplicar=False, simular=True), executor, ctx, relatorio, "t", local, gravacao,
                        entrada=entrada)  # fmt: skip
        assert "aplicacao" not in relatorio and "10 minutos" in capsys.readouterr().out

    def test_confirmacao_vazia_nada_acontece(self, tmp_path, capsys):
        executor, relatorio = self.rodar(tmp_path, ["s", "1", "", CONFIRMACAO])
        assert "aplicacao" not in relatorio and executor.escritas == []
        assert "Nada foi feito" in capsys.readouterr().out

    def test_redundancia_exige_confirmacao_separada(self, tmp_path):
        _, relatorio = self.rodar(tmp_path, ["s", "1", "", CONFIRMACAO], redundancia=True)
        assert "aplicacao" not in relatorio
        _, relatorio = self.rodar(tmp_path, ["s", "1", CONFIRMACAO_REDUNDANCIA, CONFIRMACAO], redundancia=True)
        assert relatorio["aplicacao"]["situacao"] == "simulada"

    def test_aplicar_avisa_a_trava(self, tmp_path, capsys):
        from main import etapa_aplicacao

        executor = BMCReal()
        ctx = contexto(executor)
        from msscan.pendrive.local import local_manual
        from msscan.relatorio.completo import Gravacao

        relatorio = {"atualizacoes": verificacao(NIC)}
        fila = iter(["s", "1", CONFIRMACAO])
        etapa_aplicacao(argparse.Namespace(replay=None, aplicar=True, simular=False), executor, ctx, relatorio, "t",
                        local_manual(str(tmp_path)), Gravacao(None, False, ""), entrada=lambda _: next(fila))  # fmt: skip
        saida = capsys.readouterr().out
        assert "GRAVACAO_REAL_LIBERADA" in saida and relatorio["aplicacao"]["situacao"] == "bloqueada_trava"


# --- Interface: sessão e API ---------------------------------------------------------------------------


def sessao_pronta(executor, pasta, modo=MODO_SIMULAR):
    from msscan.interface.sessao import CONCLUIDO, Sessao

    sessao = Sessao(executor, str(pasta), replay=True, modo_aplicacao=modo)
    sessao.iniciar()
    sessao.aguardar(30)
    assert sessao.iniciar_diagnostico()
    sessao.aguardar(30)
    assert sessao.etapa == CONCLUIDO
    sessao.ctx.bmc = contexto(executor).bmc
    sessao.relatorio["atualizacoes"] = verificacao(NIC, BIOS)
    return sessao


def preparar_aviso(sessao, itens: list[str]) -> dict:
    """Abre a tela de aviso (15.6) e espera a releitura do hardware."""
    assert sessao.preparar_aviso("catalogo", itens)[0]
    sessao.aguardar(30)
    return sessao.aviso


class TestInterface:
    def test_confirmacoes_conferidas_no_servidor(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mod.shutil, "disk_usage", lambda _: type("U", (), {"free": 10 * 2**30})())
        executor = BMCFalso()
        sessao = sessao_pronta(executor, tmp_path)
        assert sessao.aplicar_atualizacoes([], CONFIRMACAO)[0] is False
        ok, motivo = sessao.aplicar_atualizacoes(["item1"], "atualizar")
        assert not ok and "ATUALIZAR" in motivo
        ok, motivo = sessao.aplicar_atualizacoes(["item1"], CONFIRMACAO)
        assert not ok and "plano de atualização" in motivo  # 18.2: sem o plano, nada é instalado
        aviso = preparar_aviso(sessao, ["item1"])
        assert aviso["situacao"] == "pronto" and aviso["pode_seguir"] and aviso["titulo"] == "Plano de atualização", aviso
        assert aviso["temporarias"]  # mudanças temporárias no iDRAC: só informativo
        assert sessao.aplicar_atualizacoes(["item1"], CONFIRMACAO) == (True, "iniciado")
        sessao.aguardar(30)
        assert sessao.aviso is None  # aviso de uso único
        estado = sessao.estado()["atualizacoes"]
        assert estado["modo"] == "simular" and estado["aplicacao"]["situacao"] == "simulada"
        assert sessao.relatorio["aplicacao"]["situacao"] == "simulada" and executor.escritas == []

    def test_modo_aplicar_mostra_a_trava(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mod.shutil, "disk_usage", lambda _: type("U", (), {"free": 10 * 2**30})())
        executor = BMCReal()
        sessao = sessao_pronta(executor, tmp_path, MODO_APLICAR)
        assert sessao.estado()["atualizacoes"]["trava"] == MOTIVO_TRAVA
        preparar_aviso(sessao, ["item1"])
        assert sessao.aplicar_atualizacoes(["item1"], CONFIRMACAO)[0]
        sessao.aguardar(30)
        assert sessao.aplicacao["situacao"] == "bloqueada_trava" and executor.escritas == []

    def test_api(self, tmp_path):
        import threading
        import urllib.error
        import urllib.request

        from msscan.interface.servidor import ServidorUI

        sessao = sessao_pronta(BMCFalso(), tmp_path)
        srv = ServidorUI(sessao, porta=0)
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        try:
            def post(corpo: bytes) -> tuple[int, str]:
                req = urllib.request.Request(f"http://127.0.0.1:{srv.porta}/api/atualizacoes/aplicar", data=corpo,
                                             method="POST", headers={"X-MSscan-Token": srv.token})  # fmt: skip
                try:
                    with urllib.request.urlopen(req, timeout=10) as r:
                        return r.status, r.read().decode()
                except urllib.error.HTTPError as erro:
                    return erro.code, erro.read().decode()

            assert post(b'{"itens": "item1", "confirmacao": "ATUALIZAR"}')[0] == 400
            codigo, corpo = post(b'{"itens": ["item1"], "confirmacao": "atualizar"}')
            assert codigo == 409 and "ATUALIZAR" in corpo
        finally:
            srv.shutdown()
            srv.server_close()

    def test_retomada_ao_diagnosticar(self, tmp_path):
        from msscan.interface.sessao import Sessao

        executor = BMCFalso()
        caminho = tmp_path / "estado" / "atualizacao_pendente.json"
        executor.arquivos[str(caminho)] = json.dumps(TestRetomada.ESTADO)
        sessao = Sessao(executor, str(tmp_path), replay=True)
        sessao.iniciar()
        sessao.aguardar(30)
        sessao.iniciar_diagnostico()
        sessao.aguardar(30)
        retomada = sessao.estado()["atualizacoes"]["retomada"]
        assert retomada["itens"][0]["resultado"] == NAO_VERIFICADA  # sem inventário neste BMC falso: nunca "aplicada" inventado
        assert sessao.relatorio["retomada"] == retomada

    def test_frontend_regras(self):
        from tests.test_interface import ESTATICOS_TEXTO

        html, js = ESTATICOS_TEXTO["index.html"], ESTATICOS_TEXTO["app.js"]
        assert 'id="atu-confirmacao-redundancia"' in html and "SEM REDUNDANCIA" in html
        assert '$("atu-confirmacao").value.trim() !== "ATUALIZAR"' in js  # botão só com ATUALIZAR digitado
        criacao = js.split('class: "atu-escolha"')[1].split("})),")[0]
        assert "checked" not in criacao  # checkboxes de firmware nascem desmarcados
        assert not re.search(r"atu-escolha[^\n]*\.checked\s*=\s*true", js)


@pytest.fixture(autouse=True)
def _hardware_saudavel(monkeypatch):
    """Tela de aviso: hardware do BMC falso tratado como OK (a lógica do aviso é testada em test_aviso.py)."""
    hardware_saudavel(monkeypatch)
