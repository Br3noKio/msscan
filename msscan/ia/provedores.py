"""Provedores de IA (CLAUDE.md 10c): três tipos de API cobrem o catálogo — Gemini, compatível com OpenAI e Anthropic.

Quais usar vem de ``msscan/ia/config.py`` (``config/ia.conf``: ``principal`` e ``reserva`` do catálogo
``ia_provedores.yaml``); este módulo monta as requisições e interpreta as respostas de cada tipo. Formato
antigo do ``ia.conf`` (v0.8.2, sem ``[ia]``: seções na ordem de tentativa, ``chave`` = nome de arquivo em
``config/``) continua aceito. Sem ``ia.conf``: Gemini gratuito (``gemini.key``) e Claude (``ia.key``).
Chave nunca vem de variável de ambiente nem aparece em ``repr``, histórico ou relatório.

Formatos conferidos na documentação oficial (28/09/2026; ``generateContent`` do Gemini segue documentado).
TODO(fixture): respostas reais de cada provedor.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from msscan.core.executor import URL_IA, RespostaIA, validar_url_ia
from msscan.ia.catalogo import carregar_catalogo
from msscan.ia.config import chave_de, ler_config, normalizar_chave

TIPOS = ("gemini", "openai", "anthropic")
PADROES = {
    "gemini": {"modelo": "gemini-flash-latest", "chave": "gemini.key"},
    "anthropic": {"modelo": "claude-sonnet-5", "chave": "ia.key"},
}
PRINCIPAL = "principal"
RESERVA = "reserva"
VERSAO_ANTHROPIC = "2023-06-01"
MAX_TOKENS = 16000
_RE_ARQUIVO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_RE_MODELO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
# Erros temporários do provedor (sobrecarga, gateway): repete com espera curta; o ``Retry-After`` manda, se couber.
# Pedido do usuário (30/09/2026, CLAUDE.md 15.1): análise com esperas de 2 s e 5 s e no máximo ~20 s (esperas +
# respostas que falharam) antes de trocar de provedor; chat com UMA nova tentativa após 2 s e sem o modelo "lite".
TEMPORARIOS = (502, 503, 504, 529)
ESPERAS_S = (2, 5)
TETO_ANALISE_S = 20.0
ESPERAS_CHAT_S = (2,)
TETO_CHAT_S = 20.0
ESPERA_MAXIMA_S = 60
ANALISE, CHAT = "analise", "chat"
SOBRECARGA = "O serviço da IA está sobrecarregado agora. Tente de novo em alguns minutos ou configure uma IA reserva"
_dormir = time.sleep  # trocado nos testes
_relogio = time.monotonic
GEMINI_RECUSAS = {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "LANGUAGE", "OTHER"}


@dataclass
class Provedor:
    """Um provedor configurado. ``chave`` só em memória (nunca vai para relatório, log ou histórico)."""

    nome: str
    tipo: str
    modelo: str
    url: str
    chave: str = field(default="", repr=False)
    papel: str = ""
    """``principal``, ``reserva`` ou ``""`` (formato antigo do ia.conf)."""
    titulo: str = ""
    categoria: str = ""
    modelo_alternativo: str = ""
    """Outro modelo do mesmo provedor, com a mesma chave (catálogo): tentado antes da reserva."""
    url_alternativo: str = ""

    def descrever(self) -> dict[str, str]:
        """Para tela e relatório: sem a chave."""
        dados = {"nome": self.nome, "provedor": self.tipo, "modelo": self.modelo}
        if self.papel:
            dados.update(papel=self.papel, titulo=self.titulo or self.nome, categoria=self.categoria)
        return dados


@dataclass
class Interpretacao:
    """Resposta de um provedor: texto JSON da análise, ou o motivo da falha."""

    ok: bool
    texto: str = ""
    mensagem: str = ""
    modelo: str = ""
    uso: dict[str, Any] = field(default_factory=dict)
    duracao_s: float = 0.0
    """Tempo até a resposta (todas as tentativas neste provedor, com as esperas)."""
    situacao: str = ""
    """Para o indicador de estado da IA: ``ok``, ``limite``, ``sem_acesso``, ``sem_rede`` ou ``erro``."""


OK, LIMITE, SEM_ACESSO, SEM_REDE, ERRO = "ok", "limite", "sem_acesso", "sem_rede", "erro"


DADOS_GRANDES = "dados grandes demais"
"""Início da mensagem do HTTP 413 (a análise repete uma vez com os dados enxutos)."""


def classificar(resposta: RespostaIA) -> str:
    """Situação do provedor a partir da resposta HTTP (a chave em si nunca é olhada)."""
    if resposta.status is None:
        return SEM_REDE if resposta.erro_tipo in ("dns", "conexao", "timeout", "tls", "proxy") else ERRO
    if resposta.status in (401, 403):
        return SEM_ACESSO
    if resposta.status == 429:
        return LIMITE
    return OK if 200 <= resposta.status < 300 else ERRO


def _ler_chave(pasta_config: Path, arquivo: str) -> str | None:
    try:
        return (pasta_config / arquivo).read_text(encoding="utf-8", errors="replace").strip() or None
    except OSError:
        return None


def _url(tipo: str, modelo: str, base: str) -> str:
    if tipo == "anthropic":
        return URL_IA
    if tipo == "gemini":
        return f"https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent"
    return base.rstrip("/") + "/chat/completions"


def _legado(pasta_dados: str, secoes: list[tuple[str, dict[str, str]]], avisar_sem_chave: bool) -> tuple[list[Provedor], list[str]]:
    """Formato da v0.8.2 (sem ``[ia]``): seções na ordem de tentativa, ``chave`` = nome de arquivo."""
    pasta_config = Path(pasta_dados) / "config"
    avisos: list[str] = []
    provedores = []
    for nome, campos in secoes:
        tipo = (campos.get("provedor") or "").strip().lower()
        if tipo not in TIPOS:
            avisos.append(f"[{nome}]: provedor '{tipo}' desconhecido (use gemini, openai ou anthropic)")
            continue
        padrao = PADROES.get(tipo, {})
        modelo = (campos.get("modelo") or padrao.get("modelo") or "").strip()
        if not _RE_MODELO.match(modelo) or ".." in modelo:
            avisos.append(f"[{nome}]: modelo ausente ou inválido")
            continue
        url = _url(tipo, modelo, (campos.get("url") or "").strip())
        motivo = validar_url_ia(url)
        if motivo:
            avisos.append(f"[{nome}]: {motivo}")
            continue
        arquivo_chave = (campos.get("chave") or padrao.get("chave") or "").strip()
        chave = ""
        if arquivo_chave:
            if not _RE_ARQUIVO.match(arquivo_chave) or ".." in arquivo_chave:
                avisos.append(f"[{nome}]: 'chave' deve ser só o nome de um arquivo em config/ (sem caminho)")
                continue
            chave = _ler_chave(pasta_config, arquivo_chave) or ""
        if not chave and (tipo != "openai" or url.startswith("https://")):
            if avisar_sem_chave:
                avisos.append(f"[{nome}]: sem chave em config/{arquivo_chave or '?'}")
            continue
        provedores.append(Provedor(nome, tipo, modelo, url, chave))
    return provedores, avisos


def _proprio(identificador: str, campos: dict[str, str], papel: str) -> tuple[Provedor | None, str]:
    """Seção fora do catálogo no formato novo (ex.: Ollama local): ``provedor``, ``url``, ``modelo`` e ``chave``."""
    tipo = (campos.get("provedor") or "").strip().lower()
    if tipo not in TIPOS:
        return None, f"[{identificador}]: não está no catálogo de IAs e não tem 'provedor' (gemini, openai ou anthropic)"
    modelo = (campos.get("modelo") or PADROES.get(tipo, {}).get("modelo") or "").strip()
    if not _RE_MODELO.match(modelo) or ".." in modelo:
        return None, f"[{identificador}]: modelo ausente ou inválido"
    url = _url(tipo, modelo, (campos.get("url") or "").strip())
    motivo = validar_url_ia(url)
    if motivo:
        return None, f"[{identificador}]: {motivo}"
    chave = normalizar_chave(campos.get("chave", ""))
    return Provedor(identificador, tipo, modelo, url, chave, papel=papel, titulo=identificador), ""


def resolver_configuracao(pasta_dados: str | None) -> tuple[list[Provedor], list[str]]:
    """Principal e reserva configuradas (COM ou SEM chave), na ordem de tentativa, e os avisos.

    Formato novo (``[ia]`` com ``principal``/``reserva``): ids do catálogo, com chave no ``ia.conf`` ou no
    arquivo ``.key`` antigo; seção fora do catálogo = provedor próprio. Sem ``ia.conf``: Gemini gratuito
    (``gemini.key``) e depois Claude (``ia.key``), como na v0.7/v0.8.2.
    """
    if not pasta_dados:
        return [], []
    catalogo = carregar_catalogo(pasta_dados)
    config = ler_config(pasta_dados)
    avisos = list(catalogo.avisos) + [f"config/ia.conf: {a}" for a in config.avisos]
    if config.legado:
        provedores, mais = _legado(pasta_dados, [(n, config.secoes[n]) for n in config.ordem], avisar_sem_chave=True)
        return [p for p in provedores], avisos + mais
    ids = [(config.principal, PRINCIPAL), (config.reserva, RESERVA)] if config.existe else \
        [("gemini_gratuito", PRINCIPAL), ("anthropic", RESERVA)]  # fmt: skip
    if config.existe and not config.principal:
        avisos.append("config/ia.conf: escolha a IA principal na seção [ia] (principal = ...)")
    provedores = []
    for identificador, papel in ids:
        if not identificador:
            continue
        entrada = catalogo.obter(identificador)
        campos = config.secoes.get(identificador, {})
        if entrada is None:
            provedor, aviso = _proprio(identificador, campos, papel)
            if aviso:
                avisos.append(aviso)
            if provedor is not None:
                provedores.append(provedor)
            continue
        modelo = (campos.get("modelo") or entrada.modelo_padrao).strip()
        if not _RE_MODELO.match(modelo) or ".." in modelo:
            avisos.append(f"[{identificador}]: modelo inválido no ia.conf; usando o do catálogo ({entrada.modelo_padrao})")
            modelo = entrada.modelo_padrao
        url = entrada.url_chamada(modelo)
        motivo = validar_url_ia(url)
        if motivo:
            avisos.append(f"[{identificador}]: {motivo}")
            continue
        chave = chave_de(pasta_dados, config, entrada)
        alternativo = entrada.modelo_alternativo if entrada.modelo_alternativo != modelo else ""
        provedores.append(Provedor(identificador, entrada.tipo_api, modelo, url, chave, papel=papel,
                                   titulo=entrada.nome, categoria=entrada.categoria, modelo_alternativo=alternativo,
                                   url_alternativo=entrada.url_chamada(alternativo) if alternativo else ""))  # fmt: skip
    return provedores, avisos


def precisa_chave(provedor: Provedor) -> bool:
    """Todos precisam de chave, exceto provedor próprio compatível com OpenAI por HTTP local (Ollama)."""
    return not (provedor.tipo == "openai" and provedor.url.startswith("http://"))


def carregar_provedores(pasta_dados: str | None) -> tuple[list[Provedor], list[str]]:
    """``(provedores prontos para uso, avisos)``: só os que têm chave (principal, depois reserva)."""
    configurados, avisos = resolver_configuracao(pasta_dados)
    prontos = []
    for provedor in configurados:
        if precisa_chave(provedor) and not provedor.chave:
            if provedor.papel:
                avisos.append(f"[{provedor.nome}]: sem chave (configure na tela \"Configurar IA\" ou no ia.conf)")
            continue
        prontos.append(provedor)
    return prontos, avisos


def montar(provedor: Provedor, instrucoes: str, mensagem: str, esquema: dict[str, Any] | None, simples: bool = False,
           max_tokens: int = MAX_TOKENS) -> tuple[dict[str, str], dict[str, Any]]:  # fmt: skip
    """``(cabeçalhos, corpo)``. ``simples=True`` (ou sem esquema): sem saída estruturada."""
    return montar_conversa(provedor, instrucoes, [("usuario", mensagem)], esquema, simples, max_tokens)


def montar_conversa(provedor: Provedor, instrucoes: str, mensagens: list[tuple[str, str]], esquema: dict[str, Any] | None,
                    simples: bool = False, max_tokens: int = MAX_TOKENS) -> tuple[dict[str, str], dict[str, Any]]:  # fmt: skip
    """Como ``montar``, com várias mensagens ``(papel, texto)``: papel ``usuario`` ou ``ia``, alternados, começando e
    terminando pelo usuário (chat da interface, CLAUDE.md 10e)."""
    simples = simples or esquema is None
    papeis = {"anthropic": ("user", "assistant"), "gemini": ("user", "model")}.get(provedor.tipo, ("user", "assistant"))
    conversa = [(papeis[0] if papel == "usuario" else papeis[1], texto) for papel, texto in mensagens]
    if provedor.tipo == "anthropic":
        corpo: dict[str, Any] = {"model": provedor.modelo, "max_tokens": max_tokens, "system": instrucoes,
                                 "messages": [{"role": r, "content": t} for r, t in conversa]}  # fmt: skip
        if not simples:
            corpo["output_config"] = {"format": {"type": "json_schema", "schema": esquema}}
        return {"x-api-key": provedor.chave, "anthropic-version": VERSAO_ANTHROPIC, "content-type": "application/json"}, corpo
    instrucoes_json = instrucoes if esquema is None else (
        f"{instrucoes}\nResponda SOMENTE com um objeto JSON válido neste esquema:\n{json.dumps(esquema, ensure_ascii=False)}")
    if provedor.tipo == "gemini":
        geracao: dict[str, Any] = {"responseMimeType": "application/json", "maxOutputTokens": max_tokens}
        if not simples:
            geracao["responseJsonSchema"] = esquema
        corpo = {
            "systemInstruction": {"parts": [{"text": instrucoes_json}]},
            "contents": [{"role": r, "parts": [{"text": t}]} for r, t in conversa],
            "generationConfig": geracao,
        }
        return {"x-goog-api-key": provedor.chave, "content-type": "application/json"}, corpo
    corpo = {"model": provedor.modelo,
             "messages": [{"role": "system", "content": instrucoes_json}] + [{"role": r, "content": t} for r, t in conversa]}  # fmt: skip
    if not simples:
        corpo["response_format"] = {"type": "json_object"}
    cabecalhos = {"content-type": "application/json"}
    if provedor.chave:
        cabecalhos["authorization"] = f"Bearer {provedor.chave}"
    return cabecalhos, corpo


def temporario(resposta: RespostaIA) -> bool:
    """Sobrecarga (502/503/504/529) ou demora: vale repetir. 400/401/403 nunca."""
    return resposta.status in TEMPORARIOS or (resposta.status is None and resposta.erro_tipo == "timeout")


def _mensagem_http(resposta: RespostaIA, gratuito: bool = False) -> str:
    if resposta.status is None:
        return {
            "dns": "sem resolução de nomes (DNS)", "tls": "erro de TLS (relógio errado?)", "proxy": "erro no proxy",
            "timeout": "não respondeu a tempo", "conexao": "sem conexão", "bloqueado": resposta.erro,
        }.get(resposta.erro_tipo, resposta.erro or "sem resposta")  # fmt: skip
    detalhe = ""
    corpo = resposta.corpo
    if isinstance(corpo, dict):
        erro = corpo.get("error")
        if isinstance(erro, dict):
            detalhe = str(erro.get("message") or "")
        elif isinstance(erro, str):
            detalhe = erro
    if resposta.status in TEMPORARIOS:
        return SOBRECARGA
    textos = {400: "requisição recusada", 401: "Chave inválida ou sem permissão", 402: "problema de cobrança",
              403: "Chave inválida ou sem permissão", 404: "modelo indisponível", 413: DADOS_GRANDES,
              429: "Limite do plano gratuito atingido" if gratuito else "Limite de uso atingido"}  # fmt: skip
    texto = textos.get(resposta.status, f"HTTP {resposta.status}")
    return f"{texto}: {detalhe[:200]}" if detalhe and resposta.status in (400, 404, 413) else texto


def resumo_falhas(tentativas: list[dict[str, Any]]) -> str:
    """Motivo das falhas para a tela: só o motivo com uma IA; "IA: motivo" por IA com mais de uma."""
    if len(tentativas) == 1:
        return str(tentativas[0].get("detalhe") or "sem resposta")
    return "; ".join(f"{t.get('titulo') or t.get('nome')}: {t.get('detalhe') or 'sem resposta'}" for t in tentativas)


def recusou_esquema(resposta: RespostaIA) -> bool:
    """400 citando o formato estruturado, o streaming ou o ajuste de raciocínio (provedor/modelo sem suporte): vale
    tentar sem eles (modo ``simples``)."""
    if resposta.status != 400:
        return False
    texto = json.dumps(resposta.corpo, ensure_ascii=False).lower() if resposta.corpo is not None else ""
    return any(p in texto for p in ("responsejsonschema", "response_format", "json_schema", "output_config", "json_object",
                                    "response_mime_type", "stream", "thinking", "reasoning_effort"))  # fmt: skip


def url_fluxo(provedor: Provedor) -> str:
    """URL de streaming: Gemini usa outro método (``:streamGenerateContent?alt=sse``); os demais, a mesma URL."""
    if provedor.tipo == "gemini" and provedor.url.endswith(":generateContent"):
        return provedor.url[: -len(":generateContent")] + ":streamGenerateContent?alt=sse"
    return provedor.url


def _ajustar(provedor: Provedor, corpo: dict[str, Any], fluxo: bool, rapido: bool) -> dict[str, Any]:
    """Streaming (Anthropic e compatível com OpenAI: ``"stream": true``) e raciocínio curto no chat.

    Raciocínio curto (documentação oficial, 30/09/2026): Gemini 3 ``thinkingConfig.thinkingLevel = "low"`` ("real-time
    chat"); Groq com gpt-oss ``reasoning_effort = "low"``.
    """
    corpo = dict(corpo)
    if fluxo and provedor.tipo in ("anthropic", "openai"):
        corpo["stream"] = True
    if rapido and provedor.tipo == "gemini" and provedor.modelo.startswith("gemini-3"):
        corpo["generationConfig"] = {**corpo.get("generationConfig", {}), "thinkingConfig": {"thinkingLevel": "low"}}
    if rapido and provedor.tipo == "openai" and "gpt-oss" in provedor.modelo:
        corpo["reasoning_effort"] = "low"
    return corpo


def interpretar(provedor: Provedor, resposta: RespostaIA) -> Interpretacao:
    """Texto JSON da análise ou o motivo da falha (sempre tratado como "tentar o próximo provedor")."""
    if resposta.status != 200 or not isinstance(resposta.corpo, dict):
        return Interpretacao(False, mensagem=_mensagem_http(resposta, provedor.categoria == "gratuita"), situacao=classificar(resposta))
    corpo = resposta.corpo
    if provedor.tipo == "anthropic":
        uso = corpo.get("usage") if isinstance(corpo.get("usage"), dict) else {}
        if corpo.get("stop_reason") == "refusal":
            return Interpretacao(False, mensagem="a IA recusou a análise", uso=uso)
        if corpo.get("stop_reason") == "max_tokens":
            return Interpretacao(False, mensagem="resposta incompleta (limite de tamanho)", uso=uso)
        texto = "".join(b.get("text", "") for b in corpo.get("content") or [] if isinstance(b, dict) and b.get("type") == "text")
        return Interpretacao(True, texto, modelo=str(corpo.get("model") or provedor.modelo), uso=uso)
    if provedor.tipo == "gemini":
        uso = corpo.get("usageMetadata") if isinstance(corpo.get("usageMetadata"), dict) else {}
        bloqueio = (corpo.get("promptFeedback") or {}).get("blockReason") if isinstance(corpo.get("promptFeedback"), dict) else None
        candidatos = corpo.get("candidates") if isinstance(corpo.get("candidates"), list) else []
        if bloqueio or not candidatos:
            return Interpretacao(False, mensagem=f"o Gemini bloqueou a análise ({bloqueio or 'sem resposta'})", uso=uso)
        candidato = candidatos[0] if isinstance(candidatos[0], dict) else {}
        motivo = candidato.get("finishReason")
        if motivo == "MAX_TOKENS":
            return Interpretacao(False, mensagem="resposta incompleta (limite de tamanho)", uso=uso)
        if motivo in GEMINI_RECUSAS:
            return Interpretacao(False, mensagem=f"o Gemini recusou a análise ({motivo})", uso=uso)
        partes = (candidato.get("content") or {}).get("parts") or []
        texto = "".join(p.get("text", "") for p in partes if isinstance(p, dict) and not p.get("thought"))
        return Interpretacao(True, texto, modelo=str(corpo.get("modelVersion") or provedor.modelo), uso=uso)
    uso = corpo.get("usage") if isinstance(corpo.get("usage"), dict) else {}
    escolhas = corpo.get("choices") if isinstance(corpo.get("choices"), list) else []
    if not escolhas or not isinstance(escolhas[0], dict):
        return Interpretacao(False, mensagem="resposta sem 'choices'", uso=uso)
    mensagem = escolhas[0].get("message") or {}
    if escolhas[0].get("finish_reason") == "length":
        return Interpretacao(False, mensagem="resposta incompleta (limite de tamanho)", uso=uso)
    if escolhas[0].get("finish_reason") == "content_filter" or mensagem.get("refusal"):
        return Interpretacao(False, mensagem="a IA recusou a análise", uso=uso)
    return Interpretacao(True, str(mensagem.get("content") or ""), modelo=str(corpo.get("model") or provedor.modelo), uso=uso)


def _postar(executor: Any, provedor: Provedor, montar_corpo: Callable[[Provedor, bool], tuple[dict[str, str], dict[str, Any]]],
            timeout: float, avisar: Callable[[str], None] | None, perfil: str = ANALISE,
            ao_parcial: Callable[[str], None] | None = None) -> RespostaIA:  # fmt: skip
    """POST com novas tentativas curtas em erro temporário (análise: 2 s e 5 s; chat: uma, após 2 s).

    O ``Retry-After`` manda quando cabe no teto (~20 s de esperas + respostas que falharam); não cabe → devolve e
    quem chama passa para a reserva. ``ao_parcial``: streaming; recebe o texto acumulado a cada pedaço.
    Provedor/modelo sem saída estruturada, streaming ou ajuste de raciocínio (400 citando isso): repete sem eles.
    """
    esperas, teto = (ESPERAS_CHAT_S, TETO_CHAT_S) if perfil == CHAT else (ESPERAS_S, TETO_ANALISE_S)
    simples, gasto = False, 0.0
    for tentativa in range(len(esperas) + 1):
        inicio = _relogio()
        resposta = _enviar(executor, provedor, montar_corpo, timeout, simples, perfil, ao_parcial)
        if not simples and recusou_esquema(resposta):
            simples = True
            resposta = _enviar(executor, provedor, montar_corpo, timeout, simples, perfil, ao_parcial)
        gasto += _relogio() - inicio
        if not temporario(resposta) or tentativa == len(esperas):
            return resposta
        espera = min(resposta.espera_s, ESPERA_MAXIMA_S) if resposta.espera_s else esperas[tentativa]
        if gasto + espera > teto:
            return resposta
        if avisar is not None:
            avisar(f"Serviço da IA ocupado, tentando novamente ({tentativa + 1}/{len(esperas)})...")
        _dormir(espera)
        gasto += espera
    return resposta


def _enviar(executor: Any, provedor: Provedor, montar_corpo: Callable[[Provedor, bool], tuple[dict[str, str], dict[str, Any]]],
            timeout: float, simples: bool, perfil: str, ao_parcial: Callable[[str], None] | None) -> RespostaIA:  # fmt: skip
    """Um POST. Com streaming, junta os eventos no corpo sem streaming (``fluxo.juntar``) para ``interpretar``."""
    from msscan.ia import fluxo as fl

    cabecalhos, corpo = montar_corpo(provedor, simples)
    usar_fluxo = ao_parcial is not None and not simples
    corpo = _ajustar(provedor, corpo, usar_fluxo, perfil == CHAT and not simples)
    if not usar_fluxo:
        resposta = executor.post_ia(cabecalhos, corpo, timeout=timeout, url=provedor.url)
        del cabecalhos
        return resposta
    acumulado: list[str] = []

    def ao_evento(evento: Any) -> None:
        pedaco = fl.delta(provedor.tipo, evento)
        if pedaco:
            acumulado.append(pedaco)
            try:
                ao_parcial("".join(acumulado))
            except Exception:  # noqa: BLE001 - a tela nunca derruba a resposta
                pass

    resposta = executor.post_ia(cabecalhos, corpo, timeout=timeout, url=url_fluxo(provedor), ao_evento=ao_evento)
    del cabecalhos
    if resposta.status == 200 and isinstance(resposta.corpo, dict) and "_eventos" in resposta.corpo:
        eventos = resposta.corpo["_eventos"]
        erro = fl.erro_no_fluxo(eventos)
        if erro:  # sobrecarga no meio do fluxo (ex.: overloaded_error): temporário, como um 529
            return RespostaIA(529, {"error": {"message": erro}})
        return RespostaIA(200, fl.juntar(provedor.tipo, eventos))
    return resposta


def pedir_json(executor: Any, provedor: Provedor, montar_corpo: Callable[[Provedor, bool], tuple[dict[str, str], dict[str, Any]]],
               timeout: float, avisar: Callable[[str], None] | None = None, perfil: str = ANALISE,
               ao_parcial: Callable[[str], None] | None = None) -> tuple[Interpretacao, str]:  # fmt: skip
    """Pede à IA uma resposta JSON: ``(interpretação, texto JSON sem o bloco de código)``.

    ``montar_corpo(provedor, simples)`` monta ``(cabeçalhos, corpo)``. Erro temporário: repete (``_postar``). Só na
    análise: continuou sobrecarregado ou deu limite (429) e o provedor tem outro modelo gratuito → tenta esse modelo
    com a mesma chave antes de quem chama passar para a reserva (no chat vai direto para a reserva). Chave recusada
    (401/403) nunca é repetida. ``interpretacao.duracao_s``: tempo total neste provedor.
    """
    inicio = _relogio()
    resposta = _postar(executor, provedor, montar_corpo, timeout, avisar, perfil, ao_parcial)
    usado = provedor
    if perfil != CHAT and provedor.modelo_alternativo and (temporario(resposta) or resposta.status == 429):
        if avisar is not None:
            avisar(f"Tentando outro modelo do mesmo provedor ({provedor.modelo_alternativo})...")
        usado = replace(provedor, modelo=provedor.modelo_alternativo, url=provedor.url_alternativo)
        resposta = _postar(executor, usado, montar_corpo, timeout, avisar, perfil, ao_parcial)
    resultado = interpretar(usado, resposta)
    resultado.duracao_s = round(_relogio() - inicio, 2)
    texto = resultado.texto.strip() if resultado.ok else ""
    if texto.startswith("```"):  # alguns modelos embrulham o JSON em bloco de código
        texto = texto.strip("`").removeprefix("json").strip()
    return resultado, texto
