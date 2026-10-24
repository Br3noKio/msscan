"""Atualizações de firmware SOMENTE INFORMATIVAS (fase 8): inventário × catálogo Dell + pré-checagens.

Nada é baixado além do catálogo e nada é aplicado. Regras (CLAUDE.md 11): escopo só Dell; modelo
precisa bater EXATAMENTE (systemID do SMBIOS + nome do modelo no catálogo); nunca downgrade;
comparação incerta → "não foi possível determinar se há atualização"; o banco de compatibilidade só
restringe. As pré-checagens mostram, desde já, o que bloquearia a aplicação (fase 9).
"""

from __future__ import annotations

import re

from datetime import datetime
from typing import Any

from msscan import textos
from msscan.atualizacao.versoes import ATUALIZACAO, IGUAL, INCERTA, INSTALADA_MAIS_NOVA, _partes, comparar_versoes
from msscan.core.modelo import Status
from msscan.parsers.dmidecode import parse_sku_dell

SEM_PACOTE = "sem_pacote"
TIPOS_FIRMWARE = ("BIOS", "FRMW")
NAO_SUPORTADO = textos.t("py_verificacao.atualizacao_automatica_nao_suportada_neste")
FORA_DE_SUPORTE = textos.t("py_verificacao.fora_de_suporte_do_fabricante")
# Três estados por componente (CLAUDE.md 15.2). "Não é necessária" SÓ com confirmação: a versão instalada é igual ou
# mais nova que o pacote mais recente do catálogo para ESTE modelo. Qualquer dúvida → "não foi possível determinar".
DISPONIVEL, NAO_NECESSARIA, INDETERMINADO = "disponivel", "nao_necessaria", "indeterminado"
ESTADO = {ATUALIZACAO: DISPONIVEL, IGUAL: NAO_NECESSARIA, INSTALADA_MAIS_NOVA: NAO_NECESSARIA,
          INCERTA: INDETERMINADO, SEM_PACOTE: INDETERMINADO}  # fmt: skip
TEXTO_ESTADO = {DISPONIVEL: textos.t("py_verificacao.atualizacao_disponivel"), NAO_NECESSARIA: textos.t("py_verificacao.nao_e_necessaria_a_atualizacao"),
                INDETERMINADO: textos.t("py_verificacao.nao_foi_possivel_determinar")}  # fmt: skip
TEXTO_SITUACAO = {situacao: TEXTO_ESTADO[estado] for situacao, estado in ESTADO.items()}
MENSAGEM_FONTE_INCERTA = (
    textos.t("py_verificacao.fonte_de_alimentacao_com_problema")
)


def _nome_modelo(nome: str | None) -> str:
    """Nome do modelo sem maiúsculas nem espaços: o catálogo da Dell escreve "R650 XS", "R750 xa" e "R760XA", e o SMBIOS
    "PowerEdge R650xs" (catálogo conferido em 14/10/2026). O systemID continua exato: é ele que garante o modelo."""
    return re.sub(r"[\s_-]+", "", (nome or "").casefold())


def modelo_no_catalogo(catalogo: dict[str, Any], sku: str | None, modelo: str | None) -> tuple[str | None, str]:
    """``(systemID, "")`` se o modelo bate EXATAMENTE com o catálogo; senão ``(None, motivo)``."""
    dell = parse_sku_dell(sku)
    if dell is None:
        return None, textos.t("py_verificacao.systemid_nao_informado_pelo_smbios")
    nomes = {
        f"{s['marca']} {s['modelo']}".strip()
        for c in catalogo["componentes"] for s in c["sistemas"] if s["system_id"] == dell["system_id"]
    }  # fmt: skip
    if not nomes:
        return None, textos.t("py_verificacao.systemid_nao_aparece_no_catalogo", dell['system_id'])
    # A marca muda conforme o pacote: o iDRAC 2.40.40.40 (catálogo 26.10.08) lista o R530 como "Enterprise Servers R530",
    # e o SMBIOS diz "PowerEdge R530". Vale o nome com a marca do catálogo ou com "PowerEdge"; o modelo continua exato.
    # Catálogos antigos juntam produtos do mesmo System ID com "/" (22.12.30: "R320/NX400"): cada nome vale sozinho, exato.
    aceitos = {_nome_modelo(f"{marca} {nome}") for c in catalogo["componentes"] for s in c["sistemas"]
               if s["system_id"] == dell["system_id"] for nome in s["modelo"].split("/") for marca in (s["marca"], "PowerEdge")}  # fmt: skip
    if _nome_modelo(modelo) not in aceitos:
        return None, textos.t("py_verificacao.o_modelo_nao_bate_exatamente", modelo, ', '.join(sorted(nomes)), dell['system_id'])
    return dell["system_id"], ""


