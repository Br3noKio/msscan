"""Dados enviados à IA: só o necessário, sem identificadores (CLAUDE.md, seção 10).

Removidos antes do envio: serial, service tag, UUID, asset tag, MAC, IP, hostname e WWN.
Cada valor vira um marcador estável (``[SERIAL-1]``, ``[MAC-2]``...), igual em todo o texto,
para a IA ainda perceber "o mesmo disco" sem saber qual é. O histórico de comandos, o
local do pendrive e o endereço do BMC não vão.
"""

from __future__ import annotations

import ipaddress
import json
import re
from typing import Any

from msscan.parsers.dmidecode import valor_util

# Chave (minúsculas) → categoria do marcador. Valores dessas chaves são sempre trocados.
CHAVES_SENSIVEIS = {
    "serial": "SERIAL", "serial_number": "SERIAL", "serialnumber": "SERIAL", "serial number": "SERIAL",
    "serial no": "SERIAL", "sn": "SERIAL", "partserialnumber": "SERIAL", "numero_serie": "SERIAL",
    "service_tag": "SERVICE-TAG", "servicetag": "SERVICE-TAG", "service tag": "SERVICE-TAG", "sku": "SERVICE-TAG",
    "nodeid": "SERVICE-TAG", "chassisservicetag": "SERVICE-TAG",
    "asset_tag": "ASSET-TAG", "assettag": "ASSET-TAG", "asset tag": "ASSET-TAG",
    "uuid": "UUID", "wwn": "WWN", "wwid": "WWN", "sas_address": "WWN", "sas address": "WWN", "logical_unit_id": "WWN",
    "eui64": "WWN", "nguid": "WWN",
    "hostname": "HOSTNAME", "host_name": "HOSTNAME", "fqdn": "HOSTNAME", "nome_host": "HOSTNAME",
    "mac": "MAC", "macaddress": "MAC", "permanentmacaddress": "MAC", "permaddr": "MAC", "endereco_mac": "MAC",
}  # fmt: skip
NOMES_HOST_GENERICOS = {"", "localhost", "debian", "localhost.localdomain", "(none)"}
RE_MAC = re.compile(r"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f:])")
RE_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
RE_WWN = re.compile(r"\b(?:0x|naa\.)?[256][0-9a-fA-F]{15}\b")
RE_IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
CHAVES_VERSAO = {"instalada", "disponivel", "versao", "versao_instalada", "versao_disponivel", "versao_nova", "versao_anterior",
                 "versao_bios", "firmware", "version", "firmwareversion"}
"""Campos que guardam versão de firmware (no formato de um IPv4, ex.: 2.65.65.65): nunca são endereço."""
RE_ANTES_DE_VERSAO = re.compile(r"(?:vers[ãa]o|version|v\.)\s*$|(?:Installed|Previous|Available)-\d+-$|_(?:WN64|LN64|LN|LW64)_$"
                                r"|-LX-$|srvadmin-[\w-]+_$|Pack, $", re.IGNORECASE)
"""Texto logo antes de uma versão no formato de IPv4 (bancada de 19/10/2026: o log técnico mostrava "racadm pronto (versão
[IP-6])", "Installed-25227-[IP-3]" e "Dell-iDRACTools-Web-LX-[IP-6]-1685"; versões não identificam ninguém)."""
RE_IPV6 = re.compile(r"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![0-9A-Fa-f:])")
TAMANHO_MAXIMO = 300_000
"""Caracteres do JSON enviado. Acima disso, listas longas são encurtadas (e isso é dito à IA)."""
MAX_ITENS_LISTA = 100


def _ip_mantido(texto: str) -> bool:
    """Máscaras, loopback e "sem endereço" não identificam nada."""
    try:
        ip = ipaddress.ip_address(texto)
    except ValueError:
        return True
    if ip.version == 4 and texto.startswith("255."):
        return True
    return ip.is_loopback or ip.is_unspecified


