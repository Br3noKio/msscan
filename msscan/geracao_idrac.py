"""Geração do PowerEdge → iDRAC e o que se sabe de cada um (CLAUDE.md 28.2; fontes em ``docs/compatibilidade.md``).

Usado nas mensagens de acesso ao iDRAC: qual iDRAC é, se tem Redfish e, quando nenhuma camada de rede funciona, o firmware
mínimo recomendado e como atualizá-lo pela página web do iDRAC. Banco é dica, detecção é verdade: nada aqui libera ou
bloqueia acesso; a conexão é sempre tentada.
"""

from __future__ import annotations

import re
from typing import Any

# Nome Dell: classe + geração + ... (R610 = 11ª, R530 = 13ª, R640 = 14ª, R6515 = 15ª, R670 = 17ª).
_RE_MODELO = re.compile(r"\b(?:PowerEdge\s+)?(?:[A-Z]{1,3})(\d)(\d)\d{1,2}[a-z]{0,3}\d?\b")

MATRIZ: dict[str, dict[str, Any]] = {
    # Conferido nas páginas da Dell em 14/10/2026 (links em docs/compatibilidade.md). "minimo" = o que recomendar quando
    # nenhuma camada de rede funciona: a última versão publicada da geração.
    "iDRAC6": {"geracoes": (11,), "redfish": False,
               "tls": "não aceita só TLS 1.2; na 2.92 dá para desligar o TLS 1.0 (TLS 1.1 e superior)",
               "minimo": "2.92 (servidores rack/torre) ou 3.85 (blade), as últimas", "ultimo": "2.92 / 3.85"},
    "iDRAC7": {"geracoes": (12,), "redfish": True,
               "tls": "a partir da 2.40.40.40: TLS configurável, padrão 1.1 e superior; firmwares antigos com cifras fracas",
               "minimo": "2.65.65.65 (a última; Redfish a partir da 2.40.40.40)", "ultimo": "2.65.65.65"},
    "iDRAC8": {"geracoes": (13,), "redfish": True,
               "tls": "a partir da 2.40.40.40: TLS configurável, padrão 1.1 e superior; a 2.30 do R530 oferece DH de 1024 bits",
               "minimo": "2.86.86.86 (a última; Redfish a partir da 2.40.40.40)", "ultimo": "2.86.86.86"},
    "iDRAC9": {"geracoes": (14, 15, 16), "redfish": True, "tls": "TLS 1.2 (1.1 configurável); TLS 1.3 a partir da 5.10.00.00",
               "minimo": "14ª: 7.00.00.183 (mínimo 7.00.00.173); 15ª/16ª: 7.30.30.51", "ultimo": "7.00.00.183 / 7.30.30.51"},
    "iDRAC10": {"geracoes": (17,), "redfish": True, "tls": "TLS 1.2/1.3 (não conferido na documentação)",
                "minimo": "1.30.31.10 (a primeira publicada foi a 1.10.17.00)", "ultimo": "1.30.31.10"},
}


EXCECOES = {"C4130": 13, "C4140": 14, "XE7100": 14, "XR11": 15, "XR12": 15}
"""Modelos fora do padrão "segundo dígito = geração" (a geração está noutro dígito ou o nome tem só dois)."""
_RE_EXCECAO = re.compile(r"\b(" + "|".join(EXCECOES) + r")\b", re.IGNORECASE)


def geracao(modelo: str | None) -> int | None:
    """Geração do PowerEdge pelo nome do modelo (``None`` se o nome não segue o padrão Dell de 11ª a 17ª)."""
    excecao = _RE_EXCECAO.search(modelo or "")
    if excecao:
        return EXCECOES[excecao.group(1).upper()]
    achado = _RE_MODELO.search(modelo or "")
    if not achado:
        return None
    numero = 10 + int(achado.group(2))
    return numero if 11 <= numero <= 17 else None


def idrac(numero: int | None) -> str | None:
    """Nome do iDRAC da geração (``iDRAC8`` na 13ª)."""
    return next((nome for nome, dados in MATRIZ.items() if numero in dados["geracoes"]), None)


def tem_redfish(modelo: str | None) -> bool | None:
    """Se a geração tem Redfish em alguma versão do iDRAC (iDRAC6: nunca); ``None`` se a geração é desconhecida."""
    nome = idrac(geracao(modelo))
    return MATRIZ[nome]["redfish"] if nome else None


def recomendacao_firmware(modelo: str | None) -> str:
    """Texto do 28.2: firmware mínimo do iDRAC desta geração e como atualizar pela página web dele ("" se desconhecida)."""
    numero = geracao(modelo)
    nome = idrac(numero)
    if nome is None:
        return ""
    return (f"Este servidor é da {numero}ª geração ({nome}). Firmware do iDRAC recomendado: {MATRIZ[nome]['minimo']}. "
            "Para atualizar pela página web do iDRAC: abra https://<IP do iDRAC> num navegador de outro computador, entre como "
            "administrador e use Manutenção (Maintenance) > Atualização do sistema (System Update) ou, nos iDRAC6/7/8, "
            "Visão geral > Configurações do iDRAC > Atualização e reversão, enviando o pacote do iDRAC baixado do site da Dell.")


__all__ = ["MATRIZ", "geracao", "idrac", "recomendacao_firmware"]
