"""Integração com o DMTF Redfish Mockup Server (opcional).

Roda só com ``MSSCAN_MOCKUP_URL`` definido, por exemplo, com o mockup
``public-localstorage`` servido em http://127.0.0.1:8001:

    git clone https://github.com/DMTF/Redfish-Mockup-Server
    python3 redfishMockupServer.py -H 127.0.0.1 -p 8001 -S -D <Redfish-Publications>/mockups/public-localstorage
    MSSCAN_MOCKUP_URL=http://127.0.0.1:8001 python3 -m pytest tests/test_redfish_mockup.py

O mockup aceita qualquer senha (o limite de tentativas é testado com ``tests/redfish_falso.py``).
"""

from __future__ import annotations

import json
import os

import pytest

from msscan.core.executor import ExecutorReal
from msscan.core.modelo import Status
from msscan.deteccao.contexto import Contexto
from msscan.modulos.registro import registro_padrao
from msscan.provedores.redfish import COMPONENTES_REDFISH
from msscan.redfish.acesso import AcessoBMC
from msscan.redfish.cliente import ClienteRedfish

URL = os.environ.get("MSSCAN_MOCKUP_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="MSSCAN_MOCKUP_URL não definido (DMTF Redfish Mockup Server)")


def test_diagnostico_redfish_no_mockup():
    executor = ExecutorReal()
    cliente = ClienteRedfish(executor, URL)
    raiz = cliente.raiz()
    ctx = Contexto(executor=executor)
    ctx.redfish.confirmado = True
    ctx.cache["redfish_raiz"] = raiz
    ctx.bmc = AcessoBMC(cliente, raiz["contas"])
    ctx.bmc.tentar("Administrator", "qualquer-senha-do-mockup")
    assert ctx.bmc.validado
    try:
        resultados = registro_padrao().diagnosticar(ctx, somente=COMPONENTES_REDFISH)
    finally:
        assert ctx.bmc.encerrar()
    por_nome = {r.componente: r for r in resultados}
    assert por_nome["sensores"].fonte.startswith("Redfish") and por_nome["sensores"].status != Status.NAO_VERIFICADO
    assert por_nome["log_bmc"].fonte.startswith("Redfish")
    historico = json.dumps(executor.historico_dict())
    assert "qualquer-senha-do-mockup" not in historico
    assert any(h["comando"][0] == "DELETE" for h in executor.historico_dict())
