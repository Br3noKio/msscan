"""Provedor de discos via ``smartctl``: discos diretos (AHCI, HBA, NVMe) e atrás de MegaRAID/PERC.

Discos atrás de MegaRAID/PERC aparecem no ``--scan-open`` como ``-d megaraid,N``
e são lidos com o mesmo comando. Outras controladoras (cciss/hpsa, aacraid...)
exigem sondagem por índice e ainda aparecem como não verificados, com o motivo.

TODO(fixture): precisa de saída real de ``smartctl --scan-open -j`` e
``smartctl -a -j -d megaraid,N`` num servidor com PERC.
"""

from __future__ import annotations

import re
from typing import Any

from msscan.core.fallback import Disponibilidade, FalhaColeta, Provedor
from msscan.core.modelo import MENSAGEM_NAO_VERIFICADO, Resultado, Status, pior_status
from msscan.deteccao.contexto import Contexto
from msscan.parsers import ErroParser
from msscan.parsers.lsblk import parse_lsblk_json
from msscan.parsers.smartctl import (
    BIT_ABERTURA_FALHOU,
    BIT_ATRIBUTO_ABAIXO_LIMITE_NO_PASSADO,
    BIT_COMANDO_SMART_FALHOU,
    BIT_DISCO_FALHANDO,
    BIT_LINHA_DE_COMANDO,
    BIT_LOG_AUTOTESTE,
    BIT_LOG_DE_ERROS,
    BIT_PREFALHA_ABAIXO_LIMITE,
    bits_saida,
    parse_scan_open,
    parse_smart,
)

PRIORIDADE_SMART = 30
# Disco virtual da controladora Dell (não é um disco físico; SMART não se aplica): "PERC H730P Mini", "PERC 6/i",
# "DELLBOSS VD". TODO(fixture): conferir com a coleta de um servidor com PERC (R530: o volume contou como "NÃO verificado").
_RE_VOLUME_DELL = re.compile(r"^(PERC\b|DELLBOSS\b|BOSS-)", re.IGNORECASE)
# Discos atrás de RAID que o smartctl NÃO enumera sozinho (exigem sondagem por índice; Fase 4b).
# "megaraid,N" é enumerado pelo --scan-open e verificado normalmente (mesmo JSON).
TIPOS_ATRAS_DE_RAID = {"cciss", "aacraid", "3ware", "areca", "hpt", "sssraid"}
# Atributos ATA cujo valor bruto > 0 indica degradação da mídia.
ATRIBUTOS_ALERTA = {5: "setores realocados", 187: "erros incorrigíveis reportados", 197: "setores pendentes", 198: "setores incorrigíveis offline"}  # fmt: skip
# Atributos que só viram observação (histórico, cabo, formatos brutos variáveis por fabricante).
ATRIBUTOS_OBSERVACAO = {188: "timeouts de comando", 199: "erros de CRC na interface (cabo/backplane)"}
LIMITE_DESGASTE_NVME = 90


