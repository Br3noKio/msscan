"""Tela de AVISO obrigatória antes de instalar (CLAUDE.md 15.6): ordem, estimativa, hardware na hora, conferência."""

from __future__ import annotations

from msscan.atualizacao import aviso
from msscan.atualizacao.aviso import (
    AVISO,
    BLOQUEIA,
    HISTORICO,
    OK,
    REDUNDANCIA,
    VALIDADE_S,
    avaliar_hardware,
    conferir_aviso,
    estimativa,
    item_do_catalogo,
    montar_aviso,
    ordenar,
)


def r(status: str, mensagem: str = "", **dados) -> dict:
    return {"status": status, "mensagem": mensagem, "dados": dados}


SAUDAVEL = {"fontes": r("OK", "2 fontes OK"), "memoria": r("OK", "12 módulos"), "raid": r("NAO_APLICAVEL"),
            "sensores": r("OK"), "ventoinhas": r("OK")}  # fmt: skip


def situacoes(medido: dict) -> dict[str, str]:
    return {linha["item"]: linha["situacao"] for linha in avaliar_hardware(medido)}


def test_ordem_idrac_primeiro_e_bios_por_ultimo():
    """26/10/2026: iDRAC primeiro, depois placas de rede, demais componentes e BIOS por último."""
    itens = [{"id": "b", "categoria": "BIOS"}, {"id": "i", "categoria": "iDRAC with Lifecycle Controller"},
             {"id": "x", "categoria": "Desconhecida"}, {"id": "n", "categoria": "Network"}, {"id": "r", "categoria": "SAS RAID"}]  # fmt: skip
    assert [i["id"] for i in ordenar(itens)] == ["i", "n", "r", "x", "b"]


def test_estimativa_conta_reinicios():
    est = estimativa([{"categoria": "BIOS", "reinicio": True}, {"categoria": "Network", "reinicio": False}])
    assert (est["minutos"], est["reinicios"]) == (10 + 5 + aviso.MINUTOS_REINICIO, 1)
    assert [p["minutos"] for p in est["por_item"]] == [10, 5]


def test_estimativa_agrupa_reinicios_com_a_bios_e_idrac_sozinho():
    """RAID e BIOS no MESMO reinício; só o iDRAC (que exige reinício aqui) no seu (26/10/2026)."""
    itens = [{"categoria": "iDRAC with Lifecycle Controller", "reinicio": True}, {"categoria": "SAS RAID", "reinicio": True},
             {"categoria": "BIOS", "reinicio": True}]
    est = estimativa(itens)
    assert est["reinicios"] == 2
    assert [p["sozinho"] for p in est["por_item"]] == [True, False, False]
    assert estimativa([{"categoria": "Network", "reinicio": False}])["reinicios"] == 0


def test_hardware_saudavel_nao_bloqueia():
    assert set(situacoes(SAUDAVEL).values()) == {OK}


def test_bloqueios_do_hardware_na_hora():
    casos = {
        "fontes": r("FALHA", defeito=["PS1 Failure detected"]),
        "memoria": r("FALHA", edac=[{"nao_corrigiveis": 2, "corrigiveis": 0, "modulos": []}]),
        "raid": r("ALERTA", "VD0 degradado"),
        "sensores": r("FALHA", "Temp 95 °C crítico"),
        "ventoinhas": r("FALHA", "Fan3 parada"),
    }
    nomes = {"fontes": "Fontes", "memoria": "Memória", "raid": "RAID", "sensores": "Temperatura e sensores", "ventoinhas": "Ventoinhas"}
    for componente, resultado in casos.items():
        assert situacoes({**SAUDAVEL, componente: resultado})[nomes[componente]] == BLOQUEIA, componente


