"""Configurar o iDRAC pelo acesso interno, sem reiniciar (pedido de 01/10/2026, CLAUDE.md 17.4).

Quatro ações do catálogo fechado, pelo IPMI local (``/dev/ipmi0``) ou pelo ``racadm`` local quando estiver em ``vendor/``:

- ``idrac.ver`` — só leitura: IP, origem (DHCP/fixo), máscara, gateway, porta (dedicada/compartilhada), usuários (sem
  senhas), OS to iDRAC Pass-through e Redfish;
- (``idrac.rede`` saiu em 01/10/2026: a rede do iDRAC nunca é alterada pela atualização automática, CLAUDE.md 18.4);
- ``idrac.ip_fixo`` — exceção pedida pelo usuário (CLAUDE.md 21.5): IP fixo no iDRAC, AVULSA, com ``CORRIGIR``, nunca
  automática; antes, testa se o IP está livre (``ping`` e, com o servidor na mesma rede, ``arping -D``). Escrita só por
  ``Executor.acao_rede_idrac`` (lista fechada em ``validar_rede_idrac``); "Desfazer" volta à origem e aos valores guardados;
- ``idrac.passthrough`` — OS to iDRAC Pass-through por USB (só com racadm: ``iDRAC.OS-BMC.AdminState``/``PTMode``);
- ``idrac.usuario_temporario`` — administrador temporário do MSscan (senha aleatória forte, só na memória desta sessão),
  APAGADO ao terminar, ao desligar/reiniciar pelo MSscan e, se o MSscan fechar sem apagar, no próximo boot (marcador
  ``MSSCAN/estado/idrac_usuario_temporario.json``, sem a senha). Nunca altera a senha de usuários existentes: o executor
  só deixa alterar o slot criado pelo MSscan (``Executor.slots_temporarios``).

Antes × depois, confirmação ``CORRIGIR``, "Desfazer" (rede e pass-through voltam aos valores guardados e conferidos no
pendrive; o usuário temporário é apagado) e registro no relatório, como as outras ações (``execucao.py``).
Escrita só por ``Executor.acao_comando`` (lista fechada em ``validar_acao_idrac``), atrás da trava ``ACOES_REAIS_LIBERADAS``.

MOCKUP(rodada final): nenhuma escrita testada num iDRAC real (comandos da documentação oficial); a saída do ``racadm get``
não tem coleta real; o apagamento do usuário pelo nome vazio não está na documentação (o MSscan confere relendo a lista).
"""

from __future__ import annotations

import ipaddress
import json
import secrets
import string
from datetime import datetime
from pathlib import Path
from typing import Any

from msscan.acoes.comum import MODO_APLICAR, PASTA_ACOES, gravar_conferido, no_pendrive, novo_resultado, passo
from msscan.core.executor import NOME_TEMPORARIO, SENHA_TEMPORARIA
from msscan.parsers import ErroParser
from msscan.parsers.ipmitool import parse_lan_print, parse_user_list
from msscan.parsers.racadm import parse_racadm_get

VER, PASSTHROUGH, USUARIO, IP_FIXO = "idrac.ver", "idrac.passthrough", "idrac.usuario_temporario", "idrac.ip_fixo"
CANAL = "1"
"""Canal LAN do iDRAC no IPMI (o ``lan print 1`` do R640 real é o 802.3 LAN)."""
MARCADOR = "estado/idrac_usuario_temporario.json"
AVISO_CLIENTE = ("Esta ação ALTERA O iDRAC DO CLIENTE. Fica registrada no relatório e pode ser desfeita (o usuário temporário "
                 "é apagado ao terminar).")  # fmt: skip
_ALFABETO = string.ascii_letters + string.digits
_ORIGENS_IP = {"DHCP Address": "DHCP", "Static Address": "fixo", "BIOS Assigned Address": "BIOS", "Unspecified": "não informado"}


def racadm(ctx: Any) -> str | None:
    """Caminho do ``racadm`` local (``vendor/`` ou PATH), se houver."""
    return (getattr(ctx, "ferramentas_fabricante", None) or {}).get("racadm")


# --- Leitura (só leitura; nunca levanta exceção) ------------------------------------------------------


def _racadm_get(ctx: Any, grupo: str) -> dict[str, str] | None:
    caminho = racadm(ctx)
    if not caminho:
        return None
    rc, saida, _ = ctx.executor.rodar([caminho, "get", grupo], timeout=60)
    if rc != 0:
        return None
    try:
        return parse_racadm_get(saida)
    except ErroParser:
        return None


def ler_rede(ctx: Any) -> dict[str, Any]:
    """IP, origem, máscara e gateway do iDRAC pelo IPMI (``lan print``) e a porta (racadm ``iDRAC.NIC``)."""
    rc, saida, erro = ctx.executor.rodar(["ipmitool", "lan", "print", CANAL], timeout=20)
    rede: dict[str, Any] = {"ip": None, "origem": None, "mascara": None, "gateway": None, "porta": None, "lido": False,
                            "motivo": ""}  # fmt: skip
    if rc == 0:
        try:
            campos = parse_lan_print(saida)
            rede.update(ip=campos.get("IP Address"), mascara=campos.get("Subnet Mask"), gateway=campos.get("Default Gateway IP"),
                        origem=_ORIGENS_IP.get(campos.get("IP Address Source", ""), campos.get("IP Address Source")), lido=True)  # fmt: skip
        except ErroParser as falha:
            rede["motivo"] = str(falha)
    else:
        rede["motivo"] = (erro.strip().splitlines() or [f"código {rc}"])[-1]
    nic = _racadm_get(ctx, "iDRAC.NIC")
    if nic and nic.get("Selection"):
        rede["porta"] = "dedicada" if nic["Selection"].lower().startswith(("1", "dedicated")) else f"compartilhada ({nic['Selection']})"
    else:
        rc, saida, _ = ctx.executor.rodar(["ipmitool", "delloem", "lan", "get"], timeout=20)
        texto = saida.strip()
        if rc == 0 and texto:
            rede["porta"] = "dedicada" if "dedicated" in texto.lower() else f"compartilhada ({texto})"
    return rede


