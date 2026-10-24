"""Parsers de recursos Redfish (JSON do BMC → dicionários simples).

Escritos contra o esquema DMTF e testados com os mockups públicos da DMTF
(``public-rackmount1``, ``public-localstorage``). Recursos de fabricante (Oem)
não são interpretados.

TODO(fixture): respostas reais de iDRAC9 (R640), iLO e XCC — gravadas pelo
``--coletar`` com credenciais — para confirmar as variações de cada fabricante.
"""

from __future__ import annotations

from typing import Any

from msscan.parsers import ErroParser


def _objeto(dados: Any, recurso: str) -> dict[str, Any]:
    if not isinstance(dados, dict):
        raise ErroParser(f"resposta Redfish de {recurso} não é um objeto JSON")
    return dados


def link(dados: Any, chave: str) -> str | None:
    """``dados[chave]["@odata.id"]`` (link para outro recurso), ou ``None``."""
    valor = dados.get(chave) if isinstance(dados, dict) else None
    if isinstance(valor, dict) and isinstance(valor.get("@odata.id"), str):
        return valor["@odata.id"]
    return None


def membros(dados: Any) -> list[str]:
    """Links ``Members`` de uma coleção Redfish."""
    colecao = _objeto(dados, "coleção")
    itens = colecao.get("Members")
    if not isinstance(itens, list):
        raise ErroParser("coleção Redfish sem 'Members'")
    return [m["@odata.id"] for m in itens if isinstance(m, dict) and isinstance(m.get("@odata.id"), str)]


def proxima_pagina(dados: Any) -> str | None:
    """``Members@odata.nextLink`` de uma coleção paginada (ex.: entradas de log)."""
    valor = dados.get("Members@odata.nextLink") if isinstance(dados, dict) else None
    return valor if isinstance(valor, str) else None


def estado(dados: Any) -> tuple[str | None, str | None]:
    """``(Health, State)`` do bloco ``Status`` (``HealthRollup`` se ``Health`` faltar)."""
    status = dados.get("Status") if isinstance(dados, dict) else None
    if not isinstance(status, dict):
        return None, None
    saude = status.get("Health") or status.get("HealthRollup")
    return (saude if isinstance(saude, str) else None), (status.get("State") if isinstance(status.get("State"), str) else None)


def _nome(item: dict[str, Any], padrao: str) -> str:
    for chave in ("Name", "MemberId", "Id"):
        if isinstance(item.get(chave), str) and item[chave].strip():
            return item[chave].strip()
    return padrao


def local_redfish(item: Any) -> str | None:
    """Local físico informado pelo BMC (``Location.PartLocation``, esquema Resource): ``ServiceLabel`` (etiqueta
    impressa no equipamento, ex.: "NIC Daughter Card", "PSU 1") ou tipo + número. ``None`` se o BMC não informar."""
    if not isinstance(item, dict):
        return None
    parte = ((item.get("Location") or {}).get("PartLocation") or {}) if isinstance(item.get("Location"), dict) else {}
    if not isinstance(parte, dict):
        return None
    rotulo = parte.get("ServiceLabel")
    if isinstance(rotulo, str) and rotulo.strip():
        return rotulo.strip()
    if isinstance(parte.get("LocationType"), str) and isinstance(parte.get("LocationOrdinalValue"), int):
        return f"{parte['LocationType']} {parte['LocationOrdinalValue']}"
    return None


def _numero(valor: Any) -> float | None:
    return float(valor) if isinstance(valor, (int, float)) and not isinstance(valor, bool) else None


# --- Raiz do serviço ---------------------------------------------------------------------------


