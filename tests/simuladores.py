"""Simuladores para os testes de ponta a ponta da atualização automática (CLAUDE.md 18.6 e 18.7).

Nada aqui alcança o R640 onde os testes rodam:

- ``SimuladorIPMI``: o ``ipmi_sim`` do OpenIPMI (pacote Debian ``openipmi``) num pseudoterminal, ouvindo só em
  ``localhost``; o ``ipmitool`` do MSscan é trocado por um script que SEMPRE fala com ele por ``lanplus`` (nunca com o
  ``/dev/ipmi0`` do servidor real);
- ``BMCSimulado``: iDRAC9 falso por HTTPS (certificado gerado na hora) com sessões (login conferido no ``ipmi_sim``: o
  usuário temporário criado pelo IPMI entra no Redfish com a senha dele), ``MultipartHttpPushUri``, ``TaskService``, boot
  único, exportação e o ``DellSoftwareInstallationService`` (repositório: lê o ``Catalog.xml`` e baixa os pacotes do
  servidor HTTP do MSscan, conferindo o SHA-256 de cada um);
- ``racadm`` e DUPs (``.BIN``) falsos, scripts em ``bash`` com estado num arquivo;
- ``ExecutorSimulador``: um ``ExecutorReal`` (HTTP e processos de verdade) que só executa os simuladores acima — qualquer
  outro comando é recusado; ``systemctl reboot`` só é contado.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from email.parser import BytesParser
from email.policy import HTTP as POLITICA_HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from msscan.core.executor import ExecutorReal

FIXTURES = Path(__file__).parent / "fixtures" / "redfish"
DELL = FIXTURES / "dell_idrac9_mockup"
SENHA_ROOT = "calvin"
SISTEMA = "/redfish/v1/Systems/System.Embedded.1"
GERENTE = "/redfish/v1/Managers/iDRAC.Embedded.1"
SERVICO = SISTEMA + "/Oem/Dell/DellSoftwareInstallationService"
CANCELAR = "/redfish/v1/Dell/Managers/iDRAC.Embedded.1/DellJobService/Actions/DellJobService.DeleteJobQueue"


def porta_livre(tipo: int = socket.SOCK_STREAM, ip: str = "127.0.0.1") -> int:
    with socket.socket(socket.AF_INET, tipo) as s:
        s.bind((ip, 0))
        return s.getsockname()[1]


# --- IPMI ------------------------------------------------------------------------------------------------------------


class SimuladorIPMI:
    """``with SimuladorIPMI(pasta) as ipmi: ipmi.wrapper`` → script ``ipmitool`` que só fala com o simulador."""

    def __init__(self, pasta: Path) -> None:
        self.pasta = Path(pasta)
        self.pasta.mkdir(parents=True, exist_ok=True)
        self.porta = porta_livre(socket.SOCK_DGRAM)
        self.base = ["/usr/bin/ipmitool", "-I", "lanplus", "-H", "localhost", "-p", str(self.porta), "-N", "1", "-R", "3"]
        (self.pasta / "lan.conf").write_text(f"""name "msscansim"
set_working_mc 0x20
  startlan 1
    addr localhost {self.porta}
    priv_limit admin
    allowed_auths_callback none md2 md5 straight
    allowed_auths_user none md2 md5 straight
    allowed_auths_operator none md2 md5 straight
    allowed_auths_admin none md2 md5 straight
    guid a123456789abcdefa123456789abcdef
  endlan
  user 1 true  ""     "test"   user  10 none md2 md5 straight
  user 2 true  "root" "{SENHA_ROOT}" admin 10 none md2 md5 straight