def ler_usuarios(ctx: Any) -> list[dict[str, Any]] | None:
    """Usuários do iDRAC (slot, nome, privilégio IPMI) — nunca senhas. ``None`` se não deu para ler."""
    rc, saida, _ = ctx.executor.rodar(["ipmitool", "user", "list", CANAL], timeout=20)
    if rc != 0:
        return None
    try:
        return parse_user_list(saida)
    except ErroParser:
        return None


def ler_passthrough(ctx: Any) -> dict[str, Any]:
    """OS to iDRAC Pass-through (racadm ``iDRAC.OS-BMC``). Sem racadm: não verificado."""
    grupo = _racadm_get(ctx, "iDRAC.OS-BMC")
    if grupo is None:
        return {"lido": False, "motivo": "sem o racadm em vendor/ (Dell iDRAC Tools for Linux): não verificado"}
    capaz = grupo.get("PTCapability", "")
    return {"lido": True, "capaz": capaz.lower().startswith(("0", "capable")), "ativo": grupo.get("AdminState", "").lower() in ("1", "enabled"),
            "modo": "usb-p2p" if grupo.get("PTMode", "").lower() in ("1", "usb-p2p") else "lom-p2p",
            "ip_usb": grupo.get("UsbNicIpAddress") or None, "bruto": grupo}  # fmt: skip


def ler_configuracao(ctx: Any) -> dict[str, Any]:
    """Tudo o que ``idrac.ver`` mostra (só leitura)."""
    rede = ler_rede(ctx)
    usuarios = ler_usuarios(ctx)
    pt = ler_passthrough(ctx)
    redfish = getattr(ctx, "redfish", None)
    return {
        "IP": rede["ip"] or "?", "Origem do IP": rede["origem"] or "?", "Máscara": rede["mascara"] or "?",
        "Gateway": rede["gateway"] or "?", "Porta de rede": rede["porta"] or "não informada (sem racadm; o IPMI deste iDRAC não responde)",
        "Usuários (sem senhas)": [f"{u['slot']}: {u['nome']} ({u['privilegio'].lower()})" for u in usuarios or [] if u["nome"]] or ["?"],
        "OS to iDRAC Pass-through": ("ligado" if pt.get("ativo") else "desligado") + (f" ({pt['modo']})" if pt.get("ativo") else "")
                                    if pt.get("lido") else pt["motivo"],
        "Redfish": (f"responde em {redfish.endereco} ({redfish.origem})" if getattr(redfish, "confirmado", False)
                    else f"não responde daqui ({getattr(redfish, 'motivo', '') or 'não detectado'})"),
    }  # fmt: skip


# --- Pré-checagens ----------------------------------------------------------------------------------------


def _base(identificador: str, titulo: str) -> dict[str, Any]:
    return {"acao": identificador, "titulo": titulo, "metodo": None, "comandos": [], "parametros": {}, "antes": {},
            "previa": [], "avisos": [], "bloqueios": [], "nada_a_fazer": False, "pode_seguir": False}  # fmt: skip


def _sem_ipmi(ctx: Any, pre: dict[str, Any]) -> bool:
    if not getattr(getattr(ctx, "ipmi", None), "disponivel", False) and not racadm(ctx):
        pre["bloqueios"].append("Sem IPMI local (/dev/ipmi0) nem racadm em vendor/: o MSscan não alcança o iDRAC por dentro.")
        return True
    return False


def prechecar_ver(ctx: Any) -> dict[str, Any]:
    pre = _base(VER, "Ver a configuração do iDRAC")
    if _sem_ipmi(ctx, pre):
        return pre
    pre["antes"] = ler_configuracao(ctx)
    pre["nada_a_fazer"] = True
    pre["avisos"].append("Só leitura: nada é alterado. Para pôr um IP fixo no iDRAC, escolha \"Colocar um IP fixo no iDRAC\" "
                         "(pass-through e usuário temporário são feitos sozinhos pela atualização, e desfeitos no fim).")  # fmt: skip
    return pre


def _ipv4(texto: Any) -> str | None:
    try:
        return str(ipaddress.IPv4Address(str(texto or "").strip()))
    except ValueError:
        return None


