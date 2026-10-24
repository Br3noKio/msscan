"""Acesso ao iDRAC (CLAUDE.md 19): racadm do iDRAC Tools (opção A), caixa "Acesso ao iDRAC" e parsers novos."""

from __future__ import annotations

import pytest

from msscan.acesso_idrac import IPMI, NENHUM, RACADM, REDE, SEM_IDRAC, TEXTO_CABO_DEDICADA, TEXTO_SEM_RACADM, avaliar
from msscan.deteccao import idrac_tools
from msscan.parsers import ErroParser
from msscan.parsers.ipmitool import parse_delloem_active
from msscan.parsers.racadm import parse_racadm_get
from tests.conftest import PASTA_FIXTURES, ExecutorFalso

IDRAC = PASTA_FIXTURES / "dell" / "poweredge_r640_idrac"

# racadm getniccfg: formato do artigo oficial da Dell "PowerEdge: iDRAC Connectivity Troubleshooting" (KB 000185547).
# TODO(fixture): sem saída real do racadm nesta máquina.
NICCFG_DEDICADA_SEM_LINK = """IPv4 settings:
NIC Enabled          = 1
IPv4 Enabled         = 1
DHCP Enabled         = 1
IP Address           = 192.0.2.82
Subnet Mask          = 255.255.255.0
Gateway              = 192.0.2.1

LOM Status:
NIC Selection        = Dedicated
Link Detected        = No
"""
NICCFG_COMPARTILHADA = """LOM Status:
NIC Selection        = Shared
Link Detected        = No
Active LOM in Shared Mode = NIC1
"""


def _leituras(**mudancas):
    base = {"ipmi": True, "rede": {"ip": "192.0.2.82", "mascara": "255.255.255.0", "gateway": "192.0.2.1", "origem": "DHCP",
                                   "porta": None, "lido": True},
            "ativa": None, "niccfg": None, "racadm": {"situacao": "ausente"}, "racadm_caminho": None,
            "interfaces": [{"nome": "eno1np0", "link": False, "enderecos": []}, {"nome": "eno2np1", "link": False, "enderecos": []},
                           {"nome": "eno3", "link": True, "enderecos": ["192.0.2.85/24"]}, {"nome": "eno4", "link": False, "enderecos": []}],
            "redfish": {"anunciado": True, "confirmado": False, "endereco": "192.0.2.82", "motivo": "o BMC não respondeu a tempo",
                        "validado": False}}  # fmt: skip
    base.update(mudancas)
    return base


def _textos(estado):
    return " ".join(m["texto"] for m in estado["mensagens"])


class TestParsers:
    def test_delloem_active_real(self):
        assert parse_delloem_active((IDRAC / "ipmitool_delloem_lan_get_active.txt").read_text()) == "dedicada"

    @pytest.mark.parametrize("texto, esperado", [("\nNone\n", ""), ("\nLOM1\n", "LOM1"), ("LOM4", "LOM4")])
    def test_delloem_active_fonte(self, texto, esperado):
        assert parse_delloem_active(texto) == esperado

    def test_delloem_active_desconhecido(self):
        with pytest.raises(ErroParser):
            parse_delloem_active("Error in getting Active LOM Status")

    def test_niccfg_e_getversion(self):
        assert parse_racadm_get(NICCFG_DEDICADA_SEM_LINK)["Link Detected"] == "No"
        versoes = parse_racadm_get(" Bios Version                     = 2.21.2\n iDRAC Version                    = 7.00.00.173\n")
        assert versoes["iDRAC Version"] == "7.00.00.173"


