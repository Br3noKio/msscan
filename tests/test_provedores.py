"""Testes dos provedores da fase 2.

Casos com problema são derivados de saídas reais do R640, alterando só o
estado/valor relevante, ou de estruturas já parseadas (nunca texto inventado
de ferramenta).
"""

from __future__ import annotations

import copy

import pytest

from msscan.core.executor import ExecutorReplay
from msscan.core.modelo import Status
from msscan.deteccao.contexto import Contexto
from msscan.deteccao.detectores import detectar
from msscan.modulos.registro import registro_padrao
from msscan.parsers.dmidecode import parse_memorias, parse_processadores
from msscan.parsers.ipmitool import parse_sdr_tipo, parse_sel_elist, parse_sensor
from msscan.parsers.procfs import parse_cpuinfo
from msscan.parsers.sensors import parse_sensors_json
from msscan.parsers.smartctl import parse_smart
from msscan.provedores.comum import evento_sel_e_problema
from msscan.provedores.ipmi import (
    avaliar_fontes,
    avaliar_linha_fonte,
    avaliar_linha_sdr,
    avaliar_log_bmc,
    avaliar_sdr,
    avaliar_sensores,
    classificar_estado,
)
from msscan.provedores.lmsensors import avaliar_temperaturas, avaliar_ventoinhas
from msscan.provedores.sistema import avaliar_cpu, avaliar_memoria, ler_edac
from msscan.provedores.smart import ProvedorSmartctl, avaliar_disco
from tests.conftest import R640, ExecutorFalso, ler_saida


def sel_r640():
    return parse_sel_elist(ler_saida(R640, "ipmitool__sel__elist"))


# --- Diagnóstico completo do R640 -------------------------------------------------


class TestDiagnosticoR640:
    @pytest.fixture(scope="class")
    def execucao(self):
        ex = ExecutorReplay(R640)
        ctx = detectar(ex)
        resultados = {r.componente: r for r in registro_padrao().diagnosticar(ctx)}
        return ex, resultados

    def test_status_por_componente(self, execucao):
        _, r = execucao
        assert {nome: res.status for nome, res in r.items()} == {
            "sensores": Status.OK,
            "fontes": Status.ALERTA,
            "ventoinhas": Status.OK,
            "cpu": Status.OK,  # "Presence detected" deasserted no SEL, mas as 2 CPUs estão presentes agora (histórico)
            "memoria": Status.ALERTA,
            "log_bmc": Status.ALERTA,
            "internos": Status.ALERTA,  # cabos SAS A0/B0 e slot PCIe 1 no SEL real
            "discos": Status.OK,
            "raid": Status.NAO_APLICAVEL,
            "rede": Status.OK,
        }

    def test_fonte_sem_energia_detectada_apesar_do_status_ok(self, execucao):
        _, r = execucao
        fontes = r["fontes"]
        assert fontes.dados["problemas"] == [
            "Status (10.1): Power Supply AC lost — sem energia de entrada (redundância de energia perdida)"
        ]
        assert fontes.dados["ignoradas_sem_leitura"] == ["PS Redundancy (7.1): Disabled"]
        assert fontes.dados["redundancia_perdida"] is True
        assert fontes.dados["defeito"] is False
        assert fontes.mensagem == "2 fonte(s) avaliada(s); redundância de energia perdida"

    def test_fontes_usadas(self, execucao):
        _, r = execucao
        assert r["sensores"].fonte == "ipmitool sensor"
        assert r["cpu"].fonte == "dmidecode -t 4 + /proc/cpuinfo + ipmitool sel elist"
        assert r["memoria"].fonte == "dmidecode -t 17 + EDAC + ipmitool sel elist"
        # Redfish primeiro (indisponível: sem credenciais do BMC na fixture), depois IPMI.
        assert [t["resultado"] for t in r["sensores"].dados["tentativas"]] == ["indisponível", "sucesso"]

    def test_sel_lido_uma_vez(self, execucao):
        ex, _ = execucao
        assert [e.comando for e in ex.historico].count(["ipmitool", "sel", "elist"]) == 1

    def test_memoria_aponta_slot_do_sel(self, execucao):
        _, r = execucao
        # A2 está instalado na coleta: o evento é histórico de peça presente (continua como problema, marcado).
        assert any("o slot A2" in p and "histórico" in p for p in r["memoria"].dados["problemas"])

    def test_disco_ok_com_observacoes(self, execucao):
        _, r = execucao
        [disco] = r["discos"].dados["discos"]
        assert disco["status"] == "OK"
        assert disco["observacoes"] == [
            "timeouts de comando: 170 (histórico)",
            "erros de CRC na interface (cabo/backplane): 4 (histórico)",
        ]

    def test_nenhum_comando_de_escrita(self, execucao):
        ex, _ = execucao
        for execucao_cmd in ex.historico:
            assert execucao_cmd.comando[0] != "modprobe"
            assert not {"set", "raw", "clear", "reset"} & set(execucao_cmd.comando)


