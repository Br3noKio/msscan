"""Provedores Redfish (primeiros da cadeia): sensores, fontes, ventoinhas, log do BMC e RAID.

Só funcionam com credenciais validadas pelo técnico (sessão Redfish); sem elas,
ficam "indisponíveis" e o fallback segue para IPMI, ferramentas e sistema operacional.
A saúde vem do próprio BMC (``Status.Health``): OK → OK, Warning → ALERTA, Critical → FALHA.

Fontes: o Redfish padrão não separa "defeito" de "sem energia" pela saúde. Só há
classificação segura com ``State: Absent`` (ausente) ou ``LineInputStatus`` (sem
energia). Fora disso, uma fonte não saudável é deixada para o IPMI, que distingue
os dois casos; se não houver IPMI, o provedor de reserva mostra ALERTA com
``classificacao_incerta`` (nunca OK, nunca "defeito" inventado).
"""

from __future__ import annotations

from typing import Any

from msscan.core.fallback import Disponibilidade, FalhaColeta, Provedor
from msscan.core.modelo import Resultado, Status, pior_status
from msscan.deteccao.contexto import Contexto
from msscan.parsers import ErroParser
from msscan.parsers.redfish import (
    link,
    membros,
    parse_adaptador_rede,
    parse_controladora,
    parse_disco,
    parse_entradas_log,
    parse_fonte_subsistema,
    parse_porta_rede,
    parse_power,
    parse_servico_log,
    parse_storage,
    parse_thermal,
    parse_volume,
    proxima_pagina,
)
from msscan.provedores.comum import sel_disponivel
from msscan.provedores.ipmi import AUSENTE, EXPLICACAO_REDUNDANCIA, SEM_ENERGIA, avaliar_log_bmc

PRIORIDADE_REDFISH = 10
PRIORIDADE_RESERVA = 90
COMPONENTES_REDFISH = ("sensores", "fontes", "ventoinhas", "log_bmc", "raid", "rede")
"""Componentes com método Redfish (listados no relatório quando o técnico pula as credenciais)."""

SAUDE = {"OK": Status.OK, "Warning": Status.ALERTA, "Critical": Status.FALHA}
ESTADOS_SEM_LEITURA = {"Absent", "Disabled", "StandbyOffline", "UnavailableOffline", "Deferring", "Quiesced"}
ENTRADA_SEM_ENERGIA = {"NoPower", "OutOfRange", "LossOfInput", "InputOutOfRange"}
OPERACOES_RECONSTRUCAO = ("rebuild", "resilver", "resync", "copyback")
MAX_ENTRADAS_LOG = 1000
MAX_ENTRADAS_AVULSAS = 200
IDS_LOG_PREFERIDOS = ("sel", "iel")


class _ProvedorRedfish(Provedor):
    prioridade = PRIORIDADE_REDFISH

    def disponivel(self, ctx: Contexto) -> Disponibilidade:
        acesso = ctx.bmc
        if acesso is None:
            return Disponibilidade(False, ctx.redfish.motivo or "Redfish não detectado")
        if not acesso.validado:
            return Disponibilidade(False, acesso.mensagem or "credenciais do BMC não informadas pelo técnico")
        return Disponibilidade(True)


def obter(ctx: Contexto, caminho: str) -> Any:
    """GET autenticado com cache por diagnóstico (vários provedores leem o mesmo recurso)."""
    cache = ctx.cache.setdefault("redfish", {})
    if caminho not in cache:
        cache[caminho] = ctx.bmc.cliente.get(caminho)
    return cache[caminho]


def _colecao(ctx: Contexto, caminho: str | None) -> list[str]:
    if not caminho:
        return []
    try:
        return membros(obter(ctx, caminho))
    except ErroParser as erro:
        raise FalhaColeta(f"{caminho}: {erro}") from erro


def _raiz(ctx: Contexto) -> dict[str, Any]:
    raiz = ctx.cache.get("redfish_raiz")
    if raiz is None:
        raise FalhaColeta("raiz Redfish não lida na detecção")
    return raiz


def _chassis(ctx: Contexto) -> list[tuple[str, dict[str, Any]]]:
    """``(caminho, recurso)`` de cada chassis (o do servidor e, às vezes, gabinetes/backplanes)."""
    return [(c, obter(ctx, c)) for c in _colecao(ctx, _raiz(ctx)["chassis"])]