def parse_raiz(dados: Any) -> dict[str, Any]:
    """Raiz ``/redfish/v1`` (lida sem autenticação): versão, fabricante e links principais."""
    raiz = _objeto(dados, "/redfish/v1")
    if "RedfishVersion" not in raiz and "@odata.id" not in raiz:
        raise ErroParser("não parece a raiz de um serviço Redfish (sem RedfishVersion)")
    sessoes = link(raiz.get("Links") or {}, "Sessions") or "/redfish/v1/SessionService/Sessions"
    return {
        "versao": raiz.get("RedfishVersion"),
        "fabricante": raiz.get("Vendor"),
        "produto": raiz.get("Product"),
        "sistemas": link(raiz, "Systems"),
        "chassis": link(raiz, "Chassis"),
        "gerenciadores": link(raiz, "Managers"),
        "contas": link(raiz, "AccountService"),
        "sessoes": sessoes,
    }


# --- Energia e temperatura (esquemas Power e Thermal) ------------------------------------------


def parse_power(dados: Any) -> dict[str, Any]:
    """Recurso ``Chassis/<id>/Power``: fontes, tensões e redundância."""
    power = _objeto(dados, "Power")
    fontes = []
    for i, fonte in enumerate(power.get("PowerSupplies") or []):
        if not isinstance(fonte, dict):
            continue
        saude, situacao = estado(fonte)
        fontes.append({
            "nome": _nome(fonte, f"Fonte {i + 1}"),
            "saude": saude,
            "estado": situacao,
            "entrada_status": fonte.get("LineInputStatus"),
            "tensao_entrada": _numero(fonte.get("LineInputVoltage")),
            "potencia_saida_w": _numero(fonte.get("LastPowerOutputWatts")),
            "capacidade_w": _numero(fonte.get("PowerCapacityWatts")),
            "modelo": fonte.get("Model"),
            "firmware": fonte.get("FirmwareVersion"),
            "local": local_redfish(fonte),
        })  # fmt: skip
    return {"fontes": fontes, "tensoes": _leituras(power.get("Voltages"), "ReadingVolts", "V"), "redundancia": _redundancia(power)}


def parse_fonte_subsistema(dados: Any) -> dict[str, Any]:
    """Um membro de ``PowerSubsystem/PowerSupplies`` (esquema novo), no mesmo formato de ``parse_power``."""
    fonte = _objeto(dados, "PowerSupply")
    saude, situacao = estado(fonte)
    return {
        "nome": _nome(fonte, "Fonte"),
        "saude": saude,
        "estado": situacao,
        "entrada_status": fonte.get("LineInputStatus"),
        "tensao_entrada": None,
        "potencia_saida_w": None,
        "capacidade_w": _numero(fonte.get("PowerCapacityWatts")),
        "modelo": fonte.get("Model"),
        "firmware": fonte.get("FirmwareVersion"),
        "local": local_redfish(fonte),
    }


def parse_thermal(dados: Any) -> dict[str, Any]:
    """Recurso ``Chassis/<id>/Thermal``: temperaturas, ventoinhas e redundância."""
    thermal = _objeto(dados, "Thermal")
    ventoinhas = []
    for i, ventoinha in enumerate(thermal.get("Fans") or []):
        if not isinstance(ventoinha, dict):
            continue
        saude, situacao = estado(ventoinha)
        leitura = _numero(ventoinha.get("Reading"))
        unidade = ventoinha.get("ReadingUnits") or "RPM"
        if leitura is None and _numero(ventoinha.get("ReadingRPM")) is not None:  # esquemas antigos
            leitura, unidade = _numero(ventoinha.get("ReadingRPM")), "RPM"
        ventoinhas.append({
            "nome": _nome(ventoinha, f"Ventoinha {i + 1}"), "saude": saude, "estado": situacao,
            "leitura": leitura, "unidade": unidade, **_limiares(ventoinha),
        })  # fmt: skip
    return {
        "temperaturas": _leituras(thermal.get("Temperatures"), "ReadingCelsius", "°C"),
        "ventoinhas": ventoinhas,
        "redundancia": _redundancia(thermal),
    }


