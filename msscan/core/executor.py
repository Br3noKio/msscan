"""Execução de comandos externos e leitura de arquivos do sistema.

Todo acesso ao sistema (comandos, /proc, /sys, /dev) passa por um ``Executor``.
Há três implementações:

- ``ExecutorReal``: roda de verdade, sempre com timeout;
- ``ExecutorReplay``: não roda nada, devolve o que está gravado numa pasta de fixture;
- ``ExecutorGravador``: roda de verdade e grava tudo no formato de fixture
  (base do modo ``--coletar``).

Formato de uma pasta de fixture::

    origem.json                 metadados (fabricante, modelo, data, observações)
    comandos/<nome>.json        {"comando": [...], "rc": 0, "stderr": "", "duracao": 0.1}
    comandos/<nome>.stdout      saída padrão bruta
    arquivos/<caminho>          conteúdo de arquivos lidos (ex.: arquivos/proc/cpuinfo)
    caminhos.json               {"/dev/ipmi0": true, ...} resultado de verificações de existência
    ferramentas.json            {"smartctl": true, ...} ferramentas encontradas no PATH
    http.json                   {"https://...": {"status": 200, "erro_tipo": "", "erro": ""}} requisições GET
    redfish.json                {"GET /redfish/v1/...": {"status": 200, "corpo": {...}}} respostas do BMC
    ia.json                     {"status": 200, "corpo": {...}} resposta da API de IA (só em fixtures de teste)
    downloads.json              {"https://downloads.dell.com/...": "downloads/Catalog.xml.gz"} arquivos baixados
                                (chave sem o endereço do BMC; token e senha nunca são gravados)
    diretorios.json             {"/sys/class/net": ["eno1", ...]} listagens de diretório
"""

from __future__ import annotations

import json
import secrets
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from msscan.core import tls_bmc

TIMEOUT_PADRAO = 30
RC_FALHA_EXECUCAO = -1

Retorno = tuple[int, str, str]


@dataclass
class Execucao:
    """Registro de um comando executado, para o histórico do relatório."""

    comando: list[str]
    rc: int
    duracao: float
    origem: str
    erro: str = ""

    def para_dict(self) -> dict[str, Any]:
        """Converte para dicionário serializável em JSON."""
        return {
            "comando": self.comando,
            "rc": self.rc,
            "duracao": round(self.duracao, 3),
            "origem": self.origem,
            "erro": self.erro,
        }


@dataclass
class RespostaHttp:
    """Resultado de uma requisição HTTP(S) GET (só cabeçalhos; o corpo não é lido).

    ``status`` é ``None`` quando não houve resposta. ``erro_tipo``: "" (sem
    erro), "dns", "tls", "timeout", "proxy", "conexao" ou "outro".
    """

    url: str
    status: int | None
    erro_tipo: str = ""
    erro: str = ""

    @property
    def respondeu(self) -> bool:
        """O servidor respondeu (qualquer código HTTP prova conectividade)."""
        return self.status is not None

    def para_dict(self) -> dict[str, Any]:
        """Converte para dicionário serializável em JSON."""
        return {"status": self.status, "erro_tipo": self.erro_tipo, "erro": self.erro}


@dataclass
class RespostaRedfish:
    """Resposta de uma requisição Redfish ao BMC.

    ``corpo`` é o JSON da resposta (ou ``None``). ``token`` (``X-Auth-Token``) e
    ``local`` (``Location``) só vêm no login; o token nunca vai para o histórico,
    para gravações de coleta nem para relatórios.
    """

    url: str
    status: int | None
    corpo: Any = None
    token: str = field(default="", repr=False)
    local: str = ""
    erro_tipo: str = ""
    erro: str = ""

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300


@dataclass
class RespostaIA:
    """Resposta da API de IA (Anthropic Messages). ``corpo``: JSON da resposta ou do erro."""

    status: int | None
    corpo: Any = None
    erro_tipo: str = ""
    erro: str = ""
    espera_s: float | None = None
    """``retry-after`` informado pela API (429/529), em segundos."""


@dataclass
class RespostaDownload:
    """Resultado de um download: bytes gravados e SHA-256 calculado durante a gravação."""

    url: str
    status: int | None
    destino: str = ""
    tamanho: int = 0
    sha256: str = ""
    erro_tipo: str = ""
    erro: str = ""
    retomado_de: int = 0
    """Bytes que já estavam no ``.parcial`` (download retomado com ``Range``); o SHA-256 cobre o arquivo inteiro."""

    @property
    def ok(self) -> bool:
        return self.status == 200 and not self.erro_tipo


PARCIAL_VALIDO_S = 6 * 3600
"""``.parcial`` mais velho que isto é descartado: o arquivo no servidor pode ter mudado."""

DOMINIOS_DOWNLOAD = ("downloads.dell.com", "dl.dell.com")
"""Domínios oficiais de onde o MSscan baixa (CLAUDE.md 11.5). HTTPS sempre."""
MAX_REDIRECIONAMENTOS = 3


def validar_download(url: str) -> str:
    """``""`` se a URL for HTTPS num domínio oficial; senão, o motivo."""
    from urllib.parse import urlsplit

    partes = urlsplit(url)
    if partes.scheme != "https":
        return "download só por HTTPS"
    if (partes.hostname or "").lower() not in DOMINIOS_DOWNLOAD:
        return f"domínio não oficial para download: {partes.hostname}"
    return ""


URL_IA = "https://api.anthropic.com/v1/messages"
"""URL fixa da Anthropic."""
HOST_GEMINI = "generativelanguage.googleapis.com"


def validar_url_ia(url: str) -> str:
    """Destinos de POST para a IA (CLAUDE.md 10b). ``""`` se permitido; senão, o motivo.

    Anthropic e Gemini: só as URLs oficiais. Compatível com OpenAI (Groq, OpenRouter...): HTTPS terminando em
    ``/chat/completions``; HTTP só para endereço local/privado (Ollama na própria rede).
    """
    import ipaddress
    import re as _re
    from urllib.parse import urlsplit

    if url == URL_IA:
        return ""
    partes = urlsplit(url)
    host = (partes.hostname or "").lower()
    # Única query aceita: ``alt=sse`` do streaming do Gemini (``:streamGenerateContent?alt=sse``, documentação oficial).
    fluxo_gemini = host == HOST_GEMINI and partes.query == "alt=sse"
    if partes.username or partes.password or (partes.query and not fluxo_gemini) or partes.fragment:
        return "URL da IA não pode ter usuário, senha, query nem fragmento"
    if host == HOST_GEMINI:
        metodo = "streamGenerateContent" if fluxo_gemini else "generateContent"
        if partes.scheme == "https" and _re.fullmatch(r"/v1beta/models/[A-Za-z0-9.\-]+:" + metodo, partes.path):
            return ""
        return "URL do Gemini fora do formato oficial"
    if not partes.path.endswith("/chat/completions"):
        return "URL de IA não reconhecida (esperado .../chat/completions)"
    if partes.scheme == "https" and host:
        return ""
    if partes.scheme == "http":
        try:
            endereco = ipaddress.ip_address(host)
        except ValueError:
            return "HTTP sem TLS só é aceito para endereço IP local/privado (ex.: Ollama)" if host != "localhost" else ""
        if endereco.is_loopback or endereco.is_private:
            return ""
    return "HTTP sem TLS só é aceito para endereço IP local/privado (ex.: Ollama)"

CAMINHO_SESSOES = "/redfish/v1/SessionService/Sessions"
CAMINHOS_SESSOES = (CAMINHO_SESSOES, "/redfish/v1/Sessions")
"""Coleções de sessão aceitas: a da norma e a que o iDRAC7/8 (2.x) anuncia em ``Links.Sessions`` (POST na outra dá 405)."""
METODOS_REDFISH = ("GET", "POST", "DELETE")


def _host_url(url: str) -> str:
    """``host[:porta]`` de uma URL (chave dos modos TLS por BMC)."""
    from urllib.parse import urlsplit

    return urlsplit(url).netloc.lower()


def caminho_url(url: str) -> str:
    """Caminho de uma URL sem esquema, host e query (ex.: ``/redfish/v1/Systems``)."""
    from urllib.parse import urlsplit

    return urlsplit(url).path or "/"


def validar_redfish(metodo: str, url: str) -> str:
    """Salvaguarda: só GET livre; POST só para criar sessão; DELETE só da própria sessão.

    Devolve ``""`` se permitido, ou o motivo da recusa. Qualquer outra escrita no BMC
    (PATCH, ações, atualização) terá método próprio nas fases de atualização.
    """
    if metodo not in METODOS_REDFISH:
        return f"método {metodo} não permitido (somente GET; POST/DELETE só para sessão)"
    if not url.startswith(("https://", "http://")):
        return "URL inválida"
    caminho = caminho_url(url).rstrip("/")
    if metodo == "POST" and caminho not in CAMINHOS_SESSOES:
        return f"POST só é permitido em {CAMINHO_SESSOES} (login)"
    if metodo == "DELETE" and caminho.rpartition("/")[0] not in CAMINHOS_SESSOES:
        return f"DELETE só é permitido numa sessão em {CAMINHO_SESSOES}/<id> (logout)"
    return ""


# --- Escrita no BMC para atualizar firmware (fase 9) -----------------------------------------------

GRAVACAO_REAL_LIBERADA = True
"""Trava da gravação real de firmware. LIBERADA em 30/09/2026 por ordem do usuário ("libere as travas"; CLAUDE.md 15.6),
depois da revisão independente e das correções (PLANO.md, item 6a). Com ``False``, NENHUM executor real envia firmware,
escreve no BMC para aplicar atualização nem reinicia o servidor para aplicar. A suíte de testes roda com ela forçada a
``False`` (``tests/conftest.py``). Mudar de novo SOMENTE num commit próprio, com aprovação do usuário."""
MOTIVO_TRAVA = ("gravação real de firmware desativada pela trava GRAVACAO_REAL_LIBERADA (CLAUDE.md 14 e 15.6): "
                "nada foi enviado ao BMC")  # fmt: skip
ENVIAR_FIRMWARE, BOOT_UNICO_USB, EXPORTAR_CONFIGURACAO = "enviar_firmware", "boot_unico_usb", "exportar_configuracao"
PARAMETROS_UPDATE = {"Targets": [], "@Redfish.OperationApplyTime": "OnReset", "Oem": {}}
"""``UpdateParameters`` do envio multipart: aplicar só no próximo reinício (CLAUDE.md 11). MOCKUP(rodada final):
formato do exemplo oficial da Dell (iDRAC-Redfish-Scripting, DeviceFirmwareMultipartUpload) e da norma DMTF."""
CORPO_BOOT_USB = {"Boot": {"BootSourceOverrideTarget": "Usb", "BootSourceOverrideEnabled": "Once"}}
_CAMINHO_UEFI = re.compile(r"[A-Za-z0-9(),./:\\_ #-]{4,512}")


def corpo_boot_uefi(caminho: str) -> dict[str, Any]:
    """Boot único pelo pendrive quando o iDRAC não anuncia ``Usb`` (CLAUDE.md 21.4): ``UefiTarget`` com o caminho UEFI do
    pendrive (``BootOptions``), uma vez. Fonte: script oficial da Dell ``SetNextOneTimeBootDeviceREDFISH.py``."""
    return {"Boot": {"BootSourceOverrideTarget": "UefiTarget", "BootSourceOverrideEnabled": "Once",
                     "UefiTargetBootSourceOverride": caminho}}  # fmt: skip
CAMPOS_EXPORTACAO = {"ExportFormat", "ShareParameters", "ExportUse", "IncludeInExport"}
ENVIAR_FIRMWARE_LEGADO, SIMPLE_UPDATE = "enviar_firmware_legado", "simple_update"
"""iDRAC7/8 (fase 11): upload do .EXE no ``HttpPushUri`` (``FirmwareInventory``) com ``if-match`` (ETag) e depois
``UpdateService.SimpleUpdate`` com o pacote "Available", aplicado no reinício. Fonte: "iDRAC8 2.70.70.70 Redfish API
Guide" (Updating firmware using SimpleUpdate) e o script oficial DeviceFirmwareSimpleUpdateREDFISH.py.
MOCKUP(rodada final): sem coleta de um iDRAC7/8 real."""
CAMINHO_PUSH_LEGADO = "/redfish/v1/UpdateService/FirmwareInventory"
CAMINHO_SIMPLE_UPDATE = "/redfish/v1/UpdateService/Actions/UpdateService.SimpleUpdate"
_RE_DISPONIVEL = re.compile(r"/redfish/v1/UpdateService/FirmwareInventory/Available[A-Za-z0-9._:-]*")


REPOSITORIO_INSTALAR, REPOSITORIO_LISTA = "repositorio_instalar", "repositorio_lista"
"""Atualização por repositório pelo iDRAC (CLAUDE.md 18.7): ações OEM da Dell ``InstallFromRepository`` e
``GetRepoBasedUpdateList`` (scripts oficiais iDRAC-Redfish-Scripting). MOCKUP(rodada final)."""
SERVICO_INSTALACAO = "/redfish/v1/Systems/System.Embedded.1/Oem/Dell/DellSoftwareInstallationService"
CHAVES_REPOSITORIO = {"IPAddress", "ShareType", "ShareName", "CatalogFile", "ApplyUpdate", "RebootNeeded", "IgnoreCertWarning"}
"""Únicas chaves aceitas: nunca ``ApplySameVersions``/``ApplyDowngradeVersions`` (forçar ou voltar versão), nunca
usuário/senha de compartilhamento, nunca ``RebootNeeded`` verdadeiro (o reinício é do MSscan, depois do boot único)."""
REDE_PASSTHROUGH = "169.254.0.0/16"
CANCELAR_JOB = "cancelar_job"
"""Cancelar UM job criado pelo MSscan que não andou (CLAUDE.md 22.3): ``DellJobService.DeleteJobQueue`` com ``JobID``
(script oficial da Dell ``DeleteJobQueueREDFISH.py``). Nunca ``JID_CLEARALL``/``JID_CLEARALL_FORCE`` (limpa a fila toda e
reinicia serviços do iDRAC)."""
CAMINHO_CANCELAR_JOB = "/redfish/v1/Dell/Managers/iDRAC.Embedded.1/DellJobService/Actions/DellJobService.DeleteJobQueue"
LIMPAR_FILA_FORCADA = "limpar_fila_forcada"
"""Último recurso para um job órfão que o iDRAC não deixa apagar (pedido do usuário, 09/10/2026): ``DeleteJobQueue`` com
``JobID: JID_CLEARALL_FORCE``. Conferido no script oficial da Dell ``DeleteJobQueueREDFISH.py`` (iDRAC-Redfish-Scripting,
em 09/10/2026): apaga TODA a fila e reinicia os serviços do Lifecycle Controller ("it will take a few minutes for the
Lifecycle Controller to be back in Ready state"; "last resort ... to delete a job stuck in running state"). Só com o
técnico confirmando depois do aviso, e nunca com firmware gravando (``jobs.limpar_orfao``). MOCKUP(rodada final)."""
CORPO_LIMPAR_FILA_FORCADA = {"JobID": "JID_CLEARALL_FORCE"}


