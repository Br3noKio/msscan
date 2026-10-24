#!/usr/bin/env python3
"""Ponto de entrada do MSscan.

Fluxo atual: boas-vindas → local dos dados (e forma de gravação do pendrive) → rede obrigatória → detecção →
diagnóstico → relatório (terminal + JSON/HTML/TXT no pendrive).
Modo ``--ui``: o mesmo fluxo acompanhado por uma interface gráfica local (127.0.0.1).
Modo ``--coletar``: grava as saídas brutas num ``.tar.gz`` (formato de fixture); não exige rede.
Depois do relatório, tudo opcional (padrão NÃO): IA → ações corretivas → atualizações de firmware.
"""

from __future__ import annotations

import argparse
import getpass
import json
import textwrap
import os
import signal
import sys
from datetime import datetime
from pathlib import Path

from msscan import __version__
from msscan import textos
from msscan.core.gravando import em_gravacao, gravando
from msscan.coleta.coletor import coletar
from msscan.core.executor import Executor, ExecutorReal, ExecutorReplay
from msscan.deteccao.detectores import detectar
from msscan.acesso_idrac import diagnosticar as diagnosticar_acesso, eh_dell, formatar as formatar_acesso_idrac
from msscan.deteccao.resumo import formatar_resumo
from msscan.interface.servidor import PORTA_PADRAO, ServidorUI
from msscan.interface import fetch
from msscan.interface.terminal import Terminal
from msscan.parsers.procfs import parametro_boot, parse_cmdline
from msscan.interface.sessao import Sessao
from msscan.modulos.refazer import preparar_releitura
from msscan.modulos.registro import registro_padrao
from msscan.parsers import ErroParser
from msscan.pendrive.local import LocalDados, escrita, local_manual, localizar_dados
from msscan.rede.proxy import carregar_proxy, mascarar
from msscan.rede.monitor import ESPERA_MAXIMA_S, MonitorRede, esperar_rede, texto_status
from msscan.rede.verificacao import ResultadoRede, aguardar_rede, modo_sem_rede
from msscan.atualizacao.aplicacao import (
    CONFIRMACAO,
    CONFIRMACAO_REDUNDANCIA,
    MODO_APLICAR,
    MODO_SIMULAR,
    conferir_confirmacoes,
    executar_plano,
    montar_plano,
)
from msscan.acoes.comum import no_pendrive
from msscan.atualizacao import aplicacao_manual, manual
from msscan.atualizacao.catalogo import catalogo_em_cache
from msscan.atualizacao.aplicacao import ler_bmc
from msscan.atualizacao.aviso import aviso_catalogo, aviso_manual, conferir_aviso, conferir_hardware
from msscan.atualizacao.automatica import continuar, pode_continuar
from msscan.atualizacao.inventario import coletar_inventario
from msscan.atualizacao.retomada import arquivar_estado, conferir, ler_estado
from msscan.atualizacao.verificacao import disponibilidade_atualizacoes, executar_verificacao, prechecagens
from msscan.acoes import execucao as acoes_execucao
from msscan.acoes.catalogo import CATALOGO as CATALOGO_ACOES, recomendadas
from msscan.acoes.comum import CONFIRMACAO as CONFIRMACAO_ACAO, CONFIRMACAO_DESFAZER
from msscan.core.executor import MOTIVO_TRAVA, MOTIVO_TRAVA_ACOES
from msscan.ia.analise import AVISO_TREINAMENTO, analisar_com_rede, disponibilidade, motivo_contexto
from msscan.ia.catalogo import carregar_catalogo
from msscan.ia.config import criar_modelo_se_faltar
from msscan.ia.estado import EstadoIA, indicador
from msscan.ia.provedores import carregar_provedores, resolver_configuracao, resumo_falhas
from msscan.ia.sanitizar import nomes_do_host, preparar_envio
from msscan.redfish.acesso import perguntar_credenciais
from msscan.tela.resolucao import ControleTela
from msscan.relatorio.bmc import formatar_acesso_bmc
from msscan.relatorio.comparacao import comparar, formatar_comparacao
from msscan.relatorio.completo import Gravacao, gravar_relatorios, montar_relatorio
from msscan.ia.chat import SUGESTOES, Conversa
from msscan.relatorio.acoes import formatar_acoes
from msscan.relatorio.chat import formatar_chat, formatar_mensagem
from msscan.relatorio.atualizacoes import formatar_aplicacao, formatar_atualizacoes, formatar_retomada
from msscan.relatorio.ia import formatar_analise_ia, secao_ia
from msscan.relatorio.simbolos import Simbolos, escolher_simbolos
from msscan.relatorio.terminal import formatar_configuracao, formatar_localizacao, formatar_relatorio


def criar_parser() -> argparse.ArgumentParser:
    """Argumentos de linha de comando."""
    parser = argparse.ArgumentParser(
        prog="msscan",
        description=textos.t("py_main.diagnostico_de_servidores_fisicos_somente"),
    )
    parser.add_argument(
        "--replay",
        metavar="PASTA",
        help=textos.t("py_main.nao_executa_nada_usa_as"),
    )
    parser.add_argument(
        "--coletar",
        action="store_true",
        help=textos.t("py_main.apenas_coleta_as_saidas_brutas"),
    )
    parser.add_argument(
        "--saida",
        metavar="PASTA",
        help=textos.t("py_main.onde_gravar_o_tar_gz"),
    )
    parser.add_argument(
        "--dados",
        metavar="PASTA",
        help=(
            textos.t("py_main.pasta_de_dados_config_vendor")
        ),
    )
    parser.add_argument(
        "--simbolos",
        choices=["auto", "unicode", "console", "ascii"],
        default="auto",
        help=textos.t("py_main.simbolos_de_status_auto_console"),
    )
    parser.add_argument(
        "--ui",
        action="store_true",
        help=textos.t("py_main.interface_grafica_servidor_local_em"),
    )
    parser.add_argument(
        "--ui-porta",
        type=int,
        default=PORTA_PADRAO,
        metavar="PORTA",
        help=textos.t("py_main.porta_da_interface_grafica_em", PORTA_PADRAO),
    )
    parser.add_argument(
        "--ui-arquivo-url",
        metavar="ARQUIVO",
        help=textos.t("py_main.grava_o_endereco_da_interface"),
    )
    parser.add_argument(
        "--ui-arquivo-pronto",
        metavar="ARQUIVO",
        help=textos.t("py_main.cria_este_arquivo_quando_o"),
    )
    parser.add_argument(
        "--sem-ia",
        action="store_true",
        help=textos.t("py_main.nao_oferece_a_analise_por"),
    )
    parser.add_argument(
        "--sem-credenciais",
        action="store_true",
        help=textos.t("py_main.nao_pergunta_as_credenciais_do"),
    )
    parser.add_argument(
        "--ui-firewall-pareamento",
        action="store_true",
        help=textos.t("py_main.iso_abre_a_porta_do"),
    )
    parser.add_argument(
        "--ui-navegador",
        action="store_true",
        help=textos.t("py_main.iso_navegador_integrado_para_baixar"),
    )
    parser.add_argument(
        "--ui-arquivo-tela",
        metavar="ARQUIVO",
        help=textos.t("py_main.resultado_do_ajuste_automatico_de"),
    )
    modo = parser.add_mutually_exclusive_group()
    modo.add_argument(
        "--simular",
        action="store_true",
        help=textos.t("py_main.opcao_interna_de_desenvolvimento_testes"),
    )
    modo.add_argument(
        "--aplicar",
        action="store_true",
        help=textos.t("py_main.aplicar_firmware_e_acoes_corretivas"),
    )
    parser.add_argument("--versao", action="version", version=f"MSscan {__version__}")
    return parser


