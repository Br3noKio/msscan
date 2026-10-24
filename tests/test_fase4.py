"""Testes da fase 4: RAID, placas de rede, partição de dados e relatórios JSON/HTML.

Saídas de ferramentas vêm das fixtures reais do R640. Não há saída real de
ferramentas RAID de fabricante nem de arrays mdadm: esses casos usam estruturas
já parseadas e estão marcados com TODO(fixture).
"""

from __future__ import annotations

import copy
import html.parser
import json
from datetime import datetime
from pathlib import Path

import pytest

from main import main
from msscan.core.executor import ExecutorReplay
from msscan.core.fallback import FalhaColeta
from msscan.core.modelo import Resultado, Status
from msscan.deteccao.contexto import Contexto, Controladora
from msscan.deteccao.detectores import detectar, detectar_ferramentas
from msscan.modulos.registro import registro_padrao
from msscan.parsers.ethtool import parse_ethtool, parse_ethtool_i, velocidade_mbps
from msscan.parsers.findmnt import parse_findmnt_json
from msscan.parsers.iproute import parse_ip_link_estatisticas
from msscan.parsers.mdadm import parse_examine_scan
from msscan.parsers.procfs import parse_mdstat
from msscan.pendrive.particao import DISPOSITIVO, OPCOES_MONTAGEM, localizar_ou_montar
from msscan.provedores.nic import ProvedorPlacasRede, avaliar_placas, avaliar_porta
from msscan.provedores.raid import (
    COMANDOS_RAID,
    ProvedorMdadm,
    ProvedorRaidFabricante,
    avaliar_mdadm,
    validar_comando_leitura,
)
from msscan.provedores.smart import ProvedorSmartctl
from msscan.relatorio import completo
from msscan.relatorio.completo import montar_relatorio, salvar_relatorios
from msscan.relatorio.html import gerar_html
from tests.conftest import PASTA_FIXTURES, R640, ExecutorFalso, ler_saida

AVULSAS = PASTA_FIXTURES / "avulsas"


# --- Parsers -------------------------------------------------------------------------


class TestParsersFase4:
    def test_ethtool_com_link(self):
        eno3 = parse_ethtool(ler_saida(R640, "ethtool__eno3"))
        assert eno3["link"] is True
        assert (eno3["velocidade_mbps"], eno3["duplex"], eno3["velocidade_maxima_mbps"]) == (1000, "Full", 1000)
        assert eno3["porta"] == "Twisted Pair"
        assert "100baseT/Half" in eno3["modos_suportados"]

    def test_ethtool_sem_cabo(self):
        eno1 = parse_ethtool(ler_saida(R640, "ethtool__eno1np0"))
        assert eno1["link"] is False
        assert eno1["velocidade_mbps"] is None and eno1["duplex"] is None
        assert eno1["velocidade_maxima_mbps"] == 10000

    def test_ethtool_i(self):
        info = parse_ethtool_i(ler_saida(R640, "ethtool__-i__eno3"))
        assert info["driver"] == "igb"
        assert info["firmware"] == "1.67, 0x80000fa8, 19.0.12"
        assert info["barramento"] == "0000:01:00.0"
        assert parse_ethtool_i(ler_saida(R640, "ethtool__-i__eno1np0"))["driver"] == "i40e"

    def test_velocidade(self):
        assert velocidade_mbps("1000Mb/s") == 1000
        assert velocidade_mbps("Unknown!") is None

    def test_ip_link_estatisticas(self):
        estatisticas = parse_ip_link_estatisticas(ler_saida(R640, "ip__-j__-s__link__show"))
        eno3 = estatisticas["eno3"]
        assert eno3["estado"] == "UP"
        assert eno3["rx"]["errors"] == 0 and eno3["tx"]["carrier_errors"] == 0
        assert isinstance(eno3["rx"]["packets"], int)

    def test_mdstat_real_sem_arrays(self):
        texto = (R640 / "arquivos/proc/mdstat").read_text()
        assert texto.startswith("Personalities")
        assert parse_mdstat(texto) == []

    def test_examine_scan_real_vazio(self):
        assert parse_examine_scan(ler_saida(R640, "mdadm__--examine__--scan")) == []

    def test_findmnt_real(self):
        [raiz] = parse_findmnt_json((AVULSAS / "findmnt_raiz.json").read_text())
        assert raiz["target"] == "/" and raiz["fstype"] == "ext4"
        assert parse_findmnt_json("") == []


