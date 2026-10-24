"""Eventos do log do BMC (SEL): HISTÓRICO × ATUAL (pedido de 01/10/2026, CLAUDE.md 17.1).

O SEL é só histórico: guarda o que aconteceu, inclusive com peças que já saíram do servidor, e o relógio do BMC não é
confiável (o R640 real tem eventos datados de 1998). Por isso:

- **ATUAL** = confirmado agora por uma leitura ao vivo (EDAC desde o boot, estado de sensor, Status do Redfish, SMBIOS);
- **HISTÓRICO** = só no log. Evento de um slot/peça que está vazio agora → "peça não está mais instalada".

Bloqueios da atualização só com evidência ATUAL e crítica; evento histórico crítico vira aviso com confirmação extra
("Estou ciente de que é um evento antigo"). O evento do SEL não traz o serial da peça: "trocada" (slot ocupado por outra
peça) não é detectável só pelo log — por isso a sugestão "se a peça foi trocada, exporte e limpe o SEL".

Funções puras (dados → classificação), testadas em ``tests/test_historico.py``.
"""

from __future__ import annotations

import re
from typing import Any

ATUAL, HISTORICO = "atual", "historico"
TEXTO_CIENTE = "Estou ciente de que é um evento antigo"
SUGESTAO_LIMPAR_SEL = ("Se a peça foi trocada ou reencaixada, exporte e limpe o SEL (Ações corretivas → Exportar e limpar o "
                       "log do BMC) e refaça o diagnóstico: assim o log volta a mostrar só o que acontecer daqui em diante.")  # fmt: skip
NOTA_DATAS = "as datas do log do BMC não são confiáveis (relógio do BMC)"
# "DIMMA2" (SEL do R640), "DIMM A2", "DIMM_A2" (dmidecode do R620, 12ª) e "A2" sozinho (dmidecode do R640, 14ª).
_DIMM = re.compile(r"DIMM[\s_.]*(?:Socket[\s_.]*)?([A-Z])[\s_]*(\d{1,2})\b", re.IGNORECASE)
_SLOT_SOZINHO = re.compile(r"\s*([A-Z])(\d{1,2})\s*", re.IGNORECASE)
_CPU = re.compile(r"\bCPU\s*(\d)\b|\bProc(?:essor)?\s*(\d)\b", re.IGNORECASE)
_CRITICO = re.compile(r"uncorr|multi-?bit|fatal|ierr|machine check|thermal trip|non-recoverable|config error", re.IGNORECASE)


def slot_dimm(texto: str | None) -> str | None:
    """``"... DIMMA2)"`` → ``"A2"``; ``None`` se o texto não identifica o slot."""
    casado = _DIMM.search(texto or "") or _SLOT_SOZINHO.fullmatch(texto or "")
    return f"{casado.group(1).upper()}{int(casado.group(2))}" if casado else None


def numero_cpu(texto: str | None) -> int | None:
    """``"CPU2 Status"`` → ``2``; ``None`` se o texto não identifica o processador."""
    casado = _CPU.search(texto or "")
    return int(next(g for g in casado.groups() if g)) if casado else None


def critico(evento: dict[str, str]) -> bool:
    """Evento de erro grave (não corrigível, fatal, IERR, machine check, config error...)."""
    return bool(_CRITICO.search(f"{evento.get('sensor', '')} {evento.get('evento', '')}"))


def _texto(evento: dict[str, str]) -> str:
    descricao = evento.get("evento") or "(sem descrição)"
    direcao = f" ({evento['direcao']})" if evento.get("direcao") else ""
    data = " ".join(x for x in (evento.get("data"), evento.get("hora")) if x)
    return f"#{evento.get('id', '?')} {data}: {evento.get('sensor', '')} — {descricao}{direcao}"


