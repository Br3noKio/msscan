"""Atualização manual (fase 11, CLAUDE.md 11b): pacote baixado pelo técnico no site do fabricante.

Funções puras (sem executar nada): página de drivers do fabricante, identificação do pacote pelo nome e pelo
``package.xml`` embutido no DUP Linux (lido como bytes, sem executar o script), SHA-256 colado pelo técnico,
conferência de modelo e versão e a tabela oficial de códigos de saída do DUP.

Ordem de segurança: o SHA-256 é conferido ANTES de qualquer execução do pacote (``-c``, ``-v`` e ``--list``
também executam o script do pacote como root).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from msscan.atualizacao.versoes import ATUALIZACAO, IGUAL, INCERTA, INSTALADA_MAIS_NOVA, comparar_versoes
from msscan.parsers import ErroParser
from msscan.parsers.catalogo_dell import parse_pacote_dell
from msscan.parsers.dmidecode import parse_sku_dell

PASTA_MANUAL = "firmware/manual"
PASTA_HPE = "firmware/hpe"
EXTENSOES = (".bin", ".exe")
LIMITE_LEITURA_XML = 8 * 1024 * 1024
"""O ``package.xml`` fica logo depois do script (≈ 60 KB nos pacotes reais); não é preciso ler o pacote inteiro."""

DUP_LINUX, REDFISH_IDRAC_LEGADO, AUTOMATICO = "dup_linux", "redfish_idrac_legado", "automatico"
NAO_SUPORTADO = "Atualização automática não suportada neste equipamento"

# Nomes reais de pacotes Dell: BIOS_W1WJW_LN_2.9.0.BIN, Network_Firmware_F2K1J_WN64_38.11.70.00.EXE,
# SAS-RAID_Firmware_06FNV_WN64_52.38.0-6897_A07_01.EXE, Express-Flash-PCIe-SSD_Firmware_T79D5_LN64_1.2.0_A04_01.BIN.
_RE_NOME_DELL = re.compile(
    r"^(?P<categoria>[A-Za-z0-9-]+?)(?:_(?P<natureza>Firmware|Application|Driver|Utility))?_(?P<release>[A-Za-z0-9]{5})"
    r"_(?P<plataforma>LN64|LN|LNARM64|WN64|WN32)_(?P<versao>[^_]+?)(?:_(?P<dell>A\d{2}(?:_\d{2})?))?\.(?P<ext>BIN|EXE)$",
    re.IGNORECASE,
)
_RE_COPIA = re.compile(r"\(\d+\)(?=\.[A-Za-z]+$)")
"""O navegador renomeia um segundo download do mesmo arquivo para ``nome(1).BIN``."""
_RE_SERVICE_TAG = re.compile(r"[A-Z0-9]{7}")
_MARCADOR_PACOTE = b"\n#####Startofpackage#####"
_FIM_XML = "</SoftwareComponent>".encode("utf-16-le")

CODIGOS_DUP: dict[int, tuple[str, str, str]] = {
    0: ("SUCCESSFUL", "sucesso", "ok"),
    1: ("UNSUCCESSFUL", "erro durante a atualização: não foi aplicada", "falha"),
    2: ("REBOOT_REQUIRED", "reinício necessário para aplicar", "reinicio"),
    3: ("DEP_SOFT_ERROR", "a versão do pacote é igual ou mais antiga que a instalada (nunca downgrade)", "mesma_versao"),
    4: ("DEP_HARD_ERROR", "pré-requisito ausente (BIOS, driver ou firmware) ou nenhum dispositivo compatível", "falha"),
    5: ("QUAL_HARD_ERROR", "o pacote não se aplica a este sistema (sistema, SO ou dispositivos)", "nao_aplicavel"),
    6: ("REBOOTING_SYSTEM", "o sistema está reiniciando", "reinicio"),
    13: ("UPDATE_SUCCESSFUL_SOFT_DEPENDENCY_NOT_MET", "sucesso; dependências opcionais não atendidas", "ok"),
    14: ("REBOOT_REQUIRED_SOFT_DEPENDENCY_NOT_MET", "sucesso; reinício necessário; dependências opcionais não atendidas", "reinicio"),
    15: ("CONTAINER_POWER_CYCLE_REQUIRED", "sucesso; exige desligar e religar o chassi", "reinicio"),
    16: ("CONTAINER_POWER_CYCLE_REQUIRED_SOFT_DEPENDENCY_NOT_MET", "sucesso; exige desligar e religar o chassi; dependências opcionais não atendidas", "reinicio"),
    17: ("UPDATE_SUCCESSFUL_ROLLBACK_FAILED", "sucesso, mas a cópia para rollback falhou", "ok"),
    18: ("UPDATE_SUCCESSFUL_VIRTUAL_AC_REQUIREMENT", "sucesso após o ciclo de energia virtual (AC)", "reinicio"),
    19: ("UPDATE_SUCCESSFUL_VIRTUAL_AC_REQUIRED", "sucesso após desligar e religar a energia (AC) manualmente", "reinicio"),
    20: ("UPDATE_SUCCESSFUL_POST_SCRIPT_FAILED", "sucesso; o script posterior do pacote falhou", "ok"),
}  # fmt: skip
"""Tabela oficial: "Dell Update Packages version 21.04 User's Guide", Table 4. Exit Codes (valores 0–6 e 13–20)."""


# --- Página de drivers do fabricante ---------------------------------------------------------------


def url_drivers(fabricante: str | None, modelo: str | None, service_tag: str | None) -> dict[str, str]:
    """``{"url", "tipo", "explicacao"}`` da página de drivers deste servidor no site do fabricante.

    Dell (conferido no site em 29/09/2026: a Dell redireciona ``/support/home/...`` para estas formas):
    ``https://www.dell.com/support/product-details/pt-br/servicetag/<TAG>/drivers`` (por service tag) ou
    ``.../product/<poweredge-r640>/drivers`` (por modelo). O service tag só vai nesta URL (CLAUDE.md 11b).
    """
    marca = (fabricante or "").lower()
    if marca.startswith("dell"):
        tag = (service_tag or "").strip().upper()
        if _RE_SERVICE_TAG.fullmatch(tag):
            return {"url": f"https://www.dell.com/support/product-details/pt-br/servicetag/{tag}/drivers", "tipo": "service_tag",
                    "explicacao": "página de drivers deste servidor pelo service tag (lista só o que vale para esta configuração)"}  # fmt: skip
        slug = re.sub(r"[^a-z0-9]+", "-", (modelo or "").lower()).strip("-")
        if slug.startswith("poweredge-"):
            return {"url": f"https://www.dell.com/support/product-details/pt-br/product/{slug}/drivers", "tipo": "modelo",
                    "explicacao": "service tag indisponível: página de drivers do modelo"}  # fmt: skip
        return {"url": "https://www.dell.com/support/home/pt-br?app=drivers", "tipo": "busca",
                "explicacao": "modelo não identificado: página de busca de drivers da Dell"}  # fmt: skip
    if marca.startswith(("hpe", "hewlett")):
        return {"url": "https://support.hpe.com/", "tipo": "busca", "explicacao": "suporte da HPE (entre com a conta do cliente para baixar o SPP)"}
    if marca.startswith("lenovo"):
        return {"url": "https://datacentersupport.lenovo.com/", "tipo": "busca", "explicacao": "suporte de servidores da Lenovo"}
    return {"url": "", "tipo": "", "explicacao": "fabricante sem página de drivers conhecida"}


def pasta_destino(fabricante: str | None) -> str:
    """Onde o download fica no local dos dados: HPE em ``firmware/hpe`` (SPP), o resto em ``firmware/manual``."""
    return PASTA_HPE if (fabricante or "").lower().startswith(("hpe", "hewlett")) else PASTA_MANUAL


# --- Identificação do pacote -----------------------------------------------------------------------


def identificar_nome(nome: str) -> dict[str, Any] | None:
    """Campos do nome de um pacote Dell (categoria, release ID, plataforma, versão, extensão); ``None`` se não for."""
    casado = _RE_NOME_DELL.match(_RE_COPIA.sub("", Path(nome).name))
    if not casado:
        return None
    campos = casado.groupdict()
    campos["ext"] = campos["ext"].lower()
    campos["plataforma"] = campos["plataforma"].upper()
    campos["linux"] = campos["plataforma"].startswith("LN")
    return campos


def extrair_package_xml(conteudo: bytes) -> bytes | None:
    """``package.xml`` (UTF-16) embutido num DUP Linux, só lendo bytes: da última linha ``#####Startofpackage#####``
    até ``</SoftwareComponent>``. ``None`` se não houver."""
    marca = conteudo.rfind(_MARCADOR_PACOTE)
    if marca < 0:
        return None
    inicio = conteudo.find(b"\xff\xfe", marca)
    fim = conteudo.find(_FIM_XML, inicio)
    if inicio < 0 or fim < 0:
        return None
    return conteudo[inicio:fim + len(_FIM_XML)]


def ler_pacote(caminho: Path) -> dict[str, Any] | None:
    """Metadados do ``package.xml`` de um ``.BIN`` (sem executar); ``None`` se não houver ou estiver ilegível."""
    try:
        with open(caminho, "rb") as arquivo:
            xml = extrair_package_xml(arquivo.read(LIMITE_LEITURA_XML))
    except OSError:
        return None
    if xml is None:
        return None
    try:
        return parse_pacote_dell(xml)
    except ErroParser:
        return None


def pacote_do_catalogo(identificacao: dict[str, Any] | None, catalogo: dict[str, Any] | None,
                       sku: str | None) -> dict[str, Any] | None:  # fmt: skip
    """Entrada do catálogo oficial da Dell com o MESMO release ID do arquivo (o .BIN e o .EXE de uma versão têm o mesmo
    release ID, ex.: ``BIOS_W1WJW_LN64_2.9.0.BIN`` × ``BIOS_W1WJW_WN64_2.9.0.EXE``) e a mesma versão no nome; com
    systemID no SMBIOS, só a que atende este modelo. ``None`` se nada casar (pedido de 01/10/2026, 3b)."""
    if not identificacao or not catalogo:
        return None
    release, versao = identificacao["release"].upper(), identificacao["versao"]
    dell = parse_sku_dell(sku)
    for comp in catalogo.get("componentes") or []:
        if f"_{release}_" not in Path(str(comp.get("caminho") or "")).name.upper() or comp.get("versao") != versao:
            continue
        if dell and comp.get("sistemas") and not any(s["system_id"] == dell["system_id"] for s in comp["sistemas"]):
            continue
        return comp
    return None


def normalizar_sha256(texto: str | None) -> str | None:
    """SHA-256 colado pelo técnico (aceita ``SHA-256: ...``, espaços e maiúsculas); ``None`` se não for um SHA-256."""
    limpo = re.sub(r"(?i)^\s*sha-?256\s*[:=]?", "", texto or "")
    limpo = re.sub(r"\s+", "", limpo).lower()
    return limpo if re.fullmatch(r"[0-9a-f]{64}", limpo) else None


# --- Conferências ----------------------------------------------------------------------------------


def conferir_modelo(pacote: dict[str, Any] | None, sku: str | None, inventario: list[dict[str, Any]]) -> tuple[bool | None, str]:
    """O pacote é para ESTE servidor? ``True``/``False`` com o motivo; ``None`` se só o ``DUP -c`` pode dizer."""
    if pacote is None:
        return None, "sem package.xml legível: a aplicabilidade fica para a checagem do próprio pacote"
    if pacote["sistemas"]:
        dell = parse_sku_dell(sku)
        if dell is None:
            return False, "systemID não informado pelo SMBIOS (SKU): o modelo não pode ser confirmado"
        casados = [s for s in pacote["sistemas"] if s["system_id"] == dell["system_id"]]
        if not casados:
            lista = ", ".join(sorted({f"{s['modelo']} ({s['system_id']})" for s in pacote["sistemas"]}))
            return False, f"o pacote não é para este servidor (systemID {dell['system_id']}); ele atende: {lista}"
        return True, f"pacote para {', '.join(sorted({s['modelo'] for s in casados}))} (systemID {dell['system_id']})"
    ids = {d["component_id"] for d in pacote["dispositivos"] if d.get("component_id")}
    if ids & {str(i.get("component_id")) for i in inventario if i.get("component_id")}:
        return True, "dispositivo do pacote encontrado no inventário deste servidor"
    return None, "o pacote não lista sistemas e o dispositivo não foi achado no inventário: vale a checagem do pacote"


def conferir_versao(pacote: dict[str, Any] | None, inventario: list[dict[str, Any]]) -> tuple[str, str, str | None]:
    """``(situação, motivo, versão instalada)`` comparando com o componente do inventário (mesmo componentID;
    BIOS pelo tipo). Situações de ``versoes.py``; sem componente casado → ``INCERTA``."""
    if pacote is None:
        return INCERTA, "versão do pacote desconhecida (sem package.xml)", None
    ids = {d["component_id"] for d in pacote["dispositivos"] if d.get("component_id")}
    item = next((i for i in inventario if str(i.get("component_id") or "") in ids), None)
    if item is None and pacote["tipo"] == "BIOS":
        item = next((i for i in inventario if i.get("tipo") == "BIOS"), None)
    if item is None:
        return INCERTA, "componente do pacote não encontrado no inventário deste servidor", None
    situacao, motivo = comparar_versoes(item.get("versao"), pacote["versao"], bool(item.get("truncada")))
    return situacao, motivo.replace("do catálogo", "do pacote").replace("o catálogo", "o pacote"), item.get("versao")


def interpretar_dup(rc: int) -> dict[str, Any]:
    """Código de saída do DUP pela tabela oficial. ``categoria``: ok, reinicio, mesma_versao, nao_aplicavel ou falha."""
    nome, texto, categoria = CODIGOS_DUP.get(rc, ("DESCONHECIDO", f"código {rc} fora da tabela oficial da Dell", "falha"))
    return {"codigo": rc, "nome": nome, "texto": texto, "categoria": categoria,
            "aplicavel": categoria in ("ok", "reinicio")}  # fmt: skip


def metodo_aplicacao(identificacao: dict[str, Any] | None, bmc: dict[str, Any] | None,
                     versao_confirmada: bool = False) -> tuple[str | None, str]:  # fmt: skip
    """Como aplicar este pacote neste servidor (CLAUDE.md 18.3). ``.BIN``: pelo próprio pacote (caminho e). ``.EXE``:
    iDRAC7/8 (upload + ``SimpleUpdate``, o iDRAC confere a versão antes) ou, com a versão confirmada pelo catálogo oficial
    (mesmo release ID), automático — Redfish do iDRAC com acesso automático, racadm local ou o ``.BIN`` oficial
    equivalente. O acesso ao iDRAC (credenciais, usuário temporário, pass-through) é resolvido na instalação."""
    if identificacao is None:
        return None, "arquivo não reconhecido como pacote de atualização da Dell (.BIN/.EXE)"
    if identificacao["linux"]:
        return DUP_LINUX, "pacote Linux (.BIN): aplicado pelo próprio pacote neste servidor"
    bmc = bmc or {}
    if bmc.get("redfish") and bmc.get("simple_update") and not bmc.get("multipart"):
        return REDFISH_IDRAC_LEGADO, "pacote .EXE enviado ao iDRAC7/8 pelo Redfish (upload + SimpleUpdate)"
    if versao_confirmada:
        return AUTOMATICO, ("pacote .EXE: instalado pelo iDRAC (Redfish, com acesso automático), pelo racadm local ou pelo "
                            ".BIN oficial equivalente do catálogo")  # fmt: skip
    return None, ("versão do pacote .EXE não confirmada: o catálogo oficial da Dell não traz este release ID para este "
                  "modelo e o iDRAC9 instala sem conferir a versão antes (nunca downgrade). Use o pacote Linux (.BIN) se houver.")  # fmt: skip
