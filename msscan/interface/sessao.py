"""Sessão da interface gráfica: executa o fluxo do MSscan numa thread e expõe o estado.

A sessão NÃO tem lógica de diagnóstico própria: chama as mesmas funções do modo
texto (``verificar_rede``/``aguardar_rede``, ``detectar``, ``Registro.diagnosticar``,
``montar_relatorio``, ``gravar_relatorios``) e só guarda o andamento para a API.

Fluxo: rede (bloqueio obrigatório) → detecção (automática) → diagnóstico (quando
o técnico clicar em "Iniciar diagnóstico") → relatório gravado.
"""

from __future__ import annotations

from msscan import textos

import json
import threading
from contextlib import contextmanager
from pathlib import Path
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from typing import Any

from msscan import __version__
from msscan.core.executor import MOTIVO_TRAVA, MOTIVO_TRAVA_ACOES, Executor
from msscan.core.gravando import em_gravacao, gravando
from msscan.core.modelo import Resultado
from msscan.deteccao.contexto import Contexto
from msscan.deteccao.detectores import detectar
from msscan.deteccao.resumo import capacidades, formatar_resumo
from msscan.modulos.refazer import preparar_releitura
from msscan.modulos.registro import Registro, registro_padrao
from msscan.pendrive.local import LocalDados, escrita
from msscan.interface.ssh import AcessoSSH
from msscan.acesso_idrac import diagnosticar as diagnosticar_acesso, eh_dell as _dell, formatar as formatar_acesso_idrac
from msscan.provedores.redfish import COMPONENTES_REDFISH
from msscan.rede.proxy import carregar_proxy
from msscan.redfish.acesso import NAO_PERGUNTADO
from msscan.atualizacao.aplicacao import (
    MODO_APLICAR,
    MODO_SIMULAR,
    baixar_e_verificar,
    conferir_confirmacoes,
    executar_plano,
    ler_bmc,
    montar_plano,
)
from msscan.atualizacao.aviso import assinatura_catalogo, aviso_catalogo, aviso_manual, conferir_aviso, riscos_assumidos
from msscan.atualizacao.inventario import coletar_inventario
from msscan.atualizacao.retomada import arquivar_estado, conferir, ler_estado
from msscan.atualizacao.automatica import pode_continuar, pode_continuar_parado
from msscan.atualizacao.verificacao import disponibilidade_atualizacoes, executar_verificacao
from msscan.ia.analise import AVISO_TREINAMENTO, analisar_com_rede, disponibilidade, motivo_contexto
from msscan.ia.catalogo import AVISOS, carregar_catalogo
from msscan.ia.config import (
    atualizar_texto,
    chave_de,
    remover_chave_do_texto,
    gravar_texto,
    ler_config,
    mascarar,
    normalizar_chave,
    texto_atual_ou_modelo,
)
from msscan.ia.chat import SUGESTOES, Conversa
from msscan.ia.estado import EstadoIA, indicador, testar_chave
from msscan.ia.provedores import Provedor, carregar_provedores, resolver_configuracao, resumo_falhas
from msscan.parsers import ErroParser
from msscan.qr import gerar as gerar_qr
from msscan.qr import para_linhas
from msscan.ia.sanitizar import nomes_do_host, preparar_envio
from msscan.relatorio.bmc import formatar_acesso_bmc
from msscan.relatorio.comparacao import comparar, formatar_comparacao
from msscan.acoes import execucao as acoes_execucao
from msscan.acoes.catalogo import CATALOGO as CATALOGO_ACOES, recomendadas
from msscan.acoes.comum import CONFIRMACAO_DESFAZER, conferir_confirmacao as conferir_confirmacao_acao
from msscan.interface.manual import AssistenteManual
from msscan.interface.pareamento import FirewallQuiosque, Pareamento
from msscan.relatorio.acoes import formatar_acoes
from msscan.relatorio.chat import formatar_chat, formatar_pareamentos
from msscan.relatorio.atualizacoes import formatar_aplicacao, formatar_atualizacoes, formatar_jobs_idrac, formatar_retomada
from msscan.relatorio.ia import formatar_analise_ia, secao_ia
from msscan.rede.monitor import ESPERA_MAXIMA_S, MonitorRede, esperar_rede
from msscan.rede.verificacao import (
    ResultadoRede,
    aguardar_rede,
    modo_sem_rede,
    niveis_rede,
    verificar_rede,
)
from msscan.relatorio.completo import gravar_relatorios, montar_relatorio
from msscan.relatorio.html import gerar_html
from msscan.relatorio.simbolos import UNICODE
from msscan.relatorio.terminal import formatar_configuracao, formatar_localizacao, formatar_relatorio, veredito

ESPERA_PRINT_S = 4.0
"""Print da tela depois do fim da instalação: a tela consulta o estado a cada 2 s (no máximo); em 4 s ela já mostra o resultado final."""

INTERVALO_DISCOS_S = 30.0
"""Releitura da aba Discos: o ``smartctl -a -j`` leva ~3 s por disco atrás da PERC (R620)."""
PONTOS_TEMPERATURA_DISCO = 120
"""Uma hora de temperatura por disco a cada 30 s."""
CAMPOS_AO_VIVO_DISCO = ("temperatura", "temperatura_detalhe", "horas_ligado", "ciclos_energia", "autoteste", "autotestes")
"""O que a releitura atualiza; status e saúde continuam os do diagnóstico (refazer o diagnóstico reavalia o disco)."""

# Etapas da sessão (a interface decide a tela pela etapa).
REDE = "rede"
DETECCAO = "deteccao"
PRONTO = "pronto"
DIAGNOSTICO = "diagnostico"
CONCLUIDO = "concluido"
ERRO = "erro"

ESPERA_REDE_S = 30
DESMONTAR_PENDRIVE = "/usr/local/sbin/msscan-pendrive-desligar"
"""Script da ISO: ``sync``, desmonta as partições de dados e deixa o pendrive de boot somente leitura (sem forçar)."""
"""Espera automática por link/DHCP logo após o boot (mesmo valor do modo texto)."""


def _instantaneo(dados: Any) -> Any:
    """Cópia serializável de um dicionário que outra thread pode estar preenchendo (progresso da aplicação)."""
    for _ in range(5):
        try:
            return json.loads(json.dumps(dados, default=_serializavel))
        except RuntimeError:  # mudou durante a cópia: tenta de novo
            continue
    return {"situacao": (dados or {}).get("situacao"), "mensagem": (dados or {}).get("mensagem")}


def ferramentas_idrac(ctx: Any) -> dict[str, Any] | None:
    """iDRAC Tools for Linux (racadm) para o relatório (CLAUDE.md 21.6): situação, versão, pacote e origem. Sem URL de
    download nem caminhos locais além do nome do pacote."""
    info = ((getattr(ctx, "cache", None) or {}).get("idrac_tools") or {}) if ctx is not None else {}
    if not info:
        return None
    versoes = info.get("versoes") if isinstance(info.get("versoes"), dict) else {}
    versao = info.get("versao") or versoes.get("racadm") or ", ".join(f"{k} {v}" for k, v in versoes.items()) or textos.t("py_sessao.nao_informada")
    origem = info.get("origem") or (textos.t("py_sessao.pacote_em_msscan_vendor") if info.get("pacote") else textos.t("py_sessao.nao_informada"))
    dados = {"situacao": info.get("situacao"), "versao": versao, "pacote": info.get("pacote"), "origem": origem,
             "termos": info.get("termos"), "mensagem": info.get("mensagem") or ""}  # fmt: skip
    dados["texto"] = (textos.t("py_sessao.idrac_tools_for_linux_racadm", dados['situacao'] or '?', versao, origem)
                      + (textos.t("py_sessao.pacote_0", dados['pacote']) if dados["pacote"] else "")
                      + (textos.t("py_sessao.termos_da_dell_0", dados['termos']) if dados["termos"] else ""))  # fmt: skip
    return dados


def _serializavel(objeto: Any) -> Any:
    """O ``Progresso`` da atualização (21.1) vai para a tela como o retrato ao vivo; o resto, como texto."""
    para_dict = getattr(objeto, "para_dict", None)
    return para_dict() if callable(para_dict) else str(objeto)


def selecao_desatualizada(verificacao: dict[str, Any], itens: list[str]) -> str:
    """Bug 3a (01/10/2026): caixas marcadas numa lista antiga (diagnóstico refeito, verificação nova ou não concluída)
    viravam "item1: item desconhecido" e "0 min" na tela de aviso. Aqui a seleção é recusada com o que fazer."""
    if verificacao.get("situacao") != "verificada":
        return (textos.t("py_sessao.a_verificacao_de_atualizacoes_nao"))  # fmt: skip
    conhecidos = {i.get("id") for i in verificacao.get("itens") or [] if isinstance(i, dict)}
    if not itens or any(i not in conhecidos for i in itens):
        return textos.t("py_sessao.a_lista_de_atualizacoes_mudou")
    return ""


def _aviso_relogio(relogio: dict[str, Any] | None) -> str:
    """Relógio ajustado em um dia ou mais: aviso no Início para corrigir a data no BIOS (28.1)."""
    from msscan.rede.relogio import AVISAR_ACIMA_S

    if not relogio or not relogio.get("ajustado") or abs(float(relogio.get("diferenca_s") or 0)) < AVISAR_ACIMA_S:
        return ""
    return str(relogio.get("mensagem") or "")


