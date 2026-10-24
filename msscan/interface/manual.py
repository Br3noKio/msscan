"""Atualização manual na interface (fase 11, CLAUDE.md 11b): navegador integrado, downloads e assistente.

Fica fora da ``Sessao`` (que já concentra o resto do fluxo), mas usa a mesma trava, a mesma janela de escrita do
pendrive e o mesmo resultado de aplicação (``relatorio["aplicacao"]``) da fase 9.
"""

from __future__ import annotations

from msscan import textos

import threading
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from msscan.acoes.comum import copiar_conferido, no_pendrive
from msscan.core.gravando import gravando
from msscan.atualizacao.aviso import assinatura_manual
from msscan.atualizacao import aplicacao_manual as fluxo
from msscan.atualizacao import manual
from msscan.atualizacao.aplicacao import ler_bmc
from msscan.atualizacao.catalogo import catalogo_em_cache
from msscan.atualizacao.inventario import coletar_inventario
from msscan.atualizacao.verificacao import prechecagens
from msscan.navegador.navegador import Navegador
from msscan.navegador.proxy import ProxyOficial

if TYPE_CHECKING:
    from msscan.interface.sessao import Sessao

SEM_NAVEGADOR = (textos.t("py_manual.o_navegador_integrado_so_existe"))  # fmt: skip


