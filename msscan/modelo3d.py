"""Modelo 3D simplificado do chassi (CLAUDE.md 18.1), gerado a partir do banco de compatibilidade e do relatório.

Função pura: relatório (com ``localizacao``, ``configuracao`` e ``medido``) → lista de peças em milímetros, cada uma com
posição, estado (presente / ausente / desconhecido) e o problema dela (``atual`` = vermelho pulsando; ``historico`` =
amarelo), mais o diagnóstico para mostrar ao clicar. O desenho (three.js) fica no navegador; aqui só os dados.

As posições saem do bloco ``modelo3d`` do modelo (começando pelo R640): as áreas do ``mapa`` (vista de cima, conferida na
foto oficial) viram caixas; slots de memória, ventoinhas, baias e slots PCIe são subdivididos pela ordem do manual.
**Nunca adivinha**: peça sem informação de presença fica "desconhecido" (contorno), nunca "presente".
Eixos: ``x`` = profundidade a partir da frente, ``y`` = largura a partir da lateral esquerda (vista da frente), ``z`` = altura.
"""

from __future__ import annotations

import re
from typing import Any

PRESENTE, AUSENTE, DESCONHECIDO = "presente", "ausente", "desconhecido"
ATUAL, HISTORICO = "atual", "historico"
# Cor de cada peça e do indicador geral (pedido de 02/10/2026): verde, amarelo, vermelho, cinza; ausente = vazio.
OK, ATENCAO, PROBLEMA, NAO_VERIFICADA = "ok", "atencao", "problema", "nao_verificado"
GRAVIDADE = {AUSENTE: 0, OK: 1, NAO_VERIFICADA: 2, ATENCAO: 3, PROBLEMA: 4}
COMPONENTE_DA_PECA = {"dimm": "memoria", "soquete_vazio": "memoria", "cpu": "cpu", "fonte": "fontes", "ventoinha": "ventoinhas",
                      "baia": "discos", "controladora": "raid", "backplane": "internos", "cabo": "internos", "riser": "internos",
                      "slot_pcie": "internos"}  # fmt: skip
"""Componente do diagnóstico que responde por cada tipo de peça (status dele = cor da peça sem problema localizado)."""
APROXIMADA = "posição aproximada"
ALTURAS = {"baia": (3, 36), "backplane": (2, 38), "ventoinha": (3, 38), "cpu": (3, 30), "dimm": (6, 34), "soquete_vazio": (3, 6),
           "fonte": (3, 38), "controladora": (6, 14), "cabo": (3, 6), "riser": (3, 36), "slot_pcie": (14, 34)}  # fmt: skip
"""Faixa de altura (mm, de baixo para cima) por tipo de peça: só para o desenho ficar legível."""


def _entrada(relatorio: dict[str, Any]) -> dict[str, Any]:
    """Entrada do banco com ``modelo3d``; senão o modelo genérico da família (19.4, posições aproximadas)."""
    compat = (relatorio.get("equipamento") or {}).get("compatibilidade") or {}
    entrada = compat.get("entrada") or {}
    if not isinstance(entrada.get("modelo3d"), dict) and isinstance(compat.get("generico"), dict):
        return compat["generico"]
    return entrada


def _ordem_detectada(relatorio: dict[str, Any]) -> dict[str, list[str]]:
    """Soquetes de memória lidos do SMBIOS (modelo genérico): banco A = CPU1, B = CPU2, metade de cada lado do processador."""
    memoria = _medido(relatorio, "memoria")
    nomes = [str(s).upper().replace("DIMM", "").strip("_ ") for s in memoria.get("soquetes") or []]
    nomes = nomes or [str(m.get("slot")).upper() for m in memoria.get("modulos") or [] if isinstance(m, dict)]
    ordem = {}
    for cpu, banco in (("cpu1", "A"), ("cpu2", "B")):
        slots = sorted({n for n in nomes if re.fullmatch(rf"{banco}\d{{1,2}}", n)}, key=lambda n: int(n[1:]))
        meio = (len(slots) + 1) // 2
        ordem[cpu] = slots[:meio] + ["CPU"] + slots[meio:]
    return ordem