def ip_livre(executor: Any, ip: str) -> tuple[bool | None, str]:
    """O IP responde na rede? ``(True, como)`` livre; ``(False, motivo)`` em uso; ``(None, motivo)`` sem como testar.

    ``ping`` (2 pacotes) e, se o servidor tem uma placa na mesma rede, ``arping -D`` (detecção de endereço duplicado:
    acha até equipamento que não responde a ping). Só leitura."""
    from msscan.rede.verificacao import listar_interfaces

    rc, _, erro = executor.rodar(["ping", "-n", "-c", "2", "-W", "1", ip], timeout=10)
    if rc == 0:
        return False, f"o IP {ip} respondeu ao ping: já está em uso por outro equipamento"
    if rc not in (1,):
        return None, f"ping não pôde ser executado ({(erro or '').strip()[:120] or rc})"
    como = "sem resposta ao ping"
    try:
        alvo = ipaddress.IPv4Address(ip)
        placas = [i for i in listar_interfaces(executor) if i.fisica and not i.nic_bmc and i.link
                  and any(alvo in ipaddress.ip_interface(e).network for e in i.enderecos if ":" not in e)]  # fmt: skip
    except Exception:  # noqa: BLE001 - sem a lista de placas, fica só o ping
        placas = []
    if placas:
        rc, _, erro = executor.rodar(["arping", "-D", "-c", "2", "-w", "3", "-I", placas[0].nome, ip], timeout=10)
        if rc == 1:
            return False, f"o IP {ip} respondeu ao ARP na placa {placas[0].nome}: já está em uso"
        if rc == 0:
            como += f" nem ao ARP (placa {placas[0].nome}, mesma rede)"
        else:
            como += f"; ARP não testado ({(erro or '').strip()[:120] or rc})"
    else:
        como += " (o servidor não tem placa nessa rede: o ARP não pôde ser testado; equipamento que não responde a ping passa"
        como += " despercebido)"
    return True, como


def prechecar_ip_fixo(ctx: Any, executor: Any, parametros: dict[str, Any] | None, persistente: bool) -> dict[str, Any]:
    """21.5: IP fixo no iDRAC (ex.: DHCP sem resposta). Só leitura aqui; nada muda sem ``CORRIGIR``."""
    pre = _base(IP_FIXO, "Colocar um IP fixo no iDRAC")
    parametros = parametros or {}
    if _sem_ipmi(ctx, pre):
        return pre
    if not persistente:
        pre["bloqueios"].append("Sem local gravável no pendrive: a configuração atual precisa ficar guardada (para Desfazer).")
    rede = ler_rede(ctx)
    if not rede["lido"]:
        pre["bloqueios"].append(f"Não foi possível ler a rede atual do iDRAC ({rede['motivo']}): nada é alterado sem o antes.")
        return pre
    pre["antes"] = {"IP": rede["ip"], "Origem do IP": rede["origem"], "Máscara": rede["mascara"], "Gateway": rede["gateway"]}
    pre["bruto_antes"] = {k: rede[k] for k in ("ip", "origem", "mascara", "gateway")}
    ip, mascara, gateway = (_ipv4(parametros.get(c)) for c in ("ip", "mascara", "gateway"))
    if not ip or not mascara or not gateway:
        pre["bloqueios"].append("Informe IP, máscara e gateway IPv4 válidos (ex.: 192.168.1.120, 255.255.255.0, 192.168.1.1).")
        return pre
    try:
        rede_nova = ipaddress.IPv4Network(f"{ip}/{mascara}", strict=False)
    except ValueError:
        pre["bloqueios"].append("Máscara inválida.")
        return pre
    if ipaddress.IPv4Address(gateway) not in rede_nova or gateway == ip:
        pre["bloqueios"].append(f"O gateway {gateway} precisa estar na rede {rede_nova} e ser diferente do IP.")
        return pre
    if rede_nova.prefixlen < 31 and ipaddress.IPv4Address(ip) in (rede_nova.network_address, rede_nova.broadcast_address):
        pre["bloqueios"].append(f"{ip} é o endereço de rede ou de broadcast de {rede_nova}.")
        return pre
    pre["parametros"] = {"ip": ip, "mascara": mascara, "gateway": gateway}
    if rede["origem"] == "fixo" and (rede["ip"], rede["mascara"], rede["gateway"]) == (ip, mascara, gateway):
        pre["nada_a_fazer"] = True
        pre["avisos"].append("O iDRAC já está com esse IP fixo: nada a alterar.")
        return pre
    if ip != rede["ip"]:
        livre, como = ip_livre(executor, ip)
        if livre is not True:
            pre["bloqueios"].append(f"IP não confirmado como livre: {como}. Escolha outro IP (peça um livre ao responsável pela rede).")
            return pre
        pre["previa"].append(f"IP livre: {como}")
    usar_racadm = bool(racadm(ctx))
    pre["metodo"] = "racadm" if usar_racadm else "ipmi"
    pre["comandos"] = comandos_rede(pre["metodo"], racadm(ctx), "fixo", ip, mascara, gateway)
    pre["previa"] += [f"Origem do IP: {rede['origem']} → fixo", f"IP: {rede['ip']} → {ip}", f"Máscara: {rede['mascara']} → {mascara}",
                      f"Gateway: {rede['gateway']} → {gateway}"] + [f"Comando: {' '.join(c)}" for c in pre["comandos"]]  # fmt: skip
    pre["previa"].append(f"Antes de alterar: configuração atual guardada e conferida em MSSCAN/{PASTA_ACOES}/ (para Desfazer)")
    pre["avisos"] += ["Esta ação ALTERA A REDE DO iDRAC DO CLIENTE. Fica registrada no relatório e pode ser desfeita.",
                      "Quem acessa o iDRAC pela rede perde a conexão no endereço antigo (o servidor não reinicia).",
                      "A atualização automática nunca muda a rede do iDRAC: só esta ação, pedida pelo técnico."]  # fmt: skip
    pre["pode_seguir"] = not pre["bloqueios"]
    return pre


