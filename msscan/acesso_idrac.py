"""Diagnóstico do acesso ao iDRAC (CLAUDE.md 19.2): por onde o MSscan alcança o iDRAC e, se não alcança, o que fazer.

Duas formas (19.1): **B** — Redfish pela rede (porta do iDRAC com cabo e IP alcançável); **A** — ``racadm`` local (pacote
iDRAC Tools for Linux em ``vendor/``), por dentro, sem cabo. Ordem automática: B → A → guia com as duas soluções.

``ler`` (só leitura, pelo executor) junta: rede do iDRAC (``ipmitool lan print``), porta com link (``ipmitool delloem lan
get active`` — o ``delloem lan get`` falha no iDRAC9 real), ``racadm getniccfg`` quando houver racadm, as placas do servidor
e o Redfish detectado. ``avaliar`` (função pura) transforma isso no texto da caixa. **Nunca** sugere desligar ou
reconfigurar a porta do servidor que o iDRAC usa em modo compartilhado.
"""

from __future__ import annotations

from msscan import geracao_idrac, textos
from msscan.core import tls_bmc

import ipaddress
import re
from typing import Any

from msscan.parsers import ErroParser
from msscan.parsers.ipmitool import parse_delloem_active
from msscan.parsers.racadm import parse_racadm_get

OK, ALERTA, ERRO, INFO = "ok", "alerta", "erro", "info"
REDE, RACADM, IPMI, NENHUM, SEM_IDRAC = "rede", "racadm", "ipmi", "nenhum", "sem_idrac"
"""``IPMI``: só o IPMI local responde (28.2: "acessível só por dentro do servidor", nunca "não acessível")."""
TEXTO_CABO_DEDICADA = (textos.t("py_acesso_idrac.cabo_nao_detectado_na_porta"))  # fmt: skip
TEXTO_SEM_RACADM = (textos.t("py_acesso_idrac.para_atualizar_sem_depender_da"))  # fmt: skip
TEXTO_NUNCA_MEXE = textos.t("py_acesso_idrac.o_msscan_nunca_desliga_nem")
TEXTO_IP_FIXO = (textos.t("py_acesso_idrac.ligue_o_idrac_numa_rede"))  # fmt: skip
COMO_DEDICADA = textos.t("py_acesso_idrac.mude_o_nic_selection_para")
_RE_LOM = re.compile(r"(?:LOM|NIC)\s*(\d)", re.IGNORECASE)


# --- Leitura (só leitura; nunca levanta exceção) ------------------------------------------------------------------------


def ler(ctx: Any) -> dict[str, Any]:
    """Tudo o que ``avaliar`` precisa, lido agora pelo acesso interno."""
    from msscan.acoes import idrac
    from msscan.rede.verificacao import listar_interfaces

    ex = ctx.executor
    ipmi = bool(getattr(getattr(ctx, "ipmi", None), "disponivel", False))
    leituras: dict[str, Any] = {"ipmi": ipmi, "rede": None, "ativa": None, "niccfg": None, "interfaces": [],
                                "racadm": (getattr(ctx, "cache", {}) or {}).get("idrac_tools") or {},
                                "racadm_caminho": idrac.racadm(ctx), "redfish": _redfish(ctx),
                                "modelo": getattr(ctx, "modelo", None)}  # fmt: skip
    if ipmi or idrac.racadm(ctx):
        leituras["rede"] = idrac.ler_rede(ctx)
    if ipmi:
        rc, saida, _ = ex.rodar(["ipmitool", "delloem", "lan", "get", "active"], timeout=20)
        if rc == 0:
            try:
                leituras["ativa"] = parse_delloem_active(saida)
            except ErroParser:
                pass
    if idrac.racadm(ctx):
        from msscan.atualizacao import temporarias as temp

        leituras["passthrough"] = idrac.ler_passthrough(ctx)
        leituras["passthrough_registrado"] = any(m["tipo"] == temp.PASSTHROUGH
                                                 for m in temp.Temporarias(getattr(ctx, "pasta_dados", None)).pendentes())  # fmt: skip
        rc, saida, _ = ex.rodar([idrac.racadm(ctx), "getniccfg"], timeout=60)
        if rc == 0:
            try:
                leituras["niccfg"] = parse_racadm_get(saida)
            except ErroParser:
                pass
    try:
        leituras["interfaces"] = [{"nome": i.nome, "link": i.link, "enderecos": list(i.enderecos)}
                                  for i in listar_interfaces(ex) if i.fisica and not i.nic_bmc]  # fmt: skip
    except Exception:  # noqa: BLE001 - leitura nunca derruba a caixa
        leituras["interfaces"] = []
    return leituras