def avaliar_saude(saude: str | None) -> Status | None:
    return SAUDE.get(saude or "")


def _rotulo_valor(item: dict[str, Any]) -> str:
    return "sem leitura" if item.get("leitura") is None else f"{item['leitura']:g} {item.get('unidade', '')}".strip()


def avaliar_leitura(item: dict[str, Any]) -> Status | None:
    """Saúde informada pelo BMC; sem ela, os limiares do próprio BMC. ``None`` = sem leitura."""
    if item.get("estado") in ESTADOS_SEM_LEITURA or item.get("leitura") is None:
        return None
    situacao = avaliar_saude(item.get("saude"))
    if situacao is not None:
        return situacao
    valor = item["leitura"]
    for chave, gravidade in (("limite_fatal", Status.FALHA), ("limite_critico", Status.FALHA), ("limite_alerta", Status.ALERTA)):
        if item.get(chave) is not None and valor >= item[chave]:
            return gravidade
    for chave, gravidade in (("limite_inferior_fatal", Status.FALHA), ("limite_inferior_critico", Status.FALHA),
                             ("limite_inferior_alerta", Status.ALERTA)):  # fmt: skip
        if item.get(chave) is not None and valor <= item[chave]:
            return gravidade
    return None


def _avaliar_itens(itens: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str], list[str], list[Status]]:
    lidos, ignorados, problemas, situacoes = [], [], [], []
    for item in itens:
        situacao = avaliar_leitura(item)
        if situacao is None:
            ignorados.append(f"{item['nome']}: {item.get('estado') or 'sem saúde nem limiar'} ({_rotulo_valor(item)})")
            continue
        item["avaliacao"] = situacao.value
        lidos.append(item)
        situacoes.append(situacao)
        if situacao != Status.OK:
            problemas.append(f"{item['nome']}: {_rotulo_valor(item)} (saúde {item.get('saude') or 'pelos limiares'})")
    return lidos, ignorados, problemas, situacoes


def _thermal_e_power(ctx: Contexto) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Recursos ``Thermal`` e ``Power`` (esquema clássico) de todos os chassis."""
    termicos, energia = [], []
    for _, chassis in _chassis(ctx):
        if link(chassis, "Thermal"):
            termicos.append(parse_thermal(obter(ctx, link(chassis, "Thermal"))))
        if link(chassis, "Power"):
            energia.append(parse_power(obter(ctx, link(chassis, "Power"))))
    return termicos, energia


# --- Sensores e ventoinhas ---------------------------------------------------------------------


class ProvedorRedfishSensores(_ProvedorRedfish):
    """Temperaturas (``Thermal``) e tensões (``Power``) com a saúde e os limiares do BMC."""

    nome = "Redfish (Thermal/Power)"

    def coletar(self, ctx: Contexto) -> Resultado | None:
        termicos, energia = _thermal_e_power(ctx)
        itens = [t for r in termicos for t in r["temperaturas"]] + [v for r in energia for v in r["tensoes"]]
        lidos, ignorados, problemas, situacoes = _avaliar_itens(itens)
        if not lidos:
            return Resultado("sensores", Status.NAO_VERIFICADO, mensagem="nenhum sensor com leitura no Redfish (Thermal/Power)")
        partes = [f"{len(lidos)} sensor(es) lido(s)"]
        temperaturas = [s for s in lidos if s["unidade"] == "°C"]
        if temperaturas:
            maior = max(temperaturas, key=lambda s: s["leitura"])
            partes.append(f"temperatura máxima {maior['leitura']:g} °C ({maior['nome']})")
        if ignorados:
            partes.append(f"{len(ignorados)} sem leitura ignorado(s)")
        return Resultado("sensores", pior_status(situacoes), "Redfish (Thermal/Power)",
                         {"sensores": lidos, "ignorados_sem_leitura": ignorados, "problemas": problemas},
                         "; ".join(partes))  # fmt: skip


class ProvedorRedfishVentoinhas(_ProvedorRedfish):
    """Ventoinhas e grupos de redundância de ventilação (``Thermal``)."""

    nome = "Redfish (Thermal/Fans)"

    def coletar(self, ctx: Contexto) -> Resultado | None:
        termicos, _ = _thermal_e_power(ctx)
        ventoinhas = [v for r in termicos for v in r["ventoinhas"]]
        lidos, ignorados, problemas, situacoes = _avaliar_itens(ventoinhas)
        if not lidos:
            return Resultado("ventoinhas", Status.NAO_VERIFICADO, mensagem="nenhuma ventoinha com leitura no Redfish")
        for grupo in (g for r in termicos for g in r["redundancia"]):
            situacao = avaliar_saude(grupo["saude"])
            if situacao not in (None, Status.OK):
                situacoes.append(situacao)
                problemas.append(f"{grupo['nome']}: redundância de ventilação com saúde {grupo['saude']}")
        mensagem = f"{len(lidos)} ventoinha(s) avaliada(s)" + (f"; {len(problemas)} problema(s)" if problemas else "")
        return Resultado("ventoinhas", pior_status(situacoes), "Redfish (Thermal/Fans)",
                         {"leituras": lidos, "ignoradas_sem_leitura": ignorados, "problemas": problemas}, mensagem)  # fmt: skip


# --- Fontes ------------------------------------------------------------------------------------


def _fontes(ctx: Contexto) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Fontes pelo ``Power`` (clássico) ou, se não houver, pelo ``PowerSubsystem`` (novo)."""
    fontes: list[dict[str, Any]] = []
    redundancia: list[dict[str, Any]] = []
    for _, chassis in _chassis(ctx):
        if link(chassis, "Power"):
            power = parse_power(obter(ctx, link(chassis, "Power")))
            fontes += power["fontes"]
            redundancia += power["redundancia"]
        elif link(chassis, "PowerSubsystem"):
            subsistema = obter(ctx, link(chassis, "PowerSubsystem"))
            for membro in _colecao(ctx, link(subsistema, "PowerSupplies")):
                fontes.append(parse_fonte_subsistema(obter(ctx, membro)))
    return fontes, redundancia


