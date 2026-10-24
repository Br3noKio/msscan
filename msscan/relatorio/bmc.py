"""Seção "Acesso ao BMC" do relatório: credenciais informadas ou não, e o que isso mudou.

Regra (CLAUDE.md 6b): sem credenciais, o relatório diz "Credenciais do BMC não informadas
pelo técnico" e lista o que foi verificado por outro método ou ficou sem verificação.
Nunca contém usuário, senha ou token.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from msscan import textos
from msscan.core.modelo import Resultado, Status
from msscan.deteccao.contexto import Contexto
from msscan.provedores.redfish import COMPONENTES_REDFISH
from msscan.redfish.acesso import ESGOTADO, MENSAGEM_PULADO, NAO_PERGUNTADO, PULADO, VALIDADO

TITULOS = {
    VALIDADO: textos.t("py_rel_bmc.credenciais_do_bmc_validadas"),
    PULADO: MENSAGEM_PULADO,
    NAO_PERGUNTADO: MENSAGEM_PULADO,
    ESGOTADO: textos.t("py_rel_bmc.credenciais_do_bmc_nao_validadas"),
}


def resumo_acesso_bmc(
    ctx: Contexto, resultados: list[Resultado], titulo_de: Callable[[str], str] = lambda n: n
) -> dict[str, Any] | None:
    """Seção do relatório; ``None`` quando não há Redfish confirmado (não havia o que perguntar)."""
    if ctx.bmc is None:
        return None
    acesso = ctx.bmc.para_dict()
    por_nome = {r.componente: r for r in resultados}
    componentes = []
    for nome in COMPONENTES_REDFISH:
        r = por_nome.get(nome)
        if r is None:
            continue
        if r.status == Status.NAO_VERIFICADO:
            como = textos.t("py_rel_bmc.sem_verificacao")
        elif r.fonte.startswith("Redfish"):
            como = textos.t("py_rel_bmc.verificado_pelo_redfish", r.fonte)
        else:
            como = textos.t("py_rel_bmc.verificado_por_outro_metodo", r.fonte)
        componentes.append({"componente": nome, "titulo": titulo_de(nome), "status": r.status.value, "como": como})
    return {**acesso, "titulo": TITULOS.get(acesso["situacao"], acesso["situacao"]), "componentes": componentes}


def formatar_acesso_bmc(secao: dict[str, Any] | None) -> str:
    """Texto do terminal/TXT."""
    if not secao:
        return ""
    linhas = ["", textos.t("py_rel_bmc.acesso_ao_bmc_redfish_em", secao.get('endereco') or '?', secao['titulo'])]
    if secao["situacao"] == VALIDADO:
        linhas.append(textos.t("py_rel_bmc.perfil_do_usuario", secao['perfil']))
    elif secao.get("mensagem") and secao["mensagem"] != secao["titulo"]:
        linhas.append(f"  {secao['mensagem']}")
    if secao["situacao"] != VALIDADO:
        linhas.append(textos.t("py_rel_bmc.itens_que_teriam_o_redfish"))
    for item in secao["componentes"]:
        linhas.append(f"    - {item['titulo']}: {item['como']}")
    return "\n".join(linhas)