def test_memoria_ue_so_no_sel_e_historico_nao_bloqueia():
    """Pedido de 01/10/2026: UE só no SEL (pente A2 já retirado) não bloqueia; vira "evento antigo" com confirmação."""
    from msscan.historico import classificar_memoria

    evento = {"id": "25", "data": "01/03/98", "sensor": "Memory ECC Uncorr Err", "tipo": "Memory", "direcao": "Asserted",
              "evento": "Uncorrectable ECC (UnCorrectable ECC |  DIMMA2)"}  # fmt: skip
    [item] = classificar_memoria([evento], {"A1"}, {"*": 0})
    assert item["classe"] == "historico" and item["presente"] is False and "não está mais instalada" in item["nota"]
    medido = {**SAUDAVEL, "memoria": r("OK", eventos_sel=[item], problemas=[])}
    linhas = {x["item"]: x for x in avaliar_hardware(medido)}
    assert linhas["Memória"]["situacao"] == OK
    antigo = linhas["Registro de eventos (eventos anteriores)"]
    assert antigo["situacao"] == HISTORICO and "exporte e limpe o SEL" in antigo["texto"]
    aviso = montar_aviso("catalogo", [{"id": "item1", "categoria": "BIOS"}], list(linhas.values()), False)
    assert aviso["pode_seguir"] and aviso["historico"]
    assert "evento antigo" in conferir_aviso(aviso, "catalogo", ["item1"])
    aviso["ciente"] = True
    assert conferir_aviso(aviso, "catalogo", ["item1"]) == ""


def test_memoria_ue_atual_no_edac_bloqueia():
    edac = [{"nao_corrigiveis": 1, "corrigiveis": 0, "modulos": []}]
    assert situacoes({**SAUDAVEL, "memoria": r("FALHA", edac=edac)})["Memória"] == BLOQUEIA
    corrigivel = "log do BMC #13: DIMMA2 — Correctable ECC (Asserted)"
    assert situacoes({**SAUDAVEL, "memoria": r("ALERTA", problemas=[corrigivel])})["Memória"] == AVISO


def test_redundancia_perdida_destaca_sem_bloquear_e_incerta_bloqueia():
    assert situacoes({**SAUDAVEL, "fontes": r("ALERTA", redundancia_perdida=True)})["Fontes"] == REDUNDANCIA
    assert situacoes({**SAUDAVEL, "fontes": r("ALERTA", classificacao_incerta=True)})["Fontes"] == BLOQUEIA


def test_nao_verificado_nunca_vira_ok():
    """Sem verificar fontes, memória, sensores ou ventoinhas: BLOQUEIA; RAID não verificado só avisa (a controladora
    RAID já é recusada à parte)."""
    assert situacoes({}) == {"Fontes": BLOQUEIA, "Memória": BLOQUEIA, "RAID": AVISO, "Temperatura e sensores": BLOQUEIA,
                             "Ventoinhas": BLOQUEIA}  # fmt: skip
    assert situacoes({**SAUDAVEL, "raid": r("NAO_VERIFICADO")})["RAID"] == AVISO


def test_rollback_e_hash_no_item_do_catalogo():
    item = {"id": "1", "componente": "BIOS", "instalada": "2.21.2", "disponivel": "2.28.1",
            "pacote": {"categoria": "BIOS", "sha256": "ab" * 32, "data": "2026-08-01", "reinicio": True}}  # fmt: skip
    com = item_do_catalogo(item, "2.19.1", True)
    assert "2.19.1" in com["rollback"] and "SHA-256" in com["hash"] and com["reinicio"]
    assert "Reversão não verificada" in item_do_catalogo(item, None, False)["rollback"]
    assert "Sem versão anterior" in item_do_catalogo(item, None, True)["rollback"]


def test_conferencia_do_aviso():
    itens = [{"id": "1", "categoria": "Network"}]
    bom = montar_aviso("catalogo", itens, avaliar_hardware(SAUDAVEL), simular=False, agora=1000.0)
    assert bom["pode_seguir"] and not bom["redundancia"] and len(bom["avisos_fixos"]) == 5
    assert conferir_aviso(bom, "catalogo", ["1"], agora=1001.0) == ""
    assert bom["titulo"] == "Plano de atualização" and "texto_leitura" not in bom  # 18.2: sem a caixa "Li os avisos"
    assert "foram alterados" in conferir_aviso(bom, "catalogo", ["1", "2"], agora=1001.0)
    assert "foram alterados" in conferir_aviso(bom, "manual", ["1"], agora=1001.0)
    assert "10 minutos" in conferir_aviso(bom, "catalogo", ["1"], agora=1000.0 + VALIDADE_S + 1)
    assert "Abra o plano de atualização" in conferir_aviso(None, "catalogo", ["1"])
    ruim = montar_aviso("catalogo", itens, avaliar_hardware({**SAUDAVEL, "raid": r("FALHA", "VD0 degradado")}), False, agora=1000.0)
    assert not ruim["pode_seguir"] and "bloqueada" in conferir_aviso(ruim, "catalogo", ["1"], agora=1001.0)
    assert not montar_aviso("catalogo", [], avaliar_hardware(SAUDAVEL), False)["pode_seguir"]