def test_sem_ipmi_sensores_caem_para_lm_sensors():
    ctx = detectar(ExecutorReplay(R640))
    ctx.ipmi.disponivel = False
    ctx.ipmi.motivo = "sem BMC"
    resultados = {r.componente: r for r in registro_padrao().diagnosticar(ctx)}
    sensores = resultados["sensores"]
    assert sensores.status == Status.OK
    assert sensores.fonte == "lm-sensors (sensors -j)"
    assert [t["resultado"] for t in sensores.dados["tentativas"]] == ["indisponível", "indisponível", "sucesso"]
    assert resultados["fontes"].status == Status.NAO_VERIFICADO
    assert resultados["log_bmc"].status == Status.NAO_VERIFICADO
    # R640 não tem ventoinhas no lm-sensors: não verificado, nunca OK inventado.
    assert resultados["ventoinhas"].status == Status.NAO_VERIFICADO
    assert "log do BMC (SEL) indisponível" in " ".join(resultados["cpu"].dados["observacoes"])


# --- IPMI -----------------------------------------------------------------------


def sensores_r640():
    return parse_sensor(ler_saida(R640, "ipmitool__sensor"))


class TestSensoresIPMI:
    def test_real_ok(self):
        r = avaliar_sensores(sensores_r640())
        assert r.status == Status.OK
        assert "temperatura máxima" in r.mensagem
        assert r.dados["ignorados_sem_leitura"] == 3

    @pytest.mark.parametrize(("estado", "esperado"), [("nc", Status.ALERTA), ("cr", Status.FALHA), ("nr", Status.FALHA), ("xx", Status.ALERTA)])  # fmt: skip
    def test_estado_do_bmc(self, estado, esperado):
        sensores = sensores_r640()
        inlet = next(s for s in sensores if s["nome"] == "Inlet Temp")
        inlet["status"] = estado
        r = avaliar_sensores(sensores)
        assert r.status == esperado
        assert any(p.startswith("Inlet Temp") for p in r.dados["problemas"])

    def test_todos_sem_leitura(self):
        sensores = sensores_r640()
        for s in sensores:
            s["valor"], s["status"] = None, "na"
        assert avaliar_sensores(sensores) is None


def linha_ps(indice: int) -> dict:
    return copy.deepcopy(parse_sdr_tipo(ler_saida(R640, "ipmitool__sdr__type__Power_Supply"))[indice])


