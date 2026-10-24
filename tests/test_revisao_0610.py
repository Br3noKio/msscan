"""Revisão independente de 06/10/2026 (caminhos de escrita): "não consegui ler o iDRAC" nunca vira "nada foi feito".

C1: cancelamento/lista de jobs sem resposta não libera outro caminho depois do pedido de instalar pelo repositório.
C2: envio direto com resposta incerta (timeout, 5xx, "já existe job") não segue para o racadm/.BIN (gravaria duas vezes).
M4: o pass-through só é restaurado se estiver exatamente como o MSscan deixou.
"""

from __future__ import annotations

from types import SimpleNamespace

from msscan.atualizacao import automatica, repositorio as repo, temporarias as temp
from msscan.core.executor import CANCELAR_JOB, ENVIAR_FIRMWARE, REPOSITORIO_INSTALAR, REPOSITORIO_LISTA, RespostaRedfish
from msscan.redfish.cliente import FalhaRedfish

NADA = lambda _: None  # noqa: E731
JOB = "JID_000000000010"


class ClienteFalso:
    """iDRAC falso no nível do cliente Redfish: respostas por operação e GETs por caminho (exceção = falha de leitura)."""

    def __init__(self, escritas: dict, gets: dict) -> None:
        self.escritas, self.gets, self.feitas = escritas, gets, []

    def escrever(self, operacao, caminho, corpo, arquivo=None, timeout=900, progresso=None):
        self.feitas.append((operacao, corpo))
        fila = self.escritas[operacao]
        resposta = fila.pop(0) if isinstance(fila, list) else fila
        if isinstance(resposta, Exception):
            raise resposta
        return resposta

    def get(self, caminho, timeout=30):
        valor = self.gets.get(caminho)
        valor = valor.pop(0) if isinstance(valor, list) else valor
        if isinstance(valor, Exception) or valor is None:
            raise valor or FalhaRedfish(f"{caminho}: sem resposta")
        return valor


def _job(estado: str) -> dict:
    return {"Id": JOB, "JobState": estado, "Name": "Repository Update", "Message": estado}


def _fila(*ids: str) -> dict:
    return {"Members": [{"@odata.id": f"{repo.JOBS}/{i}"} for i in ids]}


def test_recusa_explicita_so_com_4xx_sem_indicio_de_aceite():
    assert repo.recusa_explicita(RespostaRedfish("u", 400, corpo={"error": "bad package"}))
    assert not repo.recusa_explicita(RespostaRedfish("u", None, erro="timeout"))
    assert not repo.recusa_explicita(RespostaRedfish("u", 500))
    assert not repo.recusa_explicita(RespostaRedfish("u", 202))  # aceito, sem Location: incerto
    assert not repo.recusa_explicita(RespostaRedfish("u", 400, corpo={"MessageId": "IDRAC.2.8.RED014"}))


def test_cancelar_job_sem_resposta_nao_conta_como_cancelado():
    sem_resposta = ClienteFalso({CANCELAR_JOB: FalhaRedfish("sem sessão")}, {f"{repo.JOBS}/{JOB}": FalhaRedfish("timeout")})
    ok, detalhe = repo.cancelar_job(sem_resposta, JOB)
    assert not ok and "não confirmado" in detalhe
    fora = ClienteFalso({CANCELAR_JOB: RespostaRedfish("u", 200)}, {f"{repo.JOBS}/{JOB}": FalhaRedfish("404", 404)})
    assert repo.cancelar_job(fora, JOB)[0]
    assert repo.listar_jobs(ClienteFalso({}, {})) is None  # leitura falhou: "não sei", nunca "fila vazia"


def _instalar(resposta_instalar, gets_extra: dict | None = None) -> dict:
    gets = {f"{repo.JOBS}/{JOB}": _job("Completed"), repo.JOBS: _fila(JOB), **(gets_extra or {})}
    cliente = ClienteFalso({REPOSITORIO_INSTALAR: [RespostaRedfish("u", 202, local=f"{repo.JOBS}/{JOB}"), resposta_instalar],
                            REPOSITORIO_LISTA: RespostaRedfish("u", 200, corpo={})}, gets)  # fmt: skip
    itens = [{"url": "https://downloads.dell.com/FOLDER1/Network_Firmware_X_WN64_22.5.7_A00.EXE"}]
    return repo.instalar(cliente, "192.168.1.85", itens, lambda s, d: None, NADA, via="rede local")


