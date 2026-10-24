# Guia de escrita do MSscan

Vale para interface, modo texto, mensagens, relatórios e documentação.
Os textos da interface gráfica, do modo texto (`main.py`), dos relatórios e do motor de atualização ficam em
`msscan/textos/pt_BR.json` (o `tests/test_textos.py` falha se algum ficar no código). Ainda no código: as mensagens dos
módulos de diagnóstico, detecção, ações corretivas e IA (ver "Limitações conhecidas").

## Regras

- Português formal e claro, impessoal, sem gírias, sem exclamações e sem tom de conversa. Frases curtas e diretas.
- Botões com verbo no infinitivo e ação clara: "Iniciar diagnóstico", "Exportar log técnico", "Instalar atualizações".
- Títulos e botões com apenas a primeira letra maiúscula (exceto nomes próprios e siglas).
- Mensagens de erro e aviso em três partes: o que aconteceu, o impacto e a ação recomendada.
  Ex.: "Não foi possível acessar o iDRAC. As atualizações que dependem dele estão indisponíveis. Verifique o cabo da
  porta dedicada e tente novamente."
- Jargão técnico (Redfish, pass-through, job, MultipartUpload, SEL, códigos RED/SUP) fica na seção recolhível
  "Detalhes técnicos"; o texto principal usa os termos do glossário.
- Datas e horários no padrão brasileiro (dd/mm/aaaa hh:mm: `textos.data_br`, `dataBR` no app.js); números com vírgula decimal.
- Situação sempre com ícone e texto, nunca só cor ou só emoji: Normal, Concluído, Em andamento, Atenção, Falha,
  Aguardando, Não verificado, Não aplicável.
- Rodapé discreto "MSscan <versão>" em todas as telas e relatórios.

## Glossário

| Termo oficial | Não usar |
|---|---|
| atualização de firmware | update, driver |
| controlador de gerenciamento (iDRAC na Dell, iLO na HPE, XCC na Lenovo) | BMC no texto principal |
| fila de tarefas; tarefa | Job Queue, job (exceto o botão "Limpar job travado", pedido pelo usuário, e o caminho de menu do iDRAC) |
| registro de eventos | SEL, log do BMC |
| acesso interno ao iDRAC | pass-through, OS to iDRAC Pass-through |
| conversa | chat |
| reversão | rollback |
| modo de demonstração | replay |
| verificação de integridade (SHA-256) | conferência do hash |
| verificações prévias | pré-checagens |
| plano de atualização | tela de aviso |
| riscos assumidos pelo técnico | — |