class TestFontes:
    """Defeito real → FALHA; sem energia / ausente / redundância → ALERTA."""

    def test_fonte_boa(self):
        assert avaliar_linha_fonte(linha_ps(2)) == (Status.OK, [], set())

    def test_sem_energia_real_e_alerta(self):
        situacao, problemas, categorias = avaliar_linha_fonte(linha_ps(1))
        assert situacao == Status.ALERTA
        assert categorias == {"sem_energia"}
        assert "redundância de energia perdida" in problemas[0]

    def test_ns_ignorado(self):
        assert avaliar_linha_fonte(linha_ps(0)) == (None, [], set())

    @pytest.mark.parametrize("estado", ["Failure detected", "Predictive failure", "Config Error"])
    def test_defeito_e_falha(self, estado):
        linha = linha_ps(2)
        linha["estados"] = ["Presence detected", estado]
        situacao, problemas, categorias = avaliar_linha_fonte(linha)
        assert situacao == Status.FALHA and categorias == {"defeito"}
        assert problemas == [f"Status (10.2): {estado} — defeito na fonte"]

    def test_coluna_critica_e_defeito(self):
        linha = linha_ps(2)
        linha.update(status="cr", estados=[], leitura="")
        situacao, _, categorias = avaliar_linha_fonte(linha)
        assert situacao == Status.FALHA and categorias == {"defeito"}

    def test_ausente_e_alerta(self):
        # TODO(fixture): substituir por saída real com fonte removida.
        linha = linha_ps(2)
        linha["estados"] = ["Power Supply input lost or out-of-range"]
        assert avaliar_linha_fonte(linha)[2] == {"sem_energia"}
        linha["estados"] = ["Disabled"]
        situacao, problemas, categorias = avaliar_linha_fonte(linha)
        assert situacao == Status.ALERTA
        assert categorias == {"desconhecido", "ausente"}
        assert any("ausente" in p for p in problemas)

    def test_redundancia_perdida(self):
        linha = linha_ps(0)
        linha.update(status="ok", estados=["Redundancy Lost"])
        situacao, _, categorias = avaliar_linha_fonte(linha)
        assert situacao == Status.ALERTA and categorias == {"redundancia"}

    def test_resultado_com_defeito_e_sem_energia(self):
        linhas = parse_sdr_tipo(ler_saida(R640, "ipmitool__sdr__type__Power_Supply"))
        linhas[2]["estados"] = ["Presence detected", "Failure detected"]
        r = avaliar_fontes(linhas)
        assert r.status == Status.FALHA
        assert r.dados["defeito"] is True and r.dados["redundancia_perdida"] is True
        assert "DEFEITO" in r.mensagem

    def test_tudo_sem_leitura(self):
        assert avaliar_fontes([linha_ps(0)]) is None


class TestSdr:
    def test_critico_sem_leitura_nao_e_descartado(self):
        linha = parse_sdr_tipo(ler_saida(R640, "ipmitool__sdr__type__Fan"))[0]
        linha.update(status="cr", valor=None, estados=[], leitura="")
        assert avaliar_linha_sdr(linha)[0] == Status.FALHA

    def test_estado_desconhecido_vira_alerta(self):
        linha = parse_sdr_tipo(ler_saida(R640, "ipmitool__sdr__type__Fan"))[6]
        linha["estados"] = ["Estado que ninguém conhece"]
        situacao, problemas = avaliar_linha_sdr(linha)
        assert situacao == Status.ALERTA and "não reconhecido" in problemas[0]

    @pytest.mark.parametrize(
        ("estado", "esperado"),
        [
            ("Presence detected", Status.OK),
            ("Fully Redundant", Status.OK),
            ("Power Supply AC lost", Status.FALHA),
            ("Failure detected", Status.FALHA),
            ("Predictive failure", Status.ALERTA),
            ("Redundancy Lost", Status.ALERTA),
            ("Disabled", None),
        ],
    )
    def test_classificar_estado(self, estado, esperado):
        assert classificar_estado(estado) == esperado

    def test_ventoinhas_real(self):
        r = avaliar_sdr(parse_sdr_tipo(ler_saida(R640, "ipmitool__sdr__type__Fan")), "ventoinhas", "x", "vent.")
        assert r.status == Status.OK and len(r.dados["leituras"]) == 17

    def test_ventoinha_critica(self):
        linhas = parse_sdr_tipo(ler_saida(R640, "ipmitool__sdr__type__Fan"))
        linhas[0].update(status="cr", valor=0.0, leitura="0 RPM")
        r = avaliar_sdr(linhas, "ventoinhas", "x", "vent.")
        assert r.status == Status.FALHA and r.dados["problemas"] == ["Fan1A (7.1): 0 RPM (estado cr)"]

    def test_tudo_sem_leitura(self):
        linha = parse_sdr_tipo(ler_saida(R640, "ipmitool__sdr__type__Fan"))[0]
        linha["status"] = "ns"
        assert avaliar_sdr([linha], "ventoinhas", "x", "vent.") is None