def avaliar_disco(disco: dict[str, Any]) -> dict[str, Any]:
    """Avalia um disco a partir de ``parse_smart``.

    Devolve ``{"status", "problemas", "observacoes"}``. ``NAO_VERIFICADO`` quando
    o disco não pôde ser aberto ou não tem SMART utilizável.
    """
    problemas: list[str] = []
    observacoes: list[str] = []
    bits = bits_saida(disco.get("codigo_saida") or 0)
    if BIT_LINHA_DE_COMANDO in bits or BIT_ABERTURA_FALHOU in bits:
        return {"status": Status.NAO_VERIFICADO, "problemas": ["não foi possível abrir o disco"], "observacoes": []}
    if disco.get("smart_disponivel") is False or disco.get("smart_habilitado") is False:
        return {"status": Status.NAO_VERIFICADO, "problemas": ["SMART indisponível ou desativado"], "observacoes": []}
    nvme = disco.get("nvme") or {}
    if disco.get("smart_passou") is None and not nvme and not disco.get("atributos"):
        return {"status": Status.NAO_VERIFICADO, "problemas": ["o disco não informou status SMART"], "observacoes": []}

    situacoes: list[Status] = [Status.OK]

    def marcar(situacao: Status, texto: str) -> None:
        situacoes.append(situacao)
        problemas.append(texto)

    if disco.get("smart_passou") is False or BIT_DISCO_FALHANDO in bits:
        marcar(Status.FALHA, "SMART reprovado: o próprio disco indica falha")
    if BIT_PREFALHA_ABAIXO_LIMITE in bits:
        marcar(Status.FALHA, "atributo de pré-falha abaixo do limite do fabricante")
    if BIT_ATRIBUTO_ABAIXO_LIMITE_NO_PASSADO in bits:
        marcar(Status.ALERTA, "algum atributo já esteve abaixo do limite no passado")
    if BIT_LOG_DE_ERROS in bits:
        marcar(Status.ALERTA, "o log de erros do disco contém registros")
    if BIT_LOG_AUTOTESTE in bits:
        marcar(Status.ALERTA, "o log de autotestes registra falhas")
    if BIT_COMANDO_SMART_FALHOU in bits:
        observacoes.append("algum comando SMART falhou; dados podem estar incompletos")

    for atributo in disco.get("atributos", []):
        rotulo = f"atributo {atributo['id']} ({atributo['nome']})"
        if atributo["falhou_quando"] == "now":
            marcar(Status.FALHA, f"{rotulo} abaixo do limite agora")
        elif atributo["falhou_quando"] == "past":
            marcar(Status.ALERTA, f"{rotulo} esteve abaixo do limite no passado")
        bruto = atributo.get("bruto")
        if not isinstance(bruto, int) or bruto <= 0:
            continue
        if atributo["id"] in ATRIBUTOS_ALERTA:
            marcar(Status.ALERTA, f"{ATRIBUTOS_ALERTA[atributo['id']]}: {bruto}")
        elif atributo["id"] in ATRIBUTOS_OBSERVACAO:
            observacoes.append(f"{ATRIBUTOS_OBSERVACAO[atributo['id']]}: {bruto} (histórico)")

    if nvme:
        # TODO(fixture): precisa de saída real de "smartctl -a -j" em disco NVMe.
        if nvme.get("critical_warning"):
            marcar(Status.FALHA, f"NVMe: aviso crítico ativo (critical_warning={nvme['critical_warning']})")
        reserva, limite = nvme.get("available_spare"), nvme.get("available_spare_threshold")
        if isinstance(reserva, int) and isinstance(limite, int) and reserva < limite:
            marcar(Status.FALHA, f"NVMe: reserva disponível {reserva}% abaixo do limite {limite}%")
        if (nvme.get("media_errors") or 0) > 0:
            marcar(Status.ALERTA, f"NVMe: {nvme['media_errors']} erro(s) de mídia")
        desgaste = nvme.get("percentage_used")
        if isinstance(desgaste, int) and desgaste >= LIMITE_DESGASTE_NVME:
            marcar(Status.ALERTA, f"NVMe: {desgaste}% da vida útil estimada consumida")

    defeitos = disco.get("scsi_defeitos_crescidos")
    if isinstance(defeitos, int) and defeitos > 0:
        # TODO(fixture): precisa de saída real de "smartctl -a -j" em disco SAS.
        marcar(Status.ALERTA, f"SAS: {defeitos} defeito(s) crescido(s)")

    return {"status": pior_status(situacoes), "problemas": problemas, "observacoes": observacoes}