def comandos_rede(metodo: str, caminho: str | None, modo: str, ip: str = "", mascara: str = "", gateway: str = "") -> list[list[str]]:
    """Comandos da rede do iDRAC (``ipmitool lan set`` ou ``racadm set iDRAC.IPv4.*``). ``modo``: ``fixo`` ou ``dhcp``."""
    if metodo == "racadm":
        if modo == "dhcp":
            return [[str(caminho), "set", "iDRAC.IPv4.DHCPEnable", "1"]]
        return [[str(caminho), "set", "iDRAC.IPv4.DHCPEnable", "0"], [str(caminho), "set", "iDRAC.IPv4.Address", ip],
                [str(caminho), "set", "iDRAC.IPv4.Netmask", mascara], [str(caminho), "set", "iDRAC.IPv4.Gateway", gateway]]  # fmt: skip
    if modo == "dhcp":
        return [["ipmitool", "lan", "set", CANAL, "ipsrc", "dhcp"]]
    return [["ipmitool", "lan", "set", CANAL, "ipsrc", "static"], ["ipmitool", "lan", "set", CANAL, "ipaddr", ip],
            ["ipmitool", "lan", "set", CANAL, "netmask", mascara], ["ipmitool", "lan", "set", CANAL, "defgw", "ipaddr", gateway]]  # fmt: skip


def prechecar_passthrough(ctx: Any, persistente: bool) -> dict[str, Any]:
    pre = _base(PASSTHROUGH, "Ligar o OS to iDRAC Pass-through (acesso interno ao Redfish)")
    pt = ler_passthrough(ctx)
    if not pt["lido"]:
        pre["bloqueios"].append("O pass-through só é configurado pelo racadm local: copie o racadm (Dell iDRAC Tools for Linux) "
                                "para a pasta vendor/ do pendrive. " + pt["motivo"].capitalize() + ".")  # fmt: skip
        return pre
    if not persistente:
        pre["bloqueios"].append("Sem local gravável no pendrive: a configuração atual precisa ficar guardada (para Desfazer).")
    pre["antes"] = {"Pass-through": "ligado" if pt["ativo"] else "desligado", "Modo": pt["modo"], "IP interno (USB NIC)": pt["ip_usb"]}
    pre["bruto_antes"] = {"AdminState": "1" if pt["ativo"] else "0", "PTMode": "1" if pt["modo"] == "usb-p2p" else "0"}
    if not pt["capaz"]:
        pre["bloqueios"].append("O iDRAC informa que este servidor não suporta o OS to iDRAC Pass-through (PTCapability).")
        return pre
    if pt["ativo"] and pt["modo"] == "usb-p2p":
        pre["nada_a_fazer"] = True
        pre["avisos"].append("O pass-through por USB já está ligado: nada a alterar.")
        return pre
    caminho = racadm(ctx)
    pre["metodo"] = "racadm"
    pre["comandos"] = [[caminho, "set", "iDRAC.OS-BMC.AdminState", "1"], [caminho, "set", "iDRAC.OS-BMC.PTMode", "1"]]
    pre["previa"] = ["Pass-through: " + ("ligado" if pt["ativo"] else "desligado") + " → ligado", f"Modo: {pt['modo']} → usb-p2p"]
    pre["previa"] += [f"Comando: {' '.join(c)}" for c in pre["comandos"]]
    pre["avisos"] += [AVISO_CLIENTE, f"Depois, o MSscan procura o Redfish pelo endereço interno {pt['ip_usb'] or '(informado pelo iDRAC)'}."]
    pre["pode_seguir"] = not pre["bloqueios"]
    return pre


def slot_livre(usuarios: list[dict[str, Any]]) -> int | None:
    """Primeiro slot de 3 a 16 sem nome e sem acesso (nunca 1, o anônimo, nem 2, o root de fábrica)."""
    return next((u["slot"] for u in usuarios if 3 <= u["slot"] <= 16 and not u["nome"] and u["privilegio"].upper() == "NO ACCESS"), None)


def prechecar_usuario(ctx: Any, persistente: bool) -> dict[str, Any]:
    pre = _base(USUARIO, "Criar um usuário administrador temporário do MSscan no iDRAC")
    if _sem_ipmi(ctx, pre):
        return pre
    if not persistente:
        pre["bloqueios"].append("Sem pendrive gravável: o marcador que garante o apagamento no próximo boot precisa ficar no "
                                "pendrive (se o MSscan fechar sem apagar).")  # fmt: skip
    usuarios = ler_usuarios(ctx)
    if usuarios is None:
        pre["bloqueios"].append("Não foi possível ler os usuários do iDRAC (ipmitool user list).")
        return pre
    existentes = [u for u in usuarios if NOME_TEMPORARIO.fullmatch(u["nome"])]
    pre["antes"] = {"Usuários (sem senhas)": [f"{u['slot']}: {u['nome']} ({u['privilegio'].lower()})" for u in usuarios if u["nome"]]}
    if existentes:
        pre["bloqueios"].append(f"Já há um usuário temporário do MSscan (slot {existentes[0]['slot']}): apague-o antes (Desfazer).")
    slot = slot_livre(usuarios)
    if slot is None:
        pre["bloqueios"].append("Nenhum slot livre (3 a 16, sem nome e sem acesso) no iDRAC: nada é alterado.")
        return pre
    nome = "msscan" + secrets.token_hex(2)
    pre["parametros"] = {"slot": slot, "nome": nome}
    pre["metodo"] = "racadm" if racadm(ctx) else "ipmi"
    pre["previa"] = [f"Slot {slot} (vazio) → usuário {nome}, administrador, senha aleatória de 20 caracteres (só na memória)",
                     "Usado só nesta sessão para o Redfish (diagnóstico completo e atualizações) e APAGADO ao terminar, ao "
                     "desligar/reiniciar pelo MSscan ou, se o MSscan fechar antes, no próximo boot pelo pendrive",
                     f"Marcador sem a senha em MSSCAN/{MARCADOR} (para apagar no próximo boot)",
                     "Senhas de usuários existentes NUNCA são alteradas"]  # fmt: skip
    pre["avisos"] += [AVISO_CLIENTE, "Pelo IPMI, o iDRAC9 pode não dar ao usuário o perfil de login do Redfish: o MSscan testa "
                      "a sessão logo depois; se não der, o usuário é apagado na hora e a mensagem explica."]  # fmt: skip
    pre["pode_seguir"] = not pre["bloqueios"]
    return pre