def classificar_fonte(fonte: dict[str, Any]) -> tuple[Status | None, str]:
    """``(status, categoria)``: ok, ausente, sem_energia, incerto; ``(None, "")`` sem saúde informada."""
    if fonte.get("estado") == "Absent":
        return Status.ALERTA, AUSENTE
    if fonte.get("entrada_status") in ENTRADA_SEM_ENERGIA:
        return Status.ALERTA, SEM_ENERGIA
    situacao = avaliar_saude(fonte.get("saude"))
    if situacao is None:
        return None, ""
    if situacao == Status.OK:
        return Status.OK, "ok"
    return situacao, "incerto"


def avaliar_fontes_redfish(fontes: list[dict[str, Any]], redundancia: list[dict[str, Any]], reserva: bool) -> Resultado:
    """Avalia as fontes. ``reserva=False``: devolve NAO_VERIFICADO se houver fonte não classificável."""
    avaliadas, ignoradas, problemas, situacoes = [], [], [], []
    categorias: set[str] = set()
    for fonte in fontes:
        situacao, categoria = classificar_fonte(fonte)
        if situacao is None:
            ignoradas.append(f"{fonte['nome']}: saúde não informada")
            continue
        fonte["avaliacao"], fonte["categorias"] = situacao.value, [categoria]
        avaliadas.append(fonte)
        situacoes.append(situacao)
        categorias.add(categoria)
        if categoria == AUSENTE:
            problemas.append(f"{fonte['nome']}: fonte ausente — redundância de energia perdida")
        elif categoria == SEM_ENERGIA:
            problemas.append(f"{fonte['nome']}: sem energia de entrada ({fonte['entrada_status']}) — redundância de energia perdida")
        elif categoria == "incerto":
            problemas.append(
                f"{fonte['nome']}: saúde {fonte['saude']} segundo o BMC — não foi possível distinguir defeito de falta de energia"
            )
    if not avaliadas:
        return Resultado("fontes", Status.NAO_VERIFICADO, mensagem="nenhuma fonte com saúde informada pelo Redfish")
    if "incerto" in categorias and not reserva:
        return Resultado("fontes", Status.NAO_VERIFICADO, mensagem=(
            "o Redfish indica fonte não saudável sem distinguir defeito de falta de energia; "
            "a classificação fica com o IPMI"))  # fmt: skip
    for grupo in redundancia:
        situacao = avaliar_saude(grupo["saude"])
        if situacao not in (None, Status.OK):
            situacoes.append(Status.ALERTA)
            problemas.append(f"{grupo['nome']}: redundância de energia com saúde {grupo['saude']}")
            categorias.add("redundancia")
    redundancia_perdida = bool(categorias & {AUSENTE, SEM_ENERGIA, "redundancia"})
    incerta = "incerto" in categorias
    mensagem = f"{len(avaliadas)} fonte(s) avaliada(s)"
    if incerta:
        mensagem += "; fonte com problema (defeito ou falta de energia: não classificado)"
    if redundancia_perdida:
        mensagem += "; redundância de energia perdida"
    return Resultado(
        "fontes",
        Status.ALERTA if incerta else pior_status(situacoes),
        "Redfish (Power)",
        {
            "leituras": avaliadas,
            "ignoradas_sem_leitura": ignoradas,
            "problemas": problemas,
            "defeito": False,
            "classificacao_incerta": incerta,
            "redundancia_perdida": redundancia_perdida,
            "explicacoes": [EXPLICACAO_REDUNDANCIA] if redundancia_perdida else [],
        },
        mensagem,
    )


