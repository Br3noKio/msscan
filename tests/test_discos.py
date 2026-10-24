"""Aba Discos, como o Hard Disk Sentinel (CLAUDE.md 31, pedido de 22/10/2026): saúde %, temperatura ao vivo, autoteste e
varredura de superfície pelo próprio disco. Dados reais: R640 (SSD SATA) e R620 (SAS atrás da PERC H710, um disco com 183
defeitos crescidos). Nenhum teste envia comando a disco real."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from msscan import discos
from msscan.core.executor import ExecutorReplay, validar_autoteste_disco
from msscan.interface.servidor import ServidorUI
from msscan.parsers.smartctl import parse_smart
from msscan.provedores.smart import avaliar_disco
from tests.conftest import R620, R640, ExecutorFalso, ler_saida
from tests.test_interface import sessao_concluida

SDA_R640 = "smartctl__-a__-j__-d__sat__dev_sda"
PERC_183 = "smartctl__-a__-j__-d__megaraid_0__dev_bus_0"
PERC_OK = "smartctl__-a__-j__-d__megaraid_2__dev_bus_0"


def disco_real(pasta, nome: str, **extra) -> dict:
    """Disco como o provedor o entrega: ``parse_smart`` + avaliação do diagnóstico."""
    dados = parse_smart(ler_saida(pasta, nome))
    avaliacao = avaliar_disco(dados)
    return {**dados, "status": avaliacao["status"].value, "problemas": avaliacao["problemas"], **extra}


# --- Parser -------------------------------------------------------------------------------------


def test_parser_ssd_sata_real_r640():
    d = parse_smart(ler_saida(R640, SDA_R640))
    assert d["temperatura_detalhe"] == {"atual": 24, "minima": 15, "maxima": 38, "limite": None}  # "24 (Min/Max 15/38)"
    assert d["ciclos_energia"] == 82 and d["rotacao_rpm"] == 0 and d["interface"] == "6.0 Gb/s"
    assert d["desgaste_pct"] is None  # o atributo 173 desse SSD não tem nome na base do smartctl: não se inventa
    assert d["autoteste"] == {"em_andamento": False, "restante_pct": None, "suportado": True, "minutos_curto": 2, "minutos_estendido": 14}
    assert {a["id"]: a["pior"] for a in d["atributos"]}[194] == 62


def test_parser_sas_real_r620_atras_da_perc():
    d = parse_smart(ler_saida(R620, PERC_183))
    assert d["temperatura_detalhe"]["limite"] == 68 and d["scsi_defeitos_crescidos"] == 183
    assert d["ciclos_energia"] == 51 and d["ciclos_especificados"] == 10000 and d["fabricacao"] == "24/2013"
    assert d["rotacao_rpm"] == 15000 and d["scsi_erros_nao_corrigidos"] == 0
    assert d["autoteste"]["minutos_estendido"] == 35 and d["autoteste"]["suportado"]  # 2052 s → 35 min
    assert [(t["tipo"], t["passou"]) for t in d["autotestes"]] == [("Background short", True), ("Background long", True), ("Background short", True)]


def test_parser_ata_teste_em_andamento_e_log_com_erro():
    # Formato do smartctl 7.x (TODO(fixture): saída real com teste rodando e log com falha).
    texto = json.dumps({
        "smart_status": {"passed": True}, "smart_support": {"available": True, "enabled": True},
        "ata_smart_data": {"self_test": {"status": {"value": 249, "string": "in progress, 90% remaining", "remaining_percent": 90},
                                         "polling_minutes": {"short": 1, "extended": 120}}, "capabilities": {"self_tests_supported": True}},
        "ata_smart_self_test_log": {"standard": {"table": [
            {"type": {"string": "Extended offline"}, "status": {"value": 121, "string": "Completed: read failure", "passed": False},
             "lifetime_hours": 900, "lba": 123456}]}},
        "ata_smart_attributes": {"table": [{"id": 233, "name": "Media_Wearout_Indicator", "value": 75, "thresh": 0}]},
    })  # fmt: skip
    d = parse_smart(texto)
    assert d["autoteste"]["em_andamento"] and d["autoteste"]["restante_pct"] == 90 and d["autoteste"]["minutos_estendido"] == 120
    assert d["autotestes"][0] == {"tipo": "Extended offline", "resultado": "Completed: read failure", "passou": False,
                                  "em_andamento": False, "horas": 900, "lba": 123456}  # fmt: skip
    assert d["desgaste_pct"] == 25


def test_parser_nvme_em_andamento():
    texto = json.dumps({"nvme_smart_health_information_log": {"percentage_used": 3, "temperature": 40},
                        "nvme_self_test_log": {"current_self_test_operation": {"value": 2}, "current_self_test_completion_percent": 30, "table": []}})
    d = parse_smart(texto)
    assert d["autoteste"]["em_andamento"] and d["autoteste"]["restante_pct"] == 70 and d["desgaste_pct"] == 3


# --- Saúde, vida restante e temperatura ---------------------------------------------------------


def test_saude_dos_discos_reais():
    perc = discos.saude(disco_real(R620, PERC_183))
    assert perc["percentual"] == 40 and any("183" in m for m in perc["motivos"])  # 183 defeitos: −60 (teto da penalidade)
    assert discos.saude(disco_real(R620, PERC_OK)) == {"percentual": 100, "motivos": []}
    assert discos.saude(disco_real(R640, SDA_R640))["percentual"] == 100


def test_saude_sem_dado_nunca_vira_100():
    assert discos.saude({"status": "NAO_VERIFICADO"})["percentual"] is None
    assert discos.saude({})["percentual"] is None


def test_saude_tetos_e_penalidades():
    assert discos.saude({"status": "FALHA", "smart_passou": False})["percentual"] == discos.TETO_SMART_REPROVADO
    assert discos.saude({"status": "ALERTA", "autotestes": [{"passou": False}]})["percentual"] == discos.TETO_AUTOTESTE_REPROVADO
    assert discos.saude({"status": "OK", "desgaste_pct": 30})["percentual"] == 70
    pendentes = {"status": "ALERTA", "atributos": [{"id": 197, "bruto": 4}, {"id": 199, "bruto": 50}]}  # 199 (cabo) não conta
    assert discos.saude(pendentes)["percentual"] == 88
    assert discos.saude({"status": "ALERTA", "codigo_saida": 64})["percentual"] == 100 - discos.PENALIDADE_LOG_DE_ERROS


def test_vida_restante_so_com_desgaste():
    assert discos.vida_restante_dias({"desgaste_pct": 25, "horas_ligado": 2400}) == 300
    assert discos.vida_restante_dias({"desgaste_pct": 0, "horas_ligado": 2400}) is None
    assert discos.vida_restante_dias({"horas_ligado": 40000}) is None  # HD: sem histórico, sem previsão


def test_nivel_e_limite_da_temperatura():
    sas = disco_real(R620, PERC_183)
    assert discos.limite_temperatura(sas) == 68  # drive_trip do próprio disco
    assert discos.limite_temperatura({"rotacao_rpm": 0}) == discos.LIMITE_TEMPERATURA_SSD
    assert discos.limite_temperatura({"rotacao_rpm": 7200}) == discos.LIMITE_TEMPERATURA_HDD
    assert [discos.nivel_temperatura(v, 60) for v in (None, 40, 52, 60)] == ["", "ok", "alerta", "falha"]


# --- Salvaguardas do autoteste -----------------------------------------------------------------


def test_comando_autoteste_so_para_disco_inteiro():
    sas = disco_real(R620, PERC_183)
    assert discos.comando_autoteste(sas, "estendido") == ["smartctl", "-t", "long", "-d", "megaraid,0", "/dev/bus/0"]
    assert discos.comando_autoteste(sas, None) == ["smartctl", "-X", "-d", "megaraid,0", "/dev/bus/0"]
    assert discos.comando_autoteste({"dispositivo": "/dev/sda"}, "curto") == ["smartctl", "-t", "short", "/dev/sda"]
    assert discos.comando_autoteste({"dispositivo": "/dev/sda1"}, "curto") is None  # partição
    assert discos.comando_autoteste({"dispositivo": "/dev/sda", "tipo": "sat;rm"}, "curto") is None
    assert discos.comando_autoteste({"dispositivo": "/dev/sda"}, "select") is None


@pytest.mark.parametrize("cmd", [
    ["smartctl", "-t", "short", "/dev/sda"], ["smartctl", "-t", "long", "-d", "megaraid,3", "/dev/bus/0"],
    ["smartctl", "-X", "-d", "sat", "/dev/sda"], ["smartctl", "-t", "short", "/dev/nvme0"],
])  # fmt: skip
def test_validador_aceita_so_a_lista_fechada(cmd):
    assert validar_autoteste_disco(cmd) == ""


@pytest.mark.parametrize("cmd", [
    ["smartctl", "-t", "select,0-100", "/dev/sda"], ["smartctl", "-s", "off", "/dev/sda"], ["smartctl", "-t", "short", "/dev/sda1"],
    ["smartctl", "-t", "short", "/dev/sda", "/dev/sdb"], ["dd", "if=/dev/sda", "of=/dev/null"], ["badblocks", "-sv", "/dev/sda"],
    ["smartctl", "-t", "short", "-d", "megaraid,1;reboot", "/dev/bus/0"], [],
])  # fmt: skip
def test_validador_recusa_o_resto(cmd):
    assert validar_autoteste_disco(cmd) != ""


@pytest.mark.parametrize("cmd", [["smartctl", "-t", "long", "/dev/sda"], ["smartctl", "--test=short", "/dev/sda"],
                                 ["smartctl", "-X", "/dev/sda"], ["smartctl", "-s", "off", "/dev/sda"], ["smartctl", "--set=standby,now", "/dev/sda"]])
def test_rodar_recusa_smartctl_que_altera_o_disco(cmd):
    executor = ExecutorFalso({tuple(cmd): (0, "", "")})
    rc, _, motivo = executor.rodar(cmd)
    assert rc != 0 and "smartctl" in motivo and executor.historico[-1].rc != 0


def test_autoteste_atras_da_trava_das_acoes():
    cmd = ["smartctl", "-t", "short", "/dev/sda"]
    executor = ExecutorFalso({tuple(cmd): (0, "", "")})
    assert executor.autoteste_disco(cmd)[0] != 0  # conftest: ACOES_REAIS_LIBERADAS = False
    executor.alvo_simulado = True
    assert executor.autoteste_disco(cmd)[0] == 0 and executor.historico[-1].comando == cmd
    assert executor.autoteste_disco(["smartctl", "-s", "off", "/dev/sda"])[0] != 0


# --- Sessão e API --------------------------------------------------------------------------------


def _esperar_releitura(sessao) -> None:
    for _ in range(100):
        if not sessao._discos_lendo and sessao._discos_lidos:
            return
        time.sleep(0.02)


def test_painel_da_sessao_com_o_r640_e_releitura_fora_do_historico():
    sessao = sessao_concluida()
    try:
        antes = len(sessao.executor.historico)
        painel = sessao.discos()
        _esperar_releitura(sessao)
        painel = sessao.discos()
        (disco,) = painel["discos"]
        assert disco["id"] == "sat|/dev/sda" and disco["saude"]["percentual"] == 100 and disco["temperatura"]["atual"] == 24
        assert disco["temperatura"]["limite"] == discos.LIMITE_TEMPERATURA_SSD and len(disco["temperatura"]["historico"]) == 1
        assert painel["demonstracao"] and len(sessao.executor.historico) == antes  # leitura ao vivo fora do log técnico
        ok, mensagem = sessao.autoteste_disco("sat|/dev/sda", "curto")
        assert not ok and "demonstração" in mensagem.lower()
    finally:
        sessao.encerrar()


def test_autoteste_pela_sessao_so_em_disco_diagnosticado(monkeypatch):
    sessao = sessao_concluida()
    try:
        sessao.replay = False
        sessao.executor.alvo_simulado = True
        enviados = []
        monkeypatch.setattr(sessao.executor, "_rodar", lambda cmd, timeout: (enviados.append(cmd), (0, "", "", 0.0))[1])
        assert not sessao.autoteste_disco("sat|/dev/sdb", "curto")[0]  # disco que o diagnóstico não viu
        assert not sessao.autoteste_disco("sat|/dev/sda", "select")[0]
        assert sessao.autoteste_disco("sat|/dev/sda", "estendido")[0]
        assert sessao.autoteste_disco("sat|/dev/sda", None)[0]
        assert enviados == [["smartctl", "-t", "long", "-d", "sat", "/dev/sda"], ["smartctl", "-X", "-d", "sat", "/dev/sda"]]
        monkeypatch.setattr(sessao, "_motivo_energia_bloqueada", lambda: "gravação de firmware em andamento")
        assert sessao.autoteste_disco("sat|/dev/sda", "curto") == (False, "gravação de firmware em andamento")
        assert sessao.discos()["pausa"] == "gravação de firmware em andamento" and len(enviados) == 2
    finally:
        sessao.encerrar()


def test_api_discos(monkeypatch):
    sessao = sessao_concluida()
    srv = ServidorUI(sessao, porta=0)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def pedir(caminho, corpo=None):
        req = urllib.request.Request(f"http://127.0.0.1:{srv.porta}/api/{caminho}", method="POST" if corpo is not None else "GET",
                                     data=json.dumps(corpo).encode() if corpo is not None else None,
                                     headers={"X-MSscan-Token": srv.token, "Content-Type": "application/json"})  # fmt: skip
        try:
            with urllib.request.urlopen(req, timeout=10) as resposta:
                return resposta.status, json.loads(resposta.read())
        except urllib.error.HTTPError as erro:
            return erro.code, json.loads(erro.read())

    try:
        status, corpo = pedir("discos")
        assert status == 200 and corpo["discos"][0]["modelo"] == "VK001920GWSXK"
        assert pedir("discos/autoteste", {"id": "sat|/dev/sda", "tipo": "apagar"})[0] == 400
        assert pedir("discos/autoteste", {"tipo": "curto"})[0] == 400
        assert pedir("discos/autoteste", {"id": "sat|/dev/sda", "tipo": "curto"})[0] == 409  # demonstração
    finally:
        srv.shutdown()
        srv.server_close()
        sessao.encerrar()