def classificar(evento: dict[str, str], peca: str | None, presente: bool | None, confirmado_agora: str = "") -> dict[str, Any]:
    """Classifica UM evento-problema do SEL.

    ``peca``: o que o evento identifica ("DIMM A2", "CPU2") ou ``None``; ``presente``: a peça está instalada agora
    (``None`` = não dá para saber); ``confirmado_agora``: leitura ao vivo que confirma o problema (vazio = nenhuma).
    """
    if confirmado_agora:
        classe, nota = ATUAL, f"atual — confirmado agora: {confirmado_agora}"
    elif presente is False:
        classe, nota = HISTORICO, f"histórico — {peca} está vazio agora: a peça não está mais instalada"
    elif presente:
        classe, nota = HISTORICO, (f"histórico — {peca} está ocupado agora, mas nenhuma leitura atual confirma o problema "
                                   "(se a peça foi trocada, o evento é da peça antiga)")  # fmt: skip
    else:
        classe, nota = HISTORICO, "histórico — só no log do BMC; nenhuma leitura atual confirma o problema"
    return {"texto": _texto(evento), "classe": classe, "nota": nota, "peca": peca, "presente": presente,
            "critico": critico(evento)}  # fmt: skip


def classificar_memoria(eventos: list[dict[str, str]], slots_instalados: set[str] | None,
                        ue_agora: dict[str, int] | None = None) -> list[dict[str, Any]]:  # fmt: skip
    """Eventos de memória do SEL × slots ocupados agora (``dmidecode -t 17``) × erros do EDAC desde o boot.

    ``ue_agora``: slot → erros (corrigíveis + não corrigíveis) do EDAC com rótulo de slot; a chave ``"*"`` vale para
    erros do EDAC sem slot identificado (confirma só eventos sem slot).
    """
    ue_agora = ue_agora or {}
    saida = []
    for evento in eventos:
        slot = slot_dimm(evento.get("evento")) or slot_dimm(evento.get("sensor"))
        presente = None if slot is None or slots_instalados is None else slot in slots_instalados
        erros = ue_agora[slot] if slot in ue_agora else ue_agora.get("*", 0)
        confirmado = f"EDAC com {erros} erro(s) desde a inicialização" if erros and presente is not False else ""
        saida.append(classificar(evento, f"o slot {slot}" if slot else None, presente, confirmado))
    return saida


def classificar_cpu(eventos: list[dict[str, str]], soquetes: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Eventos de processador do SEL × soquetes agora (``dmidecode -t 4``: populado/habilitado).

    "Presence detected" deasserted (processador sumiu) com todos os soquetes populados e habilitados agora é histórico;
    soquete identificado no evento e vazio agora → peça não está mais instalada; soquete desabilitado agora confirma.
    """
    saida = []
    populados = [s for s in soquetes or [] if s.get("populado")]
    todos_ok = bool(populados) and all(s.get("habilitado") for s in populados)
    for evento in eventos:
        numero = numero_cpu(evento.get("sensor")) or numero_cpu(evento.get("evento"))
        soquete = next((s for s in soquetes or [] if numero_cpu(s.get("soquete")) == numero), None) if numero else None
        presente = None if soquete is None else bool(soquete.get("populado"))
        confirmado = f"{soquete['soquete']}: {soquete.get('status')}" if soquete and soquete.get("populado") and not soquete.get("habilitado") else ""
        if not confirmado and numero is None and soquetes and not todos_ok:
            confirmado = "; ".join(f"{s['soquete']}: {s.get('status')}" for s in populados if not s.get("habilitado"))
        item = classificar(evento, f"o soquete CPU{numero}" if numero else None, presente, confirmado)
        if item["classe"] == HISTORICO and presente is None and todos_ok:
            item["nota"] = f"histórico — os {len(populados)} processador(es) estão presentes e habilitados agora"
        saida.append(item)
    return saida


def resumo(classificados: list[dict[str, Any]]) -> dict[str, int]:
    """Contagem por classe (para a tela e o relatório)."""
    return {"atual": sum(c["classe"] == ATUAL for c in classificados),
            "historico": sum(c["classe"] == HISTORICO for c in classificados)}  # fmt: skip


def historicos_criticos(medido: dict[str, dict[str, Any]]) -> list[str]:
    """Textos dos eventos HISTÓRICOS críticos de todos os componentes (``dados["eventos_sel"]``) — aviso, nunca bloqueio."""
    saida = []
    for resultado in medido.values():
        for item in ((resultado or {}).get("dados") or {}).get("eventos_sel") or []:
            if isinstance(item, dict) and item.get("classe") == HISTORICO and item.get("critico"):
                saida.append(f"{item['texto']} ({item['nota']})")
    return saida
