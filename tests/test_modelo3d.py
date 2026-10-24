"""Modelo 3D do chassi (CLAUDE.md 18.1): peças nas posições do banco, presença lida, problema atual × histórico."""

from __future__ import annotations

from pathlib import Path

from msscan import modelo3d
from msscan.compat.banco import carregar_banco
from tests.conftest import R640, com_textos


def _relatorio_r640():
    from msscan.core.executor import ExecutorReplay
    from msscan.interface.sessao import Sessao

    sessao = Sessao(ExecutorReplay(R640), None, replay=True)
    sessao.iniciar()
    sessao.aguardar(60)
    sessao.iniciar_diagnostico()
    sessao.aguardar(120)
    return sessao.relatorio


def test_r640_real_do_replay():
    relatorio = _relatorio_r640()
    modelo = relatorio["modelo3d"]
    pecas = {p["id"]: p for p in modelo["pecas"]}
    # Ordem do manual (item 38 da placa): 6 soquetes de cada lado do processador; canais A1/A7 lado a lado no outro lado.
    lado_esquerdo = [p for p in modelo["pecas"] if p["area"] == "dimm_cpu1" and p["caixa"]["y"] < pecas["cpu1"]["caixa"]["y"]]
    assert [p["id"] for p in sorted(lado_esquerdo, key=lambda p: p["caixa"]["y"])] == [f"dimm_A{n}" for n in (6, 12, 5, 11, 4, 10)]
    assert len([p for p in modelo["pecas"] if p["id"].startswith("dimm_")]) == 24 and len([p for p in pecas if p.startswith("ventoinha")]) == 8
    # Presença: o R640 do laboratório tem A1 e A2; o resto aparece vazio. Duas CPUs, duas fontes.
    assert pecas["dimm_A1"]["estado"] == pecas["dimm_A2"]["estado"] == modelo3d.PRESENTE
    assert pecas["dimm_B1"]["estado"] == pecas["dimm_A3"]["estado"] == modelo3d.AUSENTE and pecas["dimm_B1"]["tipo"] == "soquete_vazio"
    assert pecas["cpu2"]["estado"] == pecas["psu2"]["estado"] == modelo3d.PRESENTE
    # A2: erro de memória só no SEL (1998) = HISTÓRICO (amarelo); PSU1 sem AC lido agora no sensor = ATUAL (vermelho).
    assert pecas["dimm_A2"]["problema"] == modelo3d.HISTORICO and any("A2" in t for t in pecas["dimm_A2"]["textos"])
    assert pecas["psu1"]["problema"] == modelo3d.ATUAL and pecas["psu2"]["problema"] is None
    assert pecas["cabos_sas"]["problema"] == modelo3d.HISTORICO  # "Config Error" do cabo veio do log
    # Baias e slots PCIe: sem leitura da configuração, nunca "presente" inventado.
    assert {pecas[f"baia{i}"]["estado"] for i in range(10)} == {modelo3d.DESCONHECIDO}
    # Tudo dentro do chassi.
    dim = modelo["dimensoes"]
    for p in modelo["pecas"]:
        c = p["caixa"]
        assert 0 <= c["x"] and c["x"] + c["w"] <= dim["profundidade"] + 1 and 0 <= c["y"] and c["y"] + c["d"] <= dim["largura"] + 1, p["id"]
        assert c["z"] + c["h"] <= dim["altura"], p["id"]


def test_sem_bloco_no_banco_nao_ha_modelo():
    assert modelo3d.montar({"equipamento": {"compatibilidade": {"entrada": {"nome": "outro", "mapa": {"areas": []}}}}}) is None
    r410 = next(m for f in carregar_banco(Path(__file__).parent.parent / "compat_db") for m in f["modelos"] if "R410" in m["nome"])
    assert modelo3d.montar({"equipamento": {"compatibilidade": {"entrada": r410}}}) is None


