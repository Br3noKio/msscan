"""Tela única "Plano de atualização" antes de instalar driver/firmware (CLAUDE.md 15.6, substituída em parte pela 18.2).

Vale para os três caminhos (catálogo, pacote manual e navegador integrado) e para a interface e o modo texto:

- o que será instalado (componente, atual → nova, data, origem, hash);
- ordem (menor risco primeiro, BIOS por último), tempo estimado e número de reinícios;
- hardware conferido NA HORA (o chamador relê fontes, memória, RAID, sensores e ventoinhas antes de montar o aviso):
  fonte com defeito, erro de memória não corrigível, RAID degradado/em reconstrução, sensor ou ventoinha crítica
  BLOQUEIAM; redundância de energia perdida é destacada e pede a confirmação extra ``SEM REDUNDANCIA``;
- rollback disponível no iDRAC por item;
- mudanças TEMPORÁRIAS que o MSscan pode fazer no iDRAC (só informativo; ``automatica.prever_temporarias``);
- avisos fixos; a instalação segue com ``ATUALIZAR`` digitado (uma confirmação; 18.2) e, só quando se aplicam, as
  confirmações extras na mesma tela (``SEM REDUNDANCIA``, "Estou ciente de que é um evento antigo"). Depois, tudo é
  automático (``automatica.py``).

Funções puras (dados → aviso), mais ``conferir_hardware`` (releitura somente leitura) e os montadores usados pela
interface e pelo modo texto.
"""

from __future__ import annotations

import time
from typing import Any

from msscan import textos
from msscan.atualizacao import liberacao
from msscan.atualizacao.verificacao import MENSAGEM_FONTE_INCERTA
from msscan.core.modelo import Status
from msscan.historico import SUGESTAO_LIMPAR_SEL, TEXTO_CIENTE, historicos_criticos
from msscan.modulos.refazer import preparar_releitura

COMPONENTES_HARDWARE = ("fontes", "memoria", "raid", "sensores", "ventoinhas", "cpu", "internos")
"""Relidos na hora de montar o aviso (somente leitura)."""
VALIDADE_S = 600
"""O aviso vale 10 minutos: depois disso o hardware é conferido de novo."""
AVISOS_FIXOS = tuple(textos.t(f"py_aviso.fixo_{n}") for n in range(1, 6))
BLOQUEIA, REDUNDANCIA, HISTORICO, AVISO, OK = "bloqueia", "redundancia", "historico", "aviso", "ok"
ITEM_HISTORICO = textos.t("py_aviso.item_historico")
"""``historico``: evento crítico só no log do BMC (não confirmado agora) — aviso com a confirmação ``TEXTO_CIENTE``."""
SEM_VERIFICACAO = textos.t("py_aviso.sem_verificacao")

# Ordem de instalação (pedido do usuário, 09/10/2026; substitui o "iDRAC primeiro" da 21.3): do menor para o maior risco —
# placas de rede, depois os demais componentes, depois o iDRAC SOZINHO (esperando ele voltar com a versão nova) e a BIOS
# por último, também sozinha. Antes de um item sozinho, o que ficou agendado é aplicado num reinício e conferido.
PESO_IDRAC, PESO_BIOS = 8, 9
_ORDEM = (("idrac", PESO_IDRAC), ("lifecycle", PESO_IDRAC), ("integrated dell remote access", PESO_IDRAC), ("diagnostic", 2),
          ("network", 1), ("fibre", 1), ("nic", 1), ("ethernet", 1), ("sas", 3), ("raid", 3), ("storage", 3), ("power", 4),
          ("cpld", 5), ("chipset", 5), ("bios", PESO_BIOS))  # fmt: skip
PESO_DESCONHECIDO = 6
"""Categoria desconhecida: depois das conhecidas e antes da BIOS."""
_MINUTOS = (("idrac", 15), ("lifecycle", 15), ("bios", 10), ("cpld", 10), ("raid", 8), ("sas", 8), ("storage", 8),
            ("power", 10), ("network", 5), ("fibre", 5), ("nic", 5))  # fmt: skip
MINUTOS_PADRAO = 8
MINUTOS_REINICIO = 6


def _categoria(item: dict[str, Any]) -> str:
    return str(item.get("categoria") or item.get("componente") or "").lower()


