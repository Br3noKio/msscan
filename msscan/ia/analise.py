"""Análise por IA (opcional, padrão NÃO): interpretação do diagnóstico e plano de solução, nunca decisão.

Regras (CLAUDE.md 10 e 10b): só os dados fornecidos; NAO_VERIFICADO nunca é "saudável"; a IA não altera
status, não executa comandos e não decide atualizações; cada problema cita evidências dos dados
(evidência que não existe → "sugestão da IA não confirmada pelos dados"); o plano de solução pode
RECOMENDAR uma ação do catálogo fechado do MSscan pelo identificador (identificador fora do catálogo
é ignorado e sinalizado). Provedores em ``msscan/ia/provedores.py``, com fallback automático
(429, erro ou timeout → próximo). Qualquer falha de todos → relatório sem a seção de IA.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from msscan.acoes.catalogo import acao, descrever_para_ia
from msscan.core.executor import Executor
from msscan.ia.sanitizar import compactar
from msscan.ia.provedores import DADOS_GRANDES, SEM_REDE, Provedor, carregar_provedores, montar, pedir_json

TIMEOUT_S = 300
SEVERIDADES = ["critica", "alta", "media", "baixa"]
PRIORIDADES = ["imediata", "alta", "media", "baixa"]
NAO_CONFIRMADA = "sugestão da IA não confirmada pelos dados"
AVISO_TREINAMENTO = (
    "Planos gratuitos de IA (como o Gemini gratuito do Google AI Studio) podem usar os dados enviados para "
    "treinar os modelos. O MSscan remove serial, service tag, UUID, asset tag, MAC, IP, hostname e WWN "
    "antes do envio; mesmo assim, confira a prévia e só envie se a política da empresa permitir."
)

_TEXTO = {"type": "string"}
ESQUEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["resumo", "problemas", "limitacoes"],
    "properties": {
        "resumo": _TEXTO,
        "problemas": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["titulo", "severidade", "evidencias", "explicacao", "acao_recomendada", "plano"],
                "properties": {
                    "titulo": _TEXTO,
                    "severidade": {"type": "string", "enum": SEVERIDADES},
                    "evidencias": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["componente", "trecho"],
                            "properties": {"componente": _TEXTO, "trecho": _TEXTO},
                        },
                    },
                    "explicacao": _TEXTO,
                    "acao_recomendada": _TEXTO,
                    "plano": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["passos", "prioridade", "intervencao_fisica", "troca_de_peca", "acao_catalogo"],
                        "properties": {
                            "passos": {"type": "array", "items": _TEXTO},
                            "prioridade": {"type": "string", "enum": PRIORIDADES},
                            "intervencao_fisica": {"type": "boolean"},
                            "troca_de_peca": {"type": "boolean"},
                            "acao_catalogo": _TEXTO,
                        },
                    },
                },
            },
        },
        "limitacoes": {"type": "array", "items": _TEXTO},
    },
}

INSTRUCOES = f"""\
Você interpreta o resultado de um diagnóstico de servidor físico feito pelo MSscan, para um técnico \
de campo. Responda em português do Brasil, em linguagem direta.

