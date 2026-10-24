"""Mapa por posição (pedido de 22/10/2026): qual peça funciona, qual não, e onde está.

Caso que motivou: 24 pentes de 32 GB instalados (768 GB), o sistema mostrava 704 GB. Pente desabilitado no POST aparece no
SMBIOS como "No Module Installed", igual a slot vazio; o MSscan precisa apontar o slot pelas outras evidências."""

from __future__ import annotations

import json

from msscan import posicoes
from msscan.core.executor import ExecutorReplay
from msscan.core.modelo import Status
from msscan.deteccao.detectores import detectar
from msscan.modulos.registro import registro_padrao
from msscan.parsers.dmidecode import parse_memorias
from msscan.parsers.redfish import parse_memoria
from msscan.provedores.redfish import ler_memoria_redfish
from msscan.provedores.sistema import CONFERIR, VAZIO, avaliar_memoria, eventos_memoria
from msscan.relatorio.html import _componente
from tests.conftest import PASTA_FIXTURES, R620, R640, ler_saida

BASE = parse_memorias(ler_saida(R640, "dmidecode__-t__17"))[0]
SLOTS = [f"{b}{n}" for b in "AB" for n in range(1, 13)]


def memorias_24x32(sem: tuple[str, ...]) -> list[dict]:
    """24 slots com pentes de 32 GB; os de ``sem`` aparecem vazios no SMBIOS (desabilitados no POST)."""
    return [{**BASE, "slot": s, "instalado": s not in sem, "tamanho_mb": None if s in sem else 32768} for s in SLOTS]


def situacao(resultado, slot: str) -> str:
    return next(p["situacao"] for p in resultado.dados["posicoes"] if p["posicao"] == slot)


def test_caso_do_usuario_768_gb_vira_704_e_aponta_o_slot():
    r = avaliar_memoria(memorias_24x32(("B5", "B12")), None, [], dell=True)
    assert "22 de 24 slots" in r.mensagem and "704 GB" in r.mensagem and "B5" in r.mensagem
    assert r.status == Status.ALERTA and situacao(r, "B5") == "ALERTA"
    assert any(p.startswith("DIMM B5: pente provavelmente instalado e NÃO reconhecido") for p in r.dados["problemas"])
    linha = next(p for p in r.dados["posicoes"] if p["posicao"] == "B5")
    assert linha["local"] == "DIMM B5 (CPU2)"
    # B12 é o último do banco: sem o BMC, só "conferir" e a explicação com a lista dos slots vistos vazios.
    assert situacao(r, "B12") == CONFERIR and any("B12" in e for e in r.dados["explicacoes"])


def test_bmc_ve_o_pente_que_o_sistema_nao_ve():
    bmc = [{"slot": "DIMM B12", "estado": "Disabled", "saude": "Critical", "capacidade_mb": 32768}]
    r = avaliar_memoria(memorias_24x32(("B12",)), None, [], bmc=bmc, dell=True)
    assert r.status == Status.FALHA and situacao(r, "B12") == "FALHA"
    assert any("o BMC vê um pente de 32 GB" in p for p in r.dados["problemas"])
    assert "Redfish" in r.fonte


def test_bmc_com_slot_ausente_nao_inventa_pente():
    bmc = [{"slot": "DIMM B12", "estado": "Absent", "saude": None, "capacidade_mb": None}]
    r = avaliar_memoria(memorias_24x32(("B12",)), None, [], bmc=bmc, dell=True)
    assert situacao(r, "B12") == CONFERIR and r.status == Status.OK


def test_sel_memory_device_disabled_aponta_o_slot():
    evento = {"id": "7", "data": "10/20/2026", "hora": "09:00:00", "sensor": "Memory DIMM_B12", "tipo": "Memory",
              "evento": "Memory Device Disabled (DIMM_B12)", "direcao": "Asserted"}  # fmt: skip
    assert eventos_memoria([evento]) == [evento]  # sem "error"/"fail": o filtro geral do SEL deixava passar
    assert eventos_memoria([{**evento, "direcao": "Deasserted"}]) == []
    r = avaliar_memoria(memorias_24x32(("B12",)), None, [evento], dell=True)
    assert situacao(r, "B12") == "ALERTA" and any("desabilitado" in p and "DIMM B12" in p for p in r.dados["problemas"])