class Sanitizador:
    """Troca identificadores por marcadores consistentes e conta o que foi trocado."""

    def __init__(self, extras: dict[str, str] | None = None) -> None:
        self._marcadores: dict[tuple[str, str], str] = {}
        self._literais: dict[str, str] = {}  # valor exato → categoria (ex.: hostname lido do sistema)
        for valor, categoria in (extras or {}).items():
            if valor and valor.strip().lower() not in NOMES_HOST_GENERICOS and len(valor.strip()) >= 3:
                self._literais[valor.strip()] = categoria

    def marcador(self, categoria: str, valor: str) -> str:
        chave = (categoria, valor.lower())
        if chave not in self._marcadores:
            n = sum(1 for c, _ in self._marcadores if c == categoria) + 1
            self._marcadores[chave] = f"[{categoria}-{n}]"
        return self._marcadores[chave]

    def contagem(self) -> dict[str, int]:
        """Quantos valores distintos de cada tipo foram removidos."""
        resultado: dict[str, int] = {}
        for categoria, _ in self._marcadores:
            resultado[categoria] = resultado.get(categoria, 0) + 1
        return resultado

    def coletar(self, dados: Any) -> None:
        """1ª passada: valores de chaves sensíveis (para trocá-los também quando aparecem em texto livre)."""
        if isinstance(dados, dict):
            for chave, valor in dados.items():
                categoria = CHAVES_SENSIVEIS.get(str(chave).lower())
                if categoria and isinstance(valor, str) and valor_util(valor) is not None and len(valor.strip()) >= 3:
                    self._literais.setdefault(valor.strip(), categoria)
                else:
                    self.coletar(valor)
        elif isinstance(dados, list):
            for item in dados:
                self.coletar(item)

    def texto(self, texto: str, versao: bool = False) -> str:
        """``versao``: o campo é de versão de firmware (``CHAVES_VERSAO``): números com pontos ficam."""
        for literal in sorted(self._literais, key=len, reverse=True):
            if literal in texto:
                texto = texto.replace(literal, self.marcador(self._literais[literal], literal))
        texto = RE_MAC.sub(lambda m: self.marcador("MAC", m.group(0).replace("-", ":")), texto)
        texto = RE_UUID.sub(lambda m: self.marcador("UUID", m.group(0)), texto)
        texto = RE_WWN.sub(lambda m: self.marcador("WWN", m.group(0)), texto)
        def ipv4(m: re.Match[str]) -> str:
            if _ip_mantido(m.group(0)) or (versao or RE_ANTES_DE_VERSAO.search(m.string[max(0, m.start() - 40):m.start()])):
                return m.group(0)
            return self.marcador("IP", m.group(0))

        texto = RE_IPV4.sub(ipv4, texto)

        def ipv6(m: re.Match[str]) -> str:
            candidato = m.group(0)
            if candidato.count(":") < 2 or ("::" not in candidato and candidato.count(":") < 7):
                return candidato  # horários (13:45:58) e afins
            return candidato if _ip_mantido(candidato) else self.marcador("IP", candidato)

        return RE_IPV6.sub(ipv6, texto)

    def dados(self, dados: Any, chave: str = "") -> Any:
        """2ª passada: troca valores de chaves sensíveis e identificadores em qualquer texto."""
        if isinstance(dados, dict):
            resultado = {}
            for k, v in dados.items():
                categoria = CHAVES_SENSIVEIS.get(str(k).lower())
                if categoria and isinstance(v, str) and valor_util(v) is not None:
                    resultado[k] = self.marcador(categoria, v.strip())
                else:
                    resultado[k] = self.dados(v, k)
            return resultado
        if isinstance(dados, list):
            return [self.dados(v, chave) for v in dados]
        if isinstance(dados, str):
            return self.texto(dados, versao=str(chave).lower() in CHAVES_VERSAO)
        return dados


def _encurtar(dados: Any, avisos: list[str], caminho: str = "", itens: int = MAX_ITENS_LISTA) -> Any:
    if isinstance(dados, dict):
        return {k: _encurtar(v, avisos, f"{caminho}.{k}" if caminho else k, itens) for k, v in dados.items()}
    if isinstance(dados, list):
        if len(dados) > itens:
            avisos.append(f"{caminho}: {len(dados)} itens, enviados só os {itens} últimos")
            dados = dados[-itens:]
        return [_encurtar(v, avisos, caminho, itens) for v in dados]
    return dados