def _limiares(item: dict[str, Any]) -> dict[str, float | None]:
    return {
        "limite_alerta": _numero(item.get("UpperThresholdNonCritical")),
        "limite_critico": _numero(item.get("UpperThresholdCritical")),
        "limite_fatal": _numero(item.get("UpperThresholdFatal")),
        "limite_inferior_alerta": _numero(item.get("LowerThresholdNonCritical")),
        "limite_inferior_critico": _numero(item.get("LowerThresholdCritical")),
        "limite_inferior_fatal": _numero(item.get("LowerThresholdFatal")),
    }


def _leituras(itens: Any, chave_leitura: str, unidade: str) -> list[dict[str, Any]]:
    resultado = []
    for i, item in enumerate(itens or []):
        if not isinstance(item, dict):
            continue
        saude, situacao = estado(item)
        resultado.append({
            "nome": _nome(item, f"Sensor {i + 1}"), "saude": saude, "estado": situacao,
            "leitura": _numero(item.get(chave_leitura)), "unidade": unidade, **_limiares(item), "local": local_redfish(item),
        })  # fmt: skip
    return resultado


def _redundancia(recurso: dict[str, Any]) -> list[dict[str, Any]]:
    grupos = []
    for grupo in recurso.get("Redundancy") or []:
        if isinstance(grupo, dict):
            saude, situacao = estado(grupo)
            grupos.append({"nome": _nome(grupo, "Redundância"), "modo": grupo.get("Mode"), "saude": saude, "estado": situacao})
    return grupos


# --- Armazenamento -------------------------------------------------------------------------------


def parse_storage(dados: Any) -> dict[str, Any]:
    """Recurso ``Systems/<id>/Storage/<id>``: controladoras, links de discos e volumes."""
    storage = _objeto(dados, "Storage")
    saude, situacao = estado(storage)
    controladoras = []
    for i, ctrl in enumerate(storage.get("StorageControllers") or []):
        if isinstance(ctrl, dict):
            c_saude, c_estado = estado(ctrl)
            controladoras.append({
                "nome": _nome(ctrl, f"Controladora {i + 1}"), "saude": c_saude, "estado": c_estado,
                "modelo": ctrl.get("Model"), "firmware": ctrl.get("FirmwareVersion"),
            })  # fmt: skip
    discos = [d["@odata.id"] for d in storage.get("Drives") or [] if isinstance(d, dict) and "@odata.id" in d]
    return {
        "nome": _nome(storage, "Armazenamento"),
        "saude": saude,
        "estado": situacao,
        "controladoras": controladoras,
        "controladoras_link": link(storage, "Controllers"),
        "discos": discos,
        "volumes": link(storage, "Volumes"),
    }


def parse_controladora(dados: Any) -> dict[str, Any]:
    """Membro de ``Storage/<id>/Controllers`` (esquema novo ``StorageController``)."""
    ctrl = _objeto(dados, "StorageController")
    saude, situacao = estado(ctrl)
    return {"nome": _nome(ctrl, "Controladora"), "saude": saude, "estado": situacao,
            "modelo": ctrl.get("Model"), "firmware": ctrl.get("FirmwareVersion"), "local": local_redfish(ctrl)}  # fmt: skip


def parse_volume(dados: Any) -> dict[str, Any]:
    """Volume (disco virtual/array): saúde, tipo de RAID e operações em andamento (ex.: rebuild)."""
    volume = _objeto(dados, "Volume")
    saude, situacao = estado(volume)
    operacoes = []
    for op in volume.get("Operations") or []:
        if isinstance(op, dict):
            operacoes.append({"nome": op.get("OperationName"), "progresso": _numero(op.get("PercentageComplete"))})
    return {
        "nome": _nome(volume, "Volume"),
        "saude": saude,
        "estado": situacao,
        "raid": volume.get("RAIDType") or volume.get("VolumeType"),
        "operacoes": operacoes,
    }