def _corpo_repositorio_ok(corpo: Any) -> str:
    import ipaddress as _ip

    if not isinstance(corpo, dict) or not set(corpo) <= CHAVES_REPOSITORIO:
        return "InstallFromRepository só com IPAddress, ShareType, ShareName, CatalogFile, ApplyUpdate, RebootNeeded, IgnoreCertWarning"
    if corpo.get("ApplyUpdate") not in ("True", "False") or corpo.get("RebootNeeded") is not False:
        return "InstallFromRepository: ApplyUpdate \"True\"/\"False\" e RebootNeeded false (o reinício é do MSscan)"
    if corpo.get("ShareType") == "HTTPS":
        if corpo.get("IPAddress") != "downloads.dell.com" or "ShareName" in corpo or "CatalogFile" in corpo:
            return "repositório online só o oficial da Dell (downloads.dell.com)"
        return ""
    if corpo.get("ShareType") != "HTTP" or corpo.get("CatalogFile") != "Catalog.xml":
        return "repositório local só por HTTP com o Catalog.xml montado pelo MSscan"
    try:
        endereco = _ip.ip_address(str(corpo.get("IPAddress")))
    except ValueError:
        return "repositório local só por IP"
    if not (endereco in _ip.ip_network(REDE_PASSTHROUGH) or endereco.is_loopback or endereco.is_private):
        return "repositório local só na rede interna do pass-through (169.254.0.0/16) ou na rede local (IP privado)"
    return ""


def corpo_simple_update(imagem: str) -> dict[str, str]:
    """Único corpo aceito para o ``SimpleUpdate``: o pacote já enviado, aplicado só no reinício."""
    return {"ImageURI": imagem, "@Redfish.OperationApplyTime": "OnReset"}


def validar_escrita_bmc(operacao: str, url: str, corpo: Any, arquivo: str | None = None) -> str:
    """Salvaguarda da escrita no BMC: só as três operações da atualização, cada uma com URL e corpo exatos.

    ``""`` se permitido; senão, o motivo. Não substitui a trava ``GRAVACAO_REAL_LIBERADA`` (conferida depois).
    """
    import re as _re

    if not url.startswith("https://"):
        return "escrita no BMC só por HTTPS"
    caminho = caminho_url(url).rstrip("/")
    if operacao == ENVIAR_FIRMWARE:
        if not caminho.startswith("/redfish/v1/UpdateService/"):
            return "envio de firmware só para o MultipartHttpPushUri do UpdateService"
        if corpo != PARAMETROS_UPDATE:
            return "UpdateParameters diferente do permitido (só OperationApplyTime OnReset, sem alvos)"
        if not arquivo:
            return "envio de firmware sem arquivo"
        return ""
    if operacao == BOOT_UNICO_USB:
        if not _re.fullmatch(r"/redfish/v1/Systems/[A-Za-z0-9._-]+", caminho):
            return "boot único só em /redfish/v1/Systems/<id>"
        if corpo == CORPO_BOOT_USB:
            return ""
        caminho_uefi = ((corpo or {}).get("Boot") or {}).get("UefiTargetBootSourceOverride") if isinstance(corpo, dict) else None
        if (isinstance(caminho_uefi, str) and _CAMINHO_UEFI.fullmatch(caminho_uefi) and "usb" in caminho_uefi.lower()
                and corpo == corpo_boot_uefi(caminho_uefi)):  # fmt: skip
            return ""
        return "corpo diferente de Boot único pelo USB (Usb ou UefiTarget de um dispositivo USB, uma vez)"
    if operacao == EXPORTAR_CONFIGURACAO:
        if not _re.fullmatch(r"/redfish/v1/Managers/[A-Za-z0-9._-]+/Actions/Oem/[A-Za-z0-9_]+\.ExportSystemConfiguration", caminho):
            return "exportação só pela ação ExportSystemConfiguration do Manager"
        if not isinstance(corpo, dict) or not set(corpo) <= CAMPOS_EXPORTACAO or set((corpo.get("ShareParameters") or {})) - {"Target"}:
            return "exportação só local (sem compartilhamento de rede)"
        return ""
    if operacao == ENVIAR_FIRMWARE_LEGADO:
        if caminho != CAMINHO_PUSH_LEGADO:
            return f"envio de firmware do iDRAC7/8 só para {CAMINHO_PUSH_LEGADO}"
        if corpo not in (None, {}):
            return "envio de firmware do iDRAC7/8 sem corpo (só o arquivo)"
        return "" if arquivo else "envio de firmware sem arquivo"
    if operacao == SIMPLE_UPDATE:
        if caminho != CAMINHO_SIMPLE_UPDATE:
            return f"SimpleUpdate só em {CAMINHO_SIMPLE_UPDATE}"
        imagem = corpo.get("ImageURI") if isinstance(corpo, dict) else None
        if not isinstance(imagem, str) or not _RE_DISPONIVEL.fullmatch(imagem) or corpo != corpo_simple_update(imagem):
            return "SimpleUpdate só com um pacote já enviado (FirmwareInventory/Available…) e aplicação no reinício (OnReset)"
        return ""
    if operacao == CANCELAR_JOB:
        if caminho != CAMINHO_CANCELAR_JOB:
            return f"cancelar job só em {CAMINHO_CANCELAR_JOB}"
        ident = corpo.get("JobID") if isinstance(corpo, dict) and set(corpo) == {"JobID"} else None
        if not isinstance(ident, str) or not _re.fullmatch(r"JID_[0-9]{6,20}", ident) or arquivo:
            return "cancelar só UM job pelo JobID (JID_<número>; nunca JID_CLEARALL)"
        return ""
    if operacao == LIMPAR_FILA_FORCADA:
        if caminho != CAMINHO_CANCELAR_JOB or corpo != CORPO_LIMPAR_FILA_FORCADA or arquivo:
            return f"limpeza forçada da fila só em {CAMINHO_CANCELAR_JOB} com JobID JID_CLEARALL_FORCE"
        return ""
    if operacao in (REPOSITORIO_INSTALAR, REPOSITORIO_LISTA):
        acao = "InstallFromRepository" if operacao == REPOSITORIO_INSTALAR else "GetRepoBasedUpdateList"
        if caminho != f"{SERVICO_INSTALACAO}/Actions/DellSoftwareInstallationService.{acao}":
            return f"{acao} só no DellSoftwareInstallationService do sistema"
        if operacao == REPOSITORIO_LISTA:
            return "" if corpo == {} and not arquivo else "GetRepoBasedUpdateList só com corpo vazio"
        return "InstallFromRepository não envia arquivo" if arquivo else _corpo_repositorio_ok(corpo)
    return f"operação de escrita no BMC não permitida: {operacao}"


# --- Ações corretivas no BMC (fase 10) -------------------------------------------------------------

ACOES_REAIS_LIBERADAS = True
"""Trava das ações corretivas (CLAUDE.md 10b, 14 e 15.6). LIBERADA em 30/09/2026 por ordem do usuário, com a de firmware.
Com ``False``, NENHUM executor real altera o BMC por uma ação do catálogo (NTP, limpeza do SEL). A suíte de testes roda
com ela forçada a ``False`` (``tests/conftest.py``). Mudar de novo SOMENTE num commit próprio, com aprovação do usuário."""
MOTIVO_TRAVA_ACOES = ("ações corretivas reais desativadas pela trava ACOES_REAIS_LIBERADAS (CLAUDE.md 14 e 15.6): "
                      "nada foi alterado no BMC")  # fmt: skip
NTP_DELL, NTP_DMTF, LIMPAR_SEL = "ntp_dell", "ntp_dmtf", "limpar_sel"
NTP_DELL_RESTAURAR, NTP_DMTF_RESTAURAR = "ntp_dell_restaurar", "ntp_dmtf_restaurar"
"""Desfazer a alteração do NTP: volta aos valores exatos guardados no pendrive antes da alteração (ajuste de 28/09/2026).
Mesmos caminhos e chaves do NTP; aceita ``Disabled``/``false`` e servidor vazio (o que o BMC tinha antes)."""
CHAVES_NTP_DELL = ("NTPConfigGroup.1.NTPEnable", "NTPConfigGroup.1.NTP1", "NTPConfigGroup.1.NTP2", "NTPConfigGroup.1.NTP3")
COMANDO_LIMPAR_SEL = ("ipmitool", "sel", "clear")
_ID_RECURSO = r"[A-Za-z0-9._-]+"
_SERVIDOR_NTP = re.compile(r"(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*|[0-9A-Fa-f:.]{2,45}")
PALAVRAS_ESCRITA_IPMITOOL = {"clear", "set", "raw", "reset", "delete", "bootdev", "bootparam", "cold", "warm", "on", "off", "cycle",
                             "write", "edit", "upgrade", "hpm", "soft", "diag", "policy", "identify", "selftest", "add", "remove"}
# Ampliada em 30/09/2026 (revisão independente, BAIXO): fru write/edit, hpm upgrade, chassis power soft/diag/policy/identify.
"""Argumentos do ``ipmitool`` que alteram o BMC: ``Executor.rodar`` os recusa (a limpeza do SEL só por ``acao_comando``)."""


def servidor_ntp_valido(texto: Any) -> bool:
    """Nome de host ou endereço IP (sem espaço, esquema, porta ou caminho)."""
    return isinstance(texto, str) and bool(_SERVIDOR_NTP.fullmatch(texto))


def validar_acao_bmc(operacao: str, url: str, corpo: Any) -> str:
    """Salvaguarda das ações corretivas no BMC: só as operações do catálogo, com URL e corpo exatos.

    ``""`` se permitido; senão, o motivo. Não substitui a trava ``ACOES_REAIS_LIBERADAS`` (conferida depois).
    """
    if not url.startswith("https://"):
        return "escrita no BMC só por HTTPS"
    caminho = caminho_url(url).rstrip("/")
    if operacao == NTP_DELL:
        if not re.fullmatch(rf"/redfish/v1/Managers/{_ID_RECURSO}/Attributes", caminho):
            return "NTP do iDRAC só em /redfish/v1/Managers/<id>/Attributes"
        atributos = corpo.get("Attributes") if isinstance(corpo, dict) and set(corpo) == {"Attributes"} else None
        if not isinstance(atributos, dict) or not atributos or not set(atributos) <= set(CHAVES_NTP_DELL):
            return "corpo diferente de Attributes só com NTPConfigGroup.1 (NTPEnable, NTP1 a NTP3)"
        if atributos.get("NTPConfigGroup.1.NTPEnable") != "Enabled":
            return "NTPEnable só pode ser ligado (Enabled)"
        servidores = [v for k, v in atributos.items() if k != "NTPConfigGroup.1.NTPEnable"]
        if not servidores or not all(servidor_ntp_valido(s) for s in servidores):
            return "servidor NTP inválido (nome de host ou IP)"
        return ""
    if operacao == NTP_DMTF:
        if not re.fullmatch(rf"/redfish/v1/Managers/{_ID_RECURSO}/NetworkProtocol", caminho):
            return "NTP só em /redfish/v1/Managers/<id>/NetworkProtocol"
        ntp = corpo.get("NTP") if isinstance(corpo, dict) and set(corpo) == {"NTP"} else None
        if not isinstance(ntp, dict) or set(ntp) != {"ProtocolEnabled", "NTPServers"} or ntp["ProtocolEnabled"] is not True:
            return "corpo diferente de NTP com ProtocolEnabled true e NTPServers"
        servidores = ntp["NTPServers"]
        if not isinstance(servidores, list) or not 1 <= len(servidores) <= 3 or not all(servidor_ntp_valido(s) for s in servidores):
            return "NTPServers deve ter de 1 a 3 servidores válidos (nome de host ou IP)"
        return ""
    if operacao == NTP_DELL_RESTAURAR:
        if not re.fullmatch(rf"/redfish/v1/Managers/{_ID_RECURSO}/Attributes", caminho):
            return "NTP do iDRAC só em /redfish/v1/Managers/<id>/Attributes"
        atributos = corpo.get("Attributes") if isinstance(corpo, dict) and set(corpo) == {"Attributes"} else None
        if not isinstance(atributos, dict) or not atributos or not set(atributos) <= set(CHAVES_NTP_DELL):
            return "corpo diferente de Attributes só com NTPConfigGroup.1 (NTPEnable, NTP1 a NTP3)"
        if atributos.get("NTPConfigGroup.1.NTPEnable", "Enabled") not in ("Enabled", "Disabled"):
            return "NTPEnable só pode ser Enabled ou Disabled"
        servidores = [v for k, v in atributos.items() if k != "NTPConfigGroup.1.NTPEnable"]
        if not all(s == "" or servidor_ntp_valido(s) for s in servidores):
            return "servidor NTP inválido (nome de host, IP ou vazio)"
        return ""
    if operacao == NTP_DMTF_RESTAURAR:
        if not re.fullmatch(rf"/redfish/v1/Managers/{_ID_RECURSO}/NetworkProtocol", caminho):
            return "NTP só em /redfish/v1/Managers/<id>/NetworkProtocol"
        ntp = corpo.get("NTP") if isinstance(corpo, dict) and set(corpo) == {"NTP"} else None
        if not isinstance(ntp, dict) or not ntp or not set(ntp) <= {"ProtocolEnabled", "NTPServers"}:
            return "corpo diferente de NTP só com ProtocolEnabled e NTPServers"
        if "ProtocolEnabled" in ntp and not isinstance(ntp["ProtocolEnabled"], bool):
            return "ProtocolEnabled deve ser true ou false"
        servidores = ntp.get("NTPServers", [])
        if not isinstance(servidores, list) or len(servidores) > 3 or not all(s in ("", None) or servidor_ntp_valido(s) for s in servidores):
            return "NTPServers deve ter até 3 servidores válidos (nome de host, IP ou vazio)"
        return ""
    if operacao == LIMPAR_SEL:
        if not re.fullmatch(rf"/redfish/v1/Managers/{_ID_RECURSO}/LogServices/(Sel|SEL)/Actions/LogService\.ClearLog", caminho):
            return "limpeza só do SEL do Manager (LogServices/Sel/Actions/LogService.ClearLog)"
        return "" if corpo == {} else "ClearLog só com corpo vazio"
    return f"ação no BMC não permitida: {operacao}"