class ProvedorRedfishFontes(_ProvedorRedfish):
    """Fontes de alimentação via ``Power``/``PowerSubsystem``."""

    nome = "Redfish (Power)"

    def coletar(self, ctx: Contexto) -> Resultado | None:
        return avaliar_fontes_redfish(*_fontes(ctx), reserva=False)


class ProvedorRedfishFontesReserva(ProvedorRedfishFontes):
    """Reserva (depois do IPMI): mostra o que o Redfish sabe, com a classificação incerta."""

    nome = "Redfish (Power, sem classificação de defeito)"
    prioridade = PRIORIDADE_RESERVA

    def coletar(self, ctx: Contexto) -> Resultado | None:
        return avaliar_fontes_redfish(*_fontes(ctx), reserva=True)


# --- Log do BMC --------------------------------------------------------------------------------


def _servico_de_log(ctx: Contexto) -> dict[str, Any] | None:
    servicos = []
    for gerenciador in _colecao(ctx, _raiz(ctx)["gerenciadores"]):
        for membro in _colecao(ctx, link(obter(ctx, gerenciador), "LogServices")):
            servicos.append(parse_servico_log(obter(ctx, membro)))
    for servico in servicos:
        if str(servico["id"]).lower() in IDS_LOG_PREFERIDOS or servico["tipo"] == "SEL":
            return servico
    return servicos[0] if servicos else None


def _entradas(ctx: Contexto, caminho: str) -> list[dict[str, Any]]:
    entradas: list[dict[str, Any]] = []
    pagina: str | None = caminho
    while pagina and len(entradas) < MAX_ENTRADAS_LOG:
        dados = obter(ctx, pagina)
        try:
            lote = parse_entradas_log(dados)
        except ErroParser as erro:
            raise FalhaColeta(f"{pagina}: {erro}") from erro
        if lote and not any(e["completa"] for e in lote):
            # Coleção só com links: lê as entradas uma a uma (as mais recentes, até o limite).
            lote = []
            for membro in membros(dados)[-MAX_ENTRADAS_AVULSAS:]:
                lote += parse_entradas_log({"Members": [obter(ctx, membro)]})
        entradas += lote
        pagina = proxima_pagina(dados)
    return entradas


