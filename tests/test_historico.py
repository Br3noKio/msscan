"""Eventos do SEL: HISTÓRICO × ATUAL (pedido de 01/10/2026) e seleção antiga na tela de aviso (bug 3a).

Caso real: o técnico retirou o pente A2 do R640 (ficou só o A1) e o "Uncorrectable ECC" antigo do A2, ainda no SEL,
continuava bloqueando a atualização.
"""

from __future__ import annotations

from tests.conftest import R640

from msscan.atualizacao.aviso import BLOQUEIA, HISTORICO, avaliar_hardware
from msscan.core.modelo import Status
from msscan.historico import ATUAL, classificar_cpu, classificar_memoria, critico, numero_cpu, slot_dimm
from msscan.interface.sessao import selecao_desatualizada
from msscan.parsers.dmidecode import parse_memorias
from msscan.parsers.ipmitool import parse_sel_elist
from msscan.provedores.sistema import avaliar_memoria

SEL = parse_sel_elist((R640 / "comandos" / "ipmitool__sel__elist.stdout").read_text())
MEMORIAS = parse_memorias((R640 / "comandos" / "dmidecode__-t__17.stdout").read_text())
EDAC_LIMPO = [{"controlador": "mc0", "nome": "", "corrigiveis": 0, "nao_corrigiveis": 0, "tamanho_mb": 8192, "modulos": []}]


def sem_a2() -> list[dict]:
    """A coleta real tinha A1 e A2; o técnico retirou o A2 (slot passa a "No Module Installed")."""
    return [{**m, "instalado": False, "tamanho_mb": None} if m["slot"] == "A2" else m for m in MEMORIAS]


def test_identifica_slot_e_cpu():
    assert slot_dimm("Uncorrectable ECC (UnCorrectable ECC |  DIMMA2)") == "A2"
    assert slot_dimm("DIMM B12") == "B12" and slot_dimm("Memory ECC") is None
    assert numero_cpu("CPU2 Status") == 2 and numero_cpu("Processor Status") is None
    assert critico({"sensor": "Memory ECC Uncorr Err", "evento": "Uncorrectable ECC"})
    assert not critico({"sensor": "Memory", "evento": "Correctable ECC"})


def test_pente_retirado_vira_historico_e_nao_muda_o_status():
    r = avaliar_memoria(sem_a2(), EDAC_LIMPO, SEL)
    assert r.status == Status.OK, r.dados["problemas"]
    [item] = r.dados["eventos_sel"]
    assert item["classe"] == "historico" and item["presente"] is False and item["critico"]
    assert "não está mais instalada" in r.dados["historico"][0] and "slot A2" in r.dados["historico"][0]
    assert any("exporte e limpe o SEL" in e for e in r.dados["explicacoes"])


def test_pente_retirado_nao_bloqueia_a_atualizacao_mas_pede_confirmacao():
    r = avaliar_memoria(sem_a2(), EDAC_LIMPO, SEL).para_dict()
    medido = {"fontes": {"status": "OK", "dados": {}}, "memoria": r, "raid": {"status": "NAO_APLICAVEL", "dados": {}},
              "sensores": {"status": "OK", "dados": {}}, "ventoinhas": {"status": "OK", "dados": {}}}  # fmt: skip
    linhas = {x["item"]: x["situacao"] for x in avaliar_hardware(medido)}
    assert BLOQUEIA not in linhas.values() and linhas["Registro de eventos (eventos anteriores)"] == HISTORICO


def test_pente_presente_continua_alerta_marcado_como_historico():
    r = avaliar_memoria(MEMORIAS, EDAC_LIMPO, SEL)
    assert r.status == Status.ALERTA and any("histórico" in p and "slot A2" in p for p in r.dados["problemas"])


def test_edac_com_erro_confirma_evento_de_peca_presente():
    [item] = classificar_memoria([{"id": "1", "sensor": "Memory", "evento": "Uncorrectable ECC DIMMA1"}], {"A1"}, {"*": 3})
    assert item["classe"] == ATUAL and "EDAC" in item["nota"]
    # Mesmo com erro no EDAC, evento de slot vazio continua histórico (não há peça para ter o erro).
    [item] = classificar_memoria([{"id": "1", "sensor": "Memory", "evento": "Uncorrectable ECC DIMMA2"}], {"A1"}, {"*": 3})
    assert item["classe"] == "historico"


def test_cpu_ausente_e_desabilitada():
    soquetes = [{"soquete": "CPU1", "status": "Populated, Enabled", "populado": True, "habilitado": True},
                {"soquete": "CPU2", "status": "Unpopulated", "populado": False, "habilitado": False}]  # fmt: skip
    [item] = classificar_cpu([{"id": "9", "sensor": "CPU2 Status", "evento": "IERR"}], soquetes)
    assert item["presente"] is False and "não está mais instalada" in item["nota"]
    soquetes[1].update(status="Populated, Disabled By BIOS", populado=True)
    [item] = classificar_cpu([{"id": "9", "sensor": "CPU2 Status", "evento": "IERR"}], soquetes)
    assert item["classe"] == ATUAL


def test_selecao_antiga_recusada_com_o_que_fazer():
    """Bug 3a: caixas de uma lista antiga viravam "item1: item desconhecido" e "0 min" no aviso."""
    assert "não está concluída" in selecao_desatualizada({"situacao": "verificando"}, ["item1"])
    verificada = {"situacao": "verificada", "itens": [{"id": "item1"}, {"id": "item2"}]}
    assert "Selecione os componentes novamente" in selecao_desatualizada(verificada, ["item1", "item5", "item6"])
    assert "Selecione os componentes novamente" in selecao_desatualizada(verificada, [])
    assert selecao_desatualizada(verificada, ["item2"]) == ""