def _escrita_ipmitool(comando: list[str]) -> bool:
    return bool(comando) and os.path.basename(comando[0]) == "ipmitool" and any(a.lower() in PALAVRAS_ESCRITA_IPMITOOL for a in comando[1:])


# --- Configuração do iDRAC pelo acesso interno (pedido de 01/10/2026, CLAUDE.md 17.4) ----------------------------------
# Comandos conferidos na documentação oficial: "iDRAC9 RACADM CLI Guide" 3.15.15.15 (iDRAC.IPv4.DHCPEnable/Address/
# Netmask/Gateway, iDRAC.OS-BMC.AdminState 0|1 e PTMode 0=lom-p2p|1=usb-p2p, iDRAC.Users índices 1 a 16: UserName até
# 16 ASCII, Password até 20, Privilege 0–511 = 0x1FF, IpmiLanPrivilege 2/3/4/15, Enable 0|1) e o ``ipmitool`` (``lan set
# <canal> ipsrc|ipaddr|netmask|defgw ipaddr``, ``user set name|password <id> [<senha> 20]``, ``user priv``, ``user
# enable|disable``, ``channel setaccess``; senha IPMI v2.0 até 20 caracteres). MOCKUP(rodada final): nenhuma escrita
# foi testada num iDRAC real; o apagamento do usuário temporário (nome vazio) não está na documentação.

RACADM_LEITURA = {"get", "getsysinfo", "getversion", "help", "getniccfg"}
"""Subcomandos do ``racadm`` que só leem: ``Executor.rodar`` recusa todos os outros (escrita só por ``acao_comando``)."""
NOME_TEMPORARIO = re.compile(r"msscan[0-9a-f]{4}")
"""Nome do usuário temporário criado pelo MSscan (até 16 caracteres ASCII; só esse nome pode ser criado)."""
SENHA_TEMPORARIA = re.compile(r"[A-Za-z0-9]{20}")
"""Senha aleatória do usuário temporário: 20 caracteres (limite do IPMI v2.0 e do iDRAC.Users.Password)."""
_CANAL = re.compile(r"[1-9]|1[0-5]")
_SLOT = re.compile(r"[3-9]|1[0-6]")
"""Slots 1 (anônimo) e 2 (root de fábrica) nunca são usados pelo MSscan."""


def _racadm(comando: list[str]) -> bool:
    return bool(comando) and os.path.basename(comando[0]).lower().startswith("racadm")


RACADM_LEITURA_EXATA = (
    ["swinventory"], ["hwinventory"],
    *[["storage", "get", alvo, "-o"] for alvo in ("controllers", "pdisks", "vdisks", "batteries", "enclosures")],
    *[["raid", "get", alvo, "-o"] for alvo in ("controllers", "pdisks", "vdisks", "batteries", "enclosures")],
)
"""Leituras exatas do inventário e do armazenamento (bancada de 16/10/2026: o racadm local respondia em todos os servidores,
mas a RAID e a versão da PERC ficaram "não verificadas"). Só a forma exata: ``storage``/``raid`` têm verbos de escrita
(createvd, deletevd...) e ``hwinventory export`` grava arquivo. ``raid get`` é a sintaxe dos iDRAC7/8 antigos."""


def _escrita_racadm(comando: list[str]) -> bool:
    if _racadm(comando) and [c.lower() for c in comando[1:]] == ["jobqueue", "view"]:
        return False  # só a listagem da fila de jobs (CLAUDE.md 21.4); jobqueue delete/create continuam recusados
    if _racadm(comando) and [c.lower() for c in comando[1:]] in RACADM_LEITURA_EXATA:
        return False
    return _racadm(comando) and (len(comando) < 2 or comando[1].lower() not in RACADM_LEITURA)


def _senha_fora_da_linha(cmd: list[str]) -> tuple[list[str], str | None]:
    """Revisão de 06/10/2026, B6: a senha do usuário temporário nunca vai na linha de comando (visível em ``/proc/*/cmdline``
    para qualquer processo). Vai num arquivo 0600 lido por ``ipmitool exec`` e apagado logo depois (pelo chamador).
    Vale para o ``ipmitool user set password`` e para o ``racadm set iDRAC.Users.N.Password`` (mesmo usuário IPMI)."""
    a = cmd[1:]
    if os.path.basename(cmd[0]) == "ipmitool" and a[:3] == ["user", "set", "password"] and len(a) == 6:
        programa, slot, senha = cmd[0], a[3], a[4]
    elif _racadm(cmd) and len(a) == 3 and re.fullmatch(r"iDRAC\.Users\.\d{1,2}\.Password", a[1]):
        programa, slot, senha = "ipmitool", a[1].split(".")[2], a[2]
    else:
        return cmd, None
    descritor, caminho = tempfile.mkstemp(prefix="msscan-ipmi-", suffix=".txt")
    with os.fdopen(descritor, "w", encoding="ascii") as arquivo:
        arquivo.write(f"user set password {slot} {senha} 20\n")
    return [programa, "exec", caminho], caminho


def validar_acao_idrac(cmd: list[str], slots_temporarios: dict[int, str]) -> str:
    """Salvaguarda dos comandos de escrita do iDRAC pelo acesso interno: só a lista fechada, argumentos exatos.

    ``slots_temporarios``: slot → nome do usuário temporário criado NESTA sessão (ou reencontrado do boot anterior pelo
    marcador do pendrive). Senha, privilégio, ativação e apagamento só nesses slots: **nunca** num usuário existente.
    ``""`` se permitido; senão, o motivo. A trava ``ACOES_REAIS_LIBERADAS`` é conferida depois, por quem chama.
    """
    if not cmd:
        return "comando vazio"
    a = cmd[1:]
    if os.path.basename(cmd[0]) == "ipmitool":
        if a[:1] == ["lan"]:
            return "a rede do iDRAC nunca é alterada pelo MSscan (CLAUDE.md 18.4)"
        if a[:2] == ["user", "set"] and len(a) >= 4:
            slot = a[3]
        elif a[:2] == ["channel", "setaccess"] and len(a) >= 4:
            slot = a[3]
        elif a[:1] == ["user"] and len(a) >= 3 and a[1] in ("enable", "disable", "priv"):
            slot = a[2]
        else:
            return f"comando do ipmitool não permitido: ipmitool {' '.join(a[:3])}"
        if not _SLOT.fullmatch(slot):
            return "usuário do iDRAC fora dos slots 3 a 16 (1 e 2 nunca são alterados)"
        numero = int(slot)
        if a[:3] == ["user", "set", "name"] and len(a) == 5:
            if NOME_TEMPORARIO.fullmatch(a[4]) or (a[4] == "" and numero in slots_temporarios):
                return ""
            return "só o nome do usuário temporário do MSscan (msscanXXXX) pode ser gravado"
        if numero not in slots_temporarios:
            return "alteração só no usuário temporário criado pelo MSscan (nunca num usuário existente)"
        if a[:3] == ["user", "set", "password"] and len(a) == 6 and SENHA_TEMPORARIA.fullmatch(a[4]) and a[5] == "20":
            return ""
        if a[1] in ("enable", "disable") and len(a) == 3:
            return ""
        if a[1] == "priv" and len(a) == 5 and a[3] in ("4", "15") and _CANAL.fullmatch(a[4]):
            return ""
        acessos = ({"callin=on", "ipmi=on", "link=on", "privilege=4"}, {"callin=off", "ipmi=off", "link=off", "privilege=15"})
        if a[:2] == ["channel", "setaccess"] and len(a) == 8 and _CANAL.fullmatch(a[2]) and set(a[4:]) in acessos:
            return ""
        return f"comando de usuário do iDRAC não permitido: ipmitool {' '.join(a[:3])}"
    if _racadm(cmd):
        if a[:3] == ["jobqueue", "delete", "-i"] and len(a) == 4:
            # 22.4: só UM job (os do repositório criados pelo MSscan); nunca JID_CLEARALL.
            return "" if re.fullmatch(r"JID_[0-9]{6,20}", a[3]) else "racadm jobqueue delete só de UM job (JID_<número>)"
        if len(a) != 3 or a[0] != "set":
            return "racadm: só 'set <atributo> <valor>' e 'jobqueue delete -i JID_<número>'"
        atributo, valor = a[1], a[2]
        if atributo.startswith(("iDRAC.IPv4", "iDRAC.IPv6", "iDRAC.NIC")):
            return "a rede do iDRAC nunca é alterada pelo MSscan (CLAUDE.md 18.4)"
        fixos = {"iDRAC.OS-BMC.AdminState": ("0", "1"), "iDRAC.OS-BMC.PTMode": ("0", "1")}
        if atributo in fixos:
            return "" if valor in fixos[atributo] else f"valor inválido para {atributo}"
        casado = re.fullmatch(r"iDRAC\.Users\.(\d{1,2})\.(UserName|Password|Privilege|IpmiLanPrivilege|Enable)", atributo)
        if not casado or not _SLOT.fullmatch(casado.group(1)):
            return f"atributo do racadm não permitido: {atributo}"
        numero, campo = int(casado.group(1)), casado.group(2)
        if campo == "UserName":
            if NOME_TEMPORARIO.fullmatch(valor) or (valor == "" and numero in slots_temporarios):
                return ""
            return "só o nome do usuário temporário do MSscan (msscanXXXX) pode ser gravado"
        if numero not in slots_temporarios:
            return "alteração só no usuário temporário criado pelo MSscan (nunca num usuário existente)"
        permitidos = {"Password": SENHA_TEMPORARIA, "Privilege": re.compile(r"0x1ff|0x0|0"), "IpmiLanPrivilege": re.compile(r"4|15"),
                      "Enable": re.compile(r"0|1")}  # fmt: skip
        return "" if permitidos[campo].fullmatch(valor) else f"valor inválido para {atributo}"
    return "comando de ação não permitido"


_IPV4 = re.compile(r"(25[0-5]|2[0-4]\d|1?\d?\d)(\.(25[0-5]|2[0-4]\d|1?\d?\d)){3}")


def validar_rede_idrac(cmd: list[str]) -> str:
    """Única exceção à 18.4 (pedido do usuário, CLAUDE.md 21.5): a ação AVULSA ``idrac.ip_fixo`` (com ``CORRIGIR``) e o
    desfazer dela. Só IP fixo/DHCP, endereço, máscara e gateway IPv4 do canal LAN; nada de porta (NIC Selection), VLAN,
    IPv6 ou usuários. Fontes: ``ipmitool lan set`` (manual do ipmitool) e ``iDRAC.IPv4.*`` (iDRAC9 RACADM CLI Guide).
    Chamado só por ``Executor.acao_rede_idrac`` — a atualização automática nunca o usa (teste-guarda)."""
    if not cmd:
        return "comando vazio"
    a = cmd[1:]
    if os.path.basename(cmd[0]) == "ipmitool":
        if len(a) == 5 and a[:2] == ["lan", "set"] and _CANAL.fullmatch(a[2]) and a[3] == "ipsrc" and a[4] in ("dhcp", "static"):
            return ""
        if len(a) == 5 and a[:2] == ["lan", "set"] and _CANAL.fullmatch(a[2]) and a[3] in ("ipaddr", "netmask") and _IPV4.fullmatch(a[4]):
            return ""
        if len(a) == 6 and a[:2] == ["lan", "set"] and _CANAL.fullmatch(a[2]) and a[3:5] == ["defgw", "ipaddr"] and _IPV4.fullmatch(a[5]):
            return ""
        return f"rede do iDRAC: comando do ipmitool não permitido: ipmitool {' '.join(a[:4])}"
    if _racadm(cmd) and len(a) == 3 and a[0] == "set":
        atributo, valor = a[1], a[2]
        if atributo == "iDRAC.IPv4.DHCPEnable":
            return "" if valor in ("0", "1") else "valor inválido para iDRAC.IPv4.DHCPEnable"
        if atributo in ("iDRAC.IPv4.Address", "iDRAC.IPv4.Netmask", "iDRAC.IPv4.Gateway"):
            return "" if _IPV4.fullmatch(valor) else f"{atributo} só com endereço IPv4"
    return "rede do iDRAC: só IP fixo/DHCP, endereço, máscara e gateway IPv4"


OPCOES_LEITURA_DUP = {"-c", "-v", "--version", "--list", "-h", "--help"}
"""Opções do DUP Linux que não gravam nada ("Dell Update Packages 21.04 User's Guide", Table 2). ``Executor.rodar``
recusa qualquer outra (``-q``, ``-f``, ``-r``, modo interativo): a aplicação só por ``Executor.aplicar_dup`` (trava)."""