def criar_executor(args: argparse.Namespace) -> Executor:
    """Executor real ou de replay, conforme os argumentos."""
    if args.replay:
        # Na interface gráfica o replay respeita as durações gravadas (demonstração realista).
        return ExecutorReplay(args.replay, tempo_real=bool(args.ui))
    return ExecutorReal()


def executar(args: argparse.Namespace) -> int:
    """Fluxo principal. Devolve o código de saída do processo."""
    print(textos.t("py_main.msscan_diagnostico_de_servidores_somente", __version__))
    try:
        executor = criar_executor(args)
    except FileNotFoundError as erro:
        print(f"Erro: {erro}")
        return 2
    if args.replay:
        print(textos.t("py_main.modo_replay_usando_saidas_gravadas", args.replay))
    elif hasattr(os, "geteuid") and os.geteuid() != 0:
        print(textos.t("py_main.aviso_sem_privilegios_de_root"))

    local = etapa_dados(args, executor)
    pasta_dados = local.pasta if local else None
    pendrive = local.para_dict() if local else None
    if args.coletar:
        return executar_coleta(args, executor, local)
    if args.ui:
        return executar_ui(args, executor, local)

    inicio = datetime.now()
    simbolos = escolher_simbolos(args.simbolos)
    rede = etapa_rede(args, executor, simbolos, pasta_dados)
    monitor = iniciar_monitor_texto(args, executor, rede)
    print(linha_estado_ia(args, rede, pasta_dados))

    print(textos.t("py_main.detectando_capacidades_do_equipamento"))
    ctx = detectar(executor, pasta_dados, pendrive)
    resumo = formatar_resumo(ctx, simbolos)
    print(resumo)
    if eh_dell(ctx):  # caixa "Acesso ao iDRAC" (CLAUDE.md 19.2), também no modo texto
        if not args.replay and rede is not None and monitor is not None:  # iDRAC Tools em segundo plano (CLAUDE.md 20)
            from msscan.deteccao.idrac_tools import iniciar_download

            # 28.1: sem internet agora, baixa quando ela chegar (o monitor avisa).
            monitor.quando_voltar(lambda: iniciar_download(ctx, lambda: escrita(executor, local), monitor))
        print("\n" + formatar_acesso_idrac(diagnosticar_acesso(ctx)))
    try:
        desfazer_temporarias(args, ctx, local)  # sobra de uma sessão que fechou sem apagar (CLAUDE.md 17.4)
        etapa_credenciais_bmc(args, ctx)
        retomada = etapa_retomada(executor, ctx, local, modo_da_linha_de_comando(args))

        print(textos.t("py_main.diagnosticando_componentes"))
        registro = registro_padrao()
        resultados = registro.diagnosticar(ctx)
        relatorio = montar_relatorio(ctx, resultados, rede, executor, inicio, datetime.now(), registro.titulo)
        texto = formatar_relatorio(resultados, titulo_de=registro.titulo, simbolos=simbolos)
        texto += "\n" + formatar_configuracao(relatorio.get("configuracao"))
        texto += "\n" + formatar_localizacao(relatorio.get("localizacao"))
        texto += "\n" + formatar_acesso_bmc(relatorio["bmc"]) if relatorio["bmc"] else ""
        estado_continuar = (retomada or {}).pop("_estado", None)
        if retomada:
            relatorio["retomada"] = retomada
            texto += "\n" + formatar_retomada(retomada)
        print(texto)
        gravacao = etapa_salvar(args, executor, relatorio, resumo + "\n" + texto, local)
        if estado_continuar is not None:
            etapa_continuar(args, executor, ctx, relatorio, {"_estado": estado_continuar}, resumo + "\n" + texto, local, gravacao)
        rede = rede_atual(rede, monitor)  # 28.1: a internet pode ter chegado depois do início
        texto_completo = etapa_ia(args, executor, relatorio, resumo + "\n" + texto, local, rede, gravacao, monitor=monitor)
        texto_completo = etapa_acoes(args, executor, ctx, relatorio, texto_completo, local, gravacao)
        rede = rede_atual(rede, monitor)
        etapa_atualizacoes(args, executor, ctx, relatorio, texto_completo, local, rede, gravacao, monitor=monitor)
        etapa_refazer(args, executor, ctx, registro, rede, resultados, relatorio, resumo, local, gravacao, simbolos)
    finally:
        if monitor is not None:
            monitor.parar()
        desfazer_temporarias(args, ctx, local)
        encerrar_bmc(ctx)
    return 0


def etapa_refazer(
    args: argparse.Namespace,
    executor: Executor,
    ctx,
    registro,
    rede: ResultadoRede,
    resultados: list,
    relatorio: dict,
    resumo: str,
    local: LocalDados | None,
    gravacao: Gravacao,
    simbolos: Simbolos,
    entrada=input,
) -> None:
    """Refazer diagnóstico (CLAUDE.md 7b): tudo ou um componente, somente leitura, com comparação antes × depois.

    Pergunta opcional (padrão NÃO), repetível. Cada vez grava um relatório novo; o anterior fica guardado.
    """
    componentes = registro.componentes()
    while sim_ou_nao(textos.t("py_main.refazer_o_diagnostico_ex_depois"), entrada):
        print("  0) Tudo")
        for i, comp in enumerate(componentes, 1):
            print(textos.t("py_main.so", i, comp.titulo))
        try:
            escolha = entrada(textos.t("py_main.numero_enter_nada")).strip()
        except (EOFError, KeyboardInterrupt, OSError):
            print()
            return
        if not escolha.isdigit() or int(escolha) > len(componentes):
            print(textos.t("py_main.nada_refeito"))
            continue
        somente = None if escolha == "0" else (componentes[int(escolha) - 1].nome,)
        print(textos.t("py_main.refazendo_o_diagnostico_somente_leitura"))
        inicio = datetime.now()
        preparar_releitura(ctx, somente)
        novos = {r.componente: r for r in registro.diagnosticar(ctx, somente=somente)}
        resultados = [novos.get(r.componente, r) for r in resultados]
        novo = montar_relatorio(ctx, resultados, rede, executor, inicio, datetime.now(), registro.titulo)
        novo["comparacao"] = comparar(relatorio, novo, list(somente) if somente else None,
                                      str(gravacao.pasta) if gravacao.pasta else None)  # fmt: skip
        texto = formatar_relatorio(resultados, titulo_de=registro.titulo, simbolos=simbolos)
        texto += "\n" + formatar_configuracao(novo.get("configuracao"))
        texto += "\n" + formatar_localizacao(novo.get("localizacao"))
        texto += "\n" + formatar_acesso_bmc(novo["bmc"]) if novo["bmc"] else ""
        texto += "\n" + formatar_comparacao(novo["comparacao"])
        print(texto)
        print(textos.t("py_main.a_analise_por_ia_e"))
        gravacao = etapa_salvar(args, executor, novo, resumo + "\n" + texto, local)
        relatorio = novo