# --- RAID -----------------------------------------------------------------------------


class TestValidacaoLeitura:
    @pytest.mark.parametrize(("familia", "args"), [(f, c) for f, cmds in COMANDOS_RAID.items() for c in cmds])
    def test_comandos_do_projeto_sao_leitura(self, familia, args):
        validar_comando_leitura(familia, args)

    @pytest.mark.parametrize(
        ("familia", "args"),
        [
            ("storcli", ["/c0/v0", "del", "force"]),
            ("storcli", ["/c0", "set", "jbod=on"]),
            ("storcli", ["/c0", "download", "file=fw.rom"]),
            ("storcli", ["/c0/e32/s1", "start", "rebuild"]),
            ("megacli", ["-AdpBbuCmd", "-BbuLearn", "-a0"]),
            ("megacli", ["-CfgClr", "-a0"]),
            ("ssacli", ["ctrl", "slot=0", "modify", "cacheratio=50/50"]),
            ("ssacli", ["ctrl", "slot=0", "ld", "1", "delete"]),
            ("arcconf", ["SETSTATE", "1", "DEVICE", "0", "0", "HSP"]),
            ("arcconf", ["ROMUPDATE", "1", "fw"]),
        ],
    )
    def test_escrita_e_barrada(self, familia, args):
        with pytest.raises(ValueError):
            validar_comando_leitura(familia, args)


def ctx_com_controladora(driver: str, ferramentas: dict[str, str] | None = None, executor=None) -> Contexto:
    ctx = Contexto(executor=executor or ExecutorFalso())
    ctx.controladoras.append(
        Controladora("02:00.0", "0104", "PERC", "RAID", driver, "raid_hardware", "1000", "005d")
    )
    ctx.ferramentas_fabricante.update(ferramentas or {})
    return ctx


class TestRaidFabricante:
    def test_sem_controladora_compativel(self):
        disp = ProvedorRaidFabricante("storcli", 30).disponivel(ctx_com_controladora("hpsa"))
        assert not disp.ok and "megaraid_sas" in disp.motivo

    def test_ferramenta_ausente_orienta_vendor(self):
        disp = ProvedorRaidFabricante("storcli", 30).disponivel(ctx_com_controladora("megaraid_sas"))
        assert not disp.ok and "vendor/" in disp.motivo and "perccli64" in disp.motivo

    def test_executa_somente_leitura_e_nao_finge_resultado(self):
        ferramenta = "/mnt/msscan/vendor/perccli64"
        comandos = {(ferramenta, *args): (0, "{}", "") for args in COMANDOS_RAID["storcli"]}
        executor = ExecutorFalso(comandos=comandos)
        ctx = ctx_com_controladora("megaraid_sas", {"perccli64": ferramenta}, executor)
        provedor = ProvedorRaidFabricante("storcli", 30)
        assert provedor.disponivel(ctx).ok
        resultado = provedor.coletar(ctx)
        assert resultado.status == Status.NAO_VERIFICADO
        assert "interpretação ainda não foi implementada" in resultado.mensagem
        assert [c[1:] for c in executor.comandos_rodados()] == COMANDOS_RAID["storcli"]

    def test_todos_os_comandos_falham(self):
        ctx = ctx_com_controladora("megaraid_sas", {"perccli64": "perccli64"})
        with pytest.raises(FalhaColeta):
            ProvedorRaidFabricante("storcli", 30).coletar(ctx)

    def test_componente_com_hardware_sem_ferramenta_fica_nao_verificado(self):
        executor = ExecutorReplay(R640)
        ctx = detectar(executor)
        ctx.controladoras.append(Controladora("02:00.0", "0104", "PERC H730P", "x", "megaraid_sas", "raid_hardware", "1000", "005d"))
        raid = next(r for r in registro_padrao().diagnosticar(ctx) if r.componente == "raid")
        assert raid.status == Status.NAO_VERIFICADO
        resultados = [t["resultado"] for t in raid.dados["tentativas"]]
        assert resultados[0] == "indisponível" and resultados[-1] == "sem dados"


