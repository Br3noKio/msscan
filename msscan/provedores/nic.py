"""Provedor do componente "Placas de rede": ``ip -j -s link`` + ``ethtool``.

Avalia só portas físicas (ignora interfaces virtuais e a NIC USB do BMC).
Porta sem cabo é informativa: não é defeito, mas também não é verificável.
Os contadores de erro valem desde a inicialização do sistema.
"""

from __future__ import annotations

from typing import Any

from msscan.core.fallback import Disponibilidade, Provedor
from msscan.core.modelo import Resultado, Status, pior_status
from msscan.deteccao.contexto import Contexto
from msscan.parsers import ErroParser
from msscan.parsers.ethtool import parse_ethtool, parse_ethtool_i
from msscan.parsers.iproute import parse_ip_link_estatisticas
from msscan.rede.verificacao import listar_interfaces

PORTA_METALICA = "Twisted Pair"


def avaliar_porta(porta: dict[str, Any]) -> tuple[Status | None, list[str], list[str]]:
    """Avalia uma porta: ``(status, problemas, observacoes)``.

    Status ``None``: porta sem cabo ou sem dados suficientes (não conta como verificada).
    """
    nome = porta["nome"]
    ethtool = porta.get("ethtool") or {}
    link = ethtool.get("link") if ethtool.get("link") is not None else porta.get("link")
    if not link:
        return None, [], [f"{nome}: sem cabo/link"]
    if not ethtool and not porta.get("estatisticas"):
        return None, [], [f"{nome}: link ativo, mas sem dados do ethtool nem contadores: não verificada"]
    problemas, observacoes = [], []
    situacoes = [Status.OK]
    if ethtool.get("duplex") == "Half":
        situacoes.append(Status.ALERTA)
        problemas.append(f"{nome}: link em half-duplex (verifique cabo e configuração da porta do switch)")
    velocidade, maxima = ethtool.get("velocidade_mbps"), ethtool.get("velocidade_maxima_mbps")
    if velocidade and maxima and velocidade < maxima:
        texto = f"{nome}: negociou {velocidade} Mb/s, a porta suporta {maxima} Mb/s"
        if ethtool.get("porta") == PORTA_METALICA:
            situacoes.append(Status.ALERTA)
            problemas.append(texto + " (cabo ruim ou porta do switch limitada)")
        else:
            # Em portas SFP a velocidade depende do módulo instalado.
            observacoes.append(texto + " (porta óptica/SFP: depende do módulo)")
    estatisticas = porta.get("estatisticas") or {}
    rx, tx = estatisticas.get("rx", {}), estatisticas.get("tx", {})
    for rotulo, valor in (
        ("erros de recepção", rx.get("errors")),
        ("erros de transmissão", tx.get("errors")),
        ("erros de portadora (carrier)", tx.get("carrier_errors")),
    ):
        if isinstance(valor, int) and valor > 0:
            situacoes.append(Status.ALERTA)
            problemas.append(f"{nome}: {valor} {rotulo} desde a inicialização")
    descartados = (rx.get("dropped") or 0) + (tx.get("dropped") or 0)
    if descartados:
        observacoes.append(f"{nome}: {descartados} pacote(s) descartado(s) (comum; informativo)")
    return pior_status(situacoes), problemas, observacoes


def avaliar_placas(portas: list[dict[str, Any]], ignoradas: list[str]) -> Resultado | None:
    """Consolida as portas físicas no componente "rede"."""
    if not portas:
        return None
    problemas, observacoes = [], []
    situacoes: list[Status] = []
    com_link = []
    for porta in portas:
        situacao, probs, obs = avaliar_porta(porta)
        porta["avaliacao"] = situacao.value if situacao else "nao_verificada"
        problemas += probs
        observacoes += obs
        if situacao is not None:
            situacoes.append(situacao)
            eth = porta.get("ethtool") or {}
            com_link.append(f"{porta['nome']} {eth.get('velocidade_mbps') or '?'} Mb/s {eth.get('duplex') or ''}".strip())
    dados = {"portas": portas, "ignoradas": ignoradas, "problemas": problemas, "observacoes": observacoes}
    mensagem = f"{len(portas)} porta(s) física(s); {len(com_link)} com link"
    if com_link:
        mensagem += f" ({', '.join(com_link)})"
    if not situacoes:
        return Resultado(
            componente="rede",
            status=Status.NAO_VERIFICADO,
            dados=dados,
            mensagem=f"{mensagem}: nenhuma porta com dados suficientes (sem link ou sem ethtool/contadores)",
        )
    return Resultado(componente="rede", status=pior_status(situacoes), fonte="ip -s link + ethtool", dados=dados, mensagem=mensagem)


class ProvedorPlacasRede(Provedor):
    """Placas de rede físicas: link, velocidade, duplex, erros e firmware."""

    nome = "sistema (ip, ethtool)"
    prioridade = 50

    def disponivel(self, ctx: Contexto) -> Disponibilidade:
        if ctx.executor.tem_comando("ip"):
            return Disponibilidade(True)
        return Disponibilidade(False, "iproute2 (ip) não instalado")

    def coletar(self, ctx: Contexto) -> Resultado | None:
        ex = ctx.executor
        interfaces = listar_interfaces(ex)
        ignoradas = [f"{i.nome}: NIC USB do BMC" for i in interfaces if i.nic_bmc]
        ignoradas += [f"{i.nome}: interface virtual" for i in interfaces if not i.fisica]
        rc, saida, _ = ex.rodar(["ip", "-j", "-s", "link", "show"], timeout=10)
        try:
            estatisticas = parse_ip_link_estatisticas(saida) if rc == 0 else {}
        except ErroParser:
            estatisticas = {}
        tem_ethtool = ex.tem_comando("ethtool")
        portas = []
        for interface in (i for i in interfaces if i.candidata):
            porta: dict[str, Any] = {
                "nome": interface.nome,
                "driver": interface.driver,
                "link": interface.link,
                "estatisticas": estatisticas.get(interface.nome),
                "ethtool": None,
                "firmware": None,
            }
            if tem_ethtool:
                for comando, chave, parser in (
                    (["ethtool", interface.nome], "ethtool", parse_ethtool),
                    (["ethtool", "-i", interface.nome], "firmware", parse_ethtool_i),
                ):
                    rc_eth, saida_eth, _ = ex.rodar(comando, timeout=15)
                    if rc_eth == 0:
                        try:
                            porta[chave] = parser(saida_eth)
                        except ErroParser:
                            pass
            portas.append(porta)
        return avaliar_placas(portas, ignoradas)