def avaliar_log_redfish(entradas: list[dict[str, Any]], servico: str) -> Resultado:
    """Como o SEL pelo IPMI: histórico, então no máximo ALERTA."""
    problemas_entradas = [e for e in entradas if e["severidade"] in ("Warning", "Critical") and e["resolvida"] is not True]
    grupos: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for entrada in problemas_entradas:
        grupos.setdefault((entrada["severidade"], entrada["mensagem"]), []).append(entrada)
    problemas = []
    for (severidade, mensagem), repeticoes in grupos.items():
        ultimo = repeticoes[-1]
        texto = f"#{ultimo['id']} {ultimo['data']}: [{severidade}] {mensagem or ultimo['codigo']}"
        if len(repeticoes) > 1:
            texto += f" — {len(repeticoes)} ocorrências (mostrada a última)"
        problemas.append(texto)
    if not entradas:
        mensagem = f"log do BMC ({servico}) vazio"
    else:
        mensagem = f"{len(entradas)} evento(s) no log {servico}; {len(problemas_entradas)} indicam problema"
        if problemas_entradas:
            mensagem += " (histórico: podem já ter sido resolvidos)"
    return Resultado("log_bmc", Status.ALERTA if problemas else Status.OK, f"Redfish (LogServices/{servico})",
                     {"eventos": entradas, "eventos_problema": problemas_entradas, "problemas": problemas}, mensagem)  # fmt: skip


class ProvedorRedfishLogBMC(_ProvedorRedfish):
    """Log do BMC via ``Managers/<id>/LogServices`` (prefere o SEL)."""

    nome = "Redfish (LogServices)"

    def coletar(self, ctx: Contexto) -> Resultado | None:
        servico = _servico_de_log(ctx)
        if servico is None or not servico["entradas"]:
            return Resultado("log_bmc", Status.NAO_VERIFICADO, mensagem="o BMC não publica serviço de log no Redfish")
        resultado = avaliar_log_redfish(_entradas(ctx, servico["entradas"]), str(servico["id"] or servico["nome"]))
        return complementar_com_sel_ipmi(resultado, sel_disponivel(ctx))


def complementar_com_sel_ipmi(resultado: Resultado, sel: list[dict[str, str]] | None) -> Resultado:
    """Redfish com MENOS eventos que o SEL lido pelo IPMI: os mais antigos vêm do IPMI, e a mensagem diz que foi parcial.

    Bancada de 19/10/2026 (R620, iDRAC7 2.65.65.65): ``Managers/iDRAC.Embedded.1/Logs/Sel`` entregou só os 50 eventos mais
    recentes (IDs 1024 a 975) e o ``ipmitool sel elist`` os 1024; com credenciais, o log "verificado" perdia 974 eventos.
    O ``sel elist`` vem em ordem cronológica: os ``total - N`` primeiros são os que o Redfish não trouxe (um evento novo
    entre as duas leituras pode aparecer nas duas listas).
    """
    lidos = len(resultado.dados.get("eventos") or [])
    if not sel or len(sel) <= lidos:
        return resultado
    antigos = avaliar_log_bmc(sel[: len(sel) - lidos])
    extras = [f"{p} (evento mais antigo, só pelo IPMI)" for p in antigos.dados["problemas"]]
    resultado.dados["problemas"] = list(resultado.dados.get("problemas") or []) + extras
    resultado.dados["complemento_ipmi"] = {"eventos_redfish": lidos, "eventos_ipmi": len(sel), "problemas": extras}
    resultado.mensagem += (f"; o Redfish deste BMC entregou só os {lidos} evento(s) mais recentes de {len(sel)}: os "
                           f"{len(sel) - lidos} mais antigos foram lidos pelo IPMI ({len(extras)} com problema)")  # fmt: skip
    resultado.fonte += " + ipmitool sel elist"
    if extras:
        resultado.status = pior_status([resultado.status, Status.ALERTA])
    return resultado


# --- RAID / armazenamento ----------------------------------------------------------------------


def _armazenamento(ctx: Contexto) -> list[dict[str, Any]]:
    conjuntos = []
    for sistema in _colecao(ctx, _raiz(ctx)["sistemas"]):
        for membro in _colecao(ctx, link(obter(ctx, sistema), "Storage")):
            storage = parse_storage(obter(ctx, membro))
            if not storage["controladoras"] and storage["controladoras_link"]:
                storage["controladoras"] = [parse_controladora(obter(ctx, c)) for c in _colecao(ctx, storage["controladoras_link"])]
            storage["volumes_lidos"] = [parse_volume(obter(ctx, v)) for v in _colecao(ctx, storage["volumes"])]
            storage["discos_lidos"] = [parse_disco(obter(ctx, d)) for d in storage["discos"]]
            conjuntos.append(storage)
    return conjuntos