def test_pedido_de_instalar_com_resposta_incerta_e_falhou_depois():
    resultado = _instalar(RespostaRedfish("u", None, erro="timeout"))
    assert resultado["desfecho"] == repo.FALHOU_DEPOIS and "nada é reenviado" in resultado["mensagem"]
    assert _instalar(RespostaRedfish("u", 400, corpo={"error": "not supported"}))["desfecho"] == repo.FALHOU_ANTES


def test_job_queue_ilegivel_depois_de_instalar_e_falhou_depois():
    gets = {repo.JOBS: [_fila(JOB), FalhaRedfish("timeout")]}  # lida antes do pedido; falha depois
    resultado = _instalar(RespostaRedfish("u", 202, local=f"{repo.JOBS}/{JOB}"), gets)
    assert resultado["desfecho"] == repo.FALHOU_DEPOIS and "não pôde ser lida" in resultado["mensagem"]


def _contexto_redfish(resposta) -> tuple:
    cliente = ClienteFalso({ENVIAR_FIRMWARE: resposta}, {})
    ctx = SimpleNamespace(bmc=SimpleNamespace(cliente=cliente))
    bmc = {"redfish": True, "administrador": True, "multipart": "/redfish/v1/UpdateService/MultipartUpload"}
    registro = {"id": "item1", "componente": "Rede X710", "arquivo": "/tmp/Network_Firmware_X_WN64_22.5.7_A00.EXE",
                "hash_conferido": ("", "")}  # fmt: skip
    return ctx, bmc, registro


def test_envio_direto_incerto_nao_segue_para_o_racadm(monkeypatch):
    monkeypatch.setattr(automatica, "hash_ainda_confere", lambda *a: "")
    monkeypatch.setattr(automatica, "_racadm", lambda *a: (_ for _ in ()).throw(AssertionError("racadm não pode rodar")))
    for resposta in (RespostaRedfish("u", None, erro="timeout de leitura"), RespostaRedfish("u", 503),
                     RespostaRedfish("u", 400, corpo={"MessageId": "RED014"})):  # fmt: skip
        ctx, bmc, registro = _contexto_redfish(resposta)
        resultado: dict = {"passos": []}
        desfecho = automatica.aplicar_item(ctx, None, None, bmc, registro, resultado, NADA, None, None)
        assert desfecho == automatica.POSSIVELMENTE, resposta
        assert "Job Queue" in resultado["tentativas"][-1]["detalhe"]


def test_envio_direto_recusado_de_verdade_segue_para_o_proximo_caminho(monkeypatch):
    monkeypatch.setattr(automatica, "hash_ainda_confere", lambda *a: "")
    chamado = []
    monkeypatch.setattr(automatica, "_racadm", lambda *a: chamado.append(1) or automatica.AGENDADO)
    ctx, bmc, registro = _contexto_redfish(RespostaRedfish("u", 400, corpo={"error": "package not applicable"}))
    assert automatica.aplicar_item(ctx, None, None, bmc, registro, {"passos": []}, NADA, None, None) == automatica.AGENDADO and chamado


class ExecutorRegistra:
    def __init__(self) -> None:
        self.comandos: list = []

    def acao_comando(self, comando):
        self.comandos.append(comando)
        return 0, "", ""


def test_passthrough_alterado_por_fora_nao_e_mexido(monkeypatch):
    mudanca = {"antes": {"AdminState": "0", "PTMode": "0"}}
    monkeypatch.setattr(temp.idrac, "racadm", lambda ctx: "/opt/dell/srvadmin/sbin/racadm")
    for estado, esperado in (({"lido": True, "ativo": True, "modo": "lom-p2p"}, temp.ALTERADO_POR_FORA),
                             ({"lido": True, "ativo": False}, "já estava desligado"),
                             ({"lido": False}, "não foi possível ler")):  # fmt: skip
        monkeypatch.setattr(temp.idrac, "ler_passthrough", lambda ctx, e=estado: e)
        executor = ExecutorRegistra()
        _, detalhe = temp._desfazer_passthrough(None, executor, mudanca, NADA)
        assert esperado in detalhe and executor.comandos == [], estado