class Sessao:
    """Estado de uma execução do MSscan acompanhada pela interface gráfica.

    Todos os métodos públicos são seguros para chamar de várias threads (o
    servidor HTTP atende cada requisição numa thread).
    """

    def __init__(
        self,
        executor: Executor,
        pasta_dados: str | None,
        replay: bool,
        registro: Registro | None = None,
        espera_rede: float = ESPERA_REDE_S,
        dormir: Callable[[float], None] = time.sleep,
        local: LocalDados | None = None,
        sem_ia: bool = False,
        monitorar_rede: bool | None = None,
        modo_aplicacao: str = MODO_SIMULAR,
        firewall_pareamento: bool = False,
        navegador: bool = False,
    ) -> None:
        self.executor = executor
        self.pasta_dados = pasta_dados
        self.local = local
        self.replay = replay
        self.registro = registro or registro_padrao()
        self._espera_rede = 0 if replay else espera_rede
        self._dormir = dormir
        self._trava = threading.RLock()
        self._thread: threading.Thread | None = None

        self.etapa = REDE
        self.erro = ""
        self.rede_situacao = "verificando"
        self.rede: ResultadoRede | None = None
        self.ctx: Contexto | None = None
        self.inicio = datetime.now()
        self.resultados: dict[str, Resultado] = {}
        self.comandos: dict[str, list[dict[str, Any]]] = {}
        self.atual = ""
        self.relatorio: dict[str, Any] | None = None
        self.gravacao: dict[str, Any] | None = None
        self.pagina_pronta = False
        self.bmc_validando = False
        self.acesso_idrac: dict[str, Any] | None = None
        """Caixa "Acesso ao iDRAC" (CLAUDE.md 19.2): ``acesso_idrac.diagnosticar``; ``None`` fora da Dell."""
        self.acesso_verificando = False
        self.sem_ia = sem_ia
        self.texto_relatorio = ""
        self.ia: dict[str, Any] = {"situacao": "nao_solicitada", "mensagem": ""}
        self.estado_ia = EstadoIA()
        """Última situação de cada IA (Testar e análises reais), para o indicador da tela inicial."""
        self.atualizacoes: dict[str, Any] = {"situacao": "nao_solicitadas", "mensagem": ""}
        self.modo_aplicacao = MODO_APLICAR if modo_aplicacao == MODO_APLICAR else MODO_SIMULAR
        """Padrão real (18.2); só o ``--simular`` interno de desenvolvimento simula (nada disso aparece na interface)."""
        self.aplicacao: dict[str, Any] = {"situacao": "nao_solicitada", "mensagem": ""}
        self.preparo: dict[str, Any] | None = None
        """Etapas 2 e 3 do fluxo guiado (baixar e conferir os escolhidos antes do aviso)."""
        self.aviso: dict[str, Any] | None = None
        """Tela de AVISO (15.6): montado por ``preparar_aviso``, consumido pela instalação."""
        self.retomada: dict[str, Any] | None = None
        self._continuacao: dict[str, Any] | None = None
        """Estado de um plano confirmado que continua sozinho depois do diagnóstico (18.2)."""
        self.temporarias_desfeitas: list[dict[str, Any]] = []
        self.acao_pre: dict[str, Any] | None = None
        self.desfazer_pre: dict[str, Any] | None = None
        """Pré-checagem do "Desfazer" (com ``indice`` da ação em ``relatorio["acoes"]``)."""
        self.ntp_sugestoes: list[dict[str, str]] | None = None
        """Pré-checagem da ação corretiva escolhida (só leitura); a execução exige esta pré-checagem aprovada."""
        self.acao: dict[str, Any] = {"situacao": "nao_solicitada", "mensagem": ""}
        self.monitorar_rede = not replay if monitorar_rede is None else monitorar_rede
        self.monitor: MonitorRede | None = None
        """Rede contínua (CLAUDE.md 5b): começa depois da verificação inicial; fora do replay."""
        self.tela: Any = None
        """``ControleTela`` (resolução pela tela Sobre); ``None`` fora da interface em tela cheia."""
        self._trava_escrita = threading.RLock()
        """Uma janela de escrita no pendrive por vez (chat e pareamento gravam fora da tarefa em segundo plano)."""
        self._janela_aberta = False
        """A thread dona de ``_trava_escrita`` já abriu a janela (o log automático grava dentro da atualização)."""
        self._log_auto: Path | None = None
        self.coleta: dict[str, Any] = {"situacao": "nao_solicitada", "mensagem": ""}
        """Botão "Coletar dados deste servidor" (pedido de 16/10/2026): coleta somente leitura para virar fixture."""
        """Log técnico automático desta sessão (21.7): o mesmo arquivo, regravado a cada mudança de etapa."""
        self.jobs_idrac: dict[str, Any] | None = None
        self._jobs_vistos: dict[str, dict[str, Any]] = {}
        self._jobs_lidos = False
        """Jobs do iDRAC já vistos (id → conteúdo e desde quando sem mudança): detecção de job órfão (09/10/2026)."""
        self.limpeza_jobs: dict[str, Any] | None = None
        """Fila de jobs do iDRAC lida depois do diagnóstico (21.4): o que ficou de antes e o que acontece no reinício."""
        self._parado: dict[str, Any] | None = None
        """Plano parado a pedido (21.2), à espera do botão "Continuar de onde parou"."""
        self._uptime: tuple[float | None, float] | None = None
        """``(/proc/uptime, relógio monotônico)`` lido uma vez: o tempo ligado é contado a partir daí."""
        self._sensores: dict[str, Any] | None = None
        self._sensores_lidos = 0.0
        self._trava_sensores = threading.Lock()  # uma leitura ao vivo por vez (o ipmitool leva ~2 s)
        self._discos_vivo: dict[str, dict[str, Any]] = {}
        """Aba Discos (CLAUDE.md 31): última releitura de cada disco (id → ``parse_smart``) e a hora dela."""
        self._discos_historico: dict[str, list[list[Any]]] = {}
        """id → [[hora, °C], ...] da temperatura ao vivo (no máximo ``PONTOS_TEMPERATURA_DISCO``)."""
        self._discos_lidos = 0.0
        self._discos_lendo = False
        self.firewall_pareamento = firewall_pareamento
        self.ssh = AcessoSSH(self.executor.rodar, pasta_dados, firewall=firewall_pareamento)
        """22.5: acesso SSH/SFTP (desligado por padrão; aba Terminal ou ``msscan.ssh=1``)."""
        self._ssh_conferido = 0.0
        self._trava_ssh = threading.Lock()  # ativar/desativar rodam comandos: fora da trava da tela
        """Na ISO (``--ui-firewall-pareamento``): abre/fecha a porta do pareamento no firewall do quiosque."""
        self.pareamento: Pareamento | None = None
        self._qr_celular: tuple[str, list[str]] = ("", [])
        """QR Code do endereço do pareamento em andamento (gerado uma vez por endereço)."""
        self.pareamentos_ia: list[dict[str, Any]] = []
        """Registro (sem chave) de cada pareamento do celular, para o relatório (CLAUDE.md 10d)."""
        self.conversa: Conversa | None = None
        self.chat: dict[str, Any] = {"situacao": "inativo", "mensagem": "", "revisao": 0}
        self.manual = AssistenteManual(self, navegador)
        """Atualização manual (fase 11): navegador integrado (só na ISO, ``--ui-navegador``) e assistente do pacote."""
        """Chat com a IA (CLAUDE.md 10e): começa quando a análise por IA termina."""

    @contextmanager
    def _escrita(self) -> Iterator[None]:
        """Janela de escrita no pendrive, uma por vez (``escrita()`` remonta o pendrive de boot rw → ro). Reentrante: dentro
        de uma janela já aberta pela mesma thread, não remonta de novo (senão o fim da interna voltaria o pendrive a ro)."""
        with self._trava_escrita:
            if self._janela_aberta:
                yield
                return
            with escrita(self.executor, self.local):
                self._janela_aberta = True
                try:
                    yield
                finally:
                    self._janela_aberta = False

    # Fluxo -----------------------------------------------------------------

    def iniciar(self) -> None:
        """Começa a verificação de rede e, se ela passar, a detecção (em segundo plano)."""
        self._em_segundo_plano(self._fluxo_inicial)

    def tentar_rede_novamente(self) -> bool:
        """Botão "Tentar novamente" (tela de rede e faixa "Sem internet"). ``False`` se não fizer sentido agora."""
        with self._trava:
            fora = self.rede_situacao == "sem_internet" or (self.monitor is not None and not self.monitor.ok)
            if not fora or self.rede_situacao in ("verificando", "desativada") or self.replay:
                return False
            self.rede_situacao = "verificando"
        self._repetir_rede_em_segundo_plano()
        return True

    def _repetir_rede_em_segundo_plano(self) -> None:
        """Thread própria: pode rodar durante o diagnóstico ou a análise (a rede não bloqueia mais, 28.1)."""

        def protegido() -> None:
            try:
                self._repetir_rede()
            except Exception:  # noqa: BLE001 - a verificação de rede nunca derruba a sessão
                with self._trava:
                    self.rede_situacao = "sem_internet"

        threading.Thread(target=protegido, name="msscan-rede-tentar", daemon=True).start()

    def iniciar_diagnostico(self) -> bool:
        """Botão "Iniciar diagnóstico". ``False`` se a detecção não terminou ou já rodou."""
        with self._trava:
            if self.etapa != PRONTO or self._ocupado():
                return False
            self.etapa = DIAGNOSTICO
        self._em_segundo_plano(self._diagnosticar)
        return True

    def aguardar(self, timeout: float | None = None) -> None:
        """Espera a tarefa em segundo plano atual terminar (usado nos testes e no modo demonstração)."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _ocupado(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _em_segundo_plano(self, alvo: Callable[[], None]) -> None:
        def protegido() -> None:
            try:
                alvo()
            except Exception as erro:  # noqa: BLE001 - a interface nunca fica sem resposta
                with self._trava:
                    self.etapa = ERRO
                    self.erro = f"{type(erro).__name__}: {erro}"
                    # Nada fica "executando" para sempre (energia bloqueada sem fim): conta o que se sabe.
                    for campo in ("aplicacao", "acao"):
                        atual = getattr(self, campo) or {}
                        if atual.get("situacao") == "executando":
                            setattr(self, campo, {**atual, "situacao": "falhou", "mensagem": textos.t("py_sessao.erro_inesperado_0_se_algo", self.erro)})  # fmt: skip
            finally:
                # Pedido de 22/10/2026: depois de toda instalação (ou tentativa), o log técnico final fica no pendrive.
                if self.aplicacao is not None:
                    self.log_automatico()
                    espera = threading.Timer(ESPERA_PRINT_S, self.print_tela)  # a tela mostra o resultado final
                    espera.daemon = True
                    espera.start()

        self._thread = threading.Thread(target=protegido, name="msscan-sessao", daemon=True)
        self._thread.start()

    def _fluxo_inicial(self) -> None:
        if modo_sem_rede(self.executor):
            with self._trava:
                self.rede = ResultadoRede(False, sem_rede_por_parametro=True)
                self.rede_situacao = "desativada"
        else:

            def ao_aguardar(texto: str) -> None:
                if "Aguardando" in texto:
                    with self._trava:
                        self.rede_situacao = "aguardando"

            aviso_proxy = self._carregar_proxy()
            resultado = aguardar_rede(
                self.executor, saida=ao_aguardar, entrada=None, dormir=self._dormir, espera_automatica=self._espera_rede
            )
            resultado.detalhes += aviso_proxy
            with self._trava:
                self.rede = resultado
                # 28.1: sem internet o MSscan segue (diagnóstico, iDRAC, relatórios); a faixa mostra o motivo real.
                self.rede_situacao = "ok" if resultado.ok else "falha_replay" if self.replay else "sem_internet"
            self._iniciar_monitor(resultado)
            if resultado.ok:
                self._ssh_pelo_boot()
        self._detectar()

    def _ssh_pelo_boot(self) -> None:
        """22.5: parâmetro de boot ``msscan.ssh=1`` liga o acesso SSH sozinho (a senha aparece na aba Terminal)."""
        from msscan.parsers.procfs import parametro_boot, parse_cmdline

        if not self.replay and parametro_boot(parse_cmdline(self.executor.ler_arquivo("/proc/cmdline") or ""), "ssh") == "1":
            with self._trava_ssh:
                self.ssh.ativar(textos.t("py_sessao.parametro_de_boot_msscan_ssh"))

    def ativar_ssh(self) -> tuple[bool, str]:
        """Botão "Ativar acesso SSH"."""
        if self.replay:
            return False, textos.t("py_sessao.modo_replay_sem_acesso_ssh")
        with self._trava_ssh:
            return self.ssh.ativar()

    def desativar_ssh(self) -> tuple[bool, str]:
        """Botão "Desativar" (faixa "Acesso SSH ativo"); também ao desligar, reiniciar ou encerrar. Registra no relatório."""
        with self._trava_ssh:
            ok, mensagem = self.ssh.desativar()
        relatorio = self.relatorio if self.ssh.historico else None
        if relatorio is not None and not self.replay:
            try:
                with self._escrita():
                    self._regravar(relatorio)
            except Exception:  # noqa: BLE001 - desligar o SSH nunca falha por causa do relatório
                pass
        return ok, mensagem

    def _estado_ssh(self) -> dict[str, Any]:
        if self.ssh.ativo and time.monotonic() - self._ssh_conferido > 15:  # quem conectou (journal), sem pesar a cada 0,5 s
            self._ssh_conferido = time.monotonic()
            self.ssh.atualizar_conexoes()
        return self.ssh.estado()

    def _carregar_proxy(self) -> list[str]:
        """``config/proxy.conf`` relido no início e a cada "Tentar novamente" (28.1: o técnico cria o arquivo com o MSscan
        aberto). Devolve o aviso de arquivo inválido, para os detalhes da rede."""
        if self.replay or not self.pasta_dados:
            return []
        try:
            carregar_proxy(self.pasta_dados)
        except ErroParser as erro:
            return [textos.t("py_main.aviso_config_proxy_conf_ignorado", erro).strip()]
        return []

    def _repetir_rede(self) -> None:
        """"Tentar novamente" (28.1): refaz tudo, inclusive o relógio."""
        aviso_proxy = self._carregar_proxy()
        resultado = verificar_rede(self.executor, forcar_relogio=True)
        resultado.detalhes += aviso_proxy
        with self._trava:
            self.rede = resultado
            self.rede_situacao = "ok" if resultado.ok else "sem_internet"
        if self.monitor is None:
            return
        if resultado.ok:
            self.monitor.verificar_agora(forcar_internet=True)  # a faixa "Sem internet" some na hora
        elif not self.monitor.confirmar_queda():
            self._rede_voltou()  # falha passageira: o monitor confere de novo e a rede está de pé (A4)

    def ajustar_hora(self, texto: str) -> tuple[bool, str]:
        """Data e hora informadas pelo técnico (28.1, nenhuma fonte acertou o relógio). Só o relógio do sistema."""
        from msscan.rede import relogio

        if self.replay:
            return False, textos.t("py_sessao.demonstracao_relogio_nao_alterado")
        acerto = relogio.ajustar_pelo_tecnico(self.executor, texto)
        if not acerto.ok:
            return False, acerto.mensagem
        with self._trava:
            self.rede_situacao = "verificando"
        self._repetir_rede_em_segundo_plano()
        return True, acerto.mensagem or textos.t("py_sessao.relogio_conferido")

    def _iniciar_monitor(self, inicial: ResultadoRede | None = None) -> None:
        """Rede contínua: link, IP e internet em segundo plano, sem travar a interface. Sem internet no início, avisa
        quando ela chegar (e aí baixa o iDRAC Tools, CLAUDE.md 20)."""
        if self.monitorar_rede and self.monitor is None:
            # Toda volta da rede (não só a primeira) atualiza o estado e baixa o iDRAC Tools se faltar (A4).
            self.monitor = MonitorRede(self.executor, inicial=inicial,
                                       ao_mudar=lambda estado: self._rede_voltou() if estado.get("situacao") == "ok" else None)
            if inicial is not None and inicial.ok:
                self.monitor.ultimo = inicial
            self.monitor.iniciar()

    def _rede_voltou(self) -> None:
        with self._trava:
            if self.monitor is not None and self.monitor.ultimo is not None and not (self.rede and self.rede.ok):
                self.rede = self.monitor.ultimo
            if self.rede_situacao == "sem_internet" and self.rede is not None and self.rede.ok:
                self.rede_situacao = "ok"
            ctx = self.ctx
        if ctx is not None:
            self._baixar_idrac_tools(ctx)

    def _detectar(self) -> None:
        with self._trava:
            self.etapa = DETECCAO
        ctx = detectar(self.executor, self.pasta_dados, self.local.para_dict() if self.local else None)
        self._baixar_idrac_tools(ctx)
        acesso = diagnosticar_acesso(ctx) if _dell(ctx) else None
        with self._trava:
            self.ctx = ctx
            self.acesso_idrac = acesso
        if self.monitor is not None and self.monitor.ok:
            self._baixar_idrac_tools(ctx)  # a internet pode ter chegado durante a detecção (A4); não duplica o download
        self.desfazer_temporarias()  # deixado por uma sessão que fechou sem apagar (marcador no pendrive)
        with self._trava:
            self.etapa = PRONTO

    def refazer_diagnostico(self, componente: str | None = None) -> bool:
        """Botão "Refazer diagnóstico" (CLAUDE.md 7b): tudo (``None``) ou um componente, somente leitura.

        O diagnóstico anterior fica guardado (memória e pasta própria no pendrive) e o novo relatório traz a
        comparação antes × depois. ``False`` se não fizer sentido agora ou o componente não existir.
        """
        with self._trava:
            if self.etapa != CONCLUIDO or self._ocupado() or self.relatorio is None:
                return False
            if componente is not None and componente not in {c.nome for c in self.registro.componentes()}:
                return False
            anterior = {"relatorio": self.relatorio, "pasta": self.gravacao["pasta"] if self.gravacao else None}
            self.aviso = None  # hardware relido: aviso antigo não vale mais
            somente = (componente,) if componente is not None else None
            for nome in somente or list(self.resultados):
                self.resultados.pop(nome, None)
                self.comandos.pop(nome, None)
            self.etapa = DIAGNOSTICO
        self._em_segundo_plano(lambda: self._diagnosticar(somente=somente, anterior=anterior))
        return True

    def _diagnosticar(self, somente: tuple[str, ...] | None = None, anterior: dict[str, Any] | None = None) -> None:
        """Diagnóstico completo ou, com ``somente``, só os componentes indicados (itens do Redfish ou refazer).

        ``anterior`` (refazer): relatório e pasta do diagnóstico anterior, para a comparação antes × depois.
        """
        assert self.ctx is not None
        if somente is None and self.ctx.bmc is not None and self.ctx.bmc.situacao == NAO_PERGUNTADO:
            self.ctx.bmc.pular()  # o técnico começou sem informar credenciais: segue pelo fallback
        inicio_comandos = {"n": len(self.executor.historico)}

        def progresso(nome: str, resultado: Resultado | None) -> None:
            with self._trava:
                if resultado is None:
                    self.atual = nome
                    inicio_comandos["n"] = len(self.executor.historico)
                    return
                self.resultados[nome] = resultado
                self.comandos[nome] = [e.para_dict() for e in self.executor.historico[inicio_comandos["n"] :]]
                self.atual = ""

        if somente is None and anterior is None and self.retomada is None:
            self._retomar()
        with self._trava:
            if somente is None or anterior is not None:
                self.inicio = datetime.now()
        if anterior is not None:
            preparar_releitura(self.ctx, somente)
        elif somente is not None:
            self.ctx.cache.pop("redfish", None)
        self.registro.diagnosticar(self.ctx, progresso, somente=somente)
        with self._trava:
            resultados = [self.resultados[c.nome] for c in self.registro.componentes() if c.nome in self.resultados]
        relatorio = montar_relatorio(
            self.ctx, resultados, self.rede, self.executor, self.inicio, datetime.now(), self.registro.titulo
        )
        texto = formatar_resumo(self.ctx, UNICODE) + "\n" + formatar_relatorio(
            resultados, titulo_de=self.registro.titulo, simbolos=UNICODE
        )
        texto += "\n" + formatar_configuracao(relatorio.get("configuracao"))
        texto += "\n" + formatar_localizacao(relatorio.get("localizacao"))
        if relatorio["bmc"]:
            texto += "\n" + formatar_acesso_bmc(relatorio["bmc"])
        with self._trava:
            acesso = self.acesso_idrac
        if acesso:  # 19.2 no relatório (bancada de 16/10/2026: o "porquê" do Redfish sem resposta ficava só na tela)
            relatorio["acesso_idrac"] = acesso
            texto += "\n" + formatar_acesso_idrac(acesso)
        ferramentas = ferramentas_idrac(self.ctx)
        if ferramentas:  # 21.6: versão e origem do iDRAC Tools (racadm) no relatório
            relatorio["idrac_tools"] = ferramentas
            texto += "\n" + ferramentas["texto"]
        if self.retomada:
            relatorio["retomada"] = self.retomada
            texto += "\n" + formatar_retomada(self.retomada)
        if anterior is not None:
            relatorio["comparacao"] = comparar(anterior["relatorio"], relatorio, list(somente) if somente else None,
                                               anterior["pasta"])  # fmt: skip
            texto += "\n" + formatar_comparacao(relatorio["comparacao"])
        with self._trava:
            if self.pareamentos_ia:
                relatorio["pareamentos_ia"] = list(self.pareamentos_ia)
        if somente is not None or anterior is not None:
            # A análise e a verificação anteriores não valem para os dados refeitos: o técnico pode pedir de novo.
            with self._trava:
                if self.ia["situacao"] == "concluida":
                    self.ia = {"situacao": "nao_solicitada", "mensagem": textos.t("py_sessao.diagnostico_refeito_peca_a_analise")}
                if self.conversa is not None:
                    self.conversa = None
                    self.chat = {"situacao": "inativo", "mensagem": textos.t("py_sessao.diagnostico_refeito_peca_a_analise_2"),
                                 "revisao": self.chat["revisao"] + 1}  # fmt: skip
                if anterior is not None and self.atualizacoes["situacao"] != "nao_solicitadas":
                    self.atualizacoes = {"situacao": "nao_solicitadas",
                                         "mensagem": textos.t("py_sessao.diagnostico_refeito_verifique_as_atualizacoes")}  # fmt: skip
        # Credenciais informadas depois: regrava a mesma pasta. Refazer: pasta nova (a anterior fica guardada).
        pasta_anterior = Path(self.gravacao["pasta"]) if somente is not None and anterior is None and self.gravacao and self.gravacao["pasta"] else None
        with self._escrita():
            gravacao = gravar_relatorios(
                relatorio, texto + ("\n" + formatar_pareamentos(relatorio["pareamentos_ia"]) if relatorio.get("pareamentos_ia") else ""),
                self.pasta_dados, self.replay,
                persistente=self.local is None or self.local.persistente, pasta=pasta_anterior,
            )  # fmt: skip
        with self._trava:
            self.relatorio = relatorio
            self.texto_relatorio = texto
            self.gravacao = {
                "pasta": str(gravacao.pasta) if gravacao.pasta else None,
                "no_pendrive": gravacao.no_pendrive,
                "mensagem": gravacao.mensagem,
            }
            self.etapa = CONCLUIDO
        if somente is None and anterior is None:
            self._ler_jobs_idrac()
            self._continuar_automatico()

    # Análise por IA --------------------------------------------------------

    def _motivo_ia(self) -> str:
        if self.relatorio is None or self.etapa != CONCLUIDO:
            return textos.t("py_sessao.a_analise_por_ia_fica")
        rede = self.rede
        return disponibilidade(
            self.pasta_dados, bool(rede and rede.ok), bool(rede and rede.sem_rede_por_parametro), self.sem_ia, self.replay
        )

    def previa_ia(self) -> dict[str, Any] | None:
        """O que será enviado à IA (já sem identificadores). ``None`` se a IA não estiver disponível."""
        with self._trava:
            if self._motivo_ia() or self.relatorio is None:
                return None
            relatorio = self.relatorio
        dados, removidos = preparar_envio(relatorio, nomes_do_host(self.executor))
        return {"dados": dados, "removidos": removidos}

    def solicitar_ia(self) -> bool:
        """Botão "Enviar para análise": em segundo plano. ``False`` se indisponível ou ocupado."""
        with self._trava:
            if self._motivo_ia() or self._ocupado() or self.ia["situacao"] == "enviando":
                return False
            self.ia = {"situacao": "enviando", "mensagem": textos.t("py_sessao.enviando_para_analise_pode_levar")}
            self.executor.ia_cancelada.clear()
            relatorio = self.relatorio
        assert relatorio is not None

        def avisar(texto: str) -> None:
            with self._trava:
                self.ia = {"situacao": "enviando", "mensagem": texto}

        def parcial(texto: str) -> None:  # streaming: o que a IA já escreveu (resumo e títulos)
            with self._trava:
                self.ia = {**self.ia, "parcial": texto[-4000:]}

        def analisar() -> None:
            dados, removidos = preparar_envio(relatorio, nomes_do_host(self.executor))
            resultado = analisar_com_rede(self.executor, carregar_provedores(self.pasta_dados)[0], dados, self.monitor, avisar,
                                          ao_parcial=parcial)  # fmt: skip
            self.estado_ia.registrar_tentativas(resultado.tentativas)
            if not resultado.ok:
                relatorio["interpretacao_ia"] = {"situacao": "falhou", "motivo": resultado.mensagem, "tentativas": resultado.tentativas}
                pasta = Path(self.gravacao["pasta"]) if self.gravacao and self.gravacao["pasta"] else None
                with self._escrita():
                    gravar_relatorios(relatorio, self.texto_relatorio, self.pasta_dados, self.replay,
                                      persistente=self.local is None or self.local.persistente, pasta=pasta)  # fmt: skip
                with self._trava:
                    motivo = resumo_falhas(resultado.tentativas) if resultado.tentativas else resultado.mensagem
                    self.ia = {"situacao": "falhou", "mensagem": textos.t("py_sessao.analise_nao_concluida_0_1", motivo[:1].upper(), motivo[1:])}
                    if self.executor.ia_cancelada.is_set():
                        self.ia = {"situacao": "falhou", "cancelada": True,
                                   "mensagem": textos.t("py_sessao.envio_cancelado_nada_foi_recebido")}  # fmt: skip
                return
            relatorio["interpretacao_ia"] = secao_ia(resultado.analise or {}, removidos, resultado.tentativas)
            # Chat (CLAUDE.md 10e): começa com a mensagem da IA (diagnóstico e soluções por prioridade).
            conversa = Conversa(relatorio, nomes_do_host(self.executor))
            relatorio["chat"] = conversa.para_relatorio()
            with self._escrita():
                self._regravar(relatorio)
            with self._trava:
                self.conversa = conversa
                self.chat = {"situacao": "pronto", "mensagem": "", "revisao": self.chat["revisao"] + 1}
                self.ia = {"situacao": "concluida", "mensagem": textos.t("py_sessao.analise_por_ia_concluida_e")}

        self._em_segundo_plano(analisar)
        return True

    def _contexto_ia(self) -> str:
        rede = self.rede
        return motivo_contexto(bool(rede and rede.ok), bool(rede and rede.sem_rede_por_parametro), self.sem_ia, self.replay)

    def _estado_ia(self) -> dict[str, Any]:
        motivo = self._motivo_ia()
        secao = (self.relatorio or {}).get("interpretacao_ia") or {}
        configurados, _ = resolver_configuracao(self.pasta_dados)
        provedores = [p.descrever() for p in carregar_provedores(self.pasta_dados)[0]] if not motivo else []
        return {
            **self.ia,
            "disponivel": not motivo,
            "motivo": motivo,
            "provedores": provedores,
            "indicador": indicador(configurados, self.estado_ia, self._contexto_ia()),
            "sem_reserva": len(configurados) == 1,
            "aviso_treinamento": AVISO_TREINAMENTO,
            "aviso_treinamento_curto": AVISOS["treinamento"],
            "analise": secao if secao.get("situacao") == "concluida" and self.ia["situacao"] == "concluida" else None,
        }

    # Configuração da IA (CLAUDE.md 10c) -------------------------------------------

    def configuracao_ia(self) -> dict[str, Any]:
        """Tela "Configurar IA": catálogo (gratuitas × pagas), escolhas e chaves mascaradas (4 últimos no máximo)."""
        catalogo = carregar_catalogo(self.pasta_dados)
        config = ler_config(self.pasta_dados)
        configurados, avisos = resolver_configuracao(self.pasta_dados)
        papeis = {p.nome: p.papel for p in configurados if p.papel}
        opcoes = []
        for entrada in catalogo.entradas:
            chave = chave_de(self.pasta_dados, config, entrada) if self.pasta_dados else ""
            opcoes.append({**entrada.para_dict(), "chave": mascarar(chave), "tem_chave": bool(chave),
                           "situacao": self.estado_ia.situacao(entrada.id), "papel": papeis.get(entrada.id, "")})  # fmt: skip
        persistente = self.local is None or self.local.persistente
        local = ""
        if not self.pasta_dados:
            local = textos.t("py_sessao.sem_local_de_dados_modo")
        elif not persistente:
            local = textos.t("py_sessao.os_dados_estao_fora_do")
        return {
            "opcoes": opcoes,
            "principal": next((p.nome for p in configurados if p.papel == "principal"), ""),
            "reserva": next((p.nome for p in configurados if p.papel == "reserva"), ""),
            "formato_antigo": config.legado,
            "arquivo": f"{self.pasta_dados}/config/ia.conf" if self.pasta_dados else "",
            "pode_salvar": bool(self.pasta_dados),
            "aviso_local": local,
            "avisos": avisos,
            "catalogo": catalogo.origem,
            "tipos_de_aviso": AVISOS,
        }

    def _provedor_do_catalogo(self, identificador: str, chave_digitada: str = "") -> Provedor | None:
        catalogo = carregar_catalogo(self.pasta_dados)
        entrada = catalogo.obter(identificador)
        if entrada is None:
            return None
        config = ler_config(self.pasta_dados)
        modelo = (config.secoes.get(identificador, {}).get("modelo") or "").strip() if not config.legado else ""
        chave = normalizar_chave(chave_digitada) or (chave_de(self.pasta_dados, config, entrada) if self.pasta_dados else "")
        modelo = modelo or entrada.modelo_padrao
        return Provedor(identificador, entrada.tipo_api, modelo, entrada.url_chamada(modelo), chave,
                        papel="teste", titulo=entrada.nome, categoria=entrada.categoria)  # fmt: skip

    def testar_ia(self, identificador: str, chave_digitada: str = "") -> dict[str, Any] | None:
        """Botão "Testar": chamada mínima, sem nenhum dado do servidor. ``None`` se o id não existir."""
        provedor = self._provedor_do_catalogo(identificador, chave_digitada)
        if provedor is None:
            return None
        if self.replay:
            return {"situacao": "indisponivel", "mensagem": textos.t("py_sessao.teste_indisponivel_no_modo_replay"), "status": None}
        resultado = testar_chave(self.executor, provedor)
        del provedor
        if resultado.situacao != "sem_chave":
            self.estado_ia.registrar(identificador, resultado.situacao)
        return resultado.para_dict()

    def remover_chave_ia(self, identificador: str) -> tuple[bool, str]:
        """Botão "Remover chave" (17.5): esvazia a chave do provedor no ``config/ia.conf`` e apaga o arquivo ``.key``
        antigo dele (se houver), relendo para conferir. A confirmação é pedida na tela antes."""
        if not self.pasta_dados:
            return False, textos.t("py_sessao.sem_local_de_dados_nao")
        catalogo = carregar_catalogo(self.pasta_dados)
        entrada = catalogo.obter(identificador)
        if entrada is None:
            return False, textos.t("py_sessao.ia_desconhecida")
        config = ler_config(self.pasta_dados)
        arquivo_antigo = normalizar_chave((config.secoes.get(identificador) or {}).get("arquivo_chave", "")) or entrada.arquivo_chave
        try:
            with self._escrita():
                arquivo = Path(self.pasta_dados) / "config" / "ia.conf"
                if arquivo.is_file():
                    texto, _ = texto_atual_ou_modelo(self.pasta_dados, catalogo)
                    gravar_texto(self.pasta_dados, remover_chave_do_texto(texto, identificador))
                if arquivo_antigo and "/" not in arquivo_antigo and ".." not in arquivo_antigo:
                    (Path(self.pasta_dados) / "config" / arquivo_antigo).unlink(missing_ok=True)
        except OSError as erro:
            return False, textos.t("py_sessao.nao_foi_possivel_remover_0", erro.strerror or erro)
        self.estado_ia.esquecer(identificador)
        if chave_de(self.pasta_dados, ler_config(self.pasta_dados), entrada):
            return False, textos.t("py_sessao.a_chave_continua_no_pendrive")
        return True, textos.t("py_sessao.chave_de_0_removida_do", entrada.nome)

    def salvar_ia(self, principal: str | None, reserva: str | None, chaves: dict[str, str]) -> tuple[bool, str]:
        """Botão "Salvar no pendrive": grava escolhas e chaves no ``config/ia.conf`` (comentários preservados)."""
        if not self.pasta_dados:
            return False, textos.t("py_sessao.sem_local_de_dados_nao_2")
        catalogo = carregar_catalogo(self.pasta_dados)
        ids = {e.id for e in catalogo.entradas} | set(ler_config(self.pasta_dados).secoes) - {"ia"}
        if principal is not None and principal not in ids:
            return False, textos.t("py_sessao.escolha_a_ia_principal_na")
        if reserva and reserva not in ids:
            return False, textos.t("py_sessao.escolha_a_ia_reserva_na")
        if principal and reserva == principal:
            return False, textos.t("py_sessao.a_reserva_precisa_ser_diferente")
        limpas = {}
        for identificador, chave in chaves.items():
            limpa = normalizar_chave(chave)
            if catalogo.obter(identificador) is None or not limpa:
                return False, textos.t("py_sessao.chave_vazia_ou_ia_desconhecida")
            if len(limpa) > 512 or not limpa.isascii() or not limpa.isprintable():
                return False, textos.t("py_sessao.essa_chave_tem_caracteres_invalidos")
            limpas[identificador] = limpa
        try:
            with self._escrita():
                texto, aviso = texto_atual_ou_modelo(self.pasta_dados, catalogo)
                gravar_texto(self.pasta_dados, atualizar_texto(texto, principal, reserva, limpas))
        except OSError as erro:
            return False, textos.t("py_sessao.nao_foi_possivel_salvar_0", erro.strerror or erro)
        for identificador in limpas:
            self.estado_ia.esquecer(identificador)
        local = textos.t("py_sessao.no_pendrive") if self.local is None or self.local.persistente else textos.t("py_sessao.fora_do_pendrive_perde_se")
        return True, " ".join(textos.t("py_sessao.configuracao_da_ia_salva_0", local, aviso).split())

    def qr_ia(self, identificador: str) -> dict[str, Any] | None:
        """QR Code (gerado aqui, sem serviço externo) com o link de criação da chave."""
        entrada = carregar_catalogo(self.pasta_dados).obter(identificador)
        if entrada is None:
            return None
        matriz = gerar_qr(entrada.link_chave)
        return {"link": entrada.link_chave, "matriz": para_linhas(matriz) if matriz else []}

    # Chave da IA pelo celular (CLAUDE.md 10d) ----------------------------------------

    def _ip_local(self) -> str | None:
        """IPv4 da placa física com rede (o celular precisa estar nessa mesma rede)."""
        for interface in (self.rede.interfaces if self.rede else []):
            if interface.candidata and interface.link:
                for endereco in interface.enderecos:
                    ip = endereco.split("/")[0]
                    if ":" not in ip:
                        return ip
        return None

    def iniciar_pareamento(self, identificador: str) -> tuple[bool, str]:
        """Botão "Receber a chave pelo celular": abre o pareamento (porta, certificado, token, código)."""
        entrada = carregar_catalogo(self.pasta_dados).obter(identificador)
        if entrada is None:
            return False, textos.t("py_sessao.ia_desconhecida_2")
        if not self.pasta_dados:
            return False, textos.t("py_sessao.sem_local_de_dados_nao_3")
        ip = self._ip_local()
        if ip is None:
            return False, (textos.t("py_sessao.este_servidor_nao_tem_endereco"))  # fmt: skip
        with self._trava:
            anterior, self.pareamento = self.pareamento, None
        if anterior is not None:
            anterior.encerrar()
        pareamento = Pareamento(identificador, entrada.nome, entrada.link_chave, ip, FirewallQuiosque(self.executor, self.firewall_pareamento),
                                ao_terminar=self._registrar_pareamento)  # fmt: skip
        with self._trava:
            self.pareamento = pareamento
        return pareamento.iniciar()

    def _estado_celular(self) -> dict[str, Any] | None:
        """Estado do pareamento para a tela (nunca a chave), com o QR Code do endereço enquanto espera o celular."""
        pareamento = self.pareamento
        if pareamento is None:
            return None
        estado = pareamento.estado()
        if estado["url"] and self._qr_celular[0] != estado["url"]:
            matriz = gerar_qr(estado["url"])
            self._qr_celular = (estado["url"], para_linhas(matriz) if matriz else [])
        estado["qr"] = self._qr_celular[1] if estado["url"] else []
        return estado

    def manter_pareamento(self) -> bool:
        """Sinal de vida da tela "Configurar IA" (sem ele por 20 s, a porta fecha)."""
        pareamento = self.pareamento
        return pareamento is not None and pareamento.manter()

    def parar_pareamento(self) -> bool:
        """A tela foi fechada (ou o técnico cancelou): fecha a porta e descarta a chave pendente."""
        pareamento = self.pareamento
        if pareamento is not None:
            pareamento.encerrar()
        return True

    def aceitar_chave_celular(self) -> tuple[bool, str, dict[str, Any] | None]:
        """"Aceitar": salva a chave recebida (principal, se ainda não houver) e roda o "Testar" sozinho."""
        pareamento = self.pareamento
        chave = pareamento.aceitar() if pareamento is not None else None
        if pareamento is None or chave is None:
            return False, textos.t("py_sessao.nenhuma_chave_recebida_do_celular"), None
        principal = None if resolver_configuracao(self.pasta_dados)[0] else pareamento.ia
        ok, mensagem = self.salvar_ia(principal, None, {pareamento.ia: chave})
        del chave
        if not ok:
            return False, mensagem, None
        return True, mensagem, self.testar_ia(pareamento.ia)

    def recusar_chave_celular(self) -> bool:
        pareamento = self.pareamento
        if pareamento is None:
            return False
        pareamento.recusar()
        return True

    def _registrar_pareamento(self, pareamento: Pareamento) -> None:
        """Fim de um pareamento: registro sem a chave (e no relatório, se já existir)."""
        with self._trava:
            self.pareamentos_ia.append(pareamento.registro())
            relatorio = self.relatorio
            if relatorio is not None:
                relatorio["pareamentos_ia"] = list(self.pareamentos_ia)
        if relatorio is not None and self.gravacao and self.gravacao.get("pasta"):
            def gravar() -> None:
                with self._escrita():
                    self._regravar(relatorio)

            threading.Thread(target=gravar, daemon=True, name="msscan-pareamento-relatorio").start()

    # Chat com a IA (CLAUDE.md 10e) ------------------------------------------------------

    def _motivo_chat(self) -> str:
        if self.conversa is None:
            return self.chat.get("mensagem") or textos.t("py_sessao.o_chat_comeca_quando_a")
        return ""

    def enviar_chat(self, texto: str) -> tuple[bool, str]:
        """Mensagem do técnico: sanitizada, enviada em segundo plano (principal → reserva; espera a rede)."""
        with self._trava:
            conversa = self.conversa
            if conversa is None:
                return False, self._motivo_chat()
            if self.chat["situacao"] in ("digitando", "aguardando_rede"):
                return False, textos.t("py_sessao.aguarde_a_resposta_anterior")
            if not (texto or "").strip():
                return False, textos.t("py_sessao.mensagem_vazia")
            relatorio = self.relatorio
            self.chat = {"situacao": "digitando", "mensagem": "", "revisao": self.chat["revisao"] + 1}
            self.executor.ia_cancelada.clear()

        def avisar(mensagem: str) -> None:
            with self._trava:
                self.chat = {"situacao": "aguardando_rede" if "rede" in mensagem.lower() and "volta" not in mensagem.lower() else "digitando",
                             "mensagem": mensagem, "revisao": self.chat["revisao"] + 1}  # fmt: skip

        def parcial(texto_parcial: str) -> None:  # streaming: resposta aparecendo enquanto a IA escreve
            with self._trava:
                self.chat = {**self.chat, "parcial": texto_parcial[-8000:]}

        def conversar() -> None:
            try:
                resultado = conversa.conversar(self.executor, carregar_provedores(self.pasta_dados)[0], texto, self.monitor, avisar,
                                               ao_parcial=parcial)  # fmt: skip
                self.estado_ia.registrar_tentativas(resultado.tentativas)
                motivo = resumo_falhas(resultado.tentativas) if resultado.tentativas else resultado.mensagem
                mensagem = "" if resultado.ok else textos.t("py_sessao.sem_resposta_da_ia_0", motivo[:1].upper(), motivo[1:])
            except Exception as erro:  # noqa: BLE001 - o chat nunca derruba a interface
                mensagem = textos.t("py_sessao.erro_inesperado_no_chat_0", type(erro).__name__)
            if relatorio is not None:
                relatorio["chat"] = conversa.para_relatorio()
                with self._escrita():
                    self._regravar(relatorio)
            with self._trava:
                self.chat = {"situacao": "pronto" if not mensagem else "falhou", "mensagem": mensagem, "revisao": self.chat["revisao"] + 1}

        threading.Thread(target=conversar, daemon=True, name="msscan-chat").start()
        return True, "enviado"

    def cancelar_envio_ia(self) -> bool:
        """Botão "Cancelar envio" (17.5), na análise e no chat: interrompe a requisição em andamento na hora."""
        with self._trava:
            enviando = self.ia.get("situacao") == "enviando" or self.chat.get("situacao") in ("digitando", "aguardando_rede")
        if enviando:
            self.executor.cancelar_ia()
        return enviando

    def repetir_chat(self) -> tuple[bool, str]:
        """"Tentar novamente" no chat: reenvia a última pergunta que ficou sem resposta (sem refazer nada)."""
        with self._trava:
            if self.conversa is None or self.chat["situacao"] != "falhou":
                return False, textos.t("py_sessao.nada_para_repetir")
            texto = self.conversa.retirar_sem_resposta()
            if texto is None:
                return False, textos.t("py_sessao.nada_para_repetir")
        return self.enviar_chat(texto)

    def mensagens_chat(self) -> dict[str, Any]:
        """Conversa completa (textos sanitizados, sem chaves), pedida pela tela quando a revisão muda."""
        with self._trava:
            conversa = self.conversa
            return {"revisao": self.chat["revisao"], "mensagens": [dict(m) for m in conversa.mensagens] if conversa else []}

    def _estado_chat(self) -> dict[str, Any]:
        return {**self.chat, "disponivel": self.conversa is not None, "motivo": self._motivo_chat(),
                "sem_reserva": len(resolver_configuracao(self.pasta_dados)[0]) == 1,
                "sugestoes": SUGESTOES, "quantidade": len(self.conversa.mensagens) if self.conversa else 0}  # fmt: skip

    # Atualizações (somente informativo) ------------------------------------------

    def _motivo_atualizacoes(self) -> str:
        if self.relatorio is None or self.etapa != CONCLUIDO:
            return textos.t("py_sessao.a_verificacao_de_atualizacoes_fica")
        rede = self.rede
        return disponibilidade_atualizacoes(bool(rede and rede.ok), bool(rede and rede.sem_rede_por_parametro), self.replay)

    def verificar_atualizacoes(self) -> bool:
        """Botão "Verificar (somente informativo)": inventário × catálogo Dell, em segundo plano."""
        with self._trava:
            if self._motivo_atualizacoes() or self._ocupado() or self.ctx is None:
                return False
            self.atualizacoes = {"situacao": "verificando", "mensagem": textos.t("py_sessao.inventariando_o_firmware_e_lendo")}
            self.aviso = None  # a lista vai mudar: aviso antigo não vale mais
            self.preparo = None
            relatorio, ctx = self.relatorio, self.ctx
        assert relatorio is not None

        def avisar(texto: str) -> None:
            with self._trava:
                self.atualizacoes = {"situacao": "verificando", "mensagem": texto}

        def verificar() -> None:
            # Espera a rede FORA da janela de escrita: o pendrive só fica gravável durante o download.
            esperar_rede(self.monitor, avisar, ESPERA_MAXIMA_S)
            with self._escrita():
                relatorio["atualizacoes"] = executar_verificacao(ctx, relatorio, self.executor, self.pasta_dados,
                                                                 monitor=self.monitor, avisar=avisar)  # fmt: skip
                self._regravar(relatorio)
            with self._trava:
                self.atualizacoes = {"situacao": relatorio["atualizacoes"]["situacao"],
                                     "mensagem": relatorio["atualizacoes"].get("mensagem", "")}  # fmt: skip

        self._em_segundo_plano(verificar)
        return True

    def baixar_escolhidos(self, itens: list[str]) -> tuple[bool, str]:
        """Etapas 2 e 3 do fluxo guiado: baixa os escolhidos do catálogo e confere hash, modelo e versão (nada no BMC)."""
        with self._trava:
            if self._motivo_atualizacoes() or self._ocupado() or self.relatorio is None:
                return False, textos.t("py_sessao.indisponivel_agora_diagnostico_em_andamento")
            verificacao = self.relatorio.get("atualizacoes") or {}
            motivo = selecao_desatualizada(verificacao, itens)
            if motivo:
                return False, motivo
            plano = montar_plano({**verificacao, "bloqueios": []}, itens)  # bloqueios do hardware ficam para o aviso
            if not plano["itens"]:
                return False, " ".join(plano["recusados"]) or "nada a baixar"
            self.preparo = {"situacao": "baixando", "ids": sorted(itens), "itens": [], "recusados": plano["recusados"],
                            "mensagem": textos.t("py_sessao.baixando_do_catalogo_oficial_da")}  # fmt: skip
            system_id = verificacao.get("system_id")

        def avisar(texto: str) -> None:
            with self._trava:
                self.preparo = {**(self.preparo or {}), "mensagem": texto}

        def baixar() -> None:
            esperar_rede(self.monitor, avisar, ESPERA_MAXIMA_S)
            with self._escrita():
                feitos = baixar_e_verificar(plano["itens"], self.executor, self.pasta_dados, system_id, self.monitor, avisar)
            with self._trava:
                ok = all(f["ok"] for f in feitos)
                self.preparo = {**(self.preparo or {}), "situacao": "pronto" if ok else "falhou", "itens": feitos,
                                "mensagem": textos.t("py_sessao.pacotes_baixados_e_conferidos_hash") if ok
                                else textos.t("py_sessao.algum_pacote_nao_passou_na")}  # fmt: skip

        self._em_segundo_plano(baixar)
        return True, "baixando"

    def _estado_atualizacoes(self) -> dict[str, Any]:
        motivo = self._motivo_atualizacoes()
        secao = (self.relatorio or {}).get("atualizacoes") or {}
        return {
            **self.atualizacoes,
            "disponivel": not motivo,
            "motivo": motivo,
            "secao": secao if secao.get("situacao") not in (None, "nao_solicitadas") else None,
            "modo": self.modo_aplicacao,
            "trava": None if self.executor.escrita_permitida() else MOTIVO_TRAVA,
            "aplicacao": _instantaneo(self.aplicacao),
            "aviso": self.aviso,
            "preparo": self.preparo,
            "retomada": self.retomada,
            "jobs_idrac": self.jobs_idrac,
            "limpeza_jobs": self.limpeza_jobs,
            "manual": self.manual.estado(),
        }

    # Aplicação de firmware (fase 9) -----------------------------------------------

    def _retomar(self) -> None:
        """Volta de um reinício de atualização: confere antes × depois e tira o estado de "pendente"."""
        estado = ler_estado(self.executor, self.pasta_dados)
        if estado is None or self.ctx is None:
            return
        inventario, fonte = coletar_inventario(self.ctx)
        retomada = conferir(estado, inventario, fonte)
        # 18.2: o plano confirmado continua sozinho (mesmo servidor, até 24 h, último envio aceito pelo iDRAC).
        motivo = pode_continuar(estado, self.ctx, conferencia=retomada) if self.modo_aplicacao == MODO_APLICAR else textos.t("py_sessao.modo_interno_de_teste")
        retomada["continua_sozinho"] = not motivo
        retomada["motivo_nao_continua"] = motivo if estado.get("pendentes") else ""
        retomada["anterior"] = estado.get("parcial") or {}
        parado = pode_continuar_parado(estado, self.ctx, conferencia=retomada) if self.modo_aplicacao == MODO_APLICAR else textos.t("py_sessao.modo_interno_de_teste")
        retomada["pode_continuar_parado"] = not parado
        if estado.get("situacao") == "parado":
            retomada["motivo_nao_continua"] = parado or textos.t("py_sessao.parado_a_pedido_use_continuar")
        if self.local is not None and self.local.persistente and self.pasta_dados:
            with self._escrita():
                arquivar_estado(self.pasta_dados)
        with self._trava:
            self.retomada = retomada
            self._continuacao = estado if not motivo else None
            self._parado = estado if not parado else None
        self.log_automatico()  # pedido de 22/10/2026: o antes × depois da volta também fica no log

    def antes_depois(self, ctx: Any, resultado: dict[str, Any]) -> None:
        """Relatório final antes × depois (15.6): relê o inventário para o que o BMC aplicou sem reinício.

        O que ficou agendado para o reinício é conferido pela retomada, na volta.
        """
        aplicados = [i for i in resultado.get("itens") or [] if i.get("situacao") == "aplicado"]
        if aplicados and resultado.get("modo") == MODO_APLICAR:
            inventario, fonte = coletar_inventario(ctx)
            resultado["antes_depois"] = conferir({"enviados": aplicados}, inventario, fonte)["itens"]

    def motivo_sem_pendrive(self, modo: str) -> str:
        """Gravação real exige dados no pendrive: sem ele, a retomada e o backup somem no reinício (MÉDIO 8)."""
        if modo == MODO_APLICAR and self.local is not None and not self.local.persistente:
            return (textos.t("py_sessao.instalacao_real_exige_o_pendrive"))  # fmt: skip
        return ""

    def modo_para(self) -> str:
        """Modo desta instalação: real (18.2); só o ``--simular`` interno de desenvolvimento simula."""
        return self.modo_aplicacao

    def preparar_aviso(self, origem: str, itens: list[str]) -> tuple[bool, str]:
        """Tela "Plano de atualização" (CLAUDE.md 18.2): relê o hardware e o BMC (só leitura) em segundo plano."""
        simular = self.modo_aplicacao == MODO_SIMULAR
        with self._trava:
            if origem not in ("catalogo", "manual"):
                return False, textos.t("py_sessao.origem_desconhecida")
            if self.etapa != CONCLUIDO or self._ocupado() or self.relatorio is None or self.ctx is None:
                return False, textos.t("py_sessao.indisponivel_agora_diagnostico_em_andamento")
            pacote = None
            if origem == "manual":
                pacote = self.manual._pacote(itens[0]) if len(itens) == 1 else None
                if pacote is None:
                    return False, textos.t("py_sessao.pacote_desconhecido")
            verificacao, ctx = self.relatorio.get("atualizacoes") or {}, self.ctx
            if origem == "catalogo":
                motivo = selecao_desatualizada(verificacao, itens)
                if motivo:
                    return False, motivo
            self.aviso = {"situacao": "conferindo", "origem": origem,
                          "mensagem": textos.t("py_sessao.conferindo_o_hardware_agora_fontes")}  # fmt: skip

        def montar() -> None:
            try:
                bmc = ler_bmc(ctx)
                if origem == "catalogo":
                    aviso = aviso_catalogo(ctx, self.registro, montar_plano(verificacao, itens), itens, bmc, simular)
                else:
                    aviso = aviso_manual(ctx, self.registro, pacote, bmc, simular)
                aviso["situacao"] = "pronto"
            except Exception as erro:  # noqa: BLE001 - sem aviso não se instala nada
                aviso = {"situacao": "falhou", "origem": origem, "mensagem": textos.t("py_sessao.nao_foi_possivel_montar_o", erro)}
            with self._trava:
                self.aviso = aviso

        self._em_segundo_plano(montar)
        return True, "conferindo"

    def marcar_ciente(self, ciente: bool) -> bool:
        """Caixa "Estou ciente de que é um evento antigo" da tela de aviso (evento crítico só no histórico do SEL)."""
        with self._trava:
            if (self.aviso or {}).get("situacao") != "pronto":
                return False
            self.aviso["ciente"] = ciente is True
        return True

    def cancelar_aviso(self) -> bool:
        """"Cancelar" na tela de aviso: descarta o aviso (nada foi instalado)."""
        with self._trava:
            self.aviso = None
        return True

    def aviso_para_instalar(self, origem: str, itens: list[str], assinaturas: list[str],
                            confirmacao_riscos: str = "") -> tuple[dict[str, Any] | None, str]:  # fmt: skip
        """Plano válido para exatamente estes itens e conteúdo, consumido aqui (uso único). Com riscos de hardware, só segue
        com ``ASSUMO OS RISCOS`` digitado; o registro vai em ``aviso["riscos_assumidos"]`` (só esta instalação)."""
        with self._trava:
            aviso = self.aviso if (self.aviso or {}).get("situacao") == "pronto" else None
            motivo = conferir_aviso(aviso, origem, itens, assinaturas=assinaturas, confirmacao_riscos=confirmacao_riscos)
            if motivo:
                return None, motivo
            aviso["riscos_assumidos"] = riscos_assumidos(aviso, assumidos=not aviso.get("pode_seguir"))
            self.aviso = None
            return aviso, ""

    def aplicar_atualizacoes(self, itens: list[str], confirmacao: str, confirmacao_redundancia: str = "",
                             confirmacao_riscos: str = "") -> tuple[bool, str]:  # fmt: skip
        """Botão "Instalar" do plano de atualização. Plano e confirmações conferidos AQUI (não no navegador); depois do
        ``ATUALIZAR``, tudo é automático (CLAUDE.md 18.2)."""
        # Revisão de 06/10/2026, B7: a tarefa começa DENTRO da trava em que "ocupado" foi conferido (instalar e continuar
        # quase juntos nunca abrem duas threads).
        def avisar(texto: str) -> None:
            with self._trava:
                self.aplicacao = {**self.aplicacao, "situacao": "executando", "mensagem": texto}

        def executar() -> None:
            esperar_rede(self.monitor, avisar, ESPERA_MAXIMA_S)  # fora da janela de escrita do pendrive
            parcial = self._novo_acompanhamento(plano["itens"])
            parcial["riscos_assumidos"] = aviso["riscos_assumidos"]
            with self._trava:
                self.aplicacao = {**self.aplicacao, "resultado": parcial}  # progresso por componente, ao vivo
            with gravando(textos.t("py_sessao.firmware_catalogo")), self._escrita():
                resultado = executar_plano(ctx, plano, self.executor, self.pasta_dados, modo, monitor=self.monitor,
                                           avisar=avisar, parcial=parcial)  # fmt: skip
                self.antes_depois(ctx, resultado)
                relatorio["aplicacao"] = resultado
                self._regravar(relatorio)
            self._fim_aplicacao(resultado)

        with self._trava:
            if self.etapa != CONCLUIDO or self._ocupado() or self.relatorio is None or self.ctx is None:
                return False, textos.t("py_sessao.indisponivel_agora_diagnostico_em_andamento")
            verificacao = self.relatorio.get("atualizacoes") or {}
            plano = montar_plano(verificacao, itens)
            if not plano["pode_seguir"]:
                return False, " ".join(plano["bloqueios"] + plano["recusados"]) or "nada a aplicar"
            # A confirmação da redundância vale pelo diagnóstico OU pela releitura da tela de aviso.
            plano["confirmacao_redundancia"] = plano["confirmacao_redundancia"] or bool((self.aviso or {}).get("redundancia"))
            motivo = conferir_confirmacoes(plano, confirmacao, confirmacao_redundancia)
            if motivo:
                return False, motivo
            motivo = self.motivo_sem_pendrive(self.modo_para())
            if motivo:
                return False, motivo
            aviso, motivo = self.aviso_para_instalar("catalogo", itens, [assinatura_catalogo(i) for i in plano["itens"]],
                                                     confirmacao_riscos)  # fmt: skip
            if aviso is None:
                return False, motivo
            relatorio, ctx, modo = self.relatorio, self.ctx, self.modo_para()
            self.aplicacao = {"situacao": "executando", "mensagem": textos.t("py_sessao.baixando_e_conferindo_os_pacotes"),
                              "itens": [{"componente": i["componente"], "atual": i["atual"], "nova": i["nova"]} for i in aviso["itens"]]}  # fmt: skip
            self._em_segundo_plano(executar)
        return True, "iniciado"

    # Ações corretivas (fase 10) ----------------------------------------------------

    def _motivo_acoes(self) -> str:
        if self.relatorio is None or self.etapa != CONCLUIDO or self.ctx is None:
            return textos.t("py_sessao.as_acoes_corretivas_ficam_disponiveis")
        return ""

    def prechecar_acao(self, identificador: str, parametros: dict[str, Any]) -> tuple[bool, str]:
        """Botão "Verificar e ver a prévia": pré-checagens só de leitura, em segundo plano."""
        with self._trava:
            if self._motivo_acoes() or self._ocupado():
                return False, self._motivo_acoes() or textos.t("py_sessao.indisponivel_agora_outra_tarefa_rodando")
            if identificador not in CATALOGO_ACOES:
                return False, textos.t("py_sessao.acao_0_nao_existe_no", identificador)
            ctx = self.ctx
            self.acao_pre = None
            self.acao = {"situacao": "prechecando", "mensagem": textos.t("py_sessao.pre_checagens_so_leitura")}
        persistente = bool(self.local is not None and self.local.persistente and self.pasta_dados)
        sugeridos = self.ntp_sugestoes

        def prechecar() -> None:
            pre = acoes_execucao.prechecar(identificador, ctx, self.executor, parametros, persistente=persistente,
                                           sugeridos=sugeridos)  # fmt: skip
            with self._trava:
                self.acao_pre = pre
                self.acao = {"situacao": "prechecada", "mensagem": ""}

        self._em_segundo_plano(prechecar)
        return True, "iniciado"

    def executar_acao(self, identificador: str, confirmacao: str) -> tuple[bool, str]:
        """Botão "Executar a ação": só com a pré-checagem aprovada e CORRIGIR conferido AQUI (não no navegador)."""
        with self._trava:
            if self._motivo_acoes() or self._ocupado():
                return False, self._motivo_acoes() or textos.t("py_sessao.indisponivel_agora_outra_tarefa_rodando")
            pre = self.acao_pre
            if pre is None or pre.get("acao") != identificador:
                return False, textos.t("py_sessao.faca_a_pre_checagem_desta")
            if not pre.get("pode_seguir"):
                return False, " ".join(pre.get("bloqueios") or []) or "nada a alterar"
            motivo = conferir_confirmacao_acao(confirmacao)
            if motivo:
                return False, motivo
            relatorio, ctx, modo = self.relatorio, self.ctx, self.modo_para()
            self.acao_pre = None  # uma execução por pré-checagem
            self.acao = {"situacao": "executando", "mensagem": textos.t("py_sessao.executando_a_acao")}
        assert relatorio is not None

        def executar() -> None:
            with gravando(textos.t("py_sessao.acao_no_bmc")), self._escrita():
                resultado = acoes_execucao.executar(pre, confirmacao, ctx, self.executor, self.pasta_dados, modo,
                                                    pedir_administrador=None)  # fmt: skip
                relatorio.setdefault("acoes", []).append(resultado)
                self._regravar(relatorio)
            with self._trava:
                self.acao = {"situacao": resultado["situacao"], "mensagem": resultado["mensagem"], "resultado": resultado}

        self._em_segundo_plano(executar)
        return True, "iniciado"

    def _texto_completo(self, relatorio: dict[str, Any]) -> str:
        """Relatório TXT: diagnóstico + IA + conversa + pareamentos + ações + atualizações + aplicação."""
        if self.ssh.historico:
            relatorio["ssh"] = self.ssh.para_relatorio()
        partes = [self.texto_relatorio, formatar_analise_ia(relatorio.get("interpretacao_ia")), formatar_chat(relatorio.get("chat")),
                  formatar_pareamentos(relatorio.get("pareamentos_ia")), formatar_acoes(relatorio.get("acoes")),
                  formatar_atualizacoes(relatorio.get("atualizacoes")), formatar_aplicacao(relatorio.get("aplicacao")),
                  formatar_jobs_idrac(relatorio.get("jobs_idrac")), _formatar_ssh(relatorio.get("ssh"))]  # fmt: skip
        return "\n".join(p for p in partes if p)

    def _regravar(self, relatorio: dict[str, Any]) -> None:
        """Regrava o relatório (TXT/HTML/JSON) na mesma pasta. Chamar dentro de ``self._escrita()``."""
        pasta = Path(self.gravacao["pasta"]) if self.gravacao and self.gravacao["pasta"] else None
        gravar_relatorios(relatorio, self._texto_completo(relatorio), self.pasta_dados, self.replay,
                          persistente=self.local is None or self.local.persistente, pasta=pasta)  # fmt: skip

    def prechecar_desfazer(self, indice: int) -> tuple[bool, str]:
        """Botão "Desfazer": relê o BMC e os valores guardados no pendrive e mostra a prévia (só leitura)."""
        with self._trava:
            if self._motivo_acoes() or self._ocupado():
                return False, self._motivo_acoes() or textos.t("py_sessao.indisponivel_agora_outra_tarefa_rodando")
            acoes = (self.relatorio or {}).get("acoes") or []
            if not 0 <= indice < len(acoes):
                return False, textos.t("py_sessao.acao_nao_encontrada")
            resultado, ctx = acoes[indice], self.ctx
            self.desfazer_pre = None
            self.acao = {"situacao": "prechecando", "mensagem": textos.t("py_sessao.lendo_os_valores_guardados_e"),
                         "resultado": resultado}  # fmt: skip

        def prechecar() -> None:
            pre = acoes_execucao.prechecar_desfazer(resultado, ctx)
            with self._trava:
                self.desfazer_pre = {**pre, "indice": indice}
                self.acao = {"situacao": "desfazer_prechecado", "mensagem": "", "resultado": resultado}

        self._em_segundo_plano(prechecar)
        return True, "iniciado"

    def desfazer_acao(self, indice: int, confirmacao: str) -> tuple[bool, str]:
        """Executa o desfazer: só com a pré-checagem aprovada e DESFAZER conferido AQUI (não no navegador)."""
        with self._trava:
            if self._motivo_acoes() or self._ocupado():
                return False, self._motivo_acoes() or textos.t("py_sessao.indisponivel_agora_outra_tarefa_rodando")
            pre = self.desfazer_pre
            if pre is None or pre.get("indice") != indice:
                return False, textos.t("py_sessao.leia_a_previa_do_desfazer")
            if not pre.get("pode_seguir"):
                return False, " ".join(pre.get("bloqueios") or []) or "nada a desfazer"
            motivo = conferir_confirmacao_acao(confirmacao, CONFIRMACAO_DESFAZER)
            if motivo:
                return False, motivo
            relatorio, ctx, modo = self.relatorio, self.ctx, self.modo_aplicacao
            assert relatorio is not None
            resultado = relatorio["acoes"][indice]
            self.desfazer_pre = None
            self.acao = {"situacao": "executando", "mensagem": "Desfazendo...", "resultado": resultado}

        def executar() -> None:
            with gravando(textos.t("py_sessao.acao_no_bmc_desfazer")):
                registro = acoes_execucao.desfazer(pre, resultado, confirmacao, ctx, modo, pedir_administrador=None,
                                                   executor=self.executor, pasta_dados=self.pasta_dados)  # fmt: skip
            with self._escrita():
                self._regravar(relatorio)
            with self._trava:
                self.acao = {"situacao": registro["situacao"], "mensagem": "Desfazer: " + registro["mensagem"], "resultado": resultado}

        self._em_segundo_plano(executar)
        return True, "iniciado"

    def ler_sugestoes_ntp(self) -> bool:
        """Sugestões de NTP (DHCP da rede do cliente, senão pool.ntp.org): lidas uma vez, quando o técnico escolhe a
        ação de NTP (só leitura, em segundo plano). ``False`` se as ações ainda não estão disponíveis."""
        with self._trava:
            if self._motivo_acoes():
                return False
            if self.ntp_sugestoes is not None:
                return True
            self.ntp_sugestoes = []  # evita leituras repetidas enquanto a thread roda

        def ler() -> None:
            sugeridos = acoes_execucao.sugestoes_ntp(self.executor)
            with self._trava:
                self.ntp_sugestoes = sugeridos

        threading.Thread(target=ler, daemon=True, name="msscan-ntp-sugestoes").start()
        return True

    def _estado_acoes(self) -> dict[str, Any]:
        motivo = self._motivo_acoes()
        sugeridas = recomendadas(self.relatorio)
        return {
            **self.acao,
            "disponivel": not motivo,
            "motivo": motivo,
            "modo": self.modo_aplicacao,
            "trava": None if self.executor.acoes_permitidas() else MOTIVO_TRAVA_ACOES,
            "catalogo": [{"id": ident, "titulo": a["titulo"], "descricao": a["descricao"], "requer": a["requer"],
                          "modulo": a["modulo"], "recomendada": sugeridas.get(ident, [])} for ident, a in CATALOGO_ACOES.items()],  # fmt: skip
            "prechecagem": self.acao_pre,
            "desfazer": self.desfazer_pre,
            "indice_resultado": next((i for i, a in enumerate((self.relatorio or {}).get("acoes") or [])
                                      if a is self.acao.get("resultado")), None),  # fmt: skip
            "ntp_sugestoes": self.ntp_sugestoes or [],
            "executadas": (self.relatorio or {}).get("acoes") or [],
        }

    # Credenciais do BMC ------------------------------------------------------

    def informar_credenciais(self, usuario: str, senha: str) -> bool:
        """Valida usuário/senha do BMC em segundo plano. ``False`` se não fizer sentido agora.

        Com o diagnóstico já concluído, refaz só os itens do Redfish ao validar.
        A senha não é guardada: só passa por ``AcessoBMC.tentar``.
        """
        with self._trava:
            acesso = self.ctx.bmc if self.ctx is not None else None
            if acesso is None or not acesso.pode_tentar or acesso.validado or self._ocupado():
                return False
            if self.etapa not in (PRONTO, CONCLUIDO):
                return False
            self.bmc_validando = True
            refazer = self.etapa == CONCLUIDO
            if refazer:
                self.etapa = DIAGNOSTICO

        def validar() -> None:
            try:
                acesso.tentar(usuario, senha)
            finally:
                with self._trava:
                    self.bmc_validando = False
            if refazer:
                if acesso.validado:
                    self._diagnosticar(somente=COMPONENTES_REDFISH)
                else:
                    with self._trava:
                        self.etapa = CONCLUIDO

        self._em_segundo_plano(validar)
        return True

    def _baixar_idrac_tools(self, ctx: Contexto) -> None:
        """CLAUDE.md 20: com internet e sem racadm, baixa o iDRAC Tools da Dell em segundo plano (sem travar a interface),
        mesmo que ainda não seja necessário — o pendrive fica pronto para clientes sem internet."""
        from msscan.deteccao import idrac_tools

        pendente = ctx.cache.get("racadm_pendente")
        if self.replay or self.rede_situacao != "ok" or not _dell(ctx) or (pendente is not None and not pendente.is_set()):
            return

        def terminou() -> None:
            acesso = diagnosticar_acesso(ctx)
            with self._trava:
                self.acesso_idrac = acesso

        idrac_tools.iniciar_download(ctx, self._escrita, self.monitor, terminou)

    def tentar_acesso_idrac(self) -> bool:
        """Botão "Tentar de novo" da caixa "Acesso ao iDRAC" (19.2): racadm em ``vendor/``, rede do iDRAC e Redfish, sem
        reiniciar. Sessão Redfish já validada é mantida. ``False`` se não fizer sentido agora."""
        with self._trava:
            ctx = self.ctx
            if ctx is None or self.acesso_verificando or self.bmc_validando or self._ocupado() or not _dell(ctx):
                return False
            if self.etapa not in (PRONTO, CONCLUIDO):
                return False
            self.acesso_verificando = True

        def verificar() -> None:
            from msscan.deteccao.contexto import InfoRedfish
            from msscan.deteccao.detectores import detectar_redfish, preparar_racadm

            try:
                preparar_racadm(ctx)
                self._baixar_idrac_tools(ctx)  # sem pacote e sem racadm: tenta baixar de novo (ex.: a internet voltou)
                ctx.cache.pop("racadm_versoes", None)
                if not (ctx.bmc is not None and ctx.bmc.validado):
                    ctx.redfish, ctx.bmc = InfoRedfish(), None
                    detectar_redfish(ctx)
                acesso = diagnosticar_acesso(ctx)
            except Exception as erro:  # noqa: BLE001 - a caixa conta o erro, a interface segue
                acesso = {**(self.acesso_idrac or {}), "mensagens": [{"nivel": "alerta", "texto": textos.t("py_sessao.nova_verificacao_falhou_0_1", type(erro).__name__, erro)}], "botoes": ["tentar"]}  # fmt: skip
            with self._trava:
                self.acesso_idrac = acesso
                self.acesso_verificando = False

        self._em_segundo_plano(verificar)
        return True

    def pular_credenciais(self) -> bool:
        """Botão "Pular": segue sem Redfish (normal)."""
        with self._trava:
            if self.ctx is None or self.ctx.bmc is None or self.bmc_validando:
                return False
            self.ctx.bmc.pular()
            return True

    def exportar_log(self) -> tuple[bool, str, str]:
        """Botão "Exportar log técnico" (18.6): JSON sanitizado no pendrive (``MSSCAN/logs/``) e devolvido à tela.

        ``(gravado, onde, conteúdo)``; sem pendrive gravável, só devolve o conteúdo (a tela oferece salvar)."""
        from msscan.relatorio import log_tecnico

        texto = self._montar_log()
        if not self._log_gravavel():
            return False, textos.t("py_sessao.nao_gravado_no_pendrive_sem"), texto
        return self._gravar_log(Path(str(self.pasta_dados)) / log_tecnico.PASTA_LOGS / log_tecnico.nome_arquivo(), texto) + (texto,)

    def coletar_dados(self) -> tuple[bool, str]:
        """Botão "Coletar dados deste servidor": o mesmo ``--coletar`` do terminal (somente leitura, sem credenciais do BMC),
        montado na RAM e só então gravado e conferido em ``MSSCAN/coletas/`` numa janela curta de escrita."""
        from msscan.acoes.comum import gravar_conferido, no_pendrive
        from msscan.coleta.coletor import coletar

        with self._trava:
            if self.coleta["situacao"] == "coletando":
                return False, self.coleta["mensagem"]
            motivo = (textos.t("py_sessao.coleta_na_demonstracao") if self.replay
                      else textos.t("py_sessao.coleta_sem_pendrive") if not self._log_gravavel()
                      else textos.t("py_sessao.coleta_ocupado") if self._ocupado() or self.etapa not in (PRONTO, CONCLUIDO)
                      else "")  # fmt: skip
            if motivo:  # fica no estado: a tela mostra o motivo até a próxima tentativa
                self.coleta = {"situacao": "recusada", "mensagem": motivo}
                return False, motivo
            self.coleta = {"situacao": "coletando", "mensagem": textos.t("py_sessao.coleta_em_andamento")}

        def tarefa() -> None:
            import tempfile

            try:
                with tempfile.TemporaryDirectory(prefix="msscan_coleta_ui_") as temporaria:
                    pacote = coletar(temporaria, base=self.executor, pasta_dados=self.pasta_dados)
                    destino = Path(str(self.pasta_dados)) / "coletas" / pacote.name
                    with self._escrita():
                        gravar_conferido(destino, pacote.read_bytes())
                coleta = {"situacao": "concluida", "mensagem": textos.t("py_sessao.coleta_gravada", no_pendrive(destino, self.pasta_dados))}
            except Exception as erro:  # noqa: BLE001 - a coleta nunca derruba a sessão
                coleta = {"situacao": "erro", "mensagem": textos.t("py_sessao.coleta_falhou", f"{type(erro).__name__}: {erro}")}
            with self._trava:
                self.coleta = coleta

        self._em_segundo_plano(tarefa)
        return True, ""

    def _log_gravavel(self) -> bool:
        return bool(self.pasta_dados) and not self.replay and not (self.local is not None and not self.local.persistente)

    def _gravar_log(self, destino: Path, texto: str) -> tuple[bool, str]:
        from msscan.acoes.comum import gravar_conferido, no_pendrive

        try:
            with self._escrita():
                destino.parent.mkdir(parents=True, exist_ok=True)
                gravar_conferido(destino, texto.encode("utf-8"))
        except (OSError, ValueError) as erro:
            return False, textos.t("py_sessao.nao_foi_possivel_gravar_no", erro)
        return True, no_pendrive(destino, self.pasta_dados)

    def log_automatico(self) -> None:
        """Log técnico gravado no pendrive a cada mudança de etapa da atualização (21.7): um desligamento no meio não o
        perde. Sempre o mesmo arquivo nesta sessão. Nunca levanta exceção."""
        from msscan.relatorio import log_tecnico

        if not self._log_gravavel():
            return
        try:
            if self._log_auto is None:
                self._log_auto = Path(str(self.pasta_dados)) / log_tecnico.PASTA_LOGS / log_tecnico.nome_arquivo().replace(".json", "_auto.json")
            self._gravar_log(self._log_auto, self._montar_log())
        except Exception:  # noqa: BLE001 - o log nunca para a atualização
            pass

    def print_tela(self) -> None:
        """Pedido de 22/10/2026: print da tela ao fim de toda instalação (ou tentativa), ao lado do log automático
        (``..._auto_tela_<hora>.png``). Tirado na RAM (``scrot``, X do quiosque) e só então gravado no pendrive. Ao contrário
        do log, NÃO é sanitizado: mostra o que a tela mostra. Nunca levanta exceção."""
        import os
        import tempfile

        from msscan.acoes.comum import gravar_conferido

        if not self._log_gravavel() or self._log_auto is None or not os.environ.get("DISPLAY"):
            return
        try:
            with tempfile.TemporaryDirectory(prefix="msscan_tela_") as pasta:
                png = Path(pasta) / "tela.png"
                if self.executor.rodar(["scrot", "-o", str(png)], timeout=20)[0] != 0 or not png.is_file():
                    return
                with self._escrita():
                    gravar_conferido(self._log_auto.with_name(f"{self._log_auto.stem}_tela_{datetime.now():%H%M%S}.png"), png.read_bytes())
        except Exception:  # noqa: BLE001 - o print nunca atrapalha a sessão
            pass

    def _novo_acompanhamento(self, itens: list[dict[str, Any]]) -> dict[str, Any]:
        """Resultado parcial com o ``Progresso`` ao vivo (21.1) ligado ao log automático (21.7)."""
        from msscan.atualizacao.aplicacao import componentes_do_plano
        from msscan.atualizacao.progresso import Progresso

        return {"passos": [], "itens": [], "_acompanhamento": Progresso(componentes_do_plano(itens), ao_mudar=self.log_automatico)}

    def parar_depois_do_atual(self) -> tuple[bool, str]:
        """Botão "Parar depois do componente atual" (21.2): o atual termina; o próximo não começa."""
        with self._trava:
            prog = ((self.aplicacao or {}).get("resultado") or {}).get("_acompanhamento")
            if (self.aplicacao or {}).get("situacao") != "executando" or prog is None:
                return False, textos.t("py_sessao.nenhuma_instalacao_em_andamento")
            prog.pedir_parada()
        return True, textos.t("py_sessao.o_componente_atual_termina_o")

    def continuar_parado(self) -> tuple[bool, str]:
        """Botão "Continuar de onde parou" (21.2): o plano parado a pedido continua (nunca sozinho)."""
        with self._trava:
            if self._parado is None or self.etapa != CONCLUIDO or self._ocupado():
                return False, textos.t("py_sessao.nada_parado_a_pedido_ou")
            motivo = self.motivo_sem_pendrive(self.modo_aplicacao)
            if motivo:
                return False, motivo
            self._continuacao, self._parado = self._parado, None
            self._em_segundo_plano(self._continuar_automatico)  # dentro da trava (B7)
        return True, "continuando"

    def _montar_log(self) -> str:
        from msscan.ia.sanitizar import nomes_do_host
        from msscan.relatorio import log_tecnico

        with self._trava:
            estado = _instantaneo({"etapa": self.etapa, "erro": self.erro, "modo": self.modo_aplicacao, "rede": self.rede_situacao,
                                   "atualizacoes": self.atualizacoes, "preparo": self.preparo, "aplicacao": self.aplicacao,
                                   "retomada": self.retomada, "acao": self.acao, "temporarias_desfeitas": self.temporarias_desfeitas,
                                   "temporarias_registro": _registro_temporarias(self.pasta_dados),
                                   "acesso_idrac": self.acesso_idrac,
                                   "racadm": {k: ((self.ctx.cache if self.ctx else {}).get("idrac_tools") or {}).get(k)
                                              for k in ("situacao", "pacote", "mensagem", "passos")},
                                   "plano": {k: (self.aviso or {}).get(k) for k in ("situacao", "estimativa", "hardware", "bloqueios", "temporarias")}})  # fmt: skip
            relatorio = _instantaneo(self.relatorio) if self.relatorio else None
        return log_tecnico.montar(__version__, relatorio, self.executor.historico_dict(), estado,
                                  [] if self.replay else nomes_do_host(self.executor))  # fmt: skip

    def desfazer_temporarias(self) -> None:
        """Desfaz as mudanças temporárias no iDRAC (CLAUDE.md 18.4): ao terminar, antes de desligar/reiniciar e, no boot
        seguinte, o que ficou de uma sessão que fechou ou caiu (registro no pendrive). Relatório. Nunca levanta exceção."""
        from msscan.acoes import idrac
        from msscan.atualizacao import temporarias as temp

        pasta = self.pasta_dados
        if self.replay or self.ctx is None or not (self.executor.slots_temporarios or idrac.ha_marcador(pasta) or temp.ha_pendentes(pasta)):
            return
        try:
            with self._escrita():
                mudancas = temp.desfazer_tudo(self.ctx, self.executor, pasta, espera_job_s=0)
                if self.executor.slots_temporarios or idrac.ha_marcador(pasta):  # usuário de uma versão anterior (ação avulsa)
                    idrac.apagar(self.ctx, self.executor, pasta)
                with self._trava:
                    self.temporarias_desfeitas += mudancas
                if self.relatorio is not None:
                    self.relatorio["temporarias_desfeitas"] = list(self.temporarias_desfeitas)
                    self._regravar(self.relatorio)
        except Exception:  # noqa: BLE001 - nunca impede desligar/encerrar; o registro fica para o próximo boot
            pass

    def _ler_jobs_idrac(self) -> None:
        """21.4: ao iniciar, a fila de jobs do iDRAC (Redfish ou racadm, só leitura) vai para a tela e o relatório."""
        from msscan.atualizacao.aviso import fila_do_idrac

        if self.ctx is None or self.replay or not _dell(self.ctx):
            return
        primeira, self._jobs_lidos = not self._jobs_lidos, True
        fila = self._marcar_orfaos(fila_do_idrac(self.ctx), primeira=primeira)
        with self._trava:
            self.jobs_idrac = fila
            if self.relatorio is not None:
                self.relatorio["jobs_idrac"] = fila

    def _marcar_orfaos(self, fila: dict[str, Any], primeira: bool = False) -> dict[str, Any]:
        from msscan.atualizacao import jobs as fila_jobs
        from msscan.atualizacao.repositorio import ServidorRepositorio

        return fila_jobs.marcar_orfaos(fila, self._jobs_vistos, time.monotonic(), ServidorRepositorio.ativos > 0,
                                       primeira_leitura=primeira)  # fmt: skip

    def reler_jobs_idrac(self) -> bool:
        """Botão "Atualizar fila": relê a fila do iDRAC (só leitura) e recalcula os jobs órfãos."""
        with self._trava:
            if self._ocupado() or self.ctx is None:
                return False
            self._em_segundo_plano(self._ler_jobs_idrac)
        return True

    def limpar_job_orfao(self, job: str, forcar: bool) -> tuple[bool, str]:
        """Botão "Limpar job travado" (09/10/2026): só um job marcado como órfão; ``forcar`` depois do aviso de que a fila
        inteira será apagada. Tudo vai para o relatório e o log (``relatorio["jobs_limpeza"]``)."""
        from msscan.atualizacao import jobs as fila_jobs
        from msscan.atualizacao.aviso import fila_do_idrac

        with self._trava:
            if self._ocupado() or self.ctx is None or self.relatorio is None:
                return False, textos.t("py_sessao.outra_tarefa_esta_em_andamento")
            alvo = next((j for j in (self.jobs_idrac or {}).get("jobs") or [] if j.get("id") == job), None)
            if alvo is None or not alvo.get("orfao"):
                return False, textos.t("py_sessao.este_job_nao_esta_marcado")
            ctx, relatorio = self.ctx, self.relatorio
            self.limpeza_jobs = {"situacao": "executando", "job": job, "forcada": forcar,
                                 "mensagem": textos.t("py_sessao.limpando_a_fila_de_tarefas")}  # fmt: skip

            def executar() -> None:
                with gravando(textos.t("py_sessao.fila_de_tarefas_do_idrac")):
                    registro = fila_jobs.limpar_orfao(ctx, job, forcar, time.sleep)
                fila = self._marcar_orfaos(fila_do_idrac(ctx))
                with self._trava:
                    self.limpeza_jobs = registro
                    self.jobs_idrac = fila
                    relatorio.setdefault("jobs_limpeza", []).append(registro)
                    relatorio["jobs_idrac"] = fila
                self._regravar(relatorio)

            self._em_segundo_plano(executar)
        return True, "limpando"

    def _continuar_automatico(self) -> None:
        """Volta do reinício de um plano confirmado: relê o hardware e continua os itens pendentes sem perguntar (18.2)."""
        from msscan.atualizacao.automatica import continuar
        from msscan.atualizacao.aviso import conferir_hardware

        with self._trava:
            estado, self._continuacao = self._continuacao, None
            relatorio, ctx = self.relatorio, self.ctx
        if estado is None or relatorio is None or ctx is None or self.replay:
            return
        parcial = self._novo_acompanhamento(estado.get("pendentes") or [])
        with self._trava:
            self.aplicacao = {"situacao": "executando", "mensagem": textos.t("py_sessao.continuando_a_atualizacao_confirmada_antes"),
                              "resultado": parcial, "itens": [{"componente": i.get("componente"), "atual": i.get("instalada"),
                                                               "nova": i.get("disponivel")} for i in estado.get("pendentes") or []]}  # fmt: skip
        hardware = conferir_hardware(ctx, self.registro)
        with gravando("firmware (retomada)"), self._escrita():
            resultado = continuar(ctx, self.executor, str(self.pasta_dados), estado, hardware, parcial=parcial)
            self.antes_depois(ctx, resultado)
            relatorio["aplicacao"] = resultado
            self._regravar(relatorio)
        self._fim_aplicacao(resultado)

    def _fim_aplicacao(self, resultado: dict[str, Any]) -> None:
        """Parado nesta sessão (a pedido ou o iDRAC sem voltar, 09/10/2026): "Continuar de onde parou" já fica disponível,
        sem reiniciar; a versão do que já foi aplicado é conferida antes do próximo envio (``_aplicados_conferidos``)."""
        estado = ler_estado(self.executor, self.pasta_dados) if resultado["situacao"] == "parado" else None
        with self._trava:
            self.aplicacao = {"situacao": resultado["situacao"], "mensagem": resultado["mensagem"], "resultado": resultado}
            # Agendado só se aplica num reinício: aí o botão aparece na volta (_retomar), como antes.
            if (estado is not None and estado.get("situacao") == "parado"
                    and all(e.get("situacao") == "aplicado" for e in estado.get("enviados") or [])):  # fmt: skip
                self._parado = estado
                self.retomada = {**(self.retomada or {}), "pode_continuar_parado": True,
                                 "pendentes": [p.get("componente") for p in estado.get("pendentes") or []]}  # fmt: skip

    def encerrar(self) -> None:
        """Logout da sessão Redfish ao terminar (não ocupa o limite de sessões do BMC) e para o monitor da rede."""
        self.desativar_ssh()
        self.desfazer_temporarias()
        if self.pareamento is not None:
            self.pareamento.encerrar()
        self.manual.encerrar()
        if self.monitor is not None:
            self.monitor.parar()
        if self.ctx is not None and self.ctx.bmc is not None:
            self.ctx.bmc.encerrar()

    def _estado_bmc(self) -> dict[str, Any] | None:
        if self.ctx is None or self.ctx.bmc is None:
            return None
        return {**self.ctx.bmc.para_dict(), "validando": self.bmc_validando, "pode_tentar": self.ctx.bmc.pode_tentar}

    # Energia (CLAUDE.md 15.5) ------------------------------------------------

    def _motivo_energia_bloqueada(self) -> str | None:
        """Desligar/Reiniciar ficam bloqueados enquanto algo grava no firmware ou altera o BMC."""
        if em_gravacao():
            return textos.t("py_sessao.gravacao_de_firmware_ou_acao")
        with self._trava:
            if (self.aplicacao or {}).get("situacao") == "executando":
                return textos.t("py_sessao.atualizacao_de_firmware_em_andamento")
            if (self.acao or {}).get("situacao") == "executando":
                return textos.t("py_sessao.acao_no_bmc_em_andamento")
        return None

    def energia(self, acao: str) -> tuple[bool, str]:
        """Desliga ou reinicia: logout do BMC, ``sync`` e desmontagem segura do pendrive antes. Devolve (ok, mensagem)."""
        if acao not in ("desligar", "reiniciar"):
            return False, textos.t("py_sessao.acao_desconhecida")
        motivo = self._motivo_energia_bloqueada()
        if motivo:
            return False, motivo
        if self.replay:
            return False, textos.t("py_sessao.modo_replay_o_computador_nao")
        self.desativar_ssh()  # 22.5: registrado no relatório antes de desmontar o pendrive
        self.desfazer_temporarias()  # antes do logout: o apagamento encerra a sessão dele
        if self.ctx is not None and self.ctx.bmc is not None:
            self.ctx.bmc.encerrar()  # não deixa a sessão Redfish ocupando o limite do BMC
        self.executor.rodar(["sync"], timeout=120)
        rc, _, erro = self.executor.rodar([DESMONTAR_PENDRIVE], timeout=90)
        if rc != 0:
            return False, textos.t("py_sessao.nao_foi_possivel_desmontar_o", erro.strip() or rc)
        rc, _, erro = self.executor.rodar(["systemctl", "poweroff" if acao == "desligar" else "reboot"], timeout=60)
        if rc != 0:
            return False, textos.t("py_sessao.o_sistema_nao_aceitou_o", erro.strip() or rc)
        return True, "Desligando..." if acao == "desligar" else "Reiniciando..."

    # Leitura do estado -----------------------------------------------------

    def visao(self) -> dict[str, Any]:
        """Visão do servidor e sensores ao vivo (nova interface, 09/10/2026): só o que já foi lido, mais um ``ipmitool
        sensor`` de vez em quando. Nunca levanta exceção."""
        from msscan.interface import visao as vis

        with self._trava:
            ctx, resultados = self.ctx, dict(self.resultados)
        if ctx is None:
            return {"servidor": None, "sensores": None}
        return {"servidor": self.visao_servidor(), "sensores": self._sensores_ao_vivo(ctx, resultados)}

    def visao_servidor(self) -> dict[str, Any] | None:
        """Só a identidade e o que o diagnóstico leu (sem leitura ao vivo): também usada pelo ``msscan-fetch``."""
        from msscan.interface import visao as vis

        with self._trava:
            ctx, resultados, config = self.ctx, dict(self.resultados), (self.relatorio or {}).get("configuracao")
        if ctx is None:
            return None
        if self._uptime is None:
            self._uptime = (vis.tempo_ligado(self.executor.ler_arquivo("/proc/uptime")), time.monotonic())
        base, inicio = self._uptime
        return vis.montar(ctx, resultados, config, base + (time.monotonic() - inicio) if base is not None else None)

    def _sensores_ao_vivo(self, ctx: Contexto, resultados: dict[str, Any]) -> dict[str, Any] | None:
        """Releitura do ``ipmitool sensor`` (só leitura, fora do histórico) no máximo a cada 5 s; pausada durante a gravação
        de firmware, ações no controlador e o próprio diagnóstico. Sem leitura ao vivo: os valores do diagnóstico."""
        from msscan.interface import visao as vis
        from msscan.parsers import ErroParser
        from msscan.parsers.ipmitool import parse_sensor

        pausa = self._motivo_energia_bloqueada() or (textos.t("visao.pausa_diagnostico") if self.etapa == DIAGNOSTICO else None)
        agora = time.monotonic()
        if (not pausa and ctx.ipmi.disponivel and agora - self._sensores_lidos >= vis.INTERVALO_SENSORES_S
                and self._trava_sensores.acquire(blocking=False)):  # fmt: skip
            try:
                rc, saida, _ = self.executor.rodar(["ipmitool", "sensor"], timeout=20, registrar=False)
                if rc == 0:
                    self._sensores = {**vis.sensores(parse_sensor(saida)), "origem": "ao_vivo", "lido": datetime.now().isoformat(timespec="seconds")}
            except ErroParser:
                pass
            finally:
                self._sensores_lidos = time.monotonic()
                self._trava_sensores.release()
        if self._sensores is not None:
            return {**self._sensores, "pausa": pausa}
        lista = (getattr(resultados.get("sensores"), "dados", None) or {}).get("sensores") or []
        return {**vis.sensores(lista), "origem": "diagnostico", "lido": None, "pausa": pausa} if lista else None

    # Aba Discos (CLAUDE.md 31) ----------------------------------------------

    def _discos_diagnosticados(self) -> list[dict[str, Any]]:
        with self._trava:
            resultado = self.resultados.get("discos")
        return [d for d in (getattr(resultado, "dados", None) or {}).get("discos") or [] if d.get("dispositivo")]

    def _pausa_discos(self) -> str | None:
        return self._motivo_energia_bloqueada() or (textos.t("visao.pausa_diagnostico") if self.etapa == DIAGNOSTICO else None)

    def discos(self) -> dict[str, Any]:
        """Painel da aba Discos: o que o diagnóstico leu + a releitura ao vivo (só enquanto a aba pede). Nunca levanta exceção."""
        from msscan import discos as dsc

        lista = self._discos_diagnosticados()
        pausa = self._pausa_discos()
        if lista and not pausa and time.monotonic() - self._discos_lidos >= INTERVALO_DISCOS_S and not self._discos_lendo:
            self._discos_lendo = True
            threading.Thread(target=self._reler_discos, args=(lista,), name="msscan-discos", daemon=True).start()
        with self._trava:
            vivo, historico = dict(self._discos_vivo), {k: list(v) for k, v in self._discos_historico.items()}
            ignorados = ((getattr(self.resultados.get("discos"), "dados", None) or {}).get("ignorados") or [])
        cartoes = [dsc.painel({**d, **vivo.get(dsc.identificador(d), {})}, historico.get(dsc.identificador(d))) for d in lista]
        return {"discos": cartoes, "ignorados": ignorados, "pausa": pausa, "intervalo_s": INTERVALO_DISCOS_S,
                "demonstracao": self.replay}  # fmt: skip

    def _reler_discos(self, lista: list[dict[str, Any]]) -> None:
        """O mesmo ``smartctl -a -j`` do diagnóstico, fora do histórico: temperatura, horas e andamento do autoteste."""
        from msscan import discos as dsc
        from msscan.parsers.smartctl import parse_smart

        try:
            for disco in lista:
                if disco.get("status") == "NAO_VERIFICADO" or self._pausa_discos():
                    continue
                tipo, nome = disco.get("tipo"), disco["dispositivo"]
                comando = ["smartctl", "-a", "-j", "-d", tipo, nome] if tipo else ["smartctl", "-a", "-j", nome]
                _, saida, _ = self.executor.rodar(comando, timeout=60, registrar=False)
                try:
                    novo = parse_smart(saida)
                except ErroParser:
                    continue
                chave, agora = dsc.identificador(disco), datetime.now().isoformat(timespec="seconds")
                with self._trava:
                    self._discos_vivo[chave] = {c: novo.get(c) for c in CAMPOS_AO_VIVO_DISCO}
                    temperatura = novo.get("temperatura")
                    if isinstance(temperatura, (int, float)):
                        pontos = self._discos_historico.setdefault(chave, [])
                        pontos.append([agora, temperatura])
                        del pontos[:-PONTOS_TEMPERATURA_DISCO]
        except Exception:  # noqa: BLE001 - leitura ao vivo nunca derruba a sessão
            pass
        finally:
            self._discos_lidos = time.monotonic()
            self._discos_lendo = False

    def autoteste_disco(self, identificador: str, tipo: str | None) -> tuple[bool, str]:
        """Inicia (``tipo`` curto/estendido) ou interrompe (``tipo=None``) o autoteste de um disco JÁ diagnosticado, achado pelo
        id (nunca um caminho vindo da interface). A confirmação fica na interface; aqui, as salvaguardas."""
        from msscan import discos as dsc

        if self.replay:
            return False, textos.t("discos.indisponivel_demonstracao")
        pausa = self._pausa_discos()
        if pausa:
            return False, pausa
        disco = next((d for d in self._discos_diagnosticados() if dsc.identificador(d) == identificador), None)
        if disco is None or disco.get("status") == "NAO_VERIFICADO":
            return False, textos.t("discos.disco_nao_verificado")
        with self._trava:
            atual = {**disco, **self._discos_vivo.get(identificador, {})}.get("autoteste") or {}
        if tipo is not None and not atual.get("suportado"):
            return False, textos.t("discos.autoteste_nao_suportado")
        if tipo is not None and atual.get("em_andamento"):
            return False, textos.t("discos.autoteste_ja_rodando")
        comando = dsc.comando_autoteste(disco, tipo)
        if comando is None:
            return False, textos.t("discos.disco_nao_verificado")
        rc, saida, erro = self.executor.autoteste_disco(comando)
        self._discos_lidos = 0.0  # a próxima consulta da aba já relê o andamento
        if rc != 0:
            linhas = (erro or saida).strip().splitlines()
            return False, textos.t("discos.autoteste_falhou", linhas[-1] if linhas else rc)
        return True, textos.t("discos.autoteste_interrompido" if tipo is None else "discos.autoteste_iniciado")

    def estado(self) -> dict[str, Any]:
        """Estado resumido para a interface (consultado a cada ~0,5 s)."""
        with self._trava:
            return {
                "versao": __version__,
                "modo": self.executor.origem,
                "energia": {"bloqueio": self._motivo_energia_bloqueada(), "disponivel": not self.replay},
                "etapa": self.etapa,
                "erro": self.erro,
                "rede": self._estado_rede(),
                "equipamento": self._estado_equipamento(),
                "diagnostico": self._estado_diagnostico(),
                "relatorio": self._estado_relatorio(),
                "configuracao": (self.relatorio or {}).get("configuracao"),
                "bmc": self._estado_bmc(),
                "ssh": self._estado_ssh(),
                "acesso_idrac": {**self.acesso_idrac, "verificando": self.acesso_verificando,
                                 "mensagens": _mensagens_temporarias(self.temporarias_desfeitas)
                                 + list(self.acesso_idrac.get("mensagens") or [])} if self.acesso_idrac else None,  # fmt: skip
                "ia": self._estado_ia(),
                "atualizacoes": self._estado_atualizacoes(),
                "acoes": self._estado_acoes(),
                "chat": self._estado_chat(),
                "ia_celular": self._estado_celular(),
                "coleta": dict(self.coleta),
            }

    def _estado_rede(self) -> dict[str, Any]:
        r = self.rede
        return {
            "situacao": self.rede_situacao,
            "espera_s": self._espera_rede,
            "titulo": r.titulo if r else "",
            "causa": r.causa if r else "",
            "precisa_hora": bool(r and r.precisa_hora and self.rede_situacao == "sem_internet"),
            "hora_sistema": ((r.relogio or {}).get("hora_sistema") or "") if r else "",
            "relogio": (r.relogio or {}).get("mensagem", "") if r else "",
            "aviso_relogio": _aviso_relogio(r.relogio if r else None),
            "detalhes": list(r.detalhes) if r else [],
            "niveis": niveis_rede(r) if r and not r.sem_rede_por_parametro else [],
            "sem_rede_por_parametro": bool(r and r.sem_rede_por_parametro),
            "continuo": self.monitor.estado() if self.monitor is not None else None,
        }

    def _estado_equipamento(self) -> dict[str, Any] | None:
        if self.ctx is None:
            return None
        return {
            "fabricante": self.ctx.fabricante,
            "modelo": self.ctx.modelo,
            "versao_bios": self.ctx.versao_bios,
            "capacidades": capacidades(self.ctx),
            "pendrive": self.ctx.pendrive,
            "erros_deteccao": dict(self.ctx.erros_deteccao),
        }

    def _estado_diagnostico(self) -> dict[str, Any]:
        componentes = []
        for comp in self.registro.componentes():
            r = self.resultados.get(comp.nome)
            componentes.append(
                {
                    "componente": comp.nome,
                    "titulo": comp.titulo,
                    "status": r.status.value if r else None,
                    "fonte": r.fonte if r else "",
                    "mensagem": r.mensagem if r else "",
                    "em_andamento": comp.nome == self.atual,
                }
            )
        concluidos = [self.resultados[c.nome] for c in self.registro.componentes() if c.nome in self.resultados]
        return {
            "total": len(componentes),
            "concluidos": len(concluidos),
            "componentes": componentes,
            "veredito": veredito(concluidos) if self.etapa == CONCLUIDO else "",
            "comparacao": (self.relatorio or {}).get("comparacao") if self.etapa == CONCLUIDO else None,
        }

    def _estado_relatorio(self) -> dict[str, Any] | None:
        if self.relatorio is None or self.gravacao is None:
            return None
        return {**self.gravacao, "veredito": self.relatorio["veredito"], "contagem": self.relatorio["contagem"],
                "localizacao": self.relatorio.get("localizacao"), "modelo3d": self.relatorio.get("modelo3d"),
                "estado_geral": self.relatorio.get("estado_geral")}  # fmt: skip

    def componente(self, nome: str) -> dict[str, Any] | None:
        """Detalhes de um componente: resultado completo e comandos executados nele."""
        with self._trava:
            resultado = self.resultados.get(nome)
            if resultado is None:
                return None
            return {
                **resultado.para_dict(),
                "titulo": self.registro.titulo(nome),
                "comandos": list(self.comandos.get(nome, [])),
            }

    def html_relatorio(self) -> str | None:
        """O mesmo relatório HTML gravado no pendrive (gerado a partir do JSON completo)."""
        with self._trava:
            return gerar_html(self.relatorio) if self.relatorio is not None else None


def _mensagens_temporarias(mudancas: list[dict[str, Any]]) -> list[dict[str, str]]:
    """22.4: o que foi desfeito no iDRAC (sobras de sessões anteriores e desta), na caixa "Acesso ao iDRAC"."""
    return [{"nivel": "ok" if m.get("situacao") == "desfeita" else "alerta",
             "texto": textos.t("py_sessao.mudanca_temporaria_no_idrac_0", m.get('descricao'), m.get('texto') or m.get('situacao'))
                      + (f" ({m['detalhe']})" if m.get("detalhe") else "")} for m in mudancas]  # fmt: skip


def _registro_temporarias(pasta: str | None) -> list[dict[str, Any]]:
    """22.4: cada mudança temporária feita e ainda não desfeita (``estado/mudancas_temporarias.json``), para o log técnico."""
    from msscan.atualizacao import temporarias as temp

    try:
        return temp.Temporarias(pasta).mudancas
    except Exception:  # noqa: BLE001 - o log nunca falha por isso
        return []


def _formatar_ssh(ativacoes: list[dict[str, Any]] | None) -> str:
    """22.5: relatório TXT — quando o SSH foi ativado/desativado e quem conectou (sem senha)."""
    if not ativacoes:
        return ""
    linhas = ["", textos.t("py_sessao.acesso_ssh_ao_msscan_sem")]
    for a in ativacoes:
        linhas.append(textos.t("py_sessao.ativado_em_0_1_desativado", a['ativado'], a.get('origem') or textos.t('py_sessao.botao'), a.get('desativado') or '(ativo)', ', '.join(a.get('redes') or []) or '-', a.get('chaves', 0)))
        for c in a.get("conexoes") or []:
            linhas.append(textos.t("py_sessao.conexao_em_0_de_1", c['horario'], c['ip'], c['metodo']))
        if not a.get("conexoes"):
            linhas.append(textos.t("py_sessao.nenhuma_conexao_registrada"))
    return "\n".join(linhas)