def _aplicacao_dup(comando: list[str]) -> bool:
    """O comando executa um DUP (``.BIN``) com algo além das opções de leitura."""
    pacote = next((i for i, a in enumerate(comando) if a.lower().endswith(".bin")), None)
    if pacote is None:
        return False
    if pacote > 1:
        return True  # ex.: "bash -x pacote.BIN -q": forma que o MSscan nunca usa → tratada como gravação (recusada)
    if pacote == 1 and os.path.basename(comando[0]) not in ("sh", "bash"):
        return False
    opcoes = comando[pacote + 1:]
    return not opcoes or any(a not in OPCOES_LEITURA_DUP for a in opcoes)


EXTENSOES_RACADM_UPDATE = (".exe", ".d7", ".d9", ".pm", ".sc")
"""Formatos do ``racadm update -f`` local ("iDRAC9 RACADM CLI Guide", subcomando update: "Windows DUP, .d9, .pm, .sc";
``.d7`` é a imagem do iDRAC7/8, a mesma que o ``.BIN`` da Dell grava)."""
IMAGENS_NATIVAS_IDRAC = ("firmimg.d7", "firmimg.d9")


def imagem_nativa_racadm(arquivo: str) -> str | None:
    """Imagem do iDRAC (``payload/firmimg.d7``) tirada do ``.EXE`` da Dell (um zip) já conferido pelo hash do catálogo, num
    caminho curto na RAM (``/tmp/<dir>/firmware/firmimg.d7``). ``None`` se o pacote não tiver. R620 (16/10/2026): o iDRAC7
    1.40.40 recusou o ``.EXE`` (RAC989); o ``firmimg.d7`` do ``.EXE`` é idêntico (SHA-256) ao do ``.BIN`` oficial."""
    import zipfile

    try:
        with zipfile.ZipFile(arquivo) as pacote:
            membro = next((m for m in pacote.infolist() if Path(m.filename).name.lower() in IMAGENS_NATIVAS_IDRAC), None)
            if membro is None:
                return None
            pasta = Path(tempfile.mkdtemp(prefix="r", dir="/tmp")) / "firmware"
            pasta.mkdir()
            destino = pasta / Path(membro.filename).name.lower()
            with pacote.open(membro) as origem, open(destino, "wb") as saida:
                shutil.copyfileobj(origem, saida)
            return str(destino)
    except (OSError, zipfile.BadZipFile):
        return None


LIMITE_CAMINHO_RACADM = 64
"""Teste de 16/10/2026 (R620, iDRAC7 1.40, racadm 11.4): ``ERROR: RAC923 : The file path is too long ... less than 64
characters`` com o caminho do pacote no pendrive."""


def caminho_curto_racadm(arquivo: str) -> str:
    """Caminho com menos de 64 caracteres para o ``racadm update -f``: o próprio, se couber; senão um link simbólico na RAM
    (``/tmp/<dir>/firmware/fw<ext>``) para o pacote já conferido (nada é copiado nem alterado no pendrive)."""
    if len(arquivo) < LIMITE_CAMINHO_RACADM:
        return arquivo
    pasta = Path(tempfile.mkdtemp(prefix="r", dir="/tmp")) / "firmware"
    pasta.mkdir()
    link = pasta / ("fw" + Path(arquivo).suffix)
    link.symlink_to(arquivo)
    return str(link)


def validar_atualizacao_racadm(comando: list[str]) -> str:
    """Única forma aceita: ``racadm update -f <pacote>`` com o pacote baixado pelo MSscan (pasta ``firmware/``).

    Sem compartilhamento de rede, sem usuário/senha e sem opções extras (nada que force a atualização). ``""`` se ok.
    MOCKUP(rodada final): sintaxe da documentação oficial; sem coleta real do racadm local atualizando.
    """
    if len(comando) != 4 or not _racadm(comando) or comando[1:3] != ["update", "-f"]:
        return "racadm: só 'update -f <pacote>' (sem compartilhamento, usuário ou opções extras)"
    arquivo = comando[3]
    partes = Path(arquivo).parts
    if not os.path.isabs(arquivo) or ".." in partes or "firmware" not in partes:
        return "racadm update: só pacotes baixados pelo MSscan na pasta firmware/ do pendrive"
    if not arquivo.lower().endswith(EXTENSOES_RACADM_UPDATE):
        return "racadm update: formato não aceito pelo racadm local (.EXE, .d9, .pm, .sc)"
    return ""


# --- Dados do cliente (CLAUDE.md 18.5) --------------------------------------------------------------------------------
# Acesso total ao hardware (firmware, BMC, sensores, logs), MENOS os dados do cliente: nada monta, lê ou grava partições e
# sistemas de arquivos dos discos do cliente, nada cria/apaga/altera volumes RAID e nada copia arquivos do SO do cliente.

PONTOS_MSSCAN = ("/mnt/msscan", "/mnt/msscan-ventoy", "/run/live/medium")
"""Únicos pontos de montagem aceitos: os do pendrive do MSscan (``msscan/pendrive/``)."""
_OPCOES_MONTAGEM_MSSCAN = re.compile(r"(rw|ro),noatime,fmask=0022,dmask=0022|remount,(ro|rw)")
FERRAMENTAS_DADOS_CLIENTE = {
    "fsck", "e2fsck", "fsck.ext4", "fsck.vfat", "fsck.xfs", "xfs_repair", "xfs_db", "debugfs", "ntfs-3g", "ntfsfix", "ntfscp",
    "ntfscat", "dd", "ddrescue", "cp", "rsync", "scp", "tar", "cpio", "mkfs", "mke2fs", "mkfs.ext4", "mkfs.xfs", "mkfs.vfat",
    "mkfs.ntfs", "fdisk", "sfdisk", "sgdisk", "gdisk", "parted", "wipefs", "lvcreate", "lvremove", "vgcreate", "vgremove",
    "pvcreate", "pvremove", "cryptsetup", "losetup", "kpartx", "partprobe", "zpool", "btrfs", "fuse", "guestmount",
}  # fmt: skip
"""Ferramentas que leem/gravam sistemas de arquivos, partições ou volumes: sempre recusadas pelo ``rodar``."""
FERRAMENTAS_RAID = {"storcli", "storcli64", "storcli2", "perccli", "perccli64", "perccli2", "megacli", "megacli64", "ssacli", "hpssacli", "arcconf",
                    "sas2ircu", "sas3ircu"}  # fmt: skip
PALAVRAS_VOLUME_RAID = {"add", "delete", "del", "create", "destroy", "expand", "migrate", "transform", "init", "initialize",
                        "erase", "secureerase", "import", "-cfgldadd", "-cfglddel", "-cfgclr", "-cfgforeign", "-ldinit",
                        "-pdmakegood", "-pdrbld", "remove", "replace", "set"}  # fmt: skip
MDADM_PERMITIDOS = {"--detail", "-D", "--examine", "-E", "--query", "-Q", "--detail-platform", "--version", "-V"}
MDADM_PROIBIDOS = {"--assemble", "-A", "--create", "-C", "--build", "-B", "--grow", "-G", "--incremental", "-I", "--manage",
                   "--add", "-a", "--re-add", "--remove", "-r", "--fail", "-f", "--stop", "-S", "--run", "-R", "--readonly", "-o",
                   "--readwrite", "-w", "--zero-superblock", "--monitor", "-F", "--update", "-U", "--write-mostly"}  # fmt: skip


def validar_dados_cliente(cmd: list[str]) -> str:
    """``""`` se o comando não toca nos dados do cliente; senão, o motivo da recusa (CLAUDE.md 18.5)."""
    if not cmd:
        return ""
    base = os.path.basename(cmd[0]).lower()
    if base in ("hwclock", "timedatectl"):
        return f"{base}: proibido — o MSscan nunca altera o relógio do BIOS (CLAUDE.md 28.1; só o do sistema, por date -u -s)"
    if base == "mount":
        args = cmd[1:]
        if len(args) >= 3 and args[0] == "-o" and _OPCOES_MONTAGEM_MSSCAN.fullmatch(args[1]) and args[-1].rstrip("/") in PONTOS_MSSCAN:
            return ""
        return "montar partições é proibido (dados do cliente): só o pendrive do MSscan é montado"
    if base in FERRAMENTAS_DADOS_CLIENTE or base.startswith(("mkfs", "fsck")):
        return f"{base}: proibido (lê ou grava sistemas de arquivos, partições ou volumes do cliente)"
    if base in FERRAMENTAS_RAID and {a.lower().lstrip("/") for a in cmd[1:]} & PALAVRAS_VOLUME_RAID:
        return f"{base}: criar, apagar ou alterar volumes RAID é proibido"
    if base == "mdadm" and (not any(a in MDADM_PERMITIDOS for a in cmd[1:]) or any(a.split("=")[0] in MDADM_PROIBIDOS for a in cmd[1:])):
        return "mdadm: só leitura (--detail/--examine/--query); montar, criar ou alterar arrays é proibido"
    return ""


PASTA_IDRAC_TOOLS = "/run/msscan/idractools"
"""Destino fixo (RAM do sistema live) da extração do pacote "iDRAC Tools for Linux" (CLAUDE.md 19.1)."""
_RE_IDRAC_TOOLS = re.compile(r"[^/]*idractools[^/]*\.tar\.gz", re.IGNORECASE)


def validar_idrac_tools(pacote: str) -> str:
    """``""`` se ``pacote`` é um ``*iDRACTools*.tar.gz`` dentro de uma pasta ``vendor/`` do MSscan; senão, o motivo.

    O ``tar`` genérico continua recusado pelo ``rodar`` (18.5): esta é a única extração aceita, com origem e destino fixos.
    """
    caminho = Path(str(pacote))
    if ".." in caminho.parts or not caminho.is_absolute():
        return "pacote do iDRAC Tools: caminho inválido"
    if caminho.parent.name != "vendor" or not _RE_IDRAC_TOOLS.fullmatch(caminho.name):
        return "só o pacote *iDRACTools*.tar.gz da pasta vendor/ do MSscan é extraído"
    return ""


CHAVES_SECRETAS_REDFISH = {"password", "token", "x-auth-token"}


def sem_segredos(dados: Any) -> Any:
    """Remove chaves de senha/token de um JSON do BMC (ex.: mockup que ecoa o corpo do login).

    Aplicado a toda resposta Redfish antes de ela chegar a cache, relatório ou coleta.
    """
    if isinstance(dados, dict):
        return {k: ("REMOVIDO" if k.lower() in CHAVES_SECRETAS_REDFISH else sem_segredos(v)) for k, v in dados.items()}
    if isinstance(dados, list):
        return [sem_segredos(v) for v in dados]
    return dados


def classificar_erro_http(erro: Exception) -> tuple[str, str]:
    """Classifica uma exceção do ``requests`` em ``(tipo, mensagem)``."""
    try:
        import requests
    except ImportError:  # pragma: no cover - requests é dependência da ISO
        return "outro", str(erro)
    texto = str(erro)
    if isinstance(erro, requests.exceptions.SSLError):
        return "tls", texto
    if isinstance(erro, requests.exceptions.ProxyError):
        return "proxy", texto
    if isinstance(erro, requests.exceptions.Timeout):
        return "timeout", texto
    if isinstance(erro, requests.exceptions.ChunkedEncodingError):
        return "conexao", texto  # conexão caiu no meio da transferência
    if isinstance(erro, requests.exceptions.ConnectionError):
        minusculo = texto.lower()
        if any(p in minusculo for p in ("name resolution", "name or service not known", "nodename nor servname", "getaddrinfo")):
            return "dns", texto
        return "conexao", texto
    return "outro", texto


def nome_arquivo_comando(comando: list[str]) -> str:
    """Gera um nome de arquivo estável e legível para um comando.

    Exemplo: ``["ipmitool", "sdr", "type", "Power Supply"]`` →
    ``ipmitool__sdr__type__Power_Supply``.
    """
    partes = [re.sub(r"[^A-Za-z0-9.+-]+", "_", arg).strip("_") or "_" for arg in comando]
    return "__".join(partes)[:180]


def _validar_comando(comando: Any) -> list[str]:
    """Garante que o comando é uma lista de strings (nunca string de shell)."""
    if not isinstance(comando, (list, tuple)) or not comando:
        raise ValueError("o comando deve ser uma lista não vazia de argumentos")
    return [str(arg) for arg in comando]


TEMPO_MAXIMO_DUP_S: float | None = None
"""Espera pelo ``DUP -q``: sem limite (revisão independente, MÉDIO 7) — enquanto ele grava, o marcador de gravação e o
bloqueio da energia continuam; um DUP travado se resolve pelo terminal, nunca matando a gravação sozinho."""


class CorpoMultipart:
    """``multipart/form-data`` com ``UpdateParameters`` (JSON) e ``UpdateFile`` lido do disco em blocos: o pacote não vai
    inteiro para a memória e cada bloco enviado chama ``progresso(enviados, total)``. Arquivo-objeto para o requests
    (``read``, ``__iter__`` e ``__len__``: corpo em fluxo com ``Content-Length``)."""

    BLOCO = 256 * 1024

    def __init__(self, parametros: str, arquivo: str, progresso: Callable[[int, int], None] | None = None) -> None:
        separador = secrets.token_hex(16)
        self.tipo = f"multipart/form-data; boundary={separador}"
        nome = os.path.basename(arquivo).replace('"', "")
        self._cabeca = (f'--{separador}\r\nContent-Disposition: form-data; name="UpdateParameters"\r\n'
                        f"Content-Type: application/json\r\n\r\n{parametros}\r\n--{separador}\r\n"
                        f'Content-Disposition: form-data; name="UpdateFile"; filename="{nome}"\r\n'
                        "Content-Type: application/octet-stream\r\n\r\n").encode()  # fmt: skip
        self._cauda = f"\r\n--{separador}--\r\n".encode()
        self.total = os.path.getsize(arquivo)
        self._arquivo = open(arquivo, "rb")  # noqa: SIM115 - fechado no __exit__
        self._pendente = self._cabeca
        self._enviados = 0
        self._fim = False
        self.progresso = progresso

    def __len__(self) -> int:
        return len(self._cabeca) + self.total + len(self._cauda)

    def __enter__(self) -> CorpoMultipart:
        return self

    def __exit__(self, *_: object) -> None:
        self._arquivo.close()

    def __iter__(self) -> Iterator[bytes]:
        while bloco := self.read(self.BLOCO):
            yield bloco

    def read(self, tamanho: int = -1) -> bytes:
        tamanho = self.BLOCO if tamanho is None or tamanho < 0 else tamanho
        if self._pendente:
            saida, self._pendente = self._pendente[:tamanho], self._pendente[tamanho:]
            return saida
        if self._fim:
            return b""
        bloco = self._arquivo.read(tamanho)
        if bloco:
            self._enviados += len(bloco)
            if self.progresso is not None:
                try:
                    self.progresso(self._enviados, self.total)
                except Exception:  # noqa: BLE001 - o progresso nunca interrompe o envio
                    pass
            return bloco
        self._fim = True
        return self._cauda


