# Respostas de provedores de IA (testes)

Respostas **montadas no formato documentado** de cada API — não são capturas reais (não há chaves de API
nesta máquina e os testes nunca chamam APIs reais):

| Arquivo | API | Formato (documentação oficial) |
|---|---|---|
| `anthropic_*.json` | Anthropic Messages (`/v1/messages`) | `content[].text`, `stop_reason` (`end_turn`, `refusal`, `max_tokens`), erro `{"type":"error","error":{...}}` |
| `gemini_*.json` | Gemini API `generateContent` | `candidates[].content.parts[].text`, `finishReason`, `promptFeedback.blockReason`, `modelVersion`, erro `{"error":{"code","message","status"}}` |
| `openai_*.json` | Chat Completions compatível com OpenAI (Groq, OpenRouter, Ollama) | `choices[].message.content`, `finish_reason` (`stop`, `length`, `content_filter`), `model` |

A análise dentro delas tem 3 problemas: um com evidência real do R640 (PS1 sem AC), um que recomenda uma ação do
catálogo (`bmc.sel_exportar_limpar`) e um com evidência inventada e ação inexistente (`disco.formatar_tudo`).

As `chat_*.json` são respostas do **chat** (CLAUDE.md 10e) no mesmo formato: citam `[E1]` com evidência real do R640
(PS1 sem AC), `[E2]` com evidência inventada e `[E3]` sem evidência listada, e recomendam uma ação do catálogo
(`bmc.sel_exportar_limpar`) e uma inexistente (`disco.formatar_tudo`).

TODO(fixture): respostas reais de cada provedor (TESTES_PENDENTES.md, seção 5).