def avaliar_armazenamento(conjuntos: list[dict[str, Any]]) -> Resultado:
    """Controladoras, volumes (RAID) e discos físicos informados pelo BMC."""
    situacoes: list[Status] = []
    problemas: list[str] = []
    contagem = {"controladoras": 0, "volumes": 0, "discos": 0}

    def marcar(item: dict[str, Any], tipo: str, rotulo: str) -> None:
        if item.get("estado") == "Absent":
            return
        situacao = avaliar_saude(item.get("saude"))
        contagem[tipo] += 1
        if situacao is None:
            return
        item["avaliacao"] = situacao.value
        situacoes.append(situacao)
        if situacao != Status.OK:
            problemas.append(f"{rotulo}: saúde {item['saude']} segundo o BMC")

    for conjunto in conjuntos:
        for ctrl in conjunto["controladoras"]:
            marcar(ctrl, "controladoras", f"Controladora {ctrl['nome']}")
        for volume in conjunto["volumes_lidos"]:
            marcar(volume, "volumes", f"Volume {volume['nome']} ({volume['raid'] or 'RAID ?'})")
            for op in volume["operacoes"]:
                if any(p in str(op["nome"]).lower() for p in OPERACOES_RECONSTRUCAO):
                    situacoes.append(Status.ALERTA)
                    progresso = f" ({op['progresso']:g}%)" if op["progresso"] is not None else ""
                    problemas.append(f"Volume {volume['nome']}: {op['nome']} em andamento{progresso}")
        for disco in conjunto["discos_lidos"]:
            marcar(disco, "discos", f"Disco {disco['nome']}")
            if disco.get("falha_prevista"):
                situacoes.append(Status.FALHA)
                problemas.append(f"Disco {disco['nome']}: falha prevista (predictive failure)")
    if not sum(contagem.values()):
        return Resultado("raid", Status.NAO_VERIFICADO, mensagem="o Redfish não informa controladoras, volumes nem discos")
    mensagem = (f"{contagem['controladoras']} controladora(s), {contagem['volumes']} volume(s), "
                f"{contagem['discos']} disco(s) informados pelo BMC")  # fmt: skip
    if problemas:
        mensagem += f"; {len(problemas)} problema(s)"
    return Resultado("raid", pior_status(situacoes), "Redfish (Storage)",
                     {"armazenamento": conjuntos, "problemas": problemas}, mensagem)  # fmt: skip


class ProvedorRedfishArmazenamento(_ProvedorRedfish):
    """RAID de hardware pelo BMC: controladoras, volumes e discos (``Systems/<id>/Storage``)."""

    nome = "Redfish (Storage)"

    def coletar(self, ctx: Contexto) -> Resultado | None:
        return avaliar_armazenamento(_armazenamento(ctx))


# --- Placas de rede ----------------------------------------------------------------------------

LINK_ATIVO = {"LinkUp", "Up"}
LINK_INATIVO = {"LinkDown", "Down", "NoLink"}
MEIO_METALICO = "Electrical"


def _adaptadores_rede(ctx: Contexto) -> list[dict[str, Any]]:
    adaptadores = []
    for _, chassis in _chassis(ctx):
        for membro in _colecao(ctx, link(chassis, "NetworkAdapters")):
            adaptador = parse_adaptador_rede(obter(ctx, membro))
            adaptador["portas_lidas"] = [parse_porta_rede(obter(ctx, p)) for p in _colecao(ctx, adaptador["portas"])]
            adaptadores.append(adaptador)
    return adaptadores