def parse_disco(dados: Any) -> dict[str, Any]:
    """Disco físico (``Drive``): saúde, falha prevista e desgaste."""
    disco = _objeto(dados, "Drive")
    saude, situacao = estado(disco)
    capacidade = _numero(disco.get("CapacityBytes"))
    return {
        "nome": _nome(disco, "Disco"),
        "saude": saude,
        "estado": situacao,
        "falha_prevista": disco.get("FailurePredicted") if isinstance(disco.get("FailurePredicted"), bool) else None,
        "vida_restante_pct": _numero(disco.get("PredictedMediaLifeLeftPercent")),
        "midia": disco.get("MediaType"),
        "protocolo": disco.get("Protocol"),
        "modelo": disco.get("Model"),
        "capacidade_gb": round(capacidade / 1e9) if capacidade else None,
        "local": local_redfish(disco) or local_redfish({"Location": disco.get("PhysicalLocation")}),
    }


# --- Log e contas ------------------------------------------------------------------------------


def parse_servico_log(dados: Any) -> dict[str, Any]:
    """``LogService``: identificador, tipo de entrada e link das entradas."""
    servico = _objeto(dados, "LogService")
    return {
        "id": servico.get("Id"),
        "nome": _nome(servico, "Log"),
        "tipo": servico.get("LogEntryType"),
        "entradas": link(servico, "Entries"),
        "limpar": _alvo_acao(servico, "#LogService.ClearLog"),
    }


def _alvo_acao(recurso: dict[str, Any], nome: str) -> str | None:
    """``target`` de uma ação padrão (``Actions["#Recurso.Acao"]``), ou ``None``."""
    acoes = recurso.get("Actions")
    acao = acoes.get(nome) if isinstance(acoes, dict) else None
    alvo = acao.get("target") if isinstance(acao, dict) else None
    return alvo if isinstance(alvo, str) and alvo else None


# --- NTP e relógio do BMC (ações corretivas, fase 10) ------------------------------------------


def parse_gerenciador(dados: Any) -> dict[str, Any]:
    """``Manager``: id, data e hora do BMC, fuso e links usados pelas ações (NetworkProtocol, LogServices)."""
    gerente = _objeto(dados, "Manager")
    return {
        "id": gerente.get("Id"),
        "data_hora": gerente.get("DateTime") if isinstance(gerente.get("DateTime"), str) else None,
        "fuso": gerente.get("DateTimeLocalOffset") if isinstance(gerente.get("DateTimeLocalOffset"), str) else None,
        "protocolos": link(gerente, "NetworkProtocol"),
        "logs": link(gerente, "LogServices"),
    }


def parse_ntp_protocolo(dados: Any) -> dict[str, Any] | None:
    """``ManagerNetworkProtocol.NTP`` (DMTF): ``{"ativo", "servidores"}``; ``None`` se o BMC não publica NTP."""
    protocolo = _objeto(dados, "ManagerNetworkProtocol")
    ntp = protocolo.get("NTP")
    if not isinstance(ntp, dict):
        return None
    servidores = ntp.get("NTPServers")
    return {
        "ativo": ntp.get("ProtocolEnabled") if isinstance(ntp.get("ProtocolEnabled"), bool) else None,
        "servidores": [s for s in servidores if isinstance(s, str) and s.strip()] if isinstance(servidores, list) else [],
        # Valores exatos, para desfazer a alteração (fase 10, ajuste de 28/09/2026).
        "bruto": {k: ntp[k] for k in ("ProtocolEnabled", "NTPServers") if k in ntp},
    }