def test_antes_depois_no_relatorio():
    from msscan.relatorio.atualizacoes import formatar_aplicacao, html_aplicacao

    secao = {"modo": "aplicar", "situacao": "aplicada", "mensagem": "ok", "itens": [], "passos": [],
             "antes_depois": [{"componente": "Rede X710", "antes": "22.5.0", "agora": "22.5.7", "texto": "aplicada <ok>"}]}  # fmt: skip
    assert "Rede X710: 22.5.0 -> 22.5.7 — aplicada" in formatar_aplicacao(secao)
    assert "Antes × depois da aplicação" in html_aplicacao(secao) and "&lt;ok&gt;" in html_aplicacao(secao)


def test_sessao_rele_o_inventario_so_do_que_foi_aplicado(monkeypatch):
    from msscan.interface import sessao as modulo_sessao
    from tests.conftest import ExecutorFalso

    monkeypatch.setattr(modulo_sessao, "coletar_inventario", lambda _ctx: ([{"component_id": "1", "nome": "NIC", "versao": "2"}], "Redfish"))
    sessao = modulo_sessao.Sessao(ExecutorFalso(), None, replay=False)
    aplicado = {"componente": "NIC", "component_id": "1", "instalada": "1", "disponivel": "2", "situacao": "aplicado"}
    agendado = {**aplicado, "componente": "BIOS", "component_id": "9", "situacao": "agendado"}
    resultado = {"modo": "aplicar", "itens": [aplicado, agendado]}
    sessao.antes_depois(None, resultado)
    assert [(i["componente"], i["antes"], i["agora"]) for i in resultado["antes_depois"]] == [("NIC", "1", "2")]
    simulado = {"modo": "simular", "itens": [aplicado]}
    sessao.antes_depois(None, simulado)
    assert "antes_depois" not in simulado  # simulação não inventa "depois"


def test_aviso_amarrado_ao_conteudo_dos_itens():
    """Revisão independente (MÉDIO 4): mesmo id com outro conteúdo (verificação refeita) não usa o aviso antigo."""
    item = {"id": "item1", "componente": "BIOS", "component_id": "159", "instalada": "2.21.2", "disponivel": "2.28.1",
            "pacote": {"categoria": "BIOS", "url": "https://dl.dell.com/a.EXE", "sha256": "ab" * 32}}  # fmt: skip
    bom = montar_aviso("catalogo", [item_do_catalogo(item, None, True)], avaliar_hardware(SAUDAVEL), False, agora=1000.0)
    assert conferir_aviso(bom, "catalogo", ["item1"], agora=1001.0, assinaturas=[aviso.assinatura_catalogo(item)]) == ""
    outro = {**item, "disponivel": "2.30.0", "pacote": {**item["pacote"], "sha256": "cd" * 32}}
    motivo = conferir_aviso(bom, "catalogo", ["item1"], agora=1001.0, assinaturas=[aviso.assinatura_catalogo(outro)])
    assert "foram alterados" in motivo


def test_instalacao_real_exige_pendrive_persistente():
    """Revisão independente (MÉDIO 8): dados em /tmp → só simulação."""
    import types

    from msscan.interface import sessao as modulo_sessao
    from tests.conftest import ExecutorFalso

    sessao = modulo_sessao.Sessao(ExecutorFalso(), None, replay=False)
    sessao.local = types.SimpleNamespace(persistente=False)
    assert "pendrive gravável" in sessao.motivo_sem_pendrive("aplicar") and sessao.motivo_sem_pendrive("simular") == ""
    sessao.local = types.SimpleNamespace(persistente=True)
    assert sessao.motivo_sem_pendrive("aplicar") == ""