def _redfish(ctx: Any) -> dict[str, Any]:
    info = getattr(ctx, "redfish", None)
    bmc = getattr(ctx, "bmc", None)
    return {"anunciado": bool(getattr(info, "anunciado", False)), "confirmado": bool(getattr(info, "confirmado", False)),
            "endereco": getattr(info, "endereco", None), "origem": getattr(info, "origem", ""),
            "motivo": getattr(info, "motivo", ""), "validado": bool(bmc is not None and getattr(bmc, "validado", False)),
            "modo_tls": getattr(info, "modo_tls", "") or "", "respondeu_http": bool(getattr(info, "respondeu_http", False))}  # fmt: skip


# --- Avaliação (função pura) -----------------------------------------------------------------------------------------


def _porta_servidor(numero: str, interfaces: list[dict[str, Any]]) -> dict[str, Any] | None:
    """LOMn = n-ésima porta integrada = interface ``eno<n>`` (nome do kernel pelo índice do SMBIOS; ex.: eno1np0)."""
    return next((i for i in interfaces if re.fullmatch(rf"eno{numero}(np\d+)?", i["nome"])), None)


def _modo(leituras: dict[str, Any]) -> tuple[str | None, str | None]:
    """``("dedicada" | "compartilhada" | None, número da LOM ou None)``."""
    nic = leituras.get("niccfg") or {}
    selecao = (nic.get("NIC Selection") or "").strip()
    if selecao:
        if selecao.lower().startswith("dedicated"):
            return "dedicada", None
        lom = _RE_LOM.search(nic.get("Active LOM in Shared Mode") or "") or _RE_LOM.search(selecao)
        return "compartilhada", lom.group(1) if lom else None
    porta = ((leituras.get("rede") or {}).get("porta") or "").lower()
    if porta.startswith("dedicada"):
        return "dedicada", None
    if porta.startswith("compartilhada"):
        lom = _RE_LOM.search(porta)
        return "compartilhada", lom.group(1) if lom else None
    ativa = leituras.get("ativa")
    if ativa == "dedicada":
        return "dedicada", None
    if ativa:
        return "compartilhada", ativa[-1]
    return None, None


def _link(leituras: dict[str, Any]) -> bool | None:
    detectado = ((leituras.get("niccfg") or {}).get("Link Detected") or "").strip().lower()
    if detectado in ("yes", "no"):
        return detectado == "yes"
    ativa = leituras.get("ativa")
    return None if ativa is None else bool(ativa)


def _redes(leituras: dict[str, Any]) -> tuple[str | None, list[str], bool | None]:
    """``(rede do iDRAC, redes do servidor, mesma rede?)``; ``None`` quando não dá para comparar."""
    rede = leituras.get("rede") or {}
    try:
        idrac = ipaddress.ip_interface(f"{rede.get('ip')}/{rede.get('mascara')}") if rede.get("ip") and rede.get("mascara") else None
    except ValueError:
        idrac = None
    servidor = []
    for interface in leituras.get("interfaces") or []:
        for endereco in interface.get("enderecos") or []:
            try:
                servidor.append(ipaddress.ip_interface(endereco))
            except ValueError:
                continue
    servidor = [s for s in servidor if s.version == 4]
    if idrac is None or str(idrac.ip) == "0.0.0.0":
        return None, [str(s) for s in servidor], None
    if not servidor:
        return str(idrac.with_prefixlen), [], None
    mesma = any(idrac.ip in s.network or s.ip in idrac.network for s in servidor)
    return str(idrac.with_prefixlen), [str(s) for s in servidor], mesma


