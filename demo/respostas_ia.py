"""Respostas de IA GRAVADAS para a demonstração (nenhuma API é chamada; formato da API do Gemini).

Escritas à mão sobre os dados da fixture anonimizada do R640 (``tests/fixtures/dell/poweredge_r640``): cada evidência
cita um trecho literal do diagnóstico, como a validação exige. Servem só para mostrar a tela; não são análise real.
"""

from __future__ import annotations

import json
from typing import Any

ANALISE = {
    "resumo": ("Servidor funcionando com a redundância de energia perdida (PS1 sem energia de entrada) e com histórico de "
               "erro de memória não corrigível no DIMM A2 e de erros nos cabos SAS e no slot PCIe 1 registrados pelo BMC."),
    "problemas": [
        {
            "titulo": "Erro de memória não corrigível no DIMM A2",
            "severidade": "critica",
            "evidencias": [{"componente": "memoria", "trecho": "Uncorrectable ECC"}],
            "explicacao": ("O BMC registrou erro ECC não corrigível no slot A2 (CPU1). Esse tipo de erro pode travar o "
                           "servidor e corromper dados; a atualização de firmware fica bloqueada enquanto ele constar."),
            "acao_recomendada": "Reencaixar ou trocar o pente A2, rodar o diagnóstico da Dell (F10) e depois limpar o SEL.",
            "plano": {"passos": ["Desligar o servidor e reencaixar o pente do slot A2", "Se o erro voltar, trocar o pente",
                                 "Rodar o diagnóstico da Dell (F10 → Hardware Diagnostics)",
                                 "Exportar e limpar o SEL e refazer o diagnóstico da memória no MSscan"],
                      "prioridade": "alta", "intervencao_fisica": True, "troca_de_peca": True,
                      "acao_catalogo": "bmc.sel_exportar_limpar"},
        },
        {
            "titulo": "Redundância de energia perdida",
            "severidade": "alta",
            "evidencias": [{"componente": "fontes", "trecho": "Power Supply AC lost"}],
            "explicacao": "A fonte 1 está sem energia de entrada: se a outra falhar, o servidor desliga.",
            "acao_recomendada": "Conferir o cabo, a tomada e o LED da PSU1.",
            "plano": {"passos": ["Conferir o LED da PSU1 (traseira)", "Trocar o cabo de força ou a tomada",
                                 "Refazer o diagnóstico das fontes"],
                      "prioridade": "alta", "intervencao_fisica": True, "troca_de_peca": False, "acao_catalogo": ""},
        },
        {
            "titulo": "Erros de configuração nos cabos SAS A0/B0",
            "severidade": "media",
            "evidencias": [{"componente": "internos", "trecho": "Cable SAS A0"}],
            "explicacao": "O BMC registrou 'Config Error' nos cabos SAS entre a controladora e o backplane.",
            "acao_recomendada": "Reencaixar os cabos SAS A0 e B0 na controladora e no backplane.",
            "plano": {"passos": ["Desligar e abrir a tampa", "Reencaixar os cabos SAS A0 e B0",
                                 "Refazer o diagnóstico e conferir se o evento se repete"],
                      "prioridade": "media", "intervencao_fisica": True, "troca_de_peca": False, "acao_catalogo": ""},
        },
    ],
    "limitacoes": ["RAID de hardware não presente: nada a verificar de volumes.",
                   "Os eventos do log do BMC são históricos e podem já ter sido resolvidos."],
}

CHAT = {
    "O que resolvo primeiro?": {
        "resposta": ("Primeiro a memória: há erro não corrigível no DIMM A2 [E1] e ele bloqueia qualquer atualização. "
                     "Depois a energia da PSU1 [E2]. Os cabos SAS podem esperar a próxima parada."),
        "evidencias": [{"id": "E1", "componente": "memoria", "trecho": "Uncorrectable ECC"},
                       {"id": "E2", "componente": "fontes", "trecho": "Power Supply AC lost"}],
        "acoes_catalogo": [],
    },
    "Preciso trocar alguma peça?": {
        "resposta": ("Talvez o pente do slot A2 [E1]: reencaixe primeiro; se o erro voltar depois de limpar o SEL, troque. "
                     "Para a fonte, em geral basta o cabo ou a tomada [E2]."),
        "evidencias": [{"id": "E1", "componente": "memoria", "trecho": "Uncorrectable ECC"},
                       {"id": "E2", "componente": "fontes", "trecho": "Power Supply AC lost"}],
        "acoes_catalogo": ["bmc.sel_exportar_limpar"],
    },
    "O que ficou sem verificação?": {
        "resposta": ("Pelo diagnóstico, nada ficou como não verificado: não há RAID de hardware neste servidor e o "
                     "Redfish não foi usado (sem credenciais do BMC), então tudo veio do IPMI e do sistema."),
        "evidencias": [],
        "acoes_catalogo": [],
    },
    "Explique o problema mais grave.": {
        "resposta": ("O mais grave é o erro ECC não corrigível no DIMM A2 [E1]: a memória entregou um dado que o ECC não "
                     "conseguiu corrigir. Isso costuma derrubar o sistema e é sinal de pente com defeito ou mal encaixado."),
        "evidencias": [{"id": "E1", "componente": "memoria", "trecho": "Uncorrectable ECC"}],
        "acoes_catalogo": [],
    },
}


def gemini(conteudo: dict[str, Any]) -> dict[str, Any]:
    """Corpo de resposta no formato do Gemini (``generateContent``)."""
    return {"candidates": [{"content": {"parts": [{"text": json.dumps(conteudo, ensure_ascii=False)}], "role": "model"},
                            "finishReason": "STOP", "index": 0}],
            "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1, "totalTokenCount": 2}}
