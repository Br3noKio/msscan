"""Aplicação de firmware (fase 9): plano, confirmações, download + hash, envio pelo Redfish e retomada.

Regras (CLAUDE.md 11 e 14):
- Modo real é o padrão do programa (CLAUDE.md 18.2: sem simulação na interface nem no modo texto). O ``modo`` padrão
  desta função continua ``simular`` só como proteção de quem a chama sem dizer; ``--simular`` (opção interna de
  desenvolvimento) faz tudo que não grava nada no servidor e descreve, pedido por pedido, o que SERIA enviado ao BMC.
- Grava de verdade só com a trava ``GRAVACAO_REAL_LIBERADA`` ligada (liberada em 30/09/2026, CLAUDE.md 15.6).
- Depois do ``ATUALIZAR``, o caminho de cada componente (Redfish, administrador temporário, pass-through, racadm,
  ``.BIN``, repositório do iDRAC) é escolhido por ``automatica.py`` (CLAUDE.md 18.3 e 18.7), sem novas perguntas.
- Só itens com "atualização disponível" (nunca downgrade; comparação incerta não é oferecida), todos com hash
  oficial (SHA-256; MD5 só como alternativa). Hash divergente → arquivo apagado e o item abortado.
- Confirmação digitada ``ATUALIZAR``; com redundância de energia perdida, outra confirmação, separada.
- Um item por vez, do menor risco para o maior (BIOS por último); firmware agendado para o reinício (``OnReset``) → boot único pelo USB,
  estado salvo em ``estado/atualizacao_pendente.json`` e reinício; na volta, a retomada confere antes × depois.
- Usuário do BMC somente leitura ou sem credenciais → administrador TEMPORÁRIO pelo IPMI interno, desfeito no fim
  (``automatica.py`` e ``temporarias.py``, CLAUDE.md 18.4).

MOCKUP(rodada final): o formato do envio multipart, da tarefa (``TaskService``), da exportação de configuração e a
ordem do fabricante vêm do exemplo oficial da Dell e da norma DMTF, sem coleta real do iDRAC9 ainda.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from msscan import textos
from msscan.atualizacao import progresso as prog_mod
from msscan.atualizacao.aviso import eh_idrac, minutos_item, peso_ordem
from msscan.atualizacao.versoes import ATUALIZACAO
from msscan.core.executor import (
    CORPO_BOOT_USB,
    PARAMETROS_UPDATE,
    Executor,
)
from msscan.core.fallback import FalhaColeta
from msscan.parsers import ErroParser
from msscan.parsers.redfish import membros, parse_boot, parse_item_firmware, parse_tarefa, parse_update_service

MODO_SIMULAR, MODO_APLICAR = "simular", "aplicar"
CONFIRMACAO = "ATUALIZAR"
CONFIRMACAO_REDUNDANCIA = "SEM REDUNDANCIA"
"""Confirmação extra, específica e separada do ATUALIZAR (fonte sem energia/ausente: CLAUDE.md 11.3)."""
PASTA_PACOTES = "firmware/pacotes"
PASTA_BACKUP = "firmware/backup"
ARQUIVO_ESTADO = "estado/atualizacao_pendente.json"
FOLGA_ESPACO = 1.2
"""Espaço livre exigido: 20% além da soma dos pacotes."""
MAX_CONSULTAS_TAREFA = 120
FALHAS_SEGUIDAS_TAREFA = 3
ESPERA_TAREFA_S = 5.0

# Ordem de instalação: menor risco primeiro, BIOS sempre por último (CLAUDE.md 15.6) — a mesma da tela de aviso.
# MOCKUP(rodada final): conferir com a documentação da Dell e a coleta real (pré-requisitos <Dependency> já são
# conferidos no plano: item com pré-requisito não atendido é recusado, qualquer que seja a ordem).


# --- Plano ----------------------------------------------------------------------------------------


def _ordem(item: dict[str, Any]) -> int:
    return peso_ordem({"categoria": (item.get("pacote") or {}).get("categoria"), "componente": item.get("componente")})


def montar_plano(verificacao: dict[str, Any] | None, selecionados: list[str]) -> dict[str, Any]:
    """Itens escolhidos (na ordem do fabricante), o que foi recusado e o que bloqueia. Nada é executado aqui."""
    verificacao = verificacao or {}
    itens = {i.get("id"): i for i in verificacao.get("itens") or [] if isinstance(i, dict)}
    bloqueios = list(verificacao.get("bloqueios") or [])
    if verificacao.get("situacao") != "verificada":
        bloqueios.append(textos.t("py_aplicacao.verificacao_de_atualizacoes_nao_concluida", verificacao.get('mensagem') or textos.t("py_aplicacao.faca_a_verificacao_antes")))
    escolhidos, recusados, avisos_requisitos = [], [], []
    for identificador in dict.fromkeys(selecionados):
        item = itens.get(identificador)
        pacote = (item or {}).get("pacote") or {}
        if item is None:
            recusados.append(textos.t("py_aplicacao.item_desconhecido", identificador))
        elif item.get("situacao") != ATUALIZACAO:
            recusados.append(textos.t("py_aplicacao.so_itens_com_atualizacao_disponivel", item['componente'], item.get('texto_situacao')))  # fmt: skip
        elif item.get("bloqueios"):
            recusados.append(f"{item['componente']}: {'; '.join(item['bloqueios'])}")
        elif not pacote.get("url") or not (pacote.get("sha256") or pacote.get("md5")):
            recusados.append(textos.t("py_aplicacao.pacote_sem_endereco_ou_sem", item['componente']))
        elif any(r.get("atendido") is False for r in item.get("pre_requisitos") or []):
            # Pré-requisito declarado pela Dell (<Dependency>) com versão instalada mais velha: atualizar aquele antes.
            recusados.append(f"{item['componente']}: " + "; ".join(r["texto"] for r in item["pre_requisitos"] if r.get("atendido") is False))
        else:
            escolhidos.append(item)
            avisos_requisitos.extend(f"{item['componente']}: {r['texto']}" for r in item.get("pre_requisitos") or []
                                     if r.get("atendido") is None)  # fmt: skip
    if not selecionados:
        bloqueios.append(textos.t("py_aplicacao.nenhum_item_selecionado_a_selecao"))
    escolhidos.sort(key=_ordem)
    return {
        "itens": escolhidos,
        "recusados": recusados,
        "bloqueios": bloqueios,
        "confirmacao_redundancia": bool(verificacao.get("redundancia_perdida")),
        "avisos": avisos_requisitos,
        "tamanho_total": sum(int((i.get("pacote") or {}).get("tamanho") or 0) for i in escolhidos),
        "pode_seguir": bool(escolhidos) and not bloqueios and not recusados,
        "system_id": verificacao.get("system_id"),
    }


def conferir_confirmacoes(plano: dict[str, Any], confirmacao: str, confirmacao_redundancia: str = "") -> str:
    """``""`` se as confirmações digitadas batem; senão, o motivo. Espaços nas pontas são ignorados; maiúsculas não."""
    if (confirmacao or "").strip() != CONFIRMACAO:
        return textos.t("py_aplicacao.confirmacao_nao_conferida_digite_em", CONFIRMACAO)
    if plano.get("confirmacao_redundancia") and (confirmacao_redundancia or "").strip() != CONFIRMACAO_REDUNDANCIA:
        return (textos.t("py_aplicacao.redundancia_de_energia_perdida_confirme", CONFIRMACAO_REDUNDANCIA))  # fmt: skip
    return ""


# --- Execução ------------------------------------------------------------------------------------


def _hash_arquivo(caminho: Path, algoritmo: str) -> str:
    soma = hashlib.new(algoritmo)
    with open(caminho, "rb") as arquivo:
        for bloco in iter(lambda: arquivo.read(1024 * 1024), b""):
            soma.update(bloco)
    return soma.hexdigest()


def _passo(resultado: dict[str, Any], acao: str, detalhe: str, situacao: str = "ok", **extra: Any) -> None:
    resultado["passos"].append({"acao": acao, "detalhe": detalhe, "situacao": situacao, **extra})


def hash_ainda_confere(arquivo: str | Path, algoritmo: str, esperado: str) -> str:
    """``""`` se o arquivo ainda tem o hash conferido; senão o motivo (revisão independente, MÉDIO 3).

    Recalculado imediatamente antes de enviar ao BMC ou executar: o pacote fica no FAT do pendrive entre a conferência e
    o uso. ``algoritmo``: ``sha256`` ou ``md5``.
    """
    try:
        obtido = _hash_arquivo(Path(arquivo), algoritmo)
    except OSError as erro:
        return textos.t("py_aplicacao.nao_foi_possivel_reler_o", erro)
    if obtido.lower() != (esperado or "").lower():
        return textos.t("py_aplicacao.o_pacote_mudou_depois_da", algoritmo.upper())
    return ""


def _vigiar_download(parcial: Path, tamanho: int, progresso: prog_mod.Progresso, componente: str) -> Callable[[], None]:
    """Percentual do download pelo tamanho do ``.parcial`` (21.1). Devolve a função que para a vigia."""
    import threading

    parar = threading.Event()

    def vigiar() -> None:
        while not parar.wait(1.0):
            try:
                progresso.percentual(componente, min(99.0, parcial.stat().st_size * 100 / tamanho))
            except OSError:
                continue

    if tamanho > 0:
        threading.Thread(target=vigiar, name="msscan-download-progresso", daemon=True).start()
    return parar.set


def _baixar_e_conferir(executor: Executor, item: dict[str, Any], pasta: Path, monitor: Any, avisar: Any,
                       progresso: prog_mod.Progresso | None = None) -> tuple[Path | None, str]:  # fmt: skip
    """``(arquivo, "")`` com o hash oficial conferido; senão ``(None, motivo)`` (arquivo divergente é apagado)."""
    from msscan.rede.monitor import baixar_retomando

    pacote = item["pacote"]
    arquivo = pasta / (Path(urlsplit(pacote["url"]).path).name or f"{item['id']}.bin")
    if arquivo.is_file():
        # Baixado antes (etapa 2 do fluxo guiado): reusa só se o hash oficial conferir agora; senão baixa de novo.
        algoritmo, esperado = ("sha256", pacote["sha256"]) if pacote.get("sha256") else ("md5", pacote.get("md5") or "")
        if esperado and _hash_arquivo(arquivo, algoritmo) == esperado.lower():
            item["_hash_conferido"] = (algoritmo, esperado.lower())
            return arquivo, textos.t("py_aplicacao.confere_baixado_antes_e_conferido", 'SHA-256' if algoritmo == 'sha256' else 'MD5')
        arquivo.unlink(missing_ok=True)
    limite = int(int(pacote.get("tamanho") or 0) * 1.1) + 1024 * 1024 if pacote.get("tamanho") else 4 * 1024**3
    parar = (_vigiar_download(Path(str(arquivo) + ".parcial"), int(pacote.get("tamanho") or 0), progresso, item["componente"])
             if progresso is not None else lambda: None)  # fmt: skip
    try:
        resposta = baixar_retomando(executor, pacote["url"], str(arquivo), limite, timeout=600, monitor=monitor, avisar=avisar)
    finally:
        parar()
    if not resposta.ok:
        return None, textos.t("py_aplicacao.download_falhou", resposta.erro or resposta.status)
    if pacote.get("sha256"):
        algoritmo, esperado, obtido = "SHA-256", pacote["sha256"].lower(), (resposta.sha256 or "").lower()
    else:
        algoritmo, esperado, obtido = "MD5", pacote["md5"].lower(), _hash_arquivo(arquivo, "md5")
    if obtido != esperado:
        arquivo.unlink(missing_ok=True)
        return None, textos.t("py_aplicacao.hash_divergente_esperado_obtido_arquivo", algoritmo, esperado[:16], obtido[:16])
    item["_hash_conferido"] = ("sha256" if algoritmo == "SHA-256" else "md5", esperado)
    return arquivo, f"{algoritmo} confere"


def baixar_e_verificar(itens: list[dict[str, Any]], executor: Executor, pasta_dados: str | None, system_id: str | None = None,
                       monitor: Any = None, avisar: Any = None) -> list[dict[str, Any]]:  # fmt: skip
    """Etapas 2 e 3 do fluxo guiado (pedido de 01/10/2026): baixa do catálogo oficial e confere hash, modelo e versão de
    cada item ANTES da tela de aviso. Nada é enviado ao BMC; a instalação reusa o arquivo se o hash conferir de novo."""
    pasta = Path(pasta_dados or "/tmp/msscan") / PASTA_PACOTES
    pasta.mkdir(parents=True, exist_ok=True)
    saida = []
    for item in itens:
        arquivo, detalhe = _baixar_e_conferir(executor, dict(item), pasta, monitor, avisar)
        saida.append({"id": item["id"], "componente": item["componente"], "ok": arquivo is not None, "hash": detalhe,
                      "modelo": textos.t("py_aplicacao.confere_pacote_listado_pela_dell", system_id or '?'),
                      "versao": textos.t("py_aplicacao.mais_nova", item.get('instalada') or '?', item.get('disponivel') or '?')})  # fmt: skip
    return saida


def _tem_racadm(ctx: Any) -> bool:
    try:
        from msscan.acoes import idrac

        return bool(idrac.racadm(ctx))
    except Exception:  # noqa: BLE001 - só informa a mensagem
        return False


def ler_bmc(ctx: Any) -> dict[str, Any]:
    """O que o BMC informa para a aplicação (só leitura): UpdateService, sistema/boot, versões anteriores, perfil.

    Sem sessão Redfish validada: ``{"redfish": False, ...}`` com o motivo.
    """
    acesso = getattr(ctx, "bmc", None)
    info: dict[str, Any] = {"redfish": False, "multipart": None, "simple_update": None, "sistema": None, "boot_usb": None,
                            "boot_uefi_usb": None,
                            "anteriores": {}, "administrador": None, "gerente": None, "exportacao": None, "exportacao_formatos": None, "motivo": "",
                            # Para a mensagem certa (3c): o Redfish do BMC responde daqui? É Dell com IPMI local?
                            "alcancavel": bool(getattr(getattr(ctx, "redfish", None), "confirmado", False)),
                            "dell_ipmi": "dell" in str(getattr(ctx, "fabricante", "") or "").lower()
                                         and bool(getattr(getattr(ctx, "ipmi", None), "disponivel", False)),
                            "racadm": _tem_racadm(ctx)}  # fmt: skip
    if acesso is None or not acesso.validado:
        info["motivo"] = textos.t("py_aplicacao.sem_credenciais_do_bmc_validadas")
        return info
    cliente = acesso.cliente
    info["redfish"] = True
    info["administrador"] = acesso.administrador
    try:
        servico = parse_update_service(cliente.get("/redfish/v1/UpdateService"))
        info["multipart"] = servico["multipart"]
        info["simple_update"] = servico["simple_update"]
        for membro in membros(cliente.get(servico["inventario"] or "/redfish/v1/UpdateService/FirmwareInventory"))[:200]:
            if "Previous-" not in membro:
                continue
            dados = parse_item_firmware(cliente.get(membro))
            if dados["anterior"] and dados["software_id"]:
                info["anteriores"][dados["software_id"]] = dados["versao"]
        sistemas = membros(cliente.get("/redfish/v1/Systems"))
        if sistemas:
            info["sistema"] = sistemas[0]
            alvos = parse_boot(cliente.get(sistemas[0]))["alvos"]
            info["boot_usb"] = "Usb" in alvos
            if not info["boot_usb"] and "UefiTarget" in alvos:
                info["boot_uefi_usb"] = _opcao_uefi_usb(cliente, sistemas[0])
        gerentes = membros(cliente.get("/redfish/v1/Managers"))
        if gerentes:
            info["gerente"] = gerentes[0]
            acoes = ((cliente.get(gerentes[0]).get("Actions") or {}).get("Oem") or {})
            acao = next((v for k, v in acoes.items() if k.endswith("ExportSystemConfiguration") and isinstance(v, dict)), None)
            info["exportacao"] = acao.get("target") if acao else None
            # Formatos que o próprio iDRAC anuncia (DMTF: "<parâmetro>@Redfish.AllowableValues"); ausente = não anunciado.
            formatos = (acao or {}).get("ExportFormat@Redfish.AllowableValues")
            info["exportacao_formatos"] = [str(f).upper() for f in formatos] if isinstance(formatos, list) else None
    except (FalhaColeta, ErroParser, AttributeError) as erro:
        info["motivo"] = textos.t("py_aplicacao.leitura_do_bmc_incompleta", erro)
    return info


def _opcao_uefi_usb(cliente: Any, sistema: str) -> str | None:
    """Caminho UEFI do pendrive em ``BootOptions`` (CLAUDE.md 21.4): só com UMA opção USB habilitada (com mais de uma não dá
    para saber qual é o pendrive do MSscan → ``None``, e o plano avisa do F11). MOCKUP(rodada final)."""
    try:
        opcoes = [cliente.get(m) for m in membros(cliente.get(f"{sistema.rstrip('/')}/BootOptions"))[:40]]
    except (FalhaColeta, ErroParser):
        return None
    usb = [o.get("UefiDevicePath") for o in opcoes if isinstance(o, dict) and o.get("BootOptionEnabled", True) is not False
           and isinstance(o.get("UefiDevicePath"), str) and "usb" in (str(o.get("DisplayName")) + o["UefiDevicePath"]).lower()]  # fmt: skip
    return usb[0] if len(usb) == 1 and "usb" in usb[0].lower() else None


CREDENCIAIS, IDRAC_INACESSIVEL, NAO_SUPORTADO_AQUI, ACESSO_INTERNO = "credenciais", "idrac", "nao_suportado", "interno"
"""Códigos de ``motivo_metodo`` (a interface mostra o botão certo: "Informar credenciais" ou "Configurar acesso")."""


def motivo_metodo(bmc: dict[str, Any]) -> dict[str, str]:
    """Por que a aplicação pelo iDRAC não está disponível, com a causa real (pedido de 01/10/2026, 3c).

    ``{"codigo": "", "texto": ""}`` se está disponível. Nunca o genérico "não suportada" quando a causa é outra:
    falta de administrador → ``credenciais``; iDRAC sem acesso daqui → ``idrac``; só sem suporte de verdade → ``nao_suportado``.
    """
    if bmc.get("redfish"):
        if bmc.get("administrador") is not True:
            return {"codigo": CREDENCIAIS, "texto": textos.t("py_aplicacao.precisa_de_um_usuario_administrador_2")}  # fmt: skip
        if not bmc.get("multipart") and not bmc.get("simple_update"):
            return {"codigo": NAO_SUPORTADO_AQUI, "texto": textos.t("py_aplicacao.atualizacao_automatica_nao_suportada_neste_2")}  # fmt: skip
        return {"codigo": "", "texto": ""}
    if bmc.get("alcancavel"):
        return {"codigo": CREDENCIAIS, "texto": textos.t("py_aplicacao.precisa_de_um_usuario_administrador")}  # fmt: skip
    if bmc.get("racadm"):  # 28.2: o racadm local responde → o iDRAC é acessível por dentro (racadm update)
        return {"codigo": ACESSO_INTERNO, "texto": textos.t("py_aplicacao.idrac_acessivel_so_por_dentro_racadm")}  # fmt: skip
    if bmc.get("dell_ipmi"):
        return {"codigo": IDRAC_INACESSIVEL, "texto": textos.t("py_aplicacao.idrac_nao_acessivel_a_partir")}  # fmt: skip
    return {"codigo": NAO_SUPORTADO_AQUI, "texto": textos.t("py_aplicacao.atualizacao_automatica_nao_suportada_neste")}  # fmt: skip


def _rollback(item: dict[str, Any], bmc: dict[str, Any]) -> str | None:
    """Versão anterior guardada pelo BMC para este componente (rollback), ou ``None``."""
    return bmc["anteriores"].get(str(item.get("component_id") or ""))


def _acompanhar_tarefa(cliente: Any, local: str, dormir: Callable[[float], None],
                       ao_ler: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:  # fmt: skip
    """Consulta a tarefa até ela terminar, ficar agendada ou o tempo acabar. MOCKUP(rodada final).

    Falha passageira na consulta (rede, sessão) é repetida até ``FALHAS_SEGUIDAS_TAREFA`` vezes seguidas; sem resposta,
    ``sem_resposta = True`` (quem chama NÃO pode tratar isso como "o firmware não foi aplicado").
    """
    ultima: dict[str, Any] = {"terminou": False, "falhou": True, "sem_resposta": True, "mensagens": [textos.t("py_aplicacao.tarefa_sem_resposta")]}
    falhas = 0
    for _ in range(MAX_CONSULTAS_TAREFA):
        try:
            ultima = parse_tarefa(cliente.get(local))
            falhas = 0
            if ao_ler is not None:
                ao_ler(ultima)  # progresso ao vivo (CLAUDE.md 21.1)
        except (FalhaColeta, ErroParser) as erro:
            falhas += 1
            ultima = {"terminou": False, "falhou": True, "sem_resposta": True, "mensagens": [str(erro)]}
            if falhas >= FALHAS_SEGUIDAS_TAREFA:
                break
        if ultima.get("terminou"):
            break
        dormir(ESPERA_TAREFA_S)
    return ultima


def falha_explicita(tarefa: dict[str, Any]) -> bool:
    """A tarefa terminou em falha declarada pelo BMC (não só "sem resposta" ou texto com "error")."""
    return tarefa.get("estado") in ("Exception", "Killed", "Cancelled")


MENSAGEM_POSSIVELMENTE_AGENDADO = (
    textos.t("py_aplicacao.o_idrac_aceitou_o_firmware"))  # fmt: skip


def executar_plano(
    ctx: Any,
    plano: dict[str, Any],
    executor: Executor,
    pasta_dados: str | None,
    modo: str = MODO_SIMULAR,
    monitor: Any = None,
    avisar: Callable[[str], None] | None = None,
    espaco_livre: Callable[[str], int] | None = None,
    dormir: Callable[[float], None] = time.sleep,
    parcial: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Executa o plano já confirmado. Nunca levanta exceção. ``modo`` simular (padrão) ou aplicar.

    ``parcial``: dicionário preenchido durante a execução (a interface mostra o progresso por componente ao vivo).
    """
    resultado: dict[str, Any] = parcial if parcial is not None else {}
    resultado.update({"modo": modo, "situacao": "", "mensagem": "", "passos": [], "itens": [],
                      "inicio": datetime.now().isoformat(timespec="seconds")})  # fmt: skip
    if not isinstance(resultado.get("_acompanhamento"), prog_mod.Progresso):
        resultado["_acompanhamento"] = prog_mod.Progresso(componentes_do_plano(plano.get("itens") or []))
    try:
        _executar(ctx, plano, executor, pasta_dados, modo, monitor, avisar, espaco_livre, dormir, resultado)
    except Exception as erro:  # noqa: BLE001 - a aplicação nunca derruba o programa
        resultado["situacao"], resultado["mensagem"] = "falhou", textos.t("py_aplicacao.erro_inesperado", type(erro).__name__, erro)
    resultado["fim"] = datetime.now().isoformat(timespec="seconds")
    encerrar_progresso(resultado)
    return resultado