def test_r640_cor_de_cada_peca_e_estado_geral():
    """Pedido de 02/10/2026: cor por estado (verde/amarelo/vermelho/cinza) e indicador geral; nunca OK com pendência."""
    relatorio = _relatorio_r640()
    pecas = {p["id"]: p for p in relatorio["modelo3d"]["pecas"]}
    # PSU1 sem AC = alerta ATUAL (amarelo, pulsando); A2 só no histórico = amarelo; o resto do lugar certo.
    assert pecas["psu1"]["cor"] == pecas["dimm_A2"]["cor"] == modelo3d.ATENCAO and pecas["psu1"]["problema"] == modelo3d.ATUAL
    assert pecas["psu2"]["cor"] == pecas["dimm_A1"]["cor"] == pecas["cpu1"]["cor"] == pecas["ventoinha1"]["cor"] == modelo3d.OK
    assert pecas["dimm_B1"]["cor"] == modelo3d.AUSENTE
    assert pecas["baia0"]["cor"] == pecas["controladora"]["cor"] == modelo3d.NAO_VERIFICADA  # presença não lida: nunca verde
    assert "Memória" in pecas["dimm_A1"]["situacao"]
    assert relatorio["modelo3d"]["areas"]["psu1"] == modelo3d.ATENCAO and relatorio["modelo3d"]["areas"]["discos"] == modelo3d.NAO_VERIFICADA
    geral = relatorio["estado_geral"]
    assert geral["nivel"] == modelo3d.ATENCAO and geral["alertas"] == 4 and geral["historicos"] > 0 and geral["alvo"] == "fontes"


def _rel(*status, **extra):
    nomes = ["fontes", "memoria", "log_bmc", "discos"]
    return {"medido": [{"componente": n, "titulo": n.title(), "status": s} for n, s in zip(nomes, status)], **extra}


def test_estado_geral_regras():
    assert modelo3d.estado_geral(_rel("OK", "OK", "OK", "NAO_APLICAVEL"))["nivel"] == modelo3d.OK
    # Um não verificado basta para não ser "Tudo OK".
    geral = modelo3d.estado_geral(_rel("OK", "OK", "OK", "NAO_VERIFICADO"))
    assert geral["nivel"] == modelo3d.ATENCAO and geral["nao_verificados"] == 1 and geral["alvo"] == "discos"
    # Qualquer falha = Problema, com a contagem e o atalho para ela.
    geral = modelo3d.estado_geral(_rel("ALERTA", "FALHA", "OK", "NAO_VERIFICADO"))
    assert geral["nivel"] == modelo3d.PROBLEMA and geral["falhas"] == 1 and geral["alvo"] == "memoria" and "1 falha atual" in geral["texto"]
    # Evento só no histórico e aviso de configuração também pedem atenção.
    sel = [{"componente": "log_bmc", "dados": {"eventos_sel": [{"classe": "historico", "texto": "DIMM A2 erro"}]}}]
    assert modelo3d.estado_geral({"medido": _rel("OK", "OK", "OK", "OK")["medido"][:3] + [dict(sel[0], status="OK")]})["nivel"] == modelo3d.ATENCAO
    assert modelo3d.estado_geral(_rel("OK", "OK", "OK", "OK", configuracao={"avisos": ["x"]}))["nivel"] == modelo3d.ATENCAO
    assert modelo3d.estado_geral({})["nivel"] == modelo3d.ATENCAO  # nada verificado nunca é OK


def test_problema_sem_peca_conhecida_pinta_o_componente_todo():
    """Componente com falha que não caiu em nenhuma peça: todas as peças dele ficam vermelhas (nunca verdes)."""
    relatorio = _relatorio_r640()
    for m in relatorio["medido"]:
        if m["componente"] == "ventoinhas":
            m["status"] = "FALHA"
    relatorio["localizacao"]["itens"].append({"componente": "ventoinhas", "status": "FALHA", "localizado": False, "problema": "x"})
    pecas = {p["id"]: p for p in modelo3d.montar(relatorio)["pecas"]}
    assert {pecas[f"ventoinha{i}"]["cor"] for i in range(1, 9)} == {modelo3d.PROBLEMA}


# --- Modelo genérico por família (CLAUDE.md 19.4) ---------------------------------------------------------------------