class Executor:
    """Interface comum dos executores. Subclasses implementam os métodos ``_``."""

    origem = "base"
    prepara_ferramentas = False
    alvo_simulado = False
    """``True`` só nos BMCs falsos dos testes: libera ``bmc_escrita`` mesmo com a trava ligada (nunca em produção)."""

    def __init__(self) -> None:
        self.historico: list[Execucao] = []
        self.progresso_envio: Callable[[int, int], None] | None = None
        """Bytes do firmware já enviados ao BMC (só durante ``bmc_escrita``; CLAUDE.md 21.1)."""
        self.slots_temporarios: dict[int, str] = {}
        """Usuários temporários do MSscan no iDRAC (slot → nome): os únicos que ``acao_comando`` deixa alterar (17.4)."""
        self.ia_cancelada = threading.Event()
        """Botão "Cancelar envio" (17.5): ``post_ia`` devolve na hora e as novas tentativas/reserva param."""
        self._resposta_ia: Any = None
        self.modos_tls_bmc: dict[str, str] = {}
        """Modo de criptografia que funcionou com cada BMC (host → modo de ``tls_bmc``), guardado na sessão (CLAUDE.md 28.2)."""

    def rodar(self, comando: list[str], timeout: float = TIMEOUT_PADRAO, registrar: bool = True) -> Retorno:
        """Roda um comando e devolve ``(rc, stdout, stderr)``.

        Nunca levanta exceção: comando ausente, sem permissão ou que estourou o
        tempo devolve ``rc = -1`` e a explicação em ``stderr``.
        ``registrar=False`` (leituras periódicas, ex.: sensores ao vivo) só deixa a execução fora do histórico; todas as
        validações continuam e uma recusa é sempre registrada.
        """
        try:
            cmd = _validar_comando(comando)
        except ValueError as erro:
            return RC_FALHA_EXECUCAO, "", str(erro)
        motivo = validar_dados_cliente(cmd)
        if motivo:
            self.historico.append(Execucao(cmd, RC_FALHA_EXECUCAO, 0.0, self.origem, motivo))
            return RC_FALHA_EXECUCAO, "", motivo
        if _escrita_ipmitool(cmd) or _escrita_racadm(cmd):
            motivo = f"comando do {os.path.basename(cmd[0])} que altera o BMC recusado (diagnóstico é somente leitura)"
            self.historico.append(Execucao(cmd, RC_FALHA_EXECUCAO, 0.0, self.origem, motivo))
            return RC_FALHA_EXECUCAO, "", motivo
        if _aplicacao_dup(cmd):
            motivo = "execução de DUP que grava recusada (só -c, -v, --list e --help; aplicação só por aplicar_dup)"
            self.historico.append(Execucao(cmd, RC_FALHA_EXECUCAO, 0.0, self.origem, motivo))
            return RC_FALHA_EXECUCAO, "", motivo
        if not timeout or timeout <= 0:
            timeout = TIMEOUT_PADRAO
        inicio = time.monotonic()
        try:
            rc, saida, erro, duracao = self._rodar(cmd, timeout)
        except Exception as excecao:  # noqa: BLE001 - executor nunca pode quebrar
            rc, saida, erro, duracao = (
                RC_FALHA_EXECUCAO,
                "",
                f"erro inesperado ao executar: {excecao}",
                time.monotonic() - inicio,
            )
        if registrar:
            self.historico.append(Execucao(cmd, rc, duracao, self.origem, erro if rc == RC_FALHA_EXECUCAO else ""))
        return rc, saida, erro

    def ler_arquivo(self, caminho: str) -> str | None:
        """Lê um arquivo de texto do sistema; devolve ``None`` se não existir ou falhar."""
        try:
            return self._ler_arquivo(caminho)
        except Exception:  # noqa: BLE001
            return None

    def existe(self, caminho: str) -> bool:
        """Indica se um caminho existe no sistema (ex.: ``/dev/ipmi0``)."""
        try:
            return self._existe(caminho)
        except Exception:  # noqa: BLE001
            return False

    def listar(self, caminho: str) -> list[str]:
        """Lista os nomes dentro de um diretório; lista vazia se não existir."""
        try:
            return sorted(self._listar(caminho))
        except Exception:  # noqa: BLE001
            return []

    def tem_comando(self, nome: str) -> bool:
        """Indica se a ferramenta ``nome`` está instalada (sem executá-la)."""
        try:
            return self._tem_comando(nome)
        except Exception:  # noqa: BLE001
            return False

    def http_get(self, url: str, timeout: float = 10) -> RespostaHttp:
        """Faz um GET HTTP(S) lendo só os cabeçalhos. Nunca levanta exceção.

        Somente GET: métodos que alteram estado (POST/PATCH em BMC) terão
        método próprio, com salvaguardas, nas fases de atualização.
        """
        if not url.startswith(("https://", "http://")):
            return RespostaHttp(url, None, "outro", "URL inválida")
        if not timeout or timeout <= 0:
            timeout = TIMEOUT_PADRAO
        inicio = time.monotonic()
        try:
            resposta = self._http_get(url, timeout)
        except Exception as erro:  # noqa: BLE001
            resposta = RespostaHttp(url, None, "outro", f"erro inesperado: {erro}")
        self.historico.append(
            Execucao(
                ["GET", url],
                resposta.status if resposta.status is not None else RC_FALHA_EXECUCAO,
                time.monotonic() - inicio,
                self.origem,
                resposta.erro,
            )
        )
        return resposta

    def redfish(
        self, metodo: str, url: str, token: str = "", corpo: dict[str, Any] | None = None, timeout: float = 20
    ) -> RespostaRedfish:
        """Requisição Redfish ao BMC (certificado autoassinado aceito). Nunca levanta exceção.

        Salvaguardas em ``validar_redfish``. No histórico entram só método e URL:
        o corpo do login (usuário e senha) e o token nunca são registrados.
        """
        metodo = metodo.upper()
        motivo = validar_redfish(metodo, url)
        if not timeout or timeout <= 0:
            timeout = TIMEOUT_PADRAO
        inicio = time.monotonic()
        if motivo:
            resposta = RespostaRedfish(url, None, erro_tipo="bloqueado", erro=motivo)
        else:
            try:
                resposta = self._redfish(metodo, url, token, corpo, timeout)
            except Exception as erro:  # noqa: BLE001
                resposta = RespostaRedfish(url, None, erro_tipo="outro", erro=f"erro inesperado: {erro}")
        resposta.corpo = sem_segredos(resposta.corpo)
        comando = [metodo, url] + (["(credenciais omitidas)"] if metodo == "POST" else [])
        self.historico.append(
            Execucao(
                comando,
                resposta.status if resposta.status is not None else RC_FALHA_EXECUCAO,
                time.monotonic() - inicio,
                self.origem,
                resposta.erro,
            )
        )
        return resposta

    def escrita_permitida(self) -> bool:
        """Trava: executor real nunca escreve no BMC enquanto ``GRAVACAO_REAL_LIBERADA`` for False."""
        return GRAVACAO_REAL_LIBERADA or (self.alvo_simulado and self.origem != "real")

    def bmc_escrita(self, operacao: str, url: str, token: str, corpo: Any, arquivo: str | None = None,
                    timeout: float = 900, progresso: Callable[[int, int], None] | None = None) -> RespostaRedfish:  # fmt: skip
        """Escrita no BMC para atualizar firmware (salvaguardas em ``validar_escrita_bmc`` + trava). Nunca levanta exceção.

        No histórico entram só a operação, o método e a URL (nunca token, corpo nem o conteúdo do arquivo).
        ``progresso(enviados, total)``: bytes do arquivo já enviados ao BMC (CLAUDE.md 21.1), no envio do firmware.
        """
        self.progresso_envio = progresso
        metodo = "PATCH" if operacao == BOOT_UNICO_USB else "POST"
        inicio = time.monotonic()
        motivo = validar_escrita_bmc(operacao, url, corpo, arquivo)
        if motivo:
            resposta = RespostaRedfish(url, None, erro_tipo="bloqueado", erro=motivo)
        elif not self.escrita_permitida():
            resposta = RespostaRedfish(url, None, erro_tipo="trava", erro=MOTIVO_TRAVA)
        else:
            try:
                resposta = self._bmc_escrita(operacao, metodo, url, token, corpo, arquivo, timeout)
            except Exception as erro:  # noqa: BLE001
                resposta = RespostaRedfish(url, None, erro_tipo="outro", erro=f"erro inesperado: {erro}")
            finally:
                self.progresso_envio = None
        resposta.corpo = sem_segredos(resposta.corpo)
        self.historico.append(
            Execucao([metodo, url, f"({operacao})"], resposta.status if resposta.status is not None else RC_FALHA_EXECUCAO,
                     time.monotonic() - inicio, self.origem, resposta.erro)  # fmt: skip
        )
        return resposta

    def reiniciar_para_aplicar(self) -> tuple[bool, str]:
        """Reinicia o servidor para o BMC aplicar o firmware agendado (``OnReset``). Mesma trava da escrita."""
        if not self.escrita_permitida():
            self.historico.append(Execucao(["systemctl", "reboot"], RC_FALHA_EXECUCAO, 0.0, self.origem, MOTIVO_TRAVA))
            return False, MOTIVO_TRAVA
        rc, _, erro = self.rodar(["systemctl", "reboot"], timeout=60)
        return rc == 0, erro.strip()

    def checar_dup(self, caminho: str) -> Retorno:
        """``DUP -c``: o pacote diz se pode ser aplicado (não grava). Quem chama já conferiu o SHA-256."""
        return self.rodar(["bash", caminho, "-c"], timeout=900)

    def aplicar_dup(self, caminho: str) -> Retorno:
        """``DUP -q`` (aplica). Mesma trava da gravação de firmware (``GRAVACAO_REAL_LIBERADA``)."""
        comando = ["bash", caminho, "-q"]
        if not self.escrita_permitida():
            self.historico.append(Execucao(comando, RC_FALHA_EXECUCAO, 0.0, self.origem, MOTIVO_TRAVA))
            return RC_FALHA_EXECUCAO, "", MOTIVO_TRAVA
        inicio = time.monotonic()
        try:
            rc, saida, erro, _ = self._rodar_gravacao(comando, TEMPO_MAXIMO_DUP_S)
        except Exception as excecao:  # noqa: BLE001
            rc, saida, erro = RC_FALHA_EXECUCAO, "", f"erro inesperado ao executar: {excecao}"
        self.historico.append(Execucao(comando, rc, time.monotonic() - inicio, self.origem, erro if rc == RC_FALHA_EXECUCAO else ""))
        return rc, saida, erro

    def atualizar_racadm(self, racadm: str, arquivo: str) -> Retorno:
        """``racadm update -f <pacote>`` local (CLAUDE.md 18.3 d). Salvaguarda + trava ``GRAVACAO_REAL_LIBERADA``;
        desacoplado do MSscan como o ``DUP -q`` (nunca morto no meio)."""
        comando = [str(racadm), "update", "-f", str(arquivo)]
        motivo = validar_atualizacao_racadm(comando)
        if not motivo and not self.escrita_permitida():
            motivo = MOTIVO_TRAVA
        if motivo:
            self.historico.append(Execucao(comando, RC_FALHA_EXECUCAO, 0.0, self.origem, motivo))
            return RC_FALHA_EXECUCAO, "", motivo
        inicio = time.monotonic()
        try:
            comando = [str(racadm), "update", "-f", caminho_curto_racadm(str(arquivo))]
            rc, saida, erro, _ = self._rodar_gravacao(comando, TEMPO_MAXIMO_DUP_S)
        except Exception as excecao:  # noqa: BLE001
            rc, saida, erro = RC_FALHA_EXECUCAO, "", f"erro inesperado ao executar: {excecao}"
        self.historico.append(Execucao(comando, rc, time.monotonic() - inicio, self.origem, erro if rc == RC_FALHA_EXECUCAO else ""))
        return rc, saida, erro

    def acoes_permitidas(self) -> bool:
        """Trava das ações corretivas: executor real nunca altera o BMC enquanto ``ACOES_REAIS_LIBERADAS`` é False."""
        return ACOES_REAIS_LIBERADAS or (self.alvo_simulado and self.origem != "real")

    def acao_bmc(self, operacao: str, url: str, token: str, corpo: Any, timeout: float = 60) -> RespostaRedfish:
        """Ação corretiva no BMC pelo Redfish (salvaguardas em ``validar_acao_bmc`` + trava). Nunca levanta exceção.

        No histórico entram só a operação, o método e a URL (nunca token nem corpo).
        """
        metodo = "POST" if operacao == LIMPAR_SEL else "PATCH"
        inicio = time.monotonic()
        motivo = validar_acao_bmc(operacao, url, corpo)
        if motivo:
            resposta = RespostaRedfish(url, None, erro_tipo="bloqueado", erro=motivo)
        elif not self.acoes_permitidas():
            resposta = RespostaRedfish(url, None, erro_tipo="trava", erro=MOTIVO_TRAVA_ACOES)
        else:
            try:
                resposta = self._acao_bmc(metodo, url, token, corpo, timeout)
            except Exception as erro:  # noqa: BLE001
                resposta = RespostaRedfish(url, None, erro_tipo="outro", erro=f"erro inesperado: {erro}")
        resposta.corpo = sem_segredos(resposta.corpo)
        self.historico.append(
            Execucao([metodo, url, f"({operacao})"], resposta.status if resposta.status is not None else RC_FALHA_EXECUCAO,
                     time.monotonic() - inicio, self.origem, resposta.erro)  # fmt: skip
        )
        return resposta

    def acao_comando(self, comando: list[str], timeout: float = 60) -> Retorno:
        """Ação corretiva por comando local, atrás da trava ``ACOES_REAIS_LIBERADAS``. Nunca levanta exceção.

        Só ``ipmitool sel clear`` e a lista fechada do iDRAC (``validar_acao_idrac``: rede, pass-through e o usuário
        temporário do MSscan). A senha nunca vai para o histórico (aparece como ``********``).
        """
        cmd = [str(a) for a in comando] if isinstance(comando, (list, tuple)) else []
        motivo = "" if tuple(cmd) == COMANDO_LIMPAR_SEL else validar_acao_idrac(cmd, self.slots_temporarios)
        registro = [("********" if SENHA_TEMPORARIA.fullmatch(a) else a) for a in cmd] or ["?"]
        if not motivo and not self.acoes_permitidas():
            motivo = MOTIVO_TRAVA_ACOES
        if not motivo:
            motivo = self._slot_novo_vazio(cmd)
        if motivo:
            self.historico.append(Execucao(registro, RC_FALHA_EXECUCAO, 0.0, self.origem, motivo))
            return RC_FALHA_EXECUCAO, "", motivo
        inicio = time.monotonic()
        arquivo_senha = None
        try:
            real, arquivo_senha = _senha_fora_da_linha(cmd)
            rc, saida, erro, duracao = self._rodar(real, timeout)
        except Exception as excecao:  # noqa: BLE001
            rc, saida, erro, duracao = RC_FALHA_EXECUCAO, "", f"erro inesperado ao executar: {excecao}", time.monotonic() - inicio
        finally:
            if arquivo_senha:
                Path(arquivo_senha).unlink(missing_ok=True)
        self.historico.append(Execucao(registro, rc, duracao, self.origem, erro if rc == RC_FALHA_EXECUCAO else ""))
        if rc == 0:
            self._acompanhar_temporario(cmd)
        return rc, saida, erro

    def acao_rede_idrac(self, comando: list[str], timeout: float = 60) -> Retorno:
        """Ação avulsa ``idrac.ip_fixo`` (CLAUDE.md 21.5): lista fechada de ``validar_rede_idrac``, atrás da trava
        ``ACOES_REAIS_LIBERADAS``. Nunca levanta exceção."""
        cmd = [str(a) for a in comando] if isinstance(comando, (list, tuple)) else []
        motivo = validar_rede_idrac(cmd)
        if not motivo and not self.acoes_permitidas():
            motivo = MOTIVO_TRAVA_ACOES
        if motivo:
            self.historico.append(Execucao(cmd or ["?"], RC_FALHA_EXECUCAO, 0.0, self.origem, motivo))
            return RC_FALHA_EXECUCAO, "", motivo
        inicio = time.monotonic()
        try:
            rc, saida, erro, duracao = self._rodar(cmd, timeout)
        except Exception as excecao:  # noqa: BLE001
            rc, saida, erro, duracao = RC_FALHA_EXECUCAO, "", f"erro inesperado ao executar: {excecao}", time.monotonic() - inicio
        self.historico.append(Execucao(cmd, rc, duracao, self.origem, erro if rc == RC_FALHA_EXECUCAO else ""))
        return rc, saida, erro

    def _slot_novo_vazio(self, cmd: list[str]) -> str:
        """Revisão de 06/10/2026, B4: o nome ``msscanXXXX`` só entra num slot que ainda não é do MSscan se esse slot estiver
        VAZIO e sem acesso, relido agora no iDRAC (``ipmitool user list``). ``""`` se pode seguir; senão, o motivo."""
        a = cmd[1:]
        if a[:3] == ["user", "set", "name"] and len(a) == 5:
            slot, nome = a[3], a[4]
        elif _racadm(cmd) and len(a) == 3 and re.fullmatch(r"iDRAC\.Users\.\d{1,2}\.UserName", a[1]):
            slot, nome = a[1].split(".")[2], a[2]
        else:
            return ""
        if not NOME_TEMPORARIO.fullmatch(nome) or int(slot) in self.slots_temporarios:
            return ""
        from msscan.parsers.ipmitool import parse_user_list

        rc, saida, _ = self.rodar(["ipmitool", "user", "list", "1"], timeout=20)
        try:
            usuarios = parse_user_list(saida) if rc == 0 else None
        except Exception:  # noqa: BLE001 - leitura ilegível = slot não conferido
            usuarios = None
        if usuarios is None:
            return "slot do usuário temporário não conferido (a lista de usuários do iDRAC não pôde ser lida): nada alterado"
        atual = next((u for u in usuarios if u["slot"] == int(slot)), None)
        if atual is None or atual["nome"] or atual["privilegio"].upper() != "NO ACCESS":
            return f"o slot {slot} do iDRAC não está vazio agora: nenhum usuário existente é alterado"
        return ""

    def _acompanhar_temporario(self, cmd: list[str]) -> None:
        """Nome ``msscanXXXX`` gravado num slot → slot passa a ser do MSscan; nome apagado → deixa de ser."""
        a = cmd[1:]
        if a[:3] == ["user", "set", "name"]:
            slot, nome = int(a[3]), a[4]
        elif _racadm(cmd) and re.fullmatch(r"iDRAC\.Users\.\d{1,2}\.UserName", a[1] if len(a) > 1 else ""):
            slot, nome = int(a[1].split(".")[2]), a[2]
        else:
            return
        if NOME_TEMPORARIO.fullmatch(nome):
            self.slots_temporarios[slot] = nome
        elif nome == "":
            self.slots_temporarios.pop(slot, None)

    def reconhecer_temporario(self, slot: int, nome: str) -> bool:
        """Usuário temporário de uma sessão anterior (marcador no pendrive E o mesmo nome lido agora no iDRAC): pode ser
        apagado. Só nomes ``msscanXXXX`` e slots 3 a 16."""
        if not NOME_TEMPORARIO.fullmatch(nome) or not _SLOT.fullmatch(str(slot)):
            return False
        self.slots_temporarios[int(slot)] = nome
        return True

    def extrair_idrac_tools(self, pacote: str) -> Retorno:
        """Extrai o "iDRAC Tools for Linux" de ``vendor/`` em ``PASTA_IDRAC_TOOLS`` (apagada antes). Nunca levanta exceção."""
        comando = ["extrair-idrac-tools", str(pacote), PASTA_IDRAC_TOOLS]
        motivo = validar_idrac_tools(pacote)
        inicio = time.monotonic()
        if motivo:
            rc, saida, erro = RC_FALHA_EXECUCAO, "", motivo
        else:
            try:
                rc, saida, erro = self._extrair_idrac_tools(str(pacote), PASTA_IDRAC_TOOLS)
            except Exception as excecao:  # noqa: BLE001 - executor nunca pode quebrar
                rc, saida, erro = RC_FALHA_EXECUCAO, "", f"erro inesperado ao extrair: {excecao}"
        self.historico.append(Execucao(comando, rc, time.monotonic() - inicio, self.origem, erro if rc != 0 else ""))
        return rc, saida, erro

    def baixar(self, url: str, destino: str, limite_bytes: int, timeout: float = 120) -> RespostaDownload:
        """Baixa ``url`` (HTTPS, domínio oficial) para ``destino``. Nunca levanta exceção.

        Grava num arquivo temporário e só renomeia no fim (download interrompido não deixa lixo com o
        nome certo). Acima de ``limite_bytes``, aborta e apaga.
        """
        motivo = validar_download(url)
        inicio = time.monotonic()
        if motivo:
            resposta = RespostaDownload(url, None, erro_tipo="bloqueado", erro=motivo)
        else:
            try:
                resposta = self._baixar(url, destino, limite_bytes, timeout)
            except Exception as erro:  # noqa: BLE001
                resposta = RespostaDownload(url, None, erro_tipo="outro", erro=f"erro inesperado: {erro}")
        self.historico.append(
            Execucao(
                ["GET", url, f"→ {destino}"],
                resposta.status if resposta.status is not None else RC_FALHA_EXECUCAO,
                time.monotonic() - inicio,
                self.origem,
                resposta.erro,
            )
        )
        return resposta

    def post_ia(
        self, cabecalhos: dict[str, str], corpo: dict[str, Any], timeout: float = 300, url: str = URL_IA,
        ao_evento: Callable[[Any], None] | None = None,
    ) -> RespostaIA:
        """POST para a API de IA (destinos em ``validar_url_ia``). Nunca levanta exceção.

        ``ao_evento``: resposta em streaming (SSE): cada evento JSON chega a ``ao_evento`` assim que o provedor manda,
        e a resposta volta com ``corpo = {"_eventos": [...]}`` (quem chama junta, ``msscan/ia/fluxo.py``).
        Executor sem streaming devolve a resposta inteira, como sem ``ao_evento``.
        No histórico entram só método e URL: a chave (cabeçalhos) e os dados enviados nunca.
        """
        inicio = time.monotonic()
        motivo = validar_url_ia(url)
        if motivo:
            resposta = RespostaIA(None, erro_tipo="bloqueado", erro=motivo)
        elif self.ia_cancelada.is_set():
            resposta = RespostaIA(None, erro_tipo="cancelado", erro=MENSAGEM_CANCELADO)
        else:
            resposta = self._post_ia_cancelavel(url, cabecalhos, corpo, timeout, ao_evento)
        self.historico.append(
            Execucao(
                ["POST", url, "(dados e chave omitidos)"],
                resposta.status if resposta.status is not None else RC_FALHA_EXECUCAO,
                time.monotonic() - inicio,
                self.origem,
                resposta.erro,
            )
        )
        return resposta

    def _post_ia_cancelavel(self, url: str, cabecalhos: dict[str, str], corpo: dict[str, Any], timeout: float,
                            ao_evento: Callable[[Any], None] | None) -> RespostaIA:  # fmt: skip
        """O POST roda numa thread; "Cancelar envio" devolve NA HORA (a thread termina sozinha e o resultado é descartado;
        no streaming a conexão é fechada e a leitura para no próximo pedaço)."""
        saida: dict[str, RespostaIA] = {}
        limite = timeout if timeout and timeout > 0 else 300

        def evento(e: Any) -> None:
            if self.ia_cancelada.is_set():
                raise _Cancelado()
            if ao_evento is not None:
                ao_evento(e)

        def chamar() -> None:
            try:
                saida["r"] = self._post_ia(url, cabecalhos, corpo, limite) if ao_evento is None else \
                    self._post_ia_fluxo(url, cabecalhos, corpo, limite, evento)  # fmt: skip
            except _Cancelado:
                saida["r"] = RespostaIA(None, erro_tipo="cancelado", erro=MENSAGEM_CANCELADO)
            except Exception as erro:  # noqa: BLE001
                saida["r"] = RespostaIA(None, erro_tipo="outro", erro=f"erro inesperado: {erro}")

        tarefa = threading.Thread(target=chamar, daemon=True, name="msscan-ia")
        tarefa.start()
        while tarefa.is_alive():
            tarefa.join(0.1)
            if self.ia_cancelada.is_set():
                return RespostaIA(None, erro_tipo="cancelado", erro=MENSAGEM_CANCELADO)
        return saida.get("r") or RespostaIA(None, erro_tipo="outro", erro="sem resposta")

    def cancelar_ia(self) -> None:
        """"Cancelar envio": interrompe a requisição à IA em andamento e as tentativas seguintes."""
        self.ia_cancelada.set()
        resposta = self._resposta_ia
        if resposta is not None:
            try:
                resposta.close()  # streaming: fecha a conexão (a leitura da thread termina)
            except Exception:  # noqa: BLE001
                pass

    def historico_dict(self) -> list[dict[str, Any]]:
        """Histórico de comandos em formato serializável."""
        return [e.para_dict() for e in self.historico]

    # Implementações concretas -------------------------------------------------

    def _rodar(self, comando: list[str], timeout: float) -> tuple[int, str, str, float]:
        raise NotImplementedError

    def _rodar_gravacao(self, comando: list[str], timeout: float | None) -> tuple[int, str, str, float]:
        """Programa que grava firmware (``DUP -q``). Padrão: igual a ``_rodar`` (executores de teste)."""
        return self._rodar(comando, timeout or 3600)

    def _ler_arquivo(self, caminho: str) -> str | None:
        raise NotImplementedError

    def _existe(self, caminho: str) -> bool:
        raise NotImplementedError

    def _listar(self, caminho: str) -> list[str]:
        raise NotImplementedError

    def _tem_comando(self, nome: str) -> bool:
        raise NotImplementedError

    def _http_get(self, url: str, timeout: float) -> RespostaHttp:
        raise NotImplementedError

    def _redfish(
        self, metodo: str, url: str, token: str, corpo: dict[str, Any] | None, timeout: float
    ) -> RespostaRedfish:
        return RespostaRedfish(url, None, erro_tipo="outro", erro="Redfish não suportado por este executor")

    def _post_ia(self, url: str, cabecalhos: dict[str, str], corpo: dict[str, Any], timeout: float) -> RespostaIA:
        return RespostaIA(None, erro_tipo="outro", erro="IA não suportada por este executor")

    def _post_ia_fluxo(self, url: str, cabecalhos: dict[str, str], corpo: dict[str, Any], timeout: float,
                       ao_evento: Callable[[Any], None]) -> RespostaIA:  # fmt: skip
        """Sem streaming neste executor: resposta inteira (quem chama trata os dois formatos)."""
        return self._post_ia(url, cabecalhos, corpo, timeout)

    def _baixar(self, url: str, destino: str, limite_bytes: int, timeout: float) -> RespostaDownload:
        return RespostaDownload(url, None, erro_tipo="outro", erro="download não suportado por este executor")

    def _extrair_idrac_tools(self, pacote: str, destino: str) -> Retorno:
        return RC_FALHA_EXECUCAO, "", "extração não suportada por este executor"

    def _bmc_escrita(self, operacao: str, metodo: str, url: str, token: str, corpo: Any, arquivo: str | None,
                     timeout: float) -> RespostaRedfish:  # fmt: skip
        return RespostaRedfish(url, None, erro_tipo="outro", erro="escrita no BMC não suportada por este executor")

    def _acao_bmc(self, metodo: str, url: str, token: str, corpo: Any, timeout: float) -> RespostaRedfish:
        return RespostaRedfish(url, None, erro_tipo="outro", erro="ação no BMC não suportada por este executor")


