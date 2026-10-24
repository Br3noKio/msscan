# Atualização de firmware (Dell)

A parte de **maior risco** do MSscan: firmware interrompido ou errado pode inutilizar o servidor. Disponível só para
servidores Dell (o fabricante é conferido no plano e de novo antes de qualquer mudança no iDRAC).

## Regras

1. Pergunta opcional, padrão **Não**. Nenhum componente vem marcado; não existe "Atualizar tudo".
2. Só pacotes do site oficial da Dell, por HTTPS, com **SHA-256 conferido**. Hash diferente: arquivo apagado, item abortado.
3. **Nunca downgrade**, nunca pacote de outro modelo, nunca flags que forçam pacote não aplicável.
4. Versão incerta: "Não foi possível determinar", **nunca** "Não é necessária a atualização".
5. Configuração do iDRAC e da BIOS exportada para o pendrive antes de instalar, quando possível.
6. Tudo vai para o relatório e para o log técnico.

## Fluxo

```mermaid
flowchart LR
    E1[Selecionar<br/>componentes] --> E2[Baixar<br/>catálogo ou manual]
    E2 --> E3[Verificar<br/>integridade, modelo, versão]
    E3 --> E4[Plano de<br/>atualização]
    E4 --> E5[Digitar<br/>ATUALIZAR]
    E5 --> E6[Instalação<br/>automática]
```

- **Catálogo:** catálogo oficial da Dell (cache de 7 dias), filtrado pelo System ID e pelo modelo. Gerações antigas usam o
  último catálogo publicado para elas. Sem iDRAC acessível, o catálogo do Dell System Update fornece os pacotes Linux
  (`.BIN`) da mesma versão.
- **Escolha do pacote:** o mais recente aplicável, respeitando as dependências declaradas pela Dell.
- **Plano de atualização:** uma tela com o que será instalado (versão atual → nova), a ordem, o tempo estimado, os
  reinícios, a verificação de hardware feita na hora e as mudanças temporárias no iDRAC. Foco inicial em "Cancelar".

## Bloqueios

| Bloqueia sempre | Bloqueia, mas o técnico pode assumir (`ASSUMO OS RISCOS`) |
|---|---|
| Hash diferente | Fonte com defeito real ou classificação incerta |
| Pacote de outro modelo ou versão não confirmada | Erro de memória não corrigível atual |
| Downgrade | Temperatura ou ventoinha crítica |
| Outra gravação ou tarefa em andamento no iDRAC | Componente essencial não verificado |
| Fabricante que não é Dell | Configuração de memória fora do suportado |
| Firmware de RAID ou discos com RAID degradado | RAID com falha ou alerta |

Outras confirmações, só quando se aplicam: `SEM REDUNDANCIA` (fonte sem energia) e "Estou ciente de que é um evento
antigo" (evento crítico histórico). Riscos assumidos valem só para aquela instalação e ficam no relatório.

## Ordem

```mermaid
flowchart LR
    N[Placas de rede] --> O[Demais componentes] --> I[iDRAC<br/>sozinho] --> B[BIOS<br/>sozinha, por último]
```

Itens que dependem de reinício são agendados juntos. Depois do firmware do iDRAC, o MSscan espera ele voltar já com a
versão nova (até 30 min, com sessão nova). **Para na primeira falha.**

## Caminhos de instalação

Por componente, em ordem, até um funcionar; cada tentativa é registrada:

| Caminho | Como |
|---|---|
| Repositório no iDRAC9 | O iDRAC instala de um repositório servido pelo pendrive (lotes maiores; exige licença Enterprise/Datacenter) |
| a) Redfish, credenciais do técnico | Envio direto do pacote (padrão até 2 componentes) |
| b) Redfish, administrador temporário | Criado pelo IPMI interno e apagado no fim |
| c) Acesso interno temporário | Liga o OS to iDRAC Pass-through e repete a/b |
| d) racadm local | `racadm update` |
| e) Pacote Linux `.BIN` | Execução direta, com os códigos de saída oficiais |
| f) Outro formato oficial | Mesmo componente e versão, outro pacote do catálogo |
| g) Explicação | O que faltou e o que o técnico pode fazer |

**Proteção contra gravação dupla:** só uma recusa explícita do iDRAC passa ao próximo caminho. Resposta incerta (tempo
esgotado, conexão cortada) para a instalação com "confira a fila de tarefas", sem reenviar.

iDRAC7/8 sem `.BIN` para o modelo aceitam o `.EXE` pelo Redfish (`SimpleUpdate`), só se o iDRAC anunciar esse método.

## Reinícios e retomada

- Antes de cada reinício: boot único pelo pendrive. Sem como agendar, o aviso vem antes de começar ("escolha o pendrive
  no F11").
- O estado fica em `MSSCAN/estado/`. Na volta, o MSscan relê o hardware, confere antes × depois e **continua sozinho** os
  pendentes (mesmo servidor, até 24 h). Condição de hardware nova e não aceita **para tudo**.
- Faixa fixa em qualquer tela: verde "Seguro desligar agora" ou vermelha "NÃO desligue: gravando firmware".
- Botão "Parar depois do componente atual"; depois, "Continuar de onde parou".

## Mudanças temporárias no iDRAC

Usuário administrador temporário, acesso interno e tarefas do repositório são registrados no pendrive **antes** de serem
feitos e **desfeitos sozinhos** no fim, inclusive depois de queda (no próximo boot pelo pendrive). A rede do iDRAC e a
senha de usuários existentes nunca são alteradas. Se o estado real não estiver como o MSscan deixou, nada é mexido.

## Pacote manual

Para componentes sem pacote no catálogo: navegador integrado na página de drivers do servidor (só domínios oficiais da
Dell, HPE e Lenovo), download detectado sozinho, SHA-256 da página da Dell colado pelo técnico e conferido **antes de
qualquer execução**, checagem do pacote sem gravar e o mesmo plano de atualização.
