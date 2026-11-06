# Privacidade: dados enviados pelo MSscan

Esta página explica, sem letras miúdas, o que o MSscan envia para fora do servidor, por que envia e o que nunca sai.
Como o código-fonte está publicado neste repositório, tudo o que está aqui pode ser conferido no próprio código.

## O que é enviado

Com internet, a cada relatório novo (diagnóstico concluído, atualizações verificadas, instalação terminada), o MSscan envia
ao banco de dados da MSServer uma **cópia anonimizada** do relatório:

- fabricante e modelo do servidor;
- versões de BIOS, iDRAC e demais firmwares;
- peças detectadas (processadores, memória, discos, controladoras, fontes, placas de rede) e o estado de cada uma;
- resultado do diagnóstico e, quando houver, das atualizações e da instalação;
- versão do MSscan e horário do envio.

Para ver o conteúdo **exato** antes de qualquer coisa, use o botão **"Ver o que é enviado"** na tela Início.

## O que nunca é enviado

- **Removidos antes do envio:** serial, service tag, UUID, asset tag, endereços MAC e IP, nome do host e WWN.
- **Nunca enviados:** senhas (do iDRAC ou de qualquer outro acesso), chaves de IA, histórico de comandos e os
  dados dos discos do cliente (arquivos, partições, volumes).

O envio não identifica o servidor, o cliente nem quem usou o MSscan: cada envio leva um número aleatório, que muda a
cada vez.

## Por que é enviado

Para saber em quais modelos, gerações e versões de firmware o MSscan roda, onde falha e quais atualizações dão problema,
e assim corrigir e melhorar as próximas versões. Os dados não são vendidos nem repassados a terceiros e não são usados
para publicidade.

## Como é enviado e onde fica

- Por HTTPS, para um endereço fixo definido no código (`URL_BANCO_DADOS` em `msscan/core/executor.py`).
- O código do envio está em [`msscan/envio_dados.py`](msscan/envio_dados.py) e o do receptor em
  [`ferramentas/banco_dados/receptor.py`](ferramentas/banco_dados/receptor.py).
- O banco fica num servidor da MSServer. O receptor aceita só o formato do MSscan, até 2 MB por envio, e não devolve
  nenhum dado pela rede.
- Sem internet, nada sai. Envio que falhar fica no próprio pendrive (`MSSCAN/envios_pendentes/`) e vai na próxima vez
  que houver internet. Para não enviar, use o MSscan sem rede.

## LGPD

Como os dados chegam anonimizados, sem nada que identifique uma pessoa, eles não são dados pessoais nos termos do
art. 12 da Lei 13.709/2018 (LGPD). Mesmo assim, dúvidas ou pedidos sobre os dados podem ser feitos ao titular abrindo uma
[issue neste repositório](https://github.com/Br3noKio/msscan/issues).

Responsável: Breno / MSServer.