class AssistenteManual:
    """Estado do navegador e dos pacotes manuais desta sessão (um assistente por pacote)."""

    def __init__(self, sessao: Sessao, navegador_ativo: bool) -> None:
        self.s = sessao
        self.navegador_ativo = navegador_ativo
        self.navegador: Navegador | None = None
        self.proxy: ProxyOficial | None = None
        self.pacotes: dict[str, dict[str, Any]] = {}
        self._contador = 0
        self.mensagem = ""
        self._inventario: list[dict[str, Any]] | None = None
        self._bmc: dict[str, Any] | None = None
        self._trava = threading.RLock()

    # Dados do equipamento (lidos uma vez, só leitura) ----------------------------------------------

    def _pasta(self) -> Path | None:
        ctx = self.s.ctx
        if ctx is None or not self.s.pasta_dados:
            return None
        return Path(self.s.pasta_dados) / manual.pasta_destino(ctx.fabricante)

    def _dados(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if self._inventario is None:
            self._inventario = coletar_inventario(self.s.ctx)[0]
            self._bmc = ler_bmc(self.s.ctx)
        return self._inventario, self._bmc or {}

    def _motivo(self) -> str:
        if self.s.ctx is None or self.s.relatorio is None:
            return textos.t("py_manual.a_atualizacao_manual_fica_disponivel")
        return ""

    # Navegador ------------------------------------------------------------------------------------

    def _service_tag(self) -> str | None:
        """Só para a URL da Dell: lido na hora, nunca guardado no contexto, no relatório nem enviado à IA."""
        if not (self.s.ctx.fabricante or "").lower().startswith("dell"):
            return None
        rc, saida, _ = self.s.executor.rodar(["dmidecode", "-s", "system-serial-number"], timeout=15)
        return saida.strip() if rc == 0 else None

    def abrir_navegador(self) -> tuple[bool, str]:
        motivo = self._motivo()
        if motivo:
            return False, motivo
        pasta = self._pasta()
        if not self.navegador_ativo:
            return False, SEM_NAVEGADOR.format(pasta=no_pendrive(pasta, self.s.pasta_dados) if pasta else "MSSCAN/firmware/manual")
        rede = self.s.rede
        if not (rede and rede.ok):
            return False, textos.t("py_manual.sem_acesso_a_internet_o")
        with self._trava:
            if self.navegador is not None and self.navegador.aberto:
                return self.navegador.mostrar(), textos.t("py_manual.navegador_aberto")
            ctx = self.s.ctx
            pagina = manual.url_drivers(ctx.fabricante, ctx.modelo, self._service_tag())
            if not pagina["url"]:
                return False, pagina["explicacao"]
            if self.proxy is None:
                self.proxy = ProxyOficial()
                try:
                    self.proxy.iniciar()
                except OSError as erro:
                    self.proxy = None
                    return False, textos.t("py_manual.nao_foi_possivel_iniciar_o", erro)
            self.navegador = self.navegador or Navegador(self.s.executor)
            ok, mensagem = self.navegador.abrir(pagina["url"], self._baixado)
            self.mensagem = pagina["explicacao"] if ok else mensagem
            return ok, self.mensagem

    def voltar(self) -> bool:
        return self.navegador is not None and self.navegador.voltar()

    def fechar_navegador(self) -> bool:
        if self.navegador is None:
            return False
        self.navegador.fechar()
        return True

    def encerrar(self) -> None:
        if self.navegador is not None:
            self.navegador.fechar()
        if self.proxy is not None:
            self.proxy.encerrar()

    # Pacotes --------------------------------------------------------------------------------------

    def _baixado(self, origem: Path) -> None:
        """Download terminado no navegador: copia (com conferência) para o pendrive e abre o assistente."""
        pasta = self._pasta()
        if pasta is None:
            return
        destino = pasta / origem.name
        numero = 1
        while destino.exists():
            destino = pasta / f"{origem.stem}_{numero}{origem.suffix}"
            numero += 1
        try:
            with self.s._escrita():
                copiar_conferido(origem, destino)
        except (OSError, ValueError) as erro:
            with self._trava:
                self.mensagem = textos.t("py_manual.0_nao_foi_possivel_copiar", origem.name, erro)
            return
        finally:
            origem.unlink(missing_ok=True)
        with self._trava:
            self.mensagem = textos.t("py_manual.0_baixado_e_copiado_para", origem.name, no_pendrive(destino, self.s.pasta_dados))
        self._analisar(destino)

    def procurar(self) -> None:
        """Botão "Atualizar": pacotes na pasta do pendrive (baixados agora, copiados de outro computador ou antes);
        pacote cujo arquivo sumiu da pasta sai da lista (exceto o que já foi instalado: fica o registro)."""
        pasta = self._pasta()
        if pasta is None or self._motivo():
            return
        try:
            arquivos = sorted(p for p in pasta.iterdir() if p.is_file() and not p.is_symlink() and p.suffix.lower() in manual.EXTENSOES)
        except OSError:
            return
        with self._trava:
            for identificador, pacote in list(self.pacotes.items()):
                if pacote["etapa"] != "instalado" and not Path(pacote["arquivo"]).is_file():
                    del self.pacotes[identificador]
        conhecidos = {p["arquivo"] for p in self.pacotes.values()}
        for arquivo in arquivos:
            if str(arquivo) not in conhecidos:
                self._analisar(arquivo)

    # Pasta de downloads (CLAUDE.md 15.2): listar e apagar, SÓ dentro de MSSCAN/firmware/manual/ ------------------------

    def arquivos(self) -> dict[str, Any]:
        """Arquivos da pasta (nome, tamanho, data, se já foi usado). Só arquivos comuns, direto na pasta."""
        pasta = self._pasta()
        if pasta is None or self._motivo():
            return {"pasta": "", "arquivos": [], "motivo": self._motivo() or textos.t("py_manual.sem_local_de_dados")}
        with self._trava:
            uso = {p["arquivo"]: p for p in self.pacotes.values()}
        lista = []
        try:
            entradas = sorted(pasta.iterdir()) if pasta.is_dir() else []
        except OSError as erro:
            return {"pasta": no_pendrive(pasta, self.s.pasta_dados), "arquivos": [], "motivo": textos.t("py_manual.pasta_ilegivel_0", erro)}
        for caminho in entradas:
            if caminho.is_symlink() or not caminho.is_file():
                continue
            info = caminho.stat()
            pacote = uso.get(str(caminho))
            usado = {"instalado": "sim (instalado)", "pronto": textos.t("py_manual.conferido_nao_instalado"), "recusado": "recusado"}.get(
                (pacote or {}).get("etapa", ""), textos.t("py_manual.analisado_nao_instalado") if pacote else "não")  # fmt: skip
            lista.append({"nome": caminho.name, "tamanho": info.st_size,
                          "data": datetime.fromtimestamp(info.st_mtime).isoformat(timespec="minutes"), "usado": usado})  # fmt: skip
        return {"pasta": no_pendrive(pasta, self.s.pasta_dados), "arquivos": lista, "motivo": ""}

    def apagar(self, nome: str, confirmado: bool) -> tuple[bool, str]:
        """Apaga UM arquivo da pasta de downloads manuais, com confirmação. Nome simples (sem caminho), arquivo comum
        direto na pasta, fora de uso numa instalação em andamento."""
        pasta = self._pasta()
        if pasta is None or self._motivo():
            return False, self._motivo() or textos.t("py_manual.sem_local_de_dados")
        if not confirmado:
            return False, textos.t("py_manual.confirme_para_apagar")
        if not isinstance(nome, str) or not nome or nome != Path(nome).name or nome.startswith(".") or "\x00" in nome:
            return False, textos.t("py_manual.nome_de_arquivo_invalido")
        alvo = pasta / nome
        try:
            if alvo.is_symlink() or not alvo.is_file() or alvo.resolve().parent != pasta.resolve():
                return False, textos.t("py_manual.arquivo_nao_encontrado_na_pasta")
        except OSError:
            return False, textos.t("py_manual.arquivo_nao_encontrado_na_pasta")
        with self.s._trava:
            if self.s._ocupado() and (self.s.aplicacao or {}).get("situacao") == "executando":
                return False, textos.t("py_manual.ha_uma_instalacao_em_andamento")
        try:
            with self.s._escrita():
                alvo.unlink()
        except OSError as erro:
            return False, textos.t("py_manual.nao_foi_possivel_apagar_0", erro)
        with self._trava:
            for identificador, pacote in list(self.pacotes.items()):
                if pacote.get("arquivo") == str(alvo) and pacote["etapa"] != "instalado":
                    del self.pacotes[identificador]
            self.mensagem = textos.t("py_manual.0_apagado_de_1", nome, no_pendrive(pasta, self.s.pasta_dados))
        return True, self.mensagem

    def _analisar(self, arquivo: Path) -> None:
        inventario, bmc = self._dados()
        analise = fluxo.analisar(arquivo, self.s.ctx.sku, inventario, bmc, catalogo_em_cache(self.s.pasta_dados))
        analise["local"] = no_pendrive(arquivo, self.s.pasta_dados)
        with self._trava:
            self._contador += 1  # nunca reusa um id (apagar um pacote não faz o próximo herdar o aviso dele)
            identificador = f"p{self._contador}"
            self.pacotes[identificador] = {**analise, "id": identificador}

    def _pacote(self, identificador: str) -> dict[str, Any] | None:
        with self._trava:
            return self.pacotes.get(identificador)

    def informar_sha(self, identificador: str, texto: str) -> tuple[bool, str]:
        pacote = self._pacote(identificador)
        if pacote is None or pacote["etapa"] != "sha":
            return False, textos.t("py_manual.pacote_desconhecido_ou_fora_da")
        with self.s._escrita():  # SHA divergente apaga o arquivo do pendrive
            novo = fluxo.conferir_sha(pacote, texto)
        with self._trava:
            self.pacotes[identificador] = novo
        if novo["etapa"] == "checagem":
            self._checar(identificador)
        return novo["etapa"] != "sha", novo["motivo"]

    def _checar(self, identificador: str) -> None:
        """``DUP -c`` pode levar minutos: em segundo plano, com a etapa "checando" na tela."""
        with self._trava:
            pacote = self.pacotes[identificador]
            self.pacotes[identificador] = {**pacote, "etapa": "checando", "motivo": textos.t("py_manual.checando_o_pacote_dup_c")}

        def checar() -> None:
            novo = fluxo.checar(pacote, self.s.executor)
            with self._trava:
                self.pacotes[identificador] = novo

        threading.Thread(target=checar, daemon=True, name="msscan-dup-c").start()

    def instalar(self, identificador: str, confirmacao: str, confirmacao_redundancia: str,
                 confirmacao_riscos: str = "") -> tuple[bool, str]:  # fmt: skip
        """Botão "Instalar" do plano de atualização: plano, pré-checagens e confirmações conferidos AQUI; depois, tudo
        automático em segundo plano (18.2)."""
        pacote = self._pacote(identificador)
        if pacote is None or pacote["etapa"] != "pronto":
            return False, textos.t("py_manual.o_pacote_ainda_nao_passou")
        s = self.s

        def executar() -> None:
            inventario, bmc = self._dados()  # na thread: a tarefa começa dentro da trava (revisão de 06/10/2026, B7)
            parcial = s._novo_acompanhamento([])  # o componente entra na primeira etapa
            parcial["riscos_assumidos"] = aviso["riscos_assumidos"]
            with s._trava:
                s.aplicacao = {**s.aplicacao, "resultado": parcial}  # progresso ao vivo
            with gravando(textos.t("py_manual.firmware_pacote_manual")), s._escrita():
                resultado = fluxo.instalar(pacote, ctx, s.executor, pre, confirmacao, confirmacao_redundancia, s.pasta_dados,
                                           modo, inventario, bmc, parcial=parcial)  # fmt: skip
                s.antes_depois(ctx, resultado)
                relatorio["aplicacao"] = resultado
                s._regravar(relatorio)
            with s._trava:
                s.aplicacao = {"situacao": resultado["situacao"], "mensagem": resultado["mensagem"], "resultado": resultado}
            with self._trava:
                self.pacotes[identificador] = {**pacote, "etapa": "instalado", "motivo": resultado["mensagem"]}

        with s._trava:
            if s._ocupado() or s.relatorio is None or s.ctx is None:
                return False, textos.t("py_manual.indisponivel_agora_outra_tarefa_rodando")
            pre = prechecagens(s.relatorio, s.ctx.compat)
            if pre["bloqueios"]:
                return False, " ".join(pre["bloqueios"])
            # Redundância perdida pelo diagnóstico OU pela releitura da tela de aviso: confirmação extra.
            pre["redundancia_perdida"] = pre["redundancia_perdida"] or bool((s.aviso or {}).get("redundancia"))
            motivo = fluxo.conferir_confirmacoes({"confirmacao_redundancia": pre["redundancia_perdida"]}, confirmacao, confirmacao_redundancia)
            if motivo:
                return False, motivo
            motivo = s.motivo_sem_pendrive(s.modo_para())
            if motivo:
                return False, motivo
            aviso, motivo = s.aviso_para_instalar("manual", [identificador], [assinatura_manual(pacote)], confirmacao_riscos)
            if aviso is None:
                return False, motivo
            relatorio, ctx, modo = s.relatorio, s.ctx, s.modo_para()
            s.aplicacao = {"situacao": "executando", "mensagem": f"Instalando {pacote['nome']}..."}
            s._em_segundo_plano(executar)
        return True, "iniciado"

    # Estado para a tela ---------------------------------------------------------------------------

    def estado(self) -> dict[str, Any]:
        motivo = self._motivo()
        ctx = self.s.ctx
        pagina = manual.url_drivers(ctx.fabricante, ctx.modelo, None) if ctx is not None else {"tipo": "", "explicacao": ""}
        pasta = self._pasta()
        pre = prechecagens(self.s.relatorio, ctx.compat) if not motivo else {"bloqueios": [], "redundancia_perdida": False}
        with self._trava:
            pacotes = [{k: v for k, v in p.items() if k != "arquivo"} for p in self.pacotes.values()]
            return {
                "disponivel": not motivo, "motivo": motivo, "navegador_integrado": self.navegador_ativo,
                "navegador": self.navegador.estado() if self.navegador else {"aberto": False, "visivel": False},
                "pagina": {"tipo": "service_tag" if ctx is not None and (ctx.fabricante or "").lower().startswith("dell") else pagina["tipo"],
                           "fabricante": (ctx.fabricante if ctx else "") or ""},
                "pasta": no_pendrive(pasta, self.s.pasta_dados) if pasta else "",
                "mensagem": self.mensagem, "pacotes": pacotes,
                "bloqueios": pre["bloqueios"], "redundancia_perdida": pre["redundancia_perdida"],
                "dominios": self.proxy.resumo() if self.proxy else None,
            }  # fmt: skip
