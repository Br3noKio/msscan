"""Provedores baseados em IPMI (``ipmitool``), via interface local do BMC."""

from __future__ import annotations

from typing import Any

from msscan.core.fallback import Disponibilidade, Provedor
from msscan.core.modelo import Resultado, Status, pior_status
from msscan.deteccao.contexto import Contexto
from msscan.historico import NOTA_DATAS, SUGESTAO_LIMPAR_SEL, classificar, resumo
from msscan.parsers.ipmitool import parse_sdr_tipo, parse_sensor
from msscan.provedores.comum import (
    SEM_LEITURA_IPMI,
    STATUS_LIMIAR_IPMI,
    descrever_evento_sel,
    evento_sel_e_problema,
    obter_sel,
    rodar_ou_falhar,
)

PRIORIDADE_IPMI = 20


class _ProvedorIPMI(Provedor):
    prioridade = PRIORIDADE_IPMI

    def disponivel(self, ctx: Contexto) -> Disponibilidade:
        if ctx.ipmi.disponivel:
            return Disponibilidade(True)
        return Disponibilidade(False, ctx.ipmi.motivo or "IPMI não disponível")


def _formatar_valor(valor: float | None, unidade: str) -> str:
    if valor is None:
        return "sem leitura"
    numero = f"{valor:g}"
    return f"{numero} {'°C' if unidade == 'degrees C' else unidade}".strip()


# --- Sensores ----------------------------------------------------------------


def avaliar_sensores(sensores: list[dict[str, Any]]) -> Resultado | None:
    """Avalia sensores analógicos do ``ipmitool sensor`` pelo estado informado pelo BMC.

    Sensores discretos ficam para fontes/ventoinhas; sensores sem leitura
    ("na") são ignorados e contados, nunca inventados.
    """
    analogicos = [s for s in sensores if not s["discreto"]]
    lidos = [s for s in analogicos if s["valor"] is not None and s["status"] not in SEM_LEITURA_IPMI]
    if not lidos:
        return None
    problemas: list[str] = []
    situacoes: list[Status] = []
    for sensor in lidos:
        situacao = STATUS_LIMIAR_IPMI.get(sensor["status"])
        if situacao is None:
            situacao = Status.ALERTA
            problemas.append(f"{sensor['nome']}: estado '{sensor['status']}' não reconhecido")
        elif situacao != Status.OK:
            problemas.append(
                f"{sensor['nome']}: {_formatar_valor(sensor['valor'], sensor['unidade'])} (estado {sensor['status']})"
            )
        sensor["avaliacao"] = situacao.value
        situacoes.append(situacao)

    temperaturas = [s for s in lidos if s["unidade"] == "degrees C"]
    partes = [f"{len(lidos)} sensor(es) lido(s)"]
    if temperaturas:
        maior = max(temperaturas, key=lambda s: s["valor"])
        partes.append(f"temperatura máxima {maior['valor']:g} °C ({maior['nome']})")
    ignorados = len(analogicos) - len(lidos)
    if ignorados:
        partes.append(f"{ignorados} sem leitura ignorado(s)")
    return Resultado(
        componente="sensores",
        status=pior_status(situacoes),
        fonte="ipmitool sensor",
        dados={"sensores": lidos, "ignorados_sem_leitura": ignorados, "problemas": problemas},
        mensagem="; ".join(partes),
    )


class ProvedorIPMISensores(_ProvedorIPMI):
    """Sensores com limiares (temperatura, tensão, corrente, consumo) via ``ipmitool sensor``."""

    nome = "IPMI (ipmitool sensor)"

    def coletar(self, ctx: Contexto) -> Resultado | None:
        return avaliar_sensores(parse_sensor(rodar_ou_falhar(ctx, ["ipmitool", "sensor"], timeout=120)))


# --- Fontes e ventoinhas (sdr type) -------------------------------------------

# Estados textuais (como o ipmitool escreve), verificados nesta ordem.
ESTADOS_FALHA = ("failure detected", "ac lost", "input lost", "out-of-range", "config error")
ESTADOS_ALERTA = (
    "predictive failure",
    "redundancy lost",
    "redundancy degraded",
    "non-redundant",
    "insufficient resources",
)
ESTADOS_OK = ("presence detected", "fully redundant")


