"""Liberação da gravação real de firmware por servidor (pedidos do usuário de 06/10 e 09/10/2026).

- 06/10/2026: só o R640 do usuário e só as placas de rede (teste T3-1, concluído com sucesso).
- 09/10/2026: TODOS os componentes liberados neste R640, na ordem placas de rede → demais → iDRAC sozinho → BIOS por
  último, parando na primeira falha. As pré-checagens, o ``ATUALIZAR``, o progresso, a faixa de segurança e a retomada
  continuam valendo. A memória deixou de ser bloqueio fixo: a configuração é conferida a cada diagnóstico
  (``aviso._montagem_memoria``) e, fora do suportado, vira risco que o técnico pode assumir.

- 09/10/2026 (2ª parte): o primeiro ciclo completo neste R640 (placas de rede, iDRAC e BIOS) foi concluído com sucesso;
  pela regra da 1ª parte, a gravação real fica liberada em QUALQUER servidor Dell, com todas as proteções. Fabricante
  conferido agora pelo ``dmidecode -s system-manufacturer`` (só leitura); outro fabricante ou leitura falhou → bloqueado.
  Conferido em dois lugares: no "Plano de atualização" (tela e modo texto) e de novo no início de ``aplicar_automatico``
  (catálogo, pacote manual e retomada passam por ali), antes de qualquer mudança no iDRAC.

Como nas travas ``GRAVACAO_REAL_LIBERADA``/``ACOES_REAIS_LIBERADAS``, os alvos simulados dos testes (``alvo_simulado`` e
origem diferente de "real") não passam por esta restrição. Ampliar ou retirar: commit próprio, só com aprovação do usuário.
"""

from __future__ import annotations

from typing import Any

from msscan import textos

RESTRICAO_ATIVA = True
"""Desligada só pela suíte de testes (``tests/conftest.py``), como as travas; nunca no código."""
CATEGORIAS_LIBERADAS: frozenset[str] | None = None
"""``None`` = todas as categorias (09/10/2026). Uma lista restringe de novo (ex.: ``{"network"}``, como em 06/10/2026)."""
TEXTO_LIBERADO = textos.t("py_liberacao.liberado")
TEXTO_BLOQUEADO = textos.t("py_liberacao.bloqueado")


def _simulado(executor: Any) -> bool:
    if not RESTRICAO_ATIVA:
        return True
    return bool(getattr(executor, "alvo_simulado", False)) and getattr(executor, "origem", "real") != "real"


def fabricante(executor: Any) -> str:
    """Fabricante lido agora (só leitura). ``""`` se não foi possível ler."""
    try:
        rc, saida, _ = executor.rodar(["dmidecode", "-s", "system-manufacturer"], timeout=15)
    except Exception:  # noqa: BLE001 - sem leitura = servidor não liberado
        return ""
    return (saida or "").strip() if rc == 0 else ""


def servidor_liberado(executor: Any) -> bool:
    if _simulado(executor):
        return True
    return fabricante(executor).lower().startswith("dell")


def categoria_liberada(item: dict[str, Any]) -> bool:
    return CATEGORIAS_LIBERADAS is None or str(item.get("categoria") or "").strip().lower() in CATEGORIAS_LIBERADAS


def avaliar(executor: Any, itens: list[dict[str, Any]]) -> dict[str, Any]:
    """O que pode ser gravado agora. ``bloqueios``: um por motivo (vazio = tudo liberado)."""
    if _simulado(executor):
        return {"restrita": False, "servidor": True, "liberados": [i.get("componente") for i in itens], "bloqueados": [],
                "bloqueios": [], "texto_liberado": "", "texto_bloqueado": ""}  # fmt: skip
    servidor = servidor_liberado(executor)
    bloqueados = [i for i in itens if not servidor or not categoria_liberada(i)]
    bloqueios = []
    if not servidor:
        bloqueios.append(textos.t("py_liberacao.servidor_bloqueado"))
    elif bloqueados:
        nomes = ", ".join(f"{i.get('componente')} ({i.get('categoria') or textos.t('py_liberacao.categoria_desconhecida')})" for i in bloqueados)
        bloqueios.append(textos.t("py_liberacao.componentes_bloqueados", nomes))
    return {"restrita": True, "servidor": servidor, "liberados": [i.get("componente") for i in itens if i not in bloqueados],
            "bloqueados": [i.get("componente") for i in bloqueados], "bloqueios": bloqueios,
            "texto_liberado": TEXTO_LIBERADO if servidor else textos.t("py_liberacao.nenhum"),
            "texto_bloqueado": TEXTO_BLOQUEADO}  # fmt: skip