def parse_ntp_dell(dados: Any) -> dict[str, Any] | None:
    """Atributos do iDRAC (``Managers/<id>/Attributes``): NTP em ``NTPConfigGroup.1``; ``None`` se não houver.

    MOCKUP(rodada final): nomes dos atributos da documentação da Dell (``NTPConfigGroup.1.NTPEnable`` Enabled/
    Disabled, ``NTP1`` a ``NTP3``), sem coleta real do iDRAC9 ainda.
    """
    atributos = _objeto(dados, "Attributes").get("Attributes")
    if not isinstance(atributos, dict) or "NTPConfigGroup.1.NTPEnable" not in atributos:
        return None
    ativo = atributos.get("NTPConfigGroup.1.NTPEnable")
    return {
        "ativo": True if ativo == "Enabled" else False if ativo == "Disabled" else None,
        "servidores": [str(atributos[f"NTPConfigGroup.1.NTP{n}"]).strip() for n in (1, 2, 3)
                       if str(atributos.get(f"NTPConfigGroup.1.NTP{n}") or "").strip()],
        "bruto": {k: atributos[k] for k in ("NTPConfigGroup.1.NTPEnable", "NTPConfigGroup.1.NTP1", "NTPConfigGroup.1.NTP2",
                                            "NTPConfigGroup.1.NTP3") if k in atributos},
    }  # fmt: skip


def parse_entradas_log(dados: Any) -> list[dict[str, Any]]:
    """Entradas expandidas de ``LogServices/<id>/Entries`` (``Members`` com o conteúdo)."""
    colecao = _objeto(dados, "LogEntryCollection")
    itens = colecao.get("Members")
    if not isinstance(itens, list):
        raise ErroParser("coleção de log sem 'Members'")
    entradas = []
    for item in itens:
        if not isinstance(item, dict):
            continue
        entradas.append({
            "id": str(item.get("Id", "?")),
            "data": item.get("Created") or "",
            "severidade": item.get("Severity") or "",
            "mensagem": (item.get("Message") or "").strip(),
            "codigo": item.get("MessageId") or "",
            "resolvida": item.get("Resolved") if isinstance(item.get("Resolved"), bool) else None,
            "completa": "Message" in item or "Severity" in item,
        })  # fmt: skip
    return entradas


def parse_conta(dados: Any) -> dict[str, Any]:
    """``ManagerAccount``: nome de usuário e papel (``RoleId``)."""
    conta = _objeto(dados, "ManagerAccount")
    return {"usuario": conta.get("UserName"), "papel": conta.get("RoleId")}


def parse_sessao(dados: Any) -> dict[str, Any]:
    """``Session``: nome de usuário e, em esquemas novos, os papéis (``Roles``)."""
    sessao = _objeto(dados, "Session")
    papeis = sessao.get("Roles") if isinstance(sessao.get("Roles"), list) else []
    return {"usuario": sessao.get("UserName"), "papeis": [p for p in papeis if isinstance(p, str)]}


# --- Placas de rede ----------------------------------------------------------------------------


def parse_adaptador_rede(dados: Any) -> dict[str, Any]:
    """``Chassis/<id>/NetworkAdapters/<id>``: modelo, saúde, firmware e links das portas."""
    adaptador = _objeto(dados, "NetworkAdapter")
    saude, situacao = estado(adaptador)
    firmwares = [c.get("FirmwarePackageVersion") for c in adaptador.get("Controllers") or [] if isinstance(c, dict)]
    return {
        "nome": _nome(adaptador, "Placa de rede"),
        "fabricante": adaptador.get("Manufacturer"),
        "modelo": adaptador.get("Model"),
        "saude": saude,
        "estado": situacao,
        "firmware": ", ".join(f for f in firmwares if isinstance(f, str)) or None,
        "local": local_redfish(adaptador) or next((l for c in adaptador.get("Controllers") or [] if (l := local_redfish(c))), None),
        # "Ports" (esquema Port, novo) ou "NetworkPorts" (esquema NetworkPort, antigo)
        "portas": link(adaptador, "Ports") or link(adaptador, "NetworkPorts"),
    }