def classificar_estado(estado: str) -> Status | None:
    """Classifica um estado textual do SDR; ``None`` se não reconhecido."""
    texto = estado.lower()
    if any(p in texto for p in ESTADOS_FALHA):
        return Status.FALHA
    if any(p in texto for p in ESTADOS_ALERTA):
        return Status.ALERTA
    if any(p in texto for p in ESTADOS_OK):
        return Status.OK
    return None


def avaliar_linha_sdr(linha: dict[str, Any]) -> tuple[Status | None, list[str]]:
    """Avalia uma linha do ``sdr type``: status da coluna e estados textuais.

    Devolve ``(None, [])`` para linhas sem leitura ("ns"). Estados textuais não
    reconhecidos viram ALERTA com explicação: nunca são tratados como OK.
    """
    if linha["status"] in SEM_LEITURA_IPMI:
        return None, []
    if linha["valor"] is None and not linha["estados"] and linha["status"] == "ok":
        return None, []
    rotulo = f"{linha['nome']} ({linha['entidade']})"
    situacoes: list[Status] = []
    problemas: list[str] = []
    coluna = STATUS_LIMIAR_IPMI.get(linha["status"])
    if coluna is None:
        situacoes.append(Status.ALERTA)
        problemas.append(f"{rotulo}: estado '{linha['status']}' não reconhecido")
    else:
        situacoes.append(coluna)
        if coluna != Status.OK:
            problemas.append(f"{rotulo}: {linha['leitura']} (estado {linha['status']})")
    for estado in linha["estados"]:
        situacao = classificar_estado(estado)
        if situacao is None:
            situacoes.append(Status.ALERTA)
            problemas.append(f"{rotulo}: estado '{estado}' não reconhecido")
        elif situacao != Status.OK:
            situacoes.append(situacao)
            problemas.append(f"{rotulo}: {estado}")
    return pior_status(situacoes), problemas


def avaliar_sdr(linhas: list[dict[str, Any]], componente: str, fonte: str, nome_item: str) -> Resultado | None:
    """Avalia todas as linhas de um ``sdr type``; ``None`` se nenhuma tiver leitura."""
    avaliadas, ignoradas = [], []
    problemas: list[str] = []
    situacoes: list[Status] = []
    for linha in linhas:
        situacao, probs = avaliar_linha_sdr(linha)
        if situacao is None:
            ignoradas.append(f"{linha['nome']} ({linha['entidade']}): {linha['leitura'] or 'sem leitura'}")
            continue
        linha["avaliacao"] = situacao.value
        avaliadas.append(linha)
        situacoes.append(situacao)
        problemas.extend(probs)
    if not avaliadas:
        return None
    mensagem = f"{len(avaliadas)} {nome_item} avaliado(s)"
    if problemas:
        mensagem += f"; {len(problemas)} problema(s)"
    return Resultado(
        componente=componente,
        status=pior_status(situacoes),
        fonte=fonte,
        dados={"leituras": avaliadas, "ignoradas_sem_leitura": ignoradas, "problemas": problemas},
        mensagem=mensagem,
    )


# --- Fontes ---------------------------------------------------------------------
#
# Duas situações bem diferentes (decisão do projeto):
# - DEFEITO real da fonte → FALHA (e bloqueia atualização de firmware);
# - fonte sem energia de entrada ou ausente → ALERTA "redundância de energia
#   perdida" (na atualização, gera aviso com confirmação extra, não bloqueia).

ESTADOS_DEFEITO_FONTE = ("failure detected", "predictive failure", "config error")
ESTADOS_SEM_ENERGIA_FONTE = ("ac lost", "input lost", "out-of-range")
ESTADOS_REDUNDANCIA = ("redundancy lost", "redundancy degraded", "non-redundant", "insufficient resources")
ENTIDADE_FONTE = "10."  # Entity ID IPMI 10 = Power Supply

DEFEITO = "defeito"
SEM_ENERGIA = "sem_energia"
AUSENTE = "ausente"
REDUNDANCIA = "redundancia"
DESCONHECIDO = "desconhecido"