def test_job_do_repositorio_sem_resposta_fica_registrado_para_a_proxima_sessao():
    cliente = ClienteFalso({}, {f"{repo.JOBS}/{JOB}": FalhaRedfish("timeout")})
    ctx = SimpleNamespace(bmc=SimpleNamespace(validado=True, cliente=cliente))
    ok, detalhe = temp._cancelar_tarefa(ctx, ExecutorRegistra(), {"antes": {"job": JOB}})
    assert not ok and "próxima sessão" in detalhe


# --- Liberação da gravação real: 06/10 só este R640 e placas de rede; 09/10 todos os componentes em servidores Dell ----

from msscan.atualizacao import liberacao  # noqa: E402


class ExecutorReal:
    """Executor "real" (sem alvo simulado) que só responde ao ``dmidecode``."""

    alvo_simulado, origem = False, "real"

    def __init__(self, tag: str, rc: int = 0) -> None:
        self.tag, self.rc, self.comandos = tag, rc, []

    def rodar(self, comando, timeout=0):
        self.comandos.append(comando)
        return (self.rc, self.tag + "\n", "") if comando[:1] == ["dmidecode"] else (-1, "", "não esperado")


def _tag_liberada(monkeypatch) -> str:
    monkeypatch.setattr(liberacao, "RESTRICAO_ATIVA", True)
    return "Dell Inc."  # 09/10/2026 (2ª parte): liberado por fabricante, não mais por service tag


def test_liberacao_total_em_servidores_dell(monkeypatch):
    """09/10/2026: ciclo completo concluído no R640 → todos os componentes em qualquer Dell; outro fabricante bloqueado."""
    dell = _tag_liberada(monkeypatch)
    itens = [{"componente": "rNDC X710/I350", "categoria": "Network"}, {"componente": "BIOS", "categoria": "BIOS"},
             {"componente": "iDRAC", "categoria": "iDRAC with Lifecycle Controller"}, {"componente": "PERC", "categoria": "SAS RAID"},
             {"componente": "?", "categoria": ""}]  # fmt: skip
    avaliacao = liberacao.avaliar(ExecutorReal(dell), itens)
    assert avaliacao["bloqueios"] == [] and avaliacao["bloqueados"] == [] and len(avaliacao["liberados"]) == 5
    assert "Dell" in avaliacao["texto_liberado"] and "memória" not in avaliacao["texto_bloqueado"].lower()
    for executor in (ExecutorReal("HPE"), ExecutorReal("Lenovo"), ExecutorReal("", rc=1)):
        avaliacao = liberacao.avaliar(executor, itens)
        assert "não está liberado" in avaliacao["bloqueios"][0] and len(avaliacao["bloqueados"]) == 5


def test_liberacao_sem_service_tag_no_codigo():
    import importlib

    assert importlib.reload(liberacao).RESTRICAO_ATIVA is True  # ligada no código; só o conftest a desliga
    fonte = (liberacao.__file__ and open(liberacao.__file__, encoding="utf-8").read())
    assert "system-manufacturer" in fonte and liberacao.CATEGORIAS_LIBERADAS is None and "SERVICE_TAGS" not in fonte


def test_motor_recusa_item_bloqueado_antes_de_tocar_no_idrac(monkeypatch, tmp_path):
    _tag_liberada(monkeypatch)
    monkeypatch.setattr(automatica, "garantir_acesso", lambda *a, **k: (_ for _ in ()).throw(AssertionError("tocou no iDRAC")))
    resultado = {"passos": [], "itens": [{"componente": "BIOS", "categoria": "BIOS", "arquivo": "x.EXE"}]}
    automatica.aplicar_automatico(SimpleNamespace(), ExecutorReal("HPE"), tmp_path, resultado, NADA)
    assert resultado["situacao"] == "bloqueada" and "não está liberado" in resultado["mensagem"]
