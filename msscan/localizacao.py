"""Localização física dos problemas (CLAUDE.md 15.3, pedido de 30/09/2026).

Função pura: nomes que o próprio equipamento informa (sensor do IPMI, entidade IPMI, evento do SEL, rótulo
``Location.PartLocation`` do Redfish, slot do DIMM) → área física ("Fonte 1 (PSU1)", "DIMM A2 (CPU1)", "Cabo SAS A0").
O mapa do chassi (vista de cima) vem do banco de compatibilidade (``mapa`` do modelo); modelo sem mapa → só o texto.
**Nunca adivinha:** nome que não casa com nenhuma regra → "não foi possível localizar".

Regras escritas contra saídas reais (R640: ``PS1``/entidade ``10.1``, ``Fan1A``, ``DIMMA2``, ``Cable SAS A0``,
``Physical Security Intrusion``) e contra o rótulo do Redfish do mockup da DMTF ("NIC Daughter Card").
"""

from __future__ import annotations

import re
from typing import Any

NAO_LOCALIZADO = "não foi possível localizar a peça com os dados disponíveis"
STATUS_PROBLEMA = ("ALERTA", "FALHA")
MAX_ITENS = 60

# DIMM: a letra do slot indica o processador (Dell 14G: A = CPU1, B = CPU2; manual do R640). Outros modelos podem
# usar outra convenção: sem mapa do modelo, a CPU não é afirmada (só "DIMM A2").
BANCOS_DELL = {"A": 1, "B": 2}


def _regra_psu(numero: str) -> tuple[str, str]:
    return f"psu{int(numero)}", f"Fonte {int(numero)} (PSU{int(numero)})"


def local_de(texto: str, entidade: str | None = None, dell: bool = False) -> tuple[str | None, str] | None:
    """``(id da área, rótulo)`` para um nome/texto informado pelo equipamento; ``None`` se nada casar.

    ``id`` pode ser ``None`` quando o rótulo é conhecido mas não há área no mapa (ex.: "Placa de rede").
    """
    t = texto or ""
    if entidade:
        casado = re.fullmatch(r"10\.(\d+)", entidade.strip())  # entidade IPMI 10 = fonte de alimentação, instância N
        if casado:
            return _regra_psu(casado.group(1))
    regras: list[tuple[str, Any]] = [
        (r"\bPSU?\s*\.?\s*(?:Slot\s*\.?\s*)?(\d)\b|\bPower Supply\s+(\d)\b|\bFonte\s+(\d)\b|\(10\.(\d+)\)",
         lambda m: _regra_psu(next(g for g in m.groups() if g))),
        (r"\bDIMM[\s_.]*(?:Socket[\s_.]*)?([A-Z])[\s_]*(\d{1,2})\b|\bslot ([A-Z])(\d{1,2})\b",
         lambda m: _dimm(*(g for g in m.groups() if g), dell=dell)),
        (r"\bCable\s+SAS\s+([A-Z]\d)\b|\bBP\s+SAS\s+([A-Z]\d)\b",
         lambda m: ("cabos_sas", f"Cabo SAS {next(g for g in m.groups() if g)} (controladora → backplane)")),
        (r"\bCable\b|\bCabo\b", lambda m: ("cabos_sas", "Cabo interno (sem identificação no evento)")),
        (r"\bFan\s*(\d+)\s*([AB])?\b", lambda m: ("ventoinhas", f"Ventoinha {m.group(1)}{m.group(2) or ''} (Fan{m.group(1)}{m.group(2) or ''})")),
        (r"\bRiser\s*(\d)", lambda m: (f"riser{m.group(1)}", f"Riser {m.group(1)}")),
        (r"\bPCIe?\s*Slot\s*(\d+)|\bSlot\s+(\d+)\b",
         lambda m: ("risers", f"Slot PCIe {next(g for g in m.groups() if g)} (o riser exato depende da configuração)")),
        (r"\bCPU\s*(\d)\b|\bProc(?:essor)?\s*(\d)\b|\bSocket\s*(\d)\b",
         lambda m: (f"cpu{next(g for g in m.groups() if g)}", f"Processador CPU{next(g for g in m.groups() if g)}")),
        (r"Backplane|\bBP\d?\b|Drive Slot|Disk Drive Bay|\bBay\s*\d", lambda m: ("backplane", "Backplane / compartimento de discos")),
        # ROMB = "RAID on motherboard": a bateria da PERC integrada (SEL do R620: "Battery ROMB Battery Failed/Low").
        (r"\bROMB\b", lambda m: ("controladora", "Bateria da controladora RAID integrada (PERC)")),
        (r"\bPERC\b|Storage Controller|Integrated RAID|\bRAID\b|\bHBA\d*\b|\bBOSS\b", lambda m: ("controladora", "Controladora de armazenamento")),
        (r"Inlet Temp", lambda m: ("frente", "Sensor de temperatura de entrada de ar (painel frontal)")),
        (r"Exhaust Temp", lambda m: ("traseira", "Sensor de temperatura de saída de ar (traseira)")),
        (r"Intrusion|Intrus", lambda m: ("chassi", "Tampa do chassi (sensor de intrusão)")),
        (r"\bNIC\b|rNDC|Daughter Card|Network", lambda m: (None, "Placa de rede")),
    ]
    for padrao, montar in regras:
        casado = re.search(padrao, t, re.IGNORECASE)
        if casado:
            return montar(casado)
    return None