EXPLICACAO_REDUNDANCIA = (
    "Redundância de energia perdida: há fonte sem energia de entrada ou ausente. O servidor continua "
    "operando pela(s) outra(s) fonte(s), mas uma nova falha de energia o desligaria. Verifique cabo, "
    "tomada e circuito da fonte indicada (ou instale a fonte, se a redundância for necessária)."
)


def classificar_estado_fonte(estado: str) -> tuple[Status | None, str]:
    """Classifica um estado textual de fonte em ``(status, categoria)``.

    Categorias: defeito, sem_energia, redundancia, ok ou desconhecido
    (status ``None``).
    """
    texto = estado.lower()
    if any(p in texto for p in ESTADOS_DEFEITO_FONTE):
        return Status.FALHA, DEFEITO
    if any(p in texto for p in ESTADOS_SEM_ENERGIA_FONTE):
        return Status.ALERTA, SEM_ENERGIA
    if any(p in texto for p in ESTADOS_REDUNDANCIA):
        return Status.ALERTA, REDUNDANCIA
    if any(p in texto for p in ESTADOS_OK):
        return Status.OK, "ok"
    return None, DESCONHECIDO


def avaliar_linha_fonte(linha: dict[str, Any]) -> tuple[Status | None, list[str], set[str]]:
    """Avalia uma linha do ``sdr type "Power Supply"``: ``(status, problemas, categorias)``.

    Uma linha de fonte (entidade 10.x) com estados mas sem "Presence detected"
    é tratada como fonte ausente.

    TODO(fixture): precisa de saída real de ``ipmitool sdr type "Power Supply"``
    com uma fonte fisicamente removida e com uma fonte com defeito.
    """
    if linha["status"] in SEM_LEITURA_IPMI:
        return None, [], set()
    if linha["valor"] is None and not linha["estados"] and linha["status"] == "ok":
        return None, [], set()
    rotulo = f"{linha['nome']} ({linha['entidade']})"
    situacoes: list[Status] = []
    problemas: list[str] = []
    categorias: set[str] = set()
    coluna = STATUS_LIMIAR_IPMI.get(linha["status"])
    if coluna is None:
        situacoes.append(Status.ALERTA)
        categorias.add(DESCONHECIDO)
        problemas.append(f"{rotulo}: estado '{linha['status']}' não reconhecido")
    elif coluna != Status.OK:
        situacoes.append(coluna)
        categorias.add(DEFEITO if coluna == Status.FALHA else DESCONHECIDO)
        problemas.append(f"{rotulo}: {linha['leitura']} (estado {linha['status']})")
    for estado in linha["estados"]:
        situacao, categoria = classificar_estado_fonte(estado)
        if situacao is None:
            situacoes.append(Status.ALERTA)
            categorias.add(DESCONHECIDO)
            problemas.append(f"{rotulo}: estado '{estado}' não reconhecido")
        elif situacao != Status.OK:
            situacoes.append(situacao)
            categorias.add(categoria)
            sufixo = {
                DEFEITO: " — defeito na fonte",
                SEM_ENERGIA: " — sem energia de entrada (redundância de energia perdida)",
                REDUNDANCIA: " — redundância de energia perdida",
            }[categoria]
            problemas.append(f"{rotulo}: {estado}{sufixo}")
    presente = any("presence detected" in e.lower() for e in linha["estados"])
    if linha["entidade"].startswith(ENTIDADE_FONTE) and linha["estados"] and not presente and not categorias & {DEFEITO, SEM_ENERGIA}:
        situacoes.append(Status.ALERTA)
        categorias.add(AUSENTE)
        problemas.append(f"{rotulo}: fonte não detectada (ausente) — redundância de energia perdida")
    return pior_status(situacoes), problemas, categorias