def avaliar(leituras: dict[str, Any]) -> dict[str, Any]:
    """Estado da caixa "Acesso ao iDRAC": por onde se alcança o iDRAC, o que está errado e o que fazer."""
    redfish = leituras.get("redfish") or {}
    racadm_info = leituras.get("racadm") or {}
    tem_racadm = bool(leituras.get("racadm_caminho"))
    modo, lom = _modo(leituras)
    link = _link(leituras)
    rede = leituras.get("rede") or {}
    rede_idrac, redes_servidor, mesma_rede = _redes(leituras)
    interfaces = leituras.get("interfaces") or []
    mensagens: list[dict[str, str]] = []

    def dizer(nivel: str, texto: str) -> None:
        mensagens.append({"nivel": nivel, "texto": texto})

    modo_tls = redfish.get("modo_tls") or ""
    if redfish.get("confirmado"):
        situacao = REDE
        if modo_tls and modo_tls != tls_bmc.PADRAO:  # 28.2: firmware antigo, conectado com criptografia de compatibilidade
            titulo = textos.t("py_acesso_idrac.idrac_acessivel_em_modo_de_compatibilidade", redfish.get('endereco') or '?')
            dizer(INFO, textos.t("py_acesso_idrac.modo_de_compatibilidade_usado", tls_bmc.DESCRICAO.get(modo_tls, modo_tls)))
        else:
            titulo = textos.t("py_acesso_idrac.idrac_acessivel_pela_rede_em", redfish.get('endereco') or '?')
        dizer(OK, textos.t("py_acesso_idrac.o_redfish_do_idrac_respondeu") + ("" if redfish.get("validado") else textos.t("py_acesso_idrac.informe_as_credenciais_do_idrac")))  # fmt: skip
    elif tem_racadm:
        situacao = RACADM
        titulo = textos.t("py_acesso_idrac.idrac_acessivel_por_dentro_do")
        dizer(OK, textos.t("py_acesso_idrac.racadm_local_pronto_leitura_da"))  # fmt: skip
    elif leituras.get("ipmi"):
        situacao = IPMI
        titulo = textos.t("py_acesso_idrac.idrac_acessivel_so_por_dentro_ipmi")
    elif redfish.get("anunciado"):
        situacao = NENHUM
        titulo = textos.t("py_acesso_idrac.idrac_encontrado_mas_nao_respondeu")
    else:
        return {"situacao": SEM_IDRAC, "titulo": textos.t("py_acesso_idrac.idrac_nao_detectado_por_dentro"), "modo": None, "link": None,
                "ip": None, "mascara": None, "gateway": None, "rede_idrac": None, "redes_servidor": redes_servidor,
                "mesma_rede": None, "racadm": racadm_info.get("situacao") or "ausente",
                "mensagens": [{"nivel": INFO, "texto": textos.t("py_acesso_idrac.sem_ipmi_local_dev_ipmi0")}, {"nivel": INFO, "texto": TEXTO_SEM_RACADM}],
                "botoes": ["tentar"]}  # fmt: skip

    # Porta do iDRAC: dedicada, compartilhada (com qual porta do servidor) e link.
    if modo == "dedicada":
        if link is False:
            dizer(ERRO, TEXTO_CABO_DEDICADA)
        elif link:
            dizer(OK, textos.t("py_acesso_idrac.porta_dedicada_do_idrac_com"))
    elif modo == "compartilhada":
        porta = _porta_servidor(lom, interfaces) if lom else None
        nome = (f"LOM{lom}" + (f" ({porta['nome']})" if porta else "")) if lom else "integrada (LOM)"
        if (porta and porta.get("link") is False) or (porta is None and link is False):
            dizer(ERRO, textos.t("py_acesso_idrac.o_idrac_esta_usando_a", nome, COMO_DEDICADA, TEXTO_NUNCA_MEXE))  # fmt: skip
        else:
            estado = textos.t("py_acesso_idrac.com_cabo_e_link") if (porta or {}).get("link") or link else textos.t("py_acesso_idrac.link_nao_confirmado")
            dizer(INFO, textos.t("py_acesso_idrac.o_idrac_esta_em_modo", nome, estado, TEXTO_NUNCA_MEXE))
    elif link is False:
        sem_link = [i["nome"] for i in interfaces if i.get("link") is False]
        dizer(ERRO, textos.t("py_acesso_idrac.o_idrac_esta_sem_link", TEXTO_CABO_DEDICADA, ', '.join(sem_link) or 'nenhuma', COMO_DEDICADA)
              + TEXTO_NUNCA_MEXE)  # fmt: skip

    # Rede (IP) do iDRAC × rede do servidor.
    sem_ip = bool(rede.get("lido")) and (not rede.get("ip") or rede.get("ip") == "0.0.0.0")
    if sem_ip and link is False:
        dizer(ALERTA, textos.t("py_acesso_idrac.o_idrac_esta_sem_endereco")
              + (textos.t("py_acesso_idrac.e_o_dhcp_da_rede") if rede.get("origem") == "DHCP" else "."))  # fmt: skip
    elif sem_ip and rede.get("origem") == "DHCP":
        dizer(ALERTA, textos.t("py_acesso_idrac.dhcp_sem_resposta_o_idrac") + (textos.t("py_acesso_idrac.a_porta_tem_link") if link else "") + textos.t("py_acesso_idrac.mas_nenhum_servidor_dhcp_entregou", TEXTO_IP_FIXO))  # fmt: skip
    elif sem_ip:
        dizer(ALERTA, textos.t("py_acesso_idrac.o_idrac_esta_sem_endereco_2", TEXTO_IP_FIXO))
    elif mesma_rede is False:
        dizer(ALERTA, textos.t("py_acesso_idrac.o_idrac_esta_em_outra", rede_idrac, ', '.join(redes_servidor)))  # fmt: skip

    if situacao != REDE and redfish.get("respondeu_http"):
        # Teste de 15/10/2026 (R620, iDRAC7): HTTP 404 em /redfish/v1 = a rede chega ao iDRAC; falta Redfish no firmware.
        # iDRAC6 (11ª): não tem Redfish em nenhuma versão — atualizar o firmware não traz Redfish (frota sobre o R620, 20/10).
        chave = ("idrac_responde_pela_rede_sem_redfish_na_geracao" if geracao_idrac.tem_redfish(leituras.get("modelo")) is False
                 else "idrac_responde_pela_rede_sem_redfish")  # fmt: skip
        dizer(ALERTA, textos.t(f"py_acesso_idrac.{chave}", redfish.get("endereco") or "?"))
    elif situacao != REDE:
        # 28.2: a rede do iDRAC é uma camada; o acesso por dentro (racadm/IPMI) é outra — nunca "não acessível" se ela responde.
        dizer(ALERTA, textos.t("py_acesso_idrac.idrac_inacessivel_pela_rede"))
    if situacao != REDE and redfish.get("anunciado") and not redfish.get("respondeu_http") and not any(m["nivel"] == ERRO for m in mensagens) and mesma_rede is not False:
        dizer(ALERTA, textos.t("py_acesso_idrac.motivo_provavel_0_confira_se", redfish.get('motivo') or 'o Redfish não respondeu'))  # fmt: skip
    if situacao != REDE:
        recomendacao = geracao_idrac.recomendacao_firmware(leituras.get("modelo"))
        if recomendacao:
            dizer(INFO, recomendacao)

    # racadm (opção A), inclusive o download automático do site da Dell (CLAUDE.md 20).
    estado_racadm = racadm_info.get("situacao")
    if not tem_racadm:
        if estado_racadm == "baixando":
            dizer(INFO, textos.t("py_acesso_idrac.baixando_o_idrac_tools_for"))  # fmt: skip
        elif estado_racadm == "falhou":
            dizer(ALERTA, textos.t("py_acesso_idrac.racadm_nao_disponivel_0", racadm_info.get('mensagem')))
        elif situacao != REDE or estado_racadm == "sem_internet":
            dizer(INFO, TEXTO_SEM_RACADM)
    passthrough = leituras.get("passthrough") or {}
    if passthrough.get("ativo") and not leituras.get("passthrough_registrado"):
        dizer(ALERTA, textos.t("py_acesso_idrac.os_to_idrac_pass_through"))  # fmt: skip
    if racadm_info.get("aviso_termos"):
        dizer(INFO, racadm_info["aviso_termos"])
    versao = racadm_info.get("versao")
    texto_racadm = (textos.t("py_acesso_idrac.pronto_versao_0", versao) if versao else "pronto") if tem_racadm else {
        "baixando": textos.t("py_acesso_idrac.baixando_do_site_da_dell")}.get(estado_racadm or "") or (
        textos.t("py_acesso_idrac.nao_disponivel_0", racadm_info.get('mensagem') or 'sem o pacote iDRAC Tools em vendor/'))

    botoes = ["tentar"] + (["credenciais"] if redfish.get("confirmado") and not redfish.get("validado") else []) + (
        ["ip_fixo"] if (sem_ip and link is not False) or mesma_rede is False else [])  # fmt: skip
    return {"situacao": situacao, "titulo": titulo, "modo": modo, "lom": lom, "link": link, "ip": rede.get("ip"),
            "mascara": rede.get("mascara"), "gateway": rede.get("gateway"), "rede_idrac": rede_idrac,
            "redes_servidor": redes_servidor, "mesma_rede": mesma_rede,
            "racadm": "pronto" if tem_racadm else estado_racadm or "ausente", "racadm_texto": texto_racadm, "modo_tls": modo_tls,
            "mensagens": mensagens, "botoes": botoes}  # fmt: skip