class TestMdadm:
    def test_r640_real_nao_aplicavel(self):
        resultado = ProvedorMdadm().coletar(detectar(ExecutorReplay(R640)))
        assert resultado.status == Status.NAO_APLICAVEL
        assert "nenhum volume RAID" in resultado.mensagem

    def test_sem_fontes_nao_afirma_nada(self):
        assert ProvedorMdadm().coletar(Contexto(executor=ExecutorFalso())) is None

    def test_com_raid_de_hardware_nao_responde_pelo_componente(self):
        assert avaliar_mdadm([], [], ha_raid_hardware=True) is None

    # TODO(fixture): substituir as estruturas abaixo por /proc/mdstat real com arrays.
    @staticmethod
    def array(**campos):
        base = {"nome": "md0", "estado": "active", "nivel": "raid1", "dispositivos": ["sdb1", "sda1"], "falhos": [],
                "total": 2, "ativos": 2, "mapa": "UU", "degradado": False, "operacao": None, "progresso": None}  # fmt: skip
        base.update(campos)
        return base

    def test_saudavel(self):
        assert avaliar_mdadm([self.array()], [], False).status == Status.OK

    def test_degradado_e_falha(self):
        r = avaliar_mdadm([self.array(ativos=1, mapa="U_", degradado=True, falhos=["sdb1"])], [], False)
        assert r.status == Status.FALHA
        assert any("DEGRADADO" in p for p in r.dados["problemas"])

    def test_reconstrucao_e_alerta(self):
        r = avaliar_mdadm([self.array(operacao="recovery", progresso=42.5)], [], False)
        assert r.status == Status.ALERTA and "recovery em andamento (42.5%)" in r.dados["problemas"][0]

    def test_so_superblocos_nao_montados(self):
        r = avaliar_mdadm([], [{"dispositivo": "/dev/md/0", "atributos": {"metadata": "imsm"}}], False)
        assert r.status == Status.NAO_VERIFICADO and "não monta arrays" in r.mensagem


def test_detectar_ferramentas_vendor_antes_do_path():
    executor = ExecutorFalso(existentes={"/dados/vendor/perccli64"})
    executor.ferramentas = {"ssacli", "perccli64"}
    ctx = Contexto(executor=executor, pasta_dados="/dados")
    detectar_ferramentas(ctx)
    assert ctx.ferramentas_fabricante == {"perccli64": "/dados/vendor/perccli64", "ssacli": "ssacli"}


class TestDiscosMegaraid:
    def test_megaraid_e_lido_pelo_smartctl(self):
        # TODO(fixture): trocar pela saída real de "smartctl --scan-open -j" num servidor com PERC.
        scan = ler_saida(R640, "smartctl__--scan-open__-j").replace('"/dev/sda"', '"/dev/bus/0"').replace('"type": "sat"', '"type": "megaraid,0"')
        executor = ExecutorFalso(
            comandos={
                ("smartctl", "--scan-open", "-j"): (0, scan, ""),
                ("smartctl", "-a", "-j", "-d", "megaraid,0", "/dev/bus/0"): (0, ler_saida(R640, "smartctl__-a__-j__-d__sat__dev_sda"), ""),
            }
        )
        resultado = ProvedorSmartctl().coletar(Contexto(executor=executor))
        assert resultado.status == Status.OK

    def test_cciss_continua_nao_verificado(self):
        scan = ler_saida(R640, "smartctl__--scan-open__-j").replace('"type": "sat"', '"type": "cciss,0"')
        executor = ExecutorFalso(comandos={("smartctl", "--scan-open", "-j"): (0, scan, "")})
        resultado = ProvedorSmartctl().coletar(Contexto(executor=executor))
        assert resultado.status == Status.NAO_VERIFICADO


# --- Placas de rede ------------------------------------------------------------------------


def portas_r640():
    ctx = detectar(ExecutorReplay(R640))
    return ProvedorPlacasRede().coletar(ctx)


