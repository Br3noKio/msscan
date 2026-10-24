"""Atualização por repositório, executada pelo próprio iDRAC/Lifecycle Controller (CLAUDE.md 18.7).

Como o Dell Repository Manager: o MSscan monta no pendrive (``MSSCAN/firmware/repositorio/``) um repositório com o
``Catalog.xml`` OFICIAL da Dell filtrado para este modelo e para os itens confirmados no plano, mais os pacotes (hash
conferido de novo na cópia), e o serve por HTTP temporário SÓ num endereço do servidor: na rede local, quando o iDRAC está
nela (22.2, preferido; firewall só para o IP do iDRAC), ou na interface do OS to iDRAC Pass-through (rede interna
servidor–iDRAC). O caminho é conferido antes de criar a tarefa (IP na interface + ping no iDRAC) e vigiado durante ela
(22.3: interface sumiu, nenhum pedido em 2 min ou 5 min sem progresso → o job é cancelado e o MSscan segue). O iDRAC então:

1. ``InstallFromRepository`` com ``ApplyUpdate`` "False": lê o catálogo e compara (job "RepositoryUpdate");
2. ``GetRepoBasedUpdateList``: lista o que ele considera aplicável (vai para o relatório; o repositório só contém o que
   foi confirmado, então nada fora do plano pode entrar);
3. ``InstallFromRepository`` com ``ApplyUpdate`` "True" e ``RebootNeeded`` false: cria um job por componente; os que não
   precisam de reinício são aplicados na hora, os outros ficam agendados ("Scheduled") para o reinício, que é o MSscan
   quem faz (depois do boot único pelo pendrive).

Nunca são enviados ``ApplySameVersions`` nem ``ApplyDowngradeVersions`` (nunca reinstalar à força, nunca downgrade): a
salvaguarda do executor (``validar_escrita_bmc``) recusa qualquer outra chave.

Alternativa: o iDRAC direto no repositório online da Dell (``downloads.dell.com``, HTTPS), quando ele tem internet pela
própria porta — só se a lista de aplicáveis dele for exatamente o que foi confirmado (o iDRAC instala tudo o que achar).

Fonte: scripts oficiais da Dell (github.com/dell/iDRAC-Redfish-Scripting, ``InstallFromRepositoryREDFISH.py`` v19:
URIs, chaves do corpo, 202 + ``Location`` com o job, ``PackageList`` em XML; JobType "RepositoryUpdate"). O guia Redfish em
PDF estava inacessível (403). MOCKUP(rodada final): formato do ``PackageList``, estados dos jobs por componente e o IP do
servidor na interface do pass-through, sem coleta real.
"""

from __future__ import annotations

import gzip
import hashlib
import http.server
import ipaddress
import json
import re
import threading
import time
import xml.etree.ElementTree as ET
from functools import partial
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from msscan.acoes.comum import copiar_conferido
from msscan.core.executor import CAMINHO_CANCELAR_JOB, CANCELAR_JOB, REPOSITORIO_INSTALAR, REPOSITORIO_LISTA, SERVICO_INSTALACAO
from msscan.core.fallback import FalhaColeta
from msscan.parsers import ErroParser
from msscan.parsers.redfish import membros

PASTA_REPOSITORIO = "firmware/repositorio"
PORTA = 80
"""Porta do servidor HTTP do repositório: a padrão do HTTP, porque o ``InstallFromRepository`` recebe só o endereço
(``IPAddress``) e o caminho (``ShareName``). Ouve só no IP do servidor na interface do pass-through e fica aberta no
firewall do quiosque só durante a atualização."""
JOBS = "/redfish/v1/Managers/iDRAC.Embedded.1/Jobs"
ESPERA_JOB_S = 10.0
MAX_CONSULTAS_JOB = 360
"""Até ~1 h por job (download + aplicação imediata); depois disso o MSscan para e explica (Job Queue do iDRAC)."""
TERMINAIS = {"Completed", "CompletedWithErrors", "Failed", "Failed.", "Cancelled", "Killed"}
AGENDADOS = {"Scheduled", "Waiting", "Downloaded", "ReadyForExecution", "Paused", "Scheduling"}
"""Job por componente esperando o reinício (MOCKUP(rodada final): conferir os nomes exatos no iDRAC real)."""


# --- Repositório no pendrive ----------------------------------------------------------------------------------------