AMBIENTE_COMANDOS = {"LC_ALL": "C", "LANG": "C", "TERM": "vt100"}
"""``TERM=vt100``: o DUP Linux da Dell (``.BIN``) sem terminal e com outro ``TERM`` (``linux``, ``xterm``) tenta se reabrir num
``xterm``, que a ISO não tem: saía com 127 sem rodar (R620, 16/10/2026). Com ``vt100`` ele roda direto."""


class ExecutorReal(Executor):
    """Executa comandos de verdade na máquina, sempre com timeout e locale C."""

    origem = "real"
    prepara_ferramentas = True
    """Pode preparar ferramentas da Dell na RAM do sistema live (racadm do iDRAC Tools, CLAUDE.md 19.1); replay e coleta não."""

    def _rodar_gravacao(self, comando: list[str], timeout: float | None) -> tuple[int, str, str, float]:
        """``DUP -q`` desacoplado do MSscan (revisão independente, achados ALTO 1 e MÉDIO 7).

        Grupo de processos próprio (sinal para o MSscan não chega ao DUP) e saída num arquivo, não num pipe (se o
        MSscan morrer, o DUP não recebe SIGPIPE ao escrever o progresso). Tempo esgotado: o DUP NÃO é morto
        (interromper a gravação pode inutilizar o componente); a mensagem manda não desligar.
        """
        ambiente = dict(os.environ, **AMBIENTE_COMANDOS)
        inicio = time.monotonic()
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as saida:
            try:
                proc = subprocess.Popen(comando, stdout=saida, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                        env=ambiente, start_new_session=True)  # fmt: skip
            except OSError as erro:
                return RC_FALHA_EXECUCAO, "", f"não foi possível executar {comando[0]}: {erro}", time.monotonic() - inicio
            try:
                rc = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:  # só com limite explícito (testes); o padrão espera o DUP terminar
                return (RC_FALHA_EXECUCAO, "", f"o pacote ainda está rodando depois de {int((timeout or 0) // 60)} min: NÃO desligue "
                        "nem reinicie; acompanhe no terminal (ps) e no iDRAC", time.monotonic() - inicio)  # fmt: skip
            saida.seek(0)
            return rc, saida.read(), "", time.monotonic() - inicio

    def _rodar(self, comando: list[str], timeout: float) -> tuple[int, str, str, float]:
        ambiente = dict(os.environ, **AMBIENTE_COMANDOS)
        inicio = time.monotonic()
        try:
            proc = subprocess.run(
                comando,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
                stdin=subprocess.DEVNULL,
                env=ambiente,
                check=False,
            )
        except FileNotFoundError:
            return RC_FALHA_EXECUCAO, "", f"comando não encontrado: {comando[0]}", time.monotonic() - inicio
        except PermissionError:
            return RC_FALHA_EXECUCAO, "", f"sem permissão para executar: {comando[0]}", time.monotonic() - inicio
        except subprocess.TimeoutExpired:
            return RC_FALHA_EXECUCAO, "", f"tempo esgotado após {timeout:g} s", time.monotonic() - inicio
        except OSError as erro:
            return RC_FALHA_EXECUCAO, "", f"falha ao executar: {erro}", time.monotonic() - inicio
        return proc.returncode, proc.stdout, proc.stderr, time.monotonic() - inicio

    def _extrair_idrac_tools(self, pacote: str, destino: str) -> Retorno:
        import tarfile

        if not hasattr(tarfile, "data_filter"):
            return RC_FALHA_EXECUCAO, "", "Python sem o filtro seguro do tarfile: extração recusada"
        shutil.rmtree(destino, ignore_errors=True)
        os.makedirs(destino, mode=0o700)
        try:
            with tarfile.open(pacote, "r:gz") as arquivo:
                arquivo.extractall(destino, filter="data")  # sem links para fora, dispositivos nem caminhos absolutos
        except (OSError, tarfile.TarError) as erro:
            shutil.rmtree(destino, ignore_errors=True)
            return 2, "", f"pacote ilegível: {erro}"
        return 0, "", ""

    def _ler_arquivo(self, caminho: str) -> str | None:
        return Path(caminho).read_text(encoding="utf-8", errors="replace")

    def _existe(self, caminho: str) -> bool:
        return os.path.exists(caminho)

    def _listar(self, caminho: str) -> list[str]:
        return os.listdir(caminho)

    def _tem_comando(self, nome: str) -> bool:
        return shutil.which(nome) is not None

    def _http_get(self, url: str, timeout: float) -> RespostaHttp:
        try:
            import requests
        except ImportError:
            return RespostaHttp(url, None, "outro", "python3-requests não instalado")
        try:
            with requests.get(url, timeout=timeout, stream=True, allow_redirects=False) as resposta:
                return RespostaHttp(url, resposta.status_code)
        except requests.exceptions.RequestException as erro:
            tipo, mensagem = classificar_erro_http(erro)
            return RespostaHttp(url, None, tipo, mensagem)

    def _redfish(
        self, metodo: str, url: str, token: str, corpo: dict[str, Any] | None, timeout: float
    ) -> RespostaRedfish:
        try:
            import requests
            import urllib3
        except ImportError:
            return RespostaRedfish(url, None, erro_tipo="outro", erro="python3-requests não instalado")
        # BMC usa certificado autoassinado; a conexão é na rede local do servidor.
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        cabecalhos = {"Accept": "application/json", "OData-Version": "4.0"}
        if token:
            cabecalhos["X-Auth-Token"] = token
        host = _host_url(url)
        falhas: list[str] = []
        # Modos de criptografia só para o BMC (28.2): o que já funcionou com este BMC primeiro, depois os demais, em ordem.
        for modo in tls_bmc.ordem(self.modos_tls_bmc.get(host)):
            with requests.Session() as sessao:
                sessao.trust_env = False  # o BMC é acessado direto, nunca pelo proxy da internet
                tls_bmc.montar_sessao(sessao, modo)
                try:
                    resposta = sessao.request(
                        metodo, url, headers=cabecalhos, json=corpo, timeout=timeout, verify=False, allow_redirects=False
                    )
                except requests.exceptions.RequestException as erro:
                    tipo, mensagem = classificar_erro_http(erro)
                    if tls_bmc.vale_outro_modo(tipo, mensagem, metodo):
                        falhas.append(f"{tls_bmc.DESCRICAO[modo]}: {mensagem}")
                        continue
                    return RespostaRedfish(url, None, erro_tipo=tipo, erro=mensagem)
                self.modos_tls_bmc[host] = modo
                break
        else:
            return RespostaRedfish(url, None, erro_tipo="tls", erro="nenhum modo de criptografia aceito pelo BMC: " + " | ".join(falhas))
        with resposta:
            try:
                dados = resposta.json() if resposta.content else None
            except ValueError:
                dados = None
            return RespostaRedfish(
                url,
                resposta.status_code,
                corpo=dados,
                token=resposta.headers.get("X-Auth-Token", ""),
                local=resposta.headers.get("Location", ""),
            )

    def _bmc_escrita(self, operacao: str, metodo: str, url: str, token: str, corpo: Any, arquivo: str | None,
                     timeout: float) -> RespostaRedfish:  # fmt: skip
        """Implementação real (só alcançável sem a trava). MOCKUP(rodada final): multipart com as partes
        ``UpdateParameters`` (JSON) e ``UpdateFile``, como no exemplo oficial da Dell; validar no iDRAC9 real."""
        try:
            import requests
            import urllib3
        except ImportError:
            return RespostaRedfish(url, None, erro_tipo="outro", erro="python3-requests não instalado")
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        cabecalhos = {"Accept": "application/json", "OData-Version": "4.0", "X-Auth-Token": token}
        with requests.Session() as sessao:
            sessao.trust_env = False
            # Só o modo de criptografia já aprendido com este BMC (28.2): uma escrita nunca é repetida em outro modo.
            tls_bmc.montar_sessao(sessao, self.modos_tls_bmc.get(_host_url(url), tls_bmc.PADRAO))
            try:
                # Envio do pacote: o urllib3 2.x escreve o corpo com o tempo de CONEXÃO; com 15 s, o iDRAC7 2.65 do R620 parava
                # de receber o BIOS no meio ("The write operation timed out", 19/10/2026). O tempo do envio vale para as duas fases.
                if operacao == ENVIAR_FIRMWARE_LEGADO:
                    etag = sessao.get(url, headers=cabecalhos, timeout=(15, 60), verify=False, allow_redirects=False).headers.get("ETag", "")
                    with open(str(arquivo), "rb") as conteudo:
                        partes = {"file": (os.path.basename(str(arquivo)), conteudo, "multipart/form-data")}
                        resposta = sessao.post(url, files=partes, headers={**cabecalhos, "if-match": etag}, timeout=(timeout, timeout),
                                               verify=False, allow_redirects=False)  # fmt: skip
                elif operacao == ENVIAR_FIRMWARE:
                    # Corpo multipart enviado em blocos, contando os bytes (progresso do envio, CLAUDE.md 21.1); as partes e
                    # os nomes são os mesmos do ``files=`` do requests usado antes (exemplo oficial da Dell).
                    with CorpoMultipart(json.dumps(corpo), str(arquivo), self.progresso_envio) as envio:
                        resposta = sessao.post(url, data=envio, headers={**cabecalhos, "Content-Type": envio.tipo},
                                               timeout=(timeout, timeout), verify=False, allow_redirects=False)  # fmt: skip
                else:
                    resposta = sessao.request(metodo, url, json=corpo, headers=cabecalhos, timeout=(15, timeout),
                                              verify=False, allow_redirects=False)  # fmt: skip
            except requests.exceptions.RequestException as erro:
                tipo, mensagem = classificar_erro_http(erro)
                return RespostaRedfish(url, None, erro_tipo=tipo, erro=mensagem)
            except OSError as erro:
                return RespostaRedfish(url, None, erro_tipo="disco", erro=f"não foi possível ler {arquivo}: {erro}")
            try:
                dados = resposta.json() if resposta.content else None
            except ValueError:
                dados = None
            return RespostaRedfish(url, resposta.status_code, corpo=dados, local=resposta.headers.get("Location", ""))

    def _acao_bmc(self, metodo: str, url: str, token: str, corpo: Any, timeout: float) -> RespostaRedfish:
        """Implementação real (só alcançável sem a trava): PATCH/POST JSON, como a escrita da fase 9. MOCKUP(rodada final)."""
        return self._bmc_escrita(LIMPAR_SEL, metodo, url, token, corpo, None, timeout)

    def _baixar(self, url: str, destino: str, limite_bytes: int, timeout: float) -> RespostaDownload:
        """Download com retomada: queda de rede mantém o ``.parcial``; a próxima chamada continua com ``Range``."""
        import hashlib
        from urllib.parse import urljoin

        try:
            import requests
        except ImportError:
            return RespostaDownload(url, None, erro_tipo="outro", erro="python3-requests não instalado")
        atual = url
        temporario = Path(destino + ".parcial")
        try:
            if temporario.is_file() and time.time() - temporario.stat().st_mtime > PARCIAL_VALIDO_S:
                temporario.unlink()
            ja = temporario.stat().st_size if temporario.is_file() else 0
            for _ in range(MAX_REDIRECIONAMENTOS + 2):
                cabecalhos = {"Range": f"bytes={ja}-"} if ja else {}
                with requests.get(atual, stream=True, timeout=(15, timeout), allow_redirects=False, headers=cabecalhos) as resposta:
                    if resposta.status_code in (301, 302, 303, 307, 308):
                        proximo = urljoin(atual, resposta.headers.get("Location", ""))
                        motivo = validar_download(proximo)
                        if motivo:
                            return RespostaDownload(url, resposta.status_code, erro_tipo="bloqueado", erro=f"redirecionamento recusado: {motivo}")
                        atual = proximo
                        continue
                    retomar = ja > 0 and resposta.status_code == 206 and \
                        resposta.headers.get("Content-Range", "").startswith(f"bytes {ja}-")  # fmt: skip
                    if ja and not retomar and resposta.status_code in (206, 416):
                        temporario.unlink(missing_ok=True)  # parcial não serve: recomeça do zero
                        ja = 0
                        continue
                    if resposta.status_code != 200 and not retomar:
                        return RespostaDownload(url, resposta.status_code, erro_tipo="http", erro=f"HTTP {resposta.status_code}")
                    inicio_bytes = ja if retomar else 0
                    soma, tamanho = hashlib.sha256(), inicio_bytes
                    if retomar:  # o hash sempre cobre o arquivo inteiro, inclusive o que já estava gravado
                        with open(temporario, "rb") as existente:
                            for bloco in iter(lambda: existente.read(1024 * 1024), b""):
                                soma.update(bloco)
                    Path(destino).parent.mkdir(parents=True, exist_ok=True)
                    with open(temporario, "ab" if retomar else "wb") as arquivo:
                        for bloco in resposta.iter_content(1024 * 256):
                            tamanho += len(bloco)
                            if tamanho > limite_bytes:
                                arquivo.close()
                                temporario.unlink(missing_ok=True)
                                return RespostaDownload(url, 200, erro_tipo="tamanho", erro=f"maior que o limite de {limite_bytes} bytes")
                            soma.update(bloco)
                            arquivo.write(bloco)
                        arquivo.flush()
                        os.fsync(arquivo.fileno())
                    os.replace(temporario, destino)
                    return RespostaDownload(url, 200, destino, tamanho, soma.hexdigest(), retomado_de=inicio_bytes)
            return RespostaDownload(url, None, erro_tipo="outro", erro="redirecionamentos demais")
        except requests.exceptions.RequestException as erro:
            # Queda de rede: o .parcial fica para a próxima chamada continuar de onde parou.
            tipo, mensagem = classificar_erro_http(erro)
            return RespostaDownload(url, None, erro_tipo=tipo, erro=mensagem)
        except OSError as erro:
            temporario.unlink(missing_ok=True)
            return RespostaDownload(url, None, erro_tipo="disco", erro=f"não foi possível gravar {destino}: {erro}")

    def _post_ia(self, url: str, cabecalhos: dict[str, str], corpo: dict[str, Any], timeout: float) -> RespostaIA:
        try:
            import requests
        except ImportError:
            return RespostaIA(None, erro_tipo="outro", erro="python3-requests não instalado")
        try:
            with requests.Session() as sessao:
                # Internet: respeita o proxy de config/proxy.conf (variáveis de ambiente); verificação TLS normal.
                # Ollama na rede local (HTTP): direto, sem proxy.
                sessao.trust_env = url.startswith("https://")
                resposta = sessao.post(url, headers=cabecalhos, json=corpo, timeout=(15, timeout), allow_redirects=False)
        except requests.exceptions.RequestException as erro:
            tipo, mensagem = classificar_erro_http(erro)
            return RespostaIA(None, erro_tipo=tipo, erro=mensagem)
        try:
            dados = resposta.json() if resposta.content else None
        except ValueError:
            dados = None
        return RespostaIA(resposta.status_code, dados, espera_s=_retry_after(resposta.headers.get("retry-after")))

    def _post_ia_fluxo(self, url: str, cabecalhos: dict[str, str], corpo: dict[str, Any], timeout: float,
                       ao_evento: Callable[[Any], None]) -> RespostaIA:  # fmt: skip
        """Streaming (SSE): lê linha a linha as ``data: {...}``; ``timeout`` vale entre um pedaço e outro."""
        try:
            import requests
        except ImportError:
            return RespostaIA(None, erro_tipo="outro", erro="python3-requests não instalado")
        eventos: list[Any] = []
        try:
            with requests.Session() as sessao:
                sessao.trust_env = url.startswith("https://")
                with sessao.post(url, headers=cabecalhos, json=corpo, timeout=(15, timeout), allow_redirects=False,
                                 stream=True) as resposta:  # fmt: skip
                    self._resposta_ia = resposta
                    espera_s = _retry_after(resposta.headers.get("retry-after"))
                    if resposta.status_code != 200:
                        try:
                            dados = resposta.json() if resposta.content else None
                        except ValueError:
                            dados = None
                        return RespostaIA(resposta.status_code, dados, espera_s=espera_s)
                    for linha in resposta.iter_lines(decode_unicode=False):
                        texto = linha.decode("utf-8", errors="replace").strip() if linha else ""
                        if not texto.startswith("data:"):
                            continue
                        carga = texto[5:].strip()
                        if not carga or carga == "[DONE]":
                            continue
                        try:
                            evento = json.loads(carga)
                        except ValueError:
                            continue
                        eventos.append(evento)
                        ao_evento(evento)
        except requests.exceptions.RequestException as erro:
            if self.ia_cancelada.is_set():
                return RespostaIA(None, erro_tipo="cancelado", erro=MENSAGEM_CANCELADO)
            tipo, mensagem = classificar_erro_http(erro)
            return RespostaIA(None, erro_tipo=tipo, erro=mensagem)
        finally:
            self._resposta_ia = None
        return RespostaIA(200, {"_eventos": eventos})


