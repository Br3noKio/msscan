"""Testes da detecção de capacidades."""

from __future__ import annotations

from msscan.relatorio.simbolos import ASCII

import pytest

from msscan.core.executor import ExecutorReplay
from msscan.deteccao import detectores
from msscan.deteccao.detectores import classificar_controladora, detectar
from msscan.deteccao.resumo import formatar_resumo
from tests.conftest import R640, ExecutorFalso, ler_saida


class TestR640Replay:
    @pytest.fixture
    def ctx(self):
        return detectar(ExecutorReplay(R640))

    def test_identificacao(self, ctx):
        assert (ctx.fabricante, ctx.modelo, ctx.versao_bios) == ("Dell Inc.", "PowerEdge R640", "2.21.2")

    def test_ipmi(self, ctx):
        assert ctx.ipmi.disponivel
        assert ctx.ipmi.versao == "2.0"
        assert ctx.ipmi.dispositivo == "/dev/ipmi0"
        assert ctx.ipmi.smbios["interface"].startswith("KCS")
        assert not ctx.ipmi.modprobe_tentado

    def test_redfish_nao_anunciado(self, ctx):
        assert not ctx.redfish.anunciado
        assert not ctx.redfish.confirmado
        assert "tipo 42" in ctx.redfish.motivo

    def test_controladoras_sao_raid_do_chipset(self, ctx):
        assert [c.categoria for c in ctx.controladoras] == ["raid_chipset", "raid_chipset"]
        assert ctx.controladoras_raid_hardware == []
        # Subsistema genérico "Dell Device" não é usado como nome.
        assert ctx.controladoras[1].nome.startswith("Intel Corporation C620")

    def test_sensores(self, ctx):
        assert len(ctx.sensores_kernel) == 4

    def test_sem_erros(self, ctx):
        assert ctx.erros_deteccao == {}

    def test_resumo(self, ctx):
        texto = formatar_resumo(ctx)
        assert "Servidor detectado: Dell Inc. PowerEdge R640" in texto
        assert "BMC: IPMI 2.0 disponível ✓" in texto
        assert "Redfish: não disponível" in texto
        assert "RAID: nenhuma controladora RAID de hardware" in texto
        assert "RAID do chipset (driver ahci)" in texto

    def test_resumo_ascii(self, ctx):
        assert "✓" not in formatar_resumo(ctx, ASCII)

    def test_contexto_serializavel(self, ctx):
        import json

        dados = json.loads(json.dumps(ctx.para_dict()))
        assert dados["modelo"] == "PowerEdge R640"
        assert "executor" not in dados


class TestSemNada:
    """Equipamento onde nenhuma ferramenta funciona: nada pode quebrar."""

    def test_nada_disponivel(self):
        ex = ExecutorFalso()
        ctx = detectar(ex)
        assert ctx.fabricante is None and ctx.modelo is None
        assert not ctx.ipmi.disponivel
        assert ctx.ipmi.modprobe_tentado
        assert "nenhuma interface IPMI" in ctx.ipmi.motivo
        assert not ctx.redfish.anunciado
        assert ctx.controladoras == []
        assert "controladoras" in ctx.erros_deteccao
        texto = formatar_resumo(ctx)
        assert "fabricante desconhecido" in texto
        assert "BMC: IPMI não disponível" in texto

    def test_modprobe_pode_ser_desligado(self):
        ex = ExecutorFalso()
        ctx = detectores.Contexto(executor=ex)
        detectores.detectar_ipmi(ctx, permitir_modprobe=False)
        assert not ctx.ipmi.modprobe_tentado
        assert ["modprobe", "ipmi_si"] not in ex.comandos_rodados()


class ExecutorComModprobe(ExecutorFalso):
    """/dev/ipmi0 só aparece depois do modprobe ipmi_devintf."""

    def _rodar(self, comando, timeout):
        if comando == ["modprobe", "ipmi_devintf"]:
            self.existentes.add("/dev/ipmi0")
            return 0, "", "", 0.0
        return super()._rodar(comando, timeout)


def test_modprobe_carrega_dispositivo_ipmi():
    ex = ExecutorComModprobe(
        comandos={
            ("dmidecode", "-t", "38"): (0, ler_saida(R640, "dmidecode__-t__38"), ""),
            ("ipmitool", "mc", "info"): (0, ler_saida(R640, "ipmitool__mc__info"), ""),
        }
    )
    ctx = detectores.Contexto(executor=ex)
    detectores.detectar_ipmi(ctx)
    assert ctx.ipmi.modprobe_tentado
    assert ctx.ipmi.disponivel
    assert ctx.ipmi.dispositivo == "/dev/ipmi0"


def test_smbios_anuncia_ipmi_mas_dispositivo_nao_aparece():
    ex = ExecutorFalso(comandos={("dmidecode", "-t", "38"): (0, ler_saida(R640, "dmidecode__-t__38"), "")})
    ctx = detectores.Contexto(executor=ex)
    detectores.detectar_ipmi(ctx)
    assert not ctx.ipmi.disponivel
    assert "anunciado no SMBIOS" in ctx.ipmi.motivo


def test_mc_info_falha():
    ex = ExecutorFalso(existentes={"/dev/ipmi0"})
    ctx = detectores.Contexto(executor=ex)
    detectores.detectar_ipmi(ctx)
    assert not ctx.ipmi.disponivel
    assert "ipmitool mc info" in ctx.ipmi.motivo
    assert not ctx.ipmi.modprobe_tentado


def test_falha_de_um_detector_nao_derruba_os_outros(monkeypatch):
    def explode(ctx):
        raise RuntimeError("detector quebrado")

    monkeypatch.setattr(
        detectores,
        "DETECTORES",
        [("ipmi", explode)] + [d for d in detectores.DETECTORES if d[0] != "ipmi"],
    )
    ctx = detectar(ExecutorReplay(R640))
    assert ctx.erros_deteccao == {"ipmi": "RuntimeError: detector quebrado"}
    assert ctx.modelo == "PowerEdge R640"
    assert len(ctx.sensores_kernel) == 4


@pytest.mark.parametrize(
    ("classe", "driver", "esperado"),
    [
        ("0104", "megaraid_sas", "raid_hardware"),
        ("0104", "hpsa", "raid_hardware"),
        ("0107", "mpt3sas", "hba_sas"),
        ("0108", "nvme", "nvme"),
        ("0104", "ahci", "raid_chipset"),
        ("0106", "ahci", "ahci"),
        ("0104", None, "sem_driver"),
        ("0104", "driver_novo", "raid_desconhecido"),
        ("0107", "driver_novo", "sas_desconhecido"),
    ],
)
def test_classificar_controladora(classe, driver, esperado):
    assert classificar_controladora(classe, driver) == esperado
