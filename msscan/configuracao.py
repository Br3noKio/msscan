"""Configuração do servidor: processadores, pentes por processador/canal, fontes e discos (pedido de 01/10/2026).

Função pura sobre o relatório (o que o diagnóstico mediu) + regras de montagem do modelo no banco de compatibilidade
(bloco ``montagem``, conferido no manual oficial do fabricante). Mostra no início, por exemplo,
"Configuração: 2 processadores, 1 pente (8 GB), 2 fontes (redundante)", e avisa quando a montagem está fora do que o
fabricante documenta. Configuração mínima ou single (1 processador, 1 fonte) é válida: só informada, nunca erro —
exceto quando o fabricante diz que não é suportada.

**Nunca adivinha:** sem regra no banco, só o resumo; sem dado medido, "não verificado".
"""

from __future__ import annotations

import re
from typing import Any

_SLOT = re.compile(r"^(?:DIMM[\s_]*)?([A-Z])(\d{1,2})$", re.IGNORECASE)  # "A1" (R640), "DIMM_A1" (R620)


def _medido(relatorio: dict[str, Any], componente: str) -> dict[str, Any]:
    return next((m for m in relatorio.get("medido") or [] if m.get("componente") == componente), {})


def _plural(n: int, singular: str, plural: str) -> str:
    return f"{n} {singular if n == 1 else plural}"


def _fontes(medido: dict[str, Any]) -> dict[str, Any] | None:
    """Fontes instaladas e com energia, pelas leituras de presença do IPMI (entidade 10.N) ou do Redfish."""
    dados = medido.get("dados") or {}
    leituras = [x for x in dados.get("leituras") or [] if isinstance(x, dict) and str(x.get("entidade") or "").startswith("10.")]
    if leituras:
        instaladas = [x for x in leituras if any("presence detected" in str(e).lower() for e in x.get("estados") or [])]
        sem_energia = [x for x in instaladas if "sem_energia" in (x.get("categorias") or [])]
        return {"instaladas": len(instaladas), "com_energia": len(instaladas) - len(sem_energia)}
    fontes = [x for x in dados.get("fontes") or [] if isinstance(x, dict)]
    if fontes:
        instaladas = [x for x in fontes if str(x.get("estado") or "").lower() != "absent"]
        return {"instaladas": len(instaladas), "com_energia": None}
    return None


def avaliar_configuracao(relatorio: dict[str, Any]) -> dict[str, Any]:
    """Resumo da configuração e avisos de montagem. Nunca levanta exceção por dado ausente."""
    entrada = ((relatorio.get("equipamento") or {}).get("compatibilidade") or {}).get("entrada") or {}
    regras = entrada.get("montagem") or {}
    avisos: list[str] = []
    informacoes: list[str] = []
    partes: list[str] = []

    cpu = _medido(relatorio, "cpu").get("dados") or {}
    soquetes = [p for p in cpu.get("processadores") or [] if p.get("populado")]
    n_cpu = len(soquetes) if soquetes else None
    partes.append(_plural(n_cpu, "processador", "processadores") if n_cpu else "processadores não verificados")

    modulos = [m for m in (_medido(relatorio, "memoria").get("dados") or {}).get("modulos") or [] if m.get("instalado", True)]
    gb = sum(int(m.get("tamanho_mb") or 0) for m in modulos) // 1024
    partes.append(f"{_plural(len(modulos), 'pente', 'pentes')} ({gb} GB)" if modulos else "pentes não verificados")

    fontes = _fontes(_medido(relatorio, "fontes"))
    maximo_fontes = (regras.get("fontes") or {}).get("maximo")
    if fontes:
        texto = _plural(fontes["instaladas"], "fonte", "fontes")
        if fontes["instaladas"] >= 2 and fontes["com_energia"] == fontes["instaladas"]:
            texto += " (redundante)"
        elif fontes["instaladas"] >= 2:
            texto += f" ({fontes['com_energia']} com energia: sem redundância agora)"
        else:
            texto += " (sem redundância)"
        partes.append(texto)
    else:
        partes.append("fontes não verificadas")

    discos = len((_medido(relatorio, "discos").get("dados") or {}).get("discos") or [])
    if discos:
        partes.append(_plural(discos, "disco", "discos"))

    por_cpu = _por_processador(modulos, regras)
    avisos_memoria = []
    if regras:
        _regras_cpu(n_cpu, soquetes, regras, avisos, informacoes)
        avisos_memoria: list[str] = []
        _regras_memoria(n_cpu, por_cpu, regras, avisos_memoria, informacoes)
        avisos += avisos_memoria
        _regras_fontes(fontes, regras, maximo_fontes, avisos, informacoes)
    else:
        informacoes.append("Sem regras de montagem deste modelo no banco de compatibilidade: só o resumo do que foi medido.")

    return {
        "resumo": "Configuração: " + ", ".join(partes),
        "processadores": n_cpu, "pentes": len(modulos), "memoria_gb": gb, "por_processador": por_cpu,
        "fontes": {**(fontes or {}), "esperadas": maximo_fontes}, "discos": discos,
        "avisos": avisos, "avisos_memoria": avisos_memoria, "informacoes": informacoes, "fonte_regras": regras.get("fonte") or "",
        "situacao": "fora_do_recomendado" if avisos else "ok",
    }  # fmt: skip