def caminho_no_catalogo(url: str, base: str | None) -> str:
    """``https://downloads.dell.com/FOLDER.../x.EXE`` → ``FOLDER.../x.EXE`` (o ``path`` do ``SoftwareComponent``)."""
    caminho = urlsplit(url).path.lstrip("/")
    prefixo = urlsplit(f"https://{(base or '').strip('/')}").path.strip("/")
    return caminho[len(prefixo) + 1:] if prefixo and caminho.startswith(prefixo + "/") else caminho


def filtrar_catalogo(conteudo: bytes, caminhos: set[str], system_id: str | None) -> bytes:
    """Catálogo oficial → só os ``SoftwareComponent`` destes caminhos (e deste modelo) e os ``SoftwareBundle`` do modelo
    com o conteúdo reduzido a eles. ``baseLocation`` vazio: os pacotes ficam ao lado do catálogo, nos mesmos caminhos.
    Função pura. Levanta ``ErroParser`` se o catálogo for ilegível ou se faltar algum caminho pedido."""
    try:
        xml = gzip.decompress(conteudo) if conteudo[:2] == b"\x1f\x8b" else conteudo
        raiz = ET.fromstring(xml)
    except (OSError, EOFError, ET.ParseError) as erro:
        raise ErroParser(f"catálogo Dell ilegível: {erro}") from erro
    if raiz.tag != "Manifest":
        raise ErroParser(f"catálogo Dell inesperado: raiz '{raiz.tag}'")
    achados = set()
    nomes = {Path(c).name for c in caminhos}
    for elemento in list(raiz):
        if elemento.tag == "SoftwareComponent":
            caminho = elemento.get("path") or ""
            modelos = {(m.get("systemID") or "").upper() for m in elemento.iter("Model")}
            if caminho in caminhos and (not system_id or not modelos or system_id.upper() in modelos):
                achados.add(caminho)
            else:
                raiz.remove(elemento)
        elif elemento.tag == "SoftwareBundle":
            alvos = {(m.get("systemID") or "").upper() for m in elemento.iter("Model")}
            if system_id and alvos and system_id.upper() not in alvos:
                raiz.remove(elemento)
                continue
            conteudo_bundle = elemento.find("Contents")
            for pacote in list(conteudo_bundle) if conteudo_bundle is not None else []:
                if Path(pacote.get("path") or "").name not in nomes:
                    conteudo_bundle.remove(pacote)
            if conteudo_bundle is None or not len(conteudo_bundle):
                raiz.remove(elemento)
    faltando = caminhos - achados
    if faltando:
        raise ErroParser(f"o catálogo oficial não tem (para este modelo): {', '.join(sorted(faltando))}")
    raiz.set("baseLocation", "")
    raiz.set("baseLocationAccessProtocols", "HTTP")
    return ET.tostring(raiz, encoding="utf-16", xml_declaration=True)


def montar(raiz_dados: Path, itens: list[dict[str, Any]], catalogo_bruto: bytes, base: str | None,
           system_id: str | None) -> dict[str, Any]:  # fmt: skip
    """Monta o repositório no pendrive. Cada pacote é COPIADO com o SHA-256 relido da cópia e comparado ao conferido no
    download. Levanta ``OSError``/``ValueError``/``ErroParser`` (quem chama cai para os outros métodos)."""
    destino = raiz_dados / PASTA_REPOSITORIO
    destino.mkdir(parents=True, exist_ok=True)
    caminhos = {}
    for item in itens:
        if not item.get("url"):
            raise ValueError(f"{item['componente']}: pacote sem endereço no catálogo oficial")
        caminho = caminho_no_catalogo(item["url"], base)
        if ".." in Path(caminho).parts or Path(caminho).is_absolute():
            raise ValueError(f"caminho inválido no catálogo: {caminho}")
        algoritmo, esperado = item["hash_conferido"]
        alvo = destino / caminho
        alvo.parent.mkdir(parents=True, exist_ok=True)
        soma = copiar_conferido(Path(item["arquivo"]), alvo)
        if algoritmo != "sha256":
            # Revisão de 06/10/2026, B5: catálogo só com MD5 → a cópia também é conferida (MD5 relido da cópia).
            md5 = hashlib.md5(usedforsecurity=False)
            with alvo.open("rb") as copia:
                for bloco in iter(lambda: copia.read(1 << 20), b""):
                    md5.update(bloco)
            soma = md5.hexdigest()
        if soma.lower() != str(esperado).lower():
            alvo.unlink(missing_ok=True)
            raise ValueError(f"{item['componente']}: a cópia no repositório não confere com o {algoritmo.upper()} do catálogo")
        caminhos[caminho] = item["componente"]
    catalogo = filtrar_catalogo(catalogo_bruto, set(caminhos), system_id)
    (destino / "Catalog.xml").write_bytes(catalogo)
    return {"pasta": str(destino), "catalogo": "Catalog.xml", "pacotes": caminhos}