def etapa_credenciais_bmc(args: argparse.Namespace, ctx) -> None:
    """Credenciais do BMC: só com Redfish confirmado; digitadas na hora; "Pular" sempre possível."""
    if ctx.bmc is None or ctx.bmc.cliente is None or args.replay or args.sem_credenciais:
        if ctx.bmc is not None and ctx.bmc.cliente is not None:
            ctx.bmc.pular()
        return
    perguntar_credenciais(ctx.bmc)


def desfazer_temporarias(args: argparse.Namespace, ctx, local: LocalDados | None) -> None:
    """Desfaz as mudanças temporárias no iDRAC (CLAUDE.md 18.4): as desta sessão e as que ficaram de uma que fechou."""
    from msscan.acoes import idrac
    from msscan.atualizacao import temporarias as temp

    pasta = local.pasta if local else None
    if args.replay or not (ctx.executor.slots_temporarios or idrac.ha_marcador(pasta) or temp.ha_pendentes(pasta)):
        return
    with escrita(ctx.executor, local):
        mudancas = temp.desfazer_tudo(ctx, ctx.executor, pasta)
        if ctx.executor.slots_temporarios or idrac.ha_marcador(pasta):
            idrac.apagar(ctx, ctx.executor, pasta)
    for mudanca in mudancas:
        print(textos.t("py_main.mudanca_temporaria_no_idrac", mudanca['descricao'], mudanca['texto']))


def encerrar_bmc(ctx) -> None:
    """Logout da sessão Redfish, para não ocupar o limite de sessões do BMC."""
    if ctx.bmc is not None and ctx.bmc.encerrar():
        print(textos.t("py_main.sessao_redfish_encerrada_logout"))


def etapa_dados(args: argparse.Namespace, executor: Executor) -> LocalDados | None:
    """Local dos dados: o indicado em --dados ou o melhor local gravável (ver ``localizar_dados``)."""
    if args.dados:
        return local_manual(args.dados)
    if args.replay:
        return None
    local = localizar_dados(executor)
    print(textos.t("py_main.pendrive_boot", local.gravacao.descricao, local.gravacao.descricao_boot))
    print(f"Dados: {local.mensagem}")
    if local.aviso:
        print(f"  {local.aviso}")
    etapa_modelo_ia(executor, local)
    return local


def etapa_modelo_ia(executor: Executor, local: LocalDados) -> None:
    """Primeira execução: cria ``config/ia.conf`` comentado (para o Bloco de Notas). Nunca sobrescreve."""
    if not local.persistente or (Path(local.pasta) / "config" / "ia.conf").exists():
        return
    with escrita(executor, local):
        if criar_modelo_se_faltar(local.pasta, carregar_catalogo(local.pasta)):
            print(textos.t("py_main.ia_opcional_criado_config_ia"))


def iniciar_monitor_texto(args: argparse.Namespace, executor: Executor, rede: ResultadoRede) -> MonitorRede | None:
    """Rede contínua no modo texto (CLAUDE.md 5b): uma linha de status a cada mudança (caiu / voltou). Sem internet no
    início (28.1), começa em "Reconectando..." e avisa quando ela chegar."""
    if args.replay or rede.sem_rede_por_parametro:
        return None

    def linha_status(estado: dict) -> None:
        print(f"\n[Rede] {texto_status(estado)}" + ("" if estado["situacao"] == "ok" else
              " - o diagnóstico local continua; IA e downloads esperam a rede voltar."), flush=True)  # fmt: skip

    monitor = MonitorRede(executor, ao_mudar=linha_status, inicial=rede)
    monitor.iniciar()
    return monitor


def rede_atual(rede: ResultadoRede, monitor: MonitorRede | None) -> ResultadoRede:
    """Começou sem internet e ela chegou (monitor): vale a verificação nova (IA e atualizações passam a ser oferecidas)."""
    if not rede.ok and monitor is not None and monitor.ok and monitor.ultimo is not None:
        return monitor.ultimo
    return rede


def linha_estado_ia(args: argparse.Namespace, rede: ResultadoRede, pasta_dados: str | None) -> str:
    """Indicador de estado da IA (o mesmo da tela inicial da interface), em uma linha para o modo texto."""
    motivo = motivo_contexto(rede.ok, rede.sem_rede_por_parametro, args.sem_ia, bool(args.replay))
    configurados, _ = resolver_configuracao(pasta_dados)
    estado = indicador(configurados, EstadoIA(), motivo)
    return f"{estado['texto']}: {estado['detalhe']}"


def etapa_salvar(
    args: argparse.Namespace,
    executor: Executor,
    relatorio: dict,
    texto: str,
    local: LocalDados | None,
    pasta: Path | None = None,
) -> Gravacao:
    """Grava JSON, HTML e TXT; avisa com clareza quando não foi no pendrive. ``pasta``: regrava nela."""
    with escrita(executor, local):
        gravacao = gravar_relatorios(
            relatorio, texto, local.pasta if local else None, bool(args.replay),
            persistente=bool(local and local.persistente), pasta=pasta,
        )  # fmt: skip
    print("\n" + gravacao.mensagem)
    return gravacao


ROTULO_HARDWARE = {"ok": "OK", "aviso": textos.t("py_main.atencao"), "redundancia": textos.t("py_main.redundancia_perdida"), "historico": "EVENTO ANTIGO",
                   "bloqueia": "BLOQUEIA"}