# --- Execução ---------------------------------------------------------------------------------------------


def _guardar(pre: dict[str, Any], modo: str, pasta_dados: str) -> tuple[Path, str]:
    destino = Path(pasta_dados) / PASTA_ACOES / f"{pre['acao'].replace('.', '_')}_antes_{datetime.now():%Y-%m-%d_%H%M%S}.json"
    conteudo = {"acao": pre["acao"], "guardado": datetime.now().isoformat(timespec="seconds"), "modo": modo, "metodo": pre["metodo"],
                "antes": pre["antes"], "bruto": pre.get("bruto_antes") or {}}  # fmt: skip
    return destino, gravar_conferido(destino, (json.dumps(conteudo, ensure_ascii=False, indent=1) + "\n").encode("utf-8"))


def _rodar(executor: Any, resultado: dict[str, Any], comandos: list[list[str]], rotulo: str, rede: bool = False) -> bool:
    for comando in comandos:
        rc, _, erro = executor.acao_rede_idrac(comando) if rede else executor.acao_comando(comando)
        visivel = " ".join("********" if SENHA_TEMPORARIA.fullmatch(a) else (a or '""') for a in comando)
        if rc != 0:
            resultado["situacao"] = "bloqueada_trava" if "ACOES_REAIS_LIBERADAS" in (erro or "") else "falhou"
            resultado["mensagem"] = f"{rotulo}: o iDRAC recusou ({(erro or '').strip() or f'código {rc}'}). Comando: {visivel}"
            passo(resultado, rotulo, resultado["mensagem"], "bloqueado" if resultado["situacao"] == "bloqueada_trava" else "falhou")
            return False
        passo(resultado, rotulo, f"aceito: {visivel}")
    return True


def executar(ctx: Any, executor: Any, pre: dict[str, Any], modo: str, pasta_dados: str | None) -> dict[str, Any]:
    """Executa a ação pré-checada e confirmada (CORRIGIR conferido por ``execucao.py``). Nunca levanta exceção."""
    if pre["acao"] == USUARIO:
        return _executar_usuario(ctx, executor, pre, modo, pasta_dados)
    resultado = novo_resultado(pre, modo)
    try:
        if not pasta_dados:
            raise OSError("sem local gravável no pendrive")
        arquivo, soma = _guardar(pre, modo, pasta_dados)
    except (OSError, ValueError) as falha:
        resultado["situacao"], resultado["mensagem"] = "falhou", f"Configuração atual não guardada no pendrive ({falha}): nada foi alterado."
        passo(resultado, "guardar valores atuais", resultado["mensagem"], "falhou")
        return resultado
    local = no_pendrive(arquivo, pasta_dados)
    resultado["anteriores"] = {"arquivo": str(arquivo), "no_pendrive": local, "sha256": soma}
    passo(resultado, "guardar valores atuais", f"{local} (relido do pendrive; SHA-256 {soma[:16]}… conferido)")
    if modo != MODO_APLICAR:
        for comando in pre["comandos"]:
            passo(resultado, "alterar (simulado)", " ".join(comando), "simulado")
        resultado["situacao"], resultado["mensagem"] = "simulada", f"Simulação concluída: nada foi alterado no iDRAC. Valores atuais em {local}."
        resultado["desfazer"] = {"disponivel": False, "arquivo": str(arquivo), "motivo": "simulação: nada foi alterado no iDRAC"}
        return resultado
    if not _rodar(executor, resultado, pre["comandos"], "alterar", rede=pre["acao"] == IP_FIXO):
        return resultado
    resultado["desfazer"] = {"disponivel": True, "arquivo": str(arquivo), "motivo": ""}
    if pre["acao"] == IP_FIXO:
        rede, pedido = ler_rede(ctx), pre["parametros"]
        resultado["depois"] = {"IP": rede["ip"], "Origem do IP": rede["origem"], "Máscara": rede["mascara"], "Gateway": rede["gateway"]}
        confere = (rede["ip"], rede["mascara"], rede["gateway"]) == (pedido["ip"], pedido["mascara"], pedido["gateway"])
        resultado["situacao"] = "aplicada" if confere else "aplicada_sem_confirmacao"
        resultado["mensagem"] = ("IP fixo aplicado no iDRAC (relido e conferido). Use \"Tentar de novo\" na caixa \"Acesso ao iDRAC\" "
                                 "para o MSscan procurar o Redfish no endereço novo." if confere else "O iDRAC aceitou, mas a "
                                 "releitura ainda não mostra o IP novo: confira em alguns segundos com \"Ver a configuração do iDRAC\".")  # fmt: skip
    if pre["acao"] == PASSTHROUGH:
        pt = ler_passthrough(ctx)
        resultado["depois"] = {"Pass-through": "ligado" if pt.get("ativo") else "desligado", "Modo": pt.get("modo"), "IP interno (USB NIC)": pt.get("ip_usb")}
        encontrou = bool(pt.get("ip_usb")) and _procurar_redfish(ctx, pt["ip_usb"])
        resultado["situacao"] = "aplicada" if pt.get("ativo") else "aplicada_sem_confirmacao"
        resultado["mensagem"] = "Pass-through ligado. " + (f"Redfish encontrado pelo endereço interno {pt['ip_usb']}: informe as "
                                "credenciais (ou crie o usuário temporário)." if encontrou else "O Redfish ainda não respondeu pelo "
                                "endereço interno: a placa USB do iDRAC pode levar alguns segundos para aparecer; refaça o diagnóstico.")  # fmt: skip
    return resultado