class TestPlacasRede:
    def test_r640_real(self):
        resultado = portas_r640()
        assert resultado.status == Status.OK
        assert resultado.mensagem == "4 porta(s) física(s); 1 com link (eno3 1000 Mb/s Full)"
        eno3 = next(p for p in resultado.dados["portas"] if p["nome"] == "eno3")
        assert eno3["firmware"]["firmware"] == "1.67, 0x80000fa8, 19.0.12"
        assert "eno1np0: sem cabo/link" in resultado.dados["observacoes"]

    def porta(self, **mudancas):
        eno3 = copy.deepcopy(next(p for p in portas_r640().dados["portas"] if p["nome"] == "eno3"))
        for chave, valor in mudancas.items():
            eno3["ethtool"][chave] = valor
        return eno3

    def test_half_duplex(self):
        situacao, problemas, _ = avaliar_porta(self.porta(duplex="Half"))
        assert situacao == Status.ALERTA and "half-duplex" in problemas[0]

    def test_velocidade_baixa_em_cobre(self):
        situacao, problemas, _ = avaliar_porta(self.porta(velocidade_mbps=100))
        assert situacao == Status.ALERTA and "negociou 100 Mb/s" in problemas[0]

    def test_velocidade_baixa_em_sfp_e_so_observacao(self):
        situacao, problemas, observacoes = avaliar_porta(self.porta(velocidade_mbps=100, porta="FIBRE"))
        assert situacao == Status.OK and problemas == [] and "SFP" in observacoes[0]

    def test_erros_de_recepcao(self):
        porta = self.porta()
        porta["estatisticas"]["rx"]["errors"] = 12
        situacao, problemas, _ = avaliar_porta(porta)
        assert situacao == Status.ALERTA and "12 erros de recepção" in problemas[0]

    def test_sem_dados_nao_e_ok(self):
        porta = self.porta()
        porta.update(ethtool=None, estatisticas=None)
        assert avaliar_porta(porta)[0] is None
        assert avaliar_placas([porta], []).status == Status.NAO_VERIFICADO

    def test_nenhuma_porta(self):
        assert avaliar_placas([], []) is None


# --- Partição de dados ------------------------------------------------------------------------

FINDMNT = ("findmnt", "-J", "-S", "LABEL=MSSCAN")


class TestParticao:
    def test_ja_montada(self):
        # Mesmo formato da saída real do findmnt (fixture avulsa), apontando para o pendrive.
        saida = (AVULSAS / "findmnt_raiz.json").read_text().replace('"/"', '"/media/pendrive"')
        particao = localizar_ou_montar(ExecutorFalso(comandos={FINDMNT: (0, saida, "")}))
        assert particao.pasta == "/media/pendrive" and not particao.montada_agora

    def test_nao_encontrada(self):
        particao = localizar_ou_montar(ExecutorFalso(comandos={FINDMNT: (1, "", "")}))
        assert particao.pasta is None and "não encontrada" in particao.mensagem

    def test_monta_pelo_rotulo(self):
        comando = ("mount", "-o", OPCOES_MONTAGEM, DISPOSITIVO, "/mnt/msscan")
        executor = ExecutorFalso(comandos={FINDMNT: (1, "", ""), ("mkdir", "-p", "/mnt/msscan"): (0, "", ""), comando: (0, "", "")}, existentes={DISPOSITIVO})
        particao = localizar_ou_montar(executor)
        assert particao.pasta == "/mnt/msscan" and particao.montada_agora
        assert list(comando) in executor.comandos_rodados()
        assert not any(c[0].startswith("mkfs") for c in executor.comandos_rodados())

    def test_falha_ao_montar(self):
        executor = ExecutorFalso(comandos={FINDMNT: (1, "", "")}, existentes={DISPOSITIVO})
        particao = localizar_ou_montar(executor)
        assert particao.pasta is None and "não foi possível montar" in particao.mensagem


# --- Relatórios --------------------------------------------------------------------------------


def relatorio_r640():
    executor = ExecutorReplay(R640)
    ctx = detectar(executor)
    registro = registro_padrao()
    resultados = registro.diagnosticar(ctx)
    agora = datetime(2026, 9, 24, 16, 0, 0)
    return montar_relatorio(ctx, resultados, None, executor, agora, agora, registro.titulo)


