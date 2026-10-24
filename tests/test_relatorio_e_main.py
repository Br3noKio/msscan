"""Testes do relatório no terminal, do registro de componentes e do main."""

from __future__ import annotations

from msscan.relatorio.simbolos import ASCII

from main import main
from msscan.core.fallback import Disponibilidade, Provedor
from msscan.core.modelo import Resultado, Status
from msscan.deteccao.contexto import Contexto
from msscan.modulos.registro import Registro, registro_padrao
from msscan.relatorio.terminal import (
    VEREDITO_NENHUM,
    VEREDITO_PARCIAL,
    VEREDITO_SUCESSO,
    formatar_relatorio,
    veredito,
)
from tests.conftest import R640, ExecutorFalso


def r(status, nome="x"):
    return Resultado(nome, status, fonte="teste")


class TestVeredito:
    def test_sucesso(self):
        assert veredito([r(Status.OK), r(Status.NAO_APLICAVEL)]).startswith(VEREDITO_SUCESSO)

    def test_parcial_nunca_diz_saudavel_sem_ressalva(self):
        texto = veredito([r(Status.OK), r(Status.NAO_VERIFICADO)])
        assert texto.startswith(VEREDITO_PARCIAL)
        assert "(1 componente não verificado)" in texto

    def test_nenhum_verificado(self):
        assert veredito([r(Status.NAO_VERIFICADO), r(Status.NAO_VERIFICADO)]) == VEREDITO_NENHUM
        assert veredito([]) == VEREDITO_NENHUM

    def test_conta_problemas(self):
        assert "Resultado: 1 falha e 2 alertas." in veredito([r(Status.FALHA), r(Status.ALERTA), r(Status.ALERTA)])
        # Cobertura e resultado separados, sem "(s)": só alertas → "4 alertas e nenhuma falha"; nada → "nenhum problema".
        assert veredito([r(Status.OK)] + [r(Status.ALERTA)] * 4) == (
            "Servidor diagnosticado com sucesso (todos os componentes verificados). Resultado: 4 alertas e nenhuma falha.")
        assert veredito([r(Status.OK), r(Status.FALHA)]).endswith("Resultado: 1 falha e nenhum alerta.")
        assert veredito([r(Status.OK)]).endswith("Resultado: nenhum problema encontrado.")
        texto = veredito([r(Status.OK), r(Status.NAO_VERIFICADO), r(Status.NAO_VERIFICADO)])
        assert "(2 componentes não verificados)" in texto and texto.endswith("nenhum problema nos componentes verificados.")


class TestFormatacao:
    def test_simbolos_e_tentativas(self):
        resultados = [
            r(Status.OK, "cpu"),
            r(Status.ALERTA, "memoria"),
            r(Status.FALHA, "fontes"),
            r(Status.NAO_APLICAVEL, "raid"),
            Resultado(
                "sensores",
                Status.NAO_VERIFICADO,
                dados={"tentativas": [{"provedor": "ipmi", "resultado": "indisponível", "detalhe": "sem BMC"}]},
                mensagem="Não foi possível verificar este componente neste equipamento",
            ),
        ]
        texto = formatar_relatorio(resultados)
        for simbolo in ("✓", "⚠", "✗", "?", "N/A"):
            assert simbolo in texto
        assert "tentativa ipmi → indisponível: sem BMC" in texto
        assert "fonte: teste" in texto
        # Nome longo nunca encosta na situação (achado da frota simulada, 14/10/2026).
        longo = formatar_relatorio([Resultado("internos", Status.ALERTA, "teste", {}, "")], lambda _: "Componentes internos (backplane, cabos, slots)")
        assert "slots) ALERTA" in longo

    def test_ascii(self):
        texto = formatar_relatorio([r(Status.OK), r(Status.ALERTA), r(Status.FALHA)], simbolos=ASCII)
        assert not any(s in texto for s in ("✓", "⚠", "✗"))


class TestRegistro:
    def test_componentes_padrao(self):
        nomes = [c.nome for c in registro_padrao().componentes()]
        assert nomes == ["sensores", "fontes", "ventoinhas", "cpu", "memoria", "log_bmc", "internos", "discos", "raid", "rede"]

    def test_sem_provedores_tudo_nao_verificado(self):
        resultados = registro_padrao().diagnosticar(Contexto(executor=ExecutorFalso()))
        assert all(x.status == Status.NAO_VERIFICADO for x in resultados)

    def test_registrar_provedor_sem_mudar_nucleo(self):
        class Fixo(Provedor):
            nome = "fixo"
            prioridade = 1

            def disponivel(self, ctx):
                return Disponibilidade(True)

            def coletar(self, ctx):
                return Resultado("cpu", Status.OK, dados={"evidencia": "teste"})

        reg = Registro()
        reg.registrar_componente("cpu", "Processadores")
        reg.registrar_provedor("cpu", Fixo())
        [res] = reg.diagnosticar(Contexto(executor=ExecutorFalso()))
        assert res.status == Status.OK and res.fonte == "fixo"
        assert reg.titulo("cpu") == "Processadores"


class TestMain:
    def test_replay_r640(self, capsys):
        assert main(["--replay", str(R640)]) == 0
        saida = capsys.readouterr().out
        assert "Modo de demonstração" in saida
        assert "Servidor detectado: Dell Inc. PowerEdge R640" in saida
        assert "BMC: IPMI 2.0 disponível ✓" in saida
        assert "Servidor diagnosticado com sucesso (todos os componentes verificados). Resultado: 4 alertas e nenhuma falha." in " ".join(saida.split())
        assert "Status (10.1): Power Supply AC lost — sem energia de entrada" in saida
        assert "» Redundância de energia perdida" in saida
        assert max(len(linha) for linha in saida.splitlines() if "»" in linha) <= 78

    def test_replay_mostra_rede(self, capsys):
        assert main(["--replay", str(R640), "--simbolos", "console"]) == 0
        saida = capsys.readouterr().out
        assert "[3/3] Acesso à internet (HTTPS): √" in saida
        assert saida.index("Verificando a rede") < saida.index("Detectando capacidades")
        assert "✓" not in saida and "⚠" not in saida

    def test_parametro_semrede(self, capsys, tmp_path):
        import shutil

        copia = tmp_path / "r640"
        shutil.copytree(R640, copia)
        cmdline = copia / "arquivos/proc/cmdline"
        cmdline.write_text(cmdline.read_text().strip() + " msscan.semrede=1\n")
        assert main(["--replay", str(copia)]) == 0
        saida = capsys.readouterr().out
        assert "msscan.semrede=1" in saida
        assert "[1/3]" not in saida

    def test_replay_pasta_inexistente(self, capsys, tmp_path):
        assert main(["--replay", str(tmp_path / "nada")]) == 2
        assert "não encontrada" in capsys.readouterr().out

    def test_erro_inesperado_nao_quebra(self, capsys, monkeypatch):
        import main as modulo_main

        def explode(*_):
            raise RuntimeError("bug")

        monkeypatch.setattr(modulo_main, "detectar", explode)
        assert main(["--replay", str(R640)]) == 1
        assert "Erro inesperado: RuntimeError: bug" in capsys.readouterr().out
