"""Seções "Conversa com a IA" (CLAUDE.md 10e) e "Chave da IA pelo celular" (10d) do relatório.

A conversa guarda só textos sanitizados (o que foi de fato enviado à IA) e nunca chaves; evidências que não existem
nos dados ficam marcadas. O pareamento do celular registra só que houve (quando, IP do celular, resultado) — nunca a
chave.
"""

from __future__ import annotations

import textwrap
from html import escape
from typing import Any

from msscan import textos
from msscan.ia.chat import RE_MARCADOR_EVIDENCIA, texto_com_marcas

RESULTADOS_PAREAMENTO = {
    "aceita": textos.t("py_rel_chat.chave_recebida_e_aceita_pelo"), "recusada": textos.t("py_rel_chat.chave_recebida_e_recusada_pelo"),
    "expirada": textos.t("py_rel_chat.expirou_sem_envio_5_minutos"), "encerrada": textos.t("py_rel_chat.encerrada_pelo_tecnico_tela_fechada"),
    "tentativas": textos.t("py_rel_chat.encerrada_tentativas_invalidas_demais"), "erro": textos.t("py_rel_chat.nao_iniciou"),
}  # fmt: skip


def _autor(mensagem: dict[str, Any]) -> str:
    if mensagem.get("papel") == "tecnico":
        return textos.t("py_rel_chat.tecnico")
    if mensagem.get("papel") == "sistema":
        return "MSscan"
    origem = mensagem.get("provedor_titulo") or "IA"
    papel = mensagem.get("papel_ia") if mensagem.get("papel_ia") in ("principal", "reserva") else ""
    return f"IA ({origem}{', ' + papel if papel else ''}{', ' + mensagem['modelo'] if mensagem.get('modelo') else ''})"


def _evidencia(e: dict[str, Any]) -> str:
    """"[E1] componente: “trecho”", ou o aviso de que a IA citou uma evidência que não veio nos dados."""
    if not e.get("componente") and not e.get("trecho"):
        return textos.t("py_rel_chat.citada_pela_ia_mas_nao", e.get('id'))
    return f"[{e.get('id')}] {e.get('componente') or '?'}: “{e.get('trecho') or ''}”"


def formatar_mensagem(mensagem: dict[str, Any], largura: int = 76) -> list[str]:
    """Linhas de uma mensagem (texto com as evidências não confirmadas marcadas, evidências, ações e avisos)."""
    linhas: list[str] = []
    for paragrafo in texto_com_marcas(mensagem).splitlines() or [""]:
        linhas += textwrap.wrap(paragrafo, largura, initial_indent="  ", subsequent_indent="  ") or [""]
    for evidencia in mensagem.get("evidencias") or []:
        situacao = "confirmada" if evidencia.get("confirmada") else textos.t("py_rel_chat.nao_confirmada_pelos_dados")
        linhas += textwrap.wrap(f"{_evidencia(evidencia)} — {situacao}",
                                largura, initial_indent="    ", subsequent_indent="      ")  # fmt: skip
    for acao in mensagem.get("acoes") or []:
        linhas += textwrap.wrap(textos.t("py_rel_chat.acao_do_catalogo_recomendada", acao['id'], acao['titulo']), largura,
                                initial_indent="    ", subsequent_indent="      ")  # fmt: skip
    for aviso in mensagem.get("avisos") or []:
        linhas += textwrap.wrap(f"Aviso: {aviso}", largura, initial_indent="    ", subsequent_indent="      ")
    return linhas


def formatar_chat(chat: dict[str, Any] | None) -> str:
    """Texto do terminal/TXT (vazio se não houve conversa além da mensagem inicial)."""
    mensagens = (chat or {}).get("mensagens") or []
    if len(mensagens) < 2:
        return ""
    linhas = ["", textos.t("py_rel_chat.conversa_com_a_ia_chat"), "-" * 78]
    for mensagem in mensagens:
        linhas.append(f"[{str(mensagem.get('quando') or '')[11:16]}] {_autor(mensagem)}:")
        linhas += formatar_mensagem(mensagem)
    return "\n".join(linhas)


