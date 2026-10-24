"""Utilitários compartilhados pelos testes. Nenhum teste executa hardware real."""

from __future__ import annotations

from pathlib import Path

import pytest

from msscan.core.executor import Executor, RespostaHttp, RespostaIA, RespostaRedfish

PASTA_FIXTURES = Path(__file__).parent / "fixtures"
R640 = PASTA_FIXTURES / "dell" / "poweredge_r640"
R620 = PASTA_FIXTURES / "dell" / "poweredge_r620"  # coleta real de 19/10/2026 (12ª, iDRAC7 2.65.65.65, PERC H710 Mini)


def ler_saida(pasta: Path, nome: str) -> str:
    """Lê a saída bruta gravada de um comando numa fixture (ex.: ``"lspci__-nnk"``)."""
    return (pasta / "comandos" / f"{nome}.stdout").read_text(encoding="utf-8")


class ExecutorFalso(Executor):
    """Executor programável para testes de lógica.

    Só devolve saídas passadas explicitamente (que devem vir de fixtures reais);
    comando não programado se comporta como ferramenta ausente.
    """

    origem = "falso"

    def __init__(
        self,
        comandos: dict[tuple[str, ...], tuple[int, str, str]] | None = None,
        existentes: set[str] | None = None,
    ) -> None:
        super().__init__()
        self.comandos = comandos or {}
        self.existentes = existentes if existentes is not None else set()
        self.arquivos: dict[str, str] = {}
        self.diretorios: dict[str, list[str]] = {}
        self.ferramentas: set[str] = set()
        self.respostas_http: dict[str, RespostaHttp] = {}
        self.respostas_redfish: dict[tuple[str, str], RespostaRedfish] = {}
        self.respostas_ia: list[RespostaIA] = []
        self.ia_enviados: list[tuple[dict, dict]] = []  # (cabeçalhos, corpo) — só para os testes conferirem
        self.ia_urls: list[str] = []

    def _rodar(self, comando, timeout):
        rc, saida, erro = self.comandos.get(tuple(comando), (-1, "", f"comando não encontrado: {comando[0]}"))
        return rc, saida, erro, 0.0

    def _ler_arquivo(self, caminho):
        return self.arquivos.get(caminho)

    def _existe(self, caminho):
        return caminho in self.existentes

    def _listar(self, caminho):
        return self.diretorios.get(caminho, [])

    def _tem_comando(self, nome):
        return nome in self.ferramentas or any(cmd[0] == nome for cmd in self.comandos)

    def _http_get(self, url, timeout):
        return self.respostas_http.get(url, RespostaHttp(url, None, "conexao", "sem resposta programada"))

    def _redfish(self, metodo, url, token, corpo, timeout):
        resposta = self.respostas_redfish.get((metodo, url))
        return resposta or RespostaRedfish(url, None, erro_tipo="conexao", erro="sem resposta programada")

    def _post_ia(self, url, cabecalhos, corpo, timeout):
        self.ia_enviados.append((dict(cabecalhos), corpo))
        self.ia_urls.append(url)
        if not self.respostas_ia:
            return RespostaIA(None, erro_tipo="conexao", erro="sem resposta programada")
        return self.respostas_ia.pop(0)

    def comandos_rodados(self) -> list[list[str]]:
        """Comandos pedidos, na ordem."""
        return [e.comando for e in self.historico]


@pytest.fixture
def pasta_r640() -> Path:
    return R640


@pytest.fixture(autouse=True)
def _sem_espera_da_ia(monkeypatch):
    """Novas tentativas da IA (502/503/504/timeout) sem esperar de verdade nos testes."""
    import msscan.ia.provedores

    monkeypatch.setattr(msscan.ia.provedores, "_dormir", lambda segundos: None)


def hardware_saudavel(monkeypatch) -> None:
    """BMC falso sem leituras de hardware: a tela de aviso (15.6) receberia "não verificado" (bloqueia). Para os testes
    que precisam chegar ao caminho de gravação, a releitura devolve tudo OK."""
    from msscan.atualizacao import aviso

    linhas = [{"item": nome, "situacao": aviso.OK, "texto": "ok (teste)"}
              for nome in ("Fontes", "Memória", "RAID", "Temperatura e sensores", "Ventoinhas")]  # fmt: skip
    monkeypatch.setattr(aviso, "conferir_hardware", lambda _ctx, _registro: [dict(l) for l in linhas])


@pytest.fixture(autouse=True)
def _travas_forcadas_desligadas(monkeypatch):
    """As travas foram liberadas no código (30/09/2026, CLAUDE.md 15.6); os testes rodam SEMPRE com as duas desligadas,
    para nenhum teste chegar a um BMC ou a um reinício de verdade e para os testes das travas continuarem valendo.
    Quem precisa do controle "sem trava" liga só dentro do próprio teste (monkeypatch)."""
    from msscan.core import executor

    monkeypatch.setattr(executor, "GRAVACAO_REAL_LIBERADA", False)
    monkeypatch.setattr(executor, "ACOES_REAIS_LIBERADAS", False)
    # Liberação restrita (06/10/2026): fica fora do caminho para os testes das travas acima continuarem valendo; os testes
    # dela (tests/test_revisao_0610.py) a ligam de novo.
    from msscan.atualizacao import liberacao

    monkeypatch.setattr(liberacao, "RESTRICAO_ATIVA", False)


def com_textos(conteudo: str) -> str:
    """``index.html``/``app.js`` com os textos do arquivo central (``msscan/textos/pt_BR.json``) no lugar das chaves.

    Para os testes que procuram um texto da interface: desde 09/10/2026 os textos ficam todos no arquivo central."""
    import json
    import re

    from msscan.textos import carregar

    textos = carregar()
    conteudo = re.sub(r'(data-t="([^"]+)"[^>]*>)', lambda m: m.group(1) + textos.get(m.group(2), m.group(2)), conteudo)
    conteudo = re.sub(r'data-t-([a-z-]+)="([^"]+)"', lambda m: f'{m.group(1)}="{textos.get(m.group(2), m.group(2))}" {m.group(0)}', conteudo)
    return re.sub(r'\bT\("([^"]+)"', lambda m: "T(" + json.dumps(textos.get(m.group(1), m.group(1)), ensure_ascii=False), conteudo)


@pytest.fixture(autouse=True)
def _relogio_da_sessao_limpo():
    """O acerto do relógio vale para a sessão (``rede/relogio.conferir``): cada teste começa sem ele."""
    from msscan.rede import relogio

    relogio.esquecer()
    yield
    relogio.esquecer()
