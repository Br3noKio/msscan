"""Catálogo FECHADO de ações corretivas (CLAUDE.md 10b; implementação na fase 10 do PLANO.md).

A IA só pode RECOMENDAR uma ação pelo identificador; nunca gera comandos. Identificador fora deste
catálogo é ignorado e sinalizado. Cada ação tem pré-checagens (só leitura), confirmação digitada do técnico
(``CORRIGIR``, numa tela só, CLAUDE.md 18.2) e registro no relatório. Nenhuma é executada automaticamente.
Desde 01/10/2026 (CLAUDE.md 18.4): a atualização automática nunca muda a rede do iDRAC; a única exceção é a ação AVULSA
``idrac.ip_fixo`` (pedido do usuário, CLAUDE.md 21.5), com ``CORRIGIR``. Pass-through e usuário temporário
são só mudanças TEMPORÁRIAS feitas pela atualização automática (``atualizacao/automatica.py``), desfeitas no fim.
Implementação de cada ação: ``msscan/acoes/<modulo>.py``; despacho comum em ``msscan/acoes/execucao.py``.
"""

from __future__ import annotations

from typing import Any

DISPONIVEL = "disponível no MSscan (com prévia e confirmação do técnico)"

CATALOGO: dict[str, dict[str, Any]] = {
    "bmc.relogio_ntp": {
        "titulo": "Acertar o relógio do BMC via NTP",
        "descricao": "Configura o BMC para sincronizar o relógio por NTP (datas erradas no log do BMC e em certificados).",
        "situacao": DISPONIVEL,
        "modulo": "ntp",
        "requer": "Redfish com credenciais de administrador do BMC",
    },
    "bmc.sel_exportar_limpar": {
        "titulo": "Exportar e limpar o log do BMC (SEL)",
        "descricao": "Exporta o SEL para o pendrive e depois o limpa (log cheio ou com eventos antigos já resolvidos).",
        "situacao": DISPONIVEL,
        "modulo": "sel",
        "requer": "Redfish com credenciais de administrador do BMC ou IPMI local; pendrive gravável",
    },
    "idrac.ver": {
        "titulo": "Ver a configuração do iDRAC",
        "descricao": "Mostra IP, DHCP/fixo, máscara, gateway, porta, usuários (sem senhas), pass-through e Redfish. Só leitura.",
        "situacao": DISPONIVEL,
        "modulo": "idrac",
        "requer": "IPMI local (/dev/ipmi0) ou racadm em vendor/",
    },
    "idrac.ip_fixo": {
        "titulo": "Colocar um IP fixo no iDRAC",
        "descricao": "IP, máscara e gateway fixos no iDRAC (ex.: DHCP sem resposta), testando antes se o IP está livre. "
                     "Pode ser desfeito.",
        "situacao": DISPONIVEL,
        "modulo": "idrac",
        "requer": "IPMI local (/dev/ipmi0) ou racadm em vendor/; pendrive gravável",
    },
}


def acao(identificador: str | None) -> dict[str, Any] | None:
    """Entrada do catálogo, ou ``None`` se o identificador não existir."""
    return CATALOGO.get((identificador or "").strip())


def descrever_para_ia() -> str:
    """Lista enviada à IA: só identificador e o que a ação faz."""
    return "\n".join(f"- {ident}: {dados['titulo']}. {dados['descricao']}" for ident, dados in CATALOGO.items())


def recomendadas(relatorio: dict[str, Any] | None) -> dict[str, list[str]]:
    """Ações recomendadas pela IA neste relatório: ``{identificador: [títulos dos problemas]}`` (só do catálogo)."""
    ia = (relatorio or {}).get("interpretacao_ia") or {}
    saida: dict[str, list[str]] = {}
    for problema in ia.get("problemas") or []:
        plano = problema.get("plano") if isinstance(problema, dict) else None
        identificador = (plano or {}).get("acao_catalogo")
        if identificador in CATALOGO:
            saida.setdefault(identificador, []).append(str(problema.get("titulo") or ""))
    return saida
