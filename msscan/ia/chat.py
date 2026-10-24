"""Chat com a IA sobre o diagnóstico (CLAUDE.md 10e, pedido de 28/09/2026).

- A primeira mensagem é da IA, antes de o técnico escrever: o diagnóstico da análise já feita e as principais
  soluções em ordem de prioridade (montada aqui a partir da análise validada; não gasta outra chamada).
- Contexto de cada pergunta: os dados do diagnóstico SANITIZADOS (mesmo recorte da análise) + a análise.
- A IA não executa comandos, não altera status e só pode recomendar ações do catálogo fechado pelo identificador
  (``acoes_catalogo``; identificador fora do catálogo é ignorado e sinalizado).
- Evidências: a resposta cita ``[E1]``, ``[E2]``... no texto e lista cada uma com ``componente`` e ``trecho`` literal
  dos dados. Evidência que não existe nos dados — ou marcador citado sem evidência — fica MARCADA na tela e no
  relatório ("não confirmada pelos dados").
- As mensagens do técnico passam pelo mesmo sanitizador (marcadores iguais aos dos dados), com aviso do que saiu.
- Fallback principal → reserva (429, erro, timeout) e espera da rede, como na análise. Nunca levanta exceção.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from msscan.acoes.catalogo import acao as acao_do_catalogo, descrever_para_ia
from msscan.core.executor import Executor
from msscan.ia.analise import NAO_CONFIRMADA, PRIORIDADES, SEVERIDADES, cancelado, evidencia_confirmada, textos_evidencia
from msscan.ia.fluxo import texto_parcial_chat
from msscan.ia.provedores import CHAT, SEM_REDE, Provedor, montar_conversa, pedir_json, resumo_falhas
from msscan.ia.sanitizar import TAMANHO_MAXIMO, Sanitizador, _encurtar, montar_dados_ia

TIMEOUT_S = 120
MAX_TOKENS_CHAT = 3000
"""Teto da resposta do chat (as instruções pedem até ~120 palavras; o teto cobre o raciocínio de modelos que pensam)."""
MAX_ITENS_LISTA = 8
"""Contexto compacto do chat (CLAUDE.md 15.1): listas dos dados de cada componente com problema cortadas aqui."""
STATUS_SEM_DETALHE = ("OK", "NAO_APLICAVEL")
MAX_PERGUNTA = 4000
"""Caracteres de uma mensagem do técnico (o resto é cortado, com aviso)."""
MAX_TURNOS_ENVIADOS = 12
"""Pares pergunta/resposta anteriores reenviados como contexto (os mais recentes)."""
SUGESTOES = [
    "O que resolvo primeiro?",
    "Preciso trocar alguma peça?",
    "O que ficou sem verificação?",
    "Explique o problema mais grave.",
]
NOMES_CATEGORIA = {"SERIAL": "serial", "SERVICE-TAG": "service tag", "ASSET-TAG": "asset tag", "UUID": "UUID", "MAC": "MAC",
                   "IP": "IP", "HOSTNAME": "hostname", "WWN": "WWN"}  # fmt: skip
RE_HOST_LOCAL = re.compile(r"\b[A-Za-z0-9][A-Za-z0-9-]{0,62}(?:\.[A-Za-z0-9-]{1,63})*\.(?:local|lan|corp|intra|intranet|internal|home|localdomain)\b",
                           re.IGNORECASE)  # fmt: skip
RE_MARCADOR_EVIDENCIA = re.compile(r"\[(E\d{1,3})\]")

ESQUEMA_CHAT = {
    "type": "object",
    "additionalProperties": False,
    "required": ["resposta", "evidencias", "acoes_catalogo"],
    "properties": {
        "resposta": {"type": "string"},
        "evidencias": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "componente", "trecho"],
                "properties": {"id": {"type": "string"}, "componente": {"type": "string"}, "trecho": {"type": "string"}},
            },
        },
        "acoes_catalogo": {"type": "array", "items": {"type": "string"}},
    },
}

INSTRUCOES_CHAT = f"""\
Você conversa com um técnico de campo sobre o diagnóstico de um servidor físico feito pelo MSscan. Responda em \
português do Brasil, curto e direto, como um colega experiente: no máximo 120 palavras (até 8 linhas), sem \
introdução nem repetição da pergunta; detalhe mais só se o técnico pedir.

