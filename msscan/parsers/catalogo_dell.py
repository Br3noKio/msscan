"""Parser do catálogo de firmware da Dell (``Catalog.xml.gz``: XML UTF-16 compactado).

Escrito contra o catálogo real (recorte em ``tests/fixtures/dell/catalogo/``). Função pura:
bytes → lista de componentes. Não baixa nada.
"""

from __future__ import annotations

import gzip
import xml.etree.ElementTree as ET
from typing import Any

from msscan.parsers import ErroParser


def _texto(elemento: ET.Element | None, caminho: str) -> str:
    valor = elemento.findtext(caminho) if elemento is not None else None
    return (valor or "").strip()


def parse_componente(comp: ET.Element) -> dict[str, Any]:
    """Um ``<SoftwareComponent>`` (do catálogo ou do ``package.xml`` embutido num DUP)."""
    tipo = comp.find("ComponentType")
    criticidade = comp.find("Criticality")
    sistemas = [
        {"system_id": (m.get("systemID") or "").upper(), "modelo": _texto(m, "Display"), "marca": _texto(b, "Display")}
        for b in comp.findall("SupportedSystems/Brand")
        for m in b.findall("Model")
    ]
    dispositivos = []
    for dispositivo in comp.findall("SupportedDevices/Device"):
        # Um dispositivo pode ter vários ``<PCIInfo>`` (catálogo real: o rNDC X710/I350, componente 102303, traz 1521:1F9A
        # e 1572:1F99). Ler só o primeiro deixava a X710 do R640 "sem pacote" (log do T1, 02/10/2026).
        pcis = [{k: (pci.get(k) or "").lower() for k in ("vendorID", "deviceID", "subVendorID", "subDeviceID")}
                for pci in dispositivo.findall("PCIInfo")]  # fmt: skip
        dispositivos.append({
            "component_id": dispositivo.get("componentID"),
            "nome": _texto(dispositivo, "Display"),
            "pci": pcis[0] if pcis else None,
            "pcis": pcis,
            # Pré-requisito declarado pela Dell (catálogo real, ex.: ``<Dependency componentType="FRMW" componentID="25227"
            # version="2.40.40.40" ...>`` com ``DepModel systemID``): outro componente precisa estar nesta versão antes.
            "dependencias": [{
                "component_id": d.get("componentID") or "", "tipo": d.get("componentType") or "", "versao": d.get("version") or "",
                "pacote": d.get("releaseID") or "", "nome": _texto(d, "Display"),
                "sistemas": [(m.get("systemID") or "").upper() for m in d.findall("DepBrand/DepModel")],
            } for d in dispositivo.findall("Dependency")],
        })  # fmt: skip
    try:
        tamanho = int(comp.get("size") or 0)
    except ValueError:
        tamanho = 0
    return {
        "nome": _texto(comp, "Name/Display"),
        "tipo": tipo.get("value") if tipo is not None else "",
        "categoria": _texto(comp, "Category/Display"),
        "versao": comp.get("vendorVersion") or "",
        "versao_dell": comp.get("dellVersion") or "",
        "pacote": comp.get("packageID") or comp.get("releaseID") or "",
        "tipo_pacote": comp.get("packageType") or "",
        "caminho": comp.get("path") or "",
        "hash": comp.get("hash") or "",
        "algoritmo_hash": comp.get("hashAlgorithm") or "",
        "md5": comp.get("hashMD5") or "",
        "tamanho": tamanho,
        "reinicio": (comp.get("rebootRequired") or "").lower() == "true",
        "data": comp.get("releaseDate") or "",
        "criticidade": _texto(criticidade, "Display") if criticidade is not None else "",
        "sistemas": sistemas,
        "dispositivos": dispositivos,
    }  # fmt: skip


def parse_catalogo_dell(conteudo: bytes) -> dict[str, Any]:
    """``{"versao", "data", "base", "protocolos", "componentes": [...]}``.

    Cada componente: nome, tipo (``BIOS``/``FRMW``/``DRVR``/``APAC``), categoria, versão do fabricante
    (``vendorVersion``), versão Dell, caminho, hash, tamanho, reinício, data, criticidade, sistemas
    (``systemID`` + nome do modelo) e dispositivos (``componentID`` + IDs PCI, quando houver).
    """
    try:
        xml = gzip.decompress(conteudo) if conteudo[:2] == b"\x1f\x8b" else conteudo
        raiz = ET.fromstring(xml)
    except (OSError, EOFError, ET.ParseError) as erro:
        raise ErroParser(f"catálogo Dell ilegível: {erro}") from erro
    if raiz.tag != "Manifest":
        raise ErroParser(f"catálogo Dell inesperado: raiz '{raiz.tag}' (esperado 'Manifest')")
    # A Dell também põe pacotes em ``<Prerequisites>`` (catálogo 26.10.08: o iDRAC 2.40.40.40, último da 13ª geração, ex.: R530
    # 063A). Ler só os da raiz deixava o R530 "fora de suporte" (teste de 15/10/2026).
    componentes = [parse_componente(comp) for comp in raiz.findall("SoftwareComponent")]
    componentes += [{**parse_componente(comp), "prerequisito": True} for comp in raiz.findall("Prerequisites/SoftwareComponent")]
    return {
        "versao": raiz.get("version"),
        "data": raiz.get("dateTime"),
        "base": raiz.get("baseLocation"),
        "protocolos": raiz.get("baseLocationAccessProtocols"),
        "componentes": componentes,
    }


def parse_pacote_dell(xml: bytes) -> dict[str, Any]:
    """``package.xml`` de um DUP (UTF-16, raiz ``<SoftwareComponent>``), no mesmo formato de um item do catálogo."""
    try:
        raiz = ET.fromstring(xml)
    except ET.ParseError as erro:
        raise ErroParser(f"package.xml ilegível: {erro}") from erro
    if raiz.tag != "SoftwareComponent":
        raise ErroParser(f"package.xml inesperado: raiz '{raiz.tag}' (esperado 'SoftwareComponent')")
    return parse_componente(raiz)
