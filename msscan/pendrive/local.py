"""Como o pendrive foi gravado e onde o MSscan guarda os dados (relatórios, coletas, vendor...).

A ISO precisa funcionar gravada de qualquer forma (Rufus modo ISO ou DD, ``dd``,
balenaEtcher, Ventoy). O local dos dados é escolhido automaticamente, nesta ordem:

1. partição com rótulo ``MSSCAN`` (se existir);
2. pasta ``MSSCAN/`` no próprio pendrive de boot, quando ele é gravável
   (FAT32/exFAT: Rufus modo ISO);
3. pasta ``MSSCAN/`` na partição de dados do Ventoy (exFAT);
4. ``/tmp/msscan``, com aviso claro de que nada fica no pendrive.

O MSscan NUNCA formata nem cria partições. O pendrive de boot fica somente
leitura e só é remontado com escrita durante uma gravação (``escrita``), com
``sync`` no fim; assim, arrancar o pendrive entre gravações não deixa o FAT sujo.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import Any

from msscan.core.executor import Executor
from msscan.parsers import ErroParser
from msscan.parsers.efivar import parse_od_booleano_efi
from msscan.parsers.findmnt import parse_findmnt_json
from msscan.parsers.lsblk import parse_lsblk_arvore
from msscan.pendrive.particao import localizar_ou_montar

MIDIA_BOOT = "/run/live/medium"
"""Onde o live-boot monta a mídia de boot (ISO9660 no modo DD; FAT32/exFAT no modo ISO do Rufus)."""
PASTA_NO_PENDRIVE = "MSSCAN"
PASTA_ANTIGA = "SERVERDIAG"
"""Nome antigo do projeto: pasta já preparada (chave da IA, vendor/) continua valendo, com aviso para renomear."""
PONTO_VENTOY = "/mnt/msscan-ventoy"
DM_VENTOY = "msscan-ventoy"
PASTA_TEMPORARIA = "/tmp/msscan"
EFIVAR_SECURE_BOOT = "/sys/firmware/efi/efivars/SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c"
ROTULO_EFI_VENTOY = "VTOYEFI"
GRAVAVEIS = ("vfat", "exfat")
"""Sistemas de arquivos em que o MSscan grava. NTFS nunca (o initramfs nem monta)."""
OPCOES_VENTOY = "rw,noatime,fmask=0022,dmask=0022"
ARQUIVO_TESTE = ".msscan-teste-gravacao"

COMANDO_FINDMNT_MIDIA = ["findmnt", "-J", "-o", "TARGET,SOURCE,FSTYPE,OPTIONS,LABEL", MIDIA_BOOT]
COMANDO_LSBLK = ["lsblk", "-J", "-p", "-o", "NAME,PKNAME,TYPE,TRAN,RM,RO,FSTYPE,LABEL,PTTYPE,PARTTYPE,MOUNTPOINT"]
COMANDO_SECURE_BOOT = ["od", "-An", "-t", "u1", EFIVAR_SECURE_BOOT]

# Tipos de local (``LocalDados.tipo``)
PARTICAO = "particao_msscan"
PENDRIVE_BOOT = "pendrive_boot"
VENTOY = "ventoy"
TEMPORARIO = "temporario"
MANUAL = "manual"

DESCRICAO_METODO = {
    "dd": "imagem gravada bit a bit (Rufus modo DD, dd, balenaEtcher)",
    "rufus_iso": "arquivos copiados para o pendrive (Rufus modo ISO)",
    "ventoy": "arquivo .iso no pendrive do Ventoy",
    "midia_optica": "CD/DVD ou mídia virtual do BMC (iDRAC/iLO)",
    "desconhecido": "forma de gravação não identificada",
}
TABELAS = {"dos": "MBR", "gpt": "GPT"}


@dataclass
class FormaGravacao:
    """Como o pendrive foi gravado e como a máquina deu boot (mostrado na tela e no relatório)."""

    metodo: str = "desconhecido"
    tabela: str | None = None
    """``MBR`` ou ``GPT`` do disco da mídia de boot (quando é um disco)."""
    sistema_arquivos: str | None = None
    dispositivo: str | None = None
    disco: str | None = None
    boot: str = "?"
    """``UEFI`` ou ``BIOS legado``."""
    secure_boot: bool | None = None
    observacoes: list[str] = field(default_factory=list)

    @property
    def descricao(self) -> str:
        partes = [DESCRICAO_METODO.get(self.metodo, self.metodo)]
        detalhes = [d for d in (self.sistema_arquivos and self.sistema_arquivos.upper(), self.tabela) if d]
        if detalhes:
            partes.append(f"({', '.join(detalhes)})")
        return " ".join(partes)

    @property
    def descricao_boot(self) -> str:
        if self.boot != "UEFI":
            return self.boot
        if self.secure_boot is None:
            return "UEFI (Secure Boot: não verificado)"
        return f"UEFI, Secure Boot {'ligado' if self.secure_boot else 'desligado'}"

    def para_dict(self) -> dict[str, Any]:
        return {**asdict(self), "descricao": self.descricao, "descricao_boot": self.descricao_boot}


@dataclass
class LocalDados:
    """Onde ficam ``config/``, ``vendor/``, ``relatorios/``, ``coletas/``, ``estado/`` e ``firmware/``."""

    pasta: str | None
    tipo: str
    persistente: bool
    """``True`` = fica no pendrive; ``False`` = perde-se ao desligar."""
    mensagem: str
    aviso: str = ""
    remontar: str | None = None
    """Ponto de montagem do pendrive de boot: fica somente leitura e só ganha escrita em ``escrita``."""
    gravacao: FormaGravacao = field(default_factory=FormaGravacao)
    tentativas: list[str] = field(default_factory=list)

    def para_dict(self) -> dict[str, Any]:
        return {
            "pasta": self.pasta,
            "tipo": self.tipo,
            "persistente": self.persistente,
            "mensagem": self.mensagem,
            "aviso": self.aviso,
            "tentativas": list(self.tentativas),
            "gravacao": self.gravacao.para_dict(),
        }


# --- Forma de gravação -------------------------------------------------------------------------


def _json_do_comando(executor: Executor, comando: list[str], parser: Any) -> list[dict[str, Any]]:
    rc, saida, _ = executor.rodar(comando, timeout=15)
    if rc != 0:
        return []
    try:
        return parser(saida)
    except ErroParser:
        return []


def _eh_ventoy(midia: dict[str, Any], dispositivos: list[dict[str, Any]]) -> bool:
    # TODO(fixture): findmnt/lsblk reais de um boot pelo Ventoy. Documentação do Ventoy: a ISO é
    # exposta como /dev/mapper/ventoy e o pendrive tem a partição de boot "VTOYEFI".
    origem = str(midia.get("source") or "")
    if origem.startswith("/dev/mapper/ventoy"):
        return True
    return any(d.get("label") == ROTULO_EFI_VENTOY for d in dispositivos)


def detectar_gravacao(executor: Executor) -> FormaGravacao:
    """Identifica a forma de gravação pela mídia de boot (``/run/live/medium``) e o modo de boot."""
    forma = FormaGravacao()
    if executor.existe("/sys/firmware/efi"):
        forma.boot = "UEFI"
        rc, saida, _ = executor.rodar(COMANDO_SECURE_BOOT, timeout=5)
        if rc == 0:
            try:
                forma.secure_boot = parse_od_booleano_efi(saida)
            except ErroParser as erro:
                forma.observacoes.append(f"Secure Boot não verificado: {erro}")
        else:
            # Firmware UEFI sem a variável SecureBoot: não suporta ou não tem o recurso ativo.
            forma.secure_boot = False
            forma.observacoes.append("variável SecureBoot ausente no firmware (Secure Boot desligado ou não suportado)")
    else:
        forma.boot = "BIOS legado"

    midias = _json_do_comando(executor, COMANDO_FINDMNT_MIDIA, parse_findmnt_json)
    if not midias:
        forma.observacoes.append(f"mídia de boot do live não encontrada em {MIDIA_BOOT}")
        return forma
    midia = midias[0]
    dispositivos = _json_do_comando(executor, COMANDO_LSBLK, parse_lsblk_arvore)
    por_nome = {d.get("name"): d for d in dispositivos}
    forma.sistema_arquivos = midia.get("fstype")
    forma.dispositivo = midia.get("source")
    dispositivo = por_nome.get(forma.dispositivo) or {}
    disco = por_nome.get(dispositivo.get("pkname")) if dispositivo.get("type") == "part" else dispositivo
    if disco:
        forma.disco = disco.get("name")
        forma.tabela = TABELAS.get(str(disco.get("pttype")))

    if forma.sistema_arquivos == "iso9660":
        if _eh_ventoy(midia, dispositivos):
            forma.metodo = "ventoy"
            forma.tabela = None  # a tabela do pendrive do Ventoy não é a da ISO
        elif dispositivo.get("type") == "rom":
            forma.metodo = "midia_optica"
            forma.tabela = None  # o MBR é o da imagem híbrida, não de um pendrive
        else:
            forma.metodo = "dd"
    elif forma.sistema_arquivos in ("vfat", "exfat", "ntfs", "ntfs3"):
        forma.metodo = "rufus_iso"
    return forma


# --- Local dos dados ---------------------------------------------------------------------------


def _testar_gravacao(executor: Executor, pasta: str) -> str | None:
    """Cria a pasta (se faltar) e grava/apaga um arquivo de teste. ``None`` = gravável; senão, o motivo."""
    rc, _, erro = executor.rodar(["mkdir", "-p", pasta], timeout=10)
    if rc != 0:
        return (erro.strip().splitlines() or [f"código {rc}"])[-1]
    teste = f"{pasta}/{ARQUIVO_TESTE}"
    rc, _, erro = executor.rodar(["touch", teste], timeout=10)
    if rc != 0:
        return (erro.strip().splitlines() or [f"código {rc}"])[-1]
    executor.rodar(["rm", "-f", teste], timeout=10)
    executor.rodar(["sync"], timeout=60)
    return None


def _escolher_pasta(executor: Executor, base: str) -> tuple[str, str]:
    """``(nome da pasta, aviso)``: ``MSSCAN/``; se só existir a antiga ``SERVERDIAG/``, ela (com aviso)."""
    if not executor.existe(f"{base}/{PASTA_NO_PENDRIVE}") and executor.existe(f"{base}/{PASTA_ANTIGA}"):
        return PASTA_ANTIGA, (
            f"A pasta de dados ainda tem o nome antigo {PASTA_ANTIGA}/: funciona, mas renomeie para "
            f"{PASTA_NO_PENDRIVE}/ (no Windows: botão direito na pasta > Renomear)."
        )
    return PASTA_NO_PENDRIVE, ""


def _remontar(executor: Executor, ponto: str, modo: str) -> tuple[bool, str]:
    rc, _, erro = executor.rodar(["mount", "-o", f"remount,{modo}", ponto], timeout=30)
    return rc == 0, (erro.strip().splitlines() or [f"código {rc}"])[-1] if rc != 0 else ""


def _no_pendrive_de_boot(executor: Executor, forma: FormaGravacao, tentativas: list[str]) -> LocalDados | None:
    if forma.sistema_arquivos not in GRAVAVEIS:
        if forma.sistema_arquivos:
            tentativas.append(f"pendrive de boot: {forma.sistema_arquivos} não é gravável pelo MSscan")
        return None
    ok, erro = _remontar(executor, MIDIA_BOOT, "rw")
    if not ok:
        tentativas.append(f"pendrive de boot: não foi possível habilitar a escrita ({erro}); protegido contra gravação?")
        return None
    nome, aviso = _escolher_pasta(executor, MIDIA_BOOT)
    pasta = f"{MIDIA_BOOT}/{nome}"
    motivo = _testar_gravacao(executor, pasta)
    _remontar(executor, MIDIA_BOOT, "ro")
    if motivo:
        tentativas.append(f"pendrive de boot: gravação de teste falhou ({motivo})")
        return None
    return LocalDados(
        pasta, PENDRIVE_BOOT, True, f"pasta {nome}/ no próprio pendrive de boot ({pasta})",
        aviso=aviso, remontar=MIDIA_BOOT, gravacao=forma,
    )  # fmt: skip


def _particao_dados_ventoy(dispositivos: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Partição de dados do Ventoy: a irmã FAT/exFAT da partição ``VTOYEFI`` no mesmo disco."""
    for efi in (d for d in dispositivos if d.get("label") == ROTULO_EFI_VENTOY):
        irmas = [d for d in dispositivos if d.get("pkname") == efi.get("pkname") and d is not efi]
        for parte in irmas:
            if parte.get("type") == "part" and parte.get("fstype") in GRAVAVEIS:
                return parte
    return None