class TestSel:
    def test_classificacao_real(self):
        por_id = {e["id"]: e for e in sel_r640()}
        assert not evento_sel_e_problema(por_id["1"])  # log limpo
        assert not evento_sel_e_problema(por_id["4"])  # intrusão de chassi
        assert not evento_sel_e_problema(por_id["5"])  # intrusão deasserted
        assert evento_sel_e_problema(por_id["2"])  # AC lost
        assert evento_sel_e_problema(por_id["16"])  # processador deixou de ser detectado
        assert evento_sel_e_problema(por_id["25"])  # ECC não corrigível
        assert evento_sel_e_problema(por_id["26"])  # PCIe fatal
        assert evento_sel_e_problema(por_id["12"])  # cabo SAS config error
        assert not evento_sel_e_problema(por_id["42"])  # disco removido (Drive Present deasserted)

    def test_log_real_agrupado(self):
        r = avaliar_log_bmc(sel_r640())
        assert r.status == Status.ALERTA
        assert len(r.dados["eventos"]) == 79
        ac = [p for p in r.dados["problemas"] if "AC lost" in p]
        assert len(ac) == 1 and "ocorrências" in ac[0]

    def test_log_vazio(self):
        r = avaliar_log_bmc([])
        assert r.status == Status.OK and r.mensagem == "log do BMC vazio"


# --- lm-sensors -------------------------------------------------------------------


def chips_r640():
    return parse_sensors_json(ler_saida(R640, "sensors__-j"))


class TestLmSensors:
    def test_temperaturas_real(self):
        r = avaliar_temperaturas(chips_r640())
        assert r.status == Status.OK
        assert len(r.dados["leituras"]) == 39

    def test_acima_do_critico(self):
        chips = chips_r640()
        chips[0]["leituras"]["Package id 0"]["temp1_input"] = 105.0
        r = avaliar_temperaturas(chips)
        assert r.status == Status.FALHA

    def test_alarme_do_chip(self):
        chips = chips_r640()
        chips[0]["leituras"]["Core 0"]["temp2_crit_alarm"] = 1.0
        assert avaliar_temperaturas(chips).status == Status.FALHA

    def test_sem_ventoinhas_no_r640(self):
        assert avaliar_ventoinhas(chips_r640()) is None

    def test_ventoinhas_estrutura(self):
        # TODO(fixture): substituir por saída real de "sensors -j" com ventoinhas.
        chips = [
            {
                "chip": "chip",
                "adaptador": "ISA adapter",
                "leituras": {
                    "fan1": {"fan1_input": 3000.0, "fan1_min": 600.0},
                    "fan2": {"fan2_input": 0.0},
                    "fan3": {"fan3_input": 100.0, "fan3_min": 600.0},
                },
            }
        ]
        r = avaliar_ventoinhas(chips)
        assert r.status == Status.FALHA
        assert r.dados["ignoradas_sem_rotacao"] == ["chip / fan2"]
        assert len(r.dados["leituras"]) == 2


# --- CPU e memória -----------------------------------------------------------------


def cpus_r640():
    return parse_processadores(ler_saida(R640, "dmidecode__-t__4"))


def cpuinfo_r640():
    return parse_cpuinfo((R640 / "arquivos/proc/cpuinfo").read_text())