# --- Servidor HTTP temporário (só na interface do pass-through) ------------------------------------------------------


class _Manipulador(http.server.SimpleHTTPRequestHandler):
    """Só GET/HEAD de arquivos do repositório (sem listagem de pastas); cada pedido fica registrado (22.3: horário, quem,
    arquivo e resultado). Conexão parada não prende uma thread para sempre (``timeout``)."""

    timeout = 60

    def list_directory(self, path):  # noqa: ANN001, ANN201 - assinatura da stdlib
        self.send_error(404)
        return None

    def log_request(self, code: Any = "-", size: Any = "-") -> None:
        arquivo = (getattr(self, "path", "") or "").split("?", 1)[0]
        self.server.pedidos.append({"hora": time.strftime("%H:%M:%S"), "ip": self.client_address[0],  # type: ignore[attr-defined]
                                    "arquivo": arquivo, "resultado": str(getattr(code, "value", code))})  # fmt: skip

    def log_message(self, formato: str, *args: Any) -> None:
        self.server.pedidos.append({"hora": time.strftime("%H:%M:%S"), "ip": self.client_address[0],  # type: ignore[attr-defined]
                                    "arquivo": "", "resultado": formato % args})  # fmt: skip


def texto_pedido(pedido: dict[str, str]) -> str:
    return f"{pedido['hora']} {pedido['ip']} {pedido['arquivo'] or '-'} → {pedido['resultado']}"


class ServidorRepositorio:
    """``with ServidorRepositorio(pasta, ip, porta) as srv: srv.url`` — ouve só em ``ip`` (nunca em 0.0.0.0)."""

    ativos = 0
    """Quantos repositórios o MSscan está servindo agora (job de repositório sem nenhum = órfão; 09/10/2026)."""

    def __init__(self, pasta: str, ip: str, porta: int = PORTA) -> None:
        endereco = ipaddress.ip_address(ip)
        if endereco.is_unspecified:
            raise ValueError("o repositório nunca ouve em todas as interfaces")
        self.servidor = http.server.ThreadingHTTPServer((ip, porta), partial(_Manipulador, directory=pasta))
        self.servidor.pedidos = []  # type: ignore[attr-defined]
        self.ip, self.porta = ip, self.servidor.server_address[1]
        self._thread = threading.Thread(target=self.servidor.serve_forever, daemon=True, name="msscan-repositorio")

    @property
    def pedidos(self) -> list[dict[str, str]]:
        return list(self.servidor.pedidos)  # type: ignore[attr-defined]

    def __enter__(self) -> ServidorRepositorio:
        self._thread.start()
        ServidorRepositorio.ativos += 1
        return self

    def __exit__(self, *_: object) -> None:
        ServidorRepositorio.ativos -= 1
        self.servidor.shutdown()
        self.servidor.server_close()


def ip_local_na_rede_de(executor: Any, ip_idrac: str | None) -> str | None:
    """Endereço deste servidor na mesma sub-rede de ``ip_idrac`` (IP USB do pass-through ou IP do iDRAC na rede local);
    ``None`` se nenhuma interface tem IP nessa sub-rede (ex.: a interface do pass-through sumiu)."""
    if not ip_idrac:
        return None
    rc, saida, _ = executor.rodar(["ip", "-j", "-4", "addr", "show"], timeout=10)
    if rc != 0:
        return None
    try:
        interfaces = json.loads(saida or "[]")
        alvo = ipaddress.ip_address(ip_idrac)
    except ValueError:
        return None
    for interface in interfaces if isinstance(interfaces, list) else []:
        for endereco in interface.get("addr_info") or []:
            try:
                rede = ipaddress.ip_interface(f"{endereco.get('local')}/{endereco.get('prefixlen')}").network
            except ValueError:
                continue
            if alvo in rede and str(endereco.get("local")) != ip_idrac:
                return str(endereco.get("local"))
    return None