""")
        (self.pasta / "sim.emu").write_text("mc_setbmc 0x20\nmc_add 0x20 0 no-device-sdrs 0x23 9 8 0x9f 0x1291 0xf02 persist_sdr\n"
                                            "sel_enable 0x20 1000 0x0a\nmc_enable 0x20\n")  # fmt: skip
        (self.pasta / "estado").mkdir(exist_ok=True)
        self.wrapper = self.pasta / "bin" / "ipmitool"
        self.wrapper.parent.mkdir(exist_ok=True)
        # Sempre "-H localhost -p <porta do simulador>": nunca o /dev/ipmi0 do servidor onde os testes rodam.
        self.wrapper.write_text("#!/bin/sh\nexec " + " ".join(self.base + ["-U", "root", "-P", SENHA_ROOT]) + ' "$@"\n')
        self.wrapper.chmod(0o755)
        self.processo: subprocess.Popen | None = None

    def __enter__(self) -> SimuladorIPMI:
        mestre, escravo = os.openpty()
        self._pty = mestre
        self.processo = subprocess.Popen(
            ["ipmi_sim", "-c", str(self.pasta / "lan.conf"), "-f", str(self.pasta / "sim.emu"), "-s", str(self.pasta / "estado")],
            stdin=escravo, stdout=escravo, stderr=escravo, start_new_session=True)  # fmt: skip
        os.close(escravo)
        for _ in range(50):
            if self.rodar(["mc", "info"]).returncode == 0:
                break
            time.sleep(0.2)
        else:
            self.__exit__()
            raise RuntimeError("ipmi_sim não respondeu")
        # Como no iDRAC real: slots 3 a 16 sem nome e sem acesso.
        for slot in range(3, 17):
            self.rodar(["channel", "setaccess", "1", str(slot), "callin=off", "ipmi=off", "link=off", "privilege=15"])
        return self

    def __exit__(self, *_: object) -> None:
        if self.processo is not None:
            self.processo.terminate()
            try:
                self.processo.wait(5)
            except subprocess.TimeoutExpired:
                self.processo.kill()
        os.close(self._pty)

    def rodar(self, argumentos: list[str], usuario: str = "root", senha: str = SENHA_ROOT) -> subprocess.CompletedProcess:
        return subprocess.run(self.base + ["-U", usuario, "-P", senha] + argumentos, capture_output=True, text=True, timeout=30)

    def usuarios(self) -> dict[int, tuple[str, str]]:
        """slot → (nome, privilégio)."""
        saida = self.rodar(["user", "list", "1"]).stdout
        usuarios = {}
        for linha in saida.splitlines()[1:]:
            casado = re.match(r"(\d+)\s+(\S*)\s+(true|false)\s+(true|false)\s+(true|false)\s+(.+)$", linha.strip())
            if casado:
                usuarios[int(casado.group(1))] = (casado.group(2), casado.group(6).strip())
        return usuarios

    def login_admin(self, usuario: str, senha: str) -> bool:
        """O usuário entra como ADMINISTRADOR no simulador (senha e privilégio de verdade)?"""
        if not usuario or not senha:
            return False
        if self.rodar(["-L", "ADMINISTRATOR", "mc", "info"], usuario, senha).returncode == 0:
            return True
        # Limitação do ipmi_sim 1.0.13: guarda só os 16 primeiros bytes da senha IPMI 2.0 de 20 (o iDRAC guarda os 20).
        # Senha errada continua recusada (os 16 primeiros têm de bater).
        return len(senha) > 16 and self.rodar(["-L", "ADMINISTRATOR", "mc", "info"], usuario, senha[:16]).returncode == 0


# --- racadm e DUP falsos ---------------------------------------------------------------------------------------------


def criar_racadm(pasta: Path, ip_usb: str, capaz: bool = True, ipmi: SimuladorIPMI | None = None) -> Path:
    """``racadm`` falso (bash): ``get``/``set`` do grupo ``iDRAC.OS-BMC`` num arquivo de estado, ``update -f`` e, com
    ``ipmi``, os ``iDRAC.Users.N.*`` aplicados no mesmo iDRAC simulado (``ipmi_sim``)."""
    pasta.mkdir(parents=True, exist_ok=True)
    estado = pasta / "racadm_estado"
    estado.write_text(f"AdminState=Disabled\nPTMode=lom-p2p\nPTCapability={'Capable' if capaz else 'Not Capable'}\nUsbNicIpAddress={ip_usb}\n")
    script = pasta / "racadm"
    script.write_text(f"""#!/bin/bash