def _por_processador(modulos: list[dict[str, Any]], regras: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Pentes agrupados por processador e canal (letra do slot → CPU e número → canal, pelas regras do modelo)."""
    memoria = regras.get("memoria") or {}
    banco_cpu = {str(k).upper(): int(v) for k, v in (memoria.get("banco_por_cpu") or {}).items()}
    canal_do_slot = {int(k): int(v) for k, v in (memoria.get("canal_do_slot") or {}).items()}
    saida: dict[str, dict[str, Any]] = {}
    for modulo in modulos:
        casado = _SLOT.match(str(modulo.get("slot") or "").strip())
        if not casado or casado.group(1).upper() not in banco_cpu:
            chave = "?"
        else:
            chave = f"CPU{banco_cpu[casado.group(1).upper()]}"
        grupo = saida.setdefault(chave, {"slots": [], "canais": [], "gb": 0})
        grupo["slots"].append(str(modulo.get("slot")))
        grupo["gb"] += int(modulo.get("tamanho_mb") or 0) // 1024
        if casado and int(casado.group(2)) in canal_do_slot:
            canal = canal_do_slot[int(casado.group(2))]
            if canal not in grupo["canais"]:
                grupo["canais"].append(canal)
    return saida


def _regras_cpu(n_cpu: int | None, soquetes: list[dict[str, Any]], regras: dict[str, Any], avisos: list[str],
                informacoes: list[str]) -> None:  # fmt: skip
    cpu = regras.get("processadores") or {}
    if n_cpu == 1 and cpu.get("single_suportado", True):
        informacoes.append("Configuração com 1 processador (single): válida para este modelo. Só os slots de memória "
                           "desse processador funcionam.")  # fmt: skip
    if n_cpu and cpu.get("maximo") and n_cpu > int(cpu["maximo"]):
        avisos.append(f"O SMBIOS informa {n_cpu} processadores; o modelo suporta até {cpu['maximo']}.")
    desabilitados = [p["soquete"] for p in soquetes if not p.get("habilitado")]
    if desabilitados:
        avisos.append(f"Processador instalado mas desabilitado: {', '.join(desabilitados)}.")


def _regras_memoria(n_cpu: int | None, por_cpu: dict[str, dict[str, Any]], regras: dict[str, Any], avisos: list[str],
                    informacoes: list[str]) -> None:  # fmt: skip
    memoria = regras.get("memoria") or {}
    primeiro = {int(k): str(v) for k, v in (memoria.get("primeiro_slot") or {}).items()}
    if not n_cpu:
        return
    for numero in range(1, n_cpu + 1):
        grupo = por_cpu.get(f"CPU{numero}")
        if not grupo:
            aviso = f"CPU{numero} sem nenhum pente de memória"
            if primeiro.get(numero):
                aviso += f" (o primeiro slot dela é o {primeiro[numero]})"
            if numero > 1 and memoria.get("identica_entre_cpus"):
                aviso += (". O fabricante pede memória idêntica em cada processador num servidor com 2 processadores "
                          f"(ex.: pente no {primeiro.get(1, 'A1')} → pente igual no {primeiro.get(numero, 'B1')}); fora "
                          "disso o servidor pode não iniciar, travar ao configurar a memória ou operar com memória "
                          "reduzida")  # fmt: skip
            avisos.append(aviso + ".")
        elif primeiro.get(numero) and primeiro[numero] not in grupo["slots"]:
            avisos.append(f"CPU{numero}: o slot {primeiro[numero]} (o primeiro a preencher, alça branca) está vazio "
                          f"e há pentes em {', '.join(grupo['slots'])}: siga a ordem de preenchimento do fabricante.")  # fmt: skip
    if por_cpu.get("?"):
        avisos.append(f"Pentes em slots que não pertencem a nenhum processador instalado: {', '.join(por_cpu['?']['slots'])}.")
    if n_cpu > 1 and memoria.get("identica_entre_cpus"):
        quantidades = {c: len(g["slots"]) for c, g in por_cpu.items() if c != "?"}
        if len(set(quantidades.values())) > 1 and all(quantidades.get(f"CPU{n}") for n in range(1, n_cpu + 1)):
            avisos.append("Quantidade de pentes diferente entre os processadores ("
                          + ", ".join(f"{c}: {q}" for c, q in sorted(quantidades.items()))
                          + "): o fabricante pede configuração idêntica (desempenho e estabilidade).")  # fmt: skip
    canais = memoria.get("canais_por_cpu")
    for cpu, grupo in sorted(por_cpu.items()):
        if cpu != "?" and canais and len(grupo["slots"]) < int(canais):
            informacoes.append(f"{cpu}: {len(grupo['slots'])} pente(s) em {len(grupo['canais']) or '?'} de {canais} canais "
                               "(funciona; desempenho máximo com um pente por canal).")  # fmt: skip


def _regras_fontes(fontes: dict[str, Any] | None, regras: dict[str, Any], maximo: Any, avisos: list[str],
                   informacoes: list[str]) -> None:  # fmt: skip
    if not fontes:
        return
    regra = regras.get("fontes") or {}
    if fontes["instaladas"] == 1:
        if regra.get("uma_suportada") is False:
            avisos.append("Uma fonte só: o fabricante não suporta esta configuração neste modelo.")
        else:
            informacoes.append("Uma fonte instalada: funciona, sem redundância de energia." + (f" {regra['nota']}" if regra.get("nota") else ""))
    elif maximo and fontes["instaladas"] > int(maximo):
        avisos.append(f"{fontes['instaladas']} fontes detectadas; o modelo aceita até {maximo}.")
    if fontes["instaladas"] >= 2 and regra.get("iguais"):
        informacoes.append(str(regra["iguais"]))