class ProvedorSmartctl(Provedor):
    """Discos via ``smartctl --scan-open`` + ``smartctl -a -j``."""

    nome = "smartctl"
    prioridade = PRIORIDADE_SMART

    def disponivel(self, ctx: Contexto) -> Disponibilidade:
        if ctx.executor.tem_comando("smartctl"):
            return Disponibilidade(True)
        return Disponibilidade(False, "smartctl não instalado")

    def _discos_lsblk(self, ctx: Contexto) -> tuple[set[str], dict[str, str]]:
        """Discos USB (o pendrive) e volumes de RAID da Dell (disco virtual da PERC/BOSS → modelo)."""
        rc, saida, _ = ctx.executor.rodar(["lsblk", "-J", "-d", "-o", "NAME,TRAN,TYPE,SIZE,MODEL"], timeout=15)
        if rc != 0:
            return set(), {}
        try:
            discos = parse_lsblk_json(saida)
        except ErroParser:
            return set(), {}
        usb = {f"/dev/{d['name']}" for d in discos if d.get("tran") == "usb"}
        volumes = {f"/dev/{d['name']}": (d.get("model") or "").strip() for d in discos
                   if _RE_VOLUME_DELL.match((d.get("model") or "").strip())}  # fmt: skip
        return usb, volumes

    def coletar(self, ctx: Contexto) -> Resultado | None:
        rc, saida, erro = ctx.executor.rodar(["smartctl", "--scan-open", "-j"], timeout=60)
        if not saida.strip():
            raise FalhaColeta(f"'smartctl --scan-open' falhou: {erro.strip() or f'código {rc}'}")
        encontrados = parse_scan_open(saida)
        if not encontrados:
            raise FalhaColeta(motivo_sem_discos(ctx))
        usb, volumes = self._discos_lsblk(ctx)
        discos: list[dict[str, Any]] = []
        ignorados: list[str] = []
        for item in encontrados:
            nome, tipo = item["nome"], item["tipo"]
            registro: dict[str, Any] = {"dispositivo": nome, "tipo": tipo}
            if nome in usb:
                ignorados.append(f"{nome}: disco USB (provavelmente o pendrive do MSscan)")
                continue
            if nome in volumes and not tipo.startswith(("megaraid", "sat+megaraid")):
                ignorados.append(f"{nome}: volume de RAID ({volumes[nome]}); os discos físicos são lidos pela controladora")
                continue
            if tipo.split(",")[0] in TIPOS_ATRAS_DE_RAID:
                registro.update(
                    status=Status.NAO_VERIFICADO.value,
                    problemas=["disco atrás de controladora RAID: verificação ainda não suportada"],
                )
                discos.append(registro)
                continue
            if item["erro"]:
                problema = f"erro ao abrir: {item['erro']}"
                if tipo.startswith(("megaraid", "sat+megaraid")):
                    # R620 (16/10/2026): "INQUIRY failed" num ID que a própria controladora listou.
                    problema += (" — a controladora lista este disco, mas ele não respondeu: disco com defeito, sem "
                                 "energia ou mal encaixado (confira o LED da baia)")
                registro.update(status=Status.NAO_VERIFICADO.value, problemas=[problema])
                discos.append(registro)
                continue
            comando = ["smartctl", "-a", "-j", "-d", tipo, nome] if tipo else ["smartctl", "-a", "-j", nome]
            _, saida_disco, erro_disco = ctx.executor.rodar(comando, timeout=60)
            try:
                dados = parse_smart(saida_disco)
            except ErroParser:
                registro.update(
                    status=Status.NAO_VERIFICADO.value,
                    problemas=[f"saída do smartctl ilegível: {erro_disco.strip() or 'sem saída'}"],
                )
                discos.append(registro)
                continue
            avaliacao = avaliar_disco(dados)
            registro.update(dados)
            registro.update(
                status=avaliacao["status"].value,
                problemas=avaliacao["problemas"],
                observacoes=avaliacao["observacoes"],
            )
            discos.append(registro)
        return consolidar_discos(discos, ignorados)


def rotulo_disco(disco: dict[str, Any]) -> str:
    """Nome que acha o disco: atrás da PERC o ``/dev/bus/0`` é o mesmo para todos, então vale o ID na controladora
    (``megaraid,N``), o modelo e o serial (R620, 16/10/2026: "/dev/bus/0: 177 defeitos" sem dizer qual disco)."""
    tipo = disco.get("tipo") or ""
    if not tipo.startswith(("megaraid", "sat+megaraid")):
        return disco["dispositivo"]
    extras = [str(disco[c]) for c in ("modelo", "serial") if disco.get(c)]
    return f"disco ID {tipo.rsplit(',', 1)[-1]} da controladora" + (f" ({', '.join(extras)})" if extras else "")


def motivo_sem_discos(ctx: Contexto) -> str:
    """Por que o ``smartctl --scan-open`` não achou nenhum disco."""
    from msscan.provedores.raid import controladora_sem_driver

    sem_driver = [c for c in ctx.controladoras if c.categoria == "sem_driver"]
    if sem_driver:
        return controladora_sem_driver(sem_driver)
    return ("nenhum disco encontrado: o servidor pode estar sem discos instalados (confira as baias), ou os discos estão "
            "atrás de uma controladora que não os mostra ao sistema")


def consolidar_discos(discos: list[dict[str, Any]], ignorados: list[str]) -> Resultado | None:
    """Junta a avaliação de cada disco num único resultado do componente "discos"."""
    if not discos:
        return None
    verificados = [d for d in discos if d["status"] != Status.NAO_VERIFICADO.value]
    nao_verificados = [d for d in discos if d["status"] == Status.NAO_VERIFICADO.value]
    problemas = [f"{rotulo_disco(d)}: {p}" for d in discos for p in d.get("problemas", [])]
    if not verificados:
        return Resultado(
            componente="discos",
            status=Status.NAO_VERIFICADO,
            dados={"discos": discos, "ignorados": ignorados, "problemas": problemas},
            mensagem=f"{MENSAGEM_NAO_VERIFICADO}: {'; '.join(problemas)}",
        )
    status = pior_status([Status(d["status"]) for d in verificados])
    mensagem = f"{len(verificados)} disco(s) verificado(s)"
    if nao_verificados:
        mensagem += f"; {len(nao_verificados)} NÃO verificado(s)"
    return Resultado(
        componente="discos",
        status=status,
        fonte="smartctl",
        dados={"discos": discos, "ignorados": ignorados, "problemas": problemas},
        mensagem=mensagem,
    )