def _procurar_redfish(ctx: Any, endereco: str) -> bool:
    from msscan.deteccao.detectores import tentar_redfish

    try:
        return tentar_redfish(ctx, endereco, None, "OS to iDRAC Pass-through (USB)")
    except Exception:  # noqa: BLE001 - procurar nunca derruba a ação
        return False


def gerar_senha() -> str:
    """20 caracteres aleatórios (letras e dígitos: aceitos pelo IPMI e pelo racadm sem escapar nada)."""
    while True:
        senha = "".join(secrets.choice(_ALFABETO) for _ in range(20))
        if any(c.isdigit() for c in senha) and any(c.islower() for c in senha) and any(c.isupper() for c in senha):
            return senha


def comandos_criar(metodo: str, caminho: str | None, slot: int, nome: str, senha: str) -> list[list[str]]:
    if metodo == "racadm":
        prefixo = f"iDRAC.Users.{slot}."
        return [[caminho, "set", prefixo + "UserName", nome], [caminho, "set", prefixo + "Password", senha],
                [caminho, "set", prefixo + "Privilege", "0x1ff"], [caminho, "set", prefixo + "IpmiLanPrivilege", "4"],
                [caminho, "set", prefixo + "Enable", "1"]]  # fmt: skip
    s = str(slot)
    return [["ipmitool", "user", "set", "name", s, nome], ["ipmitool", "user", "set", "password", s, senha, "20"],
            ["ipmitool", "channel", "setaccess", CANAL, s, "callin=on", "ipmi=on", "link=on", "privilege=4"],
            ["ipmitool", "user", "priv", s, "4", CANAL], ["ipmitool", "user", "enable", s]]  # fmt: skip


def comandos_apagar(metodo: str, caminho: str | None, slot: int) -> list[list[str]]:
    if metodo == "racadm":
        prefixo = f"iDRAC.Users.{slot}."
        return [[caminho, "set", prefixo + "Enable", "0"], [caminho, "set", prefixo + "Privilege", "0x0"],
                [caminho, "set", prefixo + "IpmiLanPrivilege", "15"], [caminho, "set", prefixo + "UserName", ""]]  # fmt: skip
    s = str(slot)
    return [["ipmitool", "user", "disable", s],
            ["ipmitool", "channel", "setaccess", CANAL, s, "callin=off", "ipmi=off", "link=off", "privilege=15"],
            ["ipmitool", "user", "set", "password", s, gerar_senha(), "20"],  # a senha usada some de vez
            ["ipmitool", "user", "set", "name", s, ""]]  # fmt: skip


def _marcador(pasta_dados: str) -> Path:
    return Path(pasta_dados) / MARCADOR


def _executar_usuario(ctx: Any, executor: Any, pre: dict[str, Any], modo: str, pasta_dados: str | None) -> dict[str, Any]:
    resultado = novo_resultado(pre, modo)
    slot, nome = pre["parametros"]["slot"], pre["parametros"]["nome"]
    senha = gerar_senha()
    visiveis = comandos_criar(pre["metodo"], racadm(ctx), slot, nome, "********")
    if modo != MODO_APLICAR:
        for comando in visiveis:
            passo(resultado, "criar (simulado)", " ".join(comando), "simulado")
        resultado["situacao"], resultado["mensagem"] = "simulada", "Simulação concluída: nenhum usuário foi criado no iDRAC."
        resultado["desfazer"] = {"disponivel": False, "motivo": "simulação: nenhum usuário foi criado"}
        return resultado
    try:
        if not pasta_dados:
            raise OSError("sem local gravável no pendrive")
        marcador = {"slot": slot, "nome": nome, "metodo": pre["metodo"], "criado": datetime.now().isoformat(timespec="seconds")}
        gravar_conferido(_marcador(pasta_dados), (json.dumps(marcador, ensure_ascii=False) + "\n").encode("utf-8"))
    except (OSError, ValueError) as falha:
        resultado["situacao"], resultado["mensagem"] = "falhou", f"Marcador não gravado no pendrive ({falha}): nenhum usuário foi criado."
        passo(resultado, "marcador", resultado["mensagem"], "falhou")
        return resultado
    passo(resultado, "marcador", f"{no_pendrive(_marcador(pasta_dados), pasta_dados)} gravado e conferido (sem a senha)")
    ok = _rodar(executor, resultado, comandos_criar(pre["metodo"], racadm(ctx), slot, nome, senha), "criar")
    resultado["desfazer"] = {"disponivel": True, "motivo": "", "slot": slot, "nome": nome, "metodo": pre["metodo"]}
    if not ok:
        _limpar_depois_de_falha(ctx, executor, pasta_dados, resultado)  # o que entrou sai (nunca fica usuário pela metade)
        return resultado
    resultado["depois"] = {"Usuário temporário": f"{slot}: {nome} (administrador, apagado ao terminar)"}
    ctx.cache["usuario_temporario"] = {"usuario": nome, "senha": senha}  # só na memória; nunca vai para relatório/coleta
    validado = validar_redfish(ctx)
    if validado is False:
        resultado["situacao"] = "falhou"
        resultado["mensagem"] = ("O usuário foi criado, mas o Redfish não aceitou o login (pelo IPMI o iDRAC pode não dar o perfil "
                                 "de login): o usuário foi apagado. Com o racadm em vendor/, o MSscan dá o perfil completo.")  # fmt: skip
        _limpar_depois_de_falha(ctx, executor, pasta_dados, resultado)
        return resultado
    resultado["situacao"] = "aplicada"
    resultado["mensagem"] = (f"Usuário temporário {nome} criado no slot {slot}. " + ("Sessão Redfish aberta com ele." if validado
                             else "O Redfish não responde daqui agora: a sessão é aberta quando ele responder (rede ou pass-through).")
                             + " Ele é apagado ao terminar.")  # fmt: skip
    return resultado


