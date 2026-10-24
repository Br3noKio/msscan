"""Comparação de versões de firmware. Na dúvida, "incerta": nunca oferecer (CLAUDE.md 11.4).

Nunca downgrade: versão instalada mais nova que a do catálogo nunca vira "atualização".
"""

from __future__ import annotations

import re

ATUALIZACAO = "atualizacao"
IGUAL = "igual"
INSTALADA_MAIS_NOVA = "instalada_mais_nova"
INCERTA = "incerta"

_RE_NUMERICA = re.compile(r"^\d+(?:[.\-]\d+)*$")


def _partes(versao: str) -> tuple[int, ...] | None:
    texto = versao.strip()
    if not _RE_NUMERICA.match(texto):
        return None
    return tuple(int(p) for p in re.split(r"[.\-]", texto))


def comparar_versoes(instalada: str | None, disponivel: str | None, instalada_truncada: bool = False) -> tuple[str, str]:
    """``(situação, motivo)`` comparando a versão instalada com a do catálogo.

    Só versões puramente numéricas (``2.21.2``, ``52.30.0-6753``) são comparadas. ``instalada_truncada``:
    a fonte informa só o começo da versão (ex.: iDRAC "7.00" pelo IPMI); então só dá para afirmar
    atualização quando o começo do catálogo já é maior.
    """
    if not instalada or not disponivel:
        return INCERTA, "versão instalada ou disponível desconhecida"
    if instalada.strip() == disponivel.strip():
        return IGUAL, "mesma versão"
    a, b = _partes(instalada), _partes(disponivel)
    if a is None or b is None:
        return INCERTA, f"formatos não comparáveis ('{instalada}' × '{disponivel}')"
    if instalada_truncada:
        prefixo = b[: len(a)]
        if prefixo > a:
            return ATUALIZACAO, "versão do catálogo mais nova (comparado o início da versão)"
        if prefixo < a:
            return INSTALADA_MAIS_NOVA, "versão instalada mais nova que a do catálogo"
        return INCERTA, f"a fonte informa só '{instalada}': não dá para saber se '{disponivel}' é mais nova"
    tamanho = max(len(a), len(b))
    a, b = a + (0,) * (tamanho - len(a)), b + (0,) * (tamanho - len(b))
    if b > a:
        return ATUALIZACAO, "versão do catálogo mais nova"
    if b < a:
        return INSTALADA_MAIS_NOVA, "versão instalada mais nova que a do catálogo (nunca downgrade)"
    return IGUAL, "mesma versão"