def _no_ventoy(executor: Executor, forma: FormaGravacao, tentativas: list[str]) -> LocalDados | None:
    # TODO(fixture): lsblk/mount reais num pendrive do Ventoy (partição exFAT com a ISO em uso pelo dm "ventoy").
    dispositivos = _json_do_comando(executor, COMANDO_LSBLK, parse_lsblk_arvore)
    parte = _particao_dados_ventoy(dispositivos)
    if parte is None:
        if forma.metodo == "ventoy":
            tentativas.append("Ventoy: partição de dados (FAT32/exFAT) não encontrada")
        return None
    dispositivo = str(parte["name"])
    ponto = parte.get("mountpoint")
    if not ponto:
        ponto = PONTO_VENTOY
        executor.rodar(["mkdir", "-p", ponto], timeout=10)
        rc, _, erro = executor.rodar(["mount", "-o", OPCOES_VENTOY, dispositivo, ponto], timeout=30)
        if rc != 0:
            # Com a ISO em uso, o Ventoy segura a partição (device-mapper) e o mount direto dá "busy".
            # Um segundo mapeamento linear da partição inteira pode ser montado (técnica da documentação
            # do Ventoy); nada é gravado no mapeamento em si e a partição não é alterada.
            rc, setores, erro = executor.rodar(["blockdev", "--getsz", dispositivo], timeout=10)
            mapeado = f"/dev/mapper/{DM_VENTOY}"
            if rc == 0 and setores.strip().isdigit():
                tabela = f"0 {setores.strip()} linear {dispositivo} 0"
                rc, _, erro = executor.rodar(["dmsetup", "create", DM_VENTOY, "--table", tabela], timeout=30)
                if rc == 0:
                    rc, _, erro = executor.rodar(["mount", "-o", OPCOES_VENTOY, mapeado, ponto], timeout=30)
                    if rc != 0:
                        executor.rodar(["dmsetup", "remove", DM_VENTOY], timeout=30)
            if rc != 0:
                detalhe = (erro.strip().splitlines() or [f"código {rc}"])[-1]
                tentativas.append(f"Ventoy: não foi possível montar {dispositivo}: {detalhe}")
                return None
    nome, aviso = _escolher_pasta(executor, ponto)
    pasta = f"{ponto}/{nome}"
    motivo = _testar_gravacao(executor, pasta)
    if motivo:
        tentativas.append(f"Ventoy: gravação de teste falhou em {pasta} ({motivo})")
        return None
    return LocalDados(pasta, VENTOY, True, f"pasta {nome}/ na partição de dados do Ventoy ({pasta})", aviso=aviso,
                      gravacao=forma)  # fmt: skip


