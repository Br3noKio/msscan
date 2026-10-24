"""Opção A do acesso ao iDRAC (CLAUDE.md 19.1): o racadm local preparado a partir do pacote oficial "iDRAC Tools for Linux".

O técnico coloca o ``*iDRACTools*.tar.gz`` da Dell em ``MSSCAN/vendor/``. O MSscan extrai na RAM do sistema live (nunca
nos discos do cliente) e repete os passos do pós-instalação oficial dos pacotes da Dell, conferidos nos scripts dos RPMs
``srvadmin-hapi`` e ``srvadmin-idracadm7`` (iDRAC Tools 9.4.0, ``dl.dell.com``) e na estrutura do pacote 11.x (pasta
``UBUNTU22`` com ``srvadmin-hapi``, ``srvadmin-idracadm7`` e ``srvadmin-idracadm8`` em ``.deb``):

1. extrair o ``.tar.gz`` (``Executor.extrair_idrac_tools``: origem e destino fixos; o ``tar`` genérico continua recusado,
   18.5) e os ``.deb`` do racadm da pasta ``UBUNTU*`` mais nova (``dpkg-deb -x``) na raiz do sistema live. Pacotes antigos
   só com RPM (ex.: 9.4.0) não são usados: a mensagem pede a versão 11 ou mais nova;
2. ``cat /opt/dell/srvadmin/etc/omreg.d/*.cfg > /opt/dell/srvadmin/etc/omreg.cfg`` e ``ldconfig``;
3. ``chmod +x`` no ``racadm-wrapper-idrac*`` mais novo e ``racadm`` → ele, em ``/opt/dell/srvadmin/sbin``;
4. ``instsvcdrv-helper start`` (configura o acesso pelo IPMI do kernel);
5. conferir com ``racadm getversion`` (só leitura).

Sem pacote: nada muda (o racadm continua procurado em ``vendor/racadm`` e no PATH). Nunca levanta exceção.
MOCKUP(rodada final): não testado num iDRAC real com o pacote 11.x (sem ``.deb`` nem racadm nesta máquina).
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any

from msscan.core.executor import PASTA_IDRAC_TOOLS as PASTA_EXTRACAO
from msscan.parsers import ErroParser
from msscan.parsers.racadm import parse_racadm_get

SRVADMIN = "/opt/dell/srvadmin"
RACADM = f"{SRVADMIN}/sbin/racadm"
HELPERS = ("/usr/libexec/instsvcdrv-helper", f"{SRVADMIN}/sbin/instsvcdrv-helper")
_RE_PACOTE = re.compile(r".*idractools.*\.tar\.gz", re.IGNORECASE)
_RE_PASTA_DEB = re.compile(r"UBUNTU(\d+)", re.IGNORECASE)
PREPARADO, AUSENTE, FALHOU = "pronto", "ausente", "falhou"


def procurar_pacote(executor: Any, pasta_dados: str | None) -> str | None:
    """O ``*iDRACTools*.tar.gz`` de ``vendor/`` (o de nome mais novo, se houver mais de um)."""
    if not pasta_dados:
        return None
    nomes = sorted(n for n in executor.listar(f"{pasta_dados}/vendor") if _RE_PACOTE.fullmatch(n))
    return f"{pasta_dados}/vendor/{nomes[-1]}" if nomes else None


def escolher_pacotes(executor: Any, raiz: str) -> list[str]:
    """``.deb`` do racadm da pasta ``UBUNTU*`` mais nova do tarball extraído (``[]`` se não houver)."""
    base = f"{raiz}/iDRACTools/racadm"
    pastas = [p for p in executor.listar(base) if _RE_PASTA_DEB.fullmatch(p)]
    for pasta in sorted(pastas, key=lambda p: int(_RE_PASTA_DEB.fullmatch(p).group(1)), reverse=True):
        dentro = f"{base}/{pasta}/x86_64" if executor.listar(f"{base}/{pasta}/x86_64") else f"{base}/{pasta}"
        if achados := sorted(f"{dentro}/{n}" for n in executor.listar(dentro) if n.endswith(".deb")):
            return sorted(achados, key=lambda c: ("hapi" not in c, c))  # hapi primeiro, como a dependência oficial
    return []


def ler_versoes(executor: Any, racadm: str) -> dict[str, str] | None:
    """``racadm getversion`` (só leitura): ``{"iDRAC Version": "7.00.00.173", ...}``; ``None`` se não respondeu."""
    rc, saida, _ = executor.rodar([racadm, "getversion"], timeout=60)
    if rc != 0:
        return None
    try:
        return parse_racadm_get(saida)
    except ErroParser:
        return None


def preparar(executor: Any, pasta_dados: str | None) -> dict[str, Any]:
    """Prepara o racadm a partir do pacote em ``vendor/``. ``situacao``: pronto, ausente (sem pacote) ou falhou."""
    info: dict[str, Any] = {"situacao": AUSENTE, "pacote": None, "caminho": None, "versoes": None, "passos": [], "mensagem": ""}
    if not getattr(executor, "prepara_ferramentas", False):
        info["mensagem"] = "modo sem alterações no sistema (replay ou coleta): o racadm não é preparado"
        return info
    pacote = procurar_pacote(executor, pasta_dados)
    if pacote is None:
        info["mensagem"] = "sem o pacote iDRAC Tools for Linux (*iDRACTools*.tar.gz) em vendor/"
        return info
    info["pacote"] = pacote.rsplit("/", 1)[-1]

    def rodar(comando: list[str], timeout: float = 120) -> bool:
        rc, _, erro = executor.rodar(comando, timeout=timeout)
        info["passos"].append({"comando": " ".join(comando), "rc": rc, "erro": (erro or "").strip()[-200:]})
        return rc == 0

    def falhou(motivo: str) -> dict[str, Any]:
        info["situacao"], info["mensagem"] = FALHOU, f"{info['pacote']}: {motivo}"
        executor.rodar(["rm", "-rf", PASTA_EXTRACAO], timeout=60)
        return info

    rc, _, erro = executor.extrair_idrac_tools(pacote)
    info["passos"].append({"comando": f"extrair {info['pacote']}", "rc": rc, "erro": (erro or "").strip()[-200:]})
    if rc != 0:
        return falhou(f"não foi possível extrair o pacote ({erro}); arquivo incompleto? baixe de novo do site da Dell")
    pacotes = escolher_pacotes(executor, PASTA_EXTRACAO)
    if not pacotes:
        return falhou("o pacote não traz o racadm em .deb (pasta iDRACTools/racadm/UBUNTU*): use o iDRAC Tools for Linux "
                      "versão 11 ou mais nova")  # fmt: skip
    for arquivo in pacotes:
        if not rodar(["dpkg-deb", "-x", arquivo, "/"], timeout=300):
            return falhou(f"não foi possível extrair {arquivo.rsplit('/', 1)[-1]}")
    # Pós-instalação oficial (scripts dos pacotes srvadmin-hapi e srvadmin-idracadm7).
    rodar(["sh", "-c", f"cat {SRVADMIN}/etc/omreg.d/*.cfg > {SRVADMIN}/etc/omreg.cfg"])
    rodar(["ldconfig"])
    wrappers = sorted(n for n in executor.listar(f"{SRVADMIN}/sbin") if n.startswith("racadm-wrapper-idrac"))
    # Wrapper do pós-instalação oficial (9.4.0); se o pacote trouxer outro arranjo, o binário idracadm7 direto (o
    # wrapper só faz "exec idracadm7"). MOCKUP(rodada final): arranjo do pacote 11.x não conferido.
    alvo = wrappers[-1] if wrappers else f"{SRVADMIN}/bin/idracadm7" if executor.existe(f"{SRVADMIN}/bin/idracadm7") else None
    if alvo:
        # O .deb traz o wrapper SEM permissão de execução; o pós-instalação oficial faz "chmod +x" (achado no QEMU com o
        # pacote 11.4.1.0 real: "Permission denied" sem isto).
        rodar(["chmod", "+x", alvo if alvo.startswith("/") else f"{SRVADMIN}/sbin/{alvo}"])
        rodar(["ln", "-sf", alvo, RACADM])
    elif not executor.existe(RACADM):
        return falhou("o racadm não apareceu depois da extração (sem racadm-wrapper-idrac* nem bin/idracadm7)")
    helper = next((h for h in HELPERS if executor.existe(h)), None)
    if helper:
        rodar([helper, "start"], timeout=180)
    executor.rodar(["rm", "-rf", PASTA_EXTRACAO], timeout=60)  # libera a RAM: o racadm já está em /opt/dell
    versoes = ler_versoes(executor, RACADM)
    if versoes is None:
        info["situacao"] = FALHOU
        info["mensagem"] = (f"{info['pacote']}: racadm preparado, mas não respondeu ao 'racadm getversion' (o IPMI local "
                            "do iDRAC está ativo? confira em F2 > iDRAC Settings)")  # fmt: skip
        return info
    versao = re.search(r"LX-(\d+(?:\.\d+)+)", info["pacote"])
    info.update(situacao=PREPARADO, caminho=RACADM, versoes=versoes, versao=versao.group(1) if versao else None,
                origem=f"pacote em MSSCAN/vendor/ ({info['pacote']})",
                mensagem=f"racadm preparado a partir de {info['pacote']} (iDRAC {versoes.get('iDRAC Version') or '?'})")  # fmt: skip
    return info




# --- Download automático do site da Dell (CLAUDE.md 20) ---------------------------------------------------------------

CONFIG_EMBUTIDA = Path(__file__).resolve().parents[1] / "ferramentas_dell.yaml"
CONFIG_PENDRIVE = "config/ferramentas_dell.yaml"
PASTA_DOWNLOAD = "/run/msscan/download"
BAIXANDO, SEM_INTERNET = "baixando", "sem_internet"
AVISO_TERMOS = "iDRAC Tools baixado do site da Dell; uso sujeito aos termos da Dell"
ESPERA_DOWNLOAD_S = 900
"""Quanto uma atualização que precisa do racadm espera um download em andamento (CLAUDE.md 20, item 4)."""
_RE_SHA256 = re.compile(r"[0-9a-f]{64}")


def validar_config(dados: Any) -> tuple[dict[str, Any] | None, str]:
    """``(bloco idrac_tools, "")`` se válido; senão ``(None, motivo)``. Nunca aceita fora da Dell ou abaixo da versão 11."""
    from msscan.core.executor import validar_download

    bloco = dados.get("idrac_tools") if isinstance(dados, dict) else None
    if not isinstance(bloco, dict):
        return None, "sem o bloco idrac_tools"
    url, sha, arquivo, versao = (str(bloco.get(k) or "") for k in ("url", "sha256", "arquivo", "versao"))
    if validar_download(url):
        return None, f"endereço não oficial: {validar_download(url)}"
    if not _RE_SHA256.fullmatch(sha.lower()):
        return None, "SHA-256 ausente ou inválido"
    if not _RE_PACOTE.fullmatch(arquivo) or url.rsplit("/", 1)[-1] != arquivo:
        return None, "nome do arquivo não é um *iDRACTools*.tar.gz (ou não bate com o endereço)"
    if not re.fullmatch(r"\d+(\.\d+)*", versao) or int(versao.split(".")[0]) < 11:
        return None, "versão abaixo da 11 (sem os pacotes .deb do racadm)"
    return {**bloco, "sha256": sha.lower()}, ""


def carregar_config(pasta_dados: str | None) -> tuple[dict[str, Any] | None, str]:
    """Cópia do pendrive (``MSSCAN/config/ferramentas_dell.yaml``) se válida; senão a embutida na ISO."""
    import yaml

    motivos = []
    for caminho in ([Path(pasta_dados) / CONFIG_PENDRIVE] if pasta_dados else []) + [CONFIG_EMBUTIDA]:
        if not caminho.is_file():
            continue
        try:
            bloco, motivo = validar_config(yaml.safe_load(caminho.read_text(encoding="utf-8")))
        except (OSError, yaml.YAMLError) as erro:
            bloco, motivo = None, str(erro)
        if bloco is not None:
            return {**bloco, "_origem_config": str(caminho)}, ""
        motivos.append(f"{caminho.name}: {motivo}")
    return None, "; ".join(motivos) or "ferramentas_dell.yaml não encontrado"


def baixar(executor: Any, pasta_dados: str, escrita: Any, monitor: Any = None) -> dict[str, Any]:
    """Baixa o pacote (na RAM, com retomada), confere o SHA-256 oficial e só então grava em ``vendor/`` (janela curta).

    ``escrita``: context manager da janela de escrita do pendrive. ``situacao``: ``pronto`` (pacote em ``vendor/``),
    ``sem_internet`` ou ``falhou`` (com o motivo; hash divergente apaga o arquivo). Nunca levanta exceção."""
    from msscan.acoes.comum import gravar_conferido
    from msscan.rede.monitor import baixar_retomando

    cfg, motivo = carregar_config(pasta_dados)
    info: dict[str, Any] = {"situacao": FALHOU, "mensagem": "", "versao": (cfg or {}).get("versao"), "url": (cfg or {}).get("url"),
                            "termos": (cfg or {}).get("termos"), "origem": "download automático do site da Dell"}  # fmt: skip
    if cfg is None:
        info["mensagem"] = f"configuração do download inválida ({motivo})"
        return info
    ram = Path(PASTA_DOWNLOAD) / cfg["arquivo"]
    try:
        ram.parent.mkdir(parents=True, exist_ok=True)
        limite = int(int(cfg.get("tamanho") or 64 * 2**20) * 1.1) + 2**20
        resposta = baixar_retomando(executor, cfg["url"], str(ram), limite, timeout=600, monitor=monitor)
        if not resposta.ok:
            semrede = resposta.erro_tipo in ("conexao", "dns", "timeout", "proxy")
            info["situacao"] = SEM_INTERNET if semrede else FALHOU
            info["mensagem"] = f"download não concluído ({resposta.erro or resposta.status})" + (
                "; continua de onde parou na próxima tentativa" if Path(str(ram) + ".parcial").is_file() else "")
            return info
        if (resposta.sha256 or "").lower() != cfg["sha256"]:
            ram.unlink(missing_ok=True)
            info["mensagem"] = (f"SHA-256 diferente do publicado pela Dell (esperado {cfg['sha256'][:16]}…, obtido "
                                f"{(resposta.sha256 or '?')[:16]}…): arquivo apagado; seguindo sem racadm (a opção B, pela "
                                "rede do iDRAC, continua valendo)")  # fmt: skip
            return info
        destino = Path(pasta_dados) / "vendor" / cfg["arquivo"]
        with escrita():
            gravado = gravar_conferido(destino, ram.read_bytes())
        ram.unlink(missing_ok=True)
        if gravado != cfg["sha256"]:  # gravar_conferido já relê; aqui confere contra o hash da Dell
            raise ValueError("cópia no pendrive diferente do pacote conferido")
    except (OSError, ValueError) as erro:
        info["mensagem"] = f"pacote baixado e conferido, mas não gravado em vendor/ ({erro})"
        return info
    info.update(situacao=PREPARADO, mensagem=f"{cfg['arquivo']} baixado de dl.dell.com (SHA-256 conferido) e guardado em vendor/",
                pacote=cfg["arquivo"])  # fmt: skip
    return info


def precisa_baixar(ctx: Any) -> bool:
    """Sem racadm e sem pacote em ``vendor/`` (Dell, sistema live real)."""
    return (getattr(ctx.executor, "prepara_ferramentas", False) and "racadm" not in ctx.ferramentas_fabricante
            and bool(ctx.pasta_dados) and procurar_pacote(ctx.executor, ctx.pasta_dados) is None)  # fmt: skip


_TRAVA_INICIO = threading.Lock()


def iniciar_download(ctx: Any, escrita: Any, monitor: Any = None, ao_terminar: Any = None) -> threading.Thread | None:
    """Em segundo plano: baixa, guarda em ``vendor/`` e prepara o racadm. ``ctx.cache["racadm_pendente"]`` (Event) fica
    aberto enquanto isso, para a atualização esperar em vez de falhar. ``None`` se não há o que baixar."""
    from msscan.deteccao.detectores import preparar_racadm

    with _TRAVA_INICIO:  # chamado também quando a rede volta (28.1): nunca dois downloads ao mesmo tempo
        em_andamento = ctx.cache.get("racadm_pendente")
        if (em_andamento is not None and not em_andamento.is_set()) or not precisa_baixar(ctx):
            return None
        pendente = threading.Event()
        ctx.cache["racadm_pendente"] = pendente
        ctx.cache["idrac_tools"] = {"situacao": BAIXANDO, "mensagem": "baixando o iDRAC Tools for Linux do site da Dell…"}

    def trabalho() -> None:
        try:
            info = baixar(ctx.executor, ctx.pasta_dados, escrita, monitor)
            if info["situacao"] == PREPARADO:
                ctx.erros_deteccao.pop("racadm", None)
                preparar_racadm(ctx)
                preparado = ctx.cache.get("idrac_tools") or {}
                ctx.cache["idrac_tools"] = {**preparado, "origem": info["origem"], "url": info["url"], "termos": info["termos"],
                                            "versao": info["versao"], "baixado": True,
                                            "aviso_termos": f"{AVISO_TERMOS}: {info['termos']}"}  # fmt: skip
            else:
                ctx.cache["idrac_tools"] = info
        except Exception as erro:  # noqa: BLE001 - o download nunca derruba o MSscan
            ctx.cache["idrac_tools"] = {"situacao": FALHOU, "mensagem": f"erro inesperado no download: {type(erro).__name__}: {erro}"}
        finally:
            pendente.set()
            if ao_terminar is not None:
                ao_terminar()

    linha = threading.Thread(target=trabalho, name="msscan-idrac-tools", daemon=True)
    linha.start()
    return linha


def esperar_download(ctx: Any, timeout: float = ESPERA_DOWNLOAD_S) -> bool:
    """Atualização que precisa do racadm: espera o download em andamento. ``True`` se esperou (terminou ou não)."""
    pendente = (getattr(ctx, "cache", None) or {}).get("racadm_pendente")
    if pendente is None or pendente.is_set():
        return False
    pendente.wait(timeout)
    return True


__all__ = ["AUSENTE", "BAIXANDO", "FALHOU", "PREPARADO", "RACADM", "SEM_INTERNET", "baixar", "carregar_config", "escolher_pacotes",
           "esperar_download", "iniciar_download", "ler_versoes", "precisa_baixar", "preparar", "procurar_pacote"]