def avaliar_fontes(linhas: list[dict[str, Any]]) -> Resultado | None:
    """Avalia as fontes, separando defeito real (FALHA) de redundância perdida (ALERTA).

    ``dados["defeito"]`` e ``dados["redundancia_perdida"]`` são usados pela
    pré-checagem de atualização de firmware.
    """
    avaliadas, ignoradas = [], []
    problemas: list[str] = []
    situacoes: list[Status] = []
    categorias: set[str] = set()
    for linha in linhas:
        situacao, probs, cats = avaliar_linha_fonte(linha)
        if situacao is None:
            ignoradas.append(f"{linha['nome']} ({linha['entidade']}): {linha['leitura'] or 'sem leitura'}")
            continue
        linha["avaliacao"] = situacao.value
        linha["categorias"] = sorted(cats)
        avaliadas.append(linha)
        situacoes.append(situacao)
        problemas.extend(probs)
        categorias |= cats
    if not avaliadas:
        return None
    defeito = DEFEITO in categorias
    redundancia_perdida = bool(categorias & {SEM_ENERGIA, AUSENTE, REDUNDANCIA})
    fontes = sum(l["entidade"].startswith(ENTIDADE_FONTE) for l in avaliadas)
    mensagem = f"{fontes} fonte(s) avaliada(s)"
    if defeito:
        mensagem += "; DEFEITO em fonte de alimentação"
    if redundancia_perdida:
        mensagem += "; redundância de energia perdida"
    return Resultado(
        componente="fontes",
        status=pior_status(situacoes),
        fonte="ipmitool sdr type Power Supply",
        dados={
            "leituras": avaliadas,
            "ignoradas_sem_leitura": ignoradas,
            "problemas": problemas,
            "defeito": defeito,
            "redundancia_perdida": redundancia_perdida,
            "explicacoes": [EXPLICACAO_REDUNDANCIA] if redundancia_perdida else [],
        },
        mensagem=mensagem,
    )


class ProvedorIPMIFontes(_ProvedorIPMI):
    """Fontes de alimentação via ``ipmitool sdr type "Power Supply"``.

    O estado textual é decisivo: no Dell R640 real, uma fonte sem energia
    aparece com status "ok" e estado "Power Supply AC lost".
    """

    nome = "IPMI (ipmitool sdr type Power Supply)"

    def coletar(self, ctx: Contexto) -> Resultado | None:
        saida = rodar_ou_falhar(ctx, ["ipmitool", "sdr", "type", "Power Supply"], timeout=60)
        return avaliar_fontes(parse_sdr_tipo(saida))


class ProvedorIPMIVentoinhas(_ProvedorIPMI):
    """Ventoinhas via ``ipmitool sdr type Fan``."""

    nome = "IPMI (ipmitool sdr type Fan)"

    def coletar(self, ctx: Contexto) -> Resultado | None:
        saida = rodar_ou_falhar(ctx, ["ipmitool", "sdr", "type", "Fan"], timeout=60)
        return avaliar_sdr(parse_sdr_tipo(saida), "ventoinhas", "ipmitool sdr type Fan", "sensor(es) de ventoinha")


# --- Log do BMC ----------------------------------------------------------------


def avaliar_log_bmc(eventos: list[dict[str, str]]) -> Resultado:
    """Avalia o SEL: ALERTA se houver eventos de problema.

    O SEL é histórico (os eventos podem já ter sido resolvidos) e o relógio do
    BMC pode estar errado, por isso nunca gera FALHA sozinho.
    """
    eventos_problema = [e for e in eventos if evento_sel_e_problema(e)]
    # Agrupa eventos repetidos (mesmo sensor, descrição e direção), guardando o último.
    grupos: dict[tuple[str, str, str], list[dict[str, str]]] = {}
    for evento in eventos_problema:
        grupos.setdefault((evento["sensor"], evento["evento"], evento["direcao"]), []).append(evento)
    problemas = []
    for repeticoes in grupos.values():
        ultimo = repeticoes[-1]
        texto = descrever_evento_sel(ultimo)
        if len(repeticoes) > 1:
            texto += f" — {len(repeticoes)} ocorrências (mostrada a última)"
        problemas.append(texto)
    if not eventos:
        mensagem = "log do BMC vazio"
    else:
        mensagem = f"{len(eventos)} evento(s) no log; {len(eventos_problema)} indicam problema"
        if eventos_problema:
            mensagem += " (histórico: podem já ter sido resolvidos)"
    return Resultado(
        componente="log_bmc",
        status=Status.ALERTA if problemas else Status.OK,
        fonte="ipmitool sel elist",
        dados={"eventos": eventos, "eventos_problema": eventos_problema, "problemas": problemas},
        mensagem=mensagem,
    )


