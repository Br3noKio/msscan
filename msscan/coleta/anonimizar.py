"""Remove identificadores de uma pasta de coleta antes de versioná-la.

Uso: ``python3 -m msscan.coleta.anonimizar <pasta>``

- Seriais, asset tags, UUID, service tag, WWN → ``REMOVIDO``;
- MAC → ``02:00:00:00:00:NN`` (faixa administrada localmente);
- IPv4 → ``192.0.2.NN`` e IPv6 → ``2001:db8::NN`` (faixas de documentação);
- UUID solto no texto (ex.: ``root=UUID=...``) → UUID zerado numerado.

A troca é consistente dentro da pasta (o mesmo MAC vira sempre o mesmo
substituto) e preserva o formato, para os parsers continuarem funcionando.
Valores genéricos ("Not Specified"), loopback e broadcast são mantidos.
"""

from __future__ import annotations

import ipaddress
import json
import re
import sys
from pathlib import Path
from typing import Any

from msscan.parsers.dmidecode import valor_util

REMOVIDO = "REMOVIDO"
CHAVES_TEXTO = (
    "Serial Number", "Serial No", "Asset Tag", "UUID", "Service Tag", "Chassis Serial", "Board Serial",
    "Product Serial", "WWN", "WWID", "SAS Address", "SN",
)  # fmt: skip
# Chaves JSON comparadas em minúsculas (smartctl usa snake_case; storcli usa "Serial Number", "SN", "WWN"...).
CHAVES_JSON = {
    "serial_number", "serial number", "serial no", "wwn", "logical_unit_id", "uuid", "eui64", "nguid", "sn",
    "sas address", "sas_address", "asset tag",
    # Redfish (redfish.json): SKU e NodeID da Dell são a service tag
    "serialnumber", "partserialnumber", "sku", "assettag", "servicetag", "chassisservicetag", "nodeid",
    "hostname", "fqdn", "username", "durablename", "identifiers",
}  # fmt: skip
_RE_LINHA = re.compile(
    r"^(?P<prefixo>\s*(?:" + "|".join(map(re.escape, CHAVES_TEXTO)) + r")\s*:\s*)(?P<valor>.*?)\s*$",
    re.IGNORECASE,
)
RE_MAC = re.compile(r"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f:])")
# Nome de interface derivado do MAC (altname "enx" + 12 dígitos hexadecimais).
RE_ENX = re.compile(r"\benx([0-9a-fA-F]{12})\b")
RE_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
# Linha "rótulo : valor" ou "rótulo=valor" cujo rótulo é de endereço (ipmitool lan print, dmidecode -t 42, racadm getniccfg).
# Só nessas linhas o IPv4 é trocado: versões de firmware têm o mesmo formato (2.65.65.65) e precisam ficar.
RE_LINHA_IP = re.compile(r"^(?P<rotulo>[^:=|\n]*\b(?:IP|IPv4|Gateway|Address|DNS|Server)\b[^:=|\n]*)(?P<resto>[:=].*)$", re.IGNORECASE)
RE_IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
MACS_MANTIDOS = {"00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"}


def mac_anonimo(mac: str) -> bool:
    """MAC que pode ficar numa fixture (substituto, nulo ou broadcast)."""
    return mac.lower() in MACS_MANTIDOS or mac.lower().startswith("02:00:00:00:")


def ip_anonimo(texto: str) -> bool:
    """IP que pode ficar numa fixture (documentação, loopback, não roteável especial)."""
    try:
        ip = ipaddress.ip_address(texto)
    except ValueError:
        return True
    documentacao = [
        ipaddress.ip_network("192.0.2.0/24"),
        ipaddress.ip_network("198.51.100.0/24"),
        ipaddress.ip_network("203.0.113.0/24"),
        ipaddress.ip_network("2001:db8::/32"),
    ]
    # fe80::N com N pequeno é substituto nosso; link-local real deriva do MAC (EUI-64) e é grande.
    link_local_substituto = ip.version == 6 and ip.is_link_local and int(ip) & ((1 << 64) - 1) < 0x10000
    return (
        ip.is_loopback
        or ip.is_unspecified
        or link_local_substituto
        or any(ip in rede for rede in documentacao)
        or str(ip) == "255.255.255.255"
    )


