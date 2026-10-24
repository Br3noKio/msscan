"""Parser da saída de ``nmcli -t -f DHCP4 device show <interface>`` (NetworkManager).

Formato conferido numa saída real (R640, 28/09/2026; fixture ``nmcli__-t__-f__DHCP4__device__show__eno3``):
``DHCP4.OPTION[n]:<opção> = <valor>``, uma opção por linha. Listas (``domain_name_servers``) vêm separadas por espaço.
TODO(fixture): rede real cujo DHCP entrega a opção 42 (``ntp_servers``); a fixture do R640 só tem o pedido
(``requested_ntp_servers = 1``), sem resposta.
"""

from __future__ import annotations

import re

_LINHA = re.compile(r"^DHCP4\.OPTION\[\d+\]:\s*([A-Za-z0-9_]+)\s*=\s*(.*?)\s*$")


def parse_dhcp4_opcoes(texto: str) -> dict[str, str]:
    """Opções do DHCPv4 recebidas pela interface: ``{opção: valor}``. Linhas fora do formato são ignoradas."""
    opcoes: dict[str, str] = {}
    for linha in (texto or "").splitlines():
        casado = _LINHA.match(linha)
        if casado:
            opcoes.setdefault(casado.group(1), casado.group(2))
    return opcoes


def servidores_ntp_dhcp(opcoes: dict[str, str]) -> list[str]:
    """Servidores NTP entregues pelo DHCP (opção ``ntp_servers``), na ordem, sem repetição."""
    return list(dict.fromkeys((opcoes.get("ntp_servers") or "").split()))