def mostrar_aviso_texto(aviso: dict, entrada=input) -> bool:
    """Tela "Plano de atualização" no modo texto (CLAUDE.md 18.2). ``True`` só com o plano sem bloqueio (e, se houver
    evento crítico antigo no log do BMC, com "Estou ciente"). O ``ATUALIZAR`` vem logo depois; daí em diante, tudo é
    automático."""
    print("\n" + "=" * 78 + textos.t("py_main.plano_de_atualizacao") + "=" * 78)
    print(textos.t("py_main.o_que_sera_instalado_nesta"))
    for numero, item in enumerate(aviso["itens"], 1):
        print(f"  {numero}) {item['componente']}: {item['atual'] or '?'} -> {item['nova'] or '?'}" + (f" ({item['data']})" if item["data"] else ""))
        for linha in (f"origem: {item['origem']}", f"hash: {item['hash']}", item["rollback"],
                      textos.t("py_main.exige_reinicio") if item["reinicio"] else textos.t("py_main.sem_reinicio_do_servidor")):  # fmt: skip
            print(textwrap.fill(linha, 98, initial_indent="     ", subsequent_indent="       "))
    estimativa = aviso["estimativa"]
    print(textos.t("py_main.tempo_estimado_cerca_de_min", estimativa['minutos'], estimativa['reinicios']))
    liberacao = aviso.get("liberacao") or {}
    if liberacao.get("restrita"):
        print(textos.t("py_main.liberacao_nesta_fase_gravacao_real"))
        print(textwrap.fill(f"[LIBERADO] {liberacao['texto_liberado']}", 98, initial_indent="  ", subsequent_indent="      "))
        print(textwrap.fill(f"[BLOQUEADO] {liberacao['texto_bloqueado']}", 98, initial_indent="  ", subsequent_indent="      "))
        for componente in liberacao.get("bloqueados") or []:
            print(textos.t("py_main.bloqueado_agora", componente))
    print(textos.t("py_main.hardware_conferido_agora"))
    for linha in aviso["hardware"]:
        print(textwrap.fill(f"[{ROTULO_HARDWARE[linha['situacao']]}] {linha['item']}: {linha['texto']}", 98,
                            initial_indent="  ", subsequent_indent="      "))  # fmt: skip
    print(textos.t("py_main.mudancas_temporarias_no_idrac_so"))
    for linha in aviso.get("temporarias") or []:
        print(textwrap.fill(linha, 98, initial_indent="  - ", subsequent_indent="    "))
    for extra in aviso.get("avisos_extras") or []:
        print(textwrap.fill(f"Aviso: {extra}", 98, initial_indent="  ", subsequent_indent="      "))
    if not aviso["pode_seguir"]:
        print(textos.t("py_main.instalacao_bloqueada"))
        for bloqueio in aviso["bloqueios"]:
            print(textwrap.fill(bloqueio, 98, initial_indent="  - ", subsequent_indent="    "))
        return False
    print("Avisos:")
    for texto_aviso in aviso["avisos_fixos"]:
        print(textwrap.fill(texto_aviso, 98, initial_indent="  - ", subsequent_indent="    "))
    print(textos.t("py_main.depois_do_atualizar_nada_mais"))
    print(textos.t("py_main.restauracao_das_mudancas_temporarias_e"))
    if aviso.get("historico"):
        # Evento crítico só no histórico do SEL: não bloqueia, mas exige a confirmação extra (pedido de 01/10/2026).
        aviso["ciente"] = sim_ou_nao(f"{aviso['texto_ciente']}? [s/N] ", entrada)
        return aviso["ciente"]
    return True


def sim_ou_nao(pergunta: str, entrada=input) -> bool:
    """Pergunta opcional com "Não" como padrão: só "s"/"sim" confirmam."""
    try:
        return entrada(pergunta).strip().lower() in ("s", "sim")
    except (EOFError, KeyboardInterrupt, OSError):
        print()
        return False


def etapa_ia(
    args: argparse.Namespace,
    executor: Executor,
    relatorio: dict,
    texto: str,
    local: LocalDados | None,
    rede: ResultadoRede,
    gravacao: Gravacao,
    entrada=input,
    monitor: MonitorRede | None = None,
) -> str:
    """Análise por IA: opcional (padrão NÃO), com opção de ver o que será enviado. Falha → relatório sem IA.

    Devolve o texto do relatório (com a seção de IA, se houver).
    """
    pasta_dados = local.pasta if local else None
    motivo = disponibilidade(pasta_dados, rede.ok, rede.sem_rede_por_parametro, args.sem_ia, bool(args.replay))
    if motivo:
        print(f"\n{motivo[0].upper()}{motivo[1:]}.")
        return texto
    provedores, avisos = carregar_provedores(pasta_dados)
    print(textos.t("py_main.analise_por_ia_opcional_o"))
    print(textos.t("py_main.service_tag_uuid_asset_tag"))
    print(textos.t("py_main.provedores_na_ordem_de_tentativa") + ", ".join(
        f"{p.nome} ({p.modelo}){', ' + p.papel if p.papel else ''}" for p in provedores))
    for aviso in avisos:
        print(f"  (ignorado) {aviso}")
    print("\n".join(textwrap.wrap("AVISO: " + AVISO_TREINAMENTO, 98)))
    if not sim_ou_nao(textos.t("py_main.enviar_para_analise_por_ia"), entrada):
        return texto
    dados, removidos = preparar_envio(relatorio, nomes_do_host(executor))
    if sim_ou_nao(textos.t("py_main.ver_antes_o_que_sera"), entrada):
        print(json.dumps(dados, ensure_ascii=False, indent=1, default=str))
        print(textos.t("py_main.identificadores_removidos", removidos or textos.t("py_main.nenhum_encontrado")))
        if not sim_ou_nao(textos.t("py_main.confirmar_o_envio_s_n"), entrada):
            return texto
    print(textos.t("py_main.enviando_para_analise_pode_levar"), flush=True)
    while True:
        resultado = analisar_com_rede(executor, provedores, dados, monitor, avisar=lambda t: print(t, flush=True))
        for tentativa in resultado.tentativas:
            print(f"  {tentativa['nome']} ({tentativa['modelo']}): {tentativa['resultado']} {tentativa['detalhe']}".rstrip())
        if resultado.ok:
            break
        motivo = resumo_falhas(resultado.tentativas) if resultado.tentativas else resultado.mensagem
        print(textos.t("py_main.analise_nao_concluida", motivo[:1].upper(), motivo[1:]))
        if len(provedores) == 1:
            print(textos.t("py_main.dica_configure_uma_ia_reserva"))
        if not sim_ou_nao(textos.t("py_main.tentar_novamente_s_n"), entrada):
            break
    if not resultado.ok:
        relatorio["interpretacao_ia"] = {"situacao": "falhou", "motivo": resultado.mensagem, "tentativas": resultado.tentativas}
        print(textos.t("py_main.o_relatorio_segue_sem_a"))
        etapa_salvar(args, executor, relatorio, texto, local, pasta=gravacao.pasta)
        return texto
    relatorio["interpretacao_ia"] = secao_ia(resultado.analise or {}, removidos, resultado.tentativas)
    secao = formatar_analise_ia(relatorio["interpretacao_ia"])
    print(secao)
    etapa_salvar(args, executor, relatorio, texto + "\n" + secao, local, pasta=gravacao.pasta)
    secao += etapa_chat(executor, relatorio, provedores, entrada, monitor)
    if relatorio.get("chat") and len(relatorio["chat"]["mensagens"]) > 1:
        etapa_salvar(args, executor, relatorio, texto + "\n" + secao, local, pasta=gravacao.pasta)
    return texto + "\n" + secao


def etapa_chat(executor: Executor, relatorio: dict, provedores: list, entrada=input, monitor: MonitorRede | None = None) -> str:
    """Chat simples no terminal (CLAUDE.md 10e): opcional, padrão NÃO. Devolve a seção da conversa para o relatório."""
    conversa = Conversa(relatorio, nomes_do_host(executor))
    relatorio["chat"] = conversa.para_relatorio()
    if not sim_ou_nao(textos.t("py_main.conversar_com_a_ia_sobre"), entrada):
        return ""
    print("\n" + "\n".join(formatar_mensagem(conversa.mensagens[0], 98)))
    print(textos.t("py_main.sugestoes") + " | ".join(f"{n}) {t}" for n, t in enumerate(SUGESTOES, 1)))
    print(textos.t("py_main.digite_a_pergunta_ou_o"))
    while True:
        try:
            pergunta = entrada(textos.t("py_main.voce")).strip()
        except (EOFError, KeyboardInterrupt, OSError):
            print()
            break
        if not pergunta:
            break
        if pergunta.isdigit() and 1 <= int(pergunta) <= len(SUGESTOES):
            pergunta = SUGESTOES[int(pergunta) - 1]
        print(textos.t("py_main.ia_digitando"), flush=True)
        resultado = conversa.conversar(executor, provedores, pergunta, monitor, avisar=lambda t: print(t, flush=True))
        tecnico = next(m for m in reversed(conversa.mensagens) if m["papel"] == "tecnico")
        for aviso in tecnico.get("avisos") or []:
            print(textwrap.fill(f"AVISO: {aviso}", 98) + f"\n  Enviado: {tecnico['texto']}")
        if not resultado.ok or resultado.mensagem_ia is None:
            motivo = resumo_falhas(resultado.tentativas) if resultado.tentativas else resultado.mensagem
            print(textos.t("py_main.sem_resposta_da_ia_envie", motivo[:1].upper(), motivo[1:]))
            continue
        print(f"IA ({resultado.mensagem_ia['provedor_titulo']}):")
        print("\n".join(formatar_mensagem(resultado.mensagem_ia, 98)))
    relatorio["chat"] = conversa.para_relatorio()
    return formatar_chat(relatorio["chat"])