def _candidatos(item: dict[str, Any], componentes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if item.get("component_id"):
        return [c for c in componentes if any(d["component_id"] == item["component_id"] for d in c["dispositivos"])]
    if item.get("tipo"):
        return [c for c in componentes if c["tipo"] == item["tipo"]]
    if item.get("categoria"):
        return [c for c in componentes if c["categoria"] == item["categoria"]]
    if item.get("pci"):
        return [c for c in componentes if _dispositivo_pci(item, c)]
    return []


def _dispositivo_pci(item: dict[str, Any], pacote: dict[str, Any]) -> dict[str, Any] | None:
    """Dispositivo do pacote que lista os IDs PCI da placa (qualquer um dos ``<PCIInfo>`` dele)."""
    return next((d for d in pacote["dispositivos"] if item.get("pci") and item["pci"] in (d.get("pcis") or [])), None)


def _data_br(iso: str | None) -> str:
    """``2026-06-01`` → ``01/06/2026`` (vazio se não for data)."""
    try:
        return datetime.strptime(str(iso), "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        return ""


def data_pacote(texto: str) -> datetime | None:
    """``releaseDate`` do catálogo real ("January 24, 2026")."""
    try:
        return datetime.strptime((texto or "").strip(), "%B %d, %Y")
    except ValueError:
        return None


def _mais_nova(candidatos: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pacote aplicável mais recente (CLAUDE.md 15.2): maior versão; empate → lançamento mais novo.

    Versões não comparáveis entre si: ``None`` (quem chama diz "não foi possível determinar").
    """
    numericas = [(p, c) for c in candidatos if (p := _partes(c["versao"])) is not None]
    if not numericas or len(numericas) != len(candidatos):
        return None if len(candidatos) != 1 else candidatos[0]
    tamanho = max(len(p) for p, _ in numericas)
    return max(numericas, key=lambda par: (par[0] + (0,) * (tamanho - len(par[0])),
                                           data_pacote(par[1]["data"]) or datetime.min))[1]  # fmt: skip


def pre_requisitos(pacote: dict[str, Any], system_id: str, inventario: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pré-requisitos declarados pela Dell (``<Dependency>``) para este modelo, conferidos contra o inventário.

    ``atendido``: True (instalada >= exigida), False (instalada mais velha: atualizar antes) ou None (não dá para
    confirmar — componente sem versão no inventário, ex.: sem credenciais do BMC).
    """
    resultado, vistos = [], set()
    for dispositivo in pacote.get("dispositivos") or []:
        for dep in dispositivo.get("dependencias") or []:
            if dep["sistemas"] and system_id not in dep["sistemas"]:
                continue
            chave = (dep["component_id"], dep["versao"])
            if chave in vistos:
                continue
            vistos.add(chave)
            instalado = next((i for i in inventario if i.get("component_id") == dep["component_id"] and i.get("versao")), None)
            situacao, _ = comparar_versoes(instalado["versao"], dep["versao"], instalado["truncada"]) if instalado else (INCERTA, "")
            atendido = {ATUALIZACAO: False, IGUAL: True, INSTALADA_MAIS_NOVA: True}.get(situacao)
            nome = dep["nome"] or f"componente {dep['component_id']}"
            texto = (textos.t("py_verificacao.exige_na_versao_ou_mais", nome, dep['versao']) + (f" (pacote {dep['pacote']})" if dep["pacote"] else "")
                     + {True: ": atendido", False: textos.t("py_verificacao.instalada_atualize_este_antes", instalado['versao'] if instalado else '?'),
                        None: textos.t("py_verificacao.nao_foi_possivel_confirmar_a")}[atendido])  # fmt: skip
            resultado.append({"component_id": dep["component_id"], "versao": dep["versao"], "nome": nome, "atendido": atendido,
                              "texto": texto})  # fmt: skip
    return resultado


def _motivo_sem_pacote(item: dict[str, Any], todos: list[dict[str, Any]], do_modelo: list[dict[str, Any]]) -> str:
    """Por que não há pacote: nunca vira "não é necessária" (CLAUDE.md 15.2). Com o nome da placa igual ao de um
    dispositivo do catálogo para este modelo, aponta o pacote PROVÁVEL (não oferecido: a Dell não lista estes IDs)."""
    pci = item.get("pci")
    if not pci:
        return textos.t("py_verificacao.nenhum_pacote_do_catalogo_da")
    ids = f"{pci['vendorID']}:{pci['deviceID']} {pci['subVendorID']}:{pci['subDeviceID']}"
    motivo = (textos.t("py_verificacao.os_ids_pci_desta_placa", ids))
    nome = _nome_placa(item.get("nome") or "")
    for pacote in do_modelo:
        for dispositivo in pacote["dispositivos"]:
            if nome and _nome_placa(dispositivo["nome"]) == nome:
                return motivo + textos.t("py_verificacao.pacote_provavel_mesma_placa_no", dispositivo['component_id'], pacote['nome'])
    return motivo


def _nome_placa(nome: str) -> str:
    """Nome da placa sem fabricante nem interfaces: "Dell Ethernet 10G 4P X710/I350 rNDC (eno1np0)" e
    "Intel(R) Ethernet 10G 4P X710/I350 rNDC" → "ethernet 10g 4p x710/i350 rndc"."""
    import re as _re

    nome = _re.sub(r"\s*\([^)]*\)\s*$", "", nome)
    nome = _re.sub(r"^(dell|intel\(r\)|intel|broadcom|mellanox|qlogic)\s+", "", nome.strip(), flags=_re.IGNORECASE)
    return nome.lower().strip()


def cruzar(inventario: list[dict[str, Any]], catalogo: dict[str, Any], system_id: str) -> list[dict[str, Any]]:
    """Cada item instalado com o pacote mais novo do catálogo para ESTE modelo e a situação."""
    componentes = [
        c for c in catalogo["componentes"]
        if c["tipo"] in TIPOS_FIRMWARE and any(s["system_id"] == system_id for s in c["sistemas"])
    ]  # fmt: skip
    base = (catalogo.get("base") or "downloads.dell.com").strip("/")
    resultado = []
    for item in inventario:
        candidatos = _candidatos(item, componentes)
        pacote = _mais_nova(candidatos)
        if not candidatos:
            situacao, motivo = SEM_PACOTE, _motivo_sem_pacote(item, catalogo["componentes"], componentes)
        elif pacote is None:
            situacao, motivo = INCERTA, textos.t("py_verificacao.versoes_do_catalogo_nao_comparaveis")
        else:
            situacao, motivo = comparar_versoes(item["versao"], pacote["versao"], item["truncada"])
            if situacao == INCERTA and item["versao"] is None and item.get("observacao"):
                motivo = item["observacao"]
        final = None
        if situacao == ATUALIZACAO and pacote:
            degrau = _degrau(item, candidatos, pacote)
            if degrau is not None:
                final, pacote = pacote["versao"], degrau
        estado = ESTADO[situacao]
        rotulo = TEXTO_ESTADO[estado]
        if estado == DISPONIVEL and pacote:
            rotulo += f" ({pacote['versao']}" + (f", {textos.data_br(pacote['data'], com_hora=False)}" if pacote["data"] else "") + ")"
        if final:
            rotulo += textos.t("py_verificacao.degrau_antes_da_versao_final", pacote["versao"], final)
        requisitos = pre_requisitos(pacote, system_id, inventario) if pacote and estado == DISPONIVEL else []
        dispositivo = _dispositivo_pci(item, pacote) if pacote and not item.get("component_id") else None
        if dispositivo is not None:
            # Mesmo pacote e mesmo dispositivo do catálogo (ex.: as portas X710 e I350 do rNDC, componente 102303): é um
            # firmware só, instalado uma vez (CLAUDE.md 19.3). Junta só com a mesma versão instalada.
            igual = next((r for r in resultado if r["component_id"] == dispositivo["component_id"] and r["pacote"]
                          and r["pacote"]["url"].endswith(pacote["caminho"]) and r["instalada"] == item["versao"]), None)  # fmt: skip
            if igual is not None:
                igual["interfaces"] += item.get("interfaces") or []
                igual["componente"] = f"{dispositivo['nome'] or igual['componente']} ({', '.join(igual['interfaces'])})"
                continue
        resultado.append({
            "id": f"item{len(resultado) + 1}",
            "componente": item["nome"],
            "component_id": item.get("component_id") or (dispositivo or {}).get("component_id"),
            "interfaces": list(item.get("interfaces") or []),
            "instalada": item["versao"],
            "fonte": item["fonte"],
            "disponivel": pacote["versao"] if pacote else None,
            "situacao": situacao,
            "estado": estado,
            "texto_situacao": rotulo,
            "motivo": motivo if estado != DISPONIVEL else "",
            "pre_requisitos": requisitos,
            "versao_final": final,
            "versao_bruta": item.get("versao_bruta"),
            "pacote": {
                "nome": pacote["nome"], "versao_dell": pacote["versao_dell"], "data": pacote["data"],
                "criticidade": pacote["criticidade"], "reinicio": pacote["reinicio"], "tamanho": pacote["tamanho"],
                "url": f"https://{base}/{pacote['caminho'].lstrip('/')}", "sha256": pacote["hash"] if pacote["algoritmo_hash"] == "SHA256" else "",
                "md5": pacote["md5"], "categoria": pacote["categoria"], "tipo": pacote["tipo"], "release": pacote["pacote"],
            } if pacote else None,
        })  # fmt: skip
    return resultado


def _degrau(item: dict[str, Any], candidatos: list[dict[str, Any]], pacote: dict[str, Any]) -> dict[str, Any] | None:
    """Pré-requisito publicado pela Dell (seção ``<Prerequisites>`` do catálogo, ex.: iDRAC 2.40.40.40) entre a versão instalada
    e a mais nova: vai ele primeiro, como nas ferramentas da Dell; a mais nova fica para a verificação seguinte (15/10/2026)."""
    degraus = [c for c in candidatos if c.get("prerequisito") and c is not pacote
               and comparar_versoes(item["versao"], c["versao"], item["truncada"])[0] == ATUALIZACAO
               and comparar_versoes(c["versao"], pacote["versao"], False)[0] == ATUALIZACAO]  # fmt: skip
    return min(degraus, key=lambda c: _partes(c["versao"]) or (), default=None)


def prechecagens(relatorio: dict[str, Any], compat: dict[str, Any] | None) -> dict[str, list[str]]:
    """O que BLOQUEARIA a aplicação (CLAUDE.md 11.3) e os avisos com confirmação extra. Informativo aqui.

    ``bloqueios_hardware`` (fonte, RAID, sensor): na instalação, o "Plano de atualização" relê o hardware na hora e o técnico
    pode assumir esses riscos (pedido de 09/10/2026). ``bloqueios``: os que nada libera (banco de compatibilidade)."""
    por_nome = {m["componente"]: m for m in relatorio.get("medido") or []}
    bloqueios, avisos, por_componente = [], [], {"raid": []}
    hardware: list[str] = []
    fontes = por_nome.get("fontes") or {}
    dados_fontes = fontes.get("dados") or {}
    if dados_fontes.get("defeito"):
        hardware.append(textos.t("py_verificacao.fonte_de_alimentacao_com_defeito"))
    if dados_fontes.get("classificacao_incerta"):
        hardware.append(MENSAGEM_FONTE_INCERTA)
    redundancia = bool(dados_fontes.get("redundancia_perdida") and not dados_fontes.get("defeito"))
    if redundancia:
        avisos.append(textos.t("py_verificacao.redundancia_de_energia_perdida_fonte"))  # fmt: skip
    raid = por_nome.get("raid") or {}
    if raid.get("status") in (Status.FALHA.value, Status.ALERTA.value):
        hardware.append(textos.t("py_verificacao.raid_com_problema_degradado_em", raid.get('mensagem')))
    elif raid.get("status") == Status.NAO_VERIFICADO.value:
        por_componente["raid"].append(textos.t("py_verificacao.raid_desconhecido_atualizacao_de_controladora"))
    sensores = por_nome.get("sensores") or {}
    if sensores.get("status") == Status.FALHA.value:
        hardware.append(textos.t("py_verificacao.sensor_em_estado_critico", sensores.get('mensagem')))
    for bloqueio in ((compat or {}).get("entrada") or {}).get("atualizacao", {}).get("bloqueios") or []:
        bloqueios.append(textos.t("py_verificacao.banco_de_compatibilidade", bloqueio))
    avisos.append(textos.t("py_verificacao.espaco_livre_e_hash_sha"))
    return {"bloqueios": bloqueios, "bloqueios_hardware": hardware, "avisos": avisos, "por_componente": por_componente,
            "redundancia_perdida": redundancia}  # fmt: skip


def verificar_atualizacoes(
    relatorio: dict[str, Any], contexto: dict[str, Any], inventario: list[dict[str, Any]], fonte_inventario: str,
    catalogo: dict[str, Any] | None, origem_catalogo: str, aviso_catalogo: str = "", legado: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Seção ``relatorio["atualizacoes"]``. ``contexto``: fabricante, modelo, sku e compat do equipamento."""
    base = {"situacao": "verificada", "somente_informativo": True, "fonte_inventario": fonte_inventario,
            "catalogo": {"origem": origem_catalogo, "aviso": aviso_catalogo}, "itens": [], "bloqueios": [], "avisos": []}  # fmt: skip
    if not (contexto.get("fabricante") or "").lower().startswith("dell"):
        return {**base, "situacao": "nao_suportada", "mensagem": textos.t("py_verificacao.catalogo_automatico_so_da_dell", NAO_SUPORTADO)}
    if catalogo is None:
        return {**base, "situacao": "falhou", "mensagem": aviso_catalogo or textos.t("py_verificacao.catalogo_da_dell_indisponivel")}
    base["catalogo"].update({"versao": catalogo.get("versao"), "data": catalogo.get("data")})
    if legado:
        # Pedido de 15/10/2026: modelo fora do catálogo atual → último catálogo oficial da Dell que ainda o inclui.
        base["catalogo"]["legado"] = True
        base["catalogo"]["aviso"] = "; ".join(filter(None, [textos.t("py_verificacao.catalogo_antigo_da_dell", contexto.get("modelo") or "modelo",
                                                                     legado["versao"], _data_br(legado.get("data"))), aviso_catalogo]))
    system_id, motivo = modelo_no_catalogo(catalogo, contexto.get("sku"), contexto.get("modelo"))
    checagens = prechecagens(relatorio, contexto.get("compat"))
    dell = parse_sku_dell(contexto.get("sku"))
    if system_id is None and dell is not None and not any(
            s["system_id"] == dell["system_id"] for c in catalogo["componentes"] for s in c["sistemas"]):  # fmt: skip
        return {**base, "situacao": "fora_de_suporte", "bloqueios": checagens["bloqueios"], "avisos": checagens["avisos"],
                "mensagem": textos.t("py_verificacao.o_systemid_nao_esta_mais", FORA_DE_SUPORTE, contexto.get('modelo') or 'modelo', dell['system_id'])}  # fmt: skip
    if system_id is None:
        return {**base, "situacao": "modelo_nao_confirmado", "mensagem": textos.t("py_verificacao.nenhuma_atualizacao_oferecida", motivo),
                "bloqueios": [textos.t("py_verificacao.modelo_nao_confirmado", motivo)] + checagens["bloqueios"], "avisos": checagens["avisos"]}  # fmt: skip
    itens = cruzar(inventario, catalogo, system_id)
    for item in itens:
        if item["pacote"] and any(p in (item["pacote"]["nome"] or "") for p in ("RAID", "PERC", "BOSS")):
            item["bloqueios"] = list(checagens["por_componente"]["raid"])
    contagem = {e: sum(i["estado"] == e for i in itens) for e in (DISPONIVEL, NAO_NECESSARIA, INDETERMINADO)}
    mensagem = (textos.t("py_verificacao.componente_s_inventariado_s_com", len(itens), contagem[DISPONIVEL], contagem[NAO_NECESSARIA]))  # fmt: skip
    if contagem[INDETERMINADO]:
        mensagem += textos.t("py_verificacao.em_que_nao_foi_possivel", contagem[INDETERMINADO])
    return {**base, "system_id": system_id, "itens": itens, "bloqueios": checagens["bloqueios"],
            "bloqueios_hardware": checagens["bloqueios_hardware"],
            "avisos": checagens["avisos"], "redundancia_perdida": checagens["redundancia_perdida"],
            "mensagem": mensagem + textos.t("py_verificacao.somente_informativo_nada_foi_aplicado")}  # fmt: skip


def disponibilidade_atualizacoes(rede_ok: bool, sem_rede_por_parametro: bool, replay: bool = False) -> str:
    """``""`` se a verificação pode ser oferecida; senão, o motivo."""
    if replay:
        return textos.t("py_verificacao.verificacao_de_atualizacoes_indisponivel_no")
    if sem_rede_por_parametro:
        return textos.t("py_verificacao.verificacao_de_atualizacoes_desativada_pelo")
    if not rede_ok:
        return textos.t("py_verificacao.verificacao_de_atualizacoes_indisponivel_sem")
    return ""


def executar_verificacao(ctx: Any, relatorio: dict[str, Any], executor: Any, pasta_dados: str | None,
                         monitor: Any = None, avisar: Any = None) -> dict[str, Any]:  # fmt: skip
    """Inventário + catálogo (só Dell) + cruzamento. Quem chama abre a janela de escrita (cache do catálogo).

    ``monitor``/``avisar``: rede contínua (espera a rede voltar e retoma o download do catálogo).
    """
    from msscan.atualizacao.catalogo import obter_catalogo
    from msscan.atualizacao.inventario import coletar_inventario

    contexto = {"fabricante": ctx.fabricante, "modelo": ctx.modelo, "sku": ctx.sku, "compat": ctx.compat}
    inventario, fonte = coletar_inventario(ctx)
    if not (ctx.fabricante or "").lower().startswith("dell"):
        return verificar_atualizacoes(relatorio, contexto, inventario, fonte, None, "")
    dell = parse_sku_dell(ctx.sku)
    catalogo = obter_catalogo(executor, pasta_dados, monitor=monitor, avisar=avisar, system_id=dell["system_id"] if dell else None)
    resultado = verificar_atualizacoes(relatorio, contexto, inventario, fonte, catalogo.dados, catalogo.origem, catalogo.aviso,
                                       legado=catalogo.legado)
    # Acesso ao iDRAC para instalar (3c/3d): a causa real e o botão certo ("Informar credenciais" ou "Configurar acesso").
    from msscan.atualizacao.aplicacao import ler_bmc, motivo_metodo

    resultado["acesso"] = motivo_metodo(ler_bmc(ctx))
    # Pacotes .BIN do catálogo Linux da Dell (caminho f): baixados já aqui, para a instalação achá-los no cache mesmo que o
    # acesso ao iDRAC caia depois (pedido de 21/10/2026).
    from msscan.atualizacao.catalogo import obter_pacotes_linux

    bin_linux, _ = obter_pacotes_linux(executor, pasta_dados, monitor, avisar) if catalogo.dados else ([], "")
    dados = {**catalogo.dados, "bin_linux": bin_linux} if catalogo.dados else None
    resultado["sem_idrac"] = sem_idrac(resultado, dados, ctx.fabricante, ctx.modelo)
    return resultado


def sem_idrac(secao: dict[str, Any], catalogo: dict[str, Any] | None, fabricante: str | None, modelo: str | None) -> dict[str, Any] | None:
    """iDRAC sem acesso daqui e pacote do catálogo só ``.EXE`` (sem ``.BIN`` oficial da mesma versão, caminho f): o que sobra
    é o pacote Linux baixado à mão. Avisado ANTES do ATUALIZAR, com a página de drivers do modelo (pedido de 20/10/2026).
    O service tag não vai aqui: o texto entra no relatório."""
    from msscan.atualizacao.aplicacao import IDRAC_INACESSIVEL, NAO_SUPORTADO_AQUI
    from msscan.atualizacao.automatica import outro_formato
    from msscan.atualizacao.manual import url_drivers

    codigo = (secao.get("acesso") or {}).get("codigo")
    if codigo not in (IDRAC_INACESSIVEL, NAO_SUPORTADO_AQUI):
        return None
    componentes = [i["componente"] for i in secao.get("itens") or [] if i["situacao"] == ATUALIZACAO and i.get("pacote")
                   and not i["pacote"]["url"].lower().endswith(".bin") and not outro_formato(i, catalogo, secao.get("system_id"))]  # fmt: skip
    if not componentes:
        return None
    pagina = url_drivers(fabricante, modelo, None)
    inicio = textos.t("py_verificacao.sem_idrac_se_o_acesso_temporario" if codigo == IDRAC_INACESSIVEL else "py_verificacao.sem_idrac_sem_acesso")
    return {"componentes": componentes, "url": pagina["url"],
            "texto": inicio + textos.t("py_verificacao.sem_idrac_passos", ", ".join(componentes), pagina["url"])}
