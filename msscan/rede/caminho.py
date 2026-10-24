"""Consertos automáticos do caminho até a internet (CLAUDE.md 28.1), só quando o HTTPS falhou.

- **DNS da rede falhou** e um DNS público responde direto (UDP 53): a placa passa a usar também os DNS alternativos
  (``nmcli device modify``: só nesta sessão, nada é gravado no perfil).
- **Várias placas com IP**: cada uma é testada (conexão TCP à porta 443 presa à placa, ``SO_BINDTODEVICE``); a primeira
  que alcança a internet vira a rota padrão (métrica menor, também só nesta sessão).

Tudo o que muda passa pelo executor (fica no histórico). Replay e simulação não mexem em nada.
"""

from __future__ import annotations

import random
import socket
import struct
from collections.abc import Callable
from typing import Any

DNS_ALTERNATIVOS = ("1.1.1.1", "8.8.8.8", "9.9.9.9")
NOME_TESTE = "downloads.dell.com"
TEMPO_S = 3.0
METRICA_PREFERIDA = 10


def consultar_dns(servidor: str, nome: str = NOME_TESTE, tempo: float = TEMPO_S) -> bool:
    """``True`` se ``servidor`` (IP) responde a uma consulta A de ``nome`` com pelo menos um endereço."""
    ident = random.randrange(1 << 16)
    pergunta = b"".join(bytes([len(p)]) + p.encode() for p in nome.split(".")) + b"\0" + struct.pack("!HH", 1, 1)
    pacote = struct.pack("!HHHHHH", ident, 0x0100, 1, 0, 0, 0) + pergunta
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(tempo)
            s.sendto(pacote, (servidor, 53))
            resposta = s.recv(1024)
    except OSError:
        return False
    if len(resposta) < 12:
        return False
    rid, flags, _, respostas = struct.unpack("!HHHH", resposta[:8])
    return rid == ident and flags & 0x000F == 0 and respostas > 0


def alcanca_internet(placa: str, host: str = NOME_TESTE, tempo: float = TEMPO_S) -> bool:
    """Conexão TCP à porta 443 de ``host`` saindo só pela ``placa`` (``SO_BINDTODEVICE``, precisa de root)."""
    try:
        enderecos = [e[4][0] for e in socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)]
    except (OSError, UnicodeError):
        return False
    for endereco in enderecos[:2]:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, placa.encode() + b"\0")
                s.settimeout(tempo)
                s.connect((endereco, 443))
                return True
        except OSError:
            continue
    return False


def usar_dns_alternativo(executor: Any, placas: list[str], dns: Callable[[str], bool] | None = None) -> list[str]:
    """DNS da rede falhou: se um DNS público responde, as ``placas`` passam a usá-lo. Devolve o que foi feito (para os detalhes)."""
    if getattr(executor, "origem", "real") != "real" or not placas:
        return []
    publicos = [s for s in DNS_ALTERNATIVOS if (dns or consultar_dns)(s)]
    if not publicos:
        return ["DNS alternativos (1.1.1.1, 8.8.8.8, 9.9.9.9) também não responderam: a rede bloqueia DNS externo."]
    feitos = []
    for placa in placas:
        rc, _, erro = executor.rodar(["nmcli", "device", "modify", placa, "ipv4.dns", ",".join(publicos)], timeout=15)
        feitos.append(f"{placa}: DNS alternativo {', '.join(publicos)} em uso (só nesta sessão)" if rc == 0 else
                      f"{placa}: não foi possível usar o DNS alternativo ({erro.strip() or rc})")  # fmt: skip
    return feitos


def escolher_placa(executor: Any, placas: list[str], testar: Callable[[str], bool] | None = None) -> tuple[str | None, list[str]]:
    """Testa cada placa com IP; a primeira que alcança a internet vira a rota padrão. Devolve (placa, detalhes)."""
    if getattr(executor, "origem", "real") != "real" or len(placas) < 2:
        return None, []
    detalhes = []
    for placa in placas:
        if not (testar or alcanca_internet)(placa):
            detalhes.append(f"{placa}: sem saída para a internet")
            continue
        rc, _, erro = executor.rodar(["nmcli", "device", "modify", placa, "ipv4.route-metric", str(METRICA_PREFERIDA)], timeout=15)
        if rc != 0:
            detalhes.append(f"{placa}: alcança a internet, mas não foi possível torná-la a rota padrão ({erro.strip() or rc})")
            continue
        detalhes.append(f"{placa}: alcança a internet; passou a ser a rota padrão (só nesta sessão)")
        return placa, detalhes
    return None, detalhes


__all__ = ["DNS_ALTERNATIVOS", "alcanca_internet", "consultar_dns", "escolher_placa", "usar_dns_alternativo"]