def _dimm(banco: str, numero: str, dell: bool) -> tuple[str | None, str]:
    banco = banco.upper()
    cpu = BANCOS_DELL.get(banco) if dell else None
    if cpu is None:
        return None, f"DIMM {banco}{int(numero)}"
    return f"dimm_cpu{cpu}", f"DIMM {banco}{int(numero)} (CPU{cpu})"


def _problemas_do_componente(medido: dict[str, Any]) -> list[tuple[str, str | None, str | None, bool]]:
    """``(texto do problema, nome para localizar, entidade IPMI, local informado pelo BMC?)`` de um componente com
    ALERTA/FALHA."""
    dados = medido.get("dados") or {}
    saida: list[tuple[str, str | None, str | None, bool]] = []
    for chave in ("leituras", "sensores", "fontes", "discos", "portas", "controladoras"):
        for item in dados.get(chave) or []:
            if not isinstance(item, dict):
                continue
            avaliacao = str(item.get("avaliacao") or item.get("status_msscan") or "").upper()
            saude = str(item.get("saude") or "")
            if avaliacao in STATUS_PROBLEMA or saude in ("Warning", "Critical"):
                nome = str(item.get("nome") or item.get("dispositivo") or "")
                leitura = item.get("leitura") if isinstance(item.get("leitura"), str) else (avaliacao or saude)
                texto = f"{nome}: {leitura}".strip(": ")
                saida.append((texto, item.get("local") or nome, item.get("entidade"), bool(item.get("local"))))
    for evento in dados.get("eventos_problema") or []:
        texto = f"log do BMC {evento.get('data', '')} {evento.get('hora', '')}: {evento.get('sensor', '')} — {evento.get('evento', '')}"
        saida.append((texto.strip(), f"{evento.get('sensor', '')} {evento.get('evento', '')}".strip(), None, False))
    if not saida:
        for problema in dados.get("problemas") or []:
            saida.append((str(problema), str(problema), None, False))
    return saida


def _nucleo_evento(texto: str) -> str:
    """"Sensor — evento" de um texto do log do BMC, sem data, número do registro nem notas; "" se não for do log."""
    if not texto.startswith("log do BMC"):
        return ""
    nucleo = re.sub(r"^log do BMC (?:#\w+ )?.*? [-+]\d\d:\s*", "", texto)
    return nucleo.split(" (", 1)[0].split(" | ", 1)[0].strip()