def avaliar_adaptadores_rede(adaptadores: list[dict[str, Any]]) -> tuple[list[Status], list[str], list[str], dict[str, int]]:
    """``(situações, problemas, observações, contagem de portas por link)`` segundo o BMC."""
    situacoes: list[Status] = []
    problemas: list[str] = []
    observacoes: list[str] = []
    links = {"com_link": 0, "sem_link": 0, "nao_informado": 0}
    for adaptador in adaptadores:
        rotulo = " ".join(x for x in (adaptador["fabricante"], adaptador["modelo"]) if x) or adaptador["nome"]
        situacao = avaliar_saude(adaptador["saude"])
        if situacao is not None:
            situacoes.append(situacao)
            if situacao != Status.OK:
                problemas.append(f"{rotulo}: saúde {adaptador['saude']} segundo o BMC")
        for porta in adaptador["portas_lidas"]:
            nome = f"{rotulo} / {porta['nome']}"
            situacao = avaliar_saude(porta["saude"])
            if porta["link"] in LINK_INATIVO:
                links["sem_link"] += 1
                observacoes.append(f"{nome}: sem link")
                continue
            if situacao is not None:
                situacoes.append(situacao)
                if situacao != Status.OK:
                    problemas.append(f"{nome}: saúde {porta['saude']} segundo o BMC")
            links["com_link" if porta["link"] in LINK_ATIVO else "nao_informado"] += 1
            atual, maxima = porta["velocidade_mbps"], porta["velocidade_maxima_mbps"]
            if atual and maxima and atual < maxima:
                texto = f"{nome}: negociou {atual:g} Mb/s, a porta suporta {maxima:g} Mb/s"
                if porta["meio"] == MEIO_METALICO:
                    situacoes.append(Status.ALERTA)
                    problemas.append(texto + " (cabo ruim ou porta do switch limitada)")
                else:
                    observacoes.append(texto + " (óptica/SFP ou meio não informado: depende do módulo)")
    return situacoes, problemas, observacoes, links


class ProvedorRedfishPlacasRede(_ProvedorRedfish):
    """Placas de rede pelo BMC (``NetworkAdapters``: saúde, link, velocidade, firmware).

    O sistema operacional (``ip -s link`` + ``ethtool``) entra como COMPLEMENTO, não como
    alternativa: contadores de erro e half-duplex só ele vê.
    """

    nome = "Redfish (NetworkAdapters)"

    def coletar(self, ctx: Contexto) -> Resultado | None:
        from msscan.provedores.nic import ProvedorPlacasRede

        adaptadores = _adaptadores_rede(ctx)
        if not adaptadores:
            return Resultado("rede", Status.NAO_VERIFICADO, mensagem="o BMC não publica NetworkAdapters no Redfish")
        situacoes, problemas, observacoes, links = avaliar_adaptadores_rede(adaptadores)
        if not situacoes:
            return Resultado("rede", Status.NAO_VERIFICADO, mensagem="o Redfish não informa a saúde das placas de rede")
        portas = sum(len(a["portas_lidas"]) for a in adaptadores)
        mensagem = f"{len(adaptadores)} placa(s), {portas} porta(s) no BMC; {links['com_link']} com link"
        if links["nao_informado"]:
            mensagem += f", {links['nao_informado']} com link não informado pelo BMC"
        fonte = "Redfish (NetworkAdapters)"
        dados: dict[str, Any] = {"adaptadores": adaptadores, "problemas": problemas, "observacoes": observacoes}
        sistema = ProvedorPlacasRede()
        if sistema.disponivel(ctx).ok:
            try:
                complemento = sistema.coletar(ctx)
            except Exception as erro:  # noqa: BLE001 - o complemento nunca derruba o resultado do BMC
                dados["sistema_erro"] = f"{type(erro).__name__}: {erro}"
                complemento = None
            if complemento is not None and complemento.status != Status.NAO_VERIFICADO:
                situacoes.append(complemento.status)
                problemas += complemento.dados.get("problemas", [])
                observacoes += complemento.dados.get("observacoes", [])
                dados["sistema"] = complemento.dados
                fonte += " + ip -s link + ethtool"
                mensagem += f"; sistema: {complemento.mensagem}"
        return Resultado("rede", pior_status(situacoes), fonte, dados, mensagem)


def provedores_redfish() -> list[tuple[str, Provedor]]:
    """Pares ``(componente, provedor)`` para o registro."""
    return [
        ("sensores", ProvedorRedfishSensores()),
        ("fontes", ProvedorRedfishFontes()),
        ("fontes", ProvedorRedfishFontesReserva()),
        ("ventoinhas", ProvedorRedfishVentoinhas()),
        ("log_bmc", ProvedorRedfishLogBMC()),
        ("raid", ProvedorRedfishArmazenamento()),
        ("rede", ProvedorRedfishPlacasRede()),
    ]