class TestCaixa:
    def test_caso_do_t1_sem_racadm(self):
        """Log do T1: iDRAC no IP do lan print sem responder, modo não lido (iDRAC9 sem racadm), sem link."""
        estado = avaliar(_leituras(ativa=""))
        # 28.2: o IPMI local responde → "acessível só por dentro", nunca "sem acesso"/"não acessível".
        assert estado["situacao"] == IPMI and estado["titulo"] == "iDRAC acessível só por dentro do servidor (IPMI)"
        assert "iDRAC inacessível pela rede (cabo, IP, porta)" in _textos(estado)
        assert "14ª geração (iDRAC9)" in _textos(estado) or estado.get("modelo") is None
        texto = _textos(estado)
        assert "sem conexão física" in texto and TEXTO_CABO_DEDICADA in texto and "Dedicated (F2 > iDRAC Settings > Network)" in texto
        assert "eno1np0, eno2np1, eno4" in texto and "nunca desativa nem reconfigura" in texto
        assert TEXTO_SEM_RACADM in texto and estado["botoes"] == ["tentar"]

    def test_dedicada_sem_link_com_racadm(self):
        estado = avaliar(_leituras(niccfg={"NIC Selection": "Dedicated", "Link Detected": "No"}, racadm_caminho="/opt/x/racadm",
                                   racadm={"situacao": "pronto"}))  # fmt: skip
        assert estado["situacao"] == RACADM and estado["modo"] == "dedicada" and estado["link"] is False
        assert TEXTO_CABO_DEDICADA in _textos(estado) and TEXTO_SEM_RACADM not in _textos(estado)

    def test_compartilhada_porta_sem_cabo(self):
        """Caso relatado pelo usuário: NIC Selection compartilhado e o cabo fora da porta usada pelo iDRAC."""
        estado = avaliar(_leituras(niccfg=parse_racadm_get(NICCFG_COMPARTILHADA), racadm_caminho="/opt/x/racadm"))
        texto = _textos(estado)
        assert estado["modo"] == "compartilhada" and estado["lom"] == "1"
        assert ("O iDRAC está usando a porta LOM1 (eno1np0) do servidor, que está sem cabo. Conecte o cabo nessa porta ou altere "
                "a seleção de porta do iDRAC para Dedicated (F2 > iDRAC Settings > Network).") in texto  # fmt: skip
        assert "nunca desativa nem reconfigura essa porta" in texto

    def test_compartilhada_com_link_pelo_ipmi(self):
        estado = avaliar(_leituras(ativa="LOM3"))
        assert estado["modo"] == "compartilhada" and estado["link"] is True
        assert "porta LOM3 (eno3) do servidor (conectada)" in _textos(estado)

    def test_outra_rede(self):
        estado = avaliar(_leituras(ativa="dedicada", rede={"ip": "10.9.9.9", "mascara": "255.255.255.0", "lido": True}))
        assert estado["mesma_rede"] is False
        assert "iDRAC 10.9.9.9/24; servidor 192.0.2.85/24" in _textos(estado)

    def test_dhcp_sem_ip_com_link(self):
        """21.5: com link e sem endereço = DHCP sem resposta (não é cabo); oferece o IP fixo."""
        estado = avaliar(_leituras(ativa="dedicada", rede={"ip": "0.0.0.0", "mascara": "0.0.0.0", "origem": "DHCP", "lido": True}))
        assert "DHCP sem resposta: o iDRAC está configurado para DHCP, a porta está conectada" in _textos(estado)
        assert TEXTO_CABO_DEDICADA not in _textos(estado) and "ip_fixo" in estado["botoes"]

    def test_sem_ip_porque_sem_cabo(self):
        """21.5: o caso do T1 de 05/10/2026 (cabo fora, DHCP 0.0.0.0): a causa é o cabo; IP fixo não resolveria."""
        estado = avaliar(_leituras(ativa=None, niccfg={"NIC Selection": "Dedicated", "Link Detected": "No"},
                                   rede={"ip": "0.0.0.0", "mascara": "0.0.0.0", "origem": "DHCP", "lido": True}))  # fmt: skip
        assert TEXTO_CABO_DEDICADA in _textos(estado) and "porta dele está sem conexão física (cabo desconectado)" in _textos(estado)
        assert "DHCP sem resposta" not in _textos(estado) and "ip_fixo" not in estado["botoes"]

    def test_outra_rede_oferece_ip_fixo(self):
        estado = avaliar(_leituras(ativa="dedicada", rede={"ip": "10.9.9.9", "mascara": "255.255.255.0", "lido": True}))
        assert "ip_fixo" in estado["botoes"]

    def test_responde_pela_rede_pede_credenciais(self):
        redfish = {"anunciado": True, "confirmado": True, "endereco": "192.0.2.82", "motivo": "", "validado": False}
        estado = avaliar(_leituras(ativa="dedicada", redfish=redfish))
        assert estado["situacao"] == REDE and "credenciais" in estado["botoes"]
        assert "Porta dedicada do iDRAC conectada." in _textos(estado)
        assert TEXTO_SEM_RACADM not in _textos(estado)  # B disponível: o racadm é só alternativa

    def test_motivo_provavel_sem_pista(self):
        estado = avaliar(_leituras(ativa="dedicada"))
        assert "Motivo provável: o BMC não respondeu a tempo" in _textos(estado)

    def test_racadm_falhou(self):
        estado = avaliar(_leituras(racadm={"situacao": "falhou", "mensagem": "x.tar.gz: não foi possível extrair"}))
        assert "acesso interno (racadm) não disponível: x.tar.gz: não foi possível extrair" in _textos(estado)
        assert estado["racadm_texto"] == "não disponível (x.tar.gz: não foi possível extrair)"

    def test_sem_ipmi(self):
        estado = avaliar(_leituras(ipmi=False, rede=None, redfish={"anunciado": False}))
        assert estado["situacao"] == SEM_IDRAC and TEXTO_SEM_RACADM in _textos(estado)