class _Escala:
    """Mapa (unidades do desenho 2D) → milímetros do chassi."""

    def __init__(self, bloco: dict[str, Any]) -> None:
        dim, chassi = bloco["dimensoes_mm"], bloco["chassi_mapa"]
        self.px, self.py = float(dim["profundidade"]) / chassi["w"], float(dim["largura"]) / chassi["h"]
        self.x0, self.y0 = chassi["x"], chassi["y"]

    def caixa(self, x: float, y: float, w: float, h: float, tipo: str) -> dict[str, float]:
        z0, z1 = ALTURAS[tipo]
        return {"x": round((x - self.x0) * self.px, 1), "y": round((y - self.y0) * self.py, 1), "z": z0,
                "w": round(w * self.px, 1), "d": round(h * self.py, 1), "h": z1 - z0}  # fmt: skip


def _faixas(inicio: float, fim: float, n: int) -> list[tuple[float, float]]:
    if n <= 0:
        return []
    passo = (fim - inicio) / n
    return [(inicio + i * passo, passo) for i in range(n)]


def _peca(ident: str, nome: str, tipo: str, caixa: dict[str, float], estado: str, area: str) -> dict[str, Any]:
    return {"id": ident, "nome": nome, "tipo": tipo, "caixa": caixa, "estado": estado, "area": area, "problema": None, "textos": []}


def _medido(relatorio: dict[str, Any], componente: str) -> dict[str, Any]:
    return next((m.get("dados") or {} for m in relatorio.get("medido") or [] if m.get("componente") == componente), {})