def test_familias_dell_cobertas():
    from msscan.compat.genericos import familia

    assert familia("PowerEdge R650") == {"formato": "1U", "geracao": "15ª geração (15G)", "familia": "R6x5"}
    assert familia("PowerEdge R740xd")["formato"] == "2U" and familia("PowerEdge R760")["geracao"].startswith("16ª")
    assert familia("PowerEdge T640")["formato"] == "torre" and familia("PowerEdge R620")["geracao"].startswith("12ª")
    assert familia("PowerEdge R430")["formato"] == "1U" and familia("PowerEdge R540")["formato"] == "2U"
    # 11G, um processador, AMD de 4 dígitos e outros fabricantes: sem modelo genérico (nada é inventado)
    for fora in ("PowerEdge R410", "PowerEdge R250", "PowerEdge R6515", "PowerEdge R640 XL", "ProLiant DL360 Gen10", None):
        assert familia(fora) is None, fora


def test_banco_so_gera_generico_sem_posicoes_conferidas():
    from msscan.compat.banco import detectar_compatibilidade
    from msscan.deteccao.contexto import Contexto
    from tests.conftest import ExecutorFalso

    def compat(fabricante, modelo):
        ctx = Contexto(executor=ExecutorFalso(), fabricante=fabricante, modelo=modelo)
        detectar_compatibilidade(ctx)
        return ctx.compat

    assert compat("Dell Inc.", "PowerEdge R640")["generico"] is None  # posições conferidas no manual: exato
    r650 = compat("Dell Inc.", "PowerEdge R650")
    assert not r650["encontrado"] and r650["entrada"] is None and r650["generico"]["aproximado"] is True
    assert "Posições aproximadas" in r650["generico"]["aviso"] and "rack 1U" in r650["generico"]["nome"]
    assert compat("HPE", "PowerEdge R650")["generico"] is None


def test_generico_montado_com_os_soquetes_do_smbios():
    from msscan.compat.genericos import gerar

    soquetes = [f"A{i}" for i in range(1, 17)] + [f"B{i}" for i in range(1, 17)]
    relatorio = {"equipamento": {"fabricante": "Dell Inc.", "compatibilidade": {"entrada": None, "generico": gerar("Dell Inc.", "PowerEdge R650")}},
                 "medido": [{"componente": "memoria", "status": "OK", "dados": {"soquetes": soquetes, "modulos": [
                     {"slot": "A1", "instalado": True, "tamanho_mb": 16384}]}},
                     {"componente": "cpu", "status": "OK", "dados": {"processadores": [{"soquete": "CPU1", "populado": True}]}}]}  # fmt: skip
    from msscan.modelo3d import montar

    modelo = montar(relatorio)
    assert modelo["aproximado"] and "Posições aproximadas" in modelo["aviso"] and modelo["dimensoes"]["altura"] == 42.8
    dimms = [p for p in modelo["pecas"] if p["tipo"] in ("dimm", "soquete_vazio")]
    assert len(dimms) == 32 and all("posição aproximada" in p["nome"] for p in dimms)
    assert next(p for p in dimms if p["id"] == "dimm_A1")["estado"] == "presente"
    # Quantidade de baias e de ventoinhas não é afirmada: uma peça só para cada área.
    assert [p["id"] for p in modelo["pecas"] if p["tipo"] == "baia"] == ["baias"]
    assert [p["id"] for p in modelo["pecas"] if p["tipo"] == "ventoinha"] == ["ventoinhas"]
    from msscan.localizacao import localizar_relatorio

    loc = localizar_relatorio(relatorio)
    assert loc["aproximado"] and loc["mapa"]["aproximado"] and "aproximadas" in loc["mapa"]["vista"]


def test_interface_alterna_3d_e_2d_lembrando_a_escolha():
    from msscan.interface.servidor import PASTA_ESTATICOS

    html = com_textos((PASTA_ESTATICOS / "index.html").read_text(encoding="utf-8"))
    js = com_textos((PASTA_ESTATICOS / "app.js").read_text(encoding="utf-8"))
    for prefixo in ("diag", "diag-geral"):
        assert f'id="{prefixo}-vista"' in html and f'id="{prefixo}-aproximado"' in html
    assert 'localStorage.setItem("msscan-vista"' in js and '"Ver modelo 3D"' in js and "dados.aproximado" in js