def etapa_atualizacoes(
    args: argparse.Namespace,
    executor: Executor,
    ctx,
    relatorio: dict,
    texto: str,
    local: LocalDados | None,
    rede: ResultadoRede,
    gravacao: Gravacao,
    entrada=input,
    monitor: MonitorRede | None = None,
) -> None:
    """Atualizações de firmware SOMENTE INFORMATIVAS (fase 8): opcional, padrão NÃO."""
    motivo = disponibilidade_atualizacoes(rede.ok, rede.sem_rede_por_parametro, bool(args.replay))
    if motivo:
        print(f"\n{motivo[0].upper()}{motivo[1:]}.")
        return
    print(textos.t("py_main.atualizacoes_de_firmware_opcional_somente"))
    print(textos.t("py_main.catalogo_oficial_da_dell_nada"))
    if not sim_ou_nao(textos.t("py_main.verificar_atualizacoes_disponiveis_s_n"), entrada):
        etapa_manual(args, executor, ctx, relatorio, texto, local, gravacao, entrada)
        return
    avisar = lambda texto: print(texto, flush=True)  # noqa: E731
    esperar_rede(monitor, avisar, ESPERA_MAXIMA_S)  # fora da janela de escrita do pendrive
    print(textos.t("py_main.inventariando_o_firmware_e_lendo"), flush=True)
    with escrita(executor, local):
        relatorio["atualizacoes"] = executar_verificacao(ctx, relatorio, executor, local.pasta if local else None,
                                                         monitor=monitor, avisar=avisar)  # fmt: skip
    secao = formatar_atualizacoes(relatorio["atualizacoes"])
    print(secao)
    etapa_salvar(args, executor, relatorio, texto + "\n" + secao, local, pasta=gravacao.pasta)
    etapa_aplicacao(args, executor, ctx, relatorio, texto + "\n" + secao, local, gravacao, entrada, monitor)
    etapa_manual(args, executor, ctx, relatorio, texto + "\n" + secao, local, gravacao, entrada)


def etapa_manual(
    args: argparse.Namespace,
    executor: Executor,
    ctx,
    relatorio: dict,
    texto: str,
    local: LocalDados | None,
    gravacao: Gravacao,
    entrada=input,
) -> None:
    """Atualização manual (fase 11): pacotes que o técnico pôs no pendrive; SHA-256 antes de executar, DUP -c, ATUALIZAR."""
    if not local or not local.pasta:
        return
    pasta = Path(local.pasta) / manual.pasta_destino(ctx.fabricante)
    pacotes = sorted(p for p in (pasta.iterdir() if pasta.is_dir() else []) if p.suffix.lower() in manual.EXTENSOES)
    tag = None
    if (ctx.fabricante or "").lower().startswith("dell"):  # service tag só para a URL da Dell, na tela
        rc, saida, _ = executor.rodar(["dmidecode", "-s", "system-serial-number"], timeout=15)
        tag = saida.strip() if rc == 0 else None
    pagina = manual.url_drivers(ctx.fabricante, ctx.modelo, tag)
    print(textos.t("py_main.atualizacao_manual_ponha_o_pacote", no_pendrive(pasta, local.pasta)))
    if pagina["url"]:
        print(textos.t("py_main.pagina_de_drivers_deste_servidor", pagina['explicacao'], pagina['url']))
    if not pacotes or not sim_ou_nao(textos.t("py_main.pacote_s_na_pasta_usar", len(pacotes)), entrada):
        return
    modo = modo_da_linha_de_comando(args)
    if modo == MODO_APLICAR and not local.persistente:
        print(textos.t("py_main.a_instalacao_exige_o_pendrive"))
        return
    inventario, bmc = coletar_inventario(ctx)[0], ler_bmc(ctx)
    pre = prechecagens(relatorio, ctx.compat)
    for arquivo in pacotes:
        analise = aplicacao_manual.analisar(arquivo, ctx.sku, inventario, bmc, catalogo_em_cache(local.pasta))
        print(textos.t("py_main.instalada_pacote", arquivo.name, analise['instalada'] or '?', analise['disponivel'] or '?'))
        for linha in (analise["motivo_modelo"], analise["motivo_metodo"]):
            if linha:
                print(textwrap.fill(linha, 98, initial_indent="  ", subsequent_indent="  "))
        if analise["etapa"] == "recusado":
            print(f"  RECUSADO: {analise['motivo']}")
            continue
        try:
            sha = entrada(textos.t("py_main.sha_256_da_pagina_da")).strip()
        except (EOFError, KeyboardInterrupt, OSError):
            print()
            return
        if not sha:
            continue
        with escrita(executor, local):
            analise = aplicacao_manual.conferir_sha(analise, sha)
        print(f"  {analise['motivo']}")
        if analise["etapa"] != "checagem":
            continue
        print(textos.t("py_main.checando_o_pacote_dup_c"), flush=True)
        analise = aplicacao_manual.checar(analise, executor)
        print(f"  {analise['motivo']}")
        if analise["etapa"] != "pronto":
            continue
        for bloqueio in pre["bloqueios"]:
            print(f"  BLOQUEIO: {bloqueio}")
        if pre["bloqueios"]:
            continue
        print(textos.t("py_main.conferindo_o_hardware_agora_fontes_2"), flush=True)
        aviso = aviso_manual(ctx, registro_padrao(), analise, bmc, modo == MODO_SIMULAR)
        if not mostrar_aviso_texto(aviso, entrada):
            print(textos.t("py_main.nada_foi_feito_2"))
            return
        pre["redundancia_perdida"] = pre["redundancia_perdida"] or aviso["redundancia"]
        try:
            extra = entrada(textos.t("py_main.redundancia_de_energia_perdida_digite", CONFIRMACAO_REDUNDANCIA)) if pre["redundancia_perdida"] else ""
            confirmacao = entrada(textos.t("py_main.digite_para_instalar_dai_em", CONFIRMACAO))
        except (EOFError, KeyboardInterrupt, OSError):
            print()
            return
        motivo = conferir_aviso(aviso, "manual", [str(analise.get("id"))])  # validade de 10 min (ATUALIZAR demorou?)
        if motivo:
            print(textos.t("py_main.nada_foi_feito", motivo))
            return
        with gravando(textos.t("py_main.firmware_pacote_manual")), escrita(executor, local):
            resultado = aplicacao_manual.instalar(analise, ctx, executor, pre, confirmacao, extra, local.pasta, modo, inventario, bmc)
        relatorio["aplicacao"] = resultado
        secao = formatar_aplicacao(resultado)
        print(secao)
        etapa_salvar(args, executor, relatorio, texto + "\n" + secao, local, pasta=gravacao.pasta)
        return  # um pacote por vez