def parse_porta_rede(dados: Any) -> dict[str, Any]:
    """Porta de rede (esquema ``Port`` ou ``NetworkPort``): link, velocidade atual e máxima, meio físico."""
    porta = _objeto(dados, "Port")
    saude, situacao = estado(porta)
    atual = _numero(porta.get("CurrentSpeedGbps"))
    atual_mbps = atual * 1000 if atual is not None else _numero(porta.get("CurrentLinkSpeedMbps"))
    maxima = _numero(porta.get("MaxSpeedGbps"))
    maxima_mbps = maxima * 1000 if maxima is not None else None
    if maxima_mbps is None:  # NetworkPort: SupportedLinkCapabilities[].LinkSpeedMbps
        velocidades = [
            _numero(c.get("LinkSpeedMbps")) for c in porta.get("SupportedLinkCapabilities") or [] if isinstance(c, dict)
        ]
        velocidades = [v for v in velocidades if v is not None]
        maxima_mbps = max(velocidades) if velocidades else None
    return {
        "nome": _nome(porta, "Porta"),
        "saude": saude,
        "estado": situacao,
        "link": porta.get("LinkStatus") if isinstance(porta.get("LinkStatus"), str) else None,
        "velocidade_mbps": atual_mbps,
        "velocidade_maxima_mbps": maxima_mbps,
        "meio": porta.get("PortMedium"),
    }


# --- Inventário de firmware --------------------------------------------------------------------


def parse_item_firmware(dados: Any) -> dict[str, Any]:
    """Membro de ``UpdateService/FirmwareInventory`` (``SoftwareInventory``).

    TODO(fixture): FirmwareInventory real do iDRAC9 (na Dell, ``SoftwareId`` é o ``componentID``
    do catálogo; ``Id`` como "Installed-159-2.19.1__BIOS.Setup.1-1"). Hoje: mockups da DMTF.
    """
    item = _objeto(dados, "SoftwareInventory")
    saude, situacao = estado(item)
    identificador = str(item.get("Id") or "")
    return {
        "nome": _nome(item, "Firmware"),
        "versao": item.get("Version") if isinstance(item.get("Version"), str) else None,
        "software_id": str(item["SoftwareId"]) if item.get("SoftwareId") not in (None, "") else None,
        "atualizavel": item.get("Updateable") if isinstance(item.get("Updateable"), bool) else None,
        "instalado": not identificador.startswith(("Previous-", "Available-")),
        "anterior": identificador.startswith("Previous-"),
        "id": identificador or None,
        "saude": saude,
        "estado": situacao,
    }


def parse_update_service(dados: Any) -> dict[str, Any]:
    """``UpdateService``: onde enviar firmware e quando o BMC aceita aplicar.

    ``multipart`` é o ``MultipartHttpPushUri`` (o único envio que o MSscan usa; sem ele, atualização automática
    pelo Redfish não é suportada). MOCKUP(rodada final): no iDRAC9, ``/redfish/v1/UpdateService/MultipartUpload``
    (exemplo oficial da Dell); o mockup da DMTF só tem ``HttpPushUri``.
    """
    servico = _objeto(dados, "UpdateService")
    opcoes = servico.get("HttpPushUriOptions") if isinstance(servico.get("HttpPushUriOptions"), dict) else {}
    aplicacao = opcoes.get("HttpPushUriApplyTime") if isinstance(opcoes.get("HttpPushUriApplyTime"), dict) else {}
    tempos = aplicacao.get("ApplyTime@Redfish.AllowableValues")
    texto = lambda chave: servico[chave] if isinstance(servico.get(chave), str) and servico[chave] else None  # noqa: E731
    return {
        "multipart": texto("MultipartHttpPushUri"),
        "http_push": texto("HttpPushUri"),
        "habilitado": servico.get("ServiceEnabled") if isinstance(servico.get("ServiceEnabled"), bool) else None,
        "tempos_aplicacao": [t for t in tempos if isinstance(t, str)] if isinstance(tempos, list) else [],
        "inventario": link(servico, "FirmwareInventory"),
        "tarefas_ocupado": servico.get("HttpPushUriOptionsBusy") is True,
        "simple_update": _alvo_acao(servico, "#UpdateService.SimpleUpdate"),
    }