def diagnosticar(ctx: Any) -> dict[str, Any]:
    """``avaliar(ler(ctx))``; nunca levanta exceção."""
    try:
        return avaliar(ler(ctx))
    except Exception as erro:  # noqa: BLE001 - a caixa nunca derruba a interface
        return {"situacao": NENHUM, "titulo": textos.t("py_acesso_idrac.nao_foi_possivel_verificar_o"), "mensagens":
                [{"nivel": ALERTA, "texto": f"{type(erro).__name__}: {erro}"}], "botoes": ["tentar"]}  # fmt: skip


def eh_dell(ctx: Any) -> bool:
    """Servidor Dell (a caixa só existe na Dell): fabricante do SMBIOS ou do IPMI (674)."""
    if ctx is None:
        return False
    mc = getattr(getattr(ctx, "ipmi", None), "mc_info", None) or {}
    return "dell" in (getattr(ctx, "fabricante", None) or "").lower() or mc.get("fabricante_id") == "674"


def formatar(estado: dict[str, Any]) -> str:
    """A caixa em texto (modo texto no tty1/SSH)."""
    marcas = {OK: "[ok]", ALERTA: "[!]", ERRO: "[x]", INFO: "[i]"}
    linhas = [textos.t("py_acesso_idrac.acesso_ao_idrac_0", estado.get('titulo'))] + ([f"  racadm: {estado['racadm_texto']}"] if estado.get("racadm_texto") else [])
    linhas += [f"  {marcas.get(m['nivel'], '-')} {m['texto']}" for m in estado.get("mensagens") or []]
    return "\n".join(linhas)


__all__ = ["IPMI", "NENHUM", "RACADM", "REDE", "SEM_IDRAC", "avaliar", "diagnosticar", "eh_dell", "formatar", "ler"]