Regras:
1. Use SOMENTE os dados fornecidos. Não presuma nada sobre o equipamento além deles.
2. O status de cada componente foi apurado pelas ferramentas e é definitivo: não o altere, não o \
conteste e não o reclassifique. Você explica e prioriza; não diagnostica de novo.
3. NAO_VERIFICADO significa que não foi possível verificar — nunca trate como saudável. Diga o que ficou \
sem verificação em "limitacoes".
4. Cada problema precisa de evidências: para cada uma, "componente" é o identificador do componente \
(campo "componente" dos dados, ex.: "fontes") e "trecho" é um texto copiado LITERALMENTE dos dados desse \
componente (da mensagem ou de um valor em "dados"). Sem evidência nos dados, não aponte o problema.
5. Para cada problema, monte um "plano" de solução para o técnico: "passos" (passo a passo curto e \
concreto), "prioridade" (imediata, alta, media, baixa), "intervencao_fisica" (true se exige ir até o \
servidor: cabos, peças, LEDs) e "troca_de_peca" (true se a solução provável é trocar uma peça).
6. Você NÃO gera comandos e não executa nada. Se, e somente se, uma ação do catálogo abaixo resolver o \
problema, informe o identificador dela em "acao_catalogo"; senão, deixe "acao_catalogo" vazio (""). \
Nunca invente identificadores. Catálogo de ações do MSscan:
{descrever_para_ia()}
7. Você não decide atualizações de firmware: elas são decididas pelo técnico, em outra etapa.
8. Eventos do log do BMC são histórico: podem já ter sido resolvidos; diga isso quando for o caso.
9. Identificadores foram trocados por marcadores como [SERIAL-1] e [MAC-2]; use-os como estão.
10. Severidade: critica (risco imediato de parada ou perda de dados), alta, media, baixa.
11. Se não houver problema, devolva "problemas" vazio e diga isso no resumo.
"""


@dataclass
class ResultadoIA:
    """Resultado de uma solicitação. ``analise`` só existe com ``ok``; ``tentativas`` sempre (sem chaves)."""

    ok: bool
    mensagem: str
    analise: dict[str, Any] | None = None
    uso: dict[str, Any] = field(default_factory=dict)
    tentativas: list[dict[str, str]] = field(default_factory=list)


def motivo_contexto(rede_ok: bool, sem_rede_por_parametro: bool, sem_ia: bool, replay: bool) -> str:
    """``""`` se o contexto permite IA; senão, o motivo (independe de haver chave configurada)."""
    if sem_ia:
        return "análise por IA desativada (--sem-ia)"
    if sem_rede_por_parametro:
        return "análise por IA desativada pelo parâmetro de boot msscan.semrede=1"
    if replay:
        return "análise por IA indisponível no modo replay"
    if not rede_ok:
        return "análise por IA indisponível: sem acesso à internet"
    return ""


def disponibilidade(pasta_dados: str | None, rede_ok: bool, sem_rede_por_parametro: bool, sem_ia: bool, replay: bool) -> str:
    """``""`` se a IA pode ser oferecida; senão, o motivo (mostrado ao técnico)."""
    motivo = motivo_contexto(rede_ok, sem_rede_por_parametro, sem_ia, replay)
    if motivo:
        return motivo
    provedores, avisos = carregar_provedores(pasta_dados)
    if not provedores:
        local = f"{pasta_dados}/config" if pasta_dados else "config"
        detalhe = f" ({'; '.join(avisos)})" if avisos else ""
        return (f"análise por IA indisponível: nenhuma IA com chave — escolha a IA e informe a chave na tela "
                f"\"Configurar IA\" ou em {local}/ia.conf{detalhe}")  # fmt: skip
    return ""


def montar_mensagem(dados: dict[str, Any]) -> str:
    return "Dados do diagnóstico (JSON):\n" + json.dumps(dados, ensure_ascii=False, indent=1, default=str)


def _achatado(valor: Any) -> str:
    """Todos os textos e números de uma estrutura, para procurar evidências."""
    if isinstance(valor, dict):
        return " | ".join(_achatado(v) for v in valor.values())
    if isinstance(valor, list):
        return " | ".join(_achatado(v) for v in valor)
    return "" if valor is None else str(valor)


def _normalizar(texto: str) -> str:
    return " ".join(texto.lower().split())


def _plano(bruto: Any) -> tuple[dict[str, Any], str]:
    """Plano normalizado e, se a ação recomendada não existir no catálogo, o aviso."""
    plano = bruto if isinstance(bruto, dict) else {}
    passos = [str(p) for p in plano.get("passos") or [] if isinstance(p, (str, int, float)) and str(p).strip()]
    identificador = str(plano.get("acao_catalogo") or "").strip()
    aviso = ""
    entrada = acao(identificador) if identificador else None
    if identificador and entrada is None:
        aviso = f"A IA recomendou a ação '{identificador}', que não existe no catálogo do MSscan: ignorada."
    booleano = lambda v: v if isinstance(v, bool) else None  # noqa: E731
    return {
        "passos": passos,
        "prioridade": plano.get("prioridade") if plano.get("prioridade") in PRIORIDADES else "media",
        "intervencao_fisica": booleano(plano.get("intervencao_fisica")),
        "troca_de_peca": booleano(plano.get("troca_de_peca")),
        "acao_catalogo": identificador if entrada else None,
        "acao": {"id": identificador, "titulo": entrada["titulo"], "situacao": entrada["situacao"]} if entrada else None,
        "acao_ignorada": identificador if aviso else None,
    }, aviso


def textos_evidencia(dados: dict[str, Any]) -> dict[str, str]:
    """Texto normalizado de cada componente (e do servidor), onde uma evidência citada pela IA precisa estar."""
    textos = {
        str(c.get("componente")): _normalizar(_achatado(c)) for c in dados.get("componentes") or [] if isinstance(c, dict)
    }
    textos["servidor"] = _normalizar(_achatado(dados.get("servidor")))
    return textos


def evidencia_confirmada(evidencia: dict[str, Any], textos: dict[str, str]) -> bool:
    """A evidência existe nos dados: trecho (≥ 3 caracteres) literalmente nos dados do componente citado."""
    trecho = _normalizar(str(evidencia.get("trecho") or ""))
    return len(trecho) >= 3 and trecho in textos.get(str(evidencia.get("componente") or ""), "")


def validar_analise(analise: Any, dados: dict[str, Any]) -> dict[str, Any]:
    """Confere estrutura, evidências e ações do catálogo. Nunca altera status: só anota."""
    if not isinstance(analise, dict) or not isinstance(analise.get("problemas"), list):
        raise ValueError("resposta da IA fora do formato esperado")
    textos = textos_evidencia(dados)
    problemas, acoes_ignoradas = [], []
    for problema in analise["problemas"]:
        if not isinstance(problema, dict):
            continue
        evidencias = []
        for evidencia in problema.get("evidencias") or []:
            if not isinstance(evidencia, dict):
                continue
            evidencias.append({**evidencia, "confirmada": evidencia_confirmada(evidencia, textos)})
        confirmado = any(e["confirmada"] for e in evidencias)
        plano, aviso_acao = _plano(problema.get("plano"))
        if aviso_acao:
            acoes_ignoradas.append(plano["acao_ignorada"])
        problemas.append({
            **{k: v for k, v in problema.items() if k != "plano"},
            "severidade": problema.get("severidade") if problema.get("severidade") in SEVERIDADES else "media",
            "evidencias": evidencias,
            "confirmado": confirmado,
            "aviso": "" if confirmado else NAO_CONFIRMADA,
            "plano": plano,
            "aviso_acao": aviso_acao,
        })  # fmt: skip
    return {
        "resumo": str(analise.get("resumo") or ""),
        "problemas": problemas,
        "limitacoes": [str(l) for l in analise.get("limitacoes") or [] if isinstance(l, (str, int, float))],
        "acoes_ignoradas": acoes_ignoradas,
    }


def _tentar(executor: Executor, provedor: Provedor, dados: dict[str, Any], avisar: Any = None,
            ao_parcial: Any = None) -> tuple[dict[str, Any] | None, str, str, dict[str, Any], str, float]:  # fmt: skip
    """Uma tentativa num provedor: ``(análise, mensagem, modelo, uso, situação, segundos)``."""
    mensagem = montar_mensagem(dados)
    resultado, texto = pedir_json(executor, provedor, lambda p, simples: montar(p, INSTRUCOES, mensagem, ESQUEMA, simples=simples),
                                  TIMEOUT_S, avisar, ao_parcial=ao_parcial)  # fmt: skip
    if not resultado.ok:
        return None, resultado.mensagem, resultado.modelo, resultado.uso, resultado.situacao or "erro", resultado.duracao_s
    try:
        return validar_analise(json.loads(texto), dados), "", resultado.modelo, resultado.uso, "ok", resultado.duracao_s
    except (ValueError, TypeError) as erro:
        # A chave funcionou (HTTP 200); o conteúdo é que não serviu.
        return None, f"resposta inválida: {erro}", resultado.modelo, resultado.uso, "ok", resultado.duracao_s


def cancelado(executor: Any) -> bool:
    """O técnico clicou em "Cancelar envio" (17.5): nada de nova tentativa nem de IA reserva."""
    evento = getattr(executor, "ia_cancelada", None)
    return bool(evento is not None and evento.is_set())


def solicitar_analise(executor: Executor, provedores: list[Provedor], dados: dict[str, Any], avisar: Any = None,
                      ao_parcial: Any = None) -> ResultadoIA:  # fmt: skip
    """Envia os dados JÁ SANITIZADOS, provedor a provedor, até um responder. Nunca levanta exceção.

    ``ao_parcial(texto)``: streaming — resumo e títulos já escritos pela IA, para a tela (``fluxo.texto_parcial_analise``).
    """
    from msscan.ia.fluxo import texto_parcial_analise

    tentativas: list[dict[str, str]] = []
    for provedor in provedores:
        parcial = (lambda bruto: ao_parcial(texto_parcial_analise(bruto))) if ao_parcial is not None else None
        try:
            analise, mensagem, modelo, uso, situacao, segundos = _tentar(executor, provedor, dados, avisar, parcial)
        except Exception as erro:  # noqa: BLE001 - um provedor nunca derruba o fallback
            analise, mensagem, modelo, uso, situacao, segundos = None, f"erro inesperado: {type(erro).__name__}", "", {}, "erro", 0.0
        registro = {**provedor.descrever(), "resultado": "ok" if analise is not None else "falhou", "detalhe": mensagem,
                    "situacao": situacao, "segundos": segundos}  # fmt: skip
        tentativas.append(registro)
        if analise is None and mensagem.startswith(DADOS_GRANDES) and not cancelado(executor):
            # 413: a mesma IA, uma vez, com os dados enxutos (os dados continuam os já sanitizados; só listas cortadas).
            try:
                analise, mensagem, modelo, uso, situacao, segundos = _tentar(executor, provedor, compactar(dados), avisar, parcial)
            except Exception as erro:  # noqa: BLE001
                analise, mensagem, modelo, uso, situacao, segundos = None, f"erro inesperado: {type(erro).__name__}", "", {}, "erro", 0.0
            tentativas.append({**registro, "resultado": "ok" if analise is not None else "falhou",
                               "detalhe": f"dados enxutos: {mensagem}", "situacao": situacao, "segundos": segundos})  # fmt: skip
        if analise is None and cancelado(executor):
            return ResultadoIA(False, "Envio cancelado pelo técnico.", tentativas=tentativas)
        if analise is not None:
            analise["provedor"] = provedor.tipo
            analise["modelo"] = modelo or provedor.modelo
            analise["provedor_nome"] = provedor.nome
            analise["provedor_titulo"] = provedor.titulo or provedor.nome
            analise["papel"] = provedor.papel
            analise["segundos"] = segundos
            return ResultadoIA(True, f"análise por IA concluída ({provedor.nome}: {analise['modelo']})", analise, uso, tentativas)
    if not tentativas:
        return ResultadoIA(False, "nenhum provedor de IA configurado")
    resumo = "; ".join(f"{t['nome']}: {t['detalhe']}" for t in tentativas)
    return ResultadoIA(False, f"nenhum provedor respondeu ({resumo})", tentativas=tentativas)


def analisar_com_rede(executor: Executor, provedores: list[Provedor], dados: dict[str, Any], monitor: Any = None,
                      avisar: Any = None, rodadas: int = 3, ao_parcial: Any = None) -> ResultadoIA:  # fmt: skip
    """``solicitar_analise`` com a rede contínua (CLAUDE.md 5b): sem rede, espera ela voltar e envia sozinho.

    Só reenvia quando TODAS as IAs falharam por rede e o monitor confirma que a rede caiu (limite, chave
    recusada ou erro do provedor não são "rede": não adianta esperar). Nunca levanta exceção.
    """
    from msscan.rede.monitor import ESPERA_MAXIMA_S, esperar_rede

    resultado = ResultadoIA(False, f"a rede não voltou em {ESPERA_MAXIMA_S // 60} minutos; peça a análise de novo")
    for rodada in range(rodadas):
        if not esperar_rede(monitor, avisar, ESPERA_MAXIMA_S):
            break
        if rodada and avisar is not None:
            avisar("Rede de volta: enviando para análise (pode levar alguns minutos)...")
        resultado = solicitar_analise(executor, provedores, dados, avisar, ao_parcial)
        if cancelado(executor):
            break
        so_rede = bool(resultado.tentativas) and all(t.get("situacao") == SEM_REDE for t in resultado.tentativas)
        if resultado.ok or not so_rede or monitor is None or not monitor.confirmar_queda():
            break
    return resultado