def _alvo_acao(objeto: dict[str, Any], acao: str) -> str | None:
    """``target`` de uma ação em ``Actions`` (ex.: ``#UpdateService.SimpleUpdate``), ou ``None``."""
    acoes = objeto.get("Actions") if isinstance(objeto.get("Actions"), dict) else {}
    dados = acoes.get(acao) if isinstance(acoes.get(acao), dict) else {}
    return dados["target"] if isinstance(dados.get("target"), str) and dados["target"] else None


ESTADOS_TAREFA_FIM = {"Completed", "Exception", "Killed", "Cancelled"}


def parse_tarefa(dados: Any) -> dict[str, Any]:
    """``Task`` do ``TaskService`` (acompanhamento do envio de firmware).

    Padrão DMTF: ``TaskState``, ``TaskStatus``, ``PercentComplete``, ``Messages``. MOCKUP(rodada final): no iDRAC9 a
    tarefa também traz ``Oem.Dell.JobState``/``Oem.Dell.Message`` e a mensagem "scheduled" quando o firmware ficou
    agendado para o próximo reinício (campos lidos pelo exemplo oficial da Dell; confirmar na coleta real).
    """
    tarefa = _objeto(dados, "Task")
    mensagens = [
        str(m.get("Message")) for m in tarefa.get("Messages") or [] if isinstance(m, dict) and m.get("Message")
    ]  # fmt: skip
    oem = ((tarefa.get("Oem") or {}).get("Dell") or {}) if isinstance(tarefa.get("Oem"), dict) else {}
    oem = oem if isinstance(oem, dict) else {}
    dell_estado = oem.get("JobState") if isinstance(oem.get("JobState"), str) else None
    dell_mensagem = oem.get("Message") if isinstance(oem.get("Message"), str) else None
    estado_tarefa = tarefa.get("TaskState") if isinstance(tarefa.get("TaskState"), str) else None
    situacao = tarefa.get("TaskStatus") if isinstance(tarefa.get("TaskStatus"), str) else None
    textos = " ".join(mensagens + [dell_mensagem or "", dell_estado or ""]).lower()
    falhou = estado_tarefa in ("Exception", "Killed", "Cancelled") or situacao == "Critical" or \
        any(p in textos for p in ("fail", "error", "unable"))  # fmt: skip
    agendada = not falhou and ("scheduled" in textos or dell_estado == "Scheduled")
    return {
        "id": str(tarefa.get("Id") or "") or None,
        "estado": estado_tarefa,
        "status": situacao,
        "percentual": _numero(tarefa.get("PercentComplete")),
        "mensagens": mensagens,
        "dell_estado": dell_estado,
        "dell_mensagem": dell_mensagem,
        "falhou": falhou,
        "agendada": agendada,
        "concluida": not falhou and not agendada and estado_tarefa == "Completed",
        "terminou": falhou or agendada or estado_tarefa in ESTADOS_TAREFA_FIM,
    }


def parse_boot(dados: Any) -> dict[str, Any]:
    """Bloco ``Boot`` de um ``ComputerSystem``: se o boot único pelo USB é possível."""
    sistema = _objeto(dados, "ComputerSystem")
    boot = sistema.get("Boot") if isinstance(sistema.get("Boot"), dict) else {}
    alvos = boot.get("BootSourceOverrideTarget@Redfish.AllowableValues")
    return {
        "alvos": [a for a in alvos if isinstance(a, str)] if isinstance(alvos, list) else [],
        "alvo": boot.get("BootSourceOverrideTarget") if isinstance(boot.get("BootSourceOverrideTarget"), str) else None,
        "habilitado": boot.get("BootSourceOverrideEnabled") if isinstance(boot.get("BootSourceOverrideEnabled"), str) else None,
        "modo": boot.get("BootSourceOverrideMode") if isinstance(boot.get("BootSourceOverrideMode"), str) else None,
    }