class FirewallRepositorio:
    """Abre/fecha a porta do repositório no firewall do quiosque (só na ISO, conjuntos com timeout).

    Sem ``origem``: só para a rede interna do pass-through (conjunto ``repositorio``). Com ``origem`` (22.2, rede local):
    só para o IP do iDRAC (conjunto ``repositorio_lan``, par IP . porta)."""

    def __init__(self, executor: Any, ativo: bool, origem: str | None = None) -> None:
        self.executor, self.ativo, self.origem = executor, ativo, origem

    def _elemento(self, porta: int, extra: str = "") -> list[str]:
        conjunto, chave = ("repositorio_lan", f"{self.origem} . {porta}") if self.origem else ("repositorio", str(porta))
        return [conjunto, f"{{ {chave}{extra} }}"]

    def abrir(self, porta: int, segundos: int = 4 * 3600) -> str:
        if not self.ativo:
            return ""
        rc, _, erro = self.executor.rodar(["nft", "add", "element", "inet", "msscan_quiosque",
                                           *self._elemento(porta, f" timeout {segundos}s")], timeout=10)  # fmt: skip
        return "" if rc == 0 else f"não foi possível abrir a porta {porta} no firewall ({erro.strip() or rc})"

    def fechar(self, porta: int) -> None:
        if self.ativo:
            self.executor.rodar(["nft", "delete", "element", "inet", "msscan_quiosque", *self._elemento(porta)], timeout=10)


# --- Ações do iDRAC --------------------------------------------------------------------------------------------------


def servico_disponivel(cliente: Any) -> bool:
    """Detectar primeiro: o iDRAC anuncia o ``DellSoftwareInstallationService`` (GET 200)?"""
    try:
        cliente.get(SERVICO_INSTALACAO)
        return True
    except (FalhaColeta, ErroParser):
        return False


def corpo_instalar(ip: str, aplicar: bool, share: str = "/", tipo: str = "HTTP") -> dict[str, Any]:
    """Único corpo usado: nunca ``ApplySameVersions``/``ApplyDowngradeVersions``; o reinício é sempre do MSscan."""
    corpo: dict[str, Any] = {"IPAddress": ip, "ShareType": tipo, "ApplyUpdate": "True" if aplicar else "False", "RebootNeeded": False}
    if tipo == "HTTP":
        corpo.update({"ShareName": share, "CatalogFile": "Catalog.xml"})
    else:
        corpo["IgnoreCertWarning"] = "Off"
    return corpo


def ler_job(cliente: Any, job: str) -> dict[str, Any]:
    """Estado de um job do iDRAC (``JobState``, ``PercentComplete``, ``Message``, ``Name``, ``JobType``)."""
    dados = cliente.get(f"{JOBS}/{job}")
    if not isinstance(dados, dict):
        raise ErroParser("job do iDRAC sem JSON")
    return {"id": str(dados.get("Id") or job), "estado": str(dados.get("JobState") or ""), "nome": str(dados.get("Name") or ""),
            "tipo": str(dados.get("JobType") or ""), "percentual": dados.get("PercentComplete"),
            "mensagem": str(dados.get("Message") or "")}  # fmt: skip


def estado_job(cliente: Any, job: str) -> str | None:
    """``JobState`` do job; ``""`` = fora da Job Queue (o iDRAC respondeu 404); ``None`` = não foi possível ler (sem
    resposta, sessão perdida): "não sei", nunca "não existe" (revisão de 06/10/2026, C1)."""
    try:
        return ler_job(cliente, job)["estado"]
    except FalhaColeta as erro:
        return "" if getattr(erro, "status", None) == 404 else None
    except ErroParser:
        return None


def cancelar_job(cliente: Any, job: str) -> tuple[bool, str]:
    """Cancela UM job criado pelo MSscan que não andou (22.3), para não deixar sobra na Job Queue. Confere relendo: só
    conta como cancelado com o iDRAC respondendo (404 ou Cancelled/Killed).
    MOCKUP(rodada final): resposta real do ``DeleteJobQueue`` para um job "Running" do iDRAC9."""
    try:
        resposta = cliente.escrever(CANCELAR_JOB, CAMINHO_CANCELAR_JOB, {"JobID": job})
        pedido = str(resposta.erro or resposta.status)
    except FalhaColeta as erro:
        pedido = str(erro)
    estado = estado_job(cliente, job)
    if estado is None:
        return False, (f"cancelamento do job {job} não confirmado: o iDRAC não respondeu à releitura ({pedido}); confira "
                       "Maintenance → Job Queue na página do iDRAC")  # fmt: skip
    if not estado or estado in ("Cancelled", "Killed"):
        return True, f"job {job} cancelado no iDRAC (fora da Job Queue)"
    return False, (f"o iDRAC não cancelou o job {job} ({pedido}; continua {estado}): apague-o em "
                   "Maintenance → Job Queue na página do iDRAC")  # fmt: skip