def _limpar_depois_de_falha(ctx: Any, executor: Any, pasta_dados: str | None, resultado: dict[str, Any]) -> None:
    """Apaga o que a criação deixou, sem esconder o motivo da falha (a mensagem original fica)."""
    limpeza = apagar(ctx, executor, pasta_dados)
    resultado["passos"] += limpeza["passos"]
    resultado["mensagem"] += f" Limpeza: {limpeza['mensagem']}"
    resultado["desfazer"] = {"disponivel": False, "motivo": "o usuário temporário já foi apagado (ou não chegou a ser criado)"}


def validar_redfish(ctx: Any) -> bool | None:
    """Abre a sessão Redfish com o usuário temporário (se o Redfish responde). ``None`` = Redfish não responde daqui."""
    credenciais = (getattr(ctx, "cache", None) or {}).get("usuario_temporario")
    acesso = getattr(ctx, "bmc", None)
    if not credenciais or acesso is None or acesso.cliente is None:
        return None
    if acesso.validado:
        return True
    acesso.tentativas = 0  # credencial gerada agora pelo próprio MSscan (não é adivinhação de senha)
    acesso.tentar(credenciais["usuario"], credenciais["senha"])
    return bool(acesso.validado and acesso.administrador is not False)


def apagar(ctx: Any, executor: Any, pasta_dados: str | None, resultado: dict[str, Any] | None = None) -> dict[str, Any]:
    """Apaga o usuário temporário (desta sessão ou de uma anterior, pelo marcador). Confere relendo a lista. Nunca levanta."""
    registro = resultado if resultado is not None else {"passos": [], "situacao": "", "mensagem": ""}
    marcador_arquivo = _marcador(pasta_dados) if pasta_dados else None
    try:
        marcador = json.loads(marcador_arquivo.read_text(encoding="utf-8")) if marcador_arquivo and marcador_arquivo.is_file() else None
    except (OSError, ValueError):
        marcador = None
    alvo = {int(k): v for k, v in getattr(executor, "slots_temporarios", {}).items()}
    if marcador and isinstance(marcador.get("slot"), int):
        alvo.setdefault(marcador["slot"], str(marcador.get("nome") or ""))
    if not alvo:
        registro["situacao"], registro["mensagem"] = "nada", "Nenhum usuário temporário do MSscan para apagar."
        return registro
    usuarios = ler_usuarios(ctx) or []
    acesso = getattr(ctx, "bmc", None)
    if acesso is not None and (getattr(ctx, "cache", {}) or {}).get("usuario_temporario") and acesso.validado:
        acesso.encerrar()  # logout antes de apagar o usuário da sessão
    getattr(ctx, "cache", {}).pop("usuario_temporario", None)
    for slot, nome in alvo.items():
        atual = next((u["nome"] for u in usuarios if u["slot"] == slot), None)
        if atual != nome:
            passo(registro, "apagar", f"slot {slot}: o nome atual ({atual or 'vazio'}) não é {nome}: nada alterado (já apagado ou outro usuário)")
            executor.slots_temporarios.pop(slot, None)
            continue
        executor.reconhecer_temporario(slot, nome)
        metodo = (marcador or {}).get("metodo") or ("racadm" if racadm(ctx) else "ipmi")
        *tirar_acesso, apagar_nome = comandos_apagar(metodo, racadm(ctx), slot)
        if _rodar(executor, registro, tirar_acesso, "apagar"):
            # Sem acesso e com a senha trocada, o usuário já não serve para nada; apagar o nome é o passo final
            # (nome vazio não está na documentação: se o iDRAC recusar, ele fica desativado e sem acesso).
            rc, _, erro = executor.acao_comando(apagar_nome)
            passo(registro, "apagar o nome", "aceito" if rc == 0 else f"o iDRAC não aceitou apagar o nome ({(erro or '').strip() or rc}): "
                  "o usuário fica desativado e sem acesso", "ok" if rc == 0 else "aviso")  # fmt: skip
            executor.slots_temporarios.pop(slot, None)
    relidos = ler_usuarios(ctx) or []
    restantes = [u for u in relidos if u["slot"] in alvo and u["nome"] == alvo[u["slot"]] and u["privilegio"].upper() != "NO ACCESS"]
    if relidos and not restantes:
        if marcador_arquivo is not None:
            marcador_arquivo.unlink(missing_ok=True)
        registro["situacao"], registro["mensagem"] = "apagado", "Usuário temporário do MSscan apagado do iDRAC (conferido relendo a lista)."
    else:
        registro["situacao"] = "falhou"
        registro["mensagem"] = ("Não foi possível confirmar o apagamento do usuário temporário: o marcador fica no pendrive e o "
                                "MSscan tenta de novo no próximo boot. Confira no iDRAC (Usuários) e apague à mão se preciso.")  # fmt: skip
    passo(registro, "conferir", registro["mensagem"], "ok" if registro["situacao"] == "apagado" else "aviso")
    return registro