class ValidadorHtml(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.pilha, self.erros, self.externos = [], 0, []

    def handle_starttag(self, tag, attrs):
        self.externos += [v for k, v in attrs if k in ("src", "href") and v]
        if tag not in ("meta", "br", "link", "img", "input"):
            self.pilha.append(tag)

    def handle_endtag(self, tag):
        if self.pilha and self.pilha[-1] == tag:
            self.pilha.pop()
        else:
            self.erros += 1


class TestRelatorios:
    def test_estrutura_json(self):
        relatorio = relatorio_r640()
        assert {"medido", "interpretacao_ia", "atualizacoes", "historico_comandos", "equipamento", "veredito"} <= set(relatorio)
        assert relatorio["interpretacao_ia"] == {"situacao": "nao_solicitada"}
        assert relatorio["msscan"]["modo"] == "replay"
        fontes = next(i for i in relatorio["medido"] if i["componente"] == "fontes")
        assert fontes["titulo"] == "Fontes de alimentação" and fontes["status"] == "ALERTA"
        json.dumps(relatorio)  # serializável

    def test_html_autocontido_e_bem_formado(self):
        documento = gerar_html(relatorio_r640())
        validador = ValidadorHtml()
        validador.feed(documento)
        assert validador.pilha == [] and validador.erros == 0
        assert validador.externos == []
        assert "@import" not in documento and "url(" not in documento
        for secao in ("<h2>Medido</h2>", "<h2>Interpretação (IA)</h2>", "<h2>Atualizações</h2>"):
            assert secao in documento
        assert "Redundância de energia perdida" in documento

    def test_html_escapa_conteudo(self):
        relatorio = relatorio_r640()
        relatorio["medido"][0]["mensagem"] = '<script>alert("x")</script>'
        documento = gerar_html(relatorio)
        assert "<script>" not in documento and "&lt;script&gt;" in documento

    def test_salvar_no_pendrive(self, tmp_path):
        pasta, no_pendrive = salvar_relatorios(relatorio_r640(), "texto", str(tmp_path), datetime(2026, 9, 24, 16, 5, 7))
        assert no_pendrive
        assert pasta == tmp_path / "relatorios" / "poweredge_r640_2026-09-24_160507"
        assert sorted(p.name for p in pasta.iterdir()) == ["relatorio.html", "relatorio.json", "relatorio.txt"]
        assert json.loads((pasta / "relatorio.json").read_text())["equipamento"]["modelo"] == "PowerEdge R640"

    def test_salvar_sem_pendrive_usa_reserva(self, tmp_path, monkeypatch):
        monkeypatch.setattr(completo, "PASTA_RESERVA", str(tmp_path / "reserva"))
        pasta, no_pendrive = salvar_relatorios(relatorio_r640(), "texto", None)
        assert not no_pendrive and pasta.parent == tmp_path / "reserva"


class TestMainFase4:
    def test_replay_grava_relatorio_com_dados(self, tmp_path, capsys):
        assert main(["--replay", str(R640), "--dados", str(tmp_path)]) == 0
        saida = capsys.readouterr().out
        assert "Relatório gravado no pendrive" in saida
        [pasta] = list((tmp_path / "relatorios").iterdir())
        assert (pasta / "relatorio.html").is_file()

    def test_replay_sem_dados_nao_grava(self, capsys):
        assert main(["--replay", str(R640)]) == 0
        assert "relatório não gravado" in capsys.readouterr().out

    def test_coleta_vai_para_pasta_coletas(self, tmp_path):
        assert main(["--replay", str(R640), "--dados", str(tmp_path), "--coletar"]) == 0
        assert len(list((tmp_path / "coletas").glob("coleta_dell_poweredge_r640_*.tar.gz"))) == 1


def test_resultado_nao_verificado_de_provedor_nao_vira_ok():
    # Garantia central: um provedor de fabricante que só coletou não pode deixar o RAID "OK".
    r = Resultado("raid", Status.NAO_VERIFICADO, fonte="perccli64")
    assert r.status != Status.OK