ITENS_COMPACTO = 15
"""Itens por lista no envio compacto: IA que recusou o pedido por tamanho (HTTP 413; caso real: Groq gratuita, SEL de 1024
eventos do R530)."""


def compactar(dados: dict[str, Any]) -> dict[str, Any]:
    """Versão menor dos dados JÁ sanitizados (listas com os últimos ``ITENS_COMPACTO``), dizendo à IA o que foi cortado."""
    avisos: list[str] = list(dados.get("listas_encurtadas") or [])
    menor = _encurtar({k: v for k, v in dados.items() if k != "listas_encurtadas"}, avisos, itens=ITENS_COMPACTO)
    menor["listas_encurtadas"] = avisos
    return menor


def montar_dados_ia(relatorio: dict[str, Any]) -> dict[str, Any]:
    """Recorte do relatório que interessa à interpretação (sem histórico de comandos nem pendrive)."""
    eq = relatorio.get("equipamento") or {}
    compat = eq.get("compatibilidade") or {}
    entrada = compat.get("entrada") or {}
    bmc = relatorio.get("bmc") or {}
    rede = relatorio.get("rede") or {}
    return {
        "servidor": {
            "fabricante": eq.get("fabricante"),
            "modelo": eq.get("modelo"),
            "versao_bios": eq.get("versao_bios"),
            "ipmi": {"disponivel": (eq.get("ipmi") or {}).get("disponivel"), "versao": (eq.get("ipmi") or {}).get("versao")},
            "redfish": {"confirmado": (eq.get("redfish") or {}).get("confirmado"), "versao": (eq.get("redfish") or {}).get("versao")},
            "controladoras": [
                {"nome": c.get("nome"), "driver": c.get("driver"), "categoria": c.get("categoria")} for c in eq.get("controladoras") or []
            ],
            "banco_de_compatibilidade": {
                "modelo": entrada.get("nome"), "bmc": entrada.get("bmc"),
                "limitacoes_conhecidas": entrada.get("limitacoes") or [],
                "divergencias_com_a_deteccao": compat.get("divergencias") or [],
            } if compat.get("encontrado") else None,
        },  # fmt: skip
        "veredito": relatorio.get("veredito"),
        "contagem_por_status": relatorio.get("contagem"),
        "rede": {"ok": rede.get("ok"), "titulo": rede.get("titulo")} if rede else None,
        "acesso_bmc": {"situacao": bmc.get("situacao"), "perfil": bmc.get("perfil")} if bmc else None,
        "componentes": [
            {"componente": m.get("componente"), "titulo": m.get("titulo"), "status": m.get("status"),
             "fonte": m.get("fonte"), "mensagem": m.get("mensagem"), "dados": m.get("dados")}
            for m in relatorio.get("medido") or []
        ],  # fmt: skip
    }


def preparar_envio(relatorio: dict[str, Any], nomes_host: list[str] | None = None) -> tuple[dict[str, Any], dict[str, int]]:
    """Dados sanitizados para a IA e a contagem do que foi removido. Função pura."""
    sanitizador = Sanitizador({nome: "HOSTNAME" for nome in nomes_host or []})
    recorte = montar_dados_ia(relatorio)
    sanitizador.coletar(recorte)
    dados = sanitizador.dados(recorte)
    if len(json.dumps(dados, ensure_ascii=False, default=str)) > TAMANHO_MAXIMO:
        avisos: list[str] = []
        dados = _encurtar(dados, avisos)
        dados["listas_encurtadas"] = avisos
    return dados, sanitizador.contagem()


def nomes_do_host(executor: Any) -> list[str]:
    """Hostname do sistema em execução (para removê-lo também de textos livres)."""
    nomes = []
    for caminho in ("/proc/sys/kernel/hostname", "/etc/hostname"):
        conteudo = executor.ler_arquivo(caminho)
        if conteudo and conteudo.strip() and conteudo.strip() not in nomes:
            nomes.append(conteudo.strip())
    return nomes