def montar(relatorio: dict[str, Any] | None) -> dict[str, Any] | None:
    """Modelo do equipamento do relatório, ou ``None`` se o modelo não tiver ``modelo3d`` no banco (mapa 2D/texto)."""
    relatorio = relatorio or {}
    entrada = _entrada(relatorio)
    bloco, mapa = entrada.get("modelo3d"), entrada.get("mapa")
    if not isinstance(bloco, dict) or not isinstance(mapa, dict):
        return None
    areas = {a["id"]: a for a in mapa.get("areas") or [] if isinstance(a, dict) and a.get("id")}
    esc = _Escala(bloco)
    pecas: list[dict[str, Any]] = []

    def area(ident: str) -> dict[str, Any]:
        return areas[ident]

    # Baias de disco (frente) e backplane.
    a = area("discos")
    for i, (y, larg) in enumerate(_faixas(a["y"], a["y"] + a["h"], int(bloco.get("baias") or 0))):
        pecas.append(_peca(f"baia{i}", f"Baia de disco {i} (configuração de baias do servidor não lida)", "baia",
                           esc.caixa(a["x"], y + 1, a["w"], larg - 2, "baia"), DESCONHECIDO, "discos"))  # fmt: skip
    if not bloco.get("baias"):  # genérico: quantidade de baias não afirmada
        pecas.append(_peca("baias", a["nome"], "baia", esc.caixa(a["x"], a["y"] + 1, a["w"], a["h"] - 2, "baia"), DESCONHECIDO, "discos"))
    b = area("backplane")
    pecas.append(_peca("backplane", b["nome"], "backplane", esc.caixa(b["x"], b["y"], b["w"] * 0.4, b["h"], "backplane"), PRESENTE, "backplane"))
    # Ventoinhas (fileira central).
    v = area("ventoinhas")
    for i, (y, larg) in enumerate(_faixas(v["y"], v["y"] + v["h"], int(bloco.get("ventoinhas") or 0))):
        pecas.append(_peca(f"ventoinha{i + 1}", f"Ventoinha (posição {i + 1} da esquerda para a direita, vista da frente)", "ventoinha",
                           esc.caixa(v["x"], y + 2, v["w"], larg - 4, "ventoinha"), PRESENTE, "ventoinhas"))  # fmt: skip
    if not bloco.get("ventoinhas"):  # genérico: uma fileira, sem afirmar quantas
        pecas.append(_peca("ventoinhas", v["nome"], "ventoinha", esc.caixa(v["x"], v["y"] + 2, v["w"], v["h"] - 4, "ventoinha"), PRESENTE, "ventoinhas"))
    # Processadores e soquetes de memória (ordem do manual).
    processadores = {p.get("soquete"): p for p in _medido(relatorio, "cpu").get("processadores") or [] if isinstance(p, dict)}
    modulos = {str(m.get("slot")).upper(): m for m in _medido(relatorio, "memoria").get("modulos") or [] if isinstance(m, dict)}
    for cpu, chave_dimm in (("cpu1", "cpu1"), ("cpu2", "cpu2")):
        c = area(cpu)
        info = processadores.get(cpu.upper())
        estado = DESCONHECIDO if not processadores else PRESENTE if info and info.get("populado") else AUSENTE
        nome = f"{cpu.upper()}" + (f" — {info.get('modelo')}" if info and info.get("modelo") else "")
        pecas.append(_peca(cpu, nome, "cpu", esc.caixa(c["x"], c["y"], c["w"], c["h"], "cpu"), estado, cpu))
        ordens = _ordem_detectada(relatorio) if bloco.get("dimm_ordem") == "detectado" else bloco.get("dimm_ordem") or {}
        ordem = list(ordens.get(chave_dimm) or [])
        d = area(f"dimm_{chave_dimm}")
        if "CPU" not in ordem:
            continue
        meio = ordem.index("CPU")
        for slots, (inicio, fim) in ((ordem[:meio], (d["y"], c["y"])), (ordem[meio + 1:], (c["y"] + c["h"], d["y"] + d["h"]))):
            for slot, (y, larg) in zip(slots, _faixas(inicio, fim, len(slots))):
                modulo = modulos.get(str(slot).upper())
                instalado = bool(modulo and modulo.get("instalado"))
                estado = DESCONHECIDO if not modulos else PRESENTE if instalado else AUSENTE
                canal = ("aba branca" if int(str(slot)[1:]) <= 6 else "aba preta") if not bloco.get("aproximado") else APROXIMADA
                nome = f"DIMM {slot} ({cpu.upper()}, {canal})" + (f" — {modulo.get('tamanho_mb', 0) // 1024} GB {modulo.get('tipo') or ''}".rstrip()
                                                                       if instalado else " — vazio" if estado == AUSENTE else "")  # fmt: skip
                pecas.append(_peca(f"dimm_{slot}", nome, "dimm" if estado != AUSENTE else "soquete_vazio",
                                   esc.caixa(d["x"], y + larg * 0.3, d["w"], larg * 0.4, "dimm" if estado != AUSENTE else "soquete_vazio"),
                                   estado, f"dimm_{chave_dimm}"))  # fmt: skip
    # Fontes, controladora, cabos SAS, risers e slots PCIe.
    fontes = (relatorio.get("configuracao") or {}).get("fontes") or {}
    for psu in ("psu1", "psu2"):
        f = area(psu)
        numero = int(psu[-1])
        estado = DESCONHECIDO if not fontes else PRESENTE if int(fontes.get("instaladas") or 0) >= numero else AUSENTE
        pecas.append(_peca(psu, f["nome"], "fonte", esc.caixa(f["x"], f["y"] + 3, f["w"], f["h"] - 6, "fonte"), estado, psu))
    c = area("controladora")
    pecas.append(_peca("controladora", c["nome"], "controladora", esc.caixa(c["x"], c["y"], c["w"], c["h"], "controladora"), DESCONHECIDO, "controladora"))
    s = area("cabos_sas")
    pecas.append(_peca("cabos_sas", s["nome"], "cabo", esc.caixa(s["x"], s["y"] + s["h"] * 0.4, s["w"], s["h"] * 0.2, "cabo"), DESCONHECIDO, "cabos_sas"))
    for riser, slots_pcie in (bloco.get("risers") or {}).items():
        r = area(riser)
        pecas.append(_peca(riser, r["nome"], "riser", esc.caixa(r["x"], r["y"], r["w"] * 0.15, r["h"], "riser"), DESCONHECIDO, riser))
        for slot, (y, larg) in zip(slots_pcie, _faixas(r["y"], r["y"] + r["h"], len(slots_pcie))):
            pecas.append(_peca(f"slot_pcie{slot}", f"Slot PCIe {slot} ({r['curto']}; placa instalada não lida)", "slot_pcie",
                               esc.caixa(r["x"] + r["w"] * 0.2, y + 4, r["w"] * 0.75, larg - 8, "slot_pcie"), DESCONHECIDO, riser))  # fmt: skip
    explicados = _marcar_problemas(relatorio, pecas)
    _colorir(relatorio, pecas, explicados)
    cores_area: dict[str, str] = {}
    for peca in pecas:
        atual = cores_area.get(peca["area"])
        if atual is None or GRAVIDADE[peca["cor"]] > GRAVIDADE[atual]:
            cores_area[peca["area"]] = peca["cor"]
    return {"modelo": entrada.get("nome"), "fonte": bloco.get("fonte"), "dimensoes": bloco["dimensoes_mm"], "pecas": pecas,
            "aproximado": bool(bloco.get("aproximado")), "aviso": bloco.get("aviso") or "",
            "areas": cores_area,
            "legenda": {OK: "OK (verde)", ATENCAO: "atenção: alerta ou só no histórico do log do BMC (amarelo)",
                        PROBLEMA: "problema (vermelho; pulsando = ATUAL)", NAO_VERIFICADA: "não verificado (cinza)",
                        AUSENTE: "peça ausente (vazio)"}}  # fmt: skip


