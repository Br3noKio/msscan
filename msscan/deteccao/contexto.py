"""Contexto: tudo o que foi detectado sobre o equipamento, compartilhado pelos provedores."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from msscan.core.executor import Executor


@dataclass
class InfoIPMI:
    """Capacidade IPMI detectada no equipamento."""

    smbios: dict[str, str] | None = None
    dispositivo: str | None = None
    modprobe_tentado: bool = False
    mc_info: dict[str, Any] | None = None
    disponivel: bool = False
    versao: str | None = None
    motivo: str = ""


@dataclass
class InfoRedfish:
    """Redfish Host Interface anunciada pelo SMBIOS (tipo 42).

    ``anunciado``: há um endereço candidato (SMBIOS tipo 42 ou IP do BMC pelo IPMI).
    ``confirmado`` só fica verdadeiro depois de um GET (sem autenticação) em ``/redfish/v1``.
    """

    host_interface: list[dict[str, Any]] = field(default_factory=list)
    anunciado: bool = False
    endereco: str | None = None
    porta: int | None = None
    origem: str = ""
    """De onde veio o endereço: "SMBIOS tipo 42" ou "ipmitool lan print N"."""
    confirmado: bool = False
    versao: str | None = None
    fabricante: str | None = None
    produto: str | None = None
    tentativas: list[str] = field(default_factory=list)
    motivo: str = ""
    respondeu_http: bool = False
    """O BMC respondeu HTTP na raiz, mas sem Redfish (ex.: 404 no iDRAC7 antes do 2.40.40.40): a rede até ele funciona."""
    modo_tls: str = ""
    """Modo de criptografia que funcionou com o BMC (``tls_bmc``; "" = não conectou). Vai para o log e o relatório (28.2)."""


@dataclass
class Controladora:
    """Controladora de armazenamento encontrada no barramento PCI."""

    slot: str
    classe: str
    nome: str
    descricao: str
    driver: str | None
    categoria: str
    fabricante_id: str
    dispositivo_id: str
    subsistema_ids: str | None = None


@dataclass
class Contexto:
    """Resultado da detecção, passado a todos os provedores de diagnóstico."""

    executor: Executor
    fabricante: str | None = None
    modelo: str | None = None
    versao_bios: str | None = None
    sku: str | None = None
    """SKU do SMBIOS (tipo 1). Na Dell traz o systemID usado no catálogo de firmware."""
    ipmi: InfoIPMI = field(default_factory=InfoIPMI)
    redfish: InfoRedfish = field(default_factory=InfoRedfish)
    controladoras: list[Controladora] = field(default_factory=list)
    sensores_kernel: list[dict[str, Any]] = field(default_factory=list)
    pasta_dados: str | None = None
    """Pasta de dados (com ``vendor/``, ``config/``...), se houver: partição MSSCAN, pendrive, Ventoy ou /tmp."""
    pendrive: dict[str, Any] | None = None
    """Forma de gravação do pendrive e local dos dados (``LocalDados.para_dict``); ``None`` em replay."""
    ferramentas_fabricante: dict[str, str] = field(default_factory=dict)
    """Ferramentas proprietárias encontradas: nome → caminho (ex.: "perccli64" → "/mnt/msscan/vendor/perccli64")."""
    erros_deteccao: dict[str, str] = field(default_factory=dict)
    bmc: Any = None
    """``AcessoBMC`` (credenciais e sessão Redfish); ``None`` sem Redfish confirmado. Não vai para o JSON."""
    compat: dict[str, Any] | None = None
    """Entrada do banco de compatibilidade e divergências (``msscan.compat``)."""
    cache: dict[str, Any] = field(default_factory=dict)
    """Dados coletados uma vez e usados por vários provedores (ex.: SEL). Não vai para o JSON."""

    @property
    def controladoras_raid_hardware(self) -> list[Controladora]:
        """Controladoras que são RAID de hardware de fato (não RAID do chipset)."""
        return [c for c in self.controladoras if c.categoria == "raid_hardware"]

    def para_dict(self) -> dict[str, Any]:
        """Converte para dicionário serializável em JSON (sem o executor)."""
        return {
            "fabricante": self.fabricante,
            "modelo": self.modelo,
            "versao_bios": self.versao_bios,
            "sku": self.sku,
            "ipmi": asdict(self.ipmi),
            "redfish": asdict(self.redfish),
            "controladoras": [asdict(c) for c in self.controladoras],
            "sensores_kernel": [
                {"chip": c["chip"], "adaptador": c["adaptador"], "leituras": len(c["leituras"])}
                for c in self.sensores_kernel
            ],
            "ferramentas_fabricante": dict(self.ferramentas_fabricante),
            # racadm do iDRAC Tools (CLAUDE.md 19.1 e 20): versão e origem (copiado para vendor/ ou baixado da Dell)
            "idrac_tools": {k: (self.cache.get("idrac_tools") or {}).get(k)
                            for k in ("situacao", "versao", "origem", "url", "pacote", "mensagem")},
            "compatibilidade": self.compat,
            "erros_deteccao": dict(self.erros_deteccao),
        }
