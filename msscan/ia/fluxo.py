"""Respostas da IA em streaming (SSE): texto parcial para a tela e a resposta final no formato sem streaming.

Formatos conferidos na documentação oficial (30/09/2026):
- Anthropic (``"stream": true``): ``content_block_delta`` com ``delta.type = "text_delta"``; ``message_delta`` traz
  ``stop_reason``; ``message_start`` traz o modelo; ``error`` em sobrecarga no meio do fluxo.
- Gemini (``:streamGenerateContent?alt=sse``): cada evento é um ``GenerateContentResponse`` parcial.
- Compatível com OpenAI (Groq, OpenRouter...; ``"stream": true``): ``choices[0].delta.content``, fim em ``[DONE]``.
Funções puras: eventos (JSON já decodificado) → texto / corpo.
"""

from __future__ import annotations

import json
import re
from typing import Any


def delta(tipo: str, evento: Any) -> str:
    """Texto novo trazido por um evento (``""`` se não trouxer texto)."""
    if not isinstance(evento, dict):
        return ""
    if tipo == "anthropic":
        d = evento.get("delta") if evento.get("type") == "content_block_delta" else None
        return str(d.get("text") or "") if isinstance(d, dict) and d.get("type") == "text_delta" else ""
    if tipo == "gemini":
        candidatos = evento.get("candidates") if isinstance(evento.get("candidates"), list) else []
        if not candidatos or not isinstance(candidatos[0], dict):
            return ""
        partes = (candidatos[0].get("content") or {}).get("parts") or []
        return "".join(str(p.get("text") or "") for p in partes if isinstance(p, dict) and not p.get("thought"))
    escolhas = evento.get("choices") if isinstance(evento.get("choices"), list) else []
    if not escolhas or not isinstance(escolhas[0], dict):
        return ""
    return str((escolhas[0].get("delta") or {}).get("content") or "")


def erro_no_fluxo(eventos: list[Any]) -> str:
    """Erro enviado no meio do fluxo (ex.: ``overloaded_error`` da Anthropic) → mensagem; senão ``""``."""
    for evento in eventos:
        if isinstance(evento, dict) and (evento.get("type") == "error" or "error" in evento):
            erro = evento.get("error")
            return json.dumps(erro, ensure_ascii=False)[:300] if erro else "erro no fluxo"
    return ""


def juntar(tipo: str, eventos: list[Any]) -> dict[str, Any]:
    """Eventos → corpo equivalente ao da resposta sem streaming do mesmo provedor (para ``interpretar``)."""
    texto = "".join(delta(tipo, e) for e in eventos)
    dicts = [e for e in eventos if isinstance(e, dict)]
    if tipo == "anthropic":
        corpo: dict[str, Any] = {"content": [{"type": "text", "text": texto}], "stop_reason": None, "usage": {}}
        for e in dicts:
            if e.get("type") == "message_start" and isinstance(e.get("message"), dict):
                corpo["model"] = e["message"].get("model")
                corpo["usage"] = dict(e["message"].get("usage") or {})
            if e.get("type") == "message_delta":
                corpo["stop_reason"] = (e.get("delta") or {}).get("stop_reason") or corpo["stop_reason"]
                corpo["usage"].update(e.get("usage") or {})
        return corpo
    if tipo == "gemini":
        final: dict[str, Any] = {}
        motivo, bloqueio = None, None
        for e in dicts:
            for c in e.get("candidates") or []:
                if isinstance(c, dict) and c.get("finishReason"):
                    motivo = c["finishReason"]
            if isinstance(e.get("promptFeedback"), dict) and e["promptFeedback"].get("blockReason"):
                bloqueio = e["promptFeedback"]
            final.update({k: e[k] for k in ("usageMetadata", "modelVersion") if k in e})
        corpo = {**final, "candidates": [{"content": {"parts": [{"text": texto}]}, "finishReason": motivo}] if (texto or motivo) else []}
        if bloqueio:
            corpo["promptFeedback"] = bloqueio
        return corpo
    motivo, modelo, uso = None, None, {}
    for e in dicts:
        modelo = e.get("model") or modelo
        uso = e.get("usage") or uso
        for c in e.get("choices") or []:
            if isinstance(c, dict) and c.get("finish_reason"):
                motivo = c["finish_reason"]
    return {"model": modelo, "usage": uso, "choices": [{"message": {"content": texto}, "finish_reason": motivo}]}


def _string_parcial(texto: str, inicio: int) -> str:
    """Decodifica uma string JSON a partir de ``inicio`` (depois da aspa) até a aspa final ou o fim do texto."""
    saida, i = [], inicio
    escapes = {"n": "\n", "t": "\t", "r": "", '"': '"', "\\": "\\", "/": "/", "b": "", "f": ""}
    while i < len(texto):
        c = texto[i]
        if c == '"':
            break
        if c == "\\":
            if i + 1 >= len(texto):
                break
            proximo = texto[i + 1]
            if proximo == "u":
                if i + 6 > len(texto):
                    break
                try:
                    saida.append(chr(int(texto[i + 2:i + 6], 16)))
                except ValueError:
                    pass
                i += 6
                continue
            saida.append(escapes.get(proximo, proximo))
            i += 2
            continue
        saida.append(c)
        i += 1
    return "".join(saida)


def campos_parciais(texto: str, campo: str) -> list[str]:
    """Valores (mesmo incompletos) de todas as ocorrências de ``"campo": "..."`` num JSON ainda sendo escrito."""
    return [_string_parcial(texto, m.end()) for m in re.finditer(r'"' + re.escape(campo) + r'"\s*:\s*"', texto)]


def texto_parcial_chat(texto: str) -> str:
    """O que já dá para mostrar da resposta do chat (campo ``resposta``)."""
    valores = campos_parciais(texto, "resposta")
    return valores[0] if valores else ""


def texto_parcial_analise(texto: str) -> str:
    """Resumo e títulos dos problemas já escritos pela IA na análise."""
    resumo = campos_parciais(texto, "resumo")
    titulos = campos_parciais(texto, "titulo")
    linhas = [resumo[0]] if resumo else []
    linhas += [f"• {t}" for t in titulos if t]
    return "\n".join(linhas)


__all__ = ["campos_parciais", "delta", "erro_no_fluxo", "juntar", "texto_parcial_analise", "texto_parcial_chat"]