def pedir_administrador_texto(ctx, entrada=input, motivo: str = "aplicar") -> bool:
    """Pede na hora um usuário ADMINISTRADOR do BMC (senha mascarada, só na memória)."""
    print(textos.t("py_main.o_usuario_do_bmc_nao", motivo))
    try:
        usuario = entrada(textos.t("py_main.usuario_administrador_do_bmc_enter")).strip()
        if not usuario:
            return False
        senha = getpass.getpass(textos.t("py_main.senha_nao_aparece_na_tela"))
    except (EOFError, KeyboardInterrupt, OSError):
        return False
    print("  " + ctx.bmc.tentar(usuario, senha))
    del senha
    return ctx.bmc.administrador is True


def etapa_acoes(
    args: argparse.Namespace,
    executor: Executor,
    ctx,
    relatorio: dict,
    texto: str,
    local: LocalDados | None,
    gravacao: Gravacao,
    entrada=input,
) -> str:
    """Ações corretivas do catálogo fechado (fase 10): opcional, padrão NÃO, nenhuma escolhida, CORRIGIR digitado.

    Devolve o texto do relatório (com a seção de ações, se houver).
    """
    modo = modo_da_linha_de_comando(args)
    sugeridas = recomendadas(relatorio)
    print(textos.t("py_main.acoes_corretivas_opcional_correcoes_do"))
    if not sim_ou_nao(textos.t("py_main.executar_alguma_acao_corretiva_s"), entrada):
        return texto
    if modo == MODO_APLICAR and not executor.acoes_permitidas():
        print(textos.t("py_main.atencao_o_fluxo_segue_ate", MOTIVO_TRAVA_ACOES))
    identificadores = list(CATALOGO_ACOES)
    print(textos.t("py_main.acoes_disponiveis_nenhuma_vem_escolhida"))
    for numero, identificador in enumerate(identificadores, 1):
        acao = CATALOGO_ACOES[identificador]
        marca = textos.t("py_main.recomendada_pela_ia", '; '.join(sugeridas[identificador])) if identificador in sugeridas else ""
        print(f"  {numero}) {acao['titulo']} [{identificador}]{marca}")
        print(textwrap.fill(f"{acao['descricao']} Requer: {acao['requer']}.", 98, initial_indent="     ", subsequent_indent="     "))
    try:
        escolha = entrada(textos.t("py_main.numero_da_acao_enter_nenhuma")).strip()
    except (EOFError, KeyboardInterrupt, OSError):
        print()
        return texto
    numeros = _numeros(escolha, len(identificadores)) if escolha else []
    if not numeros or len(numeros) != 1:
        print(textos.t("py_main.nenhuma_acao_escolhida") if not escolha else textos.t("py_main.escolha_invalida_uma_acao_por"))
        return texto
    identificador = identificadores[numeros[0] - 1]
    parametros: dict = {}
    sugeridos = None
    if CATALOGO_ACOES[identificador]["modulo"] == "ntp":
        sugeridos = acoes_execucao.sugestoes_ntp(executor)
        print(textos.t("py_main.servidor_ntp_escolha_um_numero"))
        for numero, sugestao in enumerate(sugeridos, 1):
            print(f"  {numero}) {sugestao['servidor']} — {sugestao['origem']}")
        try:
            resposta = entrada(textos.t("py_main.servidor_ntp_enter", sugeridos[0]['servidor'])).strip()
        except (EOFError, KeyboardInterrupt, OSError):
            print()
            return texto
        if resposta.isdigit() and 1 <= int(resposta) <= len(sugeridos):
            resposta = sugeridos[int(resposta) - 1]["servidor"]
        parametros["servidores"] = resposta
    if identificador == "idrac.ip_fixo":  # 21.5: IP fixo no iDRAC (testado antes se está livre)
        try:
            for campo, rotulo in (("ip", textos.t("py_main.ip_do_idrac")), ("mascara", textos.t("py_main.mascara")), ("gateway", "Gateway")):
                parametros[campo] = entrada(f"{rotulo}: ").strip()
        except (EOFError, KeyboardInterrupt, OSError):
            print()
            return texto
    print(textos.t("py_main.pre_checagens_so_leitura"), flush=True)
    pre = acoes_execucao.prechecar(identificador, ctx, executor, parametros, persistente=bool(local and local.persistente),
                                   sugeridos=sugeridos)  # fmt: skip
    for campo, valor in (pre.get("antes") or {}).items():
        print(textwrap.fill(f"{campo}: {', '.join(valor) if isinstance(valor, list) else valor}", 98, initial_indent="  ",
                            subsequent_indent="    "))  # fmt: skip
    for aviso in pre["avisos"]:
        print(textwrap.fill(f"AVISO: {aviso}", 98, subsequent_indent="  "))
    for bloqueio in pre["bloqueios"]:
        print(textwrap.fill(f"BLOQUEIO: {bloqueio}", 98, subsequent_indent="  "))
    if not pre["pode_seguir"]:
        print(textos.t("py_main.nada_sera_feito"))
        return texto
    print(textos.t("py_main.o_que_sera_alterado"))
    for linha in pre["previa"]:
        print(textwrap.fill(linha, 98, initial_indent="  - ", subsequent_indent="    "))
    try:
        confirmacao = entrada(textos.t("py_main.digite_para_continuar_qualquer_outra", CONFIRMACAO_ACAO))
    except (EOFError, KeyboardInterrupt, OSError):
        print()
        return texto
    with gravando(textos.t("py_main.acao_no_bmc")), escrita(executor, local):
        resultado = acoes_execucao.executar(pre, confirmacao, ctx, executor, local.pasta if local else None, modo,
                                            pedir_administrador=lambda: pedir_administrador_texto(ctx, entrada, "alterar o BMC"))  # fmt: skip
    if resultado["situacao"] == "cancelada":
        print(textos.t("py_main.nada_foi_feito", resultado['mensagem']))
        return texto
    relatorio.setdefault("acoes", []).append(resultado)
    print(formatar_acoes([resultado]))
    if (resultado.get("desfazer") or {}).get("disponivel"):
        etapa_desfazer_acao(ctx, resultado, modo, local, executor, entrada)
    secao = formatar_acoes(relatorio["acoes"])
    etapa_salvar(args, executor, relatorio, texto + "\n" + secao, local, pasta=gravacao.pasta)
    return texto + "\n" + secao