class Anonimizador:
    """Aplica as substituições, lembrando o que já foi trocado nesta pasta."""

    def __init__(self) -> None:
        self._macs: dict[str, str] = {}
        self._ips: dict[str, str] = {}
        self._uuids: dict[str, str] = {}

    def _mac(self, combinacao: re.Match[str]) -> str:
        mac = combinacao.group(0)
        if mac_anonimo(mac):
            return mac
        chave = mac.lower()
        if chave not in self._macs:
            n = len(self._macs) + 1
            self._macs[chave] = f"02:00:00:00:{n // 256:02x}:{n % 256:02x}"
        return self._macs[chave]

    def _enx(self, combinacao: re.Match[str]) -> str:
        hexa = combinacao.group(1).lower()
        mac = ":".join(hexa[i : i + 2] for i in range(0, 12, 2))
        substituto = self._mac(re.match(r".*", mac))
        return "enx" + substituto.replace(":", "")

    def _uuid(self, combinacao: re.Match[str]) -> str:
        chave = combinacao.group(0).lower()
        if chave not in self._uuids:
            self._uuids[chave] = f"00000000-0000-0000-0000-{len(self._uuids) + 1:012d}"
        return self._uuids[chave]

    def ip(self, texto: str) -> str:
        """Substituto de documentação para um IP (mantém os já anônimos)."""
        if ip_anonimo(texto):
            return texto
        if texto not in self._ips:
            n = len(self._ips) + 1
            ip = ipaddress.ip_address(texto)
            if ip.version == 4:
                self._ips[texto] = f"192.0.2.{n % 254 + 1}"
            else:
                self._ips[texto] = f"{'fe80' if ip.is_link_local else '2001:db8'}::{n:x}"
        return self._ips[texto]

    def _ipv4(self, texto: str) -> str:
        try:
            return self.ip(texto)
        except ValueError:  # 999.1.1.1 não é IP
            return texto

    def texto(self, texto: str) -> str:
        """Anonimiza texto livre: linhas "Serial Number: ...", IPv4 em linhas de endereço, MAC e UUID."""
        linhas = []
        for linha in texto.split("\n"):
            combinacao = _RE_LINHA.match(linha)
            if combinacao and valor_util(combinacao["valor"]) is not None and combinacao["valor"] != REMOVIDO:
                linha = combinacao["prefixo"] + REMOVIDO
            rotulo_ip = RE_LINHA_IP.match(linha)
            if rotulo_ip and not re.search(r"mask|subnet", rotulo_ip["rotulo"], re.IGNORECASE):
                linha = rotulo_ip["rotulo"] + RE_IPV4.sub(lambda m: self._ipv4(m.group(0)), rotulo_ip["resto"])
            linhas.append(linha)
        resultado = "\n".join(linhas)
        resultado = RE_MAC.sub(self._mac, resultado)
        resultado = RE_ENX.sub(self._enx, resultado)
        return RE_UUID.sub(self._uuid, resultado)

    def json(self, dados: Any, chave: str = "") -> Any:
        """Anonimiza JSON: chaves sensíveis, MACs e IPs em campos de endereço."""
        if isinstance(dados, dict):
            return {k: (REMOVIDO if k.lower() in CHAVES_JSON else self.json(v, k)) for k, v in dados.items()}
        if isinstance(dados, list):
            return [self.json(v, chave) for v in dados]
        if isinstance(dados, str):
            if chave.lower() in {"local", "broadcast", "address", "permaddr", "peer", "anycast", "gateway", "hostipaddress"} and not RE_MAC.fullmatch(dados):
                return self.ip(dados)
            return RE_UUID.sub(self._uuid, RE_ENX.sub(self._enx, RE_MAC.sub(self._mac, dados)))
        return dados

    def conteudo(self, texto: str) -> str:
        """Anonimiza JSON (reformatando só se algo mudou) ou texto."""
        try:
            dados = json.loads(texto)
        except json.JSONDecodeError:
            return self.texto(texto)
        if not isinstance(dados, (dict, list)):
            return texto
        anonimo = self.json(dados)
        if anonimo == dados:
            return texto
        return json.dumps(anonimo, ensure_ascii=False, indent=2) + "\n"


def anonimizar_texto(texto: str) -> str:
    """Atalho: anonimiza um texto isolado."""
    return Anonimizador().texto(texto)


def anonimizar_conteudo(texto: str) -> str:
    """Atalho: anonimiza um conteúdo isolado (JSON ou texto)."""
    return Anonimizador().conteudo(texto)


def anonimizar_pasta(pasta: str | Path) -> list[Path]:
    """Anonimiza ``comandos/*.stdout``, ``arquivos/`` e ``redfish.json`` (``http.json`` só tem URL e status).

    Devolve os alterados.
    """
    pasta = Path(pasta)
    anonimizador = Anonimizador()
    alterados: list[Path] = []
    candidatos = list((pasta / "comandos").glob("*.stdout")) + [p for p in (pasta / "arquivos").rglob("*") if p.is_file()]
    if (pasta / "redfish.json").is_file():
        candidatos.append(pasta / "redfish.json")
    for arquivo in sorted(candidatos):
        original = arquivo.read_text(encoding="utf-8", errors="replace")
        novo = anonimizador.conteudo(original)
        if novo != original:
            arquivo.write_text(novo, encoding="utf-8")
            alterados.append(arquivo)
    origem = pasta / "origem.json"
    if origem.is_file():
        meta = json.loads(origem.read_text(encoding="utf-8"))
        meta["anonimizado"] = True
        origem.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return alterados


def main(argv: list[str] | None = None) -> int:
    """Ponto de entrada de linha de comando."""
    argumentos = sys.argv[1:] if argv is None else argv
    if len(argumentos) != 1 or not Path(argumentos[0]).is_dir():
        print("Uso: python3 -m msscan.coleta.anonimizar <pasta da coleta>")
        return 2
    alterados = anonimizar_pasta(argumentos[0])
    for arquivo in alterados:
        print(f"anonimizado: {arquivo}")
    print(f"{len(alterados)} arquivo(s) alterado(s). Revise antes de versionar (hostnames não são detectados).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