class ExecutorPreparo(ExecutorFalso):
    origem = "real"
    prepara_ferramentas = True
    rc_extracao = 0

    def _extrair_idrac_tools(self, pacote, destino):
        return (self.rc_extracao, "", "pacote ilegível: not a gzip file" if self.rc_extracao else "")


def _preparo(pacotes, versao_rc=0):
    ex = ExecutorPreparo()
    raiz = idrac_tools.PASTA_EXTRACAO
    ex.diretorios = {"/m/vendor": ["perccli64", "Dell-iDRACTools-Web-LX-11.3.0.0-609_A00.tar.gz"],
                     f"{raiz}/iDRACTools/racadm": list(pacotes), "/opt/dell/srvadmin/sbin": ["racadm-wrapper-idrac7"]}  # fmt: skip
    for pasta, arquivos in pacotes.items():
        ex.diretorios[f"{raiz}/iDRACTools/racadm/{pasta}/x86_64"] = arquivos
    ok = (0, "", "")
    for comando in (["rm", "-rf", raiz], ["ldconfig"], ["sh", "-c", "cat /opt/dell/srvadmin/etc/omreg.d/*.cfg > /opt/dell/srvadmin/etc/omreg.cfg"],
                    ["ln", "-sf", "racadm-wrapper-idrac7", idrac_tools.RACADM], ["/usr/libexec/instsvcdrv-helper", "start"],
                    ["chmod", "+x", "/opt/dell/srvadmin/sbin/racadm-wrapper-idrac7"], ["chmod", "+x", "/opt/dell/srvadmin/bin/idracadm7"]):
        ex.comandos[tuple(comando)] = ok
    ex.existentes = {"/usr/libexec/instsvcdrv-helper"}
    ex.comandos[(idrac_tools.RACADM, "getversion")] = (versao_rc, " iDRAC Version                    = 7.00.00.173\n", "")
    return ex