def test_pente_retirado_continua_historico_17_1():
    """17.1: erro antigo de um slot que agora está vazio não muda o status (o pente pode ter sido retirado)."""
    evento = {"id": "3", "sensor": "Memory ECC", "tipo": "Memory", "evento": "Uncorrectable ECC (DIMMB12)", "direcao": "Asserted"}
    r = avaliar_memoria(memorias_24x32(("B12",)), None, [evento], dell=True)
    assert r.status == Status.OK and situacao(r, "B12") == CONFERIR


def test_coletas_reais_sem_falso_positivo():
    for pasta, ocupados in ((R620, 4), (R640, 2)):
        r = avaliar_memoria(parse_memorias(ler_saida(pasta, "dmidecode__-t__17")), None, [], dell=True)
        assert r.status == Status.OK, r.dados["problemas"]
        assert sum(p["situacao"] == "OK" for p in r.dados["posicoes"]) == ocupados
        assert {p["situacao"] for p in r.dados["posicoes"]} == {"OK", VAZIO}
        assert not any("vê vazios" in e for e in r.dados.get("explicacoes") or [])  # poucos pentes de propósito: sem a lista


def test_ultimos_slots_dos_dois_bancos_ainda_aparecem_na_explicacao():
    r = avaliar_memoria(memorias_24x32(("A12", "B12")), None, [], dell=True)  # simétrico e no fim: nenhum indício forte
    assert r.status == Status.OK and any("A12, B12" in e for e in r.dados["explicacoes"])


def test_redfish_memoria_da_dmtf():
    pentes = [parse_memoria(p) for p in json.loads((PASTA_FIXTURES / "redfish/dmtf/public_rackmount1_memoria.json").read_text())["pentes"]]
    assert pentes[0] == {"slot": "DIMM 1", "id": "DIMM1", "saude": "OK", "estado": "Enabled", "capacidade_mb": 32768}
    assert pentes[3]["estado"] == "Absent" and pentes[3]["capacidade_mb"] is None
    assert parse_memoria({"Id": "DIMM.Socket.A1", "DeviceLocator": "DIMM A1", "Status": {"State": "Enabled", "Health": "OK"},
                          "CapacityMiB": 32768})["slot"] == "DIMM A1"  # fmt: skip


def test_sem_credenciais_nao_le_memoria_pelo_redfish():
    assert ler_memoria_redfish(detectar(ExecutorReplay(R640), None)) is None


def test_mapa_dos_demais_componentes_no_r620_real():
    ctx = detectar(ExecutorReplay(R620), None)
    res = {r.componente: r for r in registro_padrao().diagnosticar(ctx)}
    mapa = {nome: {p["local"]: p["situacao"] for p in res[nome].dados["posicoes"]} for nome in ("cpu", "fontes", "ventoinhas", "discos", "rede")}
    assert mapa["cpu"] == {"CPU1": "OK", "CPU2": "OK"}
    assert mapa["fontes"] == {"Fonte 1 (PSU1)": "OK", "Fonte 2 (PSU2)": "OK"}  # "PS Redundancy" não é uma posição
    assert not any("Redundancy" in nome for nome in mapa["ventoinhas"]) and set(mapa["ventoinhas"].values()) == {"OK"}
    assert mapa["discos"]["disco ID 1 da controladora"] == CONFERIR  # a PERC lista, o disco não responde
    assert any(s == "ALERTA" for nome, s in mapa["discos"].items() if nome.startswith("disco ID 0"))
    assert VAZIO in mapa["rede"].values() and "CONFERIR" not in mapa["rede"].values()  # porta sem cabo não é defeito


def test_cpu_desabilitada_e_soquete_vazio():
    dados = {"processadores": [{"soquete": "CPU1", "status": "Populated, Enabled", "populado": True, "habilitado": True},
                               {"soquete": "CPU2", "status": "Populated, Disabled By BIOS", "populado": True, "habilitado": False}],
             "problemas": ["CPU2: Populated, Disabled By BIOS"]}  # fmt: skip
    assert [p["situacao"] for p in posicoes.montar("cpu", dados)] == ["OK", "ALERTA"]
    dados["processadores"][1].update(status="Unpopulated", populado=False)
    assert [p["situacao"] for p in posicoes.montar("cpu", dados)] == ["OK", VAZIO]
    assert posicoes.montar("memoria", {"posicoes": []}) is None


def test_relatorio_html_mostra_as_posicoes():
    r = avaliar_memoria(memorias_24x32(("B5",)), None, [], dell=True).para_dict()
    html = _componente({**r, "titulo": "Memória"})
    assert "Por posição" in html and "DIMM B5 (CPU2)" in html and "posição(ões) vazia(s)" not in html
