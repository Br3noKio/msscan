# MOCKUP(rodada final) — iDRAC9 (atualização de firmware)

**Não são saídas reais.** Montadas só com os campos que o exemplo oficial da Dell lê
(`dell/iDRAC-Redfish-Scripting`, `DeviceFirmwareMultipartUploadREDFISH.py` e `DeviceFirmwareRollbackREDFISH.py`,
consultados em 28/09/2026) sobre a estrutura padrão da DMTF (`public_rackmount1_atualizacao`).

- `UpdateService.json`: o da DMTF + `MultipartHttpPushUri` = `/redfish/v1/UpdateService/MultipartUpload` (URI usada pelo exemplo da Dell).
- `Task_agendada.json`, `Task_concluida.json`, `Task_falhou.json`: `TaskState`, `PercentComplete`, `Messages`,
  `Oem.Dell.JobState`, `Oem.Dell.Message` (o exemplo trata "scheduled" como agendado para o próximo reinício).
- `FirmwareInventory_*.json`: itens `Installed-...` e `Previous-...` (rollback), com `Id` no formato citado no exemplo.

## Ações corretivas (fase 10)

- `Attributes_ntp.json`: só os atributos `NTPConfigGroup.1.NTPEnable` (Enabled/Disabled) e `NTP1` a `NTP3`, com os nomes
  da documentação da Dell (guia de atributos do iDRAC9 e módulo Ansible `idrac_redfish_config`, SetManagerAttributes em
  `/redfish/v1/Managers/iDRAC.Embedded.1/Attributes`), consultados em 28/09/2026. Os demais atributos do iDRAC foram omitidos.
- `LogService_Sel.json`: serviço `Sel` com a ação `#LogService.ClearLog` na URI do guia Redfish do iDRAC9
  (`/redfish/v1/Managers/iDRAC.Embedded.1/LogServices/Sel/Actions/LogService.ClearLog`, POST, resposta 204), sobre a
  estrutura DMTF do `LogService`.

Substituir pela coleta real do iDRAC9 do R640 (`TESTES_PENDENTES.md`, A6 e "Rodada final de firmware").