class TestPreparoRacadm:
    def test_deb_do_ubuntu_mais_novo(self):
        ex = _preparo({"UBUNTU20": ["a.deb"], "UBUNTU22": ["srvadmin-hapi_11.3.0.0_amd64.deb", "srvadmin-idracadm7_11.3.0.0_all.deb"],
                       "RHEL8": ["x.rpm"]})  # fmt: skip
        for nome in ("srvadmin-hapi_11.3.0.0_amd64.deb", "srvadmin-idracadm7_11.3.0.0_all.deb"):
            ex.comandos[("dpkg-deb", "-x", f"{idrac_tools.PASTA_EXTRACAO}/iDRACTools/racadm/UBUNTU22/x86_64/{nome}", "/")] = (0, "", "")
        info = idrac_tools.preparar(ex, "/m")
        assert info["situacao"] == idrac_tools.PREPARADO and info["caminho"] == idrac_tools.RACADM
        assert info["versoes"]["iDRAC Version"] == "7.00.00.173"
        rodados = [" ".join(c) for c in ex.comandos_rodados()]
        assert not any("UBUNTU20" in c or ".rpm" in c for c in rodados)
        assert rodados[-1] == f"{idrac_tools.RACADM} getversion" and f"rm -rf {idrac_tools.PASTA_EXTRACAO}" in rodados[-2]
        # Pós-instalação oficial: o wrapper ganha permissão de execução antes do link (o .deb real vem sem ela).
        assert rodados.index("chmod +x /opt/dell/srvadmin/sbin/racadm-wrapper-idrac7") < rodados.index(f"ln -sf racadm-wrapper-idrac7 {idrac_tools.RACADM}")

    def test_pacote_antigo_so_com_rpm(self):
        ex = _preparo({"RHEL8": ["srvadmin-hapi-9.4.0.rpm", "srvadmin-idracadm7-9.4.0.rpm"]})
        info = idrac_tools.preparar(ex, "/m")
        assert info["situacao"] == idrac_tools.FALHOU and "versão 11 ou mais nova" in info["mensagem"]
        assert not any(c[0] == "dpkg-deb" for c in ex.comandos_rodados())

    def test_racadm_nao_responde(self):
        ex = _preparo({"UBUNTU22": ["a.deb"]}, versao_rc=1)
        ex.comandos[("dpkg-deb", "-x", f"{idrac_tools.PASTA_EXTRACAO}/iDRACTools/racadm/UBUNTU22/x86_64/a.deb", "/")] = (0, "", "")
        info = idrac_tools.preparar(ex, "/m")
        assert info["situacao"] == idrac_tools.FALHOU and "não respondeu ao 'racadm getversion'" in info["mensagem"]

    def test_tar_corrompido(self):
        ex = _preparo({})
        ex.rc_extracao = 2
        info = idrac_tools.preparar(ex, "/m")
        assert info["situacao"] == idrac_tools.FALHOU and "baixe de novo" in info["mensagem"]

    def test_sem_pacote_e_replay_nao_mexem(self):
        ex = _preparo({})
        ex.diretorios["/m/vendor"] = ["perccli64"]
        assert idrac_tools.preparar(ex, "/m")["situacao"] == idrac_tools.AUSENTE and ex.comandos_rodados() == []
        falso = ExecutorFalso()  # replay, coleta e testes comuns: nunca alteram o sistema
        assert "não é preparado" in idrac_tools.preparar(falso, "/m")["mensagem"] and falso.comandos_rodados() == []

    def test_deteccao_usa_o_racadm_preparado(self, monkeypatch):
        from msscan.deteccao.contexto import Contexto
        from msscan.deteccao.detectores import detectar_ferramentas

        ex = _preparo({"UBUNTU22": ["a.deb"]})
        ex.comandos[("dpkg-deb", "-x", f"{idrac_tools.PASTA_EXTRACAO}/iDRACTools/racadm/UBUNTU22/x86_64/a.deb", "/")] = (0, "", "")
        ctx = Contexto(executor=ex, pasta_dados="/m")
        detectar_ferramentas(ctx)
        assert ctx.ferramentas_fabricante["racadm"] == idrac_tools.RACADM
        assert ctx.cache["idrac_tools"]["situacao"] == idrac_tools.PREPARADO


