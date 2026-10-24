"""CLAUDE.md 21.5: ação avulsa ``idrac.ip_fixo`` (exceção pedida pelo usuário à 18.4). Nenhum teste altera um iDRAC:
só o executor falso com ``alvo_simulado``."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from msscan.acoes import execucao, idrac
from msscan.acoes.comum import MODO_APLICAR
from msscan.core import executor as modulo_executor
from msscan.core.executor import MOTIVO_TRAVA_ACOES, ExecutorReal, validar_rede_idrac
from msscan.rede import verificacao
from tests.test_idrac import LAN, IdracFalso, contexto

DHCP_SEM_IP = (LAN.replace("IP Address              : 192.0.2.82", "IP Address              : 0.0.0.0")
               .replace("Subnet Mask             : 255.255.255.0", "Subnet Mask             : 0.0.0.0")
               .replace("Default Gateway IP      : 192.0.2.1", "Default Gateway IP      : 0.0.0.0"))  # fmt: skip
PEDIDO = {"ip": "192.0.2.90", "mascara": "255.255.255.0", "gateway": "192.0.2.1"}


class RedeFalsa(IdracFalso):
    """iDRAC falso com a rede de verdade: ``lan set`` muda o que o ``lan print`` mostra; ping/arping programáveis."""

    def __init__(self, ping: int = 1, arping: int = 0):
        super().__init__()
        self.rede = {"origem": "DHCP Address", "ip": "0.0.0.0", "mascara": "0.0.0.0", "gateway": "0.0.0.0"}
        self.ping, self.arping = ping, arping
        self.testes: list[list[str]] = []

    def _lan(self) -> str:
        return (DHCP_SEM_IP.replace("DHCP Address", self.rede["origem"]).replace("IP Address              : 0.0.0.0",
                f"IP Address              : {self.rede['ip']}").replace("Subnet Mask             : 0.0.0.0",
                f"Subnet Mask             : {self.rede['mascara']}").replace("Default Gateway IP      : 0.0.0.0",
                f"Default Gateway IP      : {self.rede['gateway']}"))  # fmt: skip

    def _rodar(self, comando, timeout):
        a = list(comando[1:])
        if comando[0] in ("ping", "arping"):
            self.testes.append(list(comando))
            return {"ping": self.ping, "arping": self.arping}[comando[0]], "", "", 0.0
        if a == ["lan", "print", "1"]:
            return 0, self._lan(), "", 0.0
        if a[:3] == ["lan", "set", "1"]:
            self.escritos.append(list(comando))
            chave = {"ipsrc": "origem", "ipaddr": "ip", "netmask": "mascara", "defgw": "gateway"}[a[3]]
            self.rede[chave] = {"static": "Static Address", "dhcp": "DHCP Address"}.get(a[-1], a[-1])
            return 0, "", "", 0.0
        return super()._rodar(comando, timeout)


@pytest.fixture(autouse=True)
def _sem_placas(monkeypatch):
    monkeypatch.setattr(verificacao, "listar_interfaces", lambda executor: [])


def test_dhcp_sem_ip_vira_ip_fixo_conferido_e_desfazer_volta_ao_dhcp(tmp_path):
    executor = RedeFalsa()
    ctx = contexto(executor)
    pre = execucao.prechecar(idrac.IP_FIXO, ctx, executor, PEDIDO, persistente=True)
    assert pre["pode_seguir"], pre["bloqueios"]
    assert pre["antes"]["Origem do IP"] == "DHCP" and pre["antes"]["IP"] == "0.0.0.0"
    assert any(p.startswith("IP livre: sem resposta ao ping") for p in pre["previa"]) and executor.escritos == []
    assert executor.testes == [["ping", "-n", "-c", "2", "-W", "1", "192.0.2.90"]]
    resultado = execucao.executar(pre, "CORRIGIR", ctx, executor, str(tmp_path), MODO_APLICAR)
    assert resultado["situacao"] == "aplicada", resultado["mensagem"]
    assert resultado["depois"] == {"IP": "192.0.2.90", "Origem do IP": "fixo", "Máscara": "255.255.255.0", "Gateway": "192.0.2.1"}
    assert [c[4:] for c in executor.escritos] == [["ipsrc", "static"], ["ipaddr", "192.0.2.90"], ["netmask", "255.255.255.0"],
                                                  ["defgw", "ipaddr", "192.0.2.1"]]  # fmt: skip
    assert Path(resultado["anteriores"]["arquivo"]).is_file()
    desfazer = execucao.prechecar_desfazer(resultado, ctx)
    assert desfazer["pode_seguir"] and desfazer["comandos"] == [["ipmitool", "lan", "set", "1", "ipsrc", "dhcp"]]
    registro = execucao.desfazer(desfazer, resultado, "DESFAZER", ctx, MODO_APLICAR, executor=executor, pasta_dados=str(tmp_path))
    assert registro["situacao"] == "desfeita" and executor.rede["origem"] == "DHCP Address"


@pytest.mark.parametrize("ping, arping, placas, trecho", [
    (0, 0, False, "respondeu ao ping"),
    (2, 0, False, "ping não pôde ser executado"),
    (-1, 0, False, "ping não pôde ser executado"),  # ping ausente na ISO
    (1, 1, True, "respondeu ao ARP"),
])  # fmt: skip
def test_ip_em_uso_ou_nao_testado_bloqueia(monkeypatch, ping, arping, placas, trecho):
    if placas:
        placa = SimpleNamespace(nome="eno1", fisica=True, nic_bmc=False, link=True, enderecos=["192.0.2.85/24"])
        monkeypatch.setattr(verificacao, "listar_interfaces", lambda executor: [placa])
    executor = RedeFalsa(ping=ping, arping=arping)
    pre = idrac.prechecar_ip_fixo(contexto(executor), executor, PEDIDO, persistente=True)
    assert not pre["pode_seguir"] and trecho in pre["bloqueios"][0] and not pre["comandos"]


def test_arping_na_mesma_rede_confirma_livre(monkeypatch):
    placa = SimpleNamespace(nome="eno1", fisica=True, nic_bmc=False, link=True, enderecos=["192.0.2.85/24"])
    monkeypatch.setattr(verificacao, "listar_interfaces", lambda executor: [placa])
    executor = RedeFalsa()
    pre = idrac.prechecar_ip_fixo(contexto(executor), executor, PEDIDO, persistente=True)
    assert pre["pode_seguir"] and ["arping", "-D", "-c", "2", "-w", "3", "-I", "eno1", "192.0.2.90"] in executor.testes
    assert "nem ao ARP (placa eno1, mesma rede)" in pre["previa"][0]


@pytest.mark.parametrize("parametros, trecho", [
    ({"ip": "192.0.2.90", "mascara": "255.255.255.0"}, "Informe IP, máscara e gateway"),
    ({**PEDIDO, "gateway": "198.51.100.1"}, "precisa estar na rede"),
    ({**PEDIDO, "gateway": "192.0.2.90"}, "diferente do IP"),
    ({**PEDIDO, "ip": "192.0.2.255"}, "broadcast"),
    ({**PEDIDO, "mascara": "255.0.255.0"}, "Máscara inválida"),
])  # fmt: skip
def test_parametros_invalidos(parametros, trecho):
    executor = RedeFalsa()
    pre = idrac.prechecar_ip_fixo(contexto(executor), executor, parametros, persistente=True)
    assert not pre["pode_seguir"] and trecho in " ".join(pre["bloqueios"]) and executor.testes == []


def test_sem_pendrive_gravavel_bloqueia():
    executor = RedeFalsa()
    assert not idrac.prechecar_ip_fixo(contexto(executor), executor, PEDIDO, persistente=False)["pode_seguir"]


def test_trava_das_acoes_vale(monkeypatch, tmp_path):
    monkeypatch.setattr(modulo_executor, "ACOES_REAIS_LIBERADAS", False)
    assert MOTIVO_TRAVA_ACOES in ExecutorReal().acao_rede_idrac(["ipmitool", "lan", "set", "1", "ipsrc", "static"])[2]
    executor = RedeFalsa()
    executor.alvo_simulado = False
    ctx = contexto(executor)
    pre = idrac.prechecar_ip_fixo(ctx, executor, PEDIDO, persistente=True)
    resultado = execucao.executar(pre, "CORRIGIR", ctx, executor, str(tmp_path), MODO_APLICAR)
    assert resultado["situacao"] == "bloqueada_trava" and executor.escritos == []


@pytest.mark.parametrize("comando", [
    ["ipmitool", "lan", "set", "1", "vlan", "id", "10"], ["ipmitool", "lan", "set", "1", "ipaddr", "300.1.1.1"],
    ["ipmitool", "lan", "set", "1", "password", "x"], ["ipmitool", "user", "set", "password", "2", "x"],
    ["racadm", "set", "iDRAC.NIC.Selection", "1"], ["racadm", "set", "iDRAC.IPv6.Enable", "1"],
    ["racadm", "set", "iDRAC.IPv4.Address", "192.0.2.1; reboot"], ["racadm", "racreset"],
])  # fmt: skip
def test_lista_fechada_da_rede(comando):
    assert validar_rede_idrac(comando) != ""


def test_atualizacao_automatica_nunca_muda_a_rede_do_idrac():
    """18.4 continua valendo para a atualização automática: nenhum código dela chama a escrita de rede, e o caminho comum
    das ações (``acao_comando``) continua recusando ``lan set``."""
    raiz = Path(modulo_executor.__file__).parents[1]
    for arquivo in (raiz / "atualizacao").glob("*.py"):
        texto = arquivo.read_text(encoding="utf-8")
        assert "acao_rede_idrac" not in texto and "ip_fixo" not in texto, arquivo
    assert "nunca é alterada" in IdracFalso().acao_comando(["ipmitool", "lan", "set", "1", "ipsrc", "static"])[2]