MENSAGEM_CANCELADO = "envio cancelado pelo técnico"


class _Cancelado(Exception):
    """Interrompe a leitura do streaming quando o técnico cancela."""


def _retry_after(valor: str | None) -> float | None:
    try:
        return float(valor) if valor else None
    except ValueError:
        return None


def _caminho_espelho(pasta: Path, caminho: str) -> Path:
    """Converte um caminho absoluto do sistema no espelho ``arquivos/`` da fixture."""
    relativo = Path(caminho.lstrip("/"))
    if ".." in relativo.parts:
        raise ValueError(f"caminho inválido: {caminho}")
    return pasta / "arquivos" / relativo


def _ler_json(arquivo: Path) -> Any:
    if not arquivo.is_file():
        return {}
    return json.loads(arquivo.read_text(encoding="utf-8"))


class ExecutorReplay(Executor):
    """Devolve saídas gravadas numa pasta de fixture, sem executar nada.

    Comandos sem gravação se comportam como ferramenta ausente (``rc = -1``).
    Com ``tempo_real=True`` cada comando espera a duração gravada (modo
    demonstração da interface gráfica, para o progresso ser o de um servidor real).
    """

    origem = "replay"

    def __init__(self, pasta: str | os.PathLike[str], tempo_real: bool = False) -> None:
        super().__init__()
        self.pasta = Path(pasta)
        self.tempo_real = tempo_real
        if not self.pasta.is_dir():
            raise FileNotFoundError(f"pasta de replay não encontrada: {self.pasta}")
        self._comandos: dict[tuple[str, ...], dict[str, Any]] = {}
        for meta_arquivo in sorted((self.pasta / "comandos").glob("*.json")):
            meta = json.loads(meta_arquivo.read_text(encoding="utf-8"))
            meta["_stdout"] = meta_arquivo.with_suffix(".stdout")
            self._comandos[tuple(meta["comando"])] = meta
        self._caminhos: dict[str, bool] = _ler_json(self.pasta / "caminhos.json")
        self._diretorios: dict[str, list[str]] = _ler_json(self.pasta / "diretorios.json")
        self._ferramentas: dict[str, bool] = _ler_json(self.pasta / "ferramentas.json")
        self._http: dict[str, dict[str, Any]] = _ler_json(self.pasta / "http.json")
        self._redfish_gravado: dict[str, dict[str, Any]] = _ler_json(self.pasta / "redfish.json")
        self._ia: dict[str, Any] = _ler_json(self.pasta / "ia.json")
        self._downloads: dict[str, str] = _ler_json(self.pasta / "downloads.json")

    def _rodar(self, comando: list[str], timeout: float) -> tuple[int, str, str, float]:
        meta = self._comandos.get(tuple(comando))
        if meta is None:
            return RC_FALHA_EXECUCAO, "", "comando sem gravação nesta fixture", 0.0
        stdout_arquivo: Path = meta["_stdout"]
        saida = stdout_arquivo.read_text(encoding="utf-8") if stdout_arquivo.is_file() else ""
        duracao = float(meta.get("duracao", 0.0))
        if self.tempo_real:
            time.sleep(min(duracao, timeout))
        return int(meta.get("rc", 0)), saida, meta.get("stderr", ""), duracao

    def _ler_arquivo(self, caminho: str) -> str | None:
        espelho = _caminho_espelho(self.pasta, caminho)
        if not espelho.is_file():
            return None
        return espelho.read_text(encoding="utf-8", errors="replace")

    def _existe(self, caminho: str) -> bool:
        if caminho in self._caminhos:
            return bool(self._caminhos[caminho])
        if caminho in self._diretorios:
            return True
        return _caminho_espelho(self.pasta, caminho).exists()

    def _listar(self, caminho: str) -> list[str]:
        if caminho in self._diretorios:
            return list(self._diretorios[caminho])
        espelho = _caminho_espelho(self.pasta, caminho)
        return os.listdir(espelho) if espelho.is_dir() else []

    def _tem_comando(self, nome: str) -> bool:
        if nome in self._ferramentas:
            return bool(self._ferramentas[nome])
        # Sem registro explícito: a ferramenta existia se algum comando dela rodou.
        return any(cmd[0] == nome and meta.get("rc", 0) != RC_FALHA_EXECUCAO for cmd, meta in self._comandos.items())

    def _http_get(self, url: str, timeout: float) -> RespostaHttp:
        gravado = self._http.get(url)
        if gravado is None:
            return RespostaHttp(url, None, "conexao", "requisição sem gravação nesta fixture")
        return RespostaHttp(url, gravado.get("status"), gravado.get("erro_tipo", ""), gravado.get("erro", ""))

    def _redfish(
        self, metodo: str, url: str, token: str, corpo: dict[str, Any] | None, timeout: float
    ) -> RespostaRedfish:
        gravado = self._redfish_gravado.get(f"{metodo} {caminho_url(url)}")
        if gravado is None:
            return RespostaRedfish(url, None, erro_tipo="conexao", erro="requisição sem gravação nesta fixture")
        status = gravado.get("status")
        # Login gravado com sucesso: o replay entrega um token fictício (o real nunca é gravado).
        token_replay = "token-replay" if metodo == "POST" and isinstance(status, int) and 200 <= status < 300 else ""
        return RespostaRedfish(
            url, status, corpo=gravado.get("corpo"), token=token_replay, local=gravado.get("local", ""),
            erro_tipo=gravado.get("erro_tipo", ""), erro=gravado.get("erro", ""),
        )  # fmt: skip

    def _baixar(self, url: str, destino: str, limite_bytes: int, timeout: float) -> RespostaDownload:
        import hashlib

        relativo = self._downloads.get(url)
        if relativo is None:
            return RespostaDownload(url, None, erro_tipo="conexao", erro="download sem gravação nesta fixture")
        conteudo = (self.pasta / relativo).read_bytes()
        Path(destino).parent.mkdir(parents=True, exist_ok=True)
        Path(destino).write_bytes(conteudo)
        return RespostaDownload(url, 200, destino, len(conteudo), hashlib.sha256(conteudo).hexdigest())

    def _post_ia(self, url: str, cabecalhos: dict[str, str], corpo: dict[str, Any], timeout: float) -> RespostaIA:
        if not self._ia:
            return RespostaIA(None, erro_tipo="conexao", erro="requisição sem gravação nesta fixture")
        return RespostaIA(self._ia.get("status"), self._ia.get("corpo"))

    def _post_ia_fluxo(self, url: str, cabecalhos: dict[str, str], corpo: dict[str, Any], timeout: float,
                       ao_evento: Callable[[Any], None]) -> RespostaIA:  # fmt: skip
        """Gravação com ``eventos`` (streaming): entrega um por um; sem ela, a resposta inteira."""
        if self._ia and isinstance(self._ia.get("eventos"), list):
            for evento in self._ia["eventos"]:
                ao_evento(evento)
            return RespostaIA(200, {"_eventos": list(self._ia["eventos"])})
        return self._post_ia(url, cabecalhos, corpo, timeout)


