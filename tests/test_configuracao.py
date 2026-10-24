"""Configuração do servidor × regras de montagem do banco (pedido de 01/10/2026; R640 real com 2 CPUs e só o A1)."""

from __future__ import annotations

from msscan.compat.banco import carregar_banco, consultar
from msscan.configuracao import avaliar_configuracao

R640, _ = consultar(carregar_banco(), "Dell Inc.", "PowerEdge R640")


def cpu(n: int, habilitados: int | None = None) -> dict:
    habilitados = n if habilitados is None else habilitados
    return {"soquete": f"CPU{n}", "status": "Populated, Enabled" if n <= habilitados else "Populated, Disabled By BIOS",
            "populado": True, "habilitado": n <= habilitados}  # fmt: skip


def psu(n: int, sem_energia: bool = False) -> dict:
    return {"entidade": f"10.{n}", "estados": ["Presence detected"] + (["Power Supply AC lost"] if sem_energia else []),
            "categorias": ["sem_energia"] if sem_energia else []}  # fmt: skip


def relatorio(cpus: int, slots: list[str], fontes: list[dict], entrada: dict | None = R640, habilitados: int | None = None) -> dict:
    return {
        "equipamento": {"compatibilidade": {"entrada": entrada}},
        "medido": [
            {"componente": "cpu", "dados": {"processadores": [cpu(n, habilitados) for n in range(1, cpus + 1)]}},
            {"componente": "memoria", "dados": {"modulos": [{"slot": s, "instalado": True, "tamanho_mb": 8192} for s in slots]}},
            {"componente": "fontes", "dados": {"leituras": fontes}},
            {"componente": "discos", "dados": {"discos": [{}, {}]}},
        ],
    }


def test_r640_real_dois_processadores_so_a1():
    c = avaliar_configuracao(relatorio(2, ["A1"], [psu(1), psu(2)]))
    assert c["resumo"] == "Configuração: 2 processadores, 1 pente (8 GB), 2 fontes (redundante), 2 discos"
    [aviso] = c["avisos"]
    assert "CPU2 sem nenhum pente" in aviso and "B1" in aviso and "idêntica" in aviso
    assert c["situacao"] == "fora_do_recomendado" and "Installation and Service Manual" in c["fonte_regras"]
    assert c["por_processador"] == {"CPU1": {"slots": ["A1"], "canais": [0], "gb": 8}}


def test_dois_processadores_balanceados_sem_aviso():
    c = avaliar_configuracao(relatorio(2, ["A1", "B1"], [psu(1), psu(2)]))
    assert c["avisos"] == [] and c["situacao"] == "ok"


def test_single_e_uma_fonte_sao_validos():
    c = avaliar_configuracao(relatorio(1, ["A1"], [psu(1)]))
    assert c["avisos"] == []
    assert any("1 processador (single): válida" in i for i in c["informacoes"])
    assert any("Uma fonte instalada: funciona" in i for i in c["informacoes"])
    assert "1 fonte (sem redundância)" in c["resumo"]


def test_fonte_sem_energia_e_ordem_de_preenchimento():
    c = avaliar_configuracao(relatorio(2, ["A2", "B1"], [psu(1, sem_energia=True), psu(2)]))
    assert "2 fontes (1 com energia: sem redundância agora)" in c["resumo"]
    assert any("A1 (o primeiro a preencher" in a for a in c["avisos"])


def test_pente_em_slot_de_cpu_ausente_e_cpu_desabilitada():
    c = avaliar_configuracao(relatorio(2, ["A1", "B1"], [psu(1), psu(2)], habilitados=1))
    assert any("desabilitado: CPU2" in a for a in c["avisos"])


def test_modelo_sem_regras_so_resume():
    c = avaliar_configuracao(relatorio(2, ["A1"], [psu(1)], entrada=None))
    assert c["avisos"] == [] and "Sem regras de montagem" in c["informacoes"][0]