def componentes_do_plano(itens: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Componentes do plano para o ``Progresso`` (estimativa em segundos, a mesma do Plano de atualização)."""
    return [{"componente": i.get("componente"), "estimativa_s": 60 * minutos_item({"categoria": (i.get("pacote") or {}).get("categoria")
             or i.get("categoria"), "componente": i.get("componente")})} for i in itens]  # fmt: skip


def encerrar_progresso(resultado: dict[str, Any]) -> None:
    """O ``Progresso`` vira dados (relatório e log técnico): nada "em andamento" fica para trás."""
    prog = resultado.pop("_acompanhamento", None)
    if isinstance(prog, prog_mod.Progresso):
        for item in prog.itens.values():
            if item["etapa"] not in prog_mod.FINAIS:
                prog.etapa(item["componente"], prog_mod.PARADO if item["etapa"] in (prog_mod.AGUARDANDO, prog_mod.CONFERIDO) else prog_mod.FALHOU,
                           textos.t("py_aplicacao.nao_concluido_nesta_execucao"))  # fmt: skip
        resultado["acompanhamento"] = prog.para_dict()


def _executar(ctx, plano, executor, pasta_dados, modo, monitor, avisar, espaco_livre, dormir, resultado) -> None:  # noqa: ANN001, PLR0912, PLR0915
    if not plano.get("pode_seguir"):
        resultado["situacao"] = "bloqueada"
        resultado["mensagem"] = "; ".join(plano.get("bloqueios", []) + plano.get("recusados", [])) or "nada a aplicar"
        return
    raiz = Path(pasta_dados or "/tmp/msscan")
    pacotes = raiz / PASTA_PACOTES
    pacotes.mkdir(parents=True, exist_ok=True)
    livre = (espaco_livre or (lambda p: shutil.disk_usage(p).free))(str(pacotes))
    necessario = int(plano["tamanho_total"] * FOLGA_ESPACO)
    if livre < necessario:
        resultado["situacao"] = "bloqueada"
        resultado["mensagem"] = textos.t("py_aplicacao.espaco_insuficiente_no_local_dos", livre // 2**20, necessario // 2**20)
        _passo(resultado, textos.t("py_aplicacao.espaco"), resultado["mensagem"], "bloqueado")
        return
    _passo(resultado, textos.t("py_aplicacao.espaco"), textos.t("py_aplicacao.mb_livres_para_mb_necessarios", livre // 2**20, necessario // 2**20))

    bmc = ler_bmc(ctx)
    prog = prog_mod.de(resultado)
    for item in plano["itens"]:
        prog.etapa(item["componente"], prog_mod.BAIXANDO, textos.t("py_aplicacao.baixando_do_site_da_dell"), 0)
        prog.esperando(textos.t("py_aplicacao.o_download_de", item['componente']), textos.t("py_aplicacao.a_rede_do_servidor_icone"))
        arquivo, detalhe = _baixar_e_conferir(executor, item, pacotes, monitor, avisar, prog)
        prog.etapa(item["componente"], prog_mod.CONFERIDO if arquivo else prog_mod.FALHOU, detalhe)
        anterior = _rollback(item, bmc)
        registro = {"id": item["id"], "componente": item["componente"], "instalada": item.get("instalada"),
                    "disponivel": item.get("disponivel"), "component_id": item.get("component_id"),
                    "url": (item.get("pacote") or {}).get("url"), "release": (item.get("pacote") or {}).get("release"),
                    "arquivo": str(arquivo) if arquivo else None, "hash": detalhe,
                    "reinicio": bool((item.get("pacote") or {}).get("reinicio")),
                    "rollback": anterior, "situacao": "pronto" if arquivo else "abortado",
                    "categoria": (item.get("pacote") or {}).get("categoria") or "",
                    "idrac": eh_idrac({"categoria": (item.get("pacote") or {}).get("categoria"), "componente": item.get("componente")}),
                    "hash_conferido": list(item.pop("_hash_conferido", ("", "")))}  # fmt: skip
        resultado["itens"].append(registro)
        _passo(resultado, "download", f"{item['componente']}: {detalhe}", "ok" if arquivo else "falhou")
        if arquivo is None:
            resultado["situacao"], resultado["mensagem"] = "falhou", f"{item['componente']}: {detalhe}"
            return  # um por vez: nada depois de um item que falhou
    if not bmc["redfish"] or not bmc["multipart"]:
        motivo = bmc["motivo"] or (textos.t("py_aplicacao.o_bmc_nao_anuncia_multiparthttppushuri"))  # fmt: skip
        _passo(resultado, "BMC", motivo, "aviso")
    else:
        _passo(resultado, "BMC", textos.t("py_aplicacao.updateservice_aceita_envio_em_boot", bmc['multipart'])
                                 + ("sim" if bmc["boot_usb"] else textos.t("py_aplicacao.pelo_caminho_uefi_do_pendrive") if bmc.get("boot_uefi_usb")
                                    else textos.t("py_aplicacao.nao_anunciado_depois_do_reinicio")))  # fmt: skip
    for registro in resultado["itens"]:
        texto = textos.t("py_aplicacao.rollback_disponivel_no_bmc_versao", registro['rollback']) if registro["rollback"] else \
            textos.t("py_aplicacao.sem_versao_anterior_no_bmc")  # fmt: skip
        _passo(resultado, "rollback", f"{registro['componente']}: {texto}", "ok" if registro["rollback"] else "aviso")

    if modo != MODO_APLICAR:
        _descrever_simulacao(resultado, bmc)
        return
    from msscan.atualizacao.automatica import aplicar_automatico
    from msscan.atualizacao.catalogo import catalogo_em_cache

    # CLAUDE.md 18.2/18.3: depois do ATUALIZAR, tudo automático (acesso, caminhos a–g, reinícios, restauração).
    resultado["system_id"] = plano.get("system_id")
    aplicar_automatico(ctx, executor, raiz, resultado, dormir, modo, catalogo_em_cache(str(raiz)), monitor, avisar)


def _descrever_simulacao(resultado: dict[str, Any], bmc: dict[str, Any]) -> None:
    """O que SERIA enviado ao BMC, pedido por pedido. Nada é gravado."""
    if bmc.get("exportacao"):
        _passo(resultado, "exportar (simulado)", textos.t("py_aplicacao.post_configuracao_atual_do_idrac", bmc['exportacao']), "simulado")
    else:
        _passo(resultado, "exportar (simulado)", textos.t("py_aplicacao.exportacao_da_configuracao_nao_anunciada"), "aviso")  # fmt: skip
    for registro in resultado["itens"]:
        destino = bmc.get("multipart") or "<MultipartHttpPushUri>"
        _passo(resultado, "enviar (simulado)", textos.t("py_aplicacao.post_com_updateparameters_e_o", registro['componente'], registro['instalada'], registro['disponivel'], destino, json.dumps(PARAMETROS_UPDATE), Path(registro['arquivo']).name),
               "simulado")  # fmt: skip
        if registro["reinicio"]:
            _passo(resultado, textos.t("py_aplicacao.boot_unico_simulado"), textos.t("py_aplicacao.patch_estado_salvo_em_reinicio", bmc.get('sistema') or '/redfish/v1/Systems/<id>', json.dumps(CORPO_BOOT_USB), ARQUIVO_ESTADO), "simulado")  # fmt: skip
            _passo(resultado, "retomada (simulado)", textos.t("py_aplicacao.na_volta_pelo_pendrive_conferir"), "simulado")
    resultado["situacao"] = "simulada"
    resultado["mensagem"] = textos.t("py_aplicacao.simulacao_concluida_item_ns_baixado", len(resultado['itens']))


def _apagar_estado(caminho: Path) -> None:
    """Tira o estado "enviando" quando o BMC não ficou com nada (recusa, trava) ou já aplicou sem reinício."""
    try:
        caminho.unlink()
    except OSError:
        pass


def salvar_estado(raiz: Path, conteudo: dict[str, Any]) -> Path:
    """``estado/atualizacao_pendente.json`` (gravação atômica). Quem chama abre a janela de escrita do pendrive."""
    destino = raiz / ARQUIVO_ESTADO
    destino.parent.mkdir(parents=True, exist_ok=True)
    temporario = destino.with_suffix(".tmp")
    temporario.write_text(json.dumps(conteudo, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporario, destino)
    return destino