def localizar_relatorio(relatorio: dict[str, Any]) -> dict[str, Any]:
    """``relatorio["localizacao"]``: mapa do modelo (se houver) e, por problema, a área física ou o motivo.

    Itens repetidos (mesmo componente e mesma área) são agrupados, com a contagem.
    """
    equipamento = relatorio.get("equipamento") or {}
    compat = equipamento.get("compatibilidade") or {}
    entrada = compat.get("entrada") or {}
    if not isinstance(entrada.get("mapa"), dict) and isinstance(compat.get("generico"), dict):
        entrada = compat["generico"]  # mapa genérico (19.4): áreas aproximadas, marcadas na vista e em cada nome
    mapa = entrada.get("mapa") if isinstance(entrada.get("mapa"), dict) else None
    dell = (equipamento.get("fabricante") or "").lower().startswith("dell")
    ids_mapa = {a["id"]: a for a in (mapa or {}).get("areas") or [] if isinstance(a, dict) and a.get("id")}
    itens: list[dict[str, Any]] = []
    vistos: dict[tuple[str, str], dict[str, Any]] = {}
    eventos_listados: dict[str, str] = {}
    """Evento do log do BMC → componente que já o mostrou."""
    # O log do BMC por último: evento que já aparece num componente (Memória, Componentes internos) não se repete nele
    # (bancada de 16/10/2026: o mesmo evento saía duas vezes).
    medidos = sorted(relatorio.get("medido") or [], key=lambda m: m.get("componente") == "log_bmc")
    for medido in medidos:
        if medido.get("status") not in STATUS_PROBLEMA:
            continue
        for texto, nome, entidade, do_bmc in _problemas_do_componente(medido):
            achado = local_de(nome or texto, entidade, dell) or (local_de(texto, None, dell) if nome != texto else None)
            area, rotulo = achado if achado else (None, "")
            evento = _nucleo_evento(texto)
            if medido.get("componente") == "log_bmc" and eventos_listados.get(evento, "log_bmc") != "log_bmc":
                continue
            # Agrupa repetições: mesma área, ou (sem área) o mesmo sensor/evento — sem a data.
            chave = (str(medido.get("componente")), rotulo or re.sub(r"^log do BMC .*? [-+]\d\d:\s*", "", nome or texto))
            if chave in vistos:
                vistos[chave]["ocorrencias"] += 1
                vistos[chave]["problema"] = texto[:300]  # a última ocorrência, como no resumo do componente
                continue
            if evento:
                eventos_listados.setdefault(evento, str(medido.get("componente")))
            item = {
                "componente": medido.get("componente"), "titulo": medido.get("titulo"), "status": medido.get("status"),
                "problema": texto[:300], "local": rotulo or NAO_LOCALIZADO, "localizado": bool(rotulo),
                "area": area if area in ids_mapa else None,
                "descricao_area": ids_mapa[area]["nome"] if area in ids_mapa else "",
                "origem": "local informado pelo BMC (Redfish)" if do_bmc else "nome do sensor ou do evento" if rotulo else "",
                "ocorrencias": 1,
            }  # fmt: skip
            vistos[chave] = item
            itens.append(item)
            if len(itens) >= MAX_ITENS:
                break
    return {"mapa": mapa, "modelo": entrada.get("nome") or equipamento.get("modelo"), "itens": itens,
            "aproximado": bool(entrada.get("aproximado")), "aviso": entrada.get("aviso") or ""}


def svg_mapa(mapa: dict[str, Any] | None, destacadas: set[str], titulo: str = "") -> str:
    """Mapa do chassi em SVG (para o relatório HTML autocontido): áreas com problema destacadas (cor + contorno
    grosso + rótulo "problema" — nunca só a cor). ``""`` sem mapa."""
    from html import escape

    if not mapa:
        return ""
    largura, altura = int(mapa.get("largura") or 1000), int(mapa.get("altura") or 600)
    partes = [f'<svg viewBox="0 -24 {largura} {altura + 48}" role="img" aria-label="{escape(titulo or "Mapa do chassi")}" '
              f'xmlns="http://www.w3.org/2000/svg" style="max-width:100%;height:auto;font:14px sans-serif">']
    partes.append(f'<rect x="0" y="30" width="{largura - 43}" height="{altura - 60}" fill="#161b22" stroke="#6e7681" stroke-width="2" rx="8"/>')
    rotulos = mapa.get("rotulos") or {}
    partes.append(f'<text x="4" y="-6" fill="#b1bac4">{escape(rotulos.get("frente", "FRENTE"))} ←</text>')
    partes.append(f'<text x="{largura - 60}" y="-6" fill="#b1bac4" text-anchor="end">→ {escape(rotulos.get("traseira", "TRASEIRA"))}</text>')
    for area in sorted(mapa.get("areas") or [], key=lambda a: a.get("id") in destacadas):  # destacadas por cima
        if area.get("id") in ("risers", "chassi", "frente", "traseira") and area.get("id") not in destacadas:
            continue
        marcada = area.get("id") in destacadas
        cor, borda, espessura = ("rgba(248,81,73,.35)", "#f85149", 5) if marcada else ("rgba(88,166,255,.08)", "#6e7681", 1)
        partes.append(f'<rect x="{area["x"]}" y="{area["y"]}" width="{area["w"]}" height="{area["h"]}" fill="{cor}" '
                      f'stroke="{borda}" stroke-width="{espessura}" rx="4"><title>{escape(area["nome"])}</title></rect>')  # fmt: skip
        coberto = not marcada and any(
            d.get("id") in destacadas and d.get("id") != area.get("id") and d["x"] <= area["x"] + 6 <= d["x"] + d["w"]
            and d["y"] <= area["y"] + 18 <= d["y"] + d["h"] for d in mapa.get("areas") or [])  # fmt: skip
        if coberto:
            continue
        rotulo = escape(area.get("curto") or area["nome"].split(" (")[0]) + (" — PROBLEMA" if marcada else "")
        partes.append(f'<text x="{area["x"] + 6}" y="{area["y"] + 18}" fill="{"#ffdcd7" if marcada else "#b1bac4"}" '
                      f'font-weight="{700 if marcada else 400}">{rotulo}</text>')  # fmt: skip
    partes.append("</svg>")
    return "".join(partes)


__all__ = ["NAO_LOCALIZADO", "local_de", "localizar_relatorio", "svg_mapa"]