def ha_marcador(pasta_dados: str | None) -> bool:
    return bool(pasta_dados) and _marcador(pasta_dados).is_file()


# --- Desfazer ---------------------------------------------------------------------------------------------


def prechecar_desfazer(ctx: Any, resultado: dict[str, Any]) -> dict[str, Any]:
    """Prévia do "Desfazer" (só leitura): rede e pass-through voltam aos valores guardados; o usuário é apagado."""
    acao = resultado.get("acao")
    pre = {**_base(str(acao), "Desfazer: " + str(resultado.get("titulo") or acao)), "desfazer": True}
    info = resultado.get("desfazer") or {}
    if resultado.get("desfeita"):
        pre["bloqueios"].append("Esta ação já foi desfeita.")
        return pre
    if not info.get("disponivel"):
        pre["bloqueios"].append((info.get("motivo") or "nada foi alterado").capitalize() + ".")
        return pre
    if acao == USUARIO:
        pre["previa"] = [f"Apagar o usuário temporário {info.get('nome')} (slot {info.get('slot')}), conferindo o nome antes"]
        pre["pode_seguir"] = True
        return pre
    try:
        guardado = json.loads(Path(info["arquivo"]).read_text(encoding="utf-8"))
        if guardado.get("acao") != acao:
            raise ValueError("arquivo de outra ação")
    except (OSError, ValueError, KeyError) as falha:
        pre["bloqueios"].append(f"Valores anteriores não lidos do pendrive ({falha}): restaure pela interface do iDRAC.")
        return pre
    bruto, caminho = guardado.get("bruto") or {}, racadm(ctx)
    pre["metodo"], pre["antes"] = guardado.get("metodo"), guardado.get("antes") or {}
    if acao == IP_FIXO:
        pre["metodo"] = "racadm" if caminho else "ipmi"
        if bruto.get("origem") == "DHCP":
            pre["comandos"] = comandos_rede(pre["metodo"], caminho, "dhcp")
        elif all(_ipv4(bruto.get(c)) for c in ("ip", "mascara", "gateway")) and bruto.get("ip") != "0.0.0.0":
            pre["comandos"] = comandos_rede(pre["metodo"], caminho, "fixo", bruto["ip"], bruto["mascara"], bruto["gateway"])
        else:
            pre["bloqueios"].append("Os valores guardados não têm IP, máscara e gateway válidos: restaure pela interface do iDRAC.")
            return pre
        pre["previa"] = [f"Voltar a: {json.dumps(pre['antes'], ensure_ascii=False)}"] + [f"Comando: {' '.join(c)}" for c in pre["comandos"]]
        pre["pode_seguir"] = True
        return pre
    if acao != PASSTHROUGH:
        pre["bloqueios"].append("Ação sem desfazer automático: restaure pela interface do iDRAC.")
        return pre
    pre["comandos"] = [[caminho, "set", "iDRAC.OS-BMC.AdminState", bruto.get("AdminState", "0")],
                       [caminho, "set", "iDRAC.OS-BMC.PTMode", bruto.get("PTMode", "0")]]  # fmt: skip
    if not caminho:
        pre["bloqueios"].append("Sem o racadm em vendor/: não dá para restaurar o pass-through por aqui.")
        return pre
    pre["previa"] = [f"Voltar a: {json.dumps(pre['antes'], ensure_ascii=False)}"] + [f"Comando: {' '.join(c)}" for c in pre["comandos"]]
    pre["pode_seguir"] = True
    return pre


def desfazer(ctx: Any, executor: Any, pre: dict[str, Any], resultado: dict[str, Any], modo: str, pasta_dados: str | None) -> dict[str, Any]:
    """Executa o desfazer. Registra em ``resultado["desfeita"]``. Nunca levanta exceção."""
    registro: dict[str, Any] = {"modo": modo, "situacao": "", "mensagem": "", "passos": [], "quando": datetime.now().isoformat(timespec="seconds")}
    if modo != MODO_APLICAR:
        passo(registro, "desfazer (simulado)", "; ".join(" ".join(c) for c in pre.get("comandos") or []) or pre["previa"][0], "simulado")
        registro["situacao"], registro["mensagem"] = "simulada", "Simulação do desfazer: nada foi alterado no iDRAC."
    elif pre["acao"] == USUARIO:
        apagar(ctx, executor, pasta_dados, registro)
        registro["situacao"] = "desfeita" if registro["situacao"] == "apagado" else registro["situacao"]
    elif _rodar(executor, registro, pre["comandos"], "restaurar", rede=pre["acao"] == IP_FIXO):
        registro["situacao"], registro["mensagem"] = "desfeita", "iDRAC de volta aos valores anteriores (confira com \"Ver a configuração do iDRAC\")."
    registro["fim"] = datetime.now().isoformat(timespec="seconds")
    if registro["situacao"] in ("simulada", "desfeita"):
        resultado["desfeita"] = registro
    else:
        resultado.setdefault("tentativas_desfazer", []).append(registro)
    return registro