def _historicos(relatorio: dict[str, Any]) -> list[str]:
    """Textos dos eventos que o diagnóstico classificou como HISTÓRICO (CLAUDE.md 17.1)."""
    textos = []
    for medido in relatorio.get("medido") or []:
        for evento in (medido.get("dados") or {}).get("eventos_sel") or []:
            if isinstance(evento, dict) and evento.get("classe") == "historico" and evento.get("texto"):
                textos.append(str(evento["texto"]))
    return textos


def _marcar_problemas(relatorio: dict[str, Any], pecas: list[dict[str, Any]]) -> set[str]:
    """Cada problema localizado vai para a peça exata (DIMM, fonte, CPU) ou para todas as peças da área.

    Devolve os componentes cujos problemas caíram TODOS em alguma peça (os demais têm problema sem peça conhecida).
    """
    por_id = {p["id"]: p for p in pecas}
    historicos = _historicos(relatorio)
    com_item, sem_peca = set(), set()
    for item in (relatorio.get("localizacao") or {}).get("itens") or []:
        componente = str(item.get("componente"))
        com_item.add(componente)
        if not item.get("localizado"):
            sem_peca.add(componente)
            continue
        texto = str(item.get("problema") or "")
        # Todo evento do log do BMC (SEL) é HISTÓRICO até uma leitura ao vivo confirmar (17.1); leitura de sensor = ATUAL.
        do_log = texto.lower().startswith("log do bmc")
        classe = HISTORICO if do_log or any(texto[:120] in h or h[:120] in texto for h in historicos) else ATUAL
        rotulo = str(item.get("local") or "")
        alvos: list[Any] = []
        for padrao, molde in ((r"DIMM ([AB]\d{1,2})", "dimm_{}"), (r"\(PSU(\d)\)", "psu{}"), (r"\b(CPU\d)\b", "{}"),
                              (r"Slot PCIe (\d)", "slot_pcie{}")):  # fmt: skip
            casado = re.search(padrao, rotulo)
            if casado:
                alvos = [por_id.get(molde.format(casado.group(1)).lower() if molde == "{}" else molde.format(casado.group(1)))]
                break
        if not any(alvos) and item.get("area"):
            alvos = [p for p in pecas if p["area"] == item["area"]]
        alvos = [a for a in alvos if a]
        if not alvos:
            sem_peca.add(componente)
        # Vermelho só para falha atual; alerta atual (ex.: fonte sem energia) e histórico = amarelo.
        cor = PROBLEMA if classe == ATUAL and item.get("status") == "FALHA" else ATENCAO
        for peca in alvos:
            peca["textos"].append(f"[{'ATUAL' if classe == ATUAL else 'HISTÓRICO'}] {item.get('titulo') or item.get('componente')}: {texto}")
            if peca["problema"] != ATUAL:
                peca["problema"] = classe
            if GRAVIDADE[cor] > GRAVIDADE.get(peca.get("cor_problema") or AUSENTE, 0):
                peca["cor_problema"] = cor
    return com_item - sem_peca