def listar_jobs(cliente: Any) -> set[str] | None:
    """Ids da Job Queue; ``None`` = não foi possível ler (nunca "fila vazia")."""
    try:
        return {m.rstrip("/").rsplit("/", 1)[-1] for m in membros(cliente.get(JOBS))}
    except (FalhaColeta, ErroParser):
        return None


def recusa_explicita(resposta: Any) -> bool:
    """O iDRAC respondeu e recusou (4xx), sem indício de que o pedido foi aceito. Timeout/sem resposta, 5xx, 2xx sem
    ``Location`` e "já existe job" (RED014) são INCERTOS: o pedido pode ter chegado — nunca seguir por outro caminho
    (revisão de 06/10/2026, C1/C2)."""
    status = getattr(resposta, "status", None)
    if status is None or not 400 <= status < 500:
        return False
    texto = (json.dumps(getattr(resposta, "corpo", None) or "", ensure_ascii=False) + str(getattr(resposta, "erro", "") or "")).lower()
    return "red014" not in texto and "already" not in texto


SEM_PEDIDO_S = 120.0
"""O iDRAC não buscou nada no repositório do pendrive em 2 min: pedido não entregue — avisar na hora e seguir (21.1)."""
SEM_PROGRESSO_S = 300.0
"""22.3: nenhuma mudança real (estado/percentual/mensagem do job nem pedido novo ao repositório) em 5 min → próximo
caminho. Nunca com o job gravando (``Running`` num job de firmware)."""


def esperar_job(cliente: Any, job: str, dormir: Callable[[float], None], progresso: Callable[[dict[str, Any]], None] | None = None,
                ate_agendado: bool = False, abortar: Callable[[float], str] | None = None,
                reconectar: Callable[[], bool] | None = None) -> dict[str, Any]:  # fmt: skip
    """Consulta até terminar (ou, com ``ate_agendado``, até ficar esperando o reinício). Falhas passageiras repetem.

    ``abortar(segundos, job)``: motivo para desistir já (ex.: o iDRAC não buscou o repositório) → ``{"abortado": motivo}``.
    ``reconectar``: sem resposta (o iDRAC pode estar reiniciando) → espera ele voltar uma vez e continua consultando."""
    ultimo: dict[str, Any] = {"id": job, "estado": "", "mensagem": "sem resposta do iDRAC"}
    falhas = 0
    for consulta in range(MAX_CONSULTAS_JOB):
        try:
            ultimo = ler_job(cliente, job)
            falhas = 0
        except (FalhaColeta, ErroParser) as erro:
            falhas += 1
            ultimo = {**ultimo, "mensagem": str(erro)}
            if falhas >= 3:
                if reconectar is not None and reconectar():
                    reconectar, falhas = None, 0
                    continue
                return {**ultimo, "sem_resposta": True}
        if progresso and not falhas:
            progresso(ultimo)
        if ultimo["estado"] in TERMINAIS or (ate_agendado and ultimo["estado"] in AGENDADOS):
            return ultimo
        motivo = abortar(consulta * ESPERA_JOB_S, ultimo) if abortar else ""
        if motivo:
            return {**ultimo, "abortado": motivo}
        dormir(ESPERA_JOB_S)
    return {**ultimo, "sem_resposta": True}


def parse_lista(xml: str | None) -> list[dict[str, str]]:
    """``PackageList`` do ``GetRepoBasedUpdateList`` (XML com ``<PROPERTY NAME="..."><VALUE>..</VALUE>``). Função pura.

    Cada pacote: ``DisplayName``, ``PackageVersion``, ``PackageName``, ``Criticality``, ``RebootType``, ``ComponentID``
    (os que vierem). MOCKUP(rodada final): estrutura conferida só pelos scripts oficiais."""
    if not xml:
        return []
    pacotes = []
    for bloco in re.findall(r"<INSTANCE\b.*?</INSTANCE>", xml, re.S) or [xml]:
        campos = {}
        for nome, valor in re.findall(r'<PROPERTY[^>]*NAME="([^"]+)"[^>]*>\s*<VALUE>(.*?)</VALUE>', bloco, re.S):
            campos.setdefault(nome, valor.strip())
        if campos:
            pacotes.append(campos)
    return pacotes