class TestExtracaoRestrita:
    def test_so_o_pacote_do_vendor(self):
        from msscan.core.executor import validar_idrac_tools

        assert validar_idrac_tools("/run/live/medium/MSSCAN/vendor/Dell-iDRACTools-Web-LX-11.3.0.0-609_A00.tar.gz") == ""
        for ruim in ("/home/cliente/Dell-iDRACTools.tar.gz", "/m/vendor/backup.tar.gz", "vendor/iDRACTools.tar.gz",
                     "/m/vendor/../etc/iDRACTools.tar.gz", "/m/vendor/iDRACTools.tar.gz/../../x"):
            assert validar_idrac_tools(ruim), ruim
        falso = ExecutorFalso()
        rc, _, erro = falso.extrair_idrac_tools("/dev/sda1/iDRACTools.tar.gz")
        assert rc == -1 and "vendor/" in erro and falso.historico[-1].comando[0] == "extrair-idrac-tools"

    def test_tar_generico_continua_recusado(self):
        rc, _, erro = ExecutorFalso({("tar", "-xzf", "x", "-C", "/"): (0, "", "")}).rodar(["tar", "-xzf", "x", "-C", "/"])
        assert rc == -1 and "proibido" in erro

    def test_extracao_real_com_filtro(self, tmp_path):
        import io
        import tarfile

        from msscan.core.executor import ExecutorReal

        pacote = tmp_path / "vendor" / "Dell-iDRACTools-Web-LX-11.3.0.0-609_A00.tar.gz"
        pacote.parent.mkdir()
        with tarfile.open(pacote, "w:gz") as tar:
            for nome, dados in (("iDRACTools/racadm/UBUNTU22/x86_64/a.deb", b"deb"), ("../fora.txt", b"x")):
                info = tarfile.TarInfo(nome)
                info.size = len(dados)
                tar.addfile(info, io.BytesIO(dados))
        destino = tmp_path / "destino"
        rc, _, erro = ExecutorReal()._extrair_idrac_tools(str(pacote), str(destino))
        assert rc == 2 and "ilegível" in erro and not (tmp_path / "fora.txt").exists()  # caminho para fora: recusado
        with tarfile.open(pacote, "w:gz") as tar:
            info = tarfile.TarInfo("iDRACTools/racadm/UBUNTU22/x86_64/a.deb")
            info.size = 3
            tar.addfile(info, io.BytesIO(b"deb"))
        assert ExecutorReal()._extrair_idrac_tools(str(pacote), str(destino))[0] == 0
        assert (destino / "iDRACTools/racadm/UBUNTU22/x86_64/a.deb").read_bytes() == b"deb"


def test_versao_completa_do_idrac_pelo_racadm():
    """19.3: com o racadm, o iDRAC entra no inventário com a versão completa (comparável com o catálogo)."""
    from msscan.atualizacao.inventario import inventario_sistema
    from msscan.atualizacao.versoes import comparar_versoes
    from msscan.deteccao.contexto import Contexto

    ex = ExecutorFalso({("/v/racadm", "getversion"): (0, " Bios Version   = 2.21.2\n iDRAC Version  = 7.00.00.173\n", "")})
    ctx = Contexto(executor=ex, ferramentas_fabricante={"racadm": "/v/racadm"})
    ctx.ipmi.disponivel, ctx.ipmi.mc_info = True, {"firmware": "7.00", "fabricante_id": "674"}
    idrac = next(i for i in inventario_sistema(ctx) if i["nome"] == "iDRAC (BMC)")
    assert (idrac["versao"], idrac["fonte"], idrac["truncada"]) == ("7.00.00.173", "racadm getversion", False)
    assert comparar_versoes(idrac["versao"], "7.00.00.185", False)[0] == "atualizacao"
    sem = Contexto(executor=ExecutorFalso())
    sem.ipmi.disponivel, sem.ipmi.mc_info = True, {"firmware": "7.00", "fabricante_id": "674"}
    assert next(i for i in inventario_sistema(sem) if i["nome"] == "iDRAC (BMC)")["truncada"] is True


def test_sessao_caixa_e_tentar_de_novo():
    """19.2: a caixa aparece no Dell mesmo sem Redfish; "Tentar de novo" relê tudo e, com o iDRAC respondendo pela rede
    (cabo ligado na porta dedicada), o cartão de credenciais aparece (estado.bmc)."""
    import json

    from msscan.core.executor import ExecutorReplay, RespostaRedfish
    from msscan.interface.sessao import Sessao
    from tests.conftest import R640

    sessao = Sessao(ExecutorReplay(R640), None, replay=True)
    sessao.iniciar()
    sessao.aguardar(30)
    estado = sessao.estado()
    assert estado["acesso_idrac"]["situacao"] == IPMI and estado["bmc"] is None
    assert "racadm_pendente" not in sessao.ctx.cache  # replay: nada é baixado (CLAUDE.md 20)
    assert TEXTO_SEM_RACADM in _textos(estado["acesso_idrac"])

    raiz = json.loads((PASTA_FIXTURES / "redfish/dmtf/public_rackmount1/redfish.json").read_text())["GET /redfish/v1"]["corpo"]
    falso = ExecutorFalso({("ipmitool", "lan", "print", "1"): (0, (IDRAC / "ipmitool_lan_print_1.txt").read_text(), ""),
                           ("ipmitool", "delloem", "lan", "get", "active"): (0, "\ndedicated\n", "")})  # fmt: skip
    falso.respostas_redfish[("GET", "https://192.0.2.82/redfish/v1")] = RespostaRedfish("https://192.0.2.82/redfish/v1", 200, corpo=raiz)
    sessao.ctx.executor = falso
    assert sessao.tentar_acesso_idrac()
    sessao.aguardar(30)
    estado = sessao.estado()
    acesso = estado["acesso_idrac"]
    assert acesso["situacao"] == REDE and not acesso["verificando"] and "credenciais" in acesso["botoes"]
    assert acesso["modo"] == "dedicada" and acesso["link"] is True and acesso["ip"] == "192.0.2.82"
    assert estado["bmc"] is not None and estado["bmc"]["situacao"] == "nao_perguntado"