Regras:
1. Use SOMENTE os dados do diagnóstico e a análise fornecidos na primeira mensagem. Não presuma nada além deles; se a \
pergunta não puder ser respondida com esses dados, diga isso.
2. O status de cada componente foi apurado pelas ferramentas e é definitivo: não o altere nem o conteste. \
NAO_VERIFICADO significa que não foi possível verificar — nunca trate como saudável.
3. Você NÃO executa nada e NÃO escreve comandos (de terminal, ipmitool, racadm, scripts etc.). Se uma ação do \
catálogo abaixo resolver o que o técnico perguntou, informe o identificador em "acoes_catalogo"; senão, deixe a lista \
vazia. Nunca invente identificadores. Catálogo de ações do MSscan:
{descrever_para_ia()}
4. Você não decide atualizações de firmware: elas são decididas pelo técnico, em outra etapa.
5. Toda afirmação sobre o equipamento cita a evidência no texto com um marcador [E1], [E2]... e lista cada marcador \
em "evidencias" com "componente" (identificador do componente nos dados, ex.: "fontes", ou "servidor") e "trecho" \
(texto copiado LITERALMENTE dos dados desse componente). Sem evidência nos dados, não afirme.
6. Identificadores foram trocados por marcadores como [SERIAL-1] e [MAC-2]; use-os como estão.
7. Eventos do log do BMC são histórico: podem já ter sido resolvidos.
"""


def _agora() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _ordem_problema(problema: dict[str, Any]) -> tuple[int, int]:
    plano = problema.get("plano") or {}
    prioridade = plano.get("prioridade") if plano.get("prioridade") in PRIORIDADES else "media"
    severidade = problema.get("severidade") if problema.get("severidade") in SEVERIDADES else "media"
    return PRIORIDADES.index(prioridade), SEVERIDADES.index(severidade)


def primeira_mensagem(analise: dict[str, Any]) -> dict[str, Any]:
    """Mensagem inicial da IA: o diagnóstico dela e as soluções em ordem de prioridade (da análise validada)."""
    linhas = [f"Diagnóstico: {analise.get('resumo') or '(sem resumo)'}"]
    evidencias: list[dict[str, Any]] = []
    acoes: list[dict[str, str]] = []
    problemas = sorted((p for p in analise.get("problemas") or [] if isinstance(p, dict)), key=_ordem_problema)
    if problemas:
        linhas += ["", "Principais soluções, em ordem de prioridade:"]
    for numero, problema in enumerate(problemas, 1):
        plano = problema.get("plano") or {}
        marcas = []
        for evidencia in problema.get("evidencias") or []:
            ident = f"E{len(evidencias) + 1}"
            evidencias.append({"id": ident, "componente": str(evidencia.get("componente") or ""),
                               "trecho": str(evidencia.get("trecho") or ""), "confirmada": bool(evidencia.get("confirmada"))})  # fmt: skip
            marcas.append(f"[{ident}]")
        detalhes = [f"prioridade {plano.get('prioridade') or 'media'}", f"severidade {problema.get('severidade') or 'media'}"]
        if plano.get("troca_de_peca"):
            detalhes.append("provável troca de peça")
        if plano.get("intervencao_fisica"):
            detalhes.append("exige ir até o servidor")
        linhas.append(f"{numero}. {problema.get('titulo') or 'Problema'} ({'; '.join(detalhes)}) {' '.join(marcas)}".rstrip())
        passos = plano.get("passos") or ([problema["acao_recomendada"]] if problema.get("acao_recomendada") else [])
        linhas += [f"   - {passo}" for passo in passos]
        if not problema.get("confirmado", True):
            linhas.append(f"   ({NAO_CONFIRMADA})")
        if plano.get("acao"):
            acoes.append({"id": plano["acao"]["id"], "titulo": plano["acao"]["titulo"]})
    if not problemas:
        linhas += ["", "Não encontrei problemas nos dados do diagnóstico."]
    if analise.get("limitacoes"):
        linhas += ["", "Ficou sem verificação: " + "; ".join(str(l) for l in analise["limitacoes"])]
    linhas += ["", "Pergunte o que quiser sobre esses problemas. Eu só explico e recomendo; não executo nada."]
    return {"papel": "ia", "origem": "analise", "texto": "\n".join(linhas), "evidencias": evidencias,
            "acoes": list({a["id"]: a for a in acoes}.values()), "avisos": [], "quando": _agora(),
            "provedor_titulo": analise.get("provedor_titulo") or analise.get("provedor_nome") or "",
            "modelo": analise.get("modelo") or "", "papel_ia": analise.get("papel") or ""}  # fmt: skip


def validar_resposta(bruta: Any, textos: dict[str, str]) -> dict[str, Any]:
    """Resposta do chat conferida: evidências marcadas e ações só do catálogo. Levanta ``ValueError`` se inválida."""
    if not isinstance(bruta, dict) or not isinstance(bruta.get("resposta"), str) or not bruta["resposta"].strip():
        raise ValueError("resposta do chat fora do formato esperado")
    texto = bruta["resposta"].strip()
    evidencias = []
    for evidencia in bruta.get("evidencias") or []:
        if isinstance(evidencia, dict):
            evidencias.append({"id": str(evidencia.get("id") or "").strip().strip("[]"), "componente": str(evidencia.get("componente") or ""),
                               "trecho": str(evidencia.get("trecho") or ""), "confirmada": evidencia_confirmada(evidencia, textos)})  # fmt: skip
    avisos = []
    listadas = {e["id"] for e in evidencias}
    for citada in dict.fromkeys(RE_MARCADOR_EVIDENCIA.findall(texto)):
        if citada not in listadas:
            evidencias.append({"id": citada, "componente": "", "trecho": "", "confirmada": False})
    if any(not e["confirmada"] for e in evidencias):
        avisos.append("Há afirmações com evidência que não existe nos dados (marcadas): " + NAO_CONFIRMADA + ".")
    acoes = []
    for identificador in bruta.get("acoes_catalogo") or []:
        identificador = str(identificador).strip()
        entrada = acao_do_catalogo(identificador) if identificador else None
        if entrada is None:
            if identificador:
                avisos.append(f"A IA recomendou a ação '{identificador}', que não existe no catálogo do MSscan: ignorada.")
            continue
        if identificador not in [a["id"] for a in acoes]:
            acoes.append({"id": identificador, "titulo": entrada["titulo"]})
    return {"texto": texto, "evidencias": evidencias, "acoes": acoes, "avisos": avisos}


@dataclass
class ResultadoChat:
    """Resposta de uma pergunta. ``mensagem_ia`` só com ``ok``; ``tentativas`` sempre (sem chaves)."""

    ok: bool
    mensagem: str
    mensagem_ia: dict[str, Any] | None = None
    tentativas: list[dict[str, str]] = field(default_factory=list)


class Conversa:
    """Uma conversa do técnico com a IA sobre UM relatório. Guarda só textos sanitizados (nunca chaves)."""

    def __init__(self, relatorio: dict[str, Any], nomes_host: list[str] | None = None) -> None:
        self.sanitizador = Sanitizador({nome: "HOSTNAME" for nome in nomes_host or []})
        recorte = montar_dados_ia(relatorio)
        self.sanitizador.coletar(recorte)
        self.sanitizador.coletar(relatorio.get("equipamento") or {})
        dados = self.sanitizador.dados(recorte)
        if len(json.dumps(dados, ensure_ascii=False, default=str)) > TAMANHO_MAXIMO:
            avisos: list[str] = []
            dados = _encurtar(dados, avisos)
            dados["listas_encurtadas"] = avisos
        self.dados = dados
        self.textos = textos_evidencia(dados)
        analise = relatorio.get("interpretacao_ia") or {}
        self.analise = {k: analise.get(k) for k in ("resumo", "problemas", "limitacoes")}
        self.mensagens: list[dict[str, Any]] = [primeira_mensagem(analise)]

    # --- técnico ---------------------------------------------------------------------------------

    def preparar_pergunta(self, texto: str) -> tuple[str, list[str]]:
        """Texto do técnico sanitizado e os avisos (o que foi removido; corte por tamanho)."""
        texto = (texto or "").replace("\r\n", "\n").strip()
        avisos = []
        if len(texto) > MAX_PERGUNTA:
            texto = texto[:MAX_PERGUNTA]
            avisos.append(f"Mensagem cortada em {MAX_PERGUNTA} caracteres.")
        antes = self.sanitizador.contagem()
        limpo = self.sanitizador.texto(texto)
        limpo = RE_HOST_LOCAL.sub(lambda m: self.sanitizador.marcador("HOSTNAME", m.group(0)), limpo)
        depois = self.sanitizador.contagem()
        trocados = [c for c in depois if depois[c] > antes.get(c, 0)]
        marcadores_novos = [m for m in re.findall(r"\[[A-Z-]+-\d+\]", limpo) if m not in texto]
        if trocados or marcadores_novos:
            categorias = sorted({NOMES_CATEGORIA.get(re.sub(r"-\d+$", "", m.strip("[]")), m) for m in marcadores_novos}
                                | {NOMES_CATEGORIA.get(c, c) for c in trocados})  # fmt: skip
            avisos.append("Removido antes do envio (" + ", ".join(categorias) + "): identificadores do servidor não vão "
                          "para a IA. O texto enviado está na conversa.")  # fmt: skip
        return limpo, avisos

    # --- IA --------------------------------------------------------------------------------------

    def contexto(self) -> str:
        """Resumo compacto (CLAUDE.md 15.1): componentes OK só com o status; os demais com mensagem e dados (listas
        cortadas). JSON sem espaços. As evidências continuam conferidas contra os dados completos."""
        compacto = json.dumps(resumo_compacto(self.dados), ensure_ascii=False, separators=(",", ":"), default=str)
        analise = json.dumps(self.analise, ensure_ascii=False, separators=(",", ":"), default=str)
        return (f"Dados do diagnóstico (JSON compacto, sem identificadores; componentes OK só com o status):\n{compacto}"
                f"\n\nAnálise já feita pela IA (JSON):\n{analise}\n\nA seguir, a conversa com o técnico.")

    def para_envio(self, pergunta: str) -> list[tuple[str, str]]:
        """Mensagens para a API: contexto (usuário), primeira mensagem (IA) e os turnos recentes, terminando na pergunta."""
        turnos: list[tuple[str, str]] = []
        for mensagem in self.mensagens[1:]:
            if mensagem["papel"] == "sistema":
                continue
            papel = "usuario" if mensagem["papel"] == "tecnico" else "ia"
            if turnos and turnos[-1][0] == papel:  # pergunta sem resposta (falhou): junta as duas
                turnos[-1] = (papel, turnos[-1][1] + "\n\n" + mensagem["texto"])
            else:
                turnos.append((papel, mensagem["texto"]))
        if turnos and turnos[-1][0] == "usuario":
            turnos.pop()  # a pergunta pendente é a que vai agora
        turnos = turnos[-2 * MAX_TURNOS_ENVIADOS:]
        if turnos and turnos[0][0] == "ia":
            turnos = turnos[1:]
        return [("usuario", self.contexto()), ("ia", self.mensagens[0]["texto"]), *turnos, ("usuario", pergunta)]

    def _tentar(self, executor: Executor, provedor: Provedor, pergunta: str, avisar: Callable[[str], None] | None = None,
                ao_parcial: Callable[[str], None] | None = None) -> tuple[dict[str, Any] | None, str, str, str, float]:  # fmt: skip
        """Uma tentativa num provedor: ``(resposta validada, mensagem, modelo, situação, segundos)``."""
        mensagens = self.para_envio(pergunta)
        parcial = (lambda bruto: ao_parcial(texto_parcial_chat(bruto))) if ao_parcial is not None else None
        resultado, texto = pedir_json(executor, provedor,
                                      lambda p, simples: montar_conversa(p, INSTRUCOES_CHAT, mensagens, ESQUEMA_CHAT, simples=simples,
                                                                         max_tokens=MAX_TOKENS_CHAT),
                                      TIMEOUT_S, avisar, perfil=CHAT, ao_parcial=parcial)  # fmt: skip
        if not resultado.ok:
            return None, resultado.mensagem, resultado.modelo, resultado.situacao or "erro", resultado.duracao_s
        try:
            return validar_resposta(json.loads(texto), self.textos), "", resultado.modelo, "ok", resultado.duracao_s
        except (ValueError, TypeError) as erro:
            return None, f"resposta inválida: {erro}", resultado.modelo, "ok", resultado.duracao_s

    def perguntar(self, executor: Executor, provedores: list[Provedor], pergunta: str, avisar: Callable[[str], None] | None = None,
                  ao_parcial: Callable[[str], None] | None = None) -> ResultadoChat:  # fmt: skip
        """Pergunta JÁ SANITIZADA → resposta da primeira IA que responder (principal, depois reserva)."""
        tentativas: list[dict[str, str]] = []
        for provedor in provedores:
            try:
                validada, mensagem, modelo, situacao, segundos = self._tentar(executor, provedor, pergunta, avisar, ao_parcial)
            except Exception as erro:  # noqa: BLE001 - um provedor nunca derruba o fallback
                validada, mensagem, modelo, situacao, segundos = None, f"erro inesperado: {type(erro).__name__}", "", "erro", 0.0
            tentativas.append({**provedor.descrever(), "resultado": "ok" if validada else "falhou", "detalhe": mensagem,
                               "situacao": situacao, "segundos": segundos})  # fmt: skip
            if validada is None and cancelado(executor):
                return ResultadoChat(False, "envio cancelado pelo técnico", tentativas=tentativas)
            if validada is not None:
                if len(tentativas) > 1:
                    validada["avisos"].insert(0, "Resposta da IA reserva (" + "; ".join(
                        f"{t['nome']}: {t['detalhe']}" for t in tentativas[:-1]) + ").")  # fmt: skip
                return ResultadoChat(True, "", {"papel": "ia", "origem": "chat", **validada, "quando": _agora(),
                                                "provedor_titulo": provedor.titulo or provedor.nome,
                                                "modelo": modelo or provedor.modelo, "papel_ia": provedor.papel,
                                                "segundos": segundos}, tentativas)  # fmt: skip
        if not tentativas:
            return ResultadoChat(False, "nenhuma IA configurada (Configurar IA)")
        return ResultadoChat(False, "nenhuma IA respondeu (" + "; ".join(f"{t['nome']}: {t['detalhe']}" for t in tentativas) + ")",
                             tentativas=tentativas)  # fmt: skip

    def conversar(self, executor: Executor, provedores: list[Provedor], texto: str, monitor: Any = None,
                  avisar: Callable[[str], None] | None = None, rodadas: int = 3,
                  ao_parcial: Callable[[str], None] | None = None) -> ResultadoChat:  # fmt: skip
        """Registra a pergunta (sanitizada) e busca a resposta; sem rede, espera ela voltar e retoma sozinho.

        Como na análise: só reenvia quando TODAS as IAs falharam por rede e o monitor confirma a queda.
        Nunca levanta exceção. A pergunta fica na conversa mesmo se nenhuma IA responder.
        """
        from msscan.rede.monitor import ESPERA_MAXIMA_S, esperar_rede

        pergunta, avisos = self.preparar_pergunta(texto)
        if not pergunta:
            return ResultadoChat(False, "mensagem vazia")
        self.mensagens.append({"papel": "tecnico", "texto": pergunta, "avisos": avisos, "quando": _agora()})
        resultado = ResultadoChat(False, f"a rede não voltou em {ESPERA_MAXIMA_S // 60} minutos; envie de novo")
        for rodada in range(rodadas):
            if not esperar_rede(monitor, avisar, ESPERA_MAXIMA_S):
                break
            if rodada and avisar is not None:
                avisar("Rede de volta: enviando a mensagem...")
            resultado = self.perguntar(executor, provedores, pergunta, avisar, ao_parcial)
            if cancelado(executor):
                break
            so_rede = bool(resultado.tentativas) and all(t.get("situacao") == SEM_REDE for t in resultado.tentativas)
            if resultado.ok or not so_rede or monitor is None or not monitor.confirmar_queda():
                break
        if resultado.ok and resultado.mensagem_ia is not None:
            self.mensagens.append(resultado.mensagem_ia)
        else:
            motivo = resumo_falhas(resultado.tentativas) if resultado.tentativas else resultado.mensagem
            self.mensagens.append({"papel": "sistema", "texto": f"Sem resposta da IA: {motivo}.", "quando": _agora()})
        return resultado

    def retirar_sem_resposta(self) -> str | None:
        """"Tentar novamente": tira a última pergunta que ficou sem resposta (e o aviso) e devolve o texto dela."""
        if len(self.mensagens) >= 2 and self.mensagens[-1]["papel"] == "sistema" and self.mensagens[-2]["papel"] == "tecnico":
            self.mensagens.pop()
            return str(self.mensagens.pop()["texto"])
        return None

    def para_relatorio(self) -> dict[str, Any]:
        """Conversa para o relatório (JSON/TXT/HTML): só textos sanitizados, sem chaves."""
        return {"mensagens": [dict(m) for m in self.mensagens]}


def _podar(valor: Any) -> Any:
    if isinstance(valor, dict):
        return {k: _podar(v) for k, v in valor.items()}
    if isinstance(valor, list):
        itens = [_podar(v) for v in valor[:MAX_ITENS_LISTA]]
        if len(valor) > MAX_ITENS_LISTA:
            itens.append(f"(mais {len(valor) - MAX_ITENS_LISTA} itens omitidos no chat)")
        return itens
    return valor


def resumo_compacto(dados: dict[str, Any]) -> dict[str, Any]:
    """Recorte do chat: servidor e veredito; componente OK/N/A só com o status; os demais com mensagem e dados."""
    componentes = []
    for c in dados.get("componentes") or []:
        if not isinstance(c, dict):
            continue
        if c.get("status") in STATUS_SEM_DETALHE:
            componentes.append({"componente": c.get("componente"), "status": c.get("status")})
        else:
            componentes.append(_podar({k: c.get(k) for k in ("componente", "titulo", "status", "fonte", "mensagem", "dados")}))
    return {"servidor": _podar(dados.get("servidor")), "veredito": dados.get("veredito"),
            "contagem_por_status": dados.get("contagem_por_status"), "componentes": componentes}  # fmt: skip


def texto_com_marcas(mensagem: dict[str, Any]) -> str:
    """Texto com as evidências não confirmadas marcadas (terminal e relatório TXT)."""
    confirmadas = {e["id"]: e["confirmada"] for e in mensagem.get("evidencias") or []}
    return RE_MARCADOR_EVIDENCIA.sub(lambda m: m.group(0) if confirmadas.get(m.group(1)) else f"{m.group(0)}(NÃO CONFIRMADA)",
                                     mensagem.get("texto") or "")  # fmt: skip


__all__ = ["Conversa", "ESQUEMA_CHAT", "INSTRUCOES_CHAT", "ResultadoChat", "SUGESTOES", "primeira_mensagem", "resumo_compacto",
           "texto_com_marcas", "validar_resposta"]  # fmt: skip
