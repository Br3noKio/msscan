"""Parser de variáveis UEFI lidas com ``od -An -t u1`` (ex.: ``SecureBoot``)."""

from __future__ import annotations

from msscan.parsers import ErroParser


def parse_od_booleano_efi(texto: str) -> bool:
    """Valor booleano de uma variável UEFI de 1 byte (``SecureBoot``, ``SetupMode``).

    O arquivo em ``/sys/firmware/efi/efivars`` traz 4 bytes de atributos e depois
    o valor: ``   6   0   0   0   1`` = ligado; ``... 0`` = desligado.
    """
    try:
        valores = [int(v) for v in texto.split()]
    except ValueError as erro:
        raise ErroParser(f"saída do 'od' não numérica: {texto.strip()[:40]!r}") from erro
    if len(valores) != 5 or valores[4] not in (0, 1):
        raise ErroParser(f"variável UEFI booleana inesperada: {valores}")
    return valores[4] == 1