def localizar_dados(executor: Executor) -> LocalDados:
    """Escolhe o local dos dados na ordem do projeto; nunca levanta exceção nem formata nada."""
    forma = detectar_gravacao(executor)
    tentativas: list[str] = []

    particao = localizar_ou_montar(executor)
    if particao.pasta:
        return LocalDados(particao.pasta, PARTICAO, True, particao.mensagem, aviso=particao.aviso, gravacao=forma)
    tentativas.append(particao.mensagem)

    for tentativa in (_no_pendrive_de_boot, _no_ventoy):
        local = tentativa(executor, forma, tentativas)
        if local is not None:
            local.tentativas = tentativas
            return local

    executor.rodar(["mkdir", "-p", PASTA_TEMPORARIA], timeout=10)
    aviso = (
        "ATENÇÃO: nenhum local gravável no pendrive. Relatórios e coletas ficam em "
        f"{PASTA_TEMPORARIA} e serão PERDIDOS ao desligar. Para salvar no pendrive: grave a ISO "
        "com o Rufus em modo ISO ou crie a partição MSSCAN (iso/README.md)."
    )
    return LocalDados(
        PASTA_TEMPORARIA, TEMPORARIO, False, f"{PASTA_TEMPORARIA} (temporário: nada será salvo no pendrive)",
        aviso=aviso, gravacao=forma, tentativas=tentativas,
    )  # fmt: skip


def local_manual(pasta: str) -> LocalDados:
    """Pasta indicada em ``--dados`` (desenvolvimento/testes): usada como está."""
    return LocalDados(pasta, MANUAL, True, f"pasta indicada em --dados ({pasta})")


@contextmanager
def escrita(executor: Executor, local: LocalDados | None) -> Iterator[None]:
    """Janela de gravação: habilita a escrita no pendrive de boot, e no fim ``sync`` (+ volta a somente leitura)."""
    if local is not None and local.remontar:
        _remontar(executor, local.remontar, "rw")
    try:
        yield
    finally:
        if local is not None and local.tipo != MANUAL:
            executor.rodar(["sync"], timeout=120)
        if local is not None and local.remontar:
            _remontar(executor, local.remontar, "ro")
