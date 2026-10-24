"""Configurar o iDRAC pelo acesso interno (pedido de 01/10/2026, CLAUDE.md 17.4): leitura, rede, pass-through e o
usuário administrador temporário. Nenhum teste altera um iDRAC: só o executor falso com ``alvo_simulado``."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from msscan.acoes import idrac
from msscan.acoes.comum import MODO_APLICAR, MODO_SIMULAR
from msscan.core.executor import MOTIVO_TRAVA_ACOES, validar_acao_idrac
from msscan.parsers.ipmitool import parse_user_list
from msscan.parsers.racadm import parse_racadm_get
from tests.conftest import PASTA_FIXTURES, ExecutorFalso

FIXTURE = PASTA_FIXTURES / "dell" / "poweredge_r640_idrac"
LISTA = (FIXTURE / "ipmitool_user_list_1.txt").read_text()
LAN = (FIXTURE / "ipmitool_lan_print_1.txt").read_text()
SENHA = "Ab3" + "x" * 17


class IdracFalso(ExecutorFalso):
    """iDRAC falso: guarda os usuários e a rede e responde ao ``user list``/``lan print`` de acordo."""

    alvo_simulado = True

    def __init__(self):
        super().__init__()
        self.usuarios = {u["slot"]: dict(u) for u in parse_user_list(LISTA)}
        self.escritos: list[list[str]] = []
        self.recusar_nome_vazio = False

    def _lista(self) -> str:
        linhas = ["ID  Name\t     Callin  Link Auth\tIPMI Msg   Channel Priv Limit"]
        for slot, u in sorted(self.usuarios.items()):
            linhas.append(f"{slot:<4}{u['nome']:<17}true    false      false      {u['privilegio']}")
        return "\n".join(linhas) + "\n"

    def _rodar(self, comando, timeout):
        a = list(comando[1:])
        if a == ["user", "list", "1"]:
            return 0, self._lista(), "", 0.0
        if a == ["lan", "print", "1"]:
            return 0, LAN, "", 0.0
        if a == ["delloem", "lan", "get"]:  # saída real deste iDRAC
            return 1, "", (FIXTURE / "ipmitool_delloem_lan_get.txt").read_text(), 0.0
        self.escritos.append(list(comando))
        if a[:3] == ["user", "set", "name"]:
            if a[4] == "" and self.recusar_nome_vazio:
                return 1, "", "Invalid data field in request", 0.0
            self.usuarios[int(a[3])]["nome"] = a[4]
        elif a[:2] == ["channel", "setaccess"]:
            self.usuarios[int(a[3])]["privilegio"] = "ADMINISTRATOR" if "privilege=4" in a else "NO ACCESS"
        return 0, "", "", 0.0


def contexto(executor) -> SimpleNamespace:
    return SimpleNamespace(executor=executor, ipmi=SimpleNamespace(disponivel=True), ferramentas_fabricante={}, cache={},
                           bmc=None, redfish=SimpleNamespace(confirmado=False, motivo="sem rota", endereco=None, origem=None))  # fmt: skip


def test_parsers_com_saida_real():
    usuarios = parse_user_list(LISTA)
    assert len(usuarios) == 16 and usuarios[1]["nome"] == "root" and usuarios[2] == {
        "slot": 3, "nome": "", "callin": True, "link_auth": True, "ipmi_msg": True, "privilegio": "ADMINISTRATOR"}
    # Slot 3 sem nome mas com acesso de administrador (real): nunca é usado; o primeiro livre é o 4.
    assert idrac.slot_livre(usuarios) == 4
    assert parse_racadm_get("[Key=iDRAC.Embedded.1#OS-BMC.1]\nAdminState=Disabled\n#PTCapability=Capable\n") == {
        "AdminState": "Disabled", "PTCapability": "Capable"}


def test_salvaguarda_nunca_altera_usuario_existente():
    temporarios = {4: "msscan1a2b"}
    assert validar_acao_idrac(["ipmitool", "user", "set", "password", "2", SENHA, "20"], temporarios)  # root de fábrica
    assert validar_acao_idrac(["ipmitool", "user", "set", "password", "3", SENHA, "20"], temporarios)  # existente
    assert validar_acao_idrac(["ipmitool", "user", "set", "name", "3", "admin"], temporarios)
    assert validar_acao_idrac(["ipmitool", "user", "set", "name", "3", ""], temporarios)  # apagar só o do MSscan
    assert validar_acao_idrac(["racadm", "set", "iDRAC.Users.3.Password", SENHA], temporarios)
    assert validar_acao_idrac(["ipmitool", "raw", "0x06", "0x45"], temporarios)
    assert validar_acao_idrac(["ipmitool", "lan", "set", "1", "ipaddr", "300.1.1.1"], temporarios)
    assert validar_acao_idrac(["racadm", "set", "iDRAC.OS-BMC.PTMode", "2"], temporarios)
    assert validar_acao_idrac(["ipmitool", "user", "set", "password", "4", SENHA, "20"], temporarios) == ""
    assert validar_acao_idrac(["racadm", "set", "iDRAC.Users.4.Privilege", "0x1ff"], temporarios) == ""


def test_rodar_recusa_racadm_que_grava():
    executor = ExecutorFalso()
    rc, _, erro = executor.rodar(["racadm", "set", "iDRAC.IPv4.DHCPEnable", "1"])
    assert rc == -1 and "recusado" in erro
    assert executor.rodar(["racadm", "get", "iDRAC.IPv4"])[2] != erro


def test_usuario_temporario_cria_e_apaga_e_senha_nunca_no_historico(tmp_path):
    executor = IdracFalso()
    ctx = contexto(executor)
    pre = idrac.prechecar_usuario(ctx, persistente=True)
    assert pre["pode_seguir"] and pre["parametros"]["slot"] == 4 and idrac.AVISO_CLIENTE in pre["avisos"]
    resultado = idrac.executar(ctx, executor, pre, MODO_APLICAR, str(tmp_path))
    nome = pre["parametros"]["nome"]
    assert resultado["situacao"] == "aplicada", resultado["mensagem"]
    assert executor.usuarios[4]["nome"] == nome and executor.slots_temporarios == {4: nome}
    assert json.loads((tmp_path / idrac.MARCADOR).read_text())["slot"] == 4
    senha = ctx.cache["usuario_temporario"]["senha"]
    assert len(senha) == 20 and all(senha not in " ".join(e.comando) for e in executor.historico)
    assert "senha" not in (tmp_path / idrac.MARCADOR).read_text()
    registro = idrac.apagar(ctx, executor, str(tmp_path))
    assert registro["situacao"] == "apagado" and executor.usuarios[4]["nome"] == "" and executor.slots_temporarios == {}
    assert not (tmp_path / idrac.MARCADOR).exists() and "usuario_temporario" not in ctx.cache
    # Nenhum comando tocou outro slot.
    assert all("4" in c[3:5] for c in executor.escritos if c[1] in ("user", "channel"))


def test_proximo_boot_apaga_pelo_marcador_e_nao_toca_em_outro_usuario(tmp_path):
    executor = IdracFalso()
    executor.usuarios[5]["nome"] = "msscanbeef"
    executor.usuarios[5]["privilegio"] = "ADMINISTRATOR"
    (tmp_path / "estado").mkdir()
    (tmp_path / idrac.MARCADOR).write_text(json.dumps({"slot": 5, "nome": "msscanbeef", "metodo": "ipmi"}))
    registro = idrac.apagar(contexto(executor), executor, str(tmp_path))
    assert registro["situacao"] == "apagado" and executor.usuarios[5]["nome"] == ""
    # Marcador de um slot que hoje tem outro usuário: nada é alterado.
    executor2 = IdracFalso()
    executor2.usuarios[6]["nome"] = "joao"
    (tmp_path / idrac.MARCADOR).write_text(json.dumps({"slot": 6, "nome": "msscan0001", "metodo": "ipmi"}))
    idrac.apagar(contexto(executor2), executor2, str(tmp_path))
    assert executor2.escritos == [] and executor2.usuarios[6]["nome"] == "joao"


def test_nome_vazio_recusado_deixa_usuario_sem_acesso(tmp_path):
    executor = IdracFalso()
    executor.recusar_nome_vazio = True
    ctx = contexto(executor)
    pre = idrac.prechecar_usuario(ctx, persistente=True)
    idrac.executar(ctx, executor, pre, MODO_APLICAR, str(tmp_path))
    registro = idrac.apagar(ctx, executor, str(tmp_path))
    assert registro["situacao"] == "apagado" and executor.usuarios[4]["privilegio"] == "NO ACCESS"
    assert any(p["situacao"] == "aviso" and "desativado" in p["detalhe"] for p in registro["passos"])


def test_trava_impede_criar(tmp_path):
    executor = IdracFalso()
    executor.alvo_simulado = False
    ctx = contexto(executor)
    pre = idrac.prechecar_usuario(ctx, persistente=True)
    resultado = idrac.executar(ctx, executor, pre, MODO_APLICAR, str(tmp_path))
    assert resultado["situacao"] in ("bloqueada_trava", "falhou") and executor.escritos == []
    assert MOTIVO_TRAVA_ACOES in resultado["mensagem"] and executor.usuarios[4]["nome"] == ""


@pytest.mark.parametrize("comando", [
    ["ipmitool", "lan", "set", "1", "ipsrc", "dhcp"], ["ipmitool", "lan", "set", "1", "ipaddr", "192.0.2.10"],
    ["ipmitool", "lan", "set", "1", "defgw", "ipaddr", "192.0.2.1"], ["racadm", "set", "iDRAC.IPv4.DHCPEnable", "1"],
    ["racadm", "set", "iDRAC.IPv4.Address", "192.0.2.10"], ["racadm", "set", "iDRAC.NIC.Selection", "1"],
])  # fmt: skip
def test_rede_do_idrac_nunca_e_alterada(comando):
    """18.4 (decisão do usuário, 01/10/2026): a ação ``idrac.rede`` saiu; nenhum comando de rede passa pelo executor,
    nem com a trava liberada nem num alvo simulado."""
    executor = IdracFalso()
    assert "nunca é alterada" in executor.acao_comando(comando)[2] and executor.escritos == []
    assert idrac.prechecar_ver(contexto(executor))["nada_a_fazer"]


def test_passthrough_sem_racadm_explica():
    pre = idrac.prechecar_passthrough(contexto(IdracFalso()), persistente=True)
    assert not pre["pode_seguir"] and "racadm" in pre["bloqueios"][0]


def test_ver_so_leitura():
    executor = IdracFalso()
    pre = idrac.prechecar_ver(contexto(executor))
    assert pre["nada_a_fazer"] and not pre["pode_seguir"] and executor.escritos == []
    assert pre["antes"]["IP"] == "192.0.2.82" and pre["antes"]["Usuários (sem senhas)"] == ["2: root (administrator)"]