ESTADO="{estado}"
echo "$*" >> "{pasta}/racadm_chamadas"
case "$1" in
  get)
    if [ "$2" = "iDRAC.OS-BMC" ]; then echo "[Key=iDRAC.Embedded.1#OS-BMC.1]"; cat "$ESTADO"; exit 0; fi
    if [ "$2" = "iDRAC.NIC" ]; then echo "[Key=iDRAC.Embedded.1#NIC.1]"; echo "Selection=Dedicated"; exit 0; fi
    echo "ERROR: grupo desconhecido"; exit 1;;
  set)
    if [[ "$2" == iDRAC.Users.* ]]; then
      IFS=. read -r _ _ slot campo <<< "$2"; I="{ipmi.wrapper if ipmi else '/bin/false'}"
      case "$campo" in
        UserName) "$I" user set name "$slot" "$3" >/dev/null;;
        Password) "$I" user set password "$slot" "$3" 20 >/dev/null;;
        Privilege) [ "$3" = "0x1ff" ] && {{ "$I" channel setaccess 1 "$slot" callin=on ipmi=on link=on privilege=4 >/dev/null; "$I" user priv "$slot" 4 1 >/dev/null; }};;
        IpmiLanPrivilege) [ "$3" = "15" ] && "$I" channel setaccess 1 "$slot" callin=off ipmi=off link=off privilege=15 >/dev/null;;
        Enable) [ "$3" = "1" ] && "$I" user enable "$slot" >/dev/null || "$I" user disable "$slot" >/dev/null;;
      esac
      echo "Object value modified successfully"; exit 0
    fi
    chave="${{2##*.}}"; valor="$3"
    [ "$chave" = "AdminState" ] && {{ [ "$valor" = "1" ] && valor=Enabled || valor=Disabled; }}
    [ "$chave" = "PTMode" ] && {{ [ "$valor" = "1" ] && valor=usb-p2p || valor=lom-p2p; }}
    sed -i "s/^$chave=.*/$chave=$valor/" "$ESTADO"; echo "Object value modified successfully"; exit 0;;
  jobqueue)
    if [ "$2" = "view" ]; then cat "{pasta}/racadm_jobs" 2>/dev/null || echo "No job found in the job queue."; exit 0; fi
    if [ "$2" = "delete" ] && [ "$3" = "-i" ]; then rm -f "{pasta}/racadm_jobs"; echo "RAC1032: $4 job(s) was cancelled by the user."; exit 0; fi
    echo "ERROR: só jobqueue view/delete -i é simulado"; exit 1;;
  update)
    if [ -f "{pasta}/racadm_update_falha" ]; then echo "ERROR: RAC987: falha simulada"; exit 1; fi
    # Real (R620, 16/10/2026): caminho com 64+ caracteres -> RAC923, código 4.
    if [ ${{#3}} -ge 64 ]; then echo "ERROR: RAC923 : The file path is too long."; exit 4; fi
    n=$(( $(cat "{pasta}/racadm_jobs" 2>/dev/null | grep -c "Job ID") + 1 ))
    printf '[Job ID=JID_90000000000%s]\nJob Name=Firmware Update: %s\nStatus=%s\nStart Time=[Now]\nMessage=[JCP001: Task successfully scheduled.]\nPercent Complete=[0]\n' "$n" "$(basename "$3")" "$(cat "{pasta}/racadm_estado_job" 2>/dev/null || echo Scheduled)" >> "{pasta}/racadm_jobs"
    echo "RAC987: Firmware update job for $(basename "$3") is initiated."; exit 0;;
esac
echo "ERROR: comando não suportado"; exit 1
""")
    script.chmod(0o755)
    return script


def criar_dup(arquivo: Path, rc_checar: int = 0, rc_aplicar: int = 2) -> Path:
    """DUP Linux falso (``.BIN``): ``-c`` sai com ``rc_checar`` e ``-q`` com ``rc_aplicar`` (tabela oficial da Dell)."""
    arquivo.write_text(f"""#!/bin/bash
echo "$*" >> "{arquivo}.chamadas"
case "$1" in -c) echo "Checagem simulada"; exit {rc_checar};; -q) echo "Aplicação simulada"; exit {rc_aplicar};; esac
exit 1
""")
    return arquivo


# --- BMC Redfish simulado --------------------------------------------------------------------------------------------

PERFIS_TLS = ("moderno", "dh1024", "dh1024_ecdhe", "tls10", "sha1_rsa1024", "idrac6", "so_cifras_antigas")
"""Criptografia de cada geração de iDRAC (CLAUDE.md 28.3): ``dh1024`` = iDRAC8 2.30 do R530 (só DHE com DH de 1024 bits,
TLS 1.2); ``dh1024_ecdhe`` = DHE preferido pelo servidor, ECDHE também aceito; ``tls10`` = só TLS 1.0 (iDRAC7/8 antes da
2.40); ``sha1_rsa1024`` = certificado SHA-1 com RSA de 1024 bits; ``idrac6`` = tudo junto (TLS 1.0, DH 1024, RSA 1024/SHA-1);
``so_cifras_antigas`` = TLS 1.0 com uma cifra fora do DEFAULT do OpenSSL (só o modo legado alcança)."""


def contexto_tls_servidor(pasta: Path, perfil: str = "moderno") -> ssl.SSLContext:
    """Contexto TLS do iDRAC falso no perfil pedido (certificados e DH gerados na hora, em ``pasta``)."""
    import warnings

    def gerar(*args: str) -> None:
        subprocess.run(["openssl", *args], check=True, capture_output=True)

    contexto = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    if perfil == "moderno":
        cert, chave = pasta / "bmc_cert.pem", pasta / "bmc_chave.pem"
        gerar("req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-days", "1",
              "-subj", "/CN=idrac-simulado", "-keyout", str(chave), "-out", str(cert))  # fmt: skip
        contexto.load_cert_chain(cert, chave)
        return contexto
    fraco = perfil in ("sha1_rsa1024", "idrac6")
    cert, chave = pasta / f"bmc_{perfil}_cert.pem", pasta / f"bmc_{perfil}_chave.pem"
    gerar("req", "-x509", "-newkey", "rsa:1024" if fraco else "rsa:2048", *(["-sha1"] if fraco else []), "-nodes", "-days", "1",
          "-subj", "/CN=idrac-antigo", "-keyout", str(chave), "-out", str(cert))  # fmt: skip
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        contexto.maximum_version = ssl.TLSVersion.TLSv1_2  # iDRAC7/8 não têm TLS 1.3 (no 1.3 o DH fraco nem aparece)
        if perfil in ("tls10", "idrac6", "so_cifras_antigas"):
            contexto.minimum_version = ssl.TLSVersion.TLSv1
            contexto.maximum_version = ssl.TLSVersion.TLSv1
    contexto.set_ciphers({"dh1024": "DHE-RSA-AES128-GCM-SHA256:DHE-RSA-AES256-SHA:@SECLEVEL=0",
                          "dh1024_ecdhe": "DHE-RSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:@SECLEVEL=0",
                          "idrac6": "DHE-RSA-AES256-SHA:AES256-SHA:@SECLEVEL=0",
                          "so_cifras_antigas": "CAMELLIA256-SHA:@SECLEVEL=0"}.get(perfil, "ALL:@SECLEVEL=0"))  # fmt: skip
    if perfil in ("dh1024", "dh1024_ecdhe", "idrac6"):
        dh = pasta.parent / "dh1024.pem"  # 1 por execução de teste (gerar DH demora)
        if not dh.exists():
            gerar("dhparam", "-out", str(dh), "1024")
        contexto.load_dh_params(str(dh))
        contexto.options |= ssl.OP_CIPHER_SERVER_PREFERENCE
    contexto.load_cert_chain(cert, chave)
    return contexto



def _ler(nome: str) -> Any:
    return json.loads((DELL / nome).read_text())


class BMCSimulado(ThreadingHTTPServer):
    """iDRAC9 falso por HTTPS. ``eventos``: tudo o que foi pedido (sem senhas). Opções de falha para os testes."""

    daemon_threads = True

    def __init__(self, pasta: Path, ipmi: SimuladorIPMI, ip: str = "127.0.0.1", porta: int = 0, repositorio: bool = True,
                 tarefa_concluida: bool = False, falhar_tarefas_depois_de: int | None = None,
                 internet: bool = False, porta_repositorio: int = 80, falhar_job: bool = False,
                 roteiros: list[list[dict[str, Any]]] | None = None, jobs_iniciais: list[dict[str, Any]] | None = None,
                 uefi_usb: str | None = None, nao_buscar: bool = False, dns: list[str] | None = None,
                 cancelar_falha: bool = False, idrac_novo: str | None = None,
                 reiniciar_apos_inventarios: tuple[int, int] | None = None, tls: str = "moderno") -> None:  # fmt: skip
        super().__init__((ip, porta), _ManipuladorBMC)
        self.socket = contexto_tls_servidor(pasta, tls).wrap_socket(self.socket, server_side=True)
        self.ipmi, self.pasta, self.repositorio, self.internet = ipmi, pasta, repositorio, internet
        self.tarefa_concluida, self.falhar_tarefas_depois_de = tarefa_concluida, falhar_tarefas_depois_de
        self.porta_repositorio, self.falhar_job = porta_repositorio, falhar_job
        self.sessoes: dict[str, str] = {}
        self.eventos: list[tuple] = []
        self.firmware: list[dict[str, Any]] = []
        self.jobs: dict[str, dict[str, Any]] = {}
        self.consultas_tarefa = 0
        self.lista_repositorio: list[dict[str, str]] = []
        self.boot_usb = False
        self._n = 0
        # 21.7: ``roteiros[i]`` = corpos da tarefa do i-ésimo envio de firmware, um por consulta (o último se repete);
        # ``{"_reiniciar": n}`` num corpo = o iDRAC reinicia ao responder (sessões somem, n pedidos seguintes sem resposta).
        self.roteiros = roteiros or []
        self.leituras: dict[str, int] = {}
        self.indisponivel = 0
        self.uefi_usb = uefi_usb
        # M3 (09/10/2026): o iDRAC aparece no inventário; com ``idrac_novo``, a versão muda só quando ele reinicia.
        self.versao_idrac, self.idrac_novo = "6.10.30.00", idrac_novo
        # 09/10/2026 (R640 real): ``(n, m)`` = depois do job concluído, o iDRAC ainda responde n leituras do inventário com
        # a versão antiga e a sessão válida (contadas depois do 1º firmware recebido); só então reinicia (sessões somem, m pedidos sem resposta).
        self.reiniciar_apos_inventarios, self.leituras_inventario = reiniciar_apos_inventarios, 0
        self.nao_buscar = nao_buscar  # 21.1: o iDRAC aceita o pedido do repositório mas nunca busca nada no pendrive
        self.dns = dns  # 22.2: servidores DNS do iDRAC (None = EthernetInterfaces não simulado)
        self.cancelar_falha = cancelar_falha  # 22.3: o iDRAC recusa cancelar o job
        self.boot_uefi: str | None = None
        for job in jobs_iniciais or []:
            self.novo_job(job["Name"], job.get("JobType", "FirmwareUpdate"), job["JobState"], job.get("Message", ""),
                          job.get("StartTime", ""), job.get("Id", ""))  # fmt: skip
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)

    @property
    def base(self) -> str:
        return f"https://{self.server_address[0]}:{self.server_address[1]}"

    def __enter__(self) -> BMCSimulado:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.shutdown()
        self.server_close()

    def novo_job(self, nome: str, tipo: str, estado: str, mensagem: str = "", inicio: str = "", ident: str = "") -> str:
        self._n += 1
        job = ident or f"JID_{self._n:012d}"
        self.jobs[job] = {"Id": job, "Name": nome, "JobType": tipo, "JobState": estado, "PercentComplete": 100 if estado == "Completed" else 0,
                          "Message": mensagem or estado, "StartTime": inicio}  # fmt: skip
        return job

    def reiniciar_idrac(self, pedidos_sem_resposta: int) -> None:
        self.sessoes.clear()
        self.indisponivel = pedidos_sem_resposta
        if self.idrac_novo:
            self.versao_idrac = self.idrac_novo
        self.eventos.append(("idrac_reiniciou", pedidos_sem_resposta))

    def tarefa_roteirizada(self, ident: str) -> dict[str, Any] | None:
        numero = int(ident.rsplit("_", 1)[-1]) if ident.rsplit("_", 1)[-1].isdigit() else 0
        if not 0 < numero <= len(self.roteiros) or not self.roteiros[numero - 1]:
            return None
        roteiro = self.roteiros[numero - 1]
        leitura = self.leituras.get(ident, 0)
        self.leituras[ident] = leitura + 1
        corpo = dict(roteiro[min(leitura, len(roteiro) - 1)])
        if "_reiniciar" in corpo:
            self.reiniciar_idrac(int(corpo.pop("_reiniciar")))
        return corpo

    def get(self, caminho: str, usuario: str | None) -> tuple[int, Any]:
        if caminho == "/redfish/v1":
            return 200, {"@odata.id": "/redfish/v1", "RedfishVersion": "1.11.0", "Vendor": "Dell", "Product": "Integrated Dell Remote Access Controller",
                         "Systems": {"@odata.id": "/redfish/v1/Systems"}, "Managers": {"@odata.id": "/redfish/v1/Managers"},
                         "AccountService": {"@odata.id": "/redfish/v1/AccountService"},
                         "Links": {"Sessions": {"@odata.id": "/redfish/v1/SessionService/Sessions"}}}  # fmt: skip
        if usuario is None:
            return 401, {"error": {"message": "sem sessão"}}
        if caminho.startswith("/redfish/v1/SessionService/Sessions/"):
            return 200, {"@odata.id": caminho, "UserName": usuario, "Roles": ["Administrator"]}
        if caminho == "/redfish/v1/UpdateService":
            return 200, _ler("UpdateService.json")
        if caminho == "/redfish/v1/UpdateService/FirmwareInventory" and self.reiniciar_apos_inventarios and self.firmware:
            self.leituras_inventario += 1
            n, m = self.reiniciar_apos_inventarios
            if self.leituras_inventario == n + 1:
                self.reiniciar_apos_inventarios = None
                self.reiniciar_idrac(m)
                return 401, {"error": {"message": "sem sessão"}}
        if caminho == "/redfish/v1/UpdateService/FirmwareInventory":
            return 200, {"Members": [{"@odata.id": "/redfish/v1/UpdateService/FirmwareInventory/Installed-25237-22.5.0"},
                                     {"@odata.id": "/redfish/v1/UpdateService/FirmwareInventory/Installed-25227-iDRAC"}]}
        if caminho == "/redfish/v1/UpdateService/FirmwareInventory/Installed-25227-iDRAC":
            return 200, {"@odata.id": caminho, "Id": "Installed-25227-" + self.versao_idrac, "SoftwareId": "25227",
                         "Name": "Integrated Dell Remote Access Controller", "Version": self.versao_idrac, "Updateable": True,
                         "Status": {"Health": "OK", "State": "Enabled"}}
        if caminho == "/redfish/v1/UpdateService/FirmwareInventory/Installed-25237-22.5.0":
            return 200, _ler("FirmwareInventory_instalado.json")
        if caminho == "/redfish/v1/Systems":
            return 200, {"Members": [{"@odata.id": SISTEMA}]}
        if caminho == SISTEMA:
            return 200, {"@odata.id": SISTEMA, "Id": "System.Embedded.1", "Boot": {
                "BootSourceOverrideTarget": "Usb" if self.boot_usb else "None",
                "BootSourceOverrideTarget@Redfish.AllowableValues": ["None", "Pxe", "Cd", "Hdd", "BiosSetup"]
                + (["UefiTarget"] if self.uefi_usb else ["Usb"])}}
        if caminho == SISTEMA + "/BootOptions" and self.uefi_usb:
            return 200, {"Members": [{"@odata.id": SISTEMA + "/BootOptions/Boot0001"}, {"@odata.id": SISTEMA + "/BootOptions/Boot0002"}]}
        if caminho == SISTEMA + "/BootOptions/Boot0001" and self.uefi_usb:
            return 200, {"Id": "Boot0001", "DisplayName": "Disco: Debian", "BootOptionEnabled": True,
                         "UefiDevicePath": "PciRoot(0x0)/Pci(0x17,0x0)/Sata(0x0,0xFFFF,0x0)/HD(1,GPT,ABC)/File(\\EFI\\debian\\shimx64.efi)"}
        if caminho == SISTEMA + "/BootOptions/Boot0002" and self.uefi_usb:
            return 200, {"Id": "Boot0002", "DisplayName": "Pendrive USB", "BootOptionEnabled": True, "UefiDevicePath": self.uefi_usb}
        if caminho == "/redfish/v1/Managers":
            return 200, {"Members": [{"@odata.id": GERENTE}]}
        if caminho == GERENTE:
            return 200, {"Id": "iDRAC.Embedded.1", "Actions": {"Oem": {"#OemManager.v1_4_0.OemManager#OemManager.ExportSystemConfiguration": {
                "target": GERENTE + "/Actions/Oem/OemManager.ExportSystemConfiguration"}}}}
        if caminho == SERVICO and self.repositorio:
            return 200, {"@odata.id": SERVICO, "Id": "DellSoftwareInstallationService"}
        if caminho.startswith("/redfish/v1/TaskService/Tasks/"):
            self.consultas_tarefa += 1
            if self.falhar_tarefas_depois_de is not None and self.consultas_tarefa > self.falhar_tarefas_depois_de:
                return 503, {"error": {"message": "iDRAC ocupado (falha simulada no meio)"}}
            roteirizada = None if "EXP" in caminho else self.tarefa_roteirizada(caminho.rsplit("/", 1)[-1])
            if roteirizada is not None:
                return 200, {"@odata.type": "#Task.v1_4_3.Task", "Name": "Firmware Update", **roteirizada,
                             "@odata.id": caminho, "Id": caminho.rsplit("/", 1)[-1]}  # fmt: skip
            corpo = _ler("Task_concluida.json" if self.tarefa_concluida or "EXP" in caminho else "Task_agendada.json")
            return 200, {**corpo, "@odata.id": caminho, "Id": caminho.rsplit("/", 1)[-1]}
        if caminho == GERENTE + "/EthernetInterfaces" and self.dns is not None:
            return 200, {"Members": [{"@odata.id": GERENTE + "/EthernetInterfaces/NIC.1"}]}
        if caminho == GERENTE + "/EthernetInterfaces/NIC.1" and self.dns is not None:
            return 200, {"Id": "NIC.1", "NameServers": self.dns}
        if caminho == GERENTE + "/Jobs":
            return 200, {"Members": [{"@odata.id": f"{GERENTE}/Jobs/{j}"} for j in self.jobs]}
        if caminho.startswith(GERENTE + "/Jobs/"):
            job = self.jobs.get(caminho.rsplit("/", 1)[-1])
            return (200, job) if job else (404, {"error": {"message": "job não encontrado"}})
        return 404, {"error": {"message": f"não simulado: {caminho}"}}

    # Repositório: o "iDRAC" busca o catálogo e os pacotes no servidor HTTP do MSscan e confere o SHA-256.
    def ler_repositorio(self, corpo: dict[str, Any]) -> tuple[bool, str]:
        if corpo.get("ShareType") == "HTTPS":
            if not self.internet:
                return False, "Unable to connect to downloads.dell.com (sem internet na porta do iDRAC)"
            return False, "repositório online não simulado"
        base = f"http://{corpo['IPAddress']}:{self.porta_repositorio}{'/' + str(corpo.get('ShareName') or '').strip('/') if str(corpo.get('ShareName') or '').strip('/') else ''}"
        try:
            catalogo = urllib.request.urlopen(f"{base}/{corpo.get('CatalogFile') or 'Catalog.xml'}", timeout=10).read()
            raiz = ET.fromstring(catalogo)
            pacotes = []
            for comp in raiz.findall("SoftwareComponent"):
                dados = urllib.request.urlopen(f"{base}/{comp.get('path')}", timeout=10).read()
                if hashlib.sha256(dados).hexdigest() != (comp.get("hash") or "").lower():
                    return False, f"hash divergente em {comp.get('path')}"
                pacotes.append({"DisplayName": comp.findtext("Name/Display") or comp.get("path"), "PackageName": Path(comp.get("path")).name,
                                "PackageVersion": comp.get("vendorVersion") or "", "RebootType": "HOST" if comp.get("rebootRequired") == "true" else "NONE"})  # fmt: skip
        except Exception as erro:  # noqa: BLE001 - vira mensagem do job, como no iDRAC
            return False, f"Unable to transfer file: {erro}"
        self.lista_repositorio = pacotes
        return True, "Job completed successfully."

    def lista_xml(self) -> str:
        instancias = "".join("<INSTANCE>" + "".join(f'<PROPERTY NAME="{k}" TYPE="string"><VALUE>{v}</VALUE></PROPERTY>' for k, v in p.items())
                             + "</INSTANCE>" for p in self.lista_repositorio)  # fmt: skip
        return f"<CIM><MESSAGE><SIMPLEREQ><VALUE.NAMEDINSTANCE>{instancias}</VALUE.NAMEDINSTANCE></SIMPLEREQ></MESSAGE></CIM>"


class _ManipuladorBMC(BaseHTTPRequestHandler):
    server: BMCSimulado

    def log_message(self, *_: Any) -> None:
        pass

    def _responder(self, status: int, corpo: Any = None, cabecalhos: dict[str, str] | None = None) -> None:
        dados = json.dumps(corpo).encode() if corpo is not None else b""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(dados)))
        for chave, valor in (cabecalhos or {}).items():
            self.send_header(chave, valor)
        self.end_headers()
        self.wfile.write(dados)

    def _usuario(self) -> str | None:
        return self.server.sessoes.get(self.headers.get("X-Auth-Token", ""))

    def _corpo(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def _fora_do_ar(self) -> bool:
        """iDRAC reiniciando (21.7): os próximos pedidos ficam sem resposta útil (503)."""
        if self.server.indisponivel > 0:
            self.server.indisponivel -= 1
            self._corpo()
            self._responder(503, {"error": {"message": "iDRAC reiniciando (simulado)"}})
            return True
        return False

    def do_GET(self) -> None:  # noqa: N802
        if self._fora_do_ar():
            return
        caminho = self.path.split("?", 1)[0].rstrip("/") or "/"
        status, corpo = self.server.get(caminho, self._usuario())
        self._responder(status, corpo)

    def do_DELETE(self) -> None:  # noqa: N802
        token = self.headers.get("X-Auth-Token", "")
        self.server.eventos.append(("logout", self.server.sessoes.pop(token, None)))
        self._responder(200, {})

    def do_PATCH(self) -> None:  # noqa: N802
        usuario, corpo = self._usuario(), json.loads(self._corpo() or b"{}")
        if usuario is None:
            return self._responder(401, {})
        self.server.eventos.append(("patch", self.path, corpo))
        if self.path.rstrip("/") == SISTEMA and (corpo.get("Boot") or {}).get("BootSourceOverrideTarget") == "Usb":
            self.server.boot_usb = True
            return self._responder(200, {})
        if self.path.rstrip("/") == SISTEMA and (corpo.get("Boot") or {}).get("BootSourceOverrideTarget") == "UefiTarget":
            self.server.boot_uefi = (corpo.get("Boot") or {}).get("UefiTargetBootSourceOverride")
            return self._responder(200, {})
        return self._responder(400, {})

    def do_POST(self) -> None:  # noqa: N802
        if self._fora_do_ar():
            return
        caminho = self.path.rstrip("/")
        bruto = self._corpo()
        if caminho == "/redfish/v1/SessionService/Sessions":
            dados = json.loads(bruto or b"{}")
            usuario, senha = str(dados.get("UserName") or ""), str(dados.get("Password") or "")
            if not self.server.ipmi.login_admin(usuario, senha):
                self.server.eventos.append(("login_recusado", usuario))
                return self._responder(401, {"error": {"message": "credenciais inválidas"}})
            token = hashlib.sha256(os.urandom(16)).hexdigest()
            self.server.sessoes[token] = usuario
            self.server.eventos.append(("login", usuario))
            return self._responder(201, {}, {"X-Auth-Token": token, "Location": f"/redfish/v1/SessionService/Sessions/{len(self.server.sessoes)}"})
        usuario = self._usuario()
        if usuario is None:
            return self._responder(401, {})
        if caminho == "/redfish/v1/UpdateService/MultipartUpload":
            mensagem = BytesParser(policy=POLITICA_HTTP).parsebytes(
                f"Content-Type: {self.headers.get('Content-Type')}\r\n\r\n".encode() + bruto)
            partes = {p.get_param("name", header="content-disposition"): p for p in mensagem.iter_parts()}
            arquivo = partes.get("UpdateFile")
            parametros = json.loads(partes["UpdateParameters"].get_payload(decode=True)) if "UpdateParameters" in partes else {}
            conteudo = arquivo.get_payload(decode=True) if arquivo is not None else b""
            self.server.firmware.append({"usuario": usuario, "nome": arquivo.get_filename() if arquivo else None,
                                         "sha256": hashlib.sha256(conteudo).hexdigest(), "parametros": parametros})  # fmt: skip
            self.server.eventos.append(("firmware", usuario, arquivo.get_filename() if arquivo else None))
            return self._responder(202, {}, {"Location": f"/redfish/v1/TaskService/Tasks/JID_{len(self.server.firmware):012d}"})
        dados = json.loads(bruto or b"{}")
        self.server.eventos.append(("post", caminho, dados))
        if caminho.endswith("ExportSystemConfiguration"):
            return self._responder(202, {}, {"Location": "/redfish/v1/TaskService/Tasks/JID_EXP000000001"})
        if caminho == CANCELAR and dados.get("JobID") == "JID_CLEARALL_FORCE":
            # 09/10/2026: apaga a fila toda e reinicia os serviços do iDRAC (sem resposta por alguns pedidos).
            self.server.jobs.clear()
            self.server.eventos.append(("fila_limpa_forcada",))
            self.server.reiniciar_idrac(2)
            return self._responder(200, {})
        if caminho == CANCELAR:
            if self.server.cancelar_falha or dados.get("JobID") not in self.server.jobs:
                return self._responder(400, {"error": {"message": "SUP011: job não pode ser apagado"}})
            self.server.jobs.pop(dados["JobID"])
            self.server.eventos.append(("job_cancelado", dados["JobID"]))
            return self._responder(200, {})
        if caminho == SERVICO + "/Actions/DellSoftwareInstallationService.InstallFromRepository" and self.server.repositorio:
            if self.server.nao_buscar and dados.get("ShareType") == "HTTP":
                job = self.server.novo_job("Repository Update", "RepositoryUpdate", "Running", "Downloading the catalog file.")
                return self._responder(202, {}, {"Location": f"{GERENTE}/Jobs/{job}"})
            ok, mensagem = self.server.ler_repositorio(dados)
            if not ok:
                job = self.server.novo_job("Repository Update", "RepositoryUpdate", "Failed", mensagem)
                return self._responder(202, {}, {"Location": f"{GERENTE}/Jobs/{job}"})
            job = self.server.novo_job("Repository Update", "RepositoryUpdate", "Completed", mensagem)
            if dados.get("ApplyUpdate") == "True":
                for pacote in self.server.lista_repositorio:
                    estado = "Failed" if self.server.falhar_job else "Scheduled" if pacote["RebootType"] != "NONE" else "Completed"
                    self.server.novo_job(f"Firmware Update: {pacote['DisplayName']}", "FirmwareUpdate", estado,
                                         {"Scheduled": "Task successfully scheduled.", "Failed": "Job failed."}.get(estado, "Job completed successfully."))
            return self._responder(202, {}, {"Location": f"{GERENTE}/Jobs/{job}"})
        if caminho == SERVICO + "/Actions/DellSoftwareInstallationService.GetRepoBasedUpdateList" and self.server.repositorio:
            if not self.server.lista_repositorio:
                return self._responder(400, {"error": {"@Message.ExtendedInfo": [{"Message": "Firmware versions on server match catalog, applicable updates are not present in the repository."}]}})
            return self._responder(200, {"PackageList": self.server.lista_xml()})
        return self._responder(404, {"error": {"message": f"ação não simulada: {caminho}"}})


# --- Executor que só alcança os simuladores ---------------------------------------------------------------------------


class ExecutorSimulador(ExecutorReal):
    """``ExecutorReal`` (HTTP e processos de verdade) preso aos simuladores. ``alvo_simulado``: passa pela trava só aqui."""

    origem = "simulador"
    alvo_simulado = True

    def __init__(self, ipmi: SimuladorIPMI, pasta: Path, racadm: Path | None = None, interfaces: list[dict] | None = None,
                 ping_ok: bool = True, sumir_interface_apos: int | None = None) -> None:  # fmt: skip
        super().__init__()
        self.ipmi, self.pasta, self.racadm = ipmi, Path(pasta).resolve(), racadm
        self.interfaces = interfaces or []
        self.ping_ok = ping_ok  # 22.2: o iDRAC responde ao ping a partir do servidor
        self.sumir_interface_apos = sumir_interface_apos  # 22.3: a interface some depois de N leituras de "ip addr"
        self.leituras_ip = 0
        self.reinicios = 0
        self.recusados: list[list[str]] = []

    def _traduzir(self, comando: list[str]) -> list[str] | None:
        c = [str(a) for a in comando]
        if os.path.basename(c[0]) == "ipmitool":
            return [str(self.ipmi.wrapper)] + c[1:]
        if self.racadm is not None and c[0] == str(self.racadm):
            return c
        if c[0] == "bash" and len(c) > 1 and Path(c[1]).resolve().is_relative_to(self.pasta):
            return c
        return None

    def _rodar(self, comando, timeout):
        c = [str(a) for a in comando]
        if c == ["systemctl", "reboot"]:
            self.reinicios += 1
            return 0, "", "", 0.0
        if c[:5] == ["ip", "-j", "-4", "addr", "show"]:
            self.leituras_ip += 1
            if self.sumir_interface_apos is not None and self.leituras_ip > self.sumir_interface_apos:
                return 0, "[]", "", 0.0
            return 0, json.dumps(self.interfaces), "", 0.0
        if c[0] == "ping":
            return (0 if self.ping_ok else 1), "", "", 0.0
        real = self._traduzir(c)
        if real is None:
            self.recusados.append(c)
            return -1, "", f"simulador: comando não permitido nos testes: {c[0]}", 0.0
        return super()._rodar(real, timeout)

    def _rodar_gravacao(self, comando, timeout):
        real = self._traduzir([str(a) for a in comando])
        if real is None:
            self.recusados.append(list(comando))
            return -1, "", "simulador: comando não permitido nos testes", 0.0
        return super()._rodar_gravacao(real, timeout or 120)

    def _baixar(self, url, destino, limite_bytes, timeout):
        raise AssertionError("os testes de ponta a ponta não baixam nada da internet")


def disponivel() -> str:
    """``""`` se dá para rodar os simuladores aqui; senão o motivo (o teste é pulado)."""
    for programa in ("ipmi_sim", "ipmitool", "openssl"):
        if shutil.which(programa) is None:
            return f"{programa} não instalado"
    return ""