def test_sessao_sem_caixa_fora_da_dell():
    from msscan.deteccao.contexto import Contexto
    from msscan.interface.sessao import _dell

    assert not _dell(Contexto(executor=ExecutorFalso(), fabricante="HPE")) and _dell(Contexto(executor=ExecutorFalso(), fabricante="Dell Inc."))


def test_preparo_sem_wrapper_usa_o_idracadm7():
    ex = _preparo({"UBUNTU22": ["a.deb"]})
    ex.comandos[("dpkg-deb", "-x", f"{idrac_tools.PASTA_EXTRACAO}/iDRACTools/racadm/UBUNTU22/x86_64/a.deb", "/")] = (0, "", "")
    ex.diretorios["/opt/dell/srvadmin/sbin"] = []
    ex.existentes.add("/opt/dell/srvadmin/bin/idracadm7")
    ex.comandos[("ln", "-sf", "/opt/dell/srvadmin/bin/idracadm7", idrac_tools.RACADM)] = (0, "", "")
    assert idrac_tools.preparar(ex, "/m")["situacao"] == idrac_tools.PREPARADO
    ex2 = _preparo({"UBUNTU22": ["a.deb"]})
    ex2.comandos[("dpkg-deb", "-x", f"{idrac_tools.PASTA_EXTRACAO}/iDRACTools/racadm/UBUNTU22/x86_64/a.deb", "/")] = (0, "", "")
    ex2.diretorios["/opt/dell/srvadmin/sbin"] = []
    info = idrac_tools.preparar(ex2, "/m")
    assert info["situacao"] == idrac_tools.FALHOU and "não apareceu" in info["mensagem"]


def test_redfish_404_nao_e_inacessivel_pela_rede():
    """Teste de 15/10/2026 (R620, iDRAC7 sem Redfish): HTTP 404 na raiz = o iDRAC respondeu pela rede."""
    redfish = {"anunciado": True, "confirmado": False, "endereco": "192.0.2.120", "validado": False, "respondeu_http": True,
               "motivo": "endereço do BMC encontrado, mas o serviço Redfish não respondeu: recurso não existe neste BMC (HTTP 404)"}
    estado = avaliar(_leituras(redfish=redfish, racadm_caminho="/opt/racadm", racadm={"situacao": "pronto"}))
    texto = _textos(estado)
    assert estado["situacao"] == RACADM
    assert "respondeu pela rede em 192.0.2.120" in texto and "não tem Redfish" in texto
    assert "inacessível pela rede" not in texto and "Motivo provável" not in texto
    sem_resposta = _textos(avaliar(_leituras(racadm_caminho="/opt/racadm", racadm={"situacao": "pronto"})))
    assert "inacessível pela rede" in sem_resposta  # timeout continua "inacessível"


def test_deteccao_marca_respondeu_http_no_404():
    from msscan.core.executor import RespostaRedfish
    from msscan.deteccao.contexto import Contexto
    from msscan.deteccao.detectores import tentar_redfish

    class Executor(ExecutorFalso):
        def redfish(self, metodo, url, **_):
            return RespostaRedfish(url=url, status=404)

    ctx = Contexto(executor=Executor({}))
    assert not tentar_redfish(ctx, "192.0.2.120", None, "ipmitool lan print 1")
    assert ctx.redfish.respondeu_http and not ctx.redfish.confirmado