class TestCPU:
    def test_sem_sel_ok(self):
        r = avaliar_cpu(cpus_r640(), cpuinfo_r640(), [])
        assert r.status == Status.OK
        assert r.mensagem == "2 processador(es): Intel(R) Xeon(R) Gold 5220 CPU @ 2.20GHz; 72 processadores lógicos"

    def test_sel_real_gera_alerta(self):
        # "Presence detected" deasserted × 3 no SEL real, com as 2 CPUs presentes e habilitadas agora: histórico.
        r = avaliar_cpu(cpus_r640(), cpuinfo_r640(), sel_r640())
        assert r.status == Status.OK
        assert r.dados["problemas"] == [] and len(r.dados["historico"]) == 3
        assert all("presentes e habilitados agora" in h for h in r.dados["historico"])

    def test_soquete_desabilitado(self):
        cpus = cpus_r640()
        cpus[1].update(status="Populated, Disabled By BIOS", habilitado=False)
        r = avaliar_cpu(cpus, cpuinfo_r640(), [])
        assert r.status == Status.ALERTA
        assert "CPU2: Populated, Disabled By BIOS" in r.dados["problemas"]
        assert any("sistema operacional enxerga 2" in p for p in r.dados["problemas"])

    def test_so_cpuinfo(self):
        r = avaliar_cpu(None, cpuinfo_r640(), None)
        assert r.status == Status.OK
        assert r.fonte == "/proc/cpuinfo"
        assert len(r.dados["observacoes"]) == 2

    def test_sem_nada(self):
        assert avaliar_cpu(None, None, []) is None


def memorias_r640():
    return parse_memorias(ler_saida(R640, "dmidecode__-t__17"))


def edac_r640():
    ctx = detectar(ExecutorReplay(R640))
    return ler_edac(ctx)


class TestMemoria:
    def test_edac_real(self):
        edac = edac_r640()
        assert [mc["controlador"] for mc in edac] == ["mc0", "mc1", "mc2", "mc3"]
        assert edac[0]["nome"] == "Skylake Socket#0 IMC#0"
        assert edac[0]["tamanho_mb"] == 16384
        assert [m["rotulo"] for m in edac[0]["modulos"]] == [
            "CPU_SrcID#0_MC#0_Chan#0_DIMM#0",
            "CPU_SrcID#0_MC#0_Chan#1_DIMM#0",
        ]

    def test_sem_sel_ok(self):
        r = avaliar_memoria(memorias_r640(), edac_r640(), [])
        assert r.status == Status.OK
        assert r.mensagem == "2 de 24 slots com pente reconhecido, 16 GB"  # 22/10/2026: slots reconhecidos × total

    def test_erro_nao_corrigivel(self):
        edac = edac_r640()
        edac[0]["nao_corrigiveis"] = 1
        edac[0]["modulos"][1]["nao_corrigiveis"] = 1
        r = avaliar_memoria(memorias_r640(), edac, [])
        assert r.status == Status.FALHA
        assert any("CPU_SrcID#0_MC#0_Chan#1_DIMM#0" in p for p in r.dados["problemas"])

    def test_erro_corrigivel(self):
        edac = edac_r640()
        edac[0]["corrigiveis"] = 5
        assert avaliar_memoria(memorias_r640(), edac, []).status == Status.ALERTA

    def test_memoria_nao_reconhecida(self):
        edac = edac_r640()
        edac[0]["tamanho_mb"] = 8192
        r = avaliar_memoria(memorias_r640(), edac, [])
        assert r.status == Status.ALERTA
        assert any("maior que a reconhecida" in p for p in r.dados["problemas"])

    def test_sem_edac(self):
        r = avaliar_memoria(memorias_r640(), None, None)
        assert r.status == Status.OK
        assert any("EDAC indisponível" in o for o in r.dados["observacoes"])

    def test_sem_nada(self):
        assert avaliar_memoria(None, None, []) is None


# --- Discos ---------------------------------------------------------------------------