def etapa_desfazer_acao(ctx, resultado: dict, modo: str, local: LocalDados | None, executor: Executor, entrada=input) -> None:
    """Oferece "Desfazer" (padrão NÃO): volta aos valores guardados no pendrive, com DESFAZER digitado."""
    if not sim_ou_nao(textos.t("py_main.desfazer_esta_alteracao_voltar_aos"), entrada):
        return
    pre = acoes_execucao.prechecar_desfazer(resultado, ctx)
    for aviso in pre["avisos"]:
        print(textwrap.fill(f"AVISO: {aviso}", 98, subsequent_indent="  "))
    for bloqueio in pre["bloqueios"]:
        print(textwrap.fill(f"BLOQUEIO: {bloqueio}", 98, subsequent_indent="  "))
    if not pre["pode_seguir"]:
        print(textos.t("py_main.nada_sera_feito"))
        return
    print(textos.t("py_main.o_que_volta"))
    for linha in pre["previa"]:
        print(textwrap.fill(linha, 98, initial_indent="  - ", subsequent_indent="    "))
    try:
        confirmacao = entrada(textos.t("py_main.digite_para_continuar_qualquer_outra", CONFIRMACAO_DESFAZER))
    except (EOFError, KeyboardInterrupt, OSError):
        print()
        return
    with gravando(textos.t("py_main.acao_no_bmc_desfazer")):
        registro = acoes_execucao.desfazer(pre, resultado, confirmacao, ctx, modo,
                                           pedir_administrador=lambda: pedir_administrador_texto(ctx, entrada, textos.t("py_main.desfazer_a_alteracao")),
                                           executor=ctx.executor, pasta_dados=local.pasta if local else None)  # fmt: skip
    print(f"  Desfazer: {registro['mensagem']}")


def etapa_retomada(executor: Executor, ctx, local: LocalDados | None, modo: str = MODO_APLICAR) -> dict | None:
    """Volta de um reinício de atualização: confere antes × depois e tira o estado de "pendente"."""
    pasta = local.pasta if local else None
    estado = ler_estado(executor, pasta)
    if estado is None:
        return None
    print(textos.t("py_main.atualizacao_de_firmware_pendente_encontrada"))
    inventario, fonte = coletar_inventario(ctx)
    retomada = conferir(estado, inventario, fonte)
    motivo = pode_continuar(estado, ctx, conferencia=retomada) if modo == MODO_APLICAR else textos.t("py_main.modo_interno_de_teste")
    retomada["continua_sozinho"] = not motivo
    retomada["motivo_nao_continua"] = motivo if estado.get("pendentes") else ""
    retomada["anterior"] = estado.get("parcial") or {}
    print(formatar_retomada(retomada))
    if local is not None and local.persistente:
        with escrita(executor, local):
            arquivar_estado(pasta)
    if not motivo:
        retomada["_estado"] = estado  # continua sozinho depois do diagnóstico (tirado antes de gravar o relatório)
    return retomada


def etapa_continuar(args: argparse.Namespace, executor: Executor, ctx, relatorio: dict, retomada: dict | None,
                    texto: str, local: LocalDados | None, gravacao: Gravacao) -> None:  # fmt: skip
    """Plano confirmado antes do reinício: relê o hardware e continua os itens pendentes sem perguntar (18.2)."""
    estado = (retomada or {}).pop("_estado", None)
    if estado is None or local is None or not local.pasta:
        return
    print(textos.t("py_main.continuando_a_atualizacao_confirmada_antes"), flush=True)
    hardware = conferir_hardware(ctx, registro_padrao())
    with gravando("firmware (retomada)"), escrita(executor, local):
        resultado = continuar(ctx, executor, local.pasta, estado, hardware)
    relatorio["aplicacao"] = resultado
    secao = formatar_aplicacao(resultado)
    print(secao)
    etapa_salvar(args, executor, relatorio, texto + "\n" + secao, local, pasta=gravacao.pasta)


def _numeros(texto: str, maximo: int) -> list[int] | None:
    """"1, 3" → [1, 3]; ``None`` se algo não for número válido."""
    partes = [p.strip() for p in texto.replace(";", ",").split(",") if p.strip()]
    if not all(p.isdigit() and 1 <= int(p) <= maximo for p in partes):
        return None
    return [int(p) for p in partes]