class ExecutorGravador(Executor):
    """Executa por meio de outro executor e grava tudo no formato de fixture.

    Usado pelo modo ``--coletar``. Não filtra comandos: quem chama é responsável
    por pedir apenas comandos de leitura.
    """

    def __init__(self, pasta: str | os.PathLike[str], base: Executor | None = None) -> None:
        super().__init__()
        self.pasta = Path(pasta)
        self.base = base or ExecutorReal()
        self.origem = self.base.origem
        self.modos_tls_bmc = self.base.modos_tls_bmc
        (self.pasta / "comandos").mkdir(parents=True, exist_ok=True)
        self._caminhos: dict[str, bool] = _ler_json(self.pasta / "caminhos.json")
        self._diretorios: dict[str, list[str]] = _ler_json(self.pasta / "diretorios.json")
        self._ferramentas: dict[str, bool] = _ler_json(self.pasta / "ferramentas.json")
        self._http: dict[str, dict[str, Any]] = _ler_json(self.pasta / "http.json")
        self._redfish_gravado: dict[str, dict[str, Any]] = _ler_json(self.pasta / "redfish.json")

    def _rodar(self, comando: list[str], timeout: float) -> tuple[int, str, str, float]:
        rc, saida, erro = self.base.rodar(comando, timeout)
        duracao = self.base.historico[-1].duracao if self.base.historico else 0.0
        nome = nome_arquivo_comando(comando)
        (self.pasta / "comandos" / f"{nome}.stdout").write_text(saida, encoding="utf-8")
        meta = {"comando": comando, "rc": rc, "stderr": erro, "duracao": round(duracao, 3)}
        (self.pasta / "comandos" / f"{nome}.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return rc, saida, erro, duracao

    def _ler_arquivo(self, caminho: str) -> str | None:
        conteudo = self.base.ler_arquivo(caminho)
        if conteudo is not None:
            espelho = _caminho_espelho(self.pasta, caminho)
            espelho.parent.mkdir(parents=True, exist_ok=True)
            espelho.write_text(conteudo, encoding="utf-8")
        return conteudo

    def _existe(self, caminho: str) -> bool:
        existe = self.base.existe(caminho)
        self._caminhos[caminho] = existe
        self._salvar_json("caminhos.json", self._caminhos)
        return existe

    def _listar(self, caminho: str) -> list[str]:
        nomes = self.base.listar(caminho)
        self._diretorios[caminho] = nomes
        self._salvar_json("diretorios.json", self._diretorios)
        return nomes

    def _tem_comando(self, nome: str) -> bool:
        existe = self.base.tem_comando(nome)
        self._ferramentas[nome] = existe
        self._salvar_json("ferramentas.json", self._ferramentas)
        return existe

    def _http_get(self, url: str, timeout: float) -> RespostaHttp:
        resposta = self.base.http_get(url, timeout)
        self._http[url] = resposta.para_dict()
        self._salvar_json("http.json", self._http)
        return resposta

    def _redfish(
        self, metodo: str, url: str, token: str, corpo: dict[str, Any] | None, timeout: float
    ) -> RespostaRedfish:
        resposta = self.base.redfish(metodo, url, token, corpo, timeout)
        registro: dict[str, Any] = {"status": resposta.status, "erro_tipo": resposta.erro_tipo, "erro": resposta.erro}
        if metodo == "GET":
            registro["corpo"] = resposta.corpo
        elif metodo == "POST":
            registro["local"] = caminho_url(resposta.local) if resposta.local else ""  # token e corpo: nunca
        self._redfish_gravado[f"{metodo} {caminho_url(url)}"] = registro
        self._salvar_json("redfish.json", self._redfish_gravado)
        return resposta

    def _salvar_json(self, nome: str, dados: Any) -> None:
        (self.pasta / nome).write_text(
            json.dumps(dados, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