def _colorir(relatorio: dict[str, Any], pecas: list[dict[str, Any]], explicados: set[str]) -> None:
    """Cor final de cada peça: problema localizado nela; senão ausente (vazio), presença não lida (cinza) ou o status
    do componente dela. Componente com problema que não caiu em peça nenhuma pinta todas as suas peças (nunca verde)."""
    status = {str(m.get("componente")): m.get("status") for m in relatorio.get("medido") or []}
    titulos = {str(m.get("componente")): m.get("titulo") or m.get("componente") for m in relatorio.get("medido") or []}
    rotulos = {"OK": "OK", "ALERTA": "alerta", "FALHA": "falha", "NAO_VERIFICADO": "não verificado", "NAO_APLICAVEL": "N/A"}
    for peca in pecas:
        componente = COMPONENTE_DA_PECA.get(peca["tipo"], "")
        st = status.get(componente)
        cor = peca.pop("cor_problema", None)
        if cor is None:
            if peca["estado"] == AUSENTE:
                cor = AUSENTE
            elif peca["estado"] == DESCONHECIDO or st not in ("OK", "ALERTA", "FALHA"):
                cor = NAO_VERIFICADA
            elif st == "OK" or componente in explicados:
                cor = OK
            else:
                cor = PROBLEMA if st == "FALHA" else ATENCAO
        peca["cor"] = cor
        peca["situacao"] = f"{titulos.get(componente, componente)}: {rotulos.get(st or '', 'não verificado')}" + (
            " (presença desta peça não verificada)" if peca["estado"] == DESCONHECIDO else "")


def estado_geral(relatorio: dict[str, Any] | None) -> dict[str, Any]:
    """Indicador do estado geral (pedido de 02/10/2026): ``ok`` / ``atencao`` / ``problema`` com as contagens.

    ``ok`` só com TODOS os componentes verificados (N/A conta como verificado), sem alerta, sem evento histórico e
    sem aviso de configuração. Qualquer FALHA = ``problema``. ``alvo`` = componente para o atalho da lista.
    """
    relatorio = relatorio or {}
    medidos = [m for m in relatorio.get("medido") or [] if isinstance(m, dict)]
    def de(st: str) -> list[dict[str, Any]]:
        return [m for m in medidos if m.get("status") == st]
    falhas, alertas, nao_verificados = de("FALHA"), de("ALERTA"), de("NAO_VERIFICADO")
    historicos = len(_historicos(relatorio))
    avisos_config = len(((relatorio.get("configuracao") or {}).get("avisos")) or [])
    partes = []
    for n, um, varios in ((len(falhas), "1 falha atual", "{} falhas atuais"), (len(alertas), "1 alerta", "{} alertas"),
                          (len(nao_verificados), "1 componente não verificado", "{} componentes não verificados"),
                          (historicos, "1 evento histórico no log do BMC", "{} eventos históricos no log do BMC"),
                          (avisos_config, "1 aviso de configuração", "{} avisos de configuração")):  # fmt: skip
        if n:
            partes.append(um if n == 1 else varios.format(n))
    if falhas:
        nivel, titulo, alvo = PROBLEMA, "Problema", falhas[0]
    elif not medidos or alertas or nao_verificados or historicos or avisos_config:
        nivel, titulo = ATENCAO, "Atenção"
        alvo = (alertas or nao_verificados or [m for m in medidos if m.get("componente") == "log_bmc"] or [None])[0]
    else:
        nivel, titulo, alvo = OK, "Tudo OK", None
    if not medidos:
        texto = "Nenhum componente verificado."
    elif nivel == OK:
        texto = f"Todos os {len(medidos)} componentes verificados, sem problemas."
    else:
        texto = ", ".join(partes) + "."
    return {"nivel": nivel, "titulo": titulo, "texto": texto, "falhas": len(falhas), "alertas": len(alertas),
            "nao_verificados": len(nao_verificados), "historicos": historicos, "avisos_configuracao": avisos_config,
            "alvo": (alvo or {}).get("componente"), "alvo_titulo": (alvo or {}).get("titulo")}  # fmt: skip


__all__ = ["ATENCAO", "ATUAL", "AUSENTE", "DESCONHECIDO", "HISTORICO", "NAO_VERIFICADA", "OK", "PRESENTE", "PROBLEMA",
           "estado_geral", "montar"]