def pacote_confirmado(pacote: dict[str, str], arquivos: set[str]) -> bool:
    """O pacote listado pelo iDRAC é um dos arquivos confirmados no plano (pelo nome do pacote)."""
    nome = Path(pacote.get("PackageName") or pacote.get("PackagePath") or "").name
    return bool(nome) and nome in arquivos


# --- Execução -------------------------------------------------------------------------------------------------------

APLICADO, AGENDADO, FALHOU_ANTES, FALHOU_DEPOIS, TRAVA, INDISPONIVEL = (
    "aplicado", "agendado", "falhou_antes", "falhou_depois", "trava", "indisponivel")  # fmt: skip
"""``falhou_antes``: nada foi aplicado (cai para os outros métodos); ``falhou_depois``: o iDRAC já aceitou instalar — parar
e conferir (nunca reenviar os mesmos pacotes por outro caminho)."""


def instalar(cliente: Any, ip: str, itens: list[dict[str, Any]], registrar: Callable[[str, str], None],
             dormir: Callable[[float], None], progresso: Callable[[dict[str, Any]], None] | None = None,
             online: bool = False, pedidos: Callable[[], int] | None = None,
             reconectar: Callable[[], bool] | None = None, caminho_ok: Callable[[], str] | None = None,
             ao_criar: Callable[[str], None] | None = None, via: str = "rede interna do pass-through") -> dict[str, Any]:  # fmt: skip
    """Repositório (local servido em ``ip`` ou online) → lista de aplicáveis → instalação → jobs. Nunca levanta exceção.

    ``registrar(situacao, detalhe)`` anota cada etapa no relatório; ``progresso(job)`` recebe cada leitura de job (21.1).
    Troca de caminho (22.3), conferida a cada leitura do job do repositório, independente da mensagem dele:
    ``caminho_ok()`` devolve o motivo se o caminho caiu (interface do pass-through sumiu); ``pedidos()`` = quantos pedidos o
    servidor HTTP já recebeu (nenhum em ``SEM_PEDIDO_S`` = o iDRAC não alcança o pendrive); nada mudou em
    ``SEM_PROGRESSO_S`` = parado. Em qualquer um, o job é cancelado no iDRAC e o caminho é abandonado (``falhou_antes`` se
    nenhum job de componente foi criado). ``ao_criar(job)``: cada job do repositório criado (mudança temporária a desfazer).
    ``reconectar``: espera o iDRAC voltar depois de um reinício; o passo interrompido por ele (RED115) é refeito UMA vez.
    Devolve ``{"desfecho", "lista", "jobs", "mensagem"}``.
    """
    from msscan.atualizacao.jobs import reinicio_do_idrac

    arquivos = {Path(urlsplit(str(i.get("url") or "")).path).name for i in itens}
    resultado: dict[str, Any] = {"desfecho": FALHOU_ANTES, "lista": [], "jobs": [], "mensagem": ""}
    instalacao: dict[str, Any] = {"pedida": False, "antes": None}
    """``pedida``: o POST de instalar (ApplyUpdate "True") já saiu — daí em diante, qualquer dúvida é ``falhou_depois``."""
    tipo, endereco = ("HTTPS", "downloads.dell.com") if online else ("HTTP", ip)

    def falhar(desfecho: str, mensagem: str) -> dict[str, Any]:
        resultado.update(desfecho=desfecho, mensagem=mensagem)
        registrar("falhou", mensagem)
        return resultado

    def vigia(aplicar: bool) -> Callable[[float, dict[str, Any]], str]:
        visto: dict[str, Any] = {"assinatura": None, "desde": 0.0}

        def abortar(segundos: float, job: dict[str, Any]) -> str:
            motivo = caminho_ok() if caminho_ok is not None else ""
            if motivo:
                return motivo
            n = pedidos() if pedidos is not None else 0
            assinatura = (job.get("estado"), job.get("percentual"), job.get("mensagem"), n)
            if assinatura != visto["assinatura"]:
                visto.update(assinatura=assinatura, desde=segundos)
            if not aplicar and not online and pedidos is not None and segundos >= SEM_PEDIDO_S and not n:
                return (f"o iDRAC não buscou o repositório em {int(SEM_PEDIDO_S // 60)} min (http://{ip}/): o pedido não "
                        f"chegou a ele pela {via}")  # fmt: skip
            if segundos - visto["desde"] >= SEM_PROGRESSO_S:
                return (f"sem progresso real há {int(SEM_PROGRESSO_S // 60)} min (job {job.get('estado') or '?'}: "
                        f"{job.get('mensagem') or '-'}; {n} pedido(s) ao repositório)")  # fmt: skip
            return ""

        return abortar

    def pedir_e_esperar(aplicar: bool) -> tuple[Any, dict[str, Any]]:
        """POST + espera do job; refeito uma vez se o iDRAC reiniciou no meio (RED115)."""
        for tentativa in (1, 2):
            resposta = cliente.escrever(REPOSITORIO_INSTALAR, cliente_caminho(INSTALAR_ACAO), corpo_instalar(endereco, aplicar, tipo=tipo))
            if resposta.erro_tipo == "trava" or resposta.status != 202 or not resposta.local:
                return resposta, {}
            ident = resposta.local.rstrip("/").rsplit("/", 1)[-1]
            registrar("ok", f"tarefa criada no iDRAC: job {ident} (Repository Update, {'instalar' if aplicar else 'só comparar'}; "
                            f"repositório {'online da Dell' if online else f'http://{ip}/ pela {via}'})")  # fmt: skip
            if ao_criar is not None and not aplicar:
                ao_criar(ident)  # só o job de comparar vira mudança temporária: o de instalar nunca é cancelado às cegas (M1)
            job = esperar_job(cliente, ident, dormir, progresso, abortar=vigia(aplicar), reconectar=reconectar)
            if job.get("abortado"):
                depois = listar_jobs(cliente) if aplicar else set()
                if aplicar and (depois is None or instalacao["antes"] is None or depois - instalacao["antes"] - {ident}):
                    # M1: o job de instalar já pode ter criado jobs de componente (ou não dá para saber): não cancelar.
                    job["cancelado"] = False
                    registrar("falhou", f"job {ident} não cancelado: " + ("a Job Queue não pôde ser lida" if depois is None
                              or instalacao["antes"] is None else "já há job(s) de componente criados por ele"))  # fmt: skip
                else:
                    job["cancelado"], detalhe = cancelar_job(cliente, ident)
                    registrar("ok" if job["cancelado"] else "falhou", detalhe)
            if tentativa == 1 and reconectar is not None and reinicio_do_idrac(job) and job.get("estado", "").startswith("Failed"):
                registrar("falhou", f"job {job.get('id')}: o iDRAC reiniciou no meio ({job.get('mensagem')}); esperando ele "
                                    "voltar para refazer uma vez")  # fmt: skip
                if reconectar():
                    continue
            return resposta, job
        return resposta, job

    try:
        # 1. Comparar sem aplicar.
        resposta, job = pedir_e_esperar(False)
        if resposta.erro_tipo == "trava":
            return falhar(TRAVA, resposta.erro)
        if resposta.status != 202 or not resposta.local:
            return falhar(FALHOU_ANTES, f"o iDRAC recusou ler o repositório ({resposta.erro or resposta.status}); licença "
                                        "Enterprise/Datacenter e Lifecycle Controller ligado são exigidos")  # fmt: skip
        if job.get("abortado"):
            return falhar(FALHOU_ANTES, job["abortado"] + "; nada foi aplicado")
        if job.get("estado") not in ("Completed", "CompletedWithErrors"):
            return falhar(FALHOU_ANTES, f"o iDRAC não leu o catálogo do repositório: {job.get('estado') or '?'} — {job.get('mensagem')}")
        registrar("ok", f"repositório lido pelo iDRAC ({job.get('mensagem') or 'catálogo comparado'})")
        # 2. Lista de aplicáveis (relatório; no online, tem de ser exatamente o confirmado).
        lista = cliente.escrever(REPOSITORIO_LISTA, cliente_caminho(LISTA_ACAO), {})
        pacotes = parse_lista((lista.corpo or {}).get("PackageList") if isinstance(lista.corpo, dict) else None) if lista.status == 200 else []
        resultado["lista"] = pacotes
        if lista.status != 200 and not online:
            mensagem = json.dumps(lista.corpo or {}, ensure_ascii=False)[:300]
            if "match catalog" in mensagem or "not present" in mensagem:
                return falhar(FALHOU_ANTES, "o iDRAC diz que nada do repositório se aplica (versões já iguais ou pacotes não aplicáveis)")
        registrar("ok", f"o iDRAC considera aplicável(is): {', '.join(p.get('DisplayName') or p.get('PackageName') or '?' for p in pacotes) or 'lista não informada'}")
        if online and (not pacotes or not all(pacote_confirmado(p, arquivos) for p in pacotes)):
            return falhar(FALHOU_ANTES, "repositório online: a lista do iDRAC não é exatamente o plano confirmado (o iDRAC "
                                        "instalaria tudo o que acha aplicável): não usado")  # fmt: skip
        if not online and pacotes and not all(pacote_confirmado(p, arquivos) for p in pacotes if p.get("PackageName")):
            return falhar(FALHOU_ANTES, "a lista do iDRAC traz pacote fora do repositório montado: nada aplicado")
        # 3. Instalar.
        antes = instalacao["antes"] = listar_jobs(cliente)
        instalacao["pedida"] = True
        resposta, mestre = pedir_e_esperar(True)
        if resposta.erro_tipo == "trava":
            return falhar(TRAVA, resposta.erro)
        if resposta.status != 202 or not resposta.local:
            if recusa_explicita(resposta):
                return falhar(FALHOU_ANTES, f"o iDRAC recusou instalar do repositório ({resposta.erro or resposta.status})")
            return falhar(FALHOU_DEPOIS, f"resposta incerta do iDRAC ao pedido de instalar ({resposta.erro or resposta.status}): "
                                         "o pedido pode ter chegado — confira o Job Queue do iDRAC; nada é reenviado por outro caminho")  # fmt: skip
        principal = str(mestre.get("id") or resposta.local.rstrip("/").rsplit("/", 1)[-1])
        registrar("ok", f"instalação pelo repositório aceita pelo iDRAC (job {principal})")
        depois = listar_jobs(cliente)
        if antes is None or depois is None:
            return falhar(FALHOU_DEPOIS, f"job do repositório {principal}: a Job Queue do iDRAC não pôde ser lida para achar os "
                                         "jobs de componente — confira o Job Queue; nada é reenviado por outro caminho")  # fmt: skip
        novos = sorted(depois - antes - {principal})
        if mestre.get("abortado") and not novos and mestre.get("cancelado"):
            return falhar(FALHOU_ANTES, f"job do repositório {principal}: {mestre['abortado']}; cancelado no iDRAC e nenhum job "
                                        "de componente criado, nada foi aplicado")  # fmt: skip
        if mestre.get("abortado") or mestre.get("sem_resposta") or mestre.get("estado") not in ("Completed", "CompletedWithErrors"):
            return falhar(FALHOU_DEPOIS, f"job do repositório {principal}: {mestre.get('abortado') or mestre.get('estado') or 'sem resposta'} "
                                         f"— {mestre.get('mensagem')} (confira o Job Queue do iDRAC; nada é reenviado por outro caminho)")  # fmt: skip
        jobs = []
        for job_id in novos:
            jobs.append(esperar_job(cliente, job_id, dormir, progresso, ate_agendado=True, reconectar=reconectar))
        resultado["jobs"] = jobs
        falhos = [j for j in jobs if j.get("estado", "").startswith("Failed") or j.get("estado") in ("Cancelled", "Killed")]
        if falhos:
            return falhar(FALHOU_DEPOIS, "job(s) com falha no iDRAC: " + "; ".join(f"{j['nome'] or j['id']}: {j['mensagem']}" for j in falhos))
        if any(j.get("sem_resposta") for j in jobs):
            return falhar(FALHOU_DEPOIS, "o iDRAC parou de responder sobre os jobs: confira o Job Queue antes de reiniciar")
        agendados = [j for j in jobs if j.get("estado") in AGENDADOS]
        resultado["desfecho"] = AGENDADO if agendados else APLICADO
        resultado["mensagem"] = (f"{len(agendados)} job(s) agendado(s) para o reinício" if agendados
                                 else f"{len(jobs)} job(s) concluído(s) sem reinício")  # fmt: skip
        registrar("ok", resultado["mensagem"] + ": " + "; ".join(f"{j['nome'] or j['id']}: {j['estado']}" for j in jobs))
        return resultado
    except (FalhaColeta, ErroParser, AttributeError) as erro:
        return falhar(FALHOU_DEPOIS if resultado["jobs"] or instalacao["pedida"] else FALHOU_ANTES, f"leitura do iDRAC falhou: {erro}")


INSTALAR_ACAO = "InstallFromRepository"
LISTA_ACAO = "GetRepoBasedUpdateList"


def cliente_caminho(acao: str) -> str:
    return f"{SERVICO_INSTALACAO}/Actions/DellSoftwareInstallationService.{acao}"


__all__ = ["PASTA_REPOSITORIO", "PORTA", "ServidorRepositorio", "corpo_instalar", "filtrar_catalogo", "instalar", "montar",
           "parse_lista", "servico_disponivel"]  # fmt: skip