def _html_texto(mensagem: dict[str, Any]) -> str:
    """Texto escapado; cada marcador [E1] vira etiqueta verde (confirmada) ou vermelha (não confirmada)."""
    confirmadas = {e.get("id"): e.get("confirmada") for e in mensagem.get("evidencias") or []}

    def etiqueta(casado: Any) -> str:
        ok = confirmadas.get(casado.group(1))
        return f'<span class="{"ev-ok" if ok else "ev-falha"}">[{casado.group(1)}{"" if ok else " — não confirmada"}]</span>'

    return RE_MARCADOR_EVIDENCIA.sub(etiqueta, escape(str(mensagem.get("texto") or ""))).replace("\n", "<br>")


def html_chat(chat: dict[str, Any] | None) -> str:
    """Bloco HTML da conversa (vazio se não houve conversa além da mensagem inicial)."""
    mensagens = (chat or {}).get("mensagens") or []
    if len(mensagens) < 2:
        return ""
    partes = [(f"<h2>{textos.t('py_rel_chat.conversa_com_a_ia_chat')}</h2><p class=\"sub\">"
               f"{textos.t('py_rel_chat.textos_como_foram_enviados_sem')}</p>")]  # fmt: skip
    for mensagem in mensagens:
        evidencias = "".join(
            f"<li>{escape(_evidencia(e))} — "
            f"{'confirmada' if e.get('confirmada') else '<strong>não confirmada pelos dados</strong>'}</li>"
            for e in mensagem.get("evidencias") or [])  # fmt: skip
        extras = "".join((f"<li>{textos.t('py_rel_chat.acao_do_catalogo_recomendada_2')} <code>"
                          f"{escape(a['id'])}</code> — {escape(a['titulo'])}</li>")
                         for a in mensagem.get("acoes") or [])  # fmt: skip
        extras += "".join(f"<li>Aviso: {escape(str(a))}</li>" for a in mensagem.get("avisos") or [])
        partes.append(f'<div class="cartao"><p><strong>{escape(_autor(mensagem))}</strong> <small>{escape(str(mensagem.get("quando") or ""))}</small></p>'
                      f"<p>{_html_texto(mensagem)}</p>" + (f"<ul>{evidencias}{extras}</ul>" if evidencias or extras else "") + "</div>")  # fmt: skip
    return "\n".join(partes)


def formatar_pareamentos(pareamentos: list[dict[str, Any]] | None) -> str:
    """Texto do terminal/TXT: que houve pareamento do celular (sem a chave)."""
    if not pareamentos:
        return ""
    linhas = ["", textos.t("py_rel_chat.chave_da_ia_enviada_pelo"), "-" * 78]
    for p in pareamentos:
        linhas.append(f"{p.get('inicio', '?')} → {p.get('fim', '?')}: {p.get('ia_nome') or p.get('ia')} — "
                      f"{RESULTADOS_PAREAMENTO.get(p.get('resultado'), p.get('resultado'))}")  # fmt: skip
        if p.get("ip_celular"):
            linhas.append(textos.t("py_rel_chat.celular_tentativas_invalidas_a_chave", p['ip_celular'], p.get('tentativas_invalidas', 0)))
    return "\n".join(linhas)


def html_pareamentos(pareamentos: list[dict[str, Any]] | None) -> str:
    if not pareamentos:
        return ""
    itens = "".join(
        f"<li>{escape(str(p.get('inicio', '?')))} → {escape(str(p.get('fim', '?')))}: {escape(str(p.get('ia_nome') or p.get('ia')))} — "
        f"{escape(RESULTADOS_PAREAMENTO.get(p.get('resultado'), str(p.get('resultado'))))}"
        + (textos.t("py_rel_chat.celular_tentativas_invalidas", escape(str(p['ip_celular'])), int(p.get('tentativas_invalidas', 0))) if p.get("ip_celular") else "")
        + "</li>" for p in pareamentos)  # fmt: skip
    return (f"<h2>{textos.t('py_rel_chat.chave_da_ia_enviada_pelo_2')}</h2><p class=\"sub\">"
            f"{textos.t('py_rel_chat.so_o_registro_do_pareamento')}</p><ul>{itens}</ul>")
