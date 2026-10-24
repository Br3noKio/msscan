"""CLAUDE.md 18.6: "Exportar log técnico" sem senhas, chaves nem dados do cliente."""

from __future__ import annotations

import json

from msscan.relatorio import log_tecnico


def test_log_sem_segredos_nem_identificadores():
    relatorio = {
        "equipamento": {"fabricante": "Dell Inc.", "modelo": "PowerEdge R640", "serial": "ABC1234", "service_tag": "XYZ9876"},
        "medido": [{"componente": "rede", "status": "OK", "fonte": "ethtool", "mensagem": "eno3 MAC e4:43:4b:00:11:22 IP 192.0.2.50"}],
        "aplicacao": {"situacao": "falhou", "tentativas": [{"caminho": "b", "detalhe": "login de msscan1a2b em 10.20.30.40 recusado"}],
                      "credenciais": {"usuario": "root", "senha": "calvin"}},
        "chat": {"mensagens": [{"texto": "segredo do cliente"}]},
        "interpretacao_ia": {"resumo": "algo"},
    }
    historico = [{"comando": ["ipmitool", "user", "set", "password", "4", "********", "20"], "rc": 0},
                 {"comando": ["POST", "https://10.20.30.40/redfish/v1/SessionService/Sessions", "(credenciais omitidas)"], "rc": 201}]
    estado = {"ia": {"chave": "AIzaSy-chave-secreta"}, "token": "abc"}
    texto = log_tecnico.montar("0.14.0", relatorio, historico, estado, nomes_host=["servidor-cliente"])
    dados = json.loads(texto)
    for proibido in ("calvin", "ABC1234", "XYZ9876", "e4:43:4b:00:11:22", "10.20.30.40", "192.0.2.50", "segredo do cliente",
                     "AIzaSy", "chave-secreta", "\"abc\""):
        assert proibido not in texto, proibido
    assert dados["msscan"] == "0.14.0" and dados["aplicacao"]["situacao"] == "falhou" and "chat" not in dados
    assert dados["aplicacao"]["credenciais"] == "[REMOVIDO]" and "interpretacao_ia" not in dados
    assert dados["comandos"][0]["comando"][5] == "********" and dados["diagnostico"][0]["status"] == "OK"