def peso_ordem(item: dict[str, Any]) -> int:
    """Posição do item na ordem de instalação (BIOS = 9, sempre por último)."""
    categoria = _categoria(item)
    return next((peso for chave, peso in _ORDEM if chave in categoria), PESO_DESCONHECIDO)


def ordenar(itens: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Placas de rede, demais, iDRAC e BIOS por último (ordenação estável: empate mantém a ordem recebida)."""
    return sorted(itens, key=peso_ordem)


def eh_idrac(item: dict[str, Any]) -> bool:
    """Firmware do próprio iDRAC (com Lifecycle Controller): instalado sozinho, depois dos demais e antes da BIOS."""
    return peso_ordem(item) == PESO_IDRAC


def sozinho(item: dict[str, Any]) -> bool:
    """iDRAC e BIOS: instalados sozinhos, cada um depois de o anterior ser confirmado (parar na primeira falha)."""
    return peso_ordem(item) in (PESO_IDRAC, PESO_BIOS)


def minutos_item(item: dict[str, Any]) -> int:
    return next((m for chave, m in _MINUTOS if chave in _categoria(item)), MINUTOS_PADRAO)


def estimativa(itens: list[dict[str, Any]]) -> dict[str, Any]:
    """Tempo aproximado (minutos) e reinícios, por componente e no total (CLAUDE.md 21.3).

    Os itens que exigem reinício do servidor são agrupados no MESMO reinício; o iDRAC vai sozinho (um reinício próprio só se
    o pacote dele exigir)."""
    juntos = any(i.get("reinicio") and not sozinho(i) for i in itens)
    reinicios = int(juntos) + sum(1 for i in itens if i.get("reinicio") and sozinho(i))
    por_item = [{"componente": i.get("componente"), "minutos": minutos_item(i), "reinicio": bool(i.get("reinicio")),
                 "sozinho": sozinho(i), "idrac": eh_idrac(i)} for i in itens]  # fmt: skip
    return {"minutos": sum(p["minutos"] for p in por_item) + reinicios * MINUTOS_REINICIO, "reinicios": reinicios,
            "por_item": por_item}


def assinatura_catalogo(item: dict[str, Any]) -> str:
    """O que o aviso mostrou de um item do catálogo (a instalação tem de ser exatamente isto)."""
    pacote = item.get("pacote") or {}
    return "|".join(str(x or "") for x in (item.get("componente"), item.get("component_id"), item.get("instalada"),
                                             item.get("disponivel"), pacote.get("url"), pacote.get("sha256") or pacote.get("md5")))  # fmt: skip


def assinatura_manual(analise: dict[str, Any]) -> str:
    """O que o aviso mostrou de um pacote manual (arquivo e SHA-256 conferido)."""
    return "|".join(str(analise.get(k) or "") for k in ("arquivo", "sha256", "instalada", "disponivel"))


def item_do_catalogo(item: dict[str, Any], rollback: str | None, bmc_lido: bool) -> dict[str, Any]:
    """Linha do aviso para um item do catálogo Dell (hash conferido ao baixar, antes de enviar ao BMC)."""
    pacote = item.get("pacote") or {}
    hash_oficial = "SHA-256" if pacote.get("sha256") else "MD5" if pacote.get("md5") else ""
    return {
        "id": item.get("id"), "componente": item.get("componente"), "categoria": pacote.get("categoria") or "",
        "atual": item.get("instalada"), "nova": item.get("disponivel"), "data": textos.data_br(pacote.get("data")) if pacote.get("data") else "",
        "origem": textos.t("py_aviso.origem_catalogo"),
        "hash": textos.t("py_aviso.hash_catalogo", hash_oficial),
        "reinicio": bool(pacote.get("reinicio")), "rollback": _texto_rollback(rollback, bmc_lido),
        "assinatura": assinatura_catalogo(item),
    }  # fmt: skip


def item_manual(analise: dict[str, Any], rollback: str | None, bmc_lido: bool) -> dict[str, Any]:
    """Linha do aviso para um pacote baixado pelo navegador integrado ou posto no pendrive (SHA-256 já conferido)."""
    pacote = analise.get("pacote") or {}
    identificacao = analise.get("identificacao") or {}
    return {
        "id": analise.get("id"), "componente": pacote.get("nome") or analise.get("nome"),
        "categoria": pacote.get("categoria") or identificacao.get("categoria") or "",
        "atual": analise.get("instalada"), "nova": analise.get("disponivel"), "data": pacote.get("data") or "",
        "origem": textos.t("py_aviso.origem_manual", analise.get("nome")),
        "hash": textos.t("py_aviso.hash_manual", analise.get("sha256") or "?"),
        "reinicio": bool(pacote.get("reinicio", True)), "rollback": _texto_rollback(rollback, bmc_lido),
        "assinatura": assinatura_manual(analise),
    }  # fmt: skip


def _texto_rollback(versao: str | None, bmc_lido: bool) -> str:
    if versao:
        return textos.t("py_aviso.rollback_disponivel", versao)
    if not bmc_lido:
        return textos.t("py_aviso.rollback_nao_verificado")
    return textos.t("py_aviso.rollback_ausente")


TEXTO_ASSUMO = "ASSUMO OS RISCOS"
"""Confirmação digitada para instalar com checagens de hardware falhando (pedido do usuário, 09/10/2026), além do ATUALIZAR."""
RISCOS = {item: textos.t(f"py_aviso.risco_{chave}") for item, chave in (("Fontes", "fontes"), ("Memória", "memoria"),
          ("Configuração de memória", "configuracao_memoria"), ("RAID", "raid"), ("Temperatura e sensores", "temperatura"),
          ("Ventoinhas", "ventoinhas"), ("Hardware", "hardware"))}
"""Risco concreto de cada checagem de hardware ignorada (tela "Continuar assumindo os riscos", relatório e log)."""
RISCO_NAO_VERIFICADO = textos.t("py_aviso.risco_nao_verificado")
_ARMAZENAMENTO = ("raid", "sas", "storage", "perc", "disk", "drive", "hdd", "ssd", "serial ata", "nvme")


def de_armazenamento(item: dict[str, Any]) -> bool:
    """Firmware da controladora RAID ou de discos (bloqueado com o RAID degradado, mesmo com riscos assumidos)."""
    return any(chave in _categoria(item) for chave in _ARMAZENAMENTO)


def avaliar_hardware(medido: dict[str, dict[str, Any]], compat: dict[str, Any] | None = None) -> list[dict[str, str]]:
    """Situação de cada item de hardware, relido na hora. ``medido``: componente → resultado (``para_dict``).

    ``compat``: banco de compatibilidade do modelo (regras de montagem): a configuração de memória é calculada a cada
    conferência, com os slots ocupados lidos agora (pedido de 09/10/2026; nada de texto fixo)."""
    linhas = [_fontes(medido.get("fontes")), _memoria(medido.get("memoria")), _montagem_memoria(medido, compat),
              _raid(medido.get("raid")),
              _simples(medido.get("sensores"), "Temperatura e sensores", textos.t("py_aviso.sensor_critico")),
              _simples(medido.get("ventoinhas"), "Ventoinhas", textos.t("py_aviso.ventoinha_critica"))]  # fmt: skip
    linhas = [linha for linha in linhas if linha is not None]
    antigos = historicos_criticos(medido)
    if antigos:
        # Só no log do BMC (pedido de 01/10/2026): nunca bloqueia, mas exige "Estou ciente de que é um evento antigo".
        linhas.append(_linha(ITEM_HISTORICO, HISTORICO, textos.t("py_aviso.historico", len(antigos), "; ".join(antigos[:4])
                             + (textos.t("py_aviso.e_mais", len(antigos) - 4) if len(antigos) > 4 else "")) + " " + SUGESTAO_LIMPAR_SEL))
    for linha in linhas:
        if linha["situacao"] == BLOQUEIA:
            linha["risco"] = RISCO_NAO_VERIFICADO if linha["texto"].startswith("Não foi possível verificar") else RISCOS.get(
                linha["item"], RISCOS["Hardware"])  # fmt: skip
    return linhas


def _linha(item: str, situacao: str, texto: str) -> dict[str, str]:
    return {"item": item, "situacao": situacao, "texto": texto}


def _montagem_memoria(medido: dict[str, dict[str, Any]], compat: dict[str, Any] | None) -> dict[str, str] | None:
    """Slots ocupados por processador (lidos agora) e as regras de montagem do fabricante. Fora do suportado → bloqueio
    que o técnico pode assumir (item 2 dos pedidos de 09/10/2026), nunca um bloqueio fixo."""
    from msscan.configuracao import avaliar_configuracao

    if _nao_verificado(medido.get("memoria")):
        return None  # a linha "Memória" já diz que não foi verificada
    configuracao = avaliar_configuracao({"equipamento": {"compatibilidade": compat or {}}, "medido": list(medido.values())})
    grupos = configuracao.get("por_processador") or {}
    if not grupos:
        return None
    ocupados = "; ".join(f"{textos.t('py_aviso.slots_sem_processador') if cpu == '?' else cpu}: {', '.join(g['slots'])} ({g['gb']} GB)"
                         for cpu, g in sorted(grupos.items()))  # fmt: skip
    vazios = [f"CPU{n}" for n in range(1, int(configuracao.get("processadores") or 0) + 1) if f"CPU{n}" not in grupos]
    texto = textos.t("py_aviso.memoria_slots", configuracao["pentes"], configuracao["memoria_gb"], ocupados
                     + (textos.t("py_aviso.cpu_sem_pente", ", ".join(vazios)) if vazios else ""))  # fmt: skip
    avisos = configuracao.get("avisos_memoria") or []
    if avisos:
        return _linha("Configuração de memória", BLOQUEIA, texto + " " + textos.t("py_aviso.memoria_fora", " ".join(avisos)))
    return _linha("Configuração de memória", OK, texto)


def _nao_verificado(resultado: dict[str, Any] | None) -> bool:
    return resultado is None or resultado.get("status") == Status.NAO_VERIFICADO.value


def _fontes(r: dict[str, Any] | None) -> dict[str, str]:
    dados = (r or {}).get("dados") or {}
    if dados.get("defeito"):
        return _linha("Fontes", BLOQUEIA, textos.t("py_aviso.fonte_defeito"))
    if dados.get("classificacao_incerta"):
        return _linha("Fontes", BLOQUEIA, MENSAGEM_FONTE_INCERTA)
    if dados.get("redundancia_perdida"):
        return _linha("Fontes", REDUNDANCIA, textos.t("py_aviso.redundancia"))
    if _nao_verificado(r):
        return _linha("Fontes", BLOQUEIA, textos.t("py_aviso.nao_verificado", "as fontes") + " " + SEM_VERIFICACAO)
    return _linha("Fontes", OK, (r or {}).get("mensagem") or textos.t("py_aviso.fontes_ok"))


def _memoria(r: dict[str, Any] | None) -> dict[str, str]:
    dados = (r or {}).get("dados") or {}
    ue = sum(int(mc.get("nao_corrigiveis") or 0) for mc in dados.get("edac") or [] if isinstance(mc, dict))
    if ue:
        return _linha("Memória", BLOQUEIA, textos.t("py_aviso.memoria_ue", ue))
    # Erro do SEL só bloqueia se confirmado AGORA (EDAC acima; pedido de 01/10/2026): o histórico vai para a linha
    # "eventos antigos" (confirmação extra), nunca para bloqueio — o pente pode até já ter saído do servidor.
    if _nao_verificado(r):
        return _linha("Memória", BLOQUEIA, textos.t("py_aviso.nao_verificado", "a memória") + " " + SEM_VERIFICACAO)
    if (r or {}).get("status") == Status.ALERTA.value:
        return _linha("Memória", AVISO, textos.t("py_aviso.memoria_alerta", "; ".join(map(str, dados.get("problemas") or []))[:300]))
    return _linha("Memória", OK, (r or {}).get("mensagem") or textos.t("py_aviso.memoria_ok"))


def _raid(r: dict[str, Any] | None) -> dict[str, str]:
    status = (r or {}).get("status")
    if status in (Status.FALHA.value, Status.ALERTA.value):
        return _linha("RAID", BLOQUEIA, textos.t("py_aviso.raid_problema", (r or {}).get("mensagem")))
    if _nao_verificado(r):
        return _linha("RAID", AVISO, textos.t("py_aviso.raid_nao_verificado"))
    return _linha("RAID", OK, (r or {}).get("mensagem") or textos.t("py_aviso.raid_ok"))


def _simples(r: dict[str, Any] | None, nome: str, critico: str) -> dict[str, str]:
    status = (r or {}).get("status")
    if status == Status.FALHA.value:
        return _linha(nome, BLOQUEIA, f"{critico}: {(r or {}).get('mensagem')}")
    if _nao_verificado(r):
        return _linha(nome, BLOQUEIA, textos.t("py_aviso.nao_verificado", "este item") + " " + SEM_VERIFICACAO)
    if status == Status.ALERTA.value:
        return _linha(nome, AVISO, str((r or {}).get("mensagem") or textos.t("py_aviso.alerta")))
    return _linha(nome, OK, (r or {}).get("mensagem") or textos.t("py_aviso.normal"))


def montar_aviso(origem: str, itens: list[dict[str, Any]], hardware: list[dict[str, str]], simular: bool,
                 bloqueios_extras: list[str] | None = None, agora: float | None = None,
                 temporarias: list[str] | None = None) -> dict[str, Any]:  # fmt: skip
    """Aviso completo (para a tela e o modo texto). ``itens`` já descritos por ``item_do_catalogo``/``item_manual``.

    Bloqueios em dois grupos (pedido de 09/10/2026): os de hardware (``riscos``), que o técnico pode assumir digitando
    ``ASSUMO OS RISCOS``, e os fixos (``bloqueios_fixos``: hash, modelo, downgrade, outra gravação em andamento, liberação,
    firmware de RAID/discos com o RAID degradado), que nada libera."""
    ordenados = ordenar(itens)
    fixos = list(bloqueios_extras or [])
    riscos = [{"item": h["item"], "texto": h["texto"], "risco": h.get("risco") or RISCOS["Hardware"]}
              for h in hardware if h["situacao"] == BLOQUEIA]  # fmt: skip
    # Firmware de RAID/discos só com o RAID conferido agora e sem problema (revisão de 13/10/2026, C1): degradado, não
    # verificado ou releitura que falhou (sem linha "RAID") é bloqueio fixo, que "ASSUMO OS RISCOS" não libera.
    raid = [str(i.get("componente")) for i in ordenados if de_armazenamento(i)]
    linha_raid = next((h for h in hardware if h["item"] == "RAID"), None)
    if raid and (linha_raid is None or linha_raid["situacao"] != OK):
        texto_raid = "py_aviso.raid_firmware_bloqueado" if linha_raid and linha_raid["situacao"] == BLOQUEIA else "py_aviso.raid_firmware_nao_verificado"
        fixos.append(textos.t(texto_raid, ", ".join(raid)))
    if not ordenados:
        fixos.append(textos.t("py_aviso.sem_itens"))
    bloqueios = fixos + [f"{r['item']}: {r['texto']}" for r in riscos]
    return {
        "bloqueios_fixos": fixos, "riscos": riscos, "pode_assumir": bool(riscos) and not fixos, "texto_assumo": TEXTO_ASSUMO,
        "origem": origem, "chave": chave(origem, [str(i.get("id")) for i in itens]), "gerado": agora or time.time(),
        "modo": "simular" if simular else "aplicar", "itens": ordenados, "estimativa": estimativa(ordenados),
        "hardware": hardware, "bloqueios": bloqueios, "pode_seguir": not bloqueios,
        "redundancia": any(h["situacao"] == REDUNDANCIA for h in hardware),
        "historico": any(h["situacao"] == HISTORICO for h in hardware), "texto_ciente": TEXTO_CIENTE, "ciente": False,
        "avisos_fixos": list(AVISOS_FIXOS), "titulo": "Plano de atualização", "temporarias": list(temporarias or []),
        "assinaturas": sorted(str(i.get("assinatura") or "") for i in itens),
    }  # fmt: skip


def chave(origem: str, ids: list[str]) -> str:
    """Identifica o conjunto de itens do aviso (a instalação tem de ser exatamente deste conjunto)."""
    return origem + ":" + ",".join(sorted(ids))


def conferir_aviso(aviso: dict[str, Any] | None, origem: str, ids: list[str],
                   agora: float | None = None, assinaturas: list[str] | None = None, confirmacao_riscos: str = "") -> str:  # fmt: skip
    """``""`` se a instalação pode seguir por este aviso; senão, o motivo. As confirmações digitadas vêm depois.

    ``assinaturas``: o conteúdo dos itens que serão instalados agora (versões, pacote, hash); tem de ser o do aviso.
    ``confirmacao_riscos``: ``ASSUMO OS RISCOS`` libera só os bloqueios de hardware (``riscos``), nunca os fixos.
    """
    if not aviso:
        return textos.t("py_aviso.abra_o_plano")
    if aviso.get("chave") != chave(origem, ids) or (assinaturas is not None and sorted(assinaturas) != aviso.get("assinaturas")):
        return textos.t("py_aviso.itens_mudaram")
    if (agora or time.time()) - float(aviso.get("gerado") or 0) > VALIDADE_S:
        return textos.t("py_aviso.plano_expirado")
    if not aviso.get("pode_seguir"):
        if aviso.get("pode_assumir") and (confirmacao_riscos or "").strip() == TEXTO_ASSUMO:
            pass  # riscos de hardware assumidos pelo técnico (registrados por ``riscos_assumidos``)
        elif aviso.get("pode_assumir"):
            return textos.t("py_aviso.bloqueio_assumivel", TEXTO_ASSUMO, " ".join(aviso.get("bloqueios") or []))
        else:
            return textos.t("py_aviso.bloqueada", " ".join(aviso.get("bloqueios") or []))
    if aviso.get("historico") and aviso.get("ciente") is not True:
        return textos.t("py_aviso.marque_ciente", TEXTO_CIENTE)
    return ""


def chave_hardware(linha: dict[str, Any]) -> str:
    """Identifica uma condição de hardware aceita na confirmação (a retomada para se aparecer uma nova; M5)."""
    return f"{linha.get('item')}|{linha.get('situacao')}" + (f"|{linha.get('texto')}" if linha.get("situacao") == HISTORICO else "")


def riscos_assumidos(aviso: dict[str, Any], assumidos: bool, agora: Any = None) -> dict[str, Any]:
    """O que o técnico aceitou ao confirmar: para o relatório, o log e a retomada (M5 da revisão de 06/10/2026).

    ``aceitos``: redundância perdida e eventos antigos confirmados e, com ``assumidos``, as checagens de hardware ignoradas.
    Vale só para esta instalação, neste servidor: nada fica gravado como padrão."""
    from datetime import datetime

    aceitos = [chave_hardware(h) for h in aviso.get("hardware") or [] if h["situacao"] in (REDUNDANCIA, HISTORICO, AVISO)]
    registro: dict[str, Any] = {"assumidos": bool(assumidos and aviso.get("riscos")), "aceitos": aceitos, "checagens": []}
    if registro["assumidos"]:
        registro["checagens"] = [dict(r) for r in aviso["riscos"]]
        registro["quando"] = (agora or datetime.now()).isoformat(timespec="seconds")
        registro["aceitos"] += [f"{r['item']}|{BLOQUEIA}" for r in aviso["riscos"]]
    return registro


def novos_na_volta(hardware: list[dict[str, Any]], aceitos: list[str] | None) -> list[str]:
    """Retomada (M5): condições de hardware que bloqueiam ou pedem confirmação e não foram aceitas antes do reinício.

    ``aceitos`` ``None`` (estado gravado por versão anterior): só o bloqueio crítico, como antes."""
    if aceitos is None:
        return [f"{h['item']}: {h['texto']}" for h in hardware if h["situacao"] == BLOQUEIA]
    return [f"{h['item']}: {h['texto']}" for h in hardware if h["situacao"] in (BLOQUEIA, REDUNDANCIA, HISTORICO)
            and chave_hardware(h) not in aceitos]  # fmt: skip


# --- Montagem com releitura (interface e modo texto) ---------------------------------------------


def conferir_hardware(ctx: Any, registro: Any) -> list[dict[str, str]]:
    """Relê AGORA (somente leitura) fontes, memória, RAID, sensores e ventoinhas e avalia. Nunca levanta exceção."""
    try:
        preparar_releitura(ctx, COMPONENTES_HARDWARE)
        medido = {r.componente: r.para_dict() for r in registro.diagnosticar(ctx, somente=COMPONENTES_HARDWARE)}
    except Exception as erro:  # noqa: BLE001 - sem releitura, tudo fica "não verificado" (nunca OK)
        linha = _linha("Hardware", BLOQUEIA, textos.t("py_aviso.hardware_falhou", f"{type(erro).__name__}: {erro}"))
        return [{**linha, "risco": RISCOS["Hardware"]}]
    return avaliar_hardware(medido, getattr(ctx, "compat", None))


def bloqueios_do_banco(ctx: Any) -> list[str]:
    """Bloqueios de atualização do banco de compatibilidade (o banco só restringe)."""
    entrada = (getattr(ctx, "compat", None) or {}).get("entrada") or {}
    return [textos.t("py_aviso.banco", b) for b in (entrada.get("atualizacao") or {}).get("bloqueios") or []]


def aviso_catalogo(ctx: Any, registro: Any, plano: dict[str, Any], ids: list[str], bmc: dict[str, Any], simular: bool) -> dict[str, Any]:
    """Aviso dos itens do catálogo (``plano`` de ``montar_plano`` para os mesmos ``ids``)."""
    anteriores = bmc.get("anteriores") or {}
    itens = [item_do_catalogo(i, anteriores.get(str(i.get("component_id") or "")), bool(bmc.get("redfish")))
             for i in plano.get("itens") or []]  # fmt: skip
    fila = fila_do_idrac(ctx)
    restricao = liberacao.avaliar(ctx.executor, itens)
    extras = (restricao["bloqueios"] + list(plano.get("bloqueios") or []) + list(plano.get("recusados") or []) + bloqueios_do_banco(ctx)
              + _bloqueio_jobs(fila))  # fmt: skip
    aviso = montar_aviso("catalogo", itens, conferir_hardware(ctx, registro), simular, extras, temporarias=_temporarias(ctx, bmc))
    aviso["liberacao"] = restricao
    aviso["chave"] = chave("catalogo", ids)
    aviso["jobs_idrac"] = fila
    aviso["avisos_extras"] = list(plano.get("avisos") or []) + aviso_boot(itens, bmc)
    return aviso


TEXTO_F11 = textos.t("py_aviso.f11")


def aviso_boot(itens: list[dict[str, Any]], bmc: dict[str, Any]) -> list[str]:
    """Antes de começar (21.4): sem boot único pelo pendrive anunciado pelo iDRAC, o técnico precisa saber do F11."""
    if not any(i.get("reinicio") for i in itens) or bmc.get("boot_usb") or bmc.get("boot_uefi_usb"):
        return []
    motivo = textos.t("py_aviso.sem_boot_usb") if bmc.get("redfish") else textos.t("py_aviso.idrac_inacessivel")
    return [textos.t("py_aviso.boot_nao_garantido", motivo, TEXTO_F11)]


def aviso_manual(ctx: Any, registro: Any, analise: dict[str, Any], bmc: dict[str, Any], simular: bool) -> dict[str, Any]:
    """Aviso de um pacote manual (já com SHA-256 conferido e checagem feita)."""
    extras = [] if analise.get("etapa") == "pronto" else [textos.t("py_aviso.pacote_nao_verificado")]
    rollback = (bmc.get("anteriores") or {}).get(str(analise.get("component_id") or ""))
    item = item_manual(analise, rollback, bool(bmc.get("redfish")))
    fila = fila_do_idrac(ctx)
    restricao = liberacao.avaliar(ctx.executor, [item])
    aviso = montar_aviso("manual", [item], conferir_hardware(ctx, registro), simular,
                         restricao["bloqueios"] + extras + bloqueios_do_banco(ctx) + _bloqueio_jobs(fila), temporarias=_temporarias(ctx, bmc))
    aviso["liberacao"] = restricao
    aviso["avisos_extras"] = aviso_boot([item], bmc)
    aviso["jobs_idrac"] = fila
    return aviso


def fila_do_idrac(ctx: Any) -> dict[str, Any]:
    """Fila de jobs do iDRAC resumida (21.4), só leitura. Nunca levanta exceção."""
    from msscan.atualizacao import jobs as fila_jobs

    try:
        return fila_jobs.resumir(fila_jobs.ler_fila(ctx))
    except Exception as erro:  # noqa: BLE001 - sem a fila, o plano segue como antes (o iDRAC recusa duplicatas: RED014)
        return {"origem": "", "motivo": f"fila de jobs não lida ({type(erro).__name__}: {erro})", "jobs": [], "explicacao": [],
                "bloqueio": "", "ativos": 0, "agendados": 0}  # fmt: skip


def _bloqueio_jobs(fila: dict[str, Any]) -> list[str]:
    """21.4: job ativo ou agendado de antes → instalação nova bloqueada (nunca duplicar tarefas no iDRAC)."""
    return [fila["bloqueio"]] if fila.get("bloqueio") else []


def _temporarias(ctx: Any, bmc: dict[str, Any]) -> list[str]:
    from msscan.atualizacao.automatica import prever_temporarias

    return prever_temporarias(ctx, bmc)
