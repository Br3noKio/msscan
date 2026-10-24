"""Download automático do "iDRAC Tools for Linux" (CLAUDE.md 20): hash certo, hash errado, queda no meio, sem internet,
configuração atualizável, espera da atualização e a caixa "Acesso ao iDRAC"."""

from __future__ import annotations

import contextlib
import hashlib
import threading
import time
from pathlib import Path

import pytest

from msscan.core.executor import RespostaDownload
from msscan.deteccao import idrac_tools
from tests.conftest import ExecutorFalso
from tests.test_acesso_idrac import ExecutorPreparo, _preparo

CONTEUDO = b"pacote iDRAC Tools (simulado)" * 1000
SHA = hashlib.sha256(CONTEUDO).hexdigest()
ARQUIVO = "Dell-iDRACTools-Web-LX-11.4.1.0-1685_A00.tar.gz"
URL = f"https://dl.dell.com/FOLDER14347901M/1/{ARQUIVO}"


class ExecutorDownload(ExecutorPreparo):
    """Download simulado: ``plano`` = lista de ("ok" | "queda" | "sem_rede" | "outro_conteudo") por chamada."""

    def __init__(self, plano):
        super().__init__()
        self.plano, self.chamadas = list(plano), []

    def _baixar(self, url, destino, limite_bytes, timeout):
        acao = self.plano.pop(0)
        self.chamadas.append(acao)
        parcial = Path(destino + ".parcial")
        if acao == "sem_rede":
            return RespostaDownload(url, None, erro_tipo="dns", erro="Name or service not known")
        if acao == "queda":  # metade chega e a rede cai: o .parcial fica para continuar
            parcial.write_bytes(CONTEUDO[: len(CONTEUDO) // 2])
            return RespostaDownload(url, None, erro_tipo="conexao", erro="Connection reset by peer")
        dados = b"outro conteudo" if acao == "outro_conteudo" else CONTEUDO
        ja = parcial.read_bytes() if parcial.is_file() and acao == "ok" else b""
        Path(destino).write_bytes(ja + dados[len(ja):])
        parcial.unlink(missing_ok=True)
        return RespostaDownload(url, 200, destino, len(dados), hashlib.sha256(Path(destino).read_bytes()).hexdigest())


@pytest.fixture
def config(tmp_path, monkeypatch):
    """Pendrive em tmp_path com um ferramentas_dell.yaml válido (hash do conteúdo simulado) e o download na RAM em tmp."""
    dados = tmp_path / "MSSCAN"
    (dados / "config").mkdir(parents=True)
    (dados / "config/ferramentas_dell.yaml").write_text(
        f'idrac_tools:\n  versao: "11.4.1.0"\n  arquivo: "{ARQUIVO}"\n  url: "{URL}"\n  sha256: "{SHA}"\n'
        f'  tamanho: {len(CONTEUDO)}\n  termos: "https://www.dell.com/aeula"\n', encoding="utf-8")  # fmt: skip
    monkeypatch.setattr(idrac_tools, "PASTA_DOWNLOAD", str(tmp_path / "ram"))
    return dados


def _baixar(ex, dados):
    return idrac_tools.baixar(ex, str(dados), contextlib.nullcontext)


def test_hash_certo_guarda_em_vendor(config):
    info = _baixar(ExecutorDownload(["ok"]), config)
    assert info["situacao"] == idrac_tools.PREPARADO and info["versao"] == "11.4.1.0" and info["url"] == URL
    assert (config / "vendor" / ARQUIVO).read_bytes() == CONTEUDO
    assert not any((config.parent / "ram").iterdir())  # a cópia na RAM é apagada depois de gravar no pendrive


def test_hash_errado_apaga_e_segue_sem_racadm(config):
    info = _baixar(ExecutorDownload(["outro_conteudo"]), config)
    assert info["situacao"] == idrac_tools.FALHOU and "SHA-256 diferente" in info["mensagem"] and "opção B" in info["mensagem"]
    assert not (config / "vendor" / ARQUIVO).exists() and not (config.parent / "ram" / ARQUIVO).exists()


def test_queda_no_meio_continua_de_onde_parou(config):
    ex = ExecutorDownload(["queda", "ok"])
    primeiro = _baixar(ex, config)
    assert primeiro["situacao"] == idrac_tools.SEM_INTERNET and "continua de onde parou" in primeiro["mensagem"]
    assert not (config / "vendor" / ARQUIVO).exists()
    segundo = _baixar(ex, config)  # "Tentar de novo" ou a rede de volta: retoma o .parcial, hash conferido no arquivo inteiro
    assert segundo["situacao"] == idrac_tools.PREPARADO and (config / "vendor" / ARQUIVO).read_bytes() == CONTEUDO


def test_sem_internet(config):
    info = _baixar(ExecutorDownload(["sem_rede"]), config)
    assert info["situacao"] == idrac_tools.SEM_INTERNET and not (config / "vendor").exists()


def test_configuracao_atualizavel_e_validada(config):
    cfg, _ = idrac_tools.carregar_config(str(config))
    assert cfg["sha256"] == SHA and cfg["_origem_config"].endswith("config/ferramentas_dell.yaml")
    # Inválida no pendrive (fora da Dell, versão antiga, hash ruim): vale a embutida, conferida na página da Dell.
    for ruim in (URL.replace("dl.dell.com", "exemplo.com"), ):
        (config / "config/ferramentas_dell.yaml").write_text(f'idrac_tools: {{versao: "11.4.1.0", arquivo: "{ARQUIVO}", '
                                                             f'url: "{ruim}", sha256: "{SHA}"}}', encoding="utf-8")  # fmt: skip
        cfg, _ = idrac_tools.carregar_config(str(config))
        assert cfg["_origem_config"].endswith("msscan/ferramentas_dell.yaml")
    for bloco, motivo in (({"versao": "9.4.0", "arquivo": ARQUIVO, "url": URL, "sha256": SHA}, "versão abaixo da 11"),
                          ({"versao": "11.4.1.0", "arquivo": ARQUIVO, "url": URL, "sha256": "abc"}, "SHA-256"),
                          ({"versao": "11.4.1.0", "arquivo": "x.tar.gz", "url": URL, "sha256": SHA}, "nome do arquivo")):
        assert motivo in idrac_tools.validar_config({"idrac_tools": bloco})[1]


def test_configuracao_embutida_e_a_conferida_na_dell():
    cfg, motivo = idrac_tools.carregar_config(None)
    assert motivo == "" and cfg["url"].startswith("https://dl.dell.com/") and cfg["url"].endswith(cfg["arquivo"])
    assert cfg["sha256"] == "5d6a42bc5c4c97c44b3a80431024b3ac4f9fe93a6a290291323210f560f06130" and cfg["termos"] == "https://www.dell.com/aeula"


def _ctx_download(config, plano):
    from msscan.deteccao.contexto import Contexto

    ex = _preparo({"UBUNTU24": ["srvadmin-hapi_11.4.1.0_amd64.deb"]})
    ex.diretorios[f"{config}/vendor"] = []
    ex.comandos[("dpkg-deb", "-x", f"{idrac_tools.PASTA_EXTRACAO}/iDRACTools/racadm/UBUNTU24/x86_64/srvadmin-hapi_11.4.1.0_amd64.deb", "/")] = (0, "", "")
    baixador = ExecutorDownload(plano)
    ex._baixar = baixador._baixar
    return Contexto(executor=ex, pasta_dados=str(config), fabricante="Dell Inc."), ex


def test_em_segundo_plano_prepara_o_racadm_e_avisa_os_termos(config):
    ctx, ex = _ctx_download(config, ["ok"])
    terminou = threading.Event()
    real_listar = ex._listar
    ex._listar = lambda c: [ARQUIVO] if c == f"{config}/vendor" and (config / "vendor" / ARQUIVO).exists() else real_listar(c)
    linha = idrac_tools.iniciar_download(ctx, contextlib.nullcontext, None, terminou.set)
    assert linha is not None
    linha.join(10)
    assert terminou.is_set() and ctx.cache["racadm_pendente"].is_set()
    info = ctx.cache["idrac_tools"]
    assert ctx.ferramentas_fabricante["racadm"] == idrac_tools.RACADM and info["baixado"] and info["versao"] == "11.4.1.0"
    assert info["aviso_termos"] == "iDRAC Tools baixado do site da Dell; uso sujeito aos termos da Dell: https://www.dell.com/aeula"
    assert ctx.para_dict()["idrac_tools"]["origem"] == "download automático do site da Dell"
    assert idrac_tools.iniciar_download(ctx, contextlib.nullcontext) is None  # já pronto: não baixa de novo

    from msscan.acesso_idrac import avaliar, ler

    estado = avaliar(ler(ctx))
    assert estado["racadm_texto"] == "pronto (versão 11.4.1.0)" and any("termos da Dell" in m["texto"] for m in estado["mensagens"])


def test_nao_baixa_com_pacote_no_pendrive_nem_em_replay(config):
    ctx, ex = _ctx_download(config, ["ok"])
    ex.diretorios[f"{config}/vendor"] = [ARQUIVO]
    assert not idrac_tools.precisa_baixar(ctx)
    from msscan.deteccao.contexto import Contexto

    assert not idrac_tools.precisa_baixar(Contexto(executor=ExecutorFalso(), pasta_dados=str(config)))


def test_atualizacao_espera_o_download_em_vez_de_falhar():
    from msscan.atualizacao import automatica
    from msscan.deteccao.contexto import Contexto

    ctx = Contexto(executor=ExecutorFalso())
    pendente = threading.Event()
    ctx.cache["racadm_pendente"] = pendente

    def termina():
        time.sleep(0.3)
        ctx.ferramentas_fabricante["racadm"] = "/opt/dell/srvadmin/sbin/racadm"
        pendente.set()

    threading.Thread(target=termina).start()
    resultado: dict = {"passos": []}
    inicio = time.monotonic()
    automatica._esperar_racadm(ctx, resultado)
    assert time.monotonic() - inicio >= 0.25 and ctx.ferramentas_fabricante["racadm"]
    assert resultado["passos"][-1]["detalhe"] == "esperado o download do iDRAC Tools: racadm pronto"
    assert "sendo baixado" not in " ".join(automatica.prever_temporarias(ctx, {}))  # terminou: o plano não fala mais nisso


def test_caixa_durante_o_download_e_sem_internet():
    from msscan.acesso_idrac import TEXTO_SEM_RACADM, avaliar
    from tests.test_acesso_idrac import _leituras, _textos

    baixando = avaliar(_leituras(racadm={"situacao": "baixando"}))
    assert baixando["racadm_texto"] == "baixando do site da Dell…" and "em segundo plano" in _textos(baixando)
    assert TEXTO_SEM_RACADM not in _textos(baixando)
    sem = avaliar(_leituras(racadm={"situacao": "sem_internet", "mensagem": "download não concluído (dns)"}))
    assert TEXTO_SEM_RACADM in _textos(sem) and sem["racadm_texto"] == "não disponível (download não concluído (dns))"