def etapa_aplicacao(
    args: argparse.Namespace,
    executor: Executor,
    ctx,
    relatorio: dict,
    texto: str,
    local: LocalDados | None,
    gravacao: Gravacao,
    entrada=input,
    monitor: MonitorRede | None = None,
) -> None:
    """Aplicação de firmware: opcional, padrão NÃO, seleção vazia, plano + ATUALIZAR; depois, tudo automático (18.2)."""
    verificacao = relatorio.get("atualizacoes") or {}
    oferecidos = [i for i in verificacao.get("itens") or [] if i.get("situacao") == "atualizacao"]
    if verificacao.get("situacao") != "verificada" or not oferecidos:
        return
    modo = modo_da_linha_de_comando(args)
    if modo == MODO_APLICAR and local is not None and not local.persistente:
        print(textos.t("py_main.a_instalacao_exige_o_pendrive_2"))
        return
    if not sim_ou_nao(textos.t("py_main.instalar_alguma_atualizacao_grava_firmware"), entrada):
        return
    if modo == MODO_APLICAR and not executor.escrita_permitida():
        print(textos.t("py_main.atencao_o_fluxo_segue_ate_2", MOTIVO_TRAVA))
    print(textos.t("py_main.itens_com_atualizacao_disponivel_nenhum"))
    for numero, item in enumerate(oferecidos, 1):
        pacote = item.get("pacote") or {}
        print(textos.t("py_main.reinicio", numero, item['componente'], item['instalada'], item['disponivel'], pacote.get('categoria') or '?', 'sim' if pacote.get('reinicio') else 'não'))  # fmt: skip
    try:
        escolha = entrada(textos.t("py_main.numeros_dos_itens_separados_por")).strip()
    except (EOFError, KeyboardInterrupt, OSError):
        print()
        return
    numeros = _numeros(escolha, len(oferecidos)) if escolha else []
    if not numeros:
        print(textos.t("py_main.nenhum_item_escolhido") if numeros == [] else textos.t("py_main.escolha_invalida_nada_foi_feito"))
        return
    plano = montar_plano(verificacao, [oferecidos[n - 1]["id"] for n in numeros])
    for motivo in plano["bloqueios"] + plano["recusados"]:
        print(f"  BLOQUEIO: {motivo}")
    if not plano["pode_seguir"]:
        print(textos.t("py_main.nada_sera_feito"))
        return
    print(textos.t("py_main.tamanho_total_dos_pacotes_mb", plano['tamanho_total'] // 2**20))
    print(textos.t("py_main.conferindo_o_hardware_agora_fontes"), flush=True)
    ids = [oferecidos[n - 1]["id"] for n in numeros]
    aviso = aviso_catalogo(ctx, registro_padrao(), plano, ids, ler_bmc(ctx), modo == MODO_SIMULAR)
    if not mostrar_aviso_texto(aviso, entrada):
        print(textos.t("py_main.nada_foi_feito_2"))
        return
    plano["confirmacao_redundancia"] = plano["confirmacao_redundancia"] or aviso["redundancia"]
    confirmacao_extra = ""
    try:
        if plano["confirmacao_redundancia"]:
            print(textos.t("py_main.aviso_redundancia_de_energia_perdida"))
            print(textos.t("py_main.o_firmware_pode_corromper_para"))
            confirmacao_extra = entrada(textos.t("py_main.digite_para_confirmar", CONFIRMACAO_REDUNDANCIA))
        confirmacao = entrada(textos.t("py_main.digite_para_continuar_qualquer_outra", CONFIRMACAO))
    except (EOFError, KeyboardInterrupt, OSError):
        print()
        return
    motivo = conferir_confirmacoes(plano, confirmacao, confirmacao_extra) or conferir_aviso(aviso, "catalogo", ids)
    if motivo:
        print(textos.t("py_main.nada_foi_feito", motivo))
        return

    avisar = lambda texto_aviso: print(texto_aviso, flush=True)  # noqa: E731
    esperar_rede(monitor, avisar, ESPERA_MAXIMA_S)
    print(textos.t("py_main.baixando_e_conferindo_os_pacotes"), flush=True)
    with gravando(textos.t("py_main.firmware_catalogo")), escrita(executor, local):
        resultado = executar_plano(ctx, plano, executor, local.pasta if local else None, modo, monitor=monitor, avisar=avisar)
    relatorio["aplicacao"] = resultado
    secao = formatar_aplicacao(resultado)
    print(secao)
    etapa_salvar(args, executor, relatorio, texto + "\n" + secao, local, pasta=gravacao.pasta)


def etapa_rede(
    args: argparse.Namespace, executor: Executor, simbolos: Simbolos, pasta_dados: str | None
) -> ResultadoRede:
    """Rede: espera o link e o DHCP, acerta o relógio e testa a internet. Sem internet, segue (CLAUDE.md 28.1)."""
    print("\nVerificando a rede...")
    if modo_sem_rede(executor):
        resultado = ResultadoRede(False, sem_rede_por_parametro=True)
        print(textos.t("py_main.parametro_de_boot_msscan_semrede"))
        return resultado
    if pasta_dados and not args.replay:
        try:
            proxy = carregar_proxy(pasta_dados)
        except ErroParser as erro:
            print(textos.t("py_main.aviso_config_proxy_conf_ignorado", erro))
            proxy = None
        if proxy:
            print(textos.t("py_main.proxy_configurado", mascarar(proxy.get('https') or proxy.get('http', ''))))
    resultado = aguardar_rede(
        executor,
        entrada=None if args.replay else input,
        espera_automatica=0 if args.replay else 60,
        simbolos=simbolos,
    )
    if not resultado.ok and args.replay:
        print(textos.t("py_main.modo_replay_seguindo_sem_rede"))
    return resultado


def executar_coleta(args: argparse.Namespace, executor: Executor, local: LocalDados | None) -> int:
    """Modo --coletar: roda detecção e diagnóstico gravando tudo, sem exibir relatório."""
    pasta_dados = local.pasta if local else None
    destino = args.saida or (os.path.join(pasta_dados, "coletas") if pasta_dados else ".")
    print(textos.t("py_main.modo_coleta_executando_somente_comandos"))
    credenciais = None if args.replay or args.sem_credenciais else perguntar_credenciais
    with escrita(executor, local if not args.saida else None):
        arquivo = coletar(destino, base=executor, pasta_dados=pasta_dados, credenciais=credenciais)
    print(textos.t("py_main.coleta_gravada_em", arquivo))
    if local and not local.persistente and not args.saida:
        print(f"  {local.aviso}")
    print(textos.t("py_main.atencao_o_pacote_contem_dados"))
    print(textos.t("py_main.extraia_em_tests_fixtures"))
    return 0


def executar_ui(args: argparse.Namespace, executor: Executor, local: LocalDados | None) -> int:
    """Modo --ui: o mesmo fluxo, acompanhado pelo navegador; roda até Ctrl+C."""
    sessao = Sessao(executor, local.pasta if local else None, replay=bool(args.replay), local=local, sem_ia=args.sem_ia,
                    modo_aplicacao=modo_da_linha_de_comando(args),
                    firewall_pareamento=bool(getattr(args, "ui_firewall_pareamento", False)),
                    navegador=bool(getattr(args, "ui_navegador", False)))  # fmt: skip
    sessao.tela = ControleTela(executor, args.ui_arquivo_tela)
    try:
        # Terminal embutido (15.5): nunca no replay (a demonstração não roda nada neste computador).
        servidor = ServidorUI(sessao, porta=args.ui_porta, terminal=None if args.replay else Terminal(
            __version__, apresentacao=lambda: fetch.montar(sessao.visao_servidor(), fetch.sistema_base()),
            liberado=parametro_boot(parse_cmdline(executor.ler_arquivo("/proc/cmdline") or ""), "terminal") == "1"))
    except OSError as erro:
        print(textos.t("py_main.erro_nao_foi_possivel_abrir", args.ui_porta, erro))
        print(textos.t("py_main.outra_instancia_ja_esta_rodando"))
        return 2
    if args.ui_arquivo_pronto:
        servidor.ao_pronto = lambda: Path(args.ui_arquivo_pronto).touch()
    if args.ui_arquivo_url:
        try:
            gravar_privado(args.ui_arquivo_url, servidor.url + "\n")
        except OSError as erro:
            print(textos.t("py_main.erro_nao_foi_possivel_gravar", args.ui_arquivo_url, erro))
            servidor.server_close()
            return 2
    sessao.iniciar()
    print(textos.t("py_main.interface_grafica_disponivel_somente_neste"))
    print(f"  {servidor.url}")
    print(textos.t("py_main.o_endereco_contem_o_token"), flush=True)
    try:
        servidor.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print(textos.t("py_main.interface_encerrada"))
    finally:
        servidor.server_close()
        if servidor.terminal is not None:
            servidor.terminal.encerrar()
        sessao.encerrar()
    return 0


def modo_da_linha_de_comando(args: argparse.Namespace) -> str:
    """Modo REAL (CLAUDE.md 18.2); ``--simular`` (interno, desenvolvimento) força a simulação em tudo."""
    return MODO_SIMULAR if getattr(args, "simular", False) else MODO_APLICAR


def gravar_privado(caminho: str, texto: str) -> None:
    """Grava um arquivo legível só pelo dono (0600) desde a criação: contém o token da sessão."""
    descritor = os.open(caminho, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descritor, "w", encoding="utf-8") as arquivo:
        arquivo.write(texto)
    os.chmod(caminho, 0o600)


def _sigterm(_sinal: int, _quadro: object) -> None:
    """SIGTERM (desligamento, queda da interface) vira SystemExit: os ``finally`` fazem o logout do BMC.

    Durante uma gravação de firmware ou ação no BMC o sinal é IGNORADO: interromper pode inutilizar o componente.
    """
    if em_gravacao():
        print(textos.t("py_main.aviso_gravacao_de_firmware_em"), flush=True)
        return
    raise SystemExit(143)


def main(argv: list[str] | None = None) -> int:
    """Executa o MSscan sem deixar exceções chegarem ao usuário."""
    args = criar_parser().parse_args(argv)
    try:
        signal.signal(signal.SIGTERM, _sigterm)
    except ValueError:  # fora da thread principal (testes)
        pass
    try:
        return executar(args)
    except KeyboardInterrupt:
        print(textos.t("py_main.interrompido_pelo_usuario"))
        return 130
    except Exception as erro:  # noqa: BLE001 - o programa nunca quebra na frente do técnico
        print(textos.t("py_main.erro_inesperado", type(erro).__name__, erro))
        print(textos.t("py_main.o_diagnostico_foi_interrompido_anote"))
        return 1


if __name__ == "__main__":
    sys.exit(main())