class ProvedorIPMILogBMC(_ProvedorIPMI):
    """Log de eventos do BMC (SEL) via ``ipmitool sel elist``."""

    nome = "IPMI (ipmitool sel elist)"

    def coletar(self, ctx: Contexto) -> Resultado | None:
        return avaliar_log_bmc(obter_sel(ctx))


# --- Componentes internos (CLAUDE.md 15.3): backplane, cabos, slots/risers, controladoras ---------------------------

TIPOS_SEL_INTERNOS = ("Cable / Interconnect", "Drive Slot", "Slot / Connector", "Add-in Card", "Module / Board",
                      "Critical Interrupt")  # fmt: skip
"""Tipos de sensor IPMI dos eventos de peças internas (nomes do ``ipmitool``; R640 real: "Cable / Interconnect Cable
SAS A0" Config Error, "Critical Interrupt PCIE Fatal Err" Bus Fatal Error (Slot 1))."""


def avaliar_internos(eventos: list[dict[str, str]]) -> Resultado:
    """Problemas registrados pelo BMC em backplane, cabos, slots/risers PCIe e controladoras, com o local físico.

    O SEL é histórico: ALERTA (nunca FALHA sozinho), como no log do BMC. Sem evento: OK, dizendo que a fonte é o
    registro do BMC (o BMC monitora cabos, backplane e slots e registra os erros).
    """
    from msscan.localizacao import local_de

    internos = [e for e in eventos if evento_sel_e_problema(e) and any((e.get("tipo") or "").startswith(t) for t in TIPOS_SEL_INTERNOS)]
    grupos: dict[tuple[str, str], list[dict[str, str]]] = {}
    for evento in internos:
        grupos.setdefault((evento["sensor"], evento["evento"]), []).append(evento)
    pecas, problemas = [], []
    for repeticoes in grupos.values():
        ultimo = repeticoes[-1]
        achado = local_de(f"{ultimo['sensor']} {ultimo['evento']}")
        local = achado[1] if achado else "não foi possível localizar"
        texto = descrever_evento_sel(ultimo) + (f" — {len(repeticoes)} ocorrências (mostrada a última)" if len(repeticoes) > 1 else "")
        problemas.append(f"{local}: {texto} (histórico — só no log do BMC)")
        pecas.append({"nome": local, "sensor": ultimo["sensor"], "evento": ultimo["evento"], "ocorrencias": len(repeticoes),
                      "ultima": f"{ultimo.get('data', '')} {ultimo.get('hora', '')}".strip()})  # fmt: skip
    mensagem = (f"{len(pecas)} peça(s) interna(s) com erro registrado pelo BMC (histórico: podem já ter sido resolvidos)" if pecas
                else "o BMC não registrou erro em backplane, cabos, slots/risers PCIe ou controladoras")  # fmt: skip
    dados: dict[str, Any] = {"pecas": pecas, "eventos_problema": internos, "problemas": problemas}
    if internos:
        # Só o log fala destas peças (sem leitura ao vivo de cabos/slots): tudo é HISTÓRICO (msscan/historico.py).
        classificados = [classificar(e, None, None) for e in internos]
        dados.update(eventos_sel=classificados, eventos_sel_resumo=resumo(classificados),
                     explicacoes=[f"Erros de peças internas vêm só do log do BMC ({NOTA_DATAS}). {SUGESTAO_LIMPAR_SEL}"])
    return Resultado(componente="internos", status=Status.ALERTA if pecas else Status.OK, fonte="ipmitool sel elist",
                     dados=dados, mensagem=mensagem)  # fmt: skip


class ProvedorIPMIInternos(_ProvedorIPMI):
    """Componentes internos pelo log do BMC (SEL)."""

    nome = "IPMI (ipmitool sel elist)"

    def coletar(self, ctx: Contexto) -> Resultado | None:
        return avaliar_internos(obter_sel(ctx))