def disco_r640():
    return parse_smart(ler_saida(R640, "smartctl__-a__-j__-d__sat__dev_sda"))


class TestDiscos:
    def test_real(self):
        avaliacao = avaliar_disco(disco_r640())
        assert avaliacao["status"] == Status.OK and avaliacao["problemas"] == []

    def test_smart_reprovado(self):
        disco = disco_r640()
        disco["smart_passou"] = False
        disco["codigo_saida"] = 8
        assert avaliar_disco(disco)["status"] == Status.FALHA

    def test_nao_abriu(self):
        disco = disco_r640()
        disco["codigo_saida"] = 2
        assert avaliar_disco(disco)["status"] == Status.NAO_VERIFICADO

    def test_smart_desativado(self):
        disco = disco_r640()
        disco["smart_habilitado"] = False
        assert avaliar_disco(disco)["status"] == Status.NAO_VERIFICADO

    def test_setores_realocados(self):
        disco = disco_r640()
        next(a for a in disco["atributos"] if a["id"] == 5)["bruto"] = 12
        avaliacao = avaliar_disco(disco)
        assert avaliacao["status"] == Status.ALERTA and avaliacao["problemas"] == ["setores realocados: 12"]

    def test_atributo_falhando_agora(self):
        disco = disco_r640()
        disco["atributos"][0]["falhou_quando"] = "now"
        assert avaliar_disco(disco)["status"] == Status.FALHA

    def test_nvme_estrutura(self):
        # TODO(fixture): substituir por saída real de "smartctl -a -j" em NVMe.
        disco = disco_r640()
        disco.update(atributos=[], nvme={"critical_warning": 0, "media_errors": 0, "percentage_used": 95})
        assert avaliar_disco(disco)["status"] == Status.ALERTA
        disco["nvme"]["critical_warning"] = 4
        assert avaliar_disco(disco)["status"] == Status.FALHA


def executor_discos(scan: str | None = None, lsblk: str | None = None) -> ExecutorFalso:
    base = {
        ("smartctl", "--scan-open", "-j"): (0, scan or ler_saida(R640, "smartctl__--scan-open__-j"), ""),
        ("smartctl", "-a", "-j", "-d", "sat", "/dev/sda"): (0, ler_saida(R640, "smartctl__-a__-j__-d__sat__dev_sda"), ""),
        ("lsblk", "-J", "-d", "-o", "NAME,TRAN,TYPE,SIZE,MODEL"): (
            0,
            lsblk or ler_saida(R640, "lsblk__-J__-d__-o__NAME_TRAN_TYPE_SIZE_MODEL"),
            "",
        ),
    }
    return ExecutorFalso(comandos=base)


class TestProvedorSmartctl:
    def ctx(self, ex):
        return Contexto(executor=ex)

    def test_indisponivel_sem_smartctl(self):
        disp = ProvedorSmartctl().disponivel(self.ctx(ExecutorFalso()))
        assert not disp.ok and disp.motivo == "smartctl não instalado"

    def test_pendrive_usb_e_ignorado(self):
        lsblk = ler_saida(R640, "lsblk__-J__-d__-o__NAME_TRAN_TYPE_SIZE_MODEL").replace('"sata"', '"usb"')
        assert ProvedorSmartctl().coletar(self.ctx(executor_discos(lsblk=lsblk))) is None

    def test_disco_atras_de_raid_nao_verificado(self):
        scan = ler_saida(R640, "smartctl__--scan-open__-j").replace('"type": "sat"', '"type": "cciss,0"')
        r = ProvedorSmartctl().coletar(self.ctx(executor_discos(scan=scan)))
        assert r.status == Status.NAO_VERIFICADO
        assert "controladora RAID" in r.mensagem

    def test_real(self):
        r = ProvedorSmartctl().coletar(self.ctx(executor_discos()))
        assert r.status == Status.OK and r.mensagem == "1 disco(s) verificado(s)"
