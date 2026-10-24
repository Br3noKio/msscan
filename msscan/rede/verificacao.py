"""Verificação obrigatória de rede, em três níveis.

1. placa de rede física com cabo (carrier=1), ignorando a NIC USB interna do BMC;
2. endereço IP obtido (o NetworkManager faz o DHCP; aqui só se verifica);
3. acesso HTTPS à internet.

Tudo passa pelo executor (arquivos do sysfs, ``ip -j addr``, GET HTTPS), então
funciona em replay e é gravado no modo coleta.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from msscan.core.executor import Executor
from msscan.core.modelo import Status
from msscan.parsers import ErroParser
from msscan.parsers.iproute import parse_ip_addr_json
from msscan.parsers.lspci import parse_lspci_nnk
from msscan.parsers.procfs import parametro_boot, parse_cmdline, parse_uevent
from msscan.rede import caminho, relogio
from msscan.rede.relogio import ResultadoRelogio
from msscan.relatorio.simbolos import UNICODE, Simbolos

RAIZ_NET = "/sys/class/net"
# NIC USB interna do BMC (iDRAC, iLO, XCC...): sempre tem link, mas não leva à internet.
DRIVERS_NIC_BMC = {"cdc_ether", "rndis_host", "cdc_ncm"}
FABRICANTES_USB_BMC = {"413c", "03f0", "04b3", "046b"}
"""idVendor USB da NIC interna do BMC: Dell (iDRAC), HP/HPE (iLO), IBM/Lenovo (IMM/XCC), AMI (BMCs genéricos). Um celular
(tethering) ou adaptador USB do técnico usa os mesmos drivers e NÃO é ignorado (revisão independente, M4)."""
TIPO_ETHERNET = "1"
URLS_INTERNET = (
    ("servidor de downloads da Dell", "https://downloads.dell.com/"),
    ("API de IA (Anthropic)", "https://api.anthropic.com/"),
)

SEM_PLACA = "sem_placa"
SEM_CABO = "sem_cabo"
SEM_IP = "sem_ip"
SEM_INTERNET = "sem_internet"


@dataclass
class InterfaceRede:
    """Interface de rede vista pelo kernel."""

    nome: str
    driver: str | None
    fisica: bool
    nic_bmc: bool
    link: bool | None
    enderecos: list[str] = field(default_factory=list)

    @property
    def candidata(self) -> bool:
        """Placa física que pode levar à internet."""
        return self.fisica and not self.nic_bmc


@dataclass
class ResultadoRede:
    """Resultado da verificação de rede."""

    ok: bool
    problema: str = ""
    titulo: str = ""
    detalhes: list[str] = field(default_factory=list)
    interfaces: list[InterfaceRede] = field(default_factory=list)
    https: list[dict[str, Any]] = field(default_factory=list)
    sem_rede_por_parametro: bool = False
    causa: str = ""
    """Causa em linguagem simples (CLAUDE.md 28.1), para a faixa "Sem internet" e o relatório."""
    relogio: dict[str, Any] | None = None
    """O que se fez com o relógio do sistema antes do HTTPS (``ResultadoRelogio.para_dict``)."""

    @property
    def precisa_hora(self) -> bool:
        """Nenhuma fonte acertou o relógio e a hora do sistema é impossível: perguntar ao técnico."""
        return bool(self.relogio and self.relogio.get("precisa_tecnico"))

    def para_dict(self) -> dict[str, Any]:
        """Converte para dicionário serializável em JSON."""
        dados = asdict(self)
        dados["interfaces"] = [asdict(i) for i in self.interfaces]
        return dados


def modo_sem_rede(executor: Executor) -> bool:
    """``True`` se o boot tem o parâmetro oculto ``msscan.semrede=1`` (ou o antigo ``serverdiag.semrede=1``)."""
    texto = executor.ler_arquivo("/proc/cmdline") or ""
    return parametro_boot(parse_cmdline(texto), "semrede") == "1"


def listar_interfaces(executor: Executor) -> list[InterfaceRede]:
    """Interfaces Ethernet (exceto ``lo``) com driver, tipo de placa, link e IPs globais."""
    rc, saida, _ = executor.rodar(["ip", "-j", "addr", "show"], timeout=10)
    enderecos: dict[str, list[str]] = {}
    if rc == 0:
        try:
            for item in parse_ip_addr_json(saida):
                enderecos[item["nome"]] = [
                    f"{e['endereco']}/{e['prefixo']}" for e in item["enderecos"] if e["escopo"] == "global"
                ]
        except ErroParser:
            pass
    interfaces = []
    for nome in executor.listar(RAIZ_NET):
        base = f"{RAIZ_NET}/{nome}"
        if nome == "lo" or (executor.ler_arquivo(f"{base}/type") or "").strip() != TIPO_ETHERNET:
            continue
        fisica = executor.existe(f"{base}/device")
        driver = parse_uevent(executor.ler_arquivo(f"{base}/device/uevent") or "").get("DRIVER") if fisica else None
        fabricante_usb = (executor.ler_arquivo(f"{base}/device/../idVendor") or "").strip().lower() if driver in DRIVERS_NIC_BMC else ""
        carrier = (executor.ler_arquivo(f"{base}/carrier") or "").strip()
        interfaces.append(
            InterfaceRede(
                nome=nome,
                driver=driver,
                fisica=fisica,
                nic_bmc=driver in DRIVERS_NIC_BMC and (not fabricante_usb or fabricante_usb in FABRICANTES_USB_BMC),
                link={"1": True, "0": False}.get(carrier),
                enderecos=enderecos.get(nome, []),
            )
        )
    return interfaces


def _placas_sem_driver(executor: Executor) -> list[str]:
    rc, saida, _ = executor.rodar(["lspci", "-nnk"], timeout=20)
    if rc != 0:
        return []
    try:
        dispositivos = parse_lspci_nnk(saida)
    except ErroParser:
        return []
    return [f"{d['slot']} {d['descricao']}" for d in dispositivos if d["classe"].startswith("02") and not d["driver"]]


ERROS_DE_DATA_TLS = ("not yet valid", "has expired", "certificate expired")
"""Certificado "do futuro" ou vencido: o relógio é a causa, se não foi conferido (revisão independente, A3)."""
URL_SONDA_HTTP = "http://connectivitycheck.gstatic.com/generate_204"
"""Só para separar "firewall bloqueia tudo" de "rede exige proxy" quando o HTTPS falhou (só cabeçalhos)."""


def _causa_falha_https(executor: Executor, respostas: list[Any], relogio_r: ResultadoRelogio | None) -> tuple[str, list[str]]:
    """Causa em linguagem simples (CLAUDE.md 28.1) e o que fazer. Nunca só "tls"."""
    tipos = {r.erro_tipo for r in respostas}
    proxy_configurado = bool(os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY"))
    orientar_proxy = ("Se a rede do cliente exige proxy, informe-o em MSSCAN/config/proxy.conf (ex.: "
                      "http://proxy.empresa:3128) e toque em Tentar novamente.")  # fmt: skip
    if "tls" in tipos:
        data_do_certificado = any(p in r.erro.lower() for r in respostas for p in ERROS_DE_DATA_TLS)
        if relogio_r is not None and not relogio_r.ok:
            causa = ("o relógio do servidor está errado e não pôde ser acertado pela rede" if data_do_certificado else
                     "provavelmente o relógio do servidor (não foi possível conferi-lo pela rede)")  # fmt: skip
            return causa, [
                f"Data e hora deste sistema: {relogio_r.hora_sistema or datetime.now().strftime('%d/%m/%Y %H:%M')}.",
                "Informe a data e a hora corretas (o MSscan ajusta só o relógio do sistema) e corrija a data no BIOS.",
                *([] if data_do_certificado else [orientar_proxy]),
            ]
        return "a rede do cliente intercepta as conexões seguras (proxy ou firewall com inspeção de HTTPS)", [
            "O relógio está certo, mas o certificado dos sites não confere.",
            orientar_proxy if not proxy_configurado else "Confira com a TI do cliente se o proxy configurado inspeciona HTTPS.",
        ]
    if "proxy" in tipos and any("407" in r.erro for r in respostas):
        return "o proxy configurado em config/proxy.conf pede usuário e senha (ou recusou os informados)", [
            "Informe no proxy.conf no formato http://usuario:senha@proxy.empresa:3128 e toque em Tentar novamente."]
    if "proxy" in tipos:
        return "o proxy configurado em config/proxy.conf recusou ou não respondeu", ["Confira o endereço, a porta e a senha do proxy."]
    if "dns" in tipos:
        # Rede de empresa que só sai por proxy costuma bloquear o DNS externo: o proxy resolve os nomes (teste com proxy real).
        return "os nomes dos sites não são resolvidos (DNS da rede do cliente)", [
            "Confira o DNS entregue pelo DHCP ou peça à TI do cliente para liberar DNS externo.",
            *([] if proxy_configurado else ["Redes que só saem por proxy costumam bloquear o DNS externo. " + orientar_proxy]),
        ]
    sonda = executor.http_get(URL_SONDA_HTTP, timeout=6)
    if sonda.status == 407:
        return "a rede do cliente exige proxy com usuário e senha", [orientar_proxy]
    if sonda.respondeu and not proxy_configurado:
        return "a rede do cliente libera só HTTP comum: provavelmente exige proxy para HTTPS", [orientar_proxy]
    return "o firewall da rede do cliente bloqueia a saída para a internet", [
        "Peça à TI do cliente para liberar HTTPS (porta 443) para downloads.dell.com, ou use outra rede.",
        *([] if proxy_configurado else [orientar_proxy]),
    ]


def _testar_https(executor: Executor, timeout_http: float) -> tuple[list[Any], list[dict[str, Any]]]:
    respostas = [executor.http_get(url, timeout=timeout_http) for _, url in URLS_INTERNET]
    return respostas, [{"nome": nome, **r.para_dict(), "url": r.url} for (nome, _), r in zip(URLS_INTERNET, respostas)]


def verificar_rede(executor: Executor, timeout_http: float = 10, testar_internet: bool = True,
                   acertar_relogio: bool = True, forcar_relogio: bool = False) -> ResultadoRede:  # fmt: skip
    """Executa os três níveis da verificação uma vez.

    ``testar_internet=False`` (monitor contínuo, entre testes HTTPS): para no nível do IP. Antes do HTTPS, o relógio do
    sistema é conferido (uma vez por sessão; ``forcar_relogio`` refaz, como no "Tentar novamente"). HTTPS falhou: DNS
    alternativo e as outras placas com IP são tentados sozinhos (``rede/caminho.py``) e a causa vai em linguagem simples.
    """
    interfaces = listar_interfaces(executor)
    candidatas = [i for i in interfaces if i.candidata]
    ignoradas = [f"{i.nome}: NIC USB interna do BMC (driver {i.driver}), ignorada" for i in interfaces if i.nic_bmc]

    if not candidatas:
        detalhes = ignoradas + [f"{i.nome}: interface virtual, ignorada" for i in interfaces if not i.fisica]
        detalhes += [f"placa de rede sem driver (falta firmware?): {p}" for p in _placas_sem_driver(executor)]
        detalhes.append("Use uma placa de rede suportada ou um adaptador de rede USB.")
        return ResultadoRede(False, SEM_PLACA, "SEM PLACA DE REDE: nenhuma placa de rede física utilizável foi encontrada.", detalhes,
                             interfaces, causa="nenhuma placa de rede utilizável (sem driver?)")  # fmt: skip

    com_link = [i for i in candidatas if i.link]
    if not com_link:
        detalhes = [f"{i.nome} ({i.driver or 'driver ?'}): sem link" for i in candidatas] + ignoradas
        detalhes += [f"placa de rede sem driver, não testada: {p}" for p in _placas_sem_driver(executor)]
        detalhes.append("Conecte um cabo de rede ligado a um switch com acesso à internet.")
        return ResultadoRede(False, SEM_CABO, "SEM CABO: nenhuma placa de rede física está com link.", detalhes, interfaces,
                             causa="nenhuma placa com cabo de rede (sem link)")  # fmt: skip

    com_ip = [i for i in com_link if i.enderecos]
    if not com_ip:
        detalhes = [f"{i.nome}: com link, sem endereço IP" for i in com_link]
        detalhes.append("Verifique se a rede tem servidor DHCP e se a porta do switch está na VLAN correta.")
        return ResultadoRede(False, SEM_IP, "SEM DHCP: a placa tem link, mas não recebeu endereço IP.", detalhes, interfaces,
                             causa="a placa tem cabo, mas a rede não entregou endereço IP (DHCP)")  # fmt: skip

    if not testar_internet:
        return ResultadoRede(True, "", "Rede OK (link e IP; internet conferida há pouco)", [], interfaces)

    relogio_r = relogio.conferir(executor, forcar=forcar_relogio) if acertar_relogio else None
    dados_relogio = relogio_r.para_dict() if relogio_r is not None else None
    aviso_relogio = [relogio_r.mensagem] if relogio_r is not None and relogio_r.ajustado and relogio_r.mensagem else []
    respostas, https = _testar_https(executor, timeout_http)
    consertos: list[str] = []
    if not any(r.respondeu for r in respostas):
        placas = [i.nome for i in com_ip]
        if "dns" in {r.erro_tipo for r in respostas}:
            consertos += caminho.usar_dns_alternativo(executor, placas)
        if {r.erro_tipo for r in respostas} & {"conexao", "timeout"}:  # só a rota explica; TLS/proxy/DNS não (B2)
            _, escolha = caminho.escolher_placa(executor, placas)
            consertos += escolha
        if any("só nesta sessão" in c for c in consertos):
            respostas, https = _testar_https(executor, timeout_http)
    if not any(r.respondeu for r in respostas):
        causa, orientacao = _causa_falha_https(executor, respostas, relogio_r)
        tecnicos = [f"{nome}: {r.erro_tipo or 'erro'} ({r.erro})" if r.erro else f"{nome}: {r.erro_tipo or 'erro'}"
                    for (nome, _), r in zip(URLS_INTERNET, respostas)]  # fmt: skip
        detalhes = orientacao + aviso_relogio + consertos + ["Detalhes técnicos:"] + tecnicos
        if relogio_r is not None:
            detalhes += [f"relógio: {t}" for t in relogio_r.tentativas]
        if dados_relogio is not None and relogio_r is not None and not relogio_r.ok and "relógio" in causa:
            dados_relogio["precisa_tecnico"] = True  # o técnico confirma ou corrige a hora pela tela
        return ResultadoRede(False, SEM_INTERNET, f"SEM INTERNET: {causa}.", detalhes, interfaces, https, causa=causa,
                             relogio=dados_relogio)  # fmt: skip

    detalhes = aviso_relogio + consertos
    detalhes += [f"{nome}: sem resposta ({r.erro_tipo})" for (nome, _), r in zip(URLS_INTERNET, respostas) if not r.respondeu]
    return ResultadoRede(True, "", "Rede OK", detalhes, interfaces, https, relogio=dados_relogio)


def _resumir_enderecos(enderecos: list[str]) -> str:
    """Primeiro IPv4 (ou o primeiro endereço) e quantos outros há."""
    principal = next((e for e in enderecos if ":" not in e), enderecos[0])
    outros = len(enderecos) - 1
    return f"{principal} (+{outros})" if outros else principal


NIVEL_OK = "ok"
NIVEL_FALHA = "falha"
NIVEL_NAO_TESTADO = "nao_testado"


def niveis_rede(resultado: ResultadoRede) -> list[dict[str, str]]:
    """Os três níveis (cabo, IP, internet) com a situação de cada um.

    Situação: ``ok``, ``falha`` (onde a verificação parou) ou ``nao_testado``
    (níveis seguintes ao que falhou). Usado pelo terminal e pela interface gráfica.
    """
    ordem = [SEM_PLACA, SEM_CABO, SEM_IP, SEM_INTERNET]
    falhou_em = ordem.index(resultado.problema) if resultado.problema in ordem else len(ordem)
    candidatas = [i for i in resultado.interfaces if i.candidata]
    com_link = [i.nome for i in candidatas if i.link]
    com_ip = [f"{i.nome} {_resumir_enderecos(i.enderecos)}" for i in candidatas if i.link and i.enderecos]
    respondeu = [h["nome"] for h in resultado.https if h.get("status") is not None]
    niveis = []
    for i, (chave, nome, detalhe) in enumerate(
        (
            ("cabo", "Placa de rede com cabo", ", ".join(com_link)),
            ("ip", "Endereço IP", "; ".join(com_ip)),
            ("internet", "Acesso à internet (HTTPS)", ", ".join(respondeu)),
        ),
        start=1,
    ):
        if falhou_em > i:
            situacao = NIVEL_OK
        elif falhou_em == i or (i == 1 and falhou_em == 0):
            situacao = NIVEL_FALHA
        else:
            situacao = NIVEL_NAO_TESTADO
        niveis.append({"chave": chave, "nome": nome, "situacao": situacao, "detalhe": detalhe if situacao == NIVEL_OK else ""})
    return niveis


def formatar_resultado_rede(resultado: ResultadoRede, simbolos: Simbolos = UNICODE) -> str:
    """Texto para o terminal com os três níveis e, se houver, o problema."""
    ok, falha = simbolos[Status.OK], simbolos[Status.FALHA]
    if resultado.sem_rede_por_parametro:
        return "Rede: verificação desativada pelo parâmetro de boot msscan.semrede=1 (IA e atualizações desativadas)"
    linhas = []
    for i, nivel in enumerate(niveis_rede(resultado), start=1):
        if nivel["situacao"] == NIVEL_OK:
            linhas.append(f"  [{i}/3] {nivel['nome']}: {ok} {nivel['detalhe']}")
        elif nivel["situacao"] == NIVEL_FALHA:
            linhas.append(f"  [{i}/3] {nivel['nome']}: {falha}")
            break
    if not resultado.ok:
        linhas.append("")
        linhas.append(f"{falha} {resultado.titulo}")
    for detalhe in resultado.detalhes:
        linhas.append(f"    · {detalhe}")
    return "\n".join(linhas)


FAIXA_SEM_INTERNET = "Sem internet: catálogo online, downloads e IA indisponíveis"
"""Faixa da interface e do modo texto quando se segue sem internet (CLAUDE.md 28.1)."""


def aguardar_rede(
    executor: Executor,
    saida: Callable[[str], None] = print,
    entrada: Callable[[str], str] | None = input,
    dormir: Callable[[float], None] = time.sleep,
    espera_automatica: float = 60,
    intervalo: float = 3,
    simbolos: Simbolos = UNICODE,
) -> ResultadoRede:
    """Verifica a rede e devolve o resultado, com ou sem internet (CLAUDE.md 28.1: a rede não bloqueia mais).

    Primeiro espera automaticamente (link e DHCP demoram após o boot). Se o relógio não pôde ser acertado e a hora é
    impossível, pergunta a data e hora ao técnico (com ``entrada``; ``None`` = interface/replay, que perguntam do seu
    jeito) e verifica de novo. Sem internet, quem chama segue e o monitor reconecta em segundo plano.
    """
    resultado = verificar_rede(executor)
    tentativas = int(espera_automatica // intervalo) if intervalo > 0 else 0
    avisou = False
    while not resultado.ok and resultado.problema in (SEM_CABO, SEM_IP) and tentativas > 0:
        if not avisou:
            saida(f"  Aguardando a rede ficar pronta (até {espera_automatica:g} s)...")
            avisou = True
        dormir(intervalo)
        tentativas -= 1
        resultado = verificar_rede(executor)
    saida(formatar_resultado_rede(resultado, simbolos))
    if entrada is not None and resultado.precisa_hora:
        resultado = perguntar_hora(executor, resultado, saida, entrada, simbolos)
    if not resultado.ok and not resultado.sem_rede_por_parametro:
        saida(f"\n{FAIXA_SEM_INTERNET} ({resultado.causa or resultado.titulo}).")
        saida("  O MSscan segue (diagnóstico, iDRAC, relatórios, pacotes já no pendrive) e reconecta sozinho em segundo plano.")
    return resultado


def perguntar_hora(executor: Executor, resultado: ResultadoRede, saida: Callable[[str], None],
                   entrada: Callable[[str], str], simbolos: Simbolos = UNICODE) -> ResultadoRede:  # fmt: skip
    """Modo texto: data e hora pelo técnico (até 3 vezes; Enter mantém a do sistema). Só o relógio do sistema."""
    hora = (resultado.relogio or {}).get("hora_sistema", "")
    saida("\nO relógio do servidor está errado e não pôde ser acertado pela rede (NTP, sites HTTP e iDRAC).")
    for _ in range(3):
        texto = entrada(f"Data e hora corretas (dd/mm/aaaa hh:mm) [Enter mantém {hora}]: ").strip()
        if not texto:
            return resultado
        acerto = relogio.ajustar_pelo_tecnico(executor, texto)
        if acerto.ok:
            if acerto.mensagem:
                saida(f"  {acerto.mensagem}")
            novo = verificar_rede(executor)
            saida(formatar_resultado_rede(novo, simbolos))
            return novo
        saida(f"  {acerto.mensagem}")
    return resultado
