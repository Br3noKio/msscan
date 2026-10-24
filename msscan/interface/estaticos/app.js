/*
 * MSscan — interface gráfica (JavaScript puro, sem frameworks, sem recursos externos).
 *
 * A lógica de diagnóstico fica toda no Python; aqui só se mostra o estado da API.
 * Regras de segurança da interface (valem para as fases futuras):
 *  - nenhuma ação perigosa recebe foco inicial nem é acionada por um Enter "padrão";
 *  - perguntas opcionais começam em "Não"; checkboxes de atualização começam desmarcados;
 *  - gravar firmware exige digitar ATUALIZAR; não existe "atualizar tudo";
 *  - ações corretivas: nenhuma vem escolhida, uma por vez, prévia antes e CORRIGIR digitado.
 * Dados vindos da API entram SEMPRE por textContent (nunca innerHTML).
 */
"use strict";

/* ------------------------------------------------------------------ utilidades */

const $ = (id) => document.getElementById(id);

/*
 * Textos da interface: todos no arquivo central msscan/textos/pt_BR.json (servido como /estaticos/textos.js).
 * T("secao.chave", valor0, valor1...) troca {0}, {1}... pelos valores. Nenhum texto fica escrito neste arquivo
 * (tests/test_textos.py falha se ficar). Regras de escrita e glossário: docs/guia_de_escrita.md.
 */
const TEXTOS = window.TEXTOS || {};
function T(chave, ...valores) {
  let texto = TEXTOS[chave];
  if (texto === undefined) texto = chave;  // chave ausente aparece como está (a revisão e o teste acham)
  valores.forEach((valor, posicao) => {
    const marca = "{" + posicao + "}";
    const limpo = String(valor).trim();
    // "({0})." com um valor que já termina em ponto: o ponto do valor sai (evita "texto.).").
    texto = texto.split(marca + ")").join(limpo.replace(/\.$/, "") + ")").split(marca).join(limpo);
  });
  return texto;
}

/* Data e hora no padrão brasileiro (dd/mm/aaaa hh:mm); texto não reconhecido volta como veio. */
/** Motivo vindo do servidor como frase: maiúscula no início e um ponto final só (sem ".." quando já termina em ponto). */
function frase(texto) {
  if (!texto) return "";
  const t = texto.charAt(0).toUpperCase() + texto.slice(1);
  return /[.!?]$/.test(t) ? t : t + ".";
}

function dataBR(valor) {
  if (!valor) return T("comum.data_desconhecida");
  const iso = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?/.exec(String(valor));
  return iso ? `${iso[3]}/${iso[2]}/${iso[1]}` + (iso[4] ? ` ${iso[4]}:${iso[5]}` : "") : String(valor);
}

/* Textos fixos do index.html: data-t (conteúdo) e data-t-<atributo> (aria-label, title, placeholder, alt). */
function traduzirPagina(raiz) {
  for (const alvo of (raiz || document).querySelectorAll("[data-t]")) alvo.textContent = T(alvo.dataset.t);
  for (const atributo of ["aria-label", "title", "placeholder", "alt"]) {
    for (const alvo of (raiz || document).querySelectorAll(`[data-t-${atributo}]`)) alvo.setAttribute(atributo, T(alvo.getAttribute(`data-t-${atributo}`)));
  }
}
traduzirPagina();

function el(tag, atributos, ...filhos) {
  const no = document.createElement(tag);
  for (const [chave, valor] of Object.entries(atributos || {})) {
    if (valor === null || valor === undefined || valor === false) continue;
    if (chave === "classe") no.className = valor;
    else if (chave === "texto") no.textContent = valor;
    else if (chave.startsWith("on")) no.addEventListener(chave.slice(2), valor);
    else no.setAttribute(chave, valor === true ? "" : String(valor));
  }
  for (const filho of filhos.flat(Infinity)) {
    if (filho === null || filho === undefined || filho === false) continue;
    no.append(filho instanceof Node ? filho : document.createTextNode(String(filho)));
  }
  return no;
}

/* Ícone Lucide (elementos fixos de icones.js, nunca dados da API): linha fina em currentColor. */
const SVG_NS = "http://www.w3.org/2000/svg";
function icone(nome, classe) {
  const svg = document.createElementNS(SVG_NS, "svg");
  for (const [a, v] of [["viewBox", "0 0 24 24"], ["width", "16"], ["height", "16"], ["fill", "none"], ["stroke", "currentColor"],
    ["stroke-width", "1.75"], ["stroke-linecap", "round"], ["stroke-linejoin", "round"], ["aria-hidden", "true"], ["focusable", "false"]]) {
    svg.setAttribute(a, v);
  }
  svg.setAttribute("class", "icone" + (classe ? " " + classe : ""));
  for (const [tag, atributos] of ICONES[nome] || []) {
    const parte = document.createElementNS(SVG_NS, tag);
    for (const [a, v] of Object.entries(atributos)) parte.setAttribute(a, v);
    svg.append(parte);
  }
  return svg;
}

function preencherIcones(raiz) {
  for (const alvo of raiz.querySelectorAll("[data-icone]")) {
    if (!alvo.querySelector("svg")) alvo.prepend(icone(alvo.dataset.icone));
  }
}

function anunciar(texto) {
  const regiao = $("anuncio");
  regiao.textContent = "";
  window.setTimeout(() => { regiao.textContent = texto; }, 50);
}

const STATUS = {
  OK: { texto: T("status.normal"), icone: "check-circle" },
  ALERTA: { texto: T("status.alerta"), icone: "alert" },
  FALHA: { texto: T("status.falha"), icone: "x-circle" },
  NAO_VERIFICADO: { texto: T("status.nao_verificado"), icone: "question" },
  NAO_APLICAVEL: { texto: T("status.nao_aplicavel"), icone: "skip" },
  PENDENTE: { texto: T("status.aguardando"), icone: "clock" },
  ANDAMENTO: { texto: T("status.em_andamento"), icone: "sync" },
  NA: { texto: T("status.nao_aplicavel"), icone: "skip" },
  DISPONIVEL: { texto: T("status.disponivel"), icone: "check-circle", classe: "OK" },
  // Versão nova no catálogo: informação (azul), não problema de hardware — Atenção fica para alertas reais.
  ATUALIZAVEL: { texto: T("status.atualizacao_disponivel"), icone: "download", classe: "INFO" },
};

/* Etiqueta de status: ícone + texto + cor (nunca só a cor). */
function etiqueta(status, textoExtra) {
  const def = STATUS[status] || STATUS.NAO_VERIFICADO;
  return el("span", { classe: "etiqueta st-" + (def.classe || status) },
    icone(def.icone, status === "ANDAMENTO" ? "girando" : ""), textoExtra || def.texto);
}

/* ------------------------------------------------------------------ API */

let TOKEN = "";

function lerToken() {
  const hash = window.location.hash;
  const achado = hash.match(/^#t=([A-Za-z0-9_-]+)/);
  if (achado) {
    TOKEN = achado[1];
    try { window.sessionStorage.setItem("msscan-token", TOKEN); } catch (e) { /* armazenamento indisponível */ }
    window.history.replaceState(null, "", "#/inicio");
  } else {
    try { TOKEN = window.sessionStorage.getItem("msscan-token") || ""; } catch (e) { TOKEN = ""; }
  }
  return TOKEN !== "";
}

async function api(caminho, metodo, corpo) {
  const cabecalhos = { "X-MSscan-Token": TOKEN };
  if (corpo !== undefined) cabecalhos["Content-Type"] = "application/json";
  const resposta = await fetch("/api/" + caminho, {
    method: metodo || "GET",
    headers: cabecalhos,
    body: corpo === undefined ? undefined : JSON.stringify(corpo),
    cache: "no-store",
  });
  const dados = await resposta.json().catch(() => ({}));
  if (!resposta.ok) throw Object.assign(new Error(dados.erro || ("HTTP " + resposta.status)), { status: resposta.status });
  return dados;
}

/* ------------------------------------------------------------------ navegação por setas */

/*
 * Lista/tabela com "roving tabindex": só o item atual entra no Tab; setas, Home e End
 * movem o foco; Enter (ou clique) chama aoAbrir. Serve para listas e linhas de tabela.
 */
function navegavel(container, seletor, aoAbrir) {
  const itens = () => Array.from(container.querySelectorAll(seletor)).filter((i) => !i.hidden && !i.classList.contains("alem-limite"));
  function focar(item) {
    for (const outro of itens()) {
      outro.tabIndex = outro === item ? 0 : -1;
      if (outro.getAttribute("role") === "option") outro.setAttribute("aria-selected", outro === item ? "true" : "false");
    }
    item.focus();
  }
  container.addEventListener("keydown", (ev) => {
    const lista = itens();
    const atual = lista.indexOf(document.activeElement);
    if (atual < 0) return;
    let alvo = null;
    if (ev.key === "ArrowDown") alvo = lista[Math.min(atual + 1, lista.length - 1)];
    else if (ev.key === "ArrowUp") alvo = lista[Math.max(atual - 1, 0)];
    else if (ev.key === "Home") alvo = lista[0];
    else if (ev.key === "End") alvo = lista[lista.length - 1];
    else if (ev.key === "Enter" && aoAbrir) { ev.preventDefault(); aoAbrir(lista[atual]); return; }
    if (alvo) { ev.preventDefault(); focar(alvo); }
  });
  container.addEventListener("click", (ev) => {
    const item = ev.target.closest(seletor);
    if (!item || !container.contains(item)) return;
    focar(item);
    if (aoAbrir) aoAbrir(item);
  });
  return {
    garantirTabindex() {
      const lista = itens();
      if (lista.length && !lista.some((i) => i.tabIndex === 0)) lista[0].tabIndex = 0;
      for (const i of lista) if (i.tabIndex !== 0) i.tabIndex = -1;
    },
    focar,
    primeiro() { const lista = itens(); if (lista.length) focar(lista[0]); },
  };
}

/* Abas (padrão ARIA): setas esquerda/direita, Home/End. */
function abasComSetas(lista, ativar) {
  lista.addEventListener("keydown", (ev) => {
    const abas = Array.from(lista.querySelectorAll('[role="tab"]')).filter((a) => a.getClientRects().length);
    const atual = abas.indexOf(document.activeElement);
    if (atual < 0) return;
    let alvo = null;
    if (ev.key === "ArrowRight") alvo = abas[(atual + 1) % abas.length];
    else if (ev.key === "ArrowLeft") alvo = abas[(atual - 1 + abas.length) % abas.length];
    else if (ev.key === "Home") alvo = abas[0];
    else if (ev.key === "End") alvo = abas[abas.length - 1];
    if (alvo) { ev.preventDefault(); alvo.focus(); ativar(alvo); }
  });
}

/* ------------------------------------------------------------------ estado e rotas */

let ESTADO = null;
let rotaAtual = "";
const TELAS = ["rede", "inicio", "diagnostico", "relatorios", "ia", "chat", "atualizacoes", "terminal", "sobre", "erro"];

function redeBloqueia(estado) {
  return estado && estado.etapa === "rede" && estado.rede.situacao !== "desativada";
}

function ir(rota) {
  if (window.location.hash !== "#/" + rota) window.location.hash = "#/" + rota;
  else aplicarRota();
}

function aplicarRota(focarConteudo) {
  let rota = window.location.hash.replace(/^#\/?/, "") || "inicio";
  if (ESTADO && redeBloqueia(ESTADO)) rota = "rede";
  if (ESTADO && ESTADO.etapa === "erro") rota = "erro";
  const [tela, sub] = rota.split("/");
  const nomeTela = TELAS.includes(tela) ? tela : "inicio";
  const mudou = rota !== rotaAtual;
  rotaAtual = rota;

  for (const nome of TELAS) $("tela-" + nome).hidden = nome !== nomeTela;
  if (mudou) { animarTroca(nomeTela); if (gavetaAberta()) fecharGavetas(false); }
  for (const aba of document.querySelectorAll("#abas [role=tab]")) {
    const ativa = aba.dataset.tela === nomeTela;
    aba.setAttribute("aria-selected", ativa ? "true" : "false");
    aba.tabIndex = ativa || (nomeTela === "rede" && aba.dataset.tela === "inicio") ? 0 : -1;
    aba.setAttribute("aria-disabled", redeBloqueia(ESTADO) ? "true" : "false");
  }

  /* Detalhe e visor cuidam do próprio foco (título ao abrir; item de origem ao voltar). */
  let focoTratado = false;
  if (nomeTela === "diagnostico") focoTratado = mostrarDetalhe(sub ? decodeURIComponent(sub) : null, mudou);
  if (nomeTela === "relatorios") focoTratado = mostrarVisor(sub === "ver", mudou);
  if (nomeTela === "ia") focoTratado = mostrarConfigIA(sub === "configurar", mudou);
  if (nomeTela === "chat" && mudou) rolarChat();
  if (nomeTela === "rede") renderizarRede();
  if (nomeTela === "terminal" && mudou) { abrirTerminal(); focoTratado = true; }
  if (mudou && focarConteudo && !focoTratado) $("conteudo").focus();
  if (ESTADO) renderizarEtapas(ESTADO);
  aplicarLimites();
}

/* ------------------------------------------------------------------ cabeçalho */

let redeContinuaAnterior = null;

function renderizarCabecalho(estado) {
  renderizarEnergia(estado);
  const eq = estado.equipamento;
  // Barra: só o modelo (o fabricante e a geração ficam na visão do servidor); o nome completo no aria-label.
  $("modelo").textContent = eq ? eq.modelo || eq.fabricante || T("renderizar_cabecalho.modelo_desconhecido") : T("renderizar_cabecalho.detectando");
  if (eq) $("modelo").setAttribute("aria-label", [eq.fabricante, eq.modelo].filter(Boolean).join(" "));
  const rede = estado.rede;
  const indicador = $("indicador-rede");
  const textos = {
    verificando: [T("renderizar_cabecalho.rede_verificando"), ""],
    aguardando: [T("renderizar_cabecalho.rede_aguardando"), "alerta"],
    ok: [T("renderizar_cabecalho.rede_ok"), "ok"],
    falha: [T("renderizar_cabecalho.sem_rede"), "falha"],
    sem_internet: [T("renderizar_cabecalho.sem_internet"), "alerta"],
    falha_replay: [T("renderizar_cabecalho.sem_rede_replay"), "alerta"],
    desativada: [T("renderizar_cabecalho.sem_rede_semrede_1"), "alerta"],
  };
  let [texto, classe] = textos[rede.situacao] || [T("renderizar_cabecalho.rede"), ""];
  // Rede contínua (CLAUDE.md 5b): ícone de recarregar girando + "Reconectando…" + motivo; volta sozinho.
  const continuo = rede.continuo;
  const reconectando = Boolean(continuo && continuo.situacao === "reconectando");
  let rotulo = texto;
  if (reconectando) {
    texto = T("renderizar_cabecalho.reconectando") + (continuo.curto || "rede fora") + ")";
    rotulo = T("renderizar_cabecalho.reconectando_2") + (continuo.motivo || "rede fora") + T("renderizar_cabecalho.o_diagnostico_local_continua_ia");
    classe = "alerta";
  }
  $("indicador-rede-texto").textContent = texto;
  indicador.className = "pilula pilula-botao indicador-rede" + (classe ? " " + classe : "");
  indicador.setAttribute("aria-label", rotulo + T("renderizar_cabecalho.abrir_detalhes_da_rede"));
  indicador.title = reconectando ? rotulo : "";
  const iconeAtual = indicador.querySelector("svg");
  const nomeIcone = reconectando ? "sync" : "globe";
  if (iconeAtual && iconeAtual.dataset.nome !== nomeIcone) {
    const novo = icone(nomeIcone, reconectando ? "girando" : "");
    novo.dataset.nome = nomeIcone;
    iconeAtual.replaceWith(novo);
  }
  const chaveContinuo = continuo ? continuo.situacao + "|" + (continuo.problema || "") : "";
  if (continuo && redeContinuaAnterior !== null && chaveContinuo !== redeContinuaAnterior) {
    anunciar(reconectando ? T("renderizar_cabecalho.rede_caiu") + continuo.motivo + T("renderizar_cabecalho.reconectando_3") : T("renderizar_cabecalho.rede_de_volta_conexao_restabelecida"));
  }
  if (continuo) redeContinuaAnterior = chaveContinuo;
  renderizarFaixaInternet(rede, reconectando);
  const problemas = estado.diagnostico.componentes.filter((c) => c.status === "FALHA" || c.status === "ALERTA").length;
  const contador = $("contador-problemas");
  contador.hidden = problemas === 0;
  contador.textContent = problemas ? String(problemas) : "";
  contador.setAttribute("aria-label", problemas + T("renderizar_cabecalho.com_problema"));
}

/*
 * Barra de etapas (CLAUDE.md 12b-2). Concluídas: ícone de "feito" + texto oculto para leitores de tela;
 * atual (aria-current="step"): a da tela aberta, se for uma etapa; senão, a primeira ainda não feita.
 */
const ETAPAS = [["rede", T("etapas.rede"), "rede"], ["diagnostico", T("etapas.diagnostico"), "diagnostico"], ["ia", T("etapas.analise_por_ia"), "ia"],
  ["chat", T("etapas.chat"), "chat"], ["atualizacoes", T("etapas.atualizacoes"), "atualizacoes"], ["relatorio", T("etapas.relatorio"), "relatorios"]];

function etapasFeitas(estado) {
  const concluido = estado.etapa === "concluido";
  return {
    rede: estado.etapa !== "rede",
    diagnostico: concluido,
    ia: Boolean(estado.ia && estado.ia.situacao === "concluida"),
    chat: Boolean(estado.chat && estado.chat.quantidade > 1),
    atualizacoes: Boolean(estado.atualizacoes && !["nao_solicitadas", "verificando"].includes(estado.atualizacoes.situacao)),
    relatorio: concluido && Boolean(estado.relatorio),
  };
}

function renderizarEtapas(estado) {
  const feitas = etapasFeitas(estado);
  const tela = rotaAtual.split("/")[0];
  const daTela = ETAPAS.find((e) => e[2] === tela);
  const atual = daTela ? daTela[0] : (ETAPAS.find((e) => !feitas[e[0]]) || ETAPAS[ETAPAS.length - 1])[0];
  const chave = JSON.stringify([feitas, atual]);
  const lista = $("etapas");
  if (lista.dataset.chave === chave) return;
  lista.dataset.chave = chave;
  for (const [id] of ETAPAS) {
    const aba = $("aba-" + TELA_DA_ETAPA[id]);
    if (aba) aba.classList.toggle("feita", Boolean(feitas[id]));
  }
  lista.replaceChildren(...ETAPAS.map(([id, nome]) => el("li", { classe: feitas[id] ? "feita" : "", "aria-current": id === atual ? "step" : null },
    icone(feitas[id] ? "check" : "dot-fill"), nome, feitas[id] ? el("span", { classe: "visualmente-oculto", texto: T("renderizar_etapas.concluida") }) : null)));
}

/* ------------------------------------------------------------------ 1. rede */

function renderizarRede() {
  if (!ESTADO) return;
  const rede = ESTADO.rede;
  const lista = $("rede-niveis");
  lista.replaceChildren();
  const verificando = rede.situacao === "verificando" || rede.situacao === "aguardando";
  if (rede.situacao === "desativada") {
    lista.append(el("li", {}, el("span", { classe: "num", texto: "—" }),
      el("span", { texto: T("renderizar_rede.verificacao_desativada_pelo_parametro_de") }), etiqueta("NA")));
  } else if (verificando && !rede.niveis.length) {
    const texto = rede.situacao === "aguardando"
      ? T("renderizar_rede.aguardando_o_link_e_o") + rede.espera_s + " s)…" : T("renderizar_rede.verificando_cabo_endereco_ip_e");
    lista.append(el("li", {}, el("span", { classe: "num", texto: "…" }), el("span", { texto }), etiqueta("ANDAMENTO", T("renderizar_rede.verificando"))));
  } else {
    rede.niveis.forEach((nivel, i) => {
      const status = nivel.situacao === "ok" ? "OK" : nivel.situacao === "falha" ? "FALHA" : "PENDENTE";
      const rotulo = nivel.situacao === "nao_testado" ? T("renderizar_rede.nao_testado") : null;
      lista.append(el("li", {},
        el("span", { classe: "num", texto: (i + 1) + "/3" }),
        el("span", {}, nivel.nome, nivel.detalhe ? el("span", { classe: "detalhe", texto: nivel.detalhe }) : null),
        etiqueta(verificando ? "ANDAMENTO" : status, verificando ? T("renderizar_rede.verificando") : rotulo)));
    });
  }
  const falhou = rede.situacao === "falha" || rede.situacao === "sem_internet";
  $("rede-problema").hidden = !(falhou || rede.situacao === "falha_replay");
  $("rede-problema-titulo").textContent = rede.titulo;
  $("rede-problema-detalhes").replaceChildren(...rede.detalhes.map((d) => el("li", { texto: d })));
  const tentar = $("rede-tentar");
  tentar.hidden = !(falhou || verificando);
  tentar.disabled = verificando;
  tentar.lastElementChild.textContent = verificando ? T("renderizar_rede.verificando_2") : T("renderizar_rede.tentar_novamente");
  $("rede-voltar").hidden = redeBloqueia(ESTADO);
  const hora = $("rede-hora");
  hora.hidden = !rede.precisa_hora;
  if (rede.precisa_hora && !$("rede-hora-texto").value && document.activeElement !== $("rede-hora-texto")) {
    $("rede-hora-texto").value = rede.hora_sistema || "";
  }
}

/* 28.1: sem internet o MSscan segue; a faixa fica em qualquer aba com o motivo real, até reconectar sozinho. */
function renderizarFaixaInternet(rede, reconectando) {
  const continuo = rede.continuo || {};
  const fora = rede.situacao === "sem_internet" || (reconectando && continuo.problema === "sem_internet");
  $("faixa-internet").hidden = !fora;
  if (!fora) return;
  const motivo = rede.causa || continuo.motivo || "";
  const texto = T("faixa_internet.sem_internet") + (motivo ? " (" + motivo + ")" : "") + T("faixa_internet.reconecta_sozinho");
  if ($("faixa-internet-texto").textContent !== texto) $("faixa-internet-texto").textContent = texto;
}

async function ajustarHora() {
  const mensagem = $("rede-hora-mensagem");
  $("rede-hora-ajustar").disabled = true;
  try {
    const resposta = await api("rede/hora", "POST", { texto: $("rede-hora-texto").value });
    mensagem.textContent = resposta.mensagem || T("ajustar_hora.relogio_ajustado");
  } catch (e) {
    mensagem.textContent = e.message;
  } finally {
    $("rede-hora-ajustar").disabled = false;
  }
  atualizar();
}

async function tentarRede() {
  const botao = $("rede-tentar");
  botao.disabled = true;
  $("internet-tentar").disabled = true;
  setTimeout(() => { $("internet-tentar").disabled = false; }, 3000);
  try { await api("rede/tentar", "POST"); anunciar(T("tentar_rede.verificando_a_rede_novamente")); } catch (e) { anunciar(e.message); }
  atualizar();
}

/* ------------------------------------------------------------------ 2. início */

let capacidadesDesenhadas = "";
let navCapacidades = null;

/* Configuração do servidor (pedido de 01/10/2026): resumo no Início e cartão no Diagnóstico, avisos de montagem. */
let configDesenhada = "";
function renderizarConfiguracao(config) {
  const chave = JSON.stringify(config || null);
  if (chave === configDesenhada) return;
  configDesenhada = chave;
  $("inicio-config").textContent = config ? config.resumo : "";
  const avisos = (config && config.avisos) || [];
  $("inicio-config-avisos").hidden = !avisos.length;
  $("inicio-config-avisos").textContent = avisos.length ? T("renderizar_configuracao.montagem_fora_do_recomendado_pelo") + avisos.join(" ") : "";
  $("diag-config").hidden = !config;
  if (!config) return;
  $("diag-config-resumo").textContent = config.resumo;
  $("diag-config-avisos").replaceChildren(...avisos.map((t) => el("li", {}, etiqueta("ALERTA", T("renderizar_configuracao.atencao")), " " + t)));
  $("diag-config-info").replaceChildren(...(config.informacoes || []).map((t) => el("li", { texto: t })));
  $("diag-config-fonte").textContent = config.fonte_regras ? T("renderizar_configuracao.regras") + config.fonte_regras + "." : "";
}

function renderizarInicio(estado) {
  const eq = estado.equipamento;
  const botao = $("botao-iniciar");
  const rotulo = botao.lastElementChild;
  if (!eq) {
    $("inicio-servidor").textContent = estado.etapa === "deteccao" ? T("renderizar_inicio.detectando_o_equipamento") : T("renderizar_inicio.aguardando_a_rede");
    botao.disabled = true;
    return;
  }
  $("inicio-servidor").textContent = [eq.fabricante, eq.modelo].filter(Boolean).join(" ") || T("renderizar_inicio.modelo_desconhecido");
  $("inicio-bios").textContent = eq.versao_bios ? "BIOS " + eq.versao_bios : "";
  renderizarConfiguracao(estado.configuracao);
  const diag = estado.etapa === "diagnostico" || estado.etapa === "concluido";
  botao.disabled = !(estado.etapa === "pronto" || diag);
  rotulo.textContent = diag ? T("renderizar_inicio.ver_diagnostico") : T("renderizar_inicio.iniciar_diagnostico");
  // /tmp: nada fica no pendrive. O aviso aparece antes do diagnóstico, não só no relatório.
  const avisoPendrive = $("inicio-aviso-pendrive");
  avisoPendrive.textContent = (eq.pendrive && eq.pendrive.aviso) || "";
  avisoPendrive.hidden = !avisoPendrive.textContent;
  // 28.1: relógio do sistema acertado em um dia ou mais (o do BIOS continua errado; o MSscan nunca o altera).
  const avisoRelogio = $("inicio-aviso-relogio");
  avisoRelogio.textContent = (estado.rede && estado.rede.aviso_relogio) || "";
  avisoRelogio.hidden = !avisoRelogio.textContent;
  $("inicio-modo").textContent = estado.modo === "replay"
    ? T("renderizar_inicio.modo_demonstracao_replay_nada_e") : "";

  const chave = JSON.stringify(eq.capacidades);
  if (chave === capacidadesDesenhadas) return;
  capacidadesDesenhadas = chave;
  const lista = $("inicio-capacidades");
  lista.setAttribute("aria-busy", "false");
  lista.replaceChildren(...eq.capacidades.map((c) => el("li", { classe: "item-capacidade", tabindex: "-1" },
    etiqueta(c.disponivel ? "DISPONIVEL" : "NA"),
    el("strong", { texto: c.nome }),
    el("span", { classe: "detalhe", texto: c.detalhe }))));
  navCapacidades = navCapacidades || navegavel(lista, ".item-capacidade", null);
  navCapacidades.garantirTabindex();
}

/* ------------------------------------------------------------------ acesso ao BMC (Redfish) */

/*
 * Credenciais digitadas na hora (CLAUDE.md 6b): só aparece com Redfish confirmado; "Pular"
 * sempre visível; no máximo 3 tentativas; a senha sai do campo assim que é enviada.
 */
let bmcFormAberto = null; // null = decidir pelo estado; true/false = escolha do técnico
let bmcMensagemAnterior = "";

function renderizarBMC(estado) {
  const bmc = estado.bmc;
  const cartao = $("bmc-cartao");
  cartao.hidden = !bmc;
  const faixa = $("diag-bmc");
  if (!bmc) { faixa.hidden = true; return; }
  $("bmc-explicacao").textContent = T("renderizar_bmc.bmc_com_redfish_detectado_em") + (bmc.endereco || "?") + ". " + bmc.explicacao;
  const podeInformar = bmc.pode_tentar && bmc.situacao !== "validado" && !bmc.validando
    && ["pronto", "concluido"].includes(estado.etapa);
  if (bmc.situacao === "validado" || bmc.situacao === "esgotado" || !bmc.pode_tentar) bmcFormAberto = false;
  const aberto = bmcFormAberto === null ? bmc.situacao === "nao_perguntado" : bmcFormAberto;
  const form = $("bmc-form");
  const focoNoForm = form.contains(document.activeElement);
  form.hidden = !(aberto && (bmc.pode_tentar || bmc.validando));
  for (const campo of form.querySelectorAll("input, button")) campo.disabled = bmc.validando;
  $("bmc-validar").lastElementChild.textContent = bmc.validando ? T("renderizar_bmc.validando_no_bmc") : T("renderizar_bmc.validar_credenciais");
  let mensagem = bmc.validando ? T("renderizar_bmc.validando_no_bmc") : bmc.mensagem;
  if (bmc.situacao === "validado") mensagem = "✓ " + bmc.mensagem;
  $("bmc-mensagem").textContent = mensagem;
  $("bmc-mensagem").className = bmc.situacao === "esgotado" ? "aviso aviso-alerta" : "";
  $("bmc-reabrir-area").hidden = !form.hidden || !podeInformar;
  if (mensagem !== bmcMensagemAnterior && !bmc.validando && mensagem) anunciar(mensagem);
  bmcMensagemAnterior = mensagem;
  if (focoNoForm && form.hidden) $(podeInformar ? "bmc-reabrir" : "botao-iniciar").focus();
  // Diagnóstico: quem pulou pode informar depois (refaz só os itens do Redfish).
  faixa.hidden = !(podeInformar && estado.etapa === "concluido" && bmc.situacao !== "validado");
}

/* ------------------------------------------------------------------ acesso ao iDRAC (CLAUDE.md 19.2) */

/*
 * Caixa "Acesso ao iDRAC": por onde o MSscan alcança o iDRAC (B rede, A racadm) e, se não alcança, o que fazer.
 * Aparece sempre que houver iDRAC, inclusive quando ele não responde (o cartão de credenciais só existe com Redfish).
 */
const NIVEL_IDRAC = { ok: ["check-circle", "status-ok"], alerta: ["alert", "status-alerta"], erro: ["x-circle", "status-falha"], info: ["info", ""] };
let idracSituacaoAnterior = "";

function renderizarAcessoIdrac(estado) {
  const acesso = estado.acesso_idrac;
  const cartao = $("idrac-cartao");
  cartao.hidden = !acesso;
  if (!acesso) return;
  const situacao = acesso.verificando ? T("renderizar_acesso_idrac.verificando_o_acesso_ao_idrac") : acesso.titulo;
  $("idrac-situacao").textContent = situacao;
  $("idrac-situacao").className = "idrac-situacao " + ({ rede: "status-ok", racadm: "status-ok" }[acesso.situacao] || "status-alerta");
  const dados = [];
  if (acesso.modo) dados.push(T("renderizar_acesso_idrac.porta") + (acesso.modo === "dedicada" ? "dedicada" : "compartilhada" + (acesso.lom ? T("renderizar_acesso_idrac.lom") + acesso.lom + ")" : "")));
  if (acesso.link !== null && acesso.link !== undefined) dados.push(T("renderizar_acesso_idrac.link") + (acesso.link ? "sim" : T("renderizar_acesso_idrac.nao")));
  if (acesso.ip) dados.push("IP " + acesso.ip + (acesso.mascara ? " / " + acesso.mascara : "") + (acesso.gateway ? ", gateway " + acesso.gateway : ""));
  if (acesso.redes_servidor && acesso.redes_servidor.length) dados.push(T("renderizar_acesso_idrac.servidor") + acesso.redes_servidor.join(", "));
  dados.push(T("renderizar_acesso_idrac.racadm") + (acesso.racadm_texto || acesso.racadm || "?"));
  $("idrac-dados").textContent = dados.join(" · ");
  $("idrac-mensagens").replaceChildren(...(acesso.mensagens || []).map((m) => {
    const [nome, classe] = NIVEL_IDRAC[m.nivel] || NIVEL_IDRAC.info;
    return el("li", { classe: "idrac-mensagem" }, icone(nome, classe), el("span", { texto: m.texto }));
  }));
  const botoes = acesso.botoes || [];
  $("idrac-tentar").hidden = !botoes.includes("tentar");
  $("idrac-tentar").disabled = !!acesso.verificando || !["pronto", "concluido"].includes(estado.etapa);
  $("idrac-tentar").lastElementChild.textContent = acesso.verificando ? T("renderizar_acesso_idrac.verificando") : T("renderizar_acesso_idrac.tentar_de_novo");
  $("idrac-credenciais").hidden = !botoes.includes("credenciais");
  $("idrac-ip-fixo").hidden = !botoes.includes("ip_fixo");
  if (situacao !== idracSituacaoAnterior && !acesso.verificando) anunciar(T("renderizar_acesso_idrac.acesso_ao_idrac") + situacao);
  idracSituacaoAnterior = situacao;
}

/* 21.5: atalho da caixa "Acesso ao iDRAC" para a ação avulsa idrac.ip_fixo (com prévia e CORRIGIR, nunca automática). */
function abrirIpFixo() {
  ir("atualizacoes");
  window.setTimeout(() => {
    const opcao = document.querySelector('input[name="acoes-escolha"][value="idrac.ip_fixo"]');
    if (!opcao) { anunciar(T("abrir_ip_fixo.as_acoes_corretivas_ficam_disponiveis")); return; }
    opcao.checked = true;
    aoEscolherAcao();
    $("acoes-cartao").scrollIntoView({ block: "start" });
    $("acoes-ip-ip").focus();
  }, 50);
}

async function tentarAcessoIdrac() {
  try { await api("idrac/tentar", "POST"); anunciar(T("tentar_acesso_idrac.verificando_de_novo_o_acesso")); }
  catch (e) { anunciar(T("tentar_acesso_idrac.nao_foi_possivel_verificar_agora", e.message)); }
  atualizar();
}

function abrirFormBMC() {
  bmcFormAberto = true;
  if (rotaAtual !== "inicio") ir("inicio");
  if (ESTADO) renderizarBMC(ESTADO);
  window.setTimeout(() => { if (!$("bmc-form").hidden) $("bmc-usuario").focus(); }, 0);
}

async function enviarCredenciais() {
  const usuario = $("bmc-usuario").value.trim();
  const senhaCampo = $("bmc-senha");
  if (!usuario) { pularBMC(); return; }
  const corpo = { usuario: usuario, senha: senhaCampo.value };
  senhaCampo.value = ""; // a senha não fica na tela
  try {
    await api("bmc/credenciais", "POST", corpo);
  } catch (e) { anunciar(T("enviar_credenciais.nao_foi_possivel_validar_agora", e.message)); }
  corpo.senha = "";
  atualizar();
}

async function pularBMC() {
  $("bmc-senha").value = "";
  bmcFormAberto = false;
  try { await api("bmc/pular", "POST"); } catch (e) { /* segue: pular nunca bloqueia */ }
  anunciar(T("pular_bmc.credenciais_do_bmc_puladas_o"));
  atualizar();
  window.setTimeout(() => $("botao-iniciar").focus(), 0);
}

/* ------------------------------------------------------------------ 5. análise por IA */

/* ------------------------------------------------------------------ IA: indicador e "Configurar IA" (CLAUDE.md 10c) */

const INDICADOR_IA = {
  disponivel: ["OK", "check-circle"], sem_chave: ["NA", "key"], sem_acesso: ["FALHA", "x-circle"],
  limite: ["ALERTA", "alert"], indisponivel: ["NA", "skip"],
};
const SITUACAO_CHAVE = {
  ok: ["OK", "check-circle", T("situacao_chave.chave_testada")], sem_acesso: ["FALHA", "x-circle", T("situacao_chave.sem_acesso")],
  limite: ["ALERTA", "alert", T("situacao_chave.limite_atingido")], erro: ["ALERTA", "alert", T("situacao_chave.erro_no_ultimo_uso")],
  sem_rede: ["NA", "globe", T("situacao_chave.sem_rede_no_ultimo_teste")],
};

function etiquetaLivre(classe, nomeIcone, texto) {
  return el("span", { classe: "etiqueta st-" + classe }, icone(nomeIcone), texto);
}

function renderizarIndicadorIA(indicadorIA, alvoEtiqueta, alvoDetalhe) {
  const ind = indicadorIA || { situacao: "indisponivel", texto: T("renderizar_indicador_ia.ia_indisponivel"), detalhe: "" };
  const [classe, nomeIcone] = INDICADOR_IA[ind.situacao] || INDICADOR_IA.indisponivel;
  const chave = ind.situacao + "|" + ind.texto;
  if (alvoEtiqueta.dataset.chave !== chave) {
    alvoEtiqueta.dataset.chave = chave;
    alvoEtiqueta.replaceChildren(etiquetaLivre(classe, nomeIcone, ind.texto));
  }
  if (alvoDetalhe.textContent !== (ind.detalhe || "")) alvoDetalhe.textContent = ind.detalhe || "";
}

let configIA = null;
let iaChaveAberta = "";
const RESERVA_SUGERIDA = "groq";

async function carregarConfigIA() {
  try { configIA = await api("ia/config"); } catch (e) { $("ia-config-rodape").textContent = T("carregar_config_ia.nao_foi_possivel_ler_a", e.message); return; }
  const cfg = configIA;
  $("ia-config-local").hidden = !cfg.aviso_local && !cfg.formato_antigo;
  $("ia-config-local").textContent = [cfg.aviso_local,
    cfg.formato_antigo ? T("carregar_config_ia.o_config_ia_conf_esta") : ""].filter(Boolean).join(" ");
  for (const [id, papel] of [["ia-principal", "principal"], ["ia-reserva", "reserva"]]) {
    const select = $(id);
    const grupos = [["gratuita", T("carregar_config_ia.gratuitas")], ["paga", T("carregar_config_ia.pagas")]].map(([cat, rotulo]) => el("optgroup", { label: rotulo },
      cfg.opcoes.filter((o) => o.categoria === cat).map((o) => el("option", { value: o.id, texto: o.nome
        + (papel === "reserva" && o.id === RESERVA_SUGERIDA ? T("carregar_config_ia.sugerida_rapida") : "") + (o.tem_chave ? "" : T("carregar_config_ia.sem_chave")) }))));
    select.replaceChildren(...(papel === "reserva" ? [el("option", { value: "", texto: T("carregar_config_ia.nenhuma") })] : []), ...grupos);
    // Sem reserva escolhida: o Groq vem sugerido (CLAUDE.md 15.1; só vale depois de "Salvar escolha").
    select.value = cfg[papel] || (papel === "reserva" && cfg.principal !== RESERVA_SUGERIDA ? RESERVA_SUGERIDA : "");
  }
  $("ia-salvar-escolha").disabled = !cfg.pode_salvar;
  $("ia-salvar-chave").disabled = !cfg.pode_salvar;
  for (const [id, cat] of [["ia-gratuitas", "gratuita"], ["ia-pagas", "paga"]]) {
    $(id).replaceChildren(...cfg.opcoes.filter((o) => o.categoria === cat).map(itemIA));
  }
  $("ia-config-rodape").textContent = T("carregar_config_ia.catalogo_de_ias") + cfg.catalogo + (cfg.arquivo ? T("carregar_config_ia.configuracao") + cfg.arquivo : "")
    + (cfg.avisos && cfg.avisos.length ? T("carregar_config_ia.avisos") + cfg.avisos.join("; ") : "");
}

function itemIA(o) {
  const situacao = SITUACAO_CHAVE[o.situacao];
  const cabeca = el("div", { classe: "item-ia-cabeca" },
    el("strong", { texto: o.nome }),
    etiquetaLivre(o.categoria === "gratuita" ? "INFO" : "NA", o.categoria === "gratuita" ? "check" : "law", o.categoria === "gratuita" ? T("item_ia.gratuita") : T("item_ia.paga")),
    o.papel ? etiquetaLivre("INFO", "light-bulb", o.papel === "principal" ? T("item_ia.principal") : T("item_ia.reserva")) : null,
    etiquetaLivre(o.tem_chave ? "OK" : "NA", "key", o.tem_chave ? T("item_ia.chave") + o.chave : T("item_ia.sem_chave")),
    situacao ? etiquetaLivre(situacao[0], situacao[1], situacao[2]) : null);
  const corpo = el("div", { classe: "item-ia-corpo" },
    el("span", { texto: T("item_ia.modelo_padrao") + o.modelo_padrao + T("item_ia.verificado_em") + o.verificado_em }),
    el("span", { texto: T("item_ia.limites") + o.limites }),
    el("span", { texto: T("item_ia.privacidade") + o.privacidade }),
    ...(o.avisos || []).map((a) => el("span", { classe: "aviso-ia" }, icone("alert"), a.texto)));
  const botao = el("button", { type: "button", classe: "botao botao-secundario botao-pequeno item-ia-acao", "data-ia": o.id,
    "aria-label": T("item_ia.informar_chave_de") + o.nome, onclick: () => abrirChaveIA(o.id) }, icone("key"), o.tem_chave ? T("item_ia.trocar_chave") : T("item_ia.informar_chave"));
  const acoes = el("div", { classe: "acoes item-ia-acao" }, botao);
  if (o.tem_chave) {
    // "Remover chave" (17.5): confirmação na própria linha, com "Cancelar" em foco.
    const remover = el("button", { type: "button", classe: "botao botao-perigo botao-pequeno", "aria-label": T("item_ia.remover_a_chave_de") + o.nome,
      onclick: () => {
        const cancelar = el("button", { type: "button", classe: "botao botao-secundario botao-pequeno", onclick: () => { acoes.replaceChildren(botao, remover); remover.focus(); } }, T("item_ia.cancelar"));
        const confirmar = el("button", { type: "button", classe: "botao botao-perigo botao-pequeno", onclick: async () => {
          try { const r = await api("ia/remover_chave", "POST", { id: o.id, confirmar: true }); anunciar(r.mensagem); $("ia-config-mensagem").textContent = r.mensagem; }
          catch (e) { anunciar(e.message); $("ia-config-mensagem").textContent = e.message; }
          carregarConfigIA();
        } }, icone("trash"), T("item_ia.confirmar_apagar_a_chave_do"));
        acoes.replaceChildren(el("span", { classe: "pequeno", texto: T("item_ia.remover_a_chave_de") + o.nome + T("item_ia.do_pendrive") }), cancelar, " ", confirmar);
        cancelar.focus();
      } }, icone("trash"), T("item_ia.remover_chave"));
    acoes.append(remover);
  }
  return el("li", { classe: "item-ia" }, cabeca, acoes, corpo);
}

function desenharQR(matriz, svg) {
  const margem = 4;
  const n = matriz.length;
  const total = n + 2 * margem;
  svg.setAttribute("viewBox", "0 0 " + total + " " + total);
  const ns = "http://www.w3.org/2000/svg";
  const fundo = document.createElementNS(ns, "rect");
  fundo.setAttribute("class", "qr-claro");
  fundo.setAttribute("width", String(total));
  fundo.setAttribute("height", String(total));
  let d = "";
  matriz.forEach((linha, y) => {
    let x = 0;
    while (x < n) {
      if (linha[x] !== "1") { x += 1; continue; }
      const inicio = x;
      while (x < n && linha[x] === "1") x += 1;
      d += "M" + (inicio + margem) + " " + (y + margem) + "h" + (x - inicio) + "v1h-" + (x - inicio) + "z";
    }
  });
  const modulos = document.createElementNS(ns, "path");
  modulos.setAttribute("class", "qr-escuro");
  modulos.setAttribute("d", d);
  svg.replaceChildren(fundo, modulos);
}

async function abrirChaveIA(id) {
  const opcao = configIA && configIA.opcoes.find((o) => o.id === id);
  if (!opcao) return;
  iaChaveAberta = id;
  $("ia-chave-painel").hidden = false;
  $("ia-chave-titulo").textContent = T("abrir_chave_ia.chave") + opcao.nome;
  $("ia-chave").value = "";
  $("ia-chave").setAttribute("aria-describedby", "ia-chave-atual");
  $("ia-chave-atual").textContent = opcao.tem_chave ? T("abrir_chave_ia.chave_atual") + opcao.chave + T("abrir_chave_ia.so_os_4_ultimos_caracteres") : T("abrir_chave_ia.nenhuma_chave_salva_para_esta");
  $("ia-chave-mensagem").textContent = "";
  if (celularAtivo && celularAtivo !== id) pararCelular();
  $("ia-qr-link").textContent = opcao.link_chave;
  $("ia-chave-titulo").focus();
  try {
    const qr = await api("ia/qr/" + encodeURIComponent(id));
    if (iaChaveAberta === id && qr.matriz.length) desenharQR(qr.matriz, $("ia-qr"));
  } catch (e) { $("ia-qr-link").textContent = opcao.link_chave + T("abrir_chave_ia.qr_indisponivel", e.message) + ")"; }
}

function fecharChaveIA() {
  const id = iaChaveAberta;
  iaChaveAberta = "";
  pararCelular();
  $("ia-chave").value = "";
  $("ia-chave-painel").hidden = true;
  const botao = document.querySelector('[data-ia="' + id + '"]');
  if (botao) botao.focus();
}

async function testarChaveIA() {
  if (!iaChaveAberta) return;
  const mensagem = $("ia-chave-mensagem");
  $("ia-testar").disabled = true;
  mensagem.className = "";
  mensagem.textContent = T("testar_chave_ia.testando_chamada_minima_sem_nenhum");
  try {
    const resultado = await api("ia/testar", "POST", { id: iaChaveAberta, chave: $("ia-chave").value });
    mensagem.textContent = resultado.mensagem;
    mensagem.className = resultado.situacao === "ok" ? "aviso aviso-ok" : "aviso aviso-alerta";
  } catch (e) { mensagem.textContent = T("testar_chave_ia.nao_foi_possivel_testar", e.message); mensagem.className = "aviso aviso-alerta"; }
  $("ia-testar").disabled = false;
  await carregarConfigIA();
}

async function salvarChaveIA() {
  const valor = $("ia-chave").value;
  const mensagem = $("ia-chave-mensagem");
  if (!iaChaveAberta || !valor.trim()) { mensagem.textContent = T("salvar_chave_ia.cole_ou_digite_a_chave"); mensagem.className = "aviso aviso-alerta"; return; }
  const chaves = {};
  chaves[iaChaveAberta] = valor;
  const escolha = configIA && !configIA.principal ? { principal: iaChaveAberta } : {};
  try {
    const resultado = await api("ia/salvar", "POST", Object.assign({ chaves }, escolha));
    $("ia-chave").value = "";
    mensagem.textContent = resultado.mensagem;
    mensagem.className = "aviso aviso-ok";
  } catch (e) { mensagem.textContent = e.message; mensagem.className = "aviso aviso-alerta"; }
  await carregarConfigIA();
}

async function salvarEscolhaIA() {
  const mensagem = $("ia-escolha-mensagem");
  try {
    const resultado = await api("ia/salvar", "POST", { principal: $("ia-principal").value, reserva: $("ia-reserva").value });
    mensagem.textContent = resultado.mensagem;
    mensagem.className = "aviso aviso-ok";
  } catch (e) { mensagem.textContent = e.message; mensagem.className = "aviso aviso-alerta"; }
  await carregarConfigIA();
}

/* Mostra a análise ou a configuração da IA. Devolve true quando cuidou do foco. */
function mostrarConfigIA(configurar, mudou) {
  $("ia-analise-area").hidden = configurar;
  $("ia-config-area").hidden = !configurar;
  if (!mudou) return false;
  if (configurar) {
    carregarConfigIA();
    $("ia-config-titulo").focus();
    return true;
  }
  if (!$("ia-chave-painel").hidden) { iaChaveAberta = ""; $("ia-chave").value = ""; $("ia-chave-painel").hidden = true; }
  pararCelular();
  return false;
}

/*
 * Opcional, padrão NÃO (CLAUDE.md 10): "Enviar" só com "Sim" marcado; nenhum botão com foco inicial
 * nem acionado por Enter. "Ver o que será enviado" mostra os dados já sem identificadores.
 */
const SEVERIDADE_IA = { critica: ["FALHA", T("severidade_ia.critica")], alta: ["FALHA", T("severidade_ia.alta")], media: ["ALERTA", T("severidade_ia.media")], baixa: ["NAO_VERIFICADO", T("severidade_ia.baixa")] };
let iaDesenhada = "";

let iaSituacaoAnterior = "";

function renderizarIA(estado) {
  const ia = estado.ia || { disponivel: false, motivo: "", situacao: "nao_solicitada" };
  // Análise concluída agora (e não numa visita posterior): a interface vai sozinha para o Chat (CLAUDE.md 10e).
  if (iaSituacaoAnterior === "enviando" && ia.situacao === "concluida" && estado.chat && estado.chat.disponivel) {
    window.setTimeout(() => { ir("chat"); anunciar(T("renderizar_ia.analise_por_ia_concluida_a")); }, 0);
  }
  iaSituacaoAnterior = ia.situacao;
  renderizarIndicadorIA(ia.indicador, $("ia-escolhida-etiqueta"), $("ia-escolhida-detalhe"));
  renderizarIndicadorIA(ia.indicador, $("inicio-ia-etiqueta"), $("inicio-ia-detalhe"));
  $("ia-indisponivel").hidden = ia.disponivel || !ia.motivo;
  $("ia-indisponivel-texto").textContent = frase(ia.motivo);
  $("ia-provedores").textContent = (ia.provedores || []).length
    ? T("renderizar_ia.provedores_na_ordem_de_tentativa") + ia.provedores.map((p) => p.nome + " (" + p.modelo + ")").join(" → ") : "";
  $("ia-aviso-treinamento").hidden = !ia.disponivel || !ia.aviso_treinamento;
  $("ia-aviso-treinamento").textContent = ia.aviso_treinamento_curto || "";
  $("ia-aviso-treinamento-completo").textContent = ia.aviso_treinamento || "";
  const enviando = ia.situacao === "enviando";
  // Falhou: "Tentar novamente" reenvia os mesmos dados, sem refazer o diagnóstico (uma ação principal por vez).
  const falhou = ia.situacao === "falhou" && ia.disponivel;
  $("ia-tentar").hidden = !falhou;
  $("ia-solicitar").hidden = falhou;
  $("ia-dica-reserva").hidden = !(ia.sem_reserva && ia.disponivel && ia.situacao !== "concluida");
  // Concluída: a análise vale para este diagnóstico; só volta a ser oferecida se ele for refeito.
  $("ia-sim").disabled = !ia.disponivel || enviando || ia.situacao === "concluida";
  if ($("ia-sim").disabled && $("ia-sim").checked) $("ia-nao").checked = true;
  $("ia-ver").disabled = !ia.disponivel || ia.situacao === "concluida";
  $("ia-solicitar").disabled = !ia.disponivel || enviando || !$("ia-sim").checked;
  $("ia-solicitar").lastElementChild.textContent = enviando ? T("renderizar_ia.enviando") : T("renderizar_ia.enviar_para_analise");
  $("ia-parcial").hidden = !(enviando && ia.parcial);
  if (enviando && ia.parcial && $("ia-parcial").textContent !== ia.parcial) {
    $("ia-parcial").textContent = ia.parcial;
    $("ia-parcial").scrollTop = $("ia-parcial").scrollHeight;
  }
  $("ia-cancelar").hidden = ia.situacao !== "enviando";
  if ($("ia-mensagem").textContent !== (ia.mensagem || "")) {
    $("ia-mensagem").textContent = ia.mensagem || "";
    $("ia-mensagem").className = ia.situacao === "falhou" ? "aviso aviso-alerta" : "";
  }
  const chave = JSON.stringify(ia.analise);
  if (chave === iaDesenhada) return;
  iaDesenhada = chave;
  const analise = ia.analise;
  $("ia-resultado").hidden = !analise;
  if (!analise) return;
  $("ia-aviso").textContent = analise.aviso + " " + (analise.origem || "") + ".";
  $("ia-tempos").textContent = T("renderizar_ia.tempo_de_resposta") + (analise.tentativas || []).map((t) =>
    (t.titulo || t.nome) + " " + segundos(t.segundos) + (t.resultado === "ok" ? "" : " (falhou)")).join(" · ");
  $("ia-resumo").textContent = analise.resumo || "—";
  $("ia-limitacoes").replaceChildren(analise.limitacoes && analise.limitacoes.length
    ? el("ul", {}, ...analise.limitacoes.map((l) => el("li", { texto: l })))
    : el("span", { classe: "texto-sec", texto: T("renderizar_ia.nenhuma_apontada") }));
  const linhas = (analise.problemas || []).map((p) => {
    const [status, rotulo] = SEVERIDADE_IA[p.severidade] || ["NAO_VERIFICADO", p.severidade];
    return el("tr", {},
      el("td", {}, el("strong", { texto: p.titulo }), p.confirmado ? null : el("div", { classe: "ia-nao-confirmada", texto: p.aviso }),
        el("div", { classe: "texto-sec", texto: p.explicacao })),
      el("td", {}, etiqueta(status, rotulo)),
      el("td", {}, el("ul", { classe: "ia-evidencias" }, ...(p.evidencias || []).map((e) =>
        el("li", { texto: (e.confirmada ? "✓ " : "? ") + e.componente + ": " + e.trecho })))),
      el("td", {}, planoIA(p)));
  });
  $("ia-problemas").replaceChildren(...(linhas.length ? linhas
    : [el("tr", {}, el("td", { colspan: "4", classe: "texto-sec", texto: T("renderizar_ia.nenhum_problema_apontado_pela_ia") }))]));
}

const SIM_NAO = (v) => v === true ? T("sim_nao.sim") : v === false ? T("sim_nao.nao") : T("sim_nao.nao_informado");

function planoIA(p) {
  const plano = p.plano || {};
  return [
    el("div", { classe: "pequeno", texto: T("plano_ia.prioridade") + (plano.prioridade === "media" ? T("plano_ia.prioridade_media") : plano.prioridade || "?") + T("plano_ia.intervencao_fisica")
      + SIM_NAO(plano.intervencao_fisica) + T("plano_ia.troca_de_peca") + SIM_NAO(plano.troca_de_peca) }),
    (plano.passos || []).length ? el("ol", { classe: "ia-evidencias" }, ...plano.passos.map((s) => el("li", { texto: s }))) : el("div", { texto: p.acao_recomendada || "" }),
    plano.acao ? el("div", { classe: "pequeno", texto: T("plano_ia.acao_do_catalogo") + plano.acao.id + " — " + plano.acao.titulo
      + T("plano_ia.disponivel_na_aba_atualizacoes_acoes") }) : null,
    p.aviso_acao ? el("div", { classe: "ia-nao-confirmada", texto: p.aviso_acao }) : null,
  ];
}

async function verPreviaIA() {
  try {
    const previa = await api("ia/previa");
    const removidos = Object.entries(previa.removidos || {}).map(([k, v]) => v + " " + k).join(", ");
    $("ia-removidos").textContent = T("ver_previa_ia.identificadores_removidos") + (removidos || "nenhum encontrado") + T("ver_previa_ia.e_exatamente_isto_que_sera");
    $("ia-previa").textContent = JSON.stringify(previa.dados, null, 1);
    $("ia-previa-area").hidden = false;
    $("ia-previa").focus();
  } catch (e) { anunciar(T("ver_previa_ia.nao_foi_possivel_montar_a", e.message)); }
}

async function solicitarIA() {
  if (!$("ia-sim").checked) return;
  $("ia-solicitar").disabled = true;
  $("ia-previa-area").hidden = true;
  $("ia-mensagem").setAttribute("tabindex", "-1");
  $("ia-mensagem").focus();
  try { await api("ia/solicitar", "POST"); anunciar(T("solicitar_ia.enviado_para_analise_por_ia")); }
  catch (e) { anunciar(T("solicitar_ia.nao_foi_possivel_enviar", e.message)); }
  atualizar();
}

/* ------------------------------------------------------------------ chave pelo celular (CLAUDE.md 10d) */

/*
 * A porta só fica aberta enquanto o painel da chave está aberto: a tela manda um sinal de vida a cada
 * atualização (sem sinal por 20 s, o MSscan fecha). Nada é salvo antes de o técnico aceitar aqui;
 * depois de aceitar, o "Testar" roda sozinho no servidor.
 */
let celularAtivo = "";
let celularDesenhado = "";
let celularSituacaoAnterior = "";

const CELULAR_TEXTO = {
  aguardando: T("celular_texto.aguardando_o_celular"),
  recebida: "",
  aceita: T("celular_texto.chave_aceita_e_salva"),
  recusada: T("celular_texto.chave_recusada_nada_foi_salvo"),
  expirada: T("celular_texto.passaram_5_minutos_sem_envio"),
  encerrada: T("celular_texto.pareamento_encerrado"),
  tentativas: T("celular_texto.tentativas_invalidas_demais_pareamento_encerrado"),
  erro: "",
};

async function iniciarCelular() {
  if (!iaChaveAberta) return;
  const id = iaChaveAberta;
  $("ia-celular-iniciar").disabled = true;
  $("ia-celular-area").hidden = false;
  $("ia-celular-decisao").hidden = true;
  $("ia-celular-situacao").textContent = T("iniciar_celular.abrindo_o_pareamento");
  $("ia-celular-detalhe").textContent = "";
  try {
    await api("ia/celular/iniciar", "POST", { id });
    celularAtivo = id;
    celularDesenhado = "";
    await atualizar();
  } catch (e) {
    $("ia-celular-situacao").textContent = e.message;
    $("ia-celular-situacao").className = "aviso aviso-alerta";
    $("ia-celular-iniciar").disabled = false;
  }
}

function pararCelular() {
  if (!celularAtivo) return;
  celularAtivo = "";
  $("ia-celular-area").hidden = true;
  $("ia-celular-iniciar").disabled = false;
  api("ia/celular/parar", "POST").catch(() => {});
}

function renderizarCelular(estado) {
  const c = estado.ia_celular;
  if (!celularAtivo || !c) return;
  const aberto = rotaAtual === "ia/configurar" && iaChaveAberta === celularAtivo;
  if (!aberto) { pararCelular(); return; }
  if (c.situacao === "aguardando") api("ia/celular/manter", "POST").catch(() => {});
  if (c.url && celularDesenhado !== c.url && c.qr.length) { desenharQR(c.qr, $("ia-celular-qr")); celularDesenhado = c.url; }
  $("ia-celular-qr").closest("figure").hidden = c.situacao !== "aguardando";
  $("ia-celular-codigo").textContent = c.codigo.slice(0, 3) + " " + c.codigo.slice(3);
  const situacao = $("ia-celular-situacao");
  const texto = c.situacao === "recebida" ? T("renderizar_celular.chave_recebida_do_celular_ip") + c.ip_celular + T("renderizar_celular.aceitar")
    : c.situacao === "erro" ? c.mensagem : (CELULAR_TEXTO[c.situacao] || c.mensagem);
  if (situacao.textContent !== texto) {
    situacao.textContent = texto;
    situacao.className = ["erro", "tentativas", "expirada"].includes(c.situacao) ? "aviso aviso-alerta" : "";
  }
  $("ia-celular-detalhe").textContent = c.situacao === "aguardando"
    ? T("renderizar_celular.endereco_https") + c.ip + ":" + c.porta + T("renderizar_celular.fecha_em") + Math.floor(c.restante_s / 60) + " min " + String(c.restante_s % 60).padStart(2, "0")
      + T("renderizar_celular.s_certificado") + c.impressao + T("renderizar_celular.o_celular_avisa_que_nao")
    : "";
  $("ia-celular-decisao").hidden = c.situacao !== "recebida";
  $("ia-celular-parar").hidden = !["aguardando", "recebida"].includes(c.situacao);
  $("ia-celular-iniciar").disabled = ["aguardando", "recebida"].includes(c.situacao);
  if (c.situacao !== celularSituacaoAnterior) {
    celularSituacaoAnterior = c.situacao;
    if (c.situacao === "recebida") { anunciar(texto); situacao.focus(); }
    else if (texto) anunciar(texto);
  }
}

async function aceitarCelular() {
  $("ia-celular-decisao").hidden = true;
  const mensagem = $("ia-chave-mensagem");
  try {
    const r = await api("ia/celular/aceitar", "POST", {});
    const teste = r.teste || {};
    mensagem.textContent = r.mensagem + (teste.mensagem ? T("aceitar_celular.teste") + teste.mensagem : "");
    mensagem.className = teste.situacao === "ok" ? "aviso aviso-ok" : "aviso aviso-alerta";
  } catch (e) { mensagem.textContent = e.message; mensagem.className = "aviso aviso-alerta"; }
  mensagem.setAttribute("tabindex", "-1");
  mensagem.focus();
  await carregarConfigIA();
  atualizar();
}

async function recusarCelular() {
  try { await api("ia/celular/recusar", "POST", {}); } catch (e) { /* já encerrado */ }
  $("ia-celular-iniciar").focus();
  atualizar();
}

/* ------------------------------------------------------------------ 5b. chat com a IA (CLAUDE.md 10e) */

let chatRevisao = -1;
let chatQuantidade = 0;

function rolarChat() {
  const lista = $("chat-mensagens");
  lista.scrollTop = lista.scrollHeight;
}

/* Texto com os marcadores [E1]: confirmados em verde; não confirmados marcados (ícone + texto + cor). */
function textoComMarcas(m) {
  const confirmadas = {};
  for (const e of m.evidencias || []) confirmadas[e.id] = e.confirmada;
  const partes = [];
  let inicio = 0;
  const texto = m.texto || "";
  for (const achado of texto.matchAll(/\[(E\d{1,3})\]/g)) {
    partes.push(texto.slice(inicio, achado.index));
    const ok = confirmadas[achado[1]];
    partes.push(ok ? el("span", { classe: "ev ev-ok", texto: achado[0] })
      : el("span", { classe: "ev ev-falha", title: T("texto_com_marcas.evidencia_que_nao_existe_nos") }, icone("alert"), achado[1] + T("texto_com_marcas.nao_confirmada")));
    inicio = achado.index + achado[0].length;
  }
  partes.push(texto.slice(inicio));
  return el("div", { classe: "chat-texto" }, ...partes);
}

/* Modo detalhado (CLAUDE.md 15.1): tempo de cada resposta da IA. Lembrado na sessão do navegador. */
let modoDetalhado = false;
try { modoDetalhado = sessionStorage.getItem("msscan-detalhado") === "1"; } catch (e) { /* sem armazenamento */ }

function segundos(valor) {
  return typeof valor === "number" ? valor.toLocaleString("pt-BR", { maximumFractionDigits: 1 }) + " s" : "?";
}

function aplicarModoDetalhado() {
  document.body.classList.toggle("detalhado", modoDetalhado);
  for (const caixa of document.querySelectorAll(".modo-detalhado")) caixa.checked = modoDetalhado;
  $("ia-tempos").hidden = !modoDetalhado;
}

function autorChat(m) {
  if (m.papel === "tecnico") return T("autor_chat.voce");
  if (m.papel === "sistema") return "MSscan";
  return [T("autor_chat.ia"), m.provedor_titulo, m.modelo, m.papel_ia].filter(Boolean).join(" · ");
}

function mensagemChat(m) {
  const extras = [
    ...(m.evidencias || []).map((e) => el("li", { classe: e.confirmada ? "" : "ia-nao-confirmada" },
      "[" + e.id + "] " + (e.componente || e.trecho ? (e.componente || "?") + ": “" + e.trecho + "”" : T("mensagem_chat.citada_pela_ia_mas_nao"))
      + " — " + (e.confirmada ? T("mensagem_chat.confirmada") : T("mensagem_chat.nao_confirmada_pelos_dados")))),
    ...(m.acoes || []).map((a) => el("li", {}, T("mensagem_chat.acao_do_catalogo_recomendada") + a.id + " — " + a.titulo
      + T("mensagem_chat.em_atualizacoes_acoes_corretivas_nada"))),
    ...(m.avisos || []).map((a) => el("li", { classe: "ia-nao-confirmada" }, a)),
  ];
  return el("li", { classe: "chat-msg chat-" + m.papel },
    el("div", { classe: "chat-autor pequeno" }, el("strong", { texto: autorChat(m) }), " ", el("span", { classe: "texto-sec", texto: (m.quando || "").slice(11, 16) }),
      typeof m.segundos === "number" ? el("span", { classe: "so-detalhado texto-sec", texto: T("mensagem_chat.respondeu_em") + segundos(m.segundos) }) : null),
    textoComMarcas(m),
    extras.length ? el("ul", { classe: "chat-extras pequeno" }, ...extras) : null);
}

async function carregarChat() {
  try {
    const dados = await api("chat");
    $("chat-mensagens").replaceChildren(...dados.mensagens.map(mensagemChat));
    const novas = dados.mensagens.slice(chatQuantidade);
    chatQuantidade = dados.mensagens.length;
    const ultimaIA = novas.filter((m) => m.papel === "ia").pop();
    if (ultimaIA && rotaAtual === "chat") anunciar(T("carregar_chat.resposta_da_ia") + ultimaIA.texto.slice(0, 300));
    rolarChat();
  } catch (e) { /* tenta de novo na próxima atualização */ }
}

function renderizarChat(estado) {
  const chat = estado.chat || { disponivel: false, motivo: "", situacao: "inativo", sugestoes: [] };
  $("chat-indisponivel").hidden = chat.disponivel;
  $("chat-indisponivel-texto").textContent = frase(chat.motivo);
  $("chat-area").hidden = !chat.disponivel;
  const ocupado = ["digitando", "aguardando_rede"].includes(chat.situacao);
  $("chat-digitando").textContent = chat.situacao === "digitando" ? (chat.mensagem || T("renderizar_chat.ia_digitando"))
    : chat.situacao === "aguardando_rede" ? chat.mensagem : chat.situacao === "falhou" ? chat.mensagem : "";
  $("chat-digitando").className = "chat-digitando pequeno " + (chat.situacao === "falhou" ? "aviso aviso-alerta" : "texto-sec");
  $("chat-cancelar").hidden = !["digitando", "aguardando_rede"].includes(chat.situacao);
  $("chat-tentar-area").hidden = chat.situacao !== "falhou";
  const escrevendo = ocupado && !!chat.parcial;
  $("chat-parcial").hidden = !escrevendo;
  if (escrevendo && $("chat-parcial-texto").textContent !== chat.parcial) { $("chat-parcial-texto").textContent = chat.parcial; rolarChat(); }
  $("chat-dica-reserva").hidden = !chat.sem_reserva;
  $("chat-enviar").disabled = !chat.disponivel || ocupado || !$("chat-texto").value.trim();
  const sugestoes = $("chat-sugestoes");
  if (!sugestoes.childElementCount && chat.sugestoes.length) {
    sugestoes.replaceChildren(...chat.sugestoes.map((t) => el("button", { type: "button", classe: "botao botao-secundario botao-pequeno",
      onclick: () => { $("chat-texto").value = t; enviarChat(); } }, t)));
  }
  for (const b of sugestoes.querySelectorAll("button")) b.disabled = !chat.disponivel || ocupado;
  const contador = $("contador-chat");
  contador.hidden = !chat.quantidade;
  contador.textContent = chat.quantidade ? String(chat.quantidade) : "";
  contador.setAttribute("aria-label", chat.quantidade + " mensagens");
  if (chat.revisao !== chatRevisao) { chatRevisao = chat.revisao; if (chat.disponivel || chatQuantidade) carregarChat(); }
}

async function tentarIA() {
  $("ia-tentar").disabled = true;
  try { await api("ia/solicitar", "POST"); anunciar(T("tentar_ia.tentando_de_novo_a_analise")); }
  catch (e) { anunciar(T("tentar_ia.nao_foi_possivel_enviar", e.message)); }
  $("ia-tentar").disabled = false;
  atualizar();
}

async function tentarChat() {
  try { await api("chat/repetir", "POST"); anunciar(T("tentar_chat.reenviando_a_pergunta_ia_digitando")); }
  catch (e) { anunciar(T("tentar_chat.nao_foi_possivel_reenviar", e.message)); }
  atualizar();
}

async function enviarChat() {
  const campo = $("chat-texto");
  const texto = campo.value;
  if (!texto.trim()) return;
  try {
    await api("chat/enviar", "POST", { texto });
    campo.value = "";
    anunciar(T("enviar_chat.mensagem_enviada_ia_digitando"));
  } catch (e) { anunciar(T("enviar_chat.nao_foi_possivel_enviar", e.message)); $("chat-digitando").textContent = e.message; }
  atualizar();
}

/* ------------------------------------------------------------------ 6. atualizações (informativo) */

/* Três estados (CLAUDE.md 15.2): "não sei" nunca aparece como "não é necessário". */
const ESTADO_ATU = { disponivel: "ATUALIZAVEL", nao_necessaria: "OK", indeterminado: "NAO_VERIFICADO" };
const SITUACAO_ATU = { atualizacao: "disponivel", igual: "nao_necessaria", instalada_mais_nova: "nao_necessaria", incerta: "indeterminado", sem_pacote: "indeterminado" };
let atuDesenhada = "";

function renderizarAtualizacoes(estado) {
  const atu = estado.atualizacoes || { disponivel: false, motivo: "", situacao: "nao_solicitadas" };
  $("atu-indisponivel").hidden = atu.disponivel || !atu.motivo;
  $("atu-indisponivel-texto").textContent = frase(atu.motivo);
  const ocupado = atu.situacao === "verificando";
  $("atu-sim").disabled = !atu.disponivel || ocupado;
  if ($("atu-sim").disabled && $("atu-sim").checked) $("atu-nao").checked = true;
  $("atu-verificar-botao").disabled = !atu.disponivel || ocupado || !$("atu-sim").checked;
  $("atu-mensagem").textContent = atu.mensagem || "";
  // Igual ao resumo da tabela: continua anunciado ao leitor de tela, mas não aparece duas vezes na tela.
  $("atu-mensagem").classList.toggle("visualmente-oculto", !!(atu.secao && atu.mensagem === atu.secao.mensagem));
  // Atalho para a tabela: com a verificação feita, a pergunta recolhe sozinha (seção secundária, data-longa) e a tabela
  // sobe para o topo; se o técnico abrir o cartão, a escolha dele vale na sessão. Diagnóstico refeito: volta a abrir.
  const pergunta = $("atu-pergunta");
  if (pergunta.hasAttribute("data-longa") !== Boolean(atu.secao) && pergunta.querySelector(":scope > .botao-recolher")) {
    pergunta.toggleAttribute("data-longa", Boolean(atu.secao));
    if (atu.secao) pergunta.dataset.resumo = T("aplicar_recolhido.verificacao_concluida"); else delete pergunta.dataset.resumo;
    aplicarRecolhido(pergunta);
    // A mensagem de conclusão (aria-live) fica dentro do cartão recolhido, que não é anunciado: anuncia por fora.
    if (atu.secao && pergunta.classList.contains("recolhido") && atu.mensagem) anunciar(atu.mensagem);
  }
  const chave = JSON.stringify(atu.secao);
  if (chave === atuDesenhada) return;
  atuDesenhada = chave;
  const secao = atu.secao;
  if (!secao) {
    // Diagnóstico refeito ou verificação nova: a lista antiga (e as caixas marcadas nela) não vale mais (bug 3a).
    $("atu-itens").replaceChildren();
    $("atu-checagens").hidden = true;
    atualizarBotaoAplicar();
    return;
  }
  const catalogo = secao.catalogo || {};
  // Modelo que saiu do catálogo da Dell: aviso em destaque, com o caminho (Atualização manual) logo abaixo.
  $("atu-fora-suporte").hidden = secao.situacao !== "fora_de_suporte";
  $("atu-fora-suporte").textContent = secao.situacao === "fora_de_suporte" ? secao.mensagem : "";
  const fora = secao.situacao === "fora_de_suporte";
  $("atu-resumo").textContent = (fora ? "" : secao.mensagem || "") + (catalogo.versao ? T("renderizar_atualizacoes.catalogo_dell") + catalogo.versao + " (" + catalogo.origem + ")." : "")
    + (catalogo.aviso ? T("renderizar_atualizacoes.aviso") + catalogo.aviso : "") + (secao.fonte_inventario ? T("renderizar_atualizacoes.inventario") + secao.fonte_inventario + "." : "");
  const semBloqueioGeral = !(secao.bloqueios || []).length;
  const linhas = (secao.itens || []).map((i) => el("tr", {},
    el("td", {}, el("input", { type: "checkbox", "data-id": i.id, class: "atu-escolha",
      disabled: !(semBloqueioGeral && i.situacao === "atualizacao" && !(i.bloqueios || []).length),
      "aria-label": T("renderizar_atualizacoes.selecionar") + i.componente + (i.situacao === "atualizacao" ? "" : T("renderizar_atualizacoes.sem_atualizacao_oferecida")),
      onchange: aoEscolherAtualizacao })),
    el("td", { texto: i.componente }),
    el("td", { classe: "mono", texto: i.instalada || "?" }),
    el("td", { classe: "mono", texto: i.disponivel || "—" }),
    el("td", {}, etiqueta(ESTADO_ATU[i.estado || SITUACAO_ATU[i.situacao]] || "NAO_VERIFICADO", i.texto_situacao),
      i.motivo ? el("div", { classe: "texto-sec pequeno", texto: T("renderizar_atualizacoes.motivo") + i.motivo }) : null,
      (i.estado || SITUACAO_ATU[i.situacao]) === "indeterminado"
        ? el("button", { type: "button", classe: "botao botao-secundario botao-pequeno", onclick: () => acaoManual("manual/navegador", {}, "") },
          icone("link"), T("renderizar_atualizacoes.abrir_pagina_de_drivers")) : null,
      ...(i.pre_requisitos || []).map((r) => el("div", { classe: r.atendido === false ? "ia-nao-confirmada" : "texto-sec pequeno",
        texto: T("renderizar_atualizacoes.pre_requisito_da_dell") + r.texto })),
      ...(i.bloqueios || []).map((b) => el("div", { classe: "ia-nao-confirmada", texto: T("renderizar_atualizacoes.bloqueio") + b })))));
  $("atu-itens").replaceChildren(...(linhas.length ? linhas
    : [el("tr", {}, el("td", {}, el("input", { type: "checkbox", disabled: true, "aria-label": T("renderizar_atualizacoes.selecionar_nenhum_item") })),
        el("td", { colspan: "4", classe: "texto-sec", texto: (!fora && secao.mensagem) || T("renderizar_atualizacoes.nenhum_item") }))]));
  const blocos = [];
  if ((secao.bloqueios || []).length) blocos.push(el("p", {}, el("strong", { texto: T("renderizar_atualizacoes.bloqueios") })), el("ul", {}, ...secao.bloqueios.map((t) => el("li", { texto: t }))));
  if ((secao.avisos || []).length) blocos.push(el("p", {}, el("strong", { texto: T("renderizar_atualizacoes.avisos") })), el("ul", {}, ...secao.avisos.map((t) => el("li", { texto: t }))));
  $("atu-checagens").hidden = !blocos.length;
  $("atu-checagens-corpo").replaceChildren(...blocos);
  $("atu-redundancia").hidden = !secao.redundancia_perdida;
  atualizarBotaoAplicar();
}

/* Fluxo guiado (pedido de 01/10/2026, 3d): 6 etapas e a faixa "Próximo passo". Só orienta: o servidor confere tudo. */
const PASSOS_ATU = [T("passos_atu.escolher_os_componentes"), T("passos_atu.baixar"), T("passos_atu.verificacao_automatica_hash_modelo_versao"), T("passos_atu.plano_de_atualizacao"),
  T("passos_atu.confirmar_atualizar"), T("passos_atu.instalacao_automatica")];
const FIM_APLICACAO = ["aplicada", "simulada", "aguardando_reinicio", "aguardando_reinicio_manual", "falhou", "bloqueada",
  "bloqueada_trava", "so_informativo", "parado", "possivelmente_agendado"];

function passoAtualizacao(atu, escolhidos) {
  const aplicacao = atu.aplicacao || {};
  if (aplicacao.situacao === "executando") return { n: 6, texto: T("passo_atualizacao.instalando_acompanhe_o_progresso_por") };
  const resultado = aplicacao.resultado || {};
  if (FIM_APLICACAO.includes(aplicacao.situacao)) {
    return { n: 6, texto: aplicacao.mensagem || T("passo_atualizacao.instalacao_terminada_veja_o_resultado") };
  }
  const pacotes = ((atu.manual || {}).pacotes) || [];
  const sha = pacotes.find((p) => p.etapa === "sha");
  if (sha) return { n: 3, texto: T("passo_atualizacao.cole_o_sha_256_da") + sha.nome + T("passo_atualizacao.o_msscan_confere_hash_modelo"), foco: "man-sha-" + sha.id };
  const checando = pacotes.find((p) => ["checagem", "checando"].includes(p.etapa));
  if (checando) return { n: 3, texto: T("passo_atualizacao.conferindo_o_pacote") + checando.nome + "…" };
  const pronto = pacotes.find((p) => p.etapa === "pronto");
  if (pronto) return { n: 4, texto: T("passo_atualizacao.abra_o_plano_de_atualizacao") + pronto.nome + T("passo_atualizacao.o_hardware_e_conferido_na"), foco: "man-instalar-" + pronto.id, botao: T("passo_atualizacao.ver_o_plano_de_atualizacao") };
  const secao = atu.secao;
  if (atu.situacao === "verificando") return { n: 1, texto: atu.mensagem || T("passo_atualizacao.verificando_as_atualizacoes") };
  if (secao && secao.situacao === "fora_de_suporte") return { n: 1, texto: T("passo_atualizacao.nenhuma_atualizacao_disponivel_pelo_catalogo") };
  if (secao && secao.situacao !== "verificada" && secao.mensagem) return { n: 1, texto: secao.mensagem };
  if (!secao || secao.situacao !== "verificada") {
    return { n: 1, texto: atu.disponivel ? T("passo_atualizacao.marque_sim_e_clique_em") : (atu.motivo || T("passo_atualizacao.faca_o_diagnostico_antes")), foco: atu.disponivel ? "atu-sim" : null };
  }
  if (!escolhidos.length) {
    const oferecidos = (secao.itens || []).filter((i) => i.situacao === "atualizacao");
    return oferecidos.length ? { n: 1, texto: T("passo_atualizacao.marque_na_tabela_os_componentes") + oferecidos.length + T("passo_atualizacao.com_atualizacao_disponivel"), foco: "primeira-escolha" }
      : { n: 1, texto: T("passo_atualizacao.nenhuma_atualizacao_disponivel_pelo_catalogo") };
  }
  const preparo = atu.preparo;
  const mesmos = preparo && JSON.stringify(preparo.ids) === JSON.stringify(escolhidos.slice().sort());
  if (!mesmos) return { n: 2, texto: T("passo_atualizacao.baixe_os_escolhidos_o_msscan"), botao: T("passo_atualizacao.baixar_e_conferir"), acao: "baixar" };
  if (preparo.situacao === "baixando") return { n: 2, texto: preparo.mensagem || T("passo_atualizacao.baixando") };
  if (preparo.situacao === "falhou") return { n: 3, texto: T("passo_atualizacao.algum_pacote_nao_passou_na"), botao: T("passo_atualizacao.baixar_de_novo"), acao: "baixar" };
  return { n: 4, texto: T("passo_atualizacao.abra_o_plano_de_atualizacao_2"), foco: "atu-aplicar", botao: T("passo_atualizacao.ver_o_plano_de_atualizacao") };
}

const BOTAO_ACESSO = { credenciais: T("botao_acesso.informar_credenciais"), idrac: T("botao_acesso.configurar_acesso_ao_idrac") };
let passoDesenhado = "";

function renderizarGuia(atu) {
  const passo = passoAtualizacao(atu, itensEscolhidos());
  const chave = JSON.stringify(passo);
  if (chave === passoDesenhado) return;
  passoDesenhado = chave;
  $("atu-etapas").replaceChildren(...PASSOS_ATU.map((t, i) => el("li", {
    classe: i + 1 < passo.n ? "feita" : "", "aria-current": i + 1 === passo.n ? "step" : null },
  el("span", { classe: "num", texto: String(i + 1) }), t)));
  $("atu-proximo").textContent = passo.n + ". " + passo.texto;
  const rotulo = passo.botao || BOTAO_ACESSO[passo.acesso] || "";
  $("atu-proximo-botao").hidden = !rotulo;
  $("atu-proximo-botao").textContent = rotulo;
  $("atu-proximo-botao").onclick = () => executarPasso(passo);
  // 18.2/18.3: o acesso ao iDRAC é resolvido sozinho depois do ATUALIZAR; aqui só se informa (credenciais aceleram).
  const acesso = (atu.secao || {}).acesso || {};
  $("atu-acesso").hidden = !acesso.codigo;
  $("atu-acesso-texto").textContent = acesso.codigo ? acesso.texto + T("renderizar_guia.nao_precisa_resolver_agora_depois")
    + T("renderizar_guia.sozinho_usuario_administrador_temporario_pass") : "";
  $("atu-acesso-botao").hidden = acesso.codigo !== "credenciais";
  $("atu-acesso-botao").textContent = BOTAO_ACESSO.credenciais;
  $("atu-acesso-botao").onclick = () => abrirAcesso("credenciais");
  // Sem iDRAC e só .EXE no catálogo: o pacote Linux (.BIN) baixado à mão, com a página de drivers (pedido de 20/10/2026).
  const semIdrac = (atu.secao || {}).sem_idrac;
  $("atu-sem-idrac").hidden = !semIdrac;
  $("atu-sem-idrac-texto").textContent = semIdrac ? semIdrac.texto + " " : "";
  $("atu-sem-idrac-botao").onclick = () => irEFocar("atualizacoes", "man-abrir");
}

function executarPasso(passo) {
  if (passo.acao === "baixar") return baixarEscolhidos();
  if (passo.acesso) return abrirAcesso(passo.acesso);
  const alvo = passo.foco === "primeira-escolha" ? document.querySelector("#atu-itens .atu-escolha:not(:disabled)") : passo.foco && $(passo.foco);
  if (alvo) { alvo.scrollIntoView({ block: "center" }); alvo.focus(); if (passo.botao && !alvo.disabled) alvo.click(); }
}

function abrirAcesso(codigo) {
  if (codigo === "credenciais") { location.hash = "#/inicio"; setTimeout(() => { if (typeof abrirFormBMC === "function") abrirFormBMC(); }, 50); return; }
  // "iDRAC não acessível": ações do catálogo para configurar o acesso (item 4), na mesma tela.
  $("acoes-sim").checked = !$("acoes-sim").disabled;
  lembrar(chaveCartao($("acoes-cartao")), "0");  // começa recolhido (seção longa): abre para mostrar a ação
  aplicarRecolhido($("acoes-cartao"));
  const radio = document.querySelector('input[name="acoes-escolha"][value="idrac.ver"]');
  if (radio) { radio.checked = true; aoEscolherAcao(); }
  $("acoes-cartao").scrollIntoView({ block: "start" });
  (radio || $("acoes-sim")).focus();
}

async function baixarEscolhidos() {
  const itens = itensEscolhidos();
  if (!itens.length) return;
  $("atu-baixar").disabled = true;
  try { await api("atualizacoes/baixar", "POST", { itens: itens }); anunciar(T("baixar_escolhidos.baixando_e_conferindo_os_pacotes")); }
  catch (e) { $("atu-preparo-mensagem").textContent = e.message; anunciar(e.message); }
  atualizar();
}

function renderizarPreparo(atu) {
  const preparo = atu.preparo || {};
  const mesmos = preparo.ids && JSON.stringify(preparo.ids) === JSON.stringify(itensEscolhidos().sort());
  $("atu-preparo-mensagem").textContent = mesmos ? (preparo.mensagem || "") : "";
  const itens = mesmos ? (preparo.itens || []) : [];
  const chave = JSON.stringify(itens);
  if ($("atu-preparo").dataset.chave !== chave) {
    $("atu-preparo").dataset.chave = chave;
    $("atu-preparo").replaceChildren(...itens.map((i) => el("li", {}, etiqueta(i.ok ? "OK" : "FALHA", i.ok ? T("renderizar_preparo.conferido") : T("renderizar_preparo.recusado")), " ",
      el("strong", { texto: i.componente }), el("ul", { classe: "lista-simples texto-sec pequeno" },
        ...[T("renderizar_preparo.hash") + i.hash, T("renderizar_preparo.modelo") + i.modelo, T("renderizar_preparo.versao") + i.versao].map((t) => el("li", { texto: t }))))));
  }
  $("atu-baixar").disabled = !itensEscolhidos().length || preparo.situacao === "baixando" || (atu.aplicacao || {}).situacao === "executando";
}

/* Instalação (18.2): o servidor confere o plano e as confirmações de novo; depois do ATUALIZAR, tudo é automático. */
const SITUACAO_PASSO = { ok: "OK", simulado: "NA", aviso: "ALERTA", falhou: "FALHA", bloqueado: "FALHA" };

function itensEscolhidos() {
  return Array.from(document.querySelectorAll("#atu-itens .atu-escolha:checked")).map((c) => c.dataset.id);
}

function aoEscolherAtualizacao() {
  atualizarBotaoAplicar();
  const atu = (ESTADO && ESTADO.atualizacoes) || {};
  renderizarPreparo(atu);
  renderizarGuia(atu);
}

function atualizarBotaoAplicar() {
  const atu = (ESTADO && ESTADO.atualizacoes) || {};
  const ocupado = (atu.aplicacao || {}).situacao === "executando";
  $("atu-aplicar").disabled = !itensEscolhidos().length || ocupado;
}

const SITUACAO_ITEM = { pronto: ["ANDAMENTO", T("situacao_item.pacote_conferido")], aplicado: ["OK", T("situacao_item.aplicado")], agendado: ["ALERTA", T("situacao_item.agendado_para_o_reinicio")],
  abortado: ["FALHA", T("situacao_item.abortado")], falhou: ["FALHA", T("situacao_item.falhou")], possivelmente_agendado: ["ALERTA", T("situacao_item.aceito_pelo_idrac_confira_o")] };

function duracao(segundos) {
  if (segundos === null || segundos === undefined) return "?";
  const s = Math.max(0, Math.round(segundos));
  if (s < 60) return s + " s";
  const m = Math.floor(s / 60);
  return m < 60 ? m + " min " + (s % 60 ? (s % 60) + " s" : "") : Math.floor(m / 60) + " h " + (m % 60) + " min";
}

function barraInterna(pct) {
  const span = el("span", {});
  span.style.width = pct + "%"; // CSSOM: o CSP não permite o atributo style
  return span;
}

const STATUS_ETAPA = { concluido: "OK", agendado: "ALERTA", falhou: "FALHA", parado: "NAO_VERIFICADO", aguardando: "PENDENTE" };

/* 21.1/21.2: progresso ao vivo do servidor (``_acompanhamento`` durante a instalação, ``acompanhamento`` no fim). */
function renderizarAcompanhamento(aplicacao) {
  const resultado = aplicacao.resultado || {};
  const acomp = (typeof resultado._acompanhamento === "object" && resultado._acompanhamento) || resultado.acompanhamento || null;
  const executando = aplicacao.situacao === "executando";
  $("faixa-seguranca").hidden = !(executando && acomp);
  $("faixa-riscos").hidden = !(executando && (resultado.riscos_assumidos || {}).assumidos);
  $("atu-geral").hidden = !acomp;
  if (!acomp) return false;
  if (executando) {
    $("faixa-seguranca").classList.toggle("perigo", !acomp.seguro);
    const faixa = acomp.parar_pedido && acomp.seguro ? acomp.faixa + T("renderizar_acompanhamento.parada_pedida_o_proximo_componente") : acomp.faixa;
    if ($("faixa-seguranca-texto").textContent !== faixa) $("faixa-seguranca-texto").textContent = faixa;
    $("atu-parar").disabled = !!acomp.parar_pedido;
    $("atu-parar").lastElementChild.textContent = acomp.parar_pedido ? T("renderizar_acompanhamento.parada_pedida_depois_do_componente") : T("renderizar_acompanhamento.parar_depois_do_componente_atual");
  }
  $("atu-geral-barra").firstElementChild.style.width = acomp.geral + "%";
  $("atu-geral-barra").setAttribute("aria-valuenow", acomp.geral);
  $("atu-geral-pct").textContent = acomp.geral + "%";
  const partes = [T("renderizar_acompanhamento.decorrido") + duracao(acomp.decorrido_s)];
  if (acomp.restante_s !== null && acomp.restante_s !== undefined) partes.push(T("renderizar_acompanhamento.restante_estimado") + duracao(acomp.restante_s));
  if (acomp.ultima_resposta_s !== null && acomp.ultima_resposta_s !== undefined) partes.push(T("renderizar_acompanhamento.ultima_resposta_do_idrac_ha") + duracao(acomp.ultima_resposta_s));
  if (executando && acomp.esperando) partes.push(T("renderizar_acompanhamento.esperando") + acomp.esperando);
  $("atu-geral-texto").textContent = partes.join(" · ");
  const alerta = executando ? (acomp.aviso_imediato || acomp.sem_progresso || "") : "";
  $("atu-sem-progresso").hidden = !alerta;
  if ($("atu-sem-progresso-texto").textContent !== alerta) $("atu-sem-progresso-texto").textContent = alerta;
  const linhas = (acomp.itens || []).map((i) => {
    const st = i.gravando ? ["FALHA", T("renderizar_acompanhamento.gravando_firmware_agora")] : [STATUS_ETAPA[i.etapa] || "ANDAMENTO", i.nome];
    return el("li", { classe: "item-progresso" + (i.gravando ? " gravando" : "") },
      el("div", {}, etiqueta(st[0], st[1]), " ", el("strong", { texto: i.componente }), i.detalhe ? " — " + i.detalhe : ""),
      el("div", { classe: "progresso" }, el("div", { classe: "progresso-barra", role: "progressbar", "aria-label": T("renderizar_acompanhamento.progresso_de") + i.componente,
        "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": String(i.percentual) }, barraInterna(i.percentual)),
        el("span", { texto: i.percentual + "%" })),
      el("ol", { classe: "etapas-item" }, ...(i.etapas || []).map((e) => el("li", { texto: e.nome + " (" + duracao(e.segundos) + ")" }))));
  });
  const chave = JSON.stringify(acomp.itens);
  if ($("atu-progresso").dataset.chave !== chave) { $("atu-progresso").dataset.chave = chave; $("atu-progresso").replaceChildren(...linhas); }
  $("atu-progresso").hidden = !linhas.length;
  return true;
}

/* Progresso por componente (15.6): itens do aviso × o que a aplicação já fez (resultado parcial, ao vivo). */
function renderizarProgresso(aplicacao) {
  if (renderizarAcompanhamento(aplicacao)) { renderizarAntesDepois(aplicacao); return; }
  const itens = aplicacao.itens || [];
  const feitos = ((aplicacao.resultado || {}).itens) || [];
  const executando = aplicacao.situacao === "executando";
  let andamentoMarcado = false;
  const linhas = itens.map((i) => {
    const feito = feitos.find((f) => f.componente === i.componente);
    let st = ["PENDENTE", T("renderizar_progresso.aguardando")];
    if (feito) st = SITUACAO_ITEM[feito.situacao] || ["NAO_VERIFICADO", feito.situacao];
    else if (executando && !andamentoMarcado) { st = ["ANDAMENTO", T("renderizar_progresso.em_andamento")]; andamentoMarcado = true; }
    return el("li", {}, etiqueta(st[0], st[1]), " ", el("strong", { texto: i.componente }), " " + (i.atual || "?") + " → " + (i.nova || "?"));
  });
  const chave = JSON.stringify([itens, feitos.map((f) => f.situacao), executando]);
  if ($("atu-progresso").dataset.chave !== chave) { $("atu-progresso").dataset.chave = chave; $("atu-progresso").replaceChildren(...linhas); }
  $("atu-progresso").hidden = !itens.length;
  renderizarAntesDepois(aplicacao);
}

/* 21.4: jobs que ficaram no iDRAC (de antes), traduzidos, com o que acontece no próximo reinício. */
function detalhesTecnicos(...filhos) {
  return el("details", { classe: "detalhes-tecnicos" }, el("summary", { texto: T("comum.detalhes_tecnicos") }), ...filhos);
}

function renderizarJobsIdrac(fila, limpeza) {
  const jobs = (fila && fila.jobs) || [];
  $("atu-jobs").hidden = !jobs.length && !limpeza;
  const chave = JSON.stringify([jobs, limpeza]);
  if ($("atu-jobs-corpo").dataset.chave === chave) return;
  $("atu-jobs-corpo").dataset.chave = chave;
  const grupo = { ativo: ["ANDAMENTO", T("renderizar_jobs_idrac.em_andamento")], agendado: ["ALERTA", T("renderizar_jobs_idrac.agendado")], falha: ["FALHA", T("renderizar_jobs_idrac.falhou")] };
  const limpando = limpeza && limpeza.situacao === "executando";
  const itens = jobs.map((j) => el("li", {}, etiqueta(...(j.orfao ? ["FALHA", T("renderizar_jobs_idrac.travada")] : (grupo[j.grupo] || ["NAO_VERIFICADO", j.estado]))), " ",
    el("strong", { texto: (j.nome || j.id) + ": " }), j.orfao ? T("renderizar_jobs_idrac.texto_orfao", dataBR(j.inicio)) : j.texto,
    ...(j.orfao ? [el("div", { classe: "acoes" }, el("button", { type: "button", classe: "botao botao-secundario botao-pequeno", disabled: limpando,
      onclick: () => limparJob(j.id, false) }, icone("trash"), T("renderizar_jobs_idrac.limpar_job_travado")))] : []),
    detalhesTecnicos(el("p", { classe: "mono pequeno", texto: [j.id, j.estado, j.codigo, j.mensagem, j.motivo_orfao].filter(Boolean).join(" · ") }))));
  const resultado = [];
  if (limpeza && limpeza.situacao === "precisa_forcar") {
    resultado.push(el("div", { classe: "aviso aviso-falha", role: "alert" }, el("p", { texto: limpeza.mensagem }),
      el("div", { classe: "acoes" },
        el("button", { type: "button", classe: "botao botao-secundario", id: "jobs-forcar-cancelar", onclick: () => api("jobs/reler", "POST").catch(() => {}) },
          T("renderizar_jobs_idrac.cancelar")),
        el("button", { type: "button", classe: "botao botao-perigo", onclick: () => limparJob(limpeza.job, true) }, T("renderizar_jobs_idrac.limpar_a_fila_inteira")))));
  } else if (limpeza && limpeza.mensagem) {
    resultado.push(el("p", { role: "status", texto: limpeza.mensagem }));
  }
  if (limpeza && (limpeza.passos || []).length) {
    resultado.push(detalhesTecnicos(el("ul", { classe: "lista-simples pequeno" }, ...limpeza.passos.map((p) => el("li", { texto: dataBR(p.quando) + " — " + p.acao + ": " + p.detalhe })))));
  }
  $("atu-jobs-corpo").replaceChildren(...(fila && fila.explicacao || []).map((t) => el("p", { texto: t })),
    el("ul", { classe: "lista-progresso" }, ...itens), ...resultado);
  const cancelar = $("jobs-forcar-cancelar");
  if (cancelar) cancelar.focus();
}

/* 09/10/2026: "Limpar job travado". Primeiro só o job; se o iDRAC recusar, a tela avisa e só com confirmação limpa a fila. */
async function limparJob(id, forcar) {
  try { await api("jobs/limpar", "POST", { id, forcar }); anunciar(T("limpar_job.limpando")); }
  catch (e) { anunciar(T("limpar_job.nao_foi_possivel", e.message)); }
  atualizar();
}

function renderizarAntesDepois(aplicacao) {
  const antesDepois = ((aplicacao.resultado || {}).antes_depois) || [];
  $("atu-antes-depois").hidden = !antesDepois.length;
  if (antesDepois.length && $("atu-antes-depois").dataset.chave !== JSON.stringify(antesDepois)) {
    $("atu-antes-depois").dataset.chave = JSON.stringify(antesDepois);
    const tabela = el("table", { classe: "tabela" }, el("caption", { classe: "visualmente-oculto", texto: T("renderizar_antes_depois.antes_e_depois") }),
      el("thead", {}, el("tr", {}, ...[T("renderizar_antes_depois.componente"), T("renderizar_antes_depois.antes"), T("renderizar_antes_depois.agora"), T("renderizar_antes_depois.resultado")].map((t) => el("th", { scope: "col", texto: t })))),
      el("tbody", {}, ...antesDepois.map((i) => el("tr", {}, el("td", { texto: i.componente }), el("td", { texto: i.antes || "?" }),
        el("td", { texto: i.agora || "?" }), el("td", { texto: i.texto })))));
    $("atu-antes-depois").replaceChildren(el("h3", { classe: "subtitulo", texto: T("renderizar_antes_depois.antes_depois_inventario_relido_ao") }),
      el("div", { classe: "tabela-rolagem" }, tabela));
  }
}

function renderizarAplicacao(estado) {
  const atu = estado.atualizacoes || {};
  $("atu-trava").hidden = !atu.trava;
  $("atu-trava-texto").textContent = atu.trava ? T("renderizar_aplicacao.trava_de_seguranca") + atu.trava + "." : "";
  const aplicacao = atu.aplicacao || {};
  const mensagem = aplicacao.mensagem || "";
  if ($("atu-aplicacao-mensagem").textContent !== mensagem) $("atu-aplicacao-mensagem").textContent = mensagem;
  renderizarProgresso(aplicacao);
  const retomada = atu.retomada || {};
  const parado = !!retomada.pode_continuar_parado && aplicacao.situacao !== "executando";
  $("atu-parado").hidden = !parado;
  $("atu-parado-texto").textContent = parado ? T("renderizar_aplicacao.a_atualizacao_anterior_foi_parada") + ((retomada.pendentes || []).length
    ? (retomada.pendentes.length + T("renderizar_aplicacao.item_ns_nao_iniciado_s")) : "") + T("renderizar_aplicacao.ela_so_continua_se_voce") : "";
  renderizarJobsIdrac(atu.jobs_idrac, atu.limpeza_jobs);
  const passos = ((aplicacao.resultado || {}).passos) || [];
  const chave = JSON.stringify(passos);
  if ($("atu-passos").dataset.chave !== chave) {
    $("atu-passos").dataset.chave = chave;
    $("atu-passos").replaceChildren(...passos.map((p) => el("li", {},
      etiqueta(SITUACAO_PASSO[p.situacao] || "NAO_VERIFICADO", p.situacao), " ", el("strong", { texto: p.acao + ": " }), p.detalhe)));
  }
  atualizarBotaoAplicar();
  renderizarAviso(atu);
  renderizarPreparo(atu);
  renderizarGuia(atu);
}

/* ------------------------------------------------------------------ Plano de atualização (CLAUDE.md 18.2) */

/* Tela única antes de qualquer instalação (catálogo ou pacote manual). O servidor relê o hardware e o BMC (só leitura);
   aqui só se mostra e se habilita o botão — o servidor confere o plano e o ATUALIZAR de novo. */
let avisoPedido = null, focoAntesDoAviso = null;
const SITUACAO_HARDWARE = { ok: ["OK", "OK"], aviso: ["ALERTA", T("situacao_hardware.atencao")], redundancia: ["ALERTA", T("situacao_hardware.redundancia_perdida")],
  historico: ["ALERTA", T("situacao_hardware.evento_antigo")], bloqueia: ["FALHA", "BLOQUEIA"] };

async function abrirAviso(origem, itens) {
  const atu = (ESTADO && ESTADO.atualizacoes) || {};
  avisoPedido = { origem: origem, itens: itens, gerado: null };
  focoAntesDoAviso = document.activeElement;
  for (const campo of ["aviso-ciente", "atu-confirmacao", "atu-confirmacao-redundancia"]) {
    if ($(campo).type === "checkbox") $(campo).checked = false; else $(campo).value = "";
  }
  $("aviso-conteudo").hidden = true;
  $("aviso-mensagem").textContent = "";
  $("aviso-conferindo").textContent = T("abrir_aviso.conferindo_o_hardware_agora_fontes");
  $("aviso-modo").textContent = T("abrir_aviso.instalacao_de_firmware_depois_do");
  habilitarAviso();
  $("aviso-fundo").hidden = false;
  $("aviso-cancelar").focus();
  try { await api("atualizacoes/aviso", "POST", { origem: origem, itens: itens }); }
  catch (e) { $("aviso-conferindo").textContent = T("abrir_aviso.nao_foi_possivel_conferir", e.message); anunciar($("aviso-conferindo").textContent); }
  atualizar();
}

function renderizarAviso(atu) {
  if (!avisoPedido || $("aviso-fundo").hidden) return;
  const aviso = atu.aviso;
  if (!aviso) return;
  if (aviso.situacao !== "pronto") {
    const texto = aviso.mensagem || "";
    if ($("aviso-conferindo").textContent !== texto) $("aviso-conferindo").textContent = texto;
    return;
  }
  if (avisoPedido.gerado === aviso.gerado) return;
  avisoPedido.gerado = aviso.gerado;
  avisoPedido.aviso = aviso;
  $("aviso-conferindo").textContent = "";
  // 21.3: tempo e reinício por componente; o iDRAC vai sozinho e primeiro; os que exigem reinício vão no MESMO reinício.
  const porItem = (aviso.estimativa && aviso.estimativa.por_item) || [];
  $("aviso-itens").replaceChildren(...aviso.itens.map((i, n) => el("li", {},
    el("strong", { texto: i.componente }), " " + (i.atual || "?") + " → " + (i.nova || "?") + (i.data ? " (" + i.data + ")" : ""),
    el("ul", { classe: "lista-simples texto-sec pequeno" }, ...[i.origem, T("renderizar_aviso.hash") + i.hash, i.rollback,
      T("renderizar_aviso.cerca_de") + ((porItem[n] || {}).minutos || "?") + " min" + ((porItem[n] || {}).sozinho
        ? T((porItem[n] || {}).idrac ? "renderizar_aviso.firmware_do_idrac_sozinho_e" : "renderizar_aviso.bios_sozinha") : ""),
      i.reinicio ? ((porItem[n] || {}).sozinho ? T("renderizar_aviso.exige_reinicio_do_servidor_um")
        : T("renderizar_aviso.exige_reinicio_do_servidor_junto")) : T("renderizar_aviso.sem_reinicio_do_servidor")].map((t) => el("li", { texto: t }))))));
  // Sem itens (seleção antiga ou verificação refeita), "0 min, 0 reinícios" enganaria: some e o bloqueio explica.
  $("aviso-estimativa").hidden = !aviso.itens.length;
  $("aviso-estimativa").replaceChildren(el("strong", { texto: T("renderizar_aviso.tempo_estimado_cerca_de") + aviso.estimativa.minutos + " min" }),
    T("renderizar_aviso.reinicios") + aviso.estimativa.reinicios);
  // Liberação restrita (06/10/2026): o que pode ser gravado agora e o que continua bloqueado, sempre visível.
  const lib = aviso.liberacao || {};
  $("aviso-liberacao").hidden = !lib.restrita;
  $("aviso-liberacao-lista").replaceChildren(...(lib.restrita ? [
    el("li", {}, etiqueta("OK", T("renderizar_aviso.liberado")), " " + lib.texto_liberado),
    el("li", {}, etiqueta("FALHA", T("renderizar_aviso.bloqueado")), " " + lib.texto_bloqueado),
    ...(lib.bloqueados || []).map((c) => el("li", {}, etiqueta("FALHA", T("renderizar_aviso.bloqueado_agora")), " " + c)),
  ] : []));
  $("aviso-hardware").replaceChildren(...aviso.hardware.map((h) => {
    const [st, texto] = SITUACAO_HARDWARE[h.situacao] || ["NAO_VERIFICADO", h.situacao];
    return el("li", { classe: h.situacao === "redundancia" ? "destaque-redundancia" : "" }, etiqueta(st, texto), " ", el("strong", { texto: h.item + ": " }), h.texto);
  }));
  $("aviso-extras").replaceChildren(...(aviso.avisos_extras || []).map((t) => el("li", { texto: t })));
  $("aviso-bloqueios").hidden = aviso.pode_seguir;
  $("aviso-bloqueios").replaceChildren(el("strong", { texto: T("renderizar_aviso.instalacao_bloqueada") }),
    el("ul", {}, ...aviso.bloqueios.map((b) => el("li", { texto: b }))),
    ...(aviso.pode_assumir ? [el("p", { texto: T("renderizar_aviso.bloqueio_assumivel") })] : []));
  // 09/10/2026: só checagens de hardware → "Continuar assumindo os riscos" (o foco continua no Cancelar).
  avisoPedido.assumindo = false;
  $("aviso-assumir-area").hidden = !(!aviso.pode_seguir && aviso.pode_assumir);
  $("aviso-riscos").hidden = true;
  $("atu-confirmacao-riscos").value = "";
  $("aviso-riscos-lista").replaceChildren(...(aviso.riscos || []).map((r) => el("li", {},
    etiqueta("FALHA", T("renderizar_aviso.checagem_ignorada")), " ", el("strong", { texto: r.item + ": " }), r.texto,
    el("p", { classe: "risco-concreto", texto: T("renderizar_aviso.risco", r.risco) }))));
  $("aviso-seguir").hidden = !aviso.pode_seguir;
  $("aviso-fixos").replaceChildren(...aviso.avisos_fixos.map((t) => el("li", { texto: t })));
  $("aviso-temporarias").replaceChildren(...(aviso.temporarias || []).map((t) => el("li", { texto: t })));
  $("atu-redundancia").hidden = !aviso.redundancia;
  $("aviso-historico").hidden = !aviso.historico;
  $("aviso-ciente-texto").textContent = aviso.texto_ciente || T("renderizar_aviso.estou_ciente_de_que_e");
  $("aviso-instalar").hidden = !aviso.pode_seguir;
  $("aviso-conteudo").hidden = false;
  $("aviso-instalar").textContent = T("plano.instalar_agora");
  anunciar(aviso.pode_seguir ? T("renderizar_aviso.plano_de_atualizacao_pronto_leia") : T("renderizar_aviso.instalacao_bloqueada_veja_os_motivos"));
  habilitarAviso();
}

/* "Continuar assumindo os riscos" (09/10/2026): mostra cada checagem que falhou com o risco concreto e pede ASSUMO OS RISCOS
   além do ATUALIZAR. Vale só para esta instalação, neste servidor (o servidor confere de novo). */
function assumirRiscos() {
  if (!avisoPedido || !avisoPedido.aviso || !avisoPedido.aviso.pode_assumir) return;
  avisoPedido.assumindo = true;
  $("aviso-assumir-area").hidden = true;
  $("aviso-riscos").hidden = false;
  $("aviso-seguir").hidden = false;
  $("aviso-instalar").hidden = false;
  $("aviso-instalar").textContent = T("renderizar_aviso.instalar_assumindo_os_riscos");
  $("aviso-riscos-titulo").focus();
  habilitarAviso();
}

function habilitarAviso() {
  const aviso = avisoPedido && avisoPedido.aviso;
  const assumindo = Boolean(aviso && !aviso.pode_seguir && aviso.pode_assumir && avisoPedido.assumindo);
  const pronto = Boolean(aviso && (aviso.pode_seguir || assumindo));
  const riscosOk = !assumindo || $("atu-confirmacao-riscos").value.trim() === "ASSUMO OS RISCOS";
  $("atu-confirmacao").disabled = !pronto;
  const redundanciaOk = $("atu-redundancia").hidden || $("atu-confirmacao-redundancia").value.trim() === "SEM REDUNDANCIA";
  const cienteOk = $("aviso-historico").hidden || $("aviso-ciente").checked;
  $("aviso-instalar").disabled = !pronto || $("atu-confirmacao").value.trim() !== "ATUALIZAR" || !redundanciaOk || !cienteOk || !riscosOk;
}

function fecharAviso(cancelar) {
  if (cancelar && avisoPedido) api("atualizacoes/aviso/cancelar", "POST").catch(() => {});
  avisoPedido = null;
  $("aviso-fundo").hidden = true;
  if (focoAntesDoAviso && document.contains(focoAntesDoAviso)) focoAntesDoAviso.focus();
}

async function instalarPeloAviso() {
  if ($("aviso-instalar").disabled || !avisoPedido) return;
  $("aviso-instalar").disabled = true;
  const pedido = avisoPedido;
  const comum = { confirmacao: $("atu-confirmacao").value, confirmacao_redundancia: $("atu-confirmacao-redundancia").value,
    confirmacao_riscos: pedido.assumindo ? $("atu-confirmacao-riscos").value : "" };
  try {
    if (pedido.origem === "catalogo") await api("atualizacoes/aplicar", "POST", Object.assign({ itens: pedido.itens }, comum));
    else await api("manual/instalar", "POST", Object.assign({ id: pedido.itens[0] }, comum));
    fecharAviso(false);
    anunciar(T("instalar_pelo_aviso.instalacao_iniciada_acompanhe_o_progresso"));
    $("atu-aplicacao-mensagem").scrollIntoView({ block: "center" });
  } catch (e) {
    $("aviso-mensagem").textContent = T("instalar_pelo_aviso.nada_foi_feito", e.message);
    anunciar($("aviso-mensagem").textContent);
    habilitarAviso();
  }
  atualizar();
}

/* "Exportar log técnico" (18.6): o servidor monta o JSON sanitizado (sem senhas, chaves nem dados do cliente), grava no
   pendrive e devolve o conteúdo; aqui ele também pode ser salvo pelo navegador. */
async function exportarLog(evento) {
  const botao = evento && evento.currentTarget;
  const saida = botao && botao.parentElement && botao.parentElement.nextElementSibling && botao.parentElement.nextElementSibling.matches("[data-log-mensagem]")
    ? botao.parentElement.nextElementSibling : null;
  try {
    const r = await api("log/exportar", "POST");
    const texto = r.gravado ? T("exportar_log.log_tecnico_salvo_no_pendrive") + r.onde + T("exportar_log.mande_esse_arquivo_junto_com")
      : T("exportar_log.log_tecnico_gerado") + r.onde + ").";
    if (saida) saida.textContent = texto;
    anunciar(texto);
    const link = el("a", { href: URL.createObjectURL(new Blob([r.conteudo], { type: "application/json" })), download: r.nome || "msscan_log_tecnico.json" });
    document.body.appendChild(link); link.click(); link.remove();
  } catch (e) { if (saida) saida.textContent = T("exportar_log.nao_foi_possivel_exportar", e.message); anunciar(e.message); }
}

/* "Coletar dados deste servidor" (16/10/2026): o mesmo --coletar do terminal, somente leitura; o andamento vem do estado. */
async function coletarDados() {
  try { await api("coleta/iniciar", "POST"); anunciar(T("coletar_dados.iniciada")); }
  catch (e) { $("coleta-mensagem").textContent = T("coletar_dados.nao_foi_possivel", e.message); anunciar(e.message); }
}

function renderizarColeta(estado) {
  const c = estado.coleta || { situacao: "nao_solicitada", mensagem: "" };
  $("coleta-botao").disabled = c.situacao === "coletando" || estado.modo === "replay";
  $("coleta-mensagem").textContent = c.mensagem || "";
}

async function verificarAtualizacoes() {
  if (!$("atu-sim").checked) return;
  $("atu-verificar-botao").disabled = true;
  try { await api("atualizacoes/verificar", "POST"); anunciar(T("verificar_atualizacoes.verificando_atualizacoes_somente_informativo")); }
  catch (e) { anunciar(T("verificar_atualizacoes.nao_foi_possivel_verificar", e.message)); }
  atualizar();
}

/* ------------------------------------------------------------------ atualização manual (fase 11) */
/* Navegador integrado + assistente: SHA-256 da página → checagem do pacote (DUP -c) → Instalar com ATUALIZAR.
   O servidor confere tudo de novo; aqui só se habilitam os botões. */

const ETAPA_MANUAL = {
  sha: ["NA", T("etapa_manual.falta_o_sha_256")], checagem: ["NA", "checando"], checando: ["NA", T("etapa_manual.checando_o_pacote")],
  pronto: ["OK", T("etapa_manual.pronto_para_instalar")], recusado: ["FALHA", "recusado"], instalado: ["OK", T("etapa_manual.concluido")],
};
let manualDesenhado = "";
let manualProcurado = false;

function renderizarManual(estado) {
  const man = (estado.atualizacoes || {}).manual;
  $("man-cartao").hidden = !(man && man.disponivel);
  $("atu-sem-idrac-botao").hidden = $("man-cartao").hidden;  // o aviso "sem iDRAC" leva à Atualização manual
  if (!man || !man.disponivel) return;
  if (!manualProcurado) { manualProcurado = true; api("manual/procurar", "POST", {}).catch(() => {}); }
  $("man-pasta").textContent = (man.pasta || T("renderizar_manual.msscan_firmware_manual")) + "/";
  const navegador = man.navegador || {};
  $("man-abrir").lastElementChild.textContent = navegador.aberto ? T("renderizar_manual.mostrar_o_navegador") : T("renderizar_manual.abrir_pagina_de_drivers_deste");
  $("man-fechar").hidden = !navegador.aberto;
  // Proxy do navegador: domínio oficial que não respondeu (em vez de o técnico só ver "timeout" no navegador).
  const falhas = Object.entries(((man.dominios || {}).falhas) || {});
  $("man-falhas").hidden = !(navegador.aberto && falhas.length);
  $("man-falhas").textContent = falhas.length ? T("renderizar_manual.sem_conexao_com") + falhas.map(([d, m]) => d + " (" + m + ")").join("; ")
    + T("renderizar_manual.confira_a_internet_do_servidor") : "";
  if ($("man-mensagem").dataset.servidor !== (man.mensagem || "")) {
    $("man-mensagem").dataset.servidor = man.mensagem || "";
    $("man-mensagem").textContent = man.mensagem || "";
  }
  const redundancia = !!man.redundancia_perdida;
  const aplicar = (estado.atualizacoes || {}).modo === "aplicar";
  const chave = JSON.stringify([man.pacotes, man.bloqueios, redundancia, aplicar]);
  if (chave === manualDesenhado) return;
  manualDesenhado = chave;
  const bloqueios = (man.bloqueios || []).map((b) => el("li", { classe: "ia-nao-confirmada", texto: T("renderizar_manual.bloqueio_da_instalacao") + b }));
  $("man-pacotes").replaceChildren(...bloqueios, ...(man.pacotes || []).map((p) => pacoteManual(p, redundancia, aplicar)));
}

function pacoteManual(p, redundancia, aplicar) {
  const [status, texto] = ETAPA_MANUAL[p.etapa] || ["NAO_VERIFICADO", p.etapa];
  const info = [];
  if (p.pacote) info.push(p.pacote.nome + T("pacote_manual.versao") + p.pacote.versao + (p.pacote.sistemas.length ? T("pacote_manual.para") + p.pacote.sistemas.join(", ") : ""));
  info.push(T("pacote_manual.instalada") + (p.instalada || "?") + T("pacote_manual.pacote") + (p.disponivel || "?") + (p.motivo_versao ? " (" + p.motivo_versao + ")" : ""));
  if (p.motivo_modelo) info.push(T("pacote_manual.modelo") + p.motivo_modelo);
  if (p.motivo_metodo) info.push(T("pacote_manual.metodo") + p.motivo_metodo);
  if (p.checagem) info.push(T("pacote_manual.checagem_do_pacote_codigo") + p.checagem.codigo + " (" + p.checagem.nome + ") — " + p.checagem.texto);
  if (p.sha256 && p.etapa !== "recusado") info.push(T("pacote_manual.sha_256_conferido") + p.sha256);
  const corpo = [el("div", {}, el("strong", { classe: "mono", texto: p.nome }), " ", etiqueta(status, texto)),
    el("div", { classe: "texto-sec pequeno", texto: p.local || "" }),
    el("ul", { classe: "pequeno" }, ...info.map((t) => el("li", { texto: t })))];
  if (p.motivo) corpo.push(el("p", { classe: p.etapa === "recusado" ? "ia-nao-confirmada" : "", role: "status", texto: p.motivo }));
  if (p.etapa === "sha") {
    const campo = el("input", { type: "text", id: "man-sha-" + p.id, autocomplete: "off", spellcheck: "false", classe: "mono",
      placeholder: T("pacote_manual.sha_256_da_pagina_da"),
      onkeydown: (ev) => { if (ev.key === "Enter") { ev.preventDefault(); informarSha(p.id); } } });
    corpo.push(el("label", { classe: "campo" }, T("pacote_manual.sha_256_do_arquivo_copie"), campo),
      el("div", { classe: "acoes" }, el("button", { type: "button", classe: "botao botao-secundario", onclick: () => informarSha(p.id) },
        icone("shield-check"), T("pacote_manual.conferir_o_sha_256"))));
  }
  if (p.etapa === "pronto") {
    corpo.push(el("div", { classe: "acoes" }, el("button", { type: "button", classe: "botao botao-perigo", id: "man-instalar-" + p.id,
      onclick: () => abrirAviso("manual", [p.id]) }, icone("alert"), T("pacote_manual.revisar_avisos_e_instalar"))));
  }
  return el("li", { classe: "man-pacote" }, ...corpo);
}

async function acaoManual(rota, corpo, sucesso) {
  try {
    const resposta = await api(rota, "POST", corpo || {});
    const texto = (resposta && resposta.mensagem) || sucesso;
    if (texto) { $("man-mensagem").textContent = texto; anunciar(texto); }
  } catch (e) { $("man-mensagem").textContent = e.message; anunciar(e.message); }
  manualDesenhado = "";
  atualizar();
}

/* Pasta de downloads (CLAUDE.md 15.2): lista MSSCAN/firmware/manual/; apagar pede confirmação (Cancelar com o foco). */
function tamanhoLegivel(bytes) {
  return bytes >= 2 ** 20 ? (bytes / 2 ** 20).toLocaleString("pt-BR", { maximumFractionDigits: 1 }) + " MB"
    : Math.ceil(bytes / 1024) + " KB";
}

async function carregarPastaManual() {
  let dados;
  try { dados = await api("manual/arquivos"); } catch (e) { $("man-pasta-titulo").textContent = e.message; return; }
  $("man-pasta-titulo").textContent = dados.motivo || (T("carregar_pasta_manual.pasta") + dados.pasta + "/ — " + dados.arquivos.length + T("carregar_pasta_manual.arquivo_s_nada_aqui_e"));
  $("man-pasta-lista").replaceChildren(...(dados.arquivos.length ? dados.arquivos.map((a) => {
    const acoes = el("td", {});
    const apagar = el("button", { type: "button", classe: "botao botao-perigo botao-pequeno", "aria-label": T("carregar_pasta_manual.apagar") + a.nome,
      onclick: () => {
        const cancelar = el("button", { type: "button", classe: "botao botao-secundario botao-pequeno", onclick: () => { acoes.replaceChildren(apagar); apagar.focus(); } }, T("carregar_pasta_manual.cancelar"));
        const confirmar = el("button", { type: "button", classe: "botao botao-perigo botao-pequeno", onclick: async () => {
          await acaoManual("manual/apagar", { nome: a.nome, confirmar: true }, "");
          carregarPastaManual();
        } }, icone("trash"), T("carregar_pasta_manual.confirmar_apagar"));
        acoes.replaceChildren(el("span", { classe: "pequeno", texto: T("carregar_pasta_manual.apagar") + a.nome + "? " }), cancelar, " ", confirmar);
        cancelar.focus();
      } }, icone("trash"), T("carregar_pasta_manual.apagar_2"));
    acoes.append(apagar);
    return el("tr", {}, el("td", { classe: "mono", texto: a.nome }), el("td", { texto: tamanhoLegivel(a.tamanho) }),
      el("td", { texto: a.data.replace("T", " ") }), el("td", { texto: a.usado }), acoes);
  }) : [el("tr", {}, el("td", { colspan: "5", classe: "texto-sec", texto: T("carregar_pasta_manual.nenhum_arquivo_na_pasta") }))]));
}

function alternarPastaManual() {
  const area = $("man-pasta-area");
  area.hidden = !area.hidden;
  $("man-pasta-abrir").setAttribute("aria-expanded", String(!area.hidden));
  $("man-pasta-abrir").lastElementChild.textContent = area.hidden ? T("alternar_pasta_manual.abrir_pasta_de_downloads") : T("alternar_pasta_manual.fechar_pasta_de_downloads");
  if (!area.hidden) carregarPastaManual();
}

function informarSha(id) {
  const campo = $("man-sha-" + id);
  acaoManual("manual/sha", { id, sha: campo ? campo.value : "" });
}

/* ------------------------------------------------------------------ ações corretivas (fase 10) */
/* Catálogo fechado: nenhuma ação vem escolhida; a prévia (só leitura) vem antes; CORRIGIR digitado. O servidor
   confere de novo a pré-checagem e a confirmação: o navegador só habilita os botões. */

let acoesDesenhadas = "";

function acaoEscolhida() {
  const marcada = document.querySelector('#acoes-itens input[name="acoes-escolha"]:checked');
  return marcada ? marcada.value : "";
}

function renderizarAcoes(estado) {
  const ac = estado.acoes || { disponivel: false, motivo: "", catalogo: [] };
  const ocupado = ["prechecando", "executando"].includes(ac.situacao);
  $("acoes-indisponivel").hidden = ac.disponivel || !ac.motivo;
  $("acoes-indisponivel-texto").textContent = frase(ac.motivo);
  $("acoes-sim").disabled = !ac.disponivel;
  if ($("acoes-sim").disabled && $("acoes-sim").checked) $("acoes-nao").checked = true;
  $("acoes-lista").disabled = !$("acoes-sim").checked || ocupado;
  const chave = JSON.stringify(ac.catalogo);
  if (chave !== acoesDesenhadas) {
    acoesDesenhadas = chave;
    const escolhida = acaoEscolhida();
    $("acoes-itens").replaceChildren(...(ac.catalogo || []).map((a) => el("label", { classe: "opcao-acao" },
      el("input", { type: "radio", name: "acoes-escolha", value: a.id, checked: a.id === escolhida, onchange: aoEscolherAcao }),
      el("span", {},
        el("strong", { texto: a.titulo }), " ", el("code", { classe: "pequeno", texto: a.id }),
        (a.recomendada || []).length ? el("span", {}, " ", etiquetaLivre("ALERTA", "light-bulb", T("renderizar_acoes.recomendada_pela_ia"))) : null,
        el("span", { classe: "texto-sec pequeno bloco", texto: a.descricao + T("renderizar_acoes.requer") + a.requer + "." }),
        (a.recomendada || []).length ? el("span", { classe: "texto-sec pequeno bloco", texto: T("renderizar_acoes.problemas") + a.recomendada.join("; ") }) : null))));
  }
  const modulo = ((ac.catalogo || []).find((a) => a.id === acaoEscolhida()) || {}).modulo;
  $("acoes-ntp").hidden = modulo !== "ntp";
  $("acoes-ip").hidden = acaoEscolhida() !== "idrac.ip_fixo";
  renderizarSugestoesNtp(ac.ntp_sugestoes || []);
  $("acoes-prechecar").disabled = $("acoes-lista").disabled || !acaoEscolhida();
  renderizarPrechecagem(ac);
  renderizarResultadoAcao(ac);
  renderizarDesfazer(ac);
  atualizarBotaoAcao();
}

let ntpPedido = false;

function aoEscolherAcao() {
  const ac = (ESTADO && ESTADO.acoes) || {};
  const modulo = ((ac.catalogo || []).find((a) => a.id === acaoEscolhida()) || {}).modulo;
  if (modulo === "ntp" && !ntpPedido) {
    ntpPedido = true;  // lê uma vez o NTP informado pelo DHCP (só leitura)
    api("acoes/ntp_sugestoes", "POST").then(atualizar).catch(() => { ntpPedido = false; });
  }
  if (ESTADO) renderizarAcoes(ESTADO);
}

/* NTP: sugestões (DHCP da rede do cliente, senão pool.ntp.org) ou servidores digitados. A primeira vem marcada:
   não altera nada sozinha (a prévia e o CORRIGIR continuam obrigatórios). */
let ntpDesenhado = "";
let ntpEscolhaTecnico = null;  // valor escolhido pelo técnico ("" = digitar outro); null = ainda não escolheu

function renderizarSugestoesNtp(sugestoes) {
  const chave = JSON.stringify(sugestoes);
  if (chave === ntpDesenhado) return;
  ntpDesenhado = chave;
  $("acoes-ntp-sugestoes").replaceChildren(...sugestoes.map((s, i) => el("label", { classe: "opcao-acao" },
    el("input", { type: "radio", name: "acoes-ntp-escolha", value: s.servidor, onchange: aoEscolherNtp,
      checked: ntpEscolhaTecnico === null ? i === 0 : ntpEscolhaTecnico === s.servidor }),
    el("span", {}, el("strong", { classe: "mono", texto: s.servidor }), el("span", { classe: "texto-sec pequeno bloco", texto: s.origem })))));
  $("acoes-ntp-outro").checked = ntpEscolhaTecnico === "" || (ntpEscolhaTecnico === null && !sugestoes.length);
  atualizarCampoNtp();
}

function aoEscolherNtp() {
  const marcada = document.querySelector('input[name="acoes-ntp-escolha"]:checked');
  ntpEscolhaTecnico = marcada ? marcada.value : null;
  atualizarCampoNtp();
}

function atualizarCampoNtp() {
  $("acoes-servidores").disabled = !$("acoes-ntp-outro").checked;
}

function servidoresNtpEscolhidos() {
  const marcada = document.querySelector('input[name="acoes-ntp-escolha"]:checked');
  if (!marcada || marcada.id === "acoes-ntp-outro") return $("acoes-servidores").value;
  return marcada.value;
}

function listaTitulada(titulo, itens) {
  return (itens || []).length ? [el("p", {}, el("strong", { texto: titulo })), el("ul", {}, ...itens.map((t) => el("li", { texto: t })))] : [];
}

function tabelaAntesDepois(antes, depois) {
  const campos = Array.from(new Set([...Object.keys(antes || {}), ...Object.keys(depois || {})]));
  if (!campos.length) return null;
  const rotulos = { ntp_ligado: T("tabela_antes_depois.ntp_ligado"), servidores: T("tabela_antes_depois.servidores_ntp"), data_hora_bmc: T("tabela_antes_depois.data_e_hora_do_bmc"), diferenca_s: T("tabela_antes_depois.diferenca_do_relogio_s"), eventos: T("tabela_antes_depois.eventos_no_sel") };
  const valor = (v) => v === undefined ? "—" : v === null ? "?" : v === true ? "sim" : v === false ? T("tabela_antes_depois.nao") : Array.isArray(v) ? (v.join(", ") || "(nenhum)") : String(v);
  return el("table", { classe: "tabela" },
    el("thead", {}, el("tr", {}, el("th", { scope: "col", texto: T("tabela_antes_depois.campo") }), el("th", { scope: "col", texto: T("tabela_antes_depois.antes") }), el("th", { scope: "col", texto: T("tabela_antes_depois.depois") }))),
    el("tbody", {}, ...campos.map((c) => el("tr", {}, el("td", { texto: rotulos[c] || c }), el("td", { classe: "mono", texto: valor((antes || {})[c]) }),
      el("td", { classe: "mono", texto: depois ? valor(depois[c]) : "—" })))));
}

function renderizarPrechecagem(ac) {
  const pre = ac.prechecagem;
  const chave = JSON.stringify([pre, ac.situacao]);
  if ($("acoes-prechecagem").dataset.chave === chave) return;
  $("acoes-prechecagem").dataset.chave = chave;
  if (ac.situacao === "prechecando") { $("acoes-prechecagem").replaceChildren(el("p", { texto: ac.mensagem })); }
  else if (!pre) { $("acoes-prechecagem").replaceChildren(); }
  else {
    const situacao = pre.pode_seguir ? etiqueta("OK", T("renderizar_prechecagem.pode_seguir")) : pre.nada_a_fazer ? etiqueta("NA", T("renderizar_prechecagem.nada_a_alterar")) : etiqueta("FALHA", T("renderizar_prechecagem.bloqueada"));
    $("acoes-prechecagem").replaceChildren(el("p", {}, el("strong", { texto: pre.titulo + ": " }), situacao),
      ...listaTitulada(T("renderizar_prechecagem.bloqueios"), pre.bloqueios), ...listaTitulada(T("renderizar_prechecagem.avisos"), pre.avisos),
      ...listaTitulada(T("renderizar_prechecagem.o_que_sera_alterado"), pre.previa), tabelaAntesDepois(pre.antes, null));
  }
  const pronto = !!(pre && pre.pode_seguir);
  $("acoes-confirmar").hidden = !pronto;
  // 18.2: sem simulação na interface (o --simular interno de desenvolvimento só muda o servidor).
  $("acoes-titulo-confirmacao").textContent = T("renderizar_prechecagem.confirmacao_executar_a_acao");
  $("acoes-modo-texto").textContent = T("renderizar_prechecagem.altera_o_bmc_conforme_o");
  $("acoes-executar").textContent = T("renderizar_prechecagem.executar_a_acao");
  $("acoes-trava").hidden = !ac.trava;
  $("acoes-trava-texto").textContent = ac.trava ? T("renderizar_prechecagem.trava_de_seguranca") + ac.trava + "." : "";
}

function renderizarResultadoAcao(ac) {
  const resultado = ac.resultado;
  const mostrar = !!resultado || ac.situacao === "executando";
  $("acoes-resultado-cartao").hidden = !mostrar;
  const mensagem = ac.situacao === "executando" ? ac.mensagem : (resultado ? resultado.mensagem : "");
  if ($("acoes-mensagem").textContent !== mensagem) $("acoes-mensagem").textContent = mensagem;
  const chave = JSON.stringify(resultado || null);
  if ($("acoes-resultado").dataset.chave === chave) return;
  $("acoes-resultado").dataset.chave = chave;
  if (!resultado) { $("acoes-resultado").replaceChildren(); return; }
  const copias = [];
  if (resultado.exportacao) copias.push(T("renderizar_resultado_acao.sel_exportado_e_conferido_em") + (resultado.exportacao.no_pendrive || resultado.exportacao.arquivo)
    + " (" + resultado.exportacao.eventos + " evento(s))");
  if (resultado.anteriores) copias.push(T("renderizar_resultado_acao.valores_anteriores_guardados_e_conferidos") + (resultado.anteriores.no_pendrive || resultado.anteriores.arquivo));
  const desfeita = resultado.desfeita;
  const info = resultado.desfazer || {};
  const textoDesfeita = desfeita ? "sim (" + desfeita.situacao + (desfeita.modo === "aplicar" ? "" : T("renderizar_resultado_acao.modo_interno_de_teste")) + ")"
    : info.disponivel ? T("renderizar_resultado_acao.nao_pode_ser_desfeita") : T("renderizar_resultado_acao.nao_se_aplica") + (info.motivo ? " (" + info.motivo + ")" : "");
  $("acoes-resultado").replaceChildren(
    el("p", {}, el("strong", { texto: resultado.titulo + " (" + (resultado.modo === "aplicar" ? T("renderizar_resultado_acao.aplicacao") : T("renderizar_resultado_acao.modo_interno_de_teste_2")) + "): " }), resultado.situacao),
    ...copias.map((c) => el("p", {}, etiquetaLivre("OK", "file", T("renderizar_resultado_acao.pendrive")), " ", el("span", { classe: "mono", texto: c }))),
    tabelaAntesDepois(resultado.antes, resultado.depois),
    el("p", {}, el("strong", { texto: T("renderizar_resultado_acao.desfeita") }), textoDesfeita),
    desfeita ? tabelaAntesDepois(desfeita.antes, desfeita.depois) : null,
    el("ol", { classe: "lista-passos", "aria-label": T("renderizar_resultado_acao.passos_da_acao") }, ...[...(resultado.passos || []), ...((desfeita || {}).passos || [])].map((p) => el("li", {},
      etiqueta(SITUACAO_PASSO[p.situacao] || "NAO_VERIFICADO", p.situacao), " ", el("strong", { texto: p.acao + ": " }), p.detalhe))));
}

function renderizarDesfazer(ac) {
  const resultado = ac.resultado;
  const pode = !!(resultado && (resultado.desfazer || {}).disponivel && !resultado.desfeita && ac.indice_resultado !== null);
  const ocupado = ["prechecando", "executando"].includes(ac.situacao);
  $("acoes-desfazer-acoes").hidden = !pode;
  $("acoes-desfazer-ver").disabled = ocupado;
  const pre = ac.desfazer;
  const mostrar = pode && !!pre && pre.indice === ac.indice_resultado;
  $("acoes-desfazer").hidden = !mostrar;
  const chave = JSON.stringify(mostrar ? pre : null);
  if ($("acoes-desfazer-previa").dataset.chave !== chave) {
    $("acoes-desfazer-previa").dataset.chave = chave;
    if (!mostrar) $("acoes-desfazer-previa").replaceChildren();
    else {
      const situacao = pre.pode_seguir ? etiqueta("OK", T("renderizar_desfazer.pode_desfazer")) : pre.nada_a_fazer ? etiqueta("NA", T("renderizar_desfazer.nada_a_desfazer")) : etiqueta("FALHA", T("renderizar_desfazer.bloqueado"));
      $("acoes-desfazer-previa").replaceChildren(el("p", {}, el("strong", { texto: T("renderizar_desfazer.desfazer") }), situacao),
        ...listaTitulada(T("renderizar_desfazer.bloqueios"), pre.bloqueios), ...listaTitulada(T("renderizar_desfazer.avisos"), pre.avisos),
        ...listaTitulada(ac.modo === "aplicar" ? T("renderizar_desfazer.o_que_volta") : T("renderizar_desfazer.o_que_voltaria_modo_interno"), pre.previa));
    }
  }
  $("acoes-desfazer-confirmar").hidden = !(mostrar && pre.pode_seguir);
  atualizarBotaoDesfazer();
}

function atualizarBotaoDesfazer() {
  const ac = (ESTADO && ESTADO.acoes) || {};
  const pre = ac.desfazer;
  $("acoes-desfazer-executar").disabled = ["prechecando", "executando"].includes(ac.situacao) || !pre || !pre.pode_seguir
    || $("acoes-desfazer-confirmacao").value.trim() !== "DESFAZER";
}

async function prechecarDesfazer() {
  const ac = (ESTADO && ESTADO.acoes) || {};
  if ($("acoes-desfazer-ver").disabled || ac.indice_resultado === null || ac.indice_resultado === undefined) return;
  $("acoes-desfazer-ver").disabled = true;
  $("acoes-desfazer-confirmacao").value = "";
  try { await api("acoes/desfazer/prechecar", "POST", { indice: ac.indice_resultado }); anunciar(T("prechecar_desfazer.lendo_os_valores_anteriores_so")); }
  catch (e) { $("acoes-mensagem").textContent = T("prechecar_desfazer.nada_foi_feito", e.message); anunciar(T("prechecar_desfazer.nada_foi_feito", e.message)); }
  atualizar();
}

async function executarDesfazer() {
  const ac = (ESTADO && ESTADO.acoes) || {};
  if ($("acoes-desfazer-executar").disabled) return;
  $("acoes-desfazer-executar").disabled = true;
  try {
    await api("acoes/desfazer", "POST", { indice: ac.indice_resultado, confirmacao: $("acoes-desfazer-confirmacao").value });
    $("acoes-desfazer-confirmacao").value = "";
    anunciar(ac.modo === "aplicar" ? T("executar_desfazer.desfazendo") : T("executar_desfazer.desfazer_iniciado_modo_interno_de"));
  } catch (e) { $("acoes-mensagem").textContent = T("executar_desfazer.nada_foi_feito", e.message); anunciar(T("executar_desfazer.nada_foi_feito", e.message)); }
  atualizar();
}

function atualizarBotaoAcao() {
  const ac = (ESTADO && ESTADO.acoes) || {};
  const pre = ac.prechecagem;
  const ocupado = ["prechecando", "executando"].includes(ac.situacao);
  $("acoes-executar").disabled = ocupado || !pre || !pre.pode_seguir || pre.acao !== acaoEscolhida()
    || $("acoes-confirmacao").value.trim() !== "CORRIGIR";
}

async function prechecarAcao() {
  const id = acaoEscolhida();
  if (!id || $("acoes-prechecar").disabled) return;
  $("acoes-prechecar").disabled = true;
  $("acoes-confirmacao").value = "";
  const parametros = !$("acoes-ntp").hidden ? { servidores: servidoresNtpEscolhidos() }
    : !$("acoes-ip").hidden ? { ip: $("acoes-ip-ip").value.trim(), mascara: $("acoes-ip-mascara").value.trim(), gateway: $("acoes-ip-gateway").value.trim() } : {};
  try { await api("acoes/prechecar", "POST", { id, parametros }); anunciar(T("prechecar_acao.pre_checagens_iniciadas_so_leitura")); }
  catch (e) { $("acoes-prechecagem").replaceChildren(el("p", { texto: T("prechecar_acao.nada_foi_feito", e.message) })); anunciar(T("prechecar_acao.nada_foi_feito", e.message)); }
  atualizar();
}

async function executarAcao() {
  if ($("acoes-executar").disabled) return;
  $("acoes-executar").disabled = true;
  try {
    await api("acoes/executar", "POST", { id: acaoEscolhida(), confirmacao: $("acoes-confirmacao").value });
    $("acoes-confirmacao").value = "";
    anunciar(T("executar_acao.acao_iniciada"));
  } catch (e) { $("acoes-mensagem").textContent = T("executar_acao.nada_foi_feito", e.message); $("acoes-resultado-cartao").hidden = false; anunciar(T("executar_acao.nada_foi_feito", e.message)); }
  atualizar();
}

async function iniciarDiagnostico() {
  if (ESTADO && (ESTADO.etapa === "diagnostico" || ESTADO.etapa === "concluido")) { ir("diagnostico"); return; }
  $("botao-iniciar").disabled = true;
  try {
    await api("diagnostico/iniciar", "POST");
    anunciar(T("iniciar_diagnostico.diagnostico_iniciado"));
    ir("diagnostico");
  } catch (e) { anunciar(T("iniciar_diagnostico.nao_foi_possivel_iniciar", e.message)); }
  atualizar();
}

/* ------------------------------------------------------------------ 3. diagnóstico */

let navComponentes = null;
const itensComponentes = new Map();

function statusDoComponente(c) {
  if (c.status) return c.status;
  return c.em_andamento ? "ANDAMENTO" : "PENDENTE";
}

/* Lista por gravidade (pedido de 14/10/2026): Falha, Atenção, Não verificado, Normal, Não aplicável; empate na ordem do
   núcleo. Só a apresentação: relatório, JSON e modo texto seguem a ordem do diagnóstico. */
const GRAVIDADE = { FALHA: 0, ALERTA: 1, NAO_VERIFICADO: 2, OK: 3, NAO_APLICAVEL: 4 };

function ordemPorGravidade(componentes) {
  return componentes.map((c, i) => [GRAVIDADE[statusDoComponente(c)] ?? 5, i, c.componente])
    .sort((a, b) => a[0] - b[0] || a[1] - b[1]).map((x) => x[2]);
}

function reordenarComponentes(lista, ordem) {
  const itens = ordem.map((nome) => itensComponentes.get(nome)).filter(Boolean);
  const atuais = lista.querySelectorAll(".item-componente");
  if (itens.length === atuais.length && itens.every((item, i) => atuais[i] === item)) return;
  const foco = document.activeElement;
  lista.append(...itens);
  if (foco && lista.contains(foco) && document.activeElement !== foco) foco.focus();  // mover o nó tira o foco
}

/* Onde está o problema (CLAUDE.md 15.3): mapa do chassi (vista de cima) com a área destacada por contorno grosso,
   cor e o texto "PROBLEMA" (nunca só a cor); sem mapa do modelo, só o texto; sem como localizar, diz isso. */
let localDesenhado = "";
let localAtual = null;

function svg(nome, atributos, ...filhos) {
  const n = document.createElementNS("http://www.w3.org/2000/svg", nome);
  for (const [k, v] of Object.entries(atributos || {})) n.setAttribute(k, String(v));
  for (const f of filhos) n.append(f);
  return n;
}

/* Estado escrito ao lado do nome da área, só se couber (~8 unidades por letra); senão fica no <title> e no estilo da borda. */
function sufixoCor(a, cor) {
  if (!cor || cor === "ok" || !ROTULO_COR[cor]) return "";
  const sufixo = " — " + ROTULO_COR[cor];
  return ((a.curto || a.nome.split(" (")[0]) + sufixo).length * 8 <= a.w - 12 ? sufixo : "";
}

/* cores (opcional, visão geral): id da área → ok/atencao/problema/nao_verificado, com o rótulo escrito (nunca só cor). */
function mapaChassi(mapa, destacadas, titulo, cores) {
  const largura = mapa.largura || 1000, altura = mapa.altura || 600;
  const raiz = svg("svg", { viewBox: "0 -28 " + largura + " " + (altura + 56), role: "img", "aria-label": titulo, class: "mapa-svg" });
  raiz.append(svg("rect", { x: 0, y: 30, width: largura - 43, height: altura - 60, rx: 8, class: "mapa-chassi-corpo" }));
  const rotulos = mapa.rotulos || {};
  raiz.append(svg("text", { x: 4, y: -8, class: "mapa-texto" }, (rotulos.frente || "FRENTE") + " ←"));
  raiz.append(svg("text", { x: largura - 60, y: -8, "text-anchor": "end", class: "mapa-texto" }, "→ " + (rotulos.traseira || "TRASEIRA")));
  // Destacadas por último (por cima), para o contorno e o texto não ficarem escondidos.
  const ordem = [...(mapa.areas || [])].sort((x, y) => Number(destacadas.has(x.id)) - Number(destacadas.has(y.id)));
  for (const a of ordem) {
    const marcada = destacadas.has(a.id);
    if (["risers", "chassi", "frente", "traseira"].includes(a.id) && !marcada) continue;
    if (marcada && a.id === "risers" && (destacadas.has("riser1") || destacadas.has("riser2"))) continue;
    const cor = cores && cores[a.id];
    const classe = cor && cor !== "ausente" ? "mapa-area mapa-cor-" + cor : marcada ? "mapa-area mapa-problema" : "mapa-area";
    raiz.append(svg("rect", { x: a.x, y: a.y, width: a.w, height: a.h, rx: 4, class: classe },
      svg("title", {}, a.nome + (ROTULO_COR[cor] ? ": " + ROTULO_COR[cor] : ""))));
    // Rótulo de área comum que cairia dentro de uma destacada: omitido (o nome continua no <title> e na lista).
    const coberto = !marcada && (mapa.areas || []).some((d) => destacadas.has(d.id) && d.id !== a.id
      && a.x + 6 >= d.x && a.x + 6 <= d.x + d.w && a.y + 20 >= d.y && a.y + 20 <= d.y + d.h);
    if (coberto) continue;
    raiz.append(svg("text", { x: a.x + 6, y: a.y + 20, class: marcada ? "mapa-texto mapa-texto-problema" : "mapa-texto" },
      (a.curto || a.nome.split(" (")[0]) + (marcada ? T("mapa_chassi.problema") : sufixoCor(a, cor))));
  }
  return raiz;
}

function itemLocal(i, comComponente) {
  return el("li", { classe: i.localizado ? "" : "texto-sec" },
    icone(i.localizado ? "location" : "question"), " ",
    comComponente ? el("span", { texto: (i.titulo || i.componente) + ": " }) : null,
    el("strong", { texto: i.local }), i.ocorrencias > 1 ? " (" + i.ocorrencias + "x)" : "",
    i.descricao_area ? el("div", { classe: "texto-sec pequeno", texto: i.descricao_area }) : null,
    el("div", { classe: "texto-sec pequeno", texto: i.problema }));
}

/* Modelo 3D (CLAUDE.md 18.1): com WebGL e fps bom, no lugar do mapa 2D; senão o 2D volta sozinho com um aviso discreto. */
/* Escolha 3D × 2D do técnico (19.4), lembrada neste navegador; recuo automático (sem WebGL, fps baixo) não é lembrado. */
let escolha2d = false;
try { escolha2d = window.localStorage.getItem("msscan-vista") === "2d"; } catch (e) { /* sem armazenamento */ }
let preferir2d = escolha2d, motivo2d = escolha2d ? T("preferir2d.mapa_2d_escolhido") : "";
const vistas3d = {};  // "diag" = Onde está o problema; "diag-geral" = Visão geral (pedido de 02/10/2026)

function mostrarPeca3d(caixa, peca) {
  const estado = { presente: "presente", ausente: "ausente (vazio)", desconhecido: T("mostrar_peca3d.presenca_nao_verificada") }[peca.estado] || peca.estado;
  caixa.replaceChildren(el("strong", { texto: peca.nome }), " — " + estado + ". ", peca.situacao ? el("span", { texto: peca.situacao + ". " }) : "",
    ...(peca.textos.length ? [el("ul", { classe: "lista-simples" }, ...peca.textos.map((t) => el("li", { texto: t })))]
      : [el("span", { texto: T("mostrar_peca3d.nenhum_problema_localizado_nesta_peca") })]));
  caixa.hidden = false;
}

/* Sem WebGL, fps baixo ou escolha do técnico: as duas vistas passam ao mapa 2D (a visão geral com as mesmas cores). */
function para2d(aviso) {
  preferir2d = true;
  motivo2d = aviso;
  for (const prefixo of Object.keys(vistas3d)) desenhar3d(prefixo, vistas3d[prefixo].dados);
}

function alternarVista() {
  escolha2d = !preferir2d;
  try { window.localStorage.setItem("msscan-vista", escolha2d ? "2d" : "3d"); } catch (e) { /* sem armazenamento */ }
  preferir2d = escolha2d;
  motivo2d = escolha2d ? T("alternar_vista.mapa_2d_escolhido") : "";
  for (const prefixo of Object.keys(vistas3d)) desenhar3d(prefixo, vistas3d[prefixo].dados);
  anunciar(escolha2d ? T("alternar_vista.mostrando_o_mapa_2d") : T("alternar_vista.mostrando_o_modelo_3d"));
}

function desenhar3d(prefixo, dados) {
  const vista = vistas3d[prefixo] || (vistas3d[prefixo] = { controle: null, dados: null });
  vista.dados = dados || null;
  if (vista.controle) { vista.controle.destruir(); vista.controle = null; }
  $(prefixo + "-3d-peca").hidden = true;
  const em2d = !dados || preferir2d || !window.Modelo3D;
  $(prefixo + "-3d-area").hidden = em2d;
  $(prefixo + "-mapa").hidden = !em2d;
  $(prefixo + "-3d-aviso").textContent = motivo2d;
  $(prefixo + "-3d-aviso").hidden = !(dados && preferir2d && motivo2d);
  const botao = $(prefixo + "-vista");
  botao.hidden = !dados || !window.Modelo3D || !window.Modelo3D.webgl();  // sem WebGL não há 3D para escolher
  botao.lastElementChild.textContent = em2d ? T("desenhar3d.ver_o_modelo_3d") : T("desenhar3d.ver_o_mapa_2d");
  const aproximado = $(prefixo + "-aproximado");
  aproximado.hidden = !(dados && dados.aproximado);
  aproximado.textContent = (dados && dados.aviso) || "";
  if (em2d) return;
  vista.controle = window.Modelo3D.montar($(prefixo + "-3d"), dados, {
    aoClicar: (peca) => mostrarPeca3d($(prefixo + "-3d-peca"), peca),
    aoMedir: (fps) => { $(prefixo + "-3d").dataset.fps = fps.toFixed(0); },
    aoFalhar: (motivo) => para2d(T("desenhar3d.mostrando_o_mapa_2d") + motivo + "."),
  });
}

/* Visão geral (pedido de 02/10/2026): indicador grande do estado geral + modelo 3D (ou mapa 2D com as mesmas cores),
   sempre ao fim do diagnóstico. Ícones SVG (Octicons) em vez de emoji: não depende de fonte do sistema. */
const INDICADOR_GERAL = { ok: ["OK", "check-circle"], atencao: ["ALERTA", "alert"], problema: ["FALHA", "x-circle"] };
const ROTULO_COR = { ok: T("rotulo_cor.normal"), atencao: T("rotulo_cor.atencao"), problema: T("rotulo_cor.falha"), nao_verificado: T("rotulo_cor.nao_verificado") };
let geralDesenhado = "";

function irParaComponente(componente) {
  $("busca").value = "";
  aplicarFiltro();
  const item = itensComponentes.get(componente);
  if (!item) return;
  item.scrollIntoView({ block: "center" });
  item.focus();
}

function renderizarGeral(relatorio, concluido) {
  const geral = relatorio && relatorio.estado_geral;
  $("diag-geral").hidden = !(concluido && geral);
  if (!concluido || !geral) { geralDesenhado = ""; return; }
  const dados = relatorio.modelo3d || null;
  const mapa = ((relatorio.localizacao || {}).mapa) || null;
  const chave = JSON.stringify([geral, dados]);
  if (chave === geralDesenhado) return;
  geralDesenhado = chave;
  const [classe, nomeIcone] = INDICADOR_GERAL[geral.nivel] || INDICADOR_GERAL.atencao;
  const atalho = geral.alvo ? el("button", { type: "button", classe: "botao botao-secundario botao-pequeno", id: "diag-geral-atalho" },
    el("span", { texto: T("renderizar_geral.ver_na_lista") + (geral.alvo_titulo || geral.alvo) }), icone("chevron-right")) : null;
  if (atalho) atalho.addEventListener("click", () => irParaComponente(geral.alvo));
  $("diag-geral-indicador").className = "geral-indicador st-" + classe;
  $("diag-geral-indicador").replaceChildren(icone(nomeIcone, "geral-icone"),
    el("div", {}, el("strong", { classe: "geral-titulo", texto: geral.titulo }), el("p", { texto: geral.texto }), atalho));
  if (dados) {
    $("diag-geral-mapa").replaceChildren(mapa ? mapaChassi(mapa, new Set(), T("renderizar_geral.mapa_do") + (dados.modelo || "chassi") + T("renderizar_geral.com_o_estado_de_cada"), dados.areas || {}) : "");
    $("diag-geral-nota").textContent = T("renderizar_geral.verde_ok_amarelo_atencao_alerta")
      + T("renderizar_geral.cinza_nao_verificado_contorno_escuro");
  } else {
    $("diag-geral-mapa").replaceChildren();
    $("diag-geral-nota").textContent = T("renderizar_geral.sem_modelo_3d_para_este");
  }
  $("diag-geral-modelo").hidden = !dados;
  desenhar3d("diag-geral", dados);
}

function renderizarLocalizacao(loc, dados3d) {
  localAtual = loc || null;
  const chave = JSON.stringify([loc || null, dados3d || null]);
  if (chave === localDesenhado) return;
  localDesenhado = chave;
  const itens = (loc && loc.itens) || [];
  $("diag-local").hidden = !itens.length && !dados3d;
  desenhar3d("diag", dados3d || null);
  if (!itens.length && !dados3d) return;
  const areas = new Set(itens.filter((i) => i.area).map((i) => i.area));
  $("diag-mapa").replaceChildren(loc.mapa ? mapaChassi(loc.mapa, areas, T("renderizar_localizacao.mapa_do") + (loc.modelo || "chassi") + ", " + loc.mapa.vista
    + T("renderizar_localizacao.areas_com_problema_destacadas")) : "");
  $("diag-mapa-nota").textContent = loc.mapa ? (loc.aproximado ? T("renderizar_localizacao.posicoes_aproximadas") : "") + T("renderizar_localizacao.mapa_do") + (loc.modelo || "chassi") + " (" + loc.mapa.vista + T("renderizar_localizacao.area_com_contorno_vermelho_grosso")
    : T("renderizar_localizacao.sem_mapa_do_chassi_para");
  $("diag-local-lista").replaceChildren(...itens.map((i) => itemLocal(i, true)));
}

function renderizarDiagnostico(estado) {
  const diag = estado.diagnostico;
  const lista = $("diag-componentes");
  for (const c of diag.componentes) {
    let item = itensComponentes.get(c.componente);
    if (!item) {
      item = el("li", { classe: "item-componente", role: "option", tabindex: "-1", "aria-selected": "false", "data-componente": c.componente },
        el("span", { classe: "nome" }, el("span", { classe: "col-nome" }, el("strong"), el("span", { classe: "mensagem" }))),
        el("span", { classe: "col-status" }),
        el("span", { classe: "fonte" }));
      itensComponentes.set(c.componente, item);
      lista.append(item);
    }
    const status = statusDoComponente(c);
    const pronto = Boolean(c.status);
    item.querySelector("strong").textContent = c.titulo;
    item.querySelector(".mensagem").textContent = c.mensagem || (c.em_andamento ? T("renderizar_diagnostico.diagnosticando") : "");
    const coluna = item.querySelector(".col-status");
    if (coluna.dataset.status !== status) { coluna.dataset.status = status; coluna.replaceChildren(etiqueta(status)); }
    // A coluna já se chama "Origem": o prefixo fica só para o leitor de tela; o title mostra a origem que não coube.
    const fonte = item.querySelector(".fonte");
    if (fonte.dataset.valor !== (c.fonte || "")) {
      fonte.dataset.valor = c.fonte || "";
      fonte.replaceChildren(...(c.fonte ? [el("span", { classe: "visualmente-oculto", texto: T("renderizar_diagnostico.fonte") }), c.fonte] : []));
      fonte.title = c.fonte || "";
    }
    item.setAttribute("aria-disabled", pronto ? "false" : "true");
    item.setAttribute("aria-label", c.titulo + ": " + (STATUS[status] || {}).texto + (c.fonte ? ", fonte " + c.fonte : "")
      + (pronto ? T("renderizar_diagnostico.enter_abre_os_detalhes") : ""));
    item.dataset.busca = (c.titulo + " " + c.componente + " " + (c.fonte || "") + " " + ((STATUS[status] || {}).texto || "")).toLowerCase();
  }
  navComponentes = navComponentes || navegavel(lista, ".item-componente", (item) => {
    if (item.getAttribute("aria-disabled") === "true") return;
    ir("diagnostico/" + encodeURIComponent(item.dataset.componente));
  });
  // Durante a coleta as linhas não mudam de lugar (o técnico acompanha o andamento); a ordem por gravidade vem no fim.
  if (estado.etapa !== "diagnostico") reordenarComponentes(lista, ordemPorGravidade(diag.componentes));
  aplicarFiltro();
  navComponentes.garantirTabindex();

  const barra = $("diag-progresso");
  barra.setAttribute("aria-valuemax", String(diag.total));
  barra.setAttribute("aria-valuenow", String(diag.concluidos));
  $("diag-progresso-preenchido").style.width = (diag.total ? (100 * diag.concluidos / diag.total) : 0) + "%";
  const atual = diag.componentes.find((c) => c.em_andamento);
  let texto = T("renderizar_diagnostico.diagnostico_nao_iniciado");
  if (estado.etapa === "diagnostico") texto = T("renderizar_diagnostico.diagnosticando_2") + (atual ? ": " + atual.titulo : "…") + " (" + diag.concluidos + "/" + diag.total + ")";
  else if (estado.etapa === "concluido") texto = T("renderizar_diagnostico.concluido") + diag.concluidos + "/" + diag.total + " componentes";
  else if (estado.etapa === "pronto") texto = T("renderizar_diagnostico.pronto_para_iniciar_tela_inicio");
  const textoProgresso = $("diag-progresso-texto");
  if (textoProgresso.textContent !== texto) textoProgresso.textContent = texto;

  const veredito = $("diag-veredito");
  veredito.hidden = !diag.veredito;
  if (diag.veredito && veredito.dataset.texto !== diag.veredito) {
    veredito.dataset.texto = diag.veredito;
    const c = estado.relatorio ? estado.relatorio.contagem : {};
    const status = c.FALHA ? "FALHA" : c.ALERTA ? "ALERTA" : c.NAO_VERIFICADO ? "NAO_VERIFICADO" : "OK";
    veredito.replaceChildren(etiqueta(status), el("span", { texto: diag.veredito }));
    anunciar(diag.veredito);
  }
}

/* Refazer diagnóstico (CLAUDE.md 7b): tudo ou um componente; comparação antes × depois. */
const TENDENCIA = { melhorou: ["OK", "check", T("tendencia.melhorou")], piorou: ["FALHA", "alert", T("tendencia.piorou")], igual: ["NA", "dot-fill", T("tendencia.igual")] };

function renderizarRefazer(estado) {
  const diag = estado.diagnostico;
  const escopo = $("diag-refazer-escopo");
  if (escopo.options.length === 1) {
    for (const c of diag.componentes) escopo.append(el("option", { value: c.componente, texto: T("renderizar_refazer.so") + c.titulo }));
  }
  const cartaoRefazer = $("diag-refazer");
  // Aparece ao fim do primeiro diagnóstico e continua visível (desabilitado) enquanto um refazer roda.
  cartaoRefazer.hidden = !(estado.etapa === "concluido" || (estado.etapa === "diagnostico" && !cartaoRefazer.hidden));
  const livre = estado.etapa === "concluido";
  // aria-disabled (e não disabled): o foco continua no botão enquanto o refazer roda.
  escopo.setAttribute("aria-disabled", livre ? "false" : "true");
  $("diag-refazer-botao").setAttribute("aria-disabled", livre ? "false" : "true");
  $("diag-refazer-botao").lastElementChild.textContent = livre ? T("renderizar_refazer.refazer_diagnostico") : T("renderizar_refazer.refazendo");

  renderizarLocalizacao((estado.relatorio || {}).localizacao, (estado.relatorio || {}).modelo3d);
  renderizarGeral(estado.relatorio, estado.etapa === "concluido");

  const comp = diag.comparacao;
  const cartao = $("diag-comparacao");
  cartao.hidden = !comp;
  const chave = comp ? JSON.stringify(comp) : "";
  if (!comp || cartao.dataset.chave === chave) return;
  cartao.dataset.chave = chave;
  const r = comp.resumo || {};
  const alvo = comp.escopo === "tudo" ? T("renderizar_refazer.diagnostico_completo_refeito") : T("renderizar_refazer.refeito_so") + comp.componentes.map((c) => c.titulo).join(", ");
  const resumo = alvo + " — " + (r.melhorou || 0) + " melhorou, " + (r.piorou || 0) + " piorou, " + (r.igual || 0)
    + T("renderizar_refazer.igual_antes") + (comp.anterior.veredito || "?") + " (" + (comp.anterior.inicio || "?") + ")."
    + (comp.anterior.pasta ? T("renderizar_refazer.relatorio_anterior_guardado_em") + comp.anterior.pasta + "." : "");
  $("diag-comparacao-resumo").textContent = T("renderizar_refazer.comparacao") + resumo;
  $("diag-comparacao-linhas").replaceChildren(...comp.componentes.map((c) => {
    const [classe, nomeIcone, texto] = TENDENCIA[c.tendencia] || TENDENCIA.igual;
    return el("tr", {},
      el("td", { texto: c.titulo }),
      el("td", {}, c.status_antes ? etiqueta(c.status_antes) : "—"),
      el("td", {}, c.status_depois ? etiqueta(c.status_depois) : "—"),
      el("td", {}, etiquetaLivre(classe, nomeIcone, texto)));
  }));
  anunciar(T("renderizar_refazer.comparacao_antes_depois") + resumo);
}

async function refazerDiagnostico() {
  if ($("diag-refazer-botao").getAttribute("aria-disabled") === "true") return;
  const componente = $("diag-refazer-escopo").value || null;
  $("diag-refazer-botao").setAttribute("aria-disabled", "true");
  try {
    await api("diagnostico/refazer", "POST", { componente });
    anunciar(componente ? T("refazer_diagnostico.refazendo_o_diagnostico_de") + $("diag-refazer-escopo").selectedOptions[0].textContent.replace(/^Só /, "")
      : T("refazer_diagnostico.refazendo_o_diagnostico_completo"));
  } catch (e) { anunciar(T("refazer_diagnostico.nao_foi_possivel_refazer", e.message)); }
  atualizar();
}

function aplicarFiltro() {
  const termo = $("busca").value.trim().toLowerCase();
  for (const item of itensComponentes.values()) item.hidden = Boolean(termo) && !item.dataset.busca.includes(termo);
  aplicarLimites();
}

let detalheAtual = null;

/* Mostra a lista ou o detalhe. Devolve true quando cuidou do foco. */
function mostrarDetalhe(nome, mudou) {
  $("diag-lista-area").hidden = Boolean(nome);
  $("diag-detalhe-area").hidden = !nome;
  if (!nome) {
    const origem = detalheAtual;
    detalheAtual = null;
    if (mudou && origem && itensComponentes.has(origem)) { navComponentes.focar(itensComponentes.get(origem)); return true; }
    return false;
  }
  if (!mudou && detalheAtual === nome) return false;
  detalheAtual = nome;
  carregarDetalhe(nome);
  return true;
}

async function carregarDetalhe(nome) {
  $("detalhe-titulo").textContent = T("carregar_detalhe.carregando");
  let dados;
  try { dados = await api("componente/" + encodeURIComponent(nome)); } catch (e) {
    $("detalhe-titulo").textContent = T("carregar_detalhe.componente_ainda_nao_diagnosticado");
    $("detalhe-resumo").textContent = e.message;
    for (const p of ["dados", "tentativas", "comandos"]) $("painel-" + p).replaceChildren();
    $("detalhe-titulo").focus();
    return;
  }
  if (detalheAtual !== nome) return; /* o técnico já voltou ou abriu outro */
  $("detalhe-titulo").textContent = dados.titulo;
  $("detalhe-status").replaceChildren(etiqueta(dados.status));
  $("detalhe-resumo").textContent = [dados.fonte ? T("carregar_detalhe.fonte") + dados.fonte : T("carregar_detalhe.sem_fonte"), dados.mensagem].filter(Boolean).join(" · ");
  const locais = ((localAtual && localAtual.itens) || []).filter((i) => i.componente === nome);
  $("detalhe-local").hidden = !locais.length;
  $("detalhe-local").replaceChildren(...locais.map((i) => itemLocal(i, false)));
  const tentativas = dados.dados.tentativas || [];
  $("contador-tentativas").textContent = String(tentativas.length);
  $("contador-comandos").textContent = String(dados.comandos.length);
  renderizarDados($("painel-dados"), dados.dados);
  renderizarTabela($("painel-tentativas"), [T("carregar_detalhe.provedor"), T("carregar_detalhe.resultado"), T("carregar_detalhe.detalhe")],
    tentativas.map((t) => [t.provedor, t.resultado, t.detalhe || "—"]), T("carregar_detalhe.nenhuma_tentativa_registrada"));
  renderizarTabela($("painel-comandos"), [T("carregar_detalhe.comando"), T("carregar_detalhe.codigo"), T("carregar_detalhe.duracao"), T("carregar_detalhe.erro")],
    dados.comandos.map((c) => [c.comando.join(" "), String(c.rc), formatarDuracao(c.duracao), c.erro || ""]),
    T("carregar_detalhe.nenhum_comando_executado_neste_componente"), 0);
  ativarAbaDetalhe($("aba-dados"), false);
  $("detalhe-titulo").focus();
  anunciar(T("carregar_detalhe.detalhes_de") + dados.titulo);
}

function formatarDuracao(segundos) {
  return typeof segundos === "number" ? segundos.toFixed(2) + " s" : "—";
}

function ativarAbaDetalhe(aba, focar) {
  for (const outra of document.querySelectorAll("#detalhe-abas [role=tab]")) {
    const ativa = outra === aba;
    outra.setAttribute("aria-selected", ativa ? "true" : "false");
    outra.tabIndex = ativa ? 0 : -1;
    $("painel-" + outra.dataset.painel).hidden = !ativa;
  }
  if (focar) aba.focus();
}

function renderizarTabela(painel, colunas, linhas, vazio, colunaMono) {
  if (!linhas.length) { painel.replaceChildren(el("p", { classe: "texto-sec", texto: vazio })); return; }
  const corpo = el("tbody", {}, linhas.map((linha) => el("tr", { tabindex: "-1" },
    linha.map((valor, i) => el("td", { classe: i === colunaMono ? "mono cmd" : "", texto: valor })))));
  const tabela = el("table", { classe: "tabela" },
    el("thead", {}, el("tr", {}, colunas.map((c) => el("th", { scope: "col", texto: c })))), corpo);
  painel.replaceChildren(el("p", { classe: "texto-sec pequeno", texto: T("renderizar_tabela.setas_percorrem_as_linhas") }), el("div", { classe: "tabela-rolagem" }, tabela));
  navegavel(corpo, "tr", null).garantirTabindex();
}

/* Dados de um componente: primeiro problemas e explicações; depois o resto, de forma genérica. */
function renderizarDados(painel, dados) {
  const bloco = el("div", { classe: "bloco-dados" });
  if (dados.problemas && dados.problemas.length) {
    bloco.append(el("h3", { texto: T("renderizar_dados.problemas") }), el("ul", { classe: "lista-simples" }, dados.problemas.map((p) => el("li", { texto: textoDe(p) }))));
  }
  for (const explicacao of dados.explicacoes || []) bloco.append(el("p", { classe: "explicacao", texto: textoDe(explicacao) }));
  const resto = Object.entries(dados).filter(([chave]) => !["tentativas", "problemas", "explicacoes"].includes(chave));
  const simples = resto.filter(([, valor]) => !ehEstruturado(valor));
  if (simples.length) {
    const lista = el("dl", { classe: "chave-valor" });
    for (const [chave, valor] of simples) lista.append(el("dt", { texto: humanizar(chave) }), el("dd", {}, valorParaNo(valor, 0)));
    bloco.append(el("h3", { texto: T("renderizar_dados.resumo_medido") }), lista);
  }
  /* Tabelas e objetos ocupam a largura toda, cada um com o seu título. */
  for (const [chave, valor] of resto.filter(([, v]) => ehEstruturado(v))) {
    bloco.append(el("h3", { texto: humanizar(chave) }), valorParaNo(valor, 0));
  }
  if (!bloco.childElementCount) bloco.append(el("p", { classe: "texto-sec", texto: T("renderizar_dados.nenhum_dado_medido") }));
  painel.replaceChildren(bloco);
  for (const corpo of painel.querySelectorAll("tbody")) navegavel(corpo, "tr", null).garantirTabindex();
}

function ehEstruturado(valor) {
  if (Array.isArray(valor)) return valor.some((v) => v && typeof v === "object");
  return Boolean(valor) && typeof valor === "object" && Object.keys(valor).length > 0;
}

function humanizar(chave) {
  const texto = String(chave).replace(/_/g, " ");
  return texto.charAt(0).toUpperCase() + texto.slice(1);
}

function textoDe(valor) {
  return typeof valor === "string" ? valor : JSON.stringify(valor);
}

function valorParaNo(valor, profundidade) {
  if (valor === null || valor === undefined || valor === "") return el("span", { classe: "valor-nulo", texto: "—" });
  if (typeof valor === "boolean") return document.createTextNode(valor ? "sim" : T("valor_para_no.nao"));
  if (typeof valor !== "object") return document.createTextNode(String(valor));
  if (profundidade > 3) return el("code", { texto: JSON.stringify(valor) });
  if (Array.isArray(valor)) {
    if (!valor.length) return el("span", { classe: "valor-nulo", texto: "(vazio)" });
    const objetos = valor.every((v) => v && typeof v === "object" && !Array.isArray(v));
    if (objetos) {
      const colunas = [...new Set(valor.flatMap((v) => Object.keys(v)))];
      if (colunas.length <= 10) {
        const corpo = el("tbody", {}, valor.map((linha) => el("tr", { tabindex: "-1" },
          colunas.map((c) => el("td", {}, valorParaNo(linha[c], profundidade + 1))))));
        return el("div", { classe: "tabela-rolagem" }, el("table", { classe: "tabela" },
          el("thead", {}, el("tr", {}, colunas.map((c) => el("th", { scope: "col", texto: humanizar(c) })))), corpo));
      }
    }
    return el("ul", { classe: "lista-simples" }, valor.map((v) => el("li", {}, valorParaNo(v, profundidade + 1))));
  }
  const lista = el("dl", { classe: "chave-valor" });
  for (const [chave, v] of Object.entries(valor)) lista.append(el("dt", { texto: humanizar(chave) }), el("dd", {}, valorParaNo(v, profundidade + 1)));
  return lista;
}

/* ------------------------------------------------------------------ 4. relatórios */

let relatorioDesenhado = "";

function renderizarRelatorios(estado) {
  const rel = estado.relatorio;
  const chave = JSON.stringify(rel) + estado.etapa;
  if (chave === relatorioDesenhado) return;
  relatorioDesenhado = chave;
  const corpo = $("rel-corpo");
  if (!rel) {
    corpo.replaceChildren(el("p", { classe: "texto-sec", texto: estado.etapa === "diagnostico"
      ? T("renderizar_relatorios.diagnostico_em_andamento_o_relatorio") : T("renderizar_relatorios.o_relatorio_e_gerado_ao") }));
    return;
  }
  const c = rel.contagem;
  const status = c.FALHA ? "FALHA" : c.ALERTA ? "ALERTA" : c.NAO_VERIFICADO ? "NAO_VERIFICADO" : "OK";
  const contagem = el("p", {}, ["OK", "ALERTA", "FALHA", "NAO_VERIFICADO", "NAO_APLICAVEL"].map((s) =>
    el("span", { style: null }, etiqueta(s, (STATUS[s].texto) + ": " + (c[s] || 0)), " ")));
  const local = rel.pasta
    ? [el("p", { texto: rel.no_pendrive ? T("renderizar_relatorios.gravado_no_pendrive") : T("renderizar_relatorios.gravado_fora_do_pendrive_sera") }),
      el("p", { classe: "caminho mono", texto: rel.pasta }),
      el("p", { classe: "texto-sec pequeno", texto: T("renderizar_relatorios.arquivos_relatorio_html_relatorio_json") })]
    : [el("p", { classe: "texto-sec", texto: rel.mensagem })];
  const aviso = rel.pasta && !rel.no_pendrive ? el("div", { classe: "aviso aviso-alerta", texto: rel.mensagem }) : null;
  const botao = el("button", { type: "button", classe: "botao botao-primario", id: "rel-abrir", onclick: () => ir("relatorios/ver") },
    icone("file"), T("renderizar_relatorios.abrir_relatorio_html"));
  corpo.replaceChildren(el("p", { classe: "veredito" }, etiqueta(status), el("span", { texto: rel.veredito })), contagem, ...(aviso ? [aviso] : []), ...local,
    el("div", { classe: "acoes" }, botao));
}

/* Mostra o resumo ou o relatório embutido. Devolve true quando cuidou do foco. */
function mostrarVisor(ver, mudou) {
  const mostrar = Boolean(ver && ESTADO && ESTADO.relatorio);
  $("rel-resumo-area").hidden = mostrar;
  $("rel-visor-area").hidden = !mostrar;
  if (!mudou) return false;
  const visor = $("rel-visor");
  if (mostrar) {
    visor.src = "/relatorio.html?t=" + encodeURIComponent(TOKEN);
    $("rel-fechar").focus();
    return true;
  }
  const vinhaDoVisor = visor.hasAttribute("src");
  visor.removeAttribute("src");
  const abrir = $("rel-abrir");
  if (vinhaDoVisor && abrir) { abrir.focus(); return true; }
  return false;
}

/* ------------------------------------------------------------------ 7. atalhos */

const ATALHOS = [
  [["?"], T("atalhos.mostrar_os_atalhos_de_teclado")],
  [["/"], T("atalhos.filtrar_componentes_tela_diagnostico")],
  [["Alt", "1…7"], T("atalhos.espacos_de_trabalho")],
  [["g", "i"], T("atalhos.ir_para_inicio")],
  [["g", "d"], T("atalhos.ir_para_diagnostico")],
  [["g", "r"], T("atalhos.ir_para_relatorios")],
  [["g", "c"], T("atalhos.ir_para_o_chat_com")],
  [["g", "a"], T("atalhos.ir_para_atualizacoes")],
  [["g", "t"], T("atalhos.ir_para_o_terminal_f6")],
  [["g", "s"], T("atalhos.ir_para_atalhos_sobre_tela")],
  [["Tab"], T("atalhos.proximo_elemento_shift_tab_anterior")],
  [["←", "→"], T("atalhos.trocar_de_aba")],
  [["↑", "↓"], T("atalhos.percorrer_listas_e_tabelas_home")],
  [["Enter"], T("atalhos.abrir_o_item_selecionado")],
  [["Esc"], T("atalhos.voltar_fechar")],
  [["Ctrl", "+"], T("atalhos.aumentar_o_zoom_ctrl_diminui")],
];

function tabelaAtalhos() {
  return el("table", { classe: "atalhos" }, el("tbody", {}, ATALHOS.map(([teclas, descricao]) => el("tr", {},
    el("td", {}, teclas.map((t, i) => [i ? " " : null, el("kbd", { texto: t })])),
    el("td", { texto: descricao })))));
}

let focoAntesDoDialogo = null;

function abrirAtalhos() {
  if (!$("dialogo-fundo").hidden) return;
  focoAntesDoDialogo = document.activeElement;
  $("dialogo-fundo").hidden = false;
  $("dialogo-fechar").focus();
}

function fecharAtalhos() {
  $("dialogo-fundo").hidden = true;
  if (focoAntesDoDialogo && document.contains(focoAntesDoDialogo)) focoAntesDoDialogo.focus();
}

/* ------------------------------------------------------------------ teclado global */

let prefixoG = 0;

function digitando(alvo) {
  return alvo && (alvo.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(alvo.tagName));
}

/* Elementos que recebem Tab, na ordem, dentro de raiz (ignora ocultos e desabilitados). */
function focaveis(raiz) {
  const seletor = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), details > summary, [tabindex="0"]';
  return Array.from(raiz.querySelectorAll(seletor))
    .filter((e) => e.tabIndex >= 0 && e.getClientRects().length > 0 && !e.closest("[hidden]") && getComputedStyle(e).visibility !== "hidden");
}

/*
 * No modo quiosque não há barra do navegador visível: o Tab que sairia da página
 * (e deixaria o técnico sem foco na tela) volta para o primeiro/último elemento.
 */
function cicloDeTab(ev) {
  const lista = focaveis(document.body);
  if (!lista.length) return;
  const primeiro = lista[0];
  const ultimo = lista[lista.length - 1];
  const ativo = document.activeElement;
  if (ev.shiftKey && (ativo === primeiro || ativo === document.body)) { ev.preventDefault(); ultimo.focus(); }
  else if (!ev.shiftKey && ativo === ultimo) { ev.preventDefault(); primeiro.focus(); }
}

/* ------------------------------------------------------------------ zoom (escala da interface) */

/*
 * Escala automática pela resolução (CLAUDE.md 12c, item 4): o layout foi pensado para 800x600–1280x800;
 * em telas maiores tudo cresce na mesma proporção (texto legível em 1920x1080), nunca diminui abaixo de
 * 100% (800x600 continua sem rolagem horizontal). Ctrl +/− somam um ajuste do técnico; Ctrl 0 zera.
 */
const ZOOM_MIN = 0.5, ZOOM_MAX = 3, ZOOM_PASSO = 0.1;
let zoomAjuste = 0;

function zoomAutomatico() {
  const fator = Math.min(window.innerWidth / 1280, window.innerHeight / 800);
  return Math.max(1, Math.min(2, Math.floor(fator * 8) / 8));
}

function zoomAtual() {
  return Math.round(Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, zoomAutomatico() + zoomAjuste)) * 100) / 100;
}

function aplicarZoom(anunciarMudanca) {
  const zoom = zoomAtual();
  document.documentElement.style.zoom = String(zoom);
  document.documentElement.style.setProperty("--ms-vh", Math.floor(window.innerHeight / zoom) + "px");
  ajustarLayout();
  const texto = Math.round(zoom * 100) + "%" + (zoomAjuste ? " (ajustado)" : T("aplicar_zoom.automatico"));
  if ($("tela-zoom")) $("tela-zoom").textContent = texto;
  if (anunciarMudanca) anunciar(T("aplicar_zoom.zoom") + texto);
}

function ajustarZoom(delta) {
  zoomAjuste = delta === 0 ? 0 : Math.round((zoomAjuste + delta) * 100) / 100;
  try { window.localStorage.setItem("msscan-zoom-ajuste", String(zoomAjuste)); } catch (e) { /* sem armazenamento */ }
  aplicarZoom(true);
}

function teclaDeZoom(ev) {
  if (!ev.ctrlKey || ev.altKey || ev.metaKey) return false;
  if (["+", "=", "Add"].includes(ev.key)) ajustarZoom(ZOOM_PASSO);
  else if (["-", "_", "Subtract"].includes(ev.key)) ajustarZoom(-ZOOM_PASSO);
  else if (ev.key === "0") ajustarZoom(0);
  else return false;
  ev.preventDefault(); // o zoom é da interface, não o do navegador
  return true;
}

/* ------------------------------------------------------------------ tela (resolução, na aba Sobre) */

let telaEstado = null;
let telaVigia = 0;

/* Na aba Sobre, a tela é consultada a cada segundo (contagem de confirmação e mudança de resolução). */
function vigiarTela() {
  window.clearTimeout(telaVigia);
  if (rotaAtual !== "sobre") return;
  carregarTela();
  telaVigia = window.setTimeout(vigiarTela, 1000);
}

async function carregarTela() {
  try { telaEstado = await api("tela"); } catch (e) { telaEstado = { disponivel: false, motivo: e.message }; }
  renderizarTela();
}

function renderizarTela() {
  const t = telaEstado;
  if (!t) return;
  const auto = t.automatico || {};
  $("tela-atual").textContent = t.atual || (auto.modo || "—");
  $("tela-origem").textContent = t.disponivel
    ? "(" + t.saida + (t.edid ? T("renderizar_tela.monitor_com_edid") : T("renderizar_tela.monitor_sem_edid_modo_seguro")) + ")"
    : (t.motivo ? "(" + t.motivo + ")" : "");
  $("tela-troca").hidden = !t.disponivel;
  if (t.disponivel) {
    const selecionado = $("tela-modos").value;
    $("tela-modos").replaceChildren(...t.modos.filter((m) => !m.entrelacado).map((m) =>
      el("option", { value: m.nome, texto: m.nome + (m.preferido ? " (preferido)" : "") + (m.atual ? T("renderizar_tela.em_uso") : "") })));
    // Mantém a escolha do técnico; se a resolução em uso mudou (troca ou volta automática), mostra a em uso.
    const mudou = renderizarTela.atual !== t.atual;
    renderizarTela.atual = t.atual;
    $("tela-modos").value = !mudou && selecionado && t.modos.some((m) => m.nome === selecionado) ? selecionado : (t.atual || "");
  }
  const pendente = t.pendente;
  $("tela-confirmar").hidden = !pendente;
  $("tela-mensagem").textContent = pendente ? "" : (t.mensagem || "");
  if (pendente) atualizarContagemTela();
}

function atualizarContagemTela() {
  const pendente = telaEstado && telaEstado.pendente;
  if (!pendente) return;
  $("tela-confirmar-texto").textContent = T("atualizar_contagem_tela.resolucao") + pendente.modo + T("atualizar_contagem_tela.aplicada_a_imagem_esta_boa")
    + pendente.anterior + T("atualizar_contagem_tela.em") + Math.max(0, pendente.restante_s) + " s.";
}

async function aplicarResolucao() {
  const modo = $("tela-modos").value;
  if (!modo) return;
  try {
    await api("tela/modo", "POST", { modo: modo });
    await carregarTela();
    $("tela-manter").focus();
  } catch (e) { $("tela-mensagem").textContent = T("aplicar_resolucao.nao_foi_possivel_aplicar", e.message); anunciar($("tela-mensagem").textContent); }
}

async function respostaResolucao(rota) {
  try { await api(rota, "POST"); } catch (e) { /* o servidor já voltou sozinho */ }
  await carregarTela();
  anunciar($("tela-mensagem").textContent);
  $("tela-aplicar").focus();
}

function teclaGlobal(ev) {
  if (teclaDeZoom(ev)) return;
  if (!$("aviso-fundo").hidden) {
    if (ev.key === "Escape") { ev.preventDefault(); fecharAviso(true); }
    else if (ev.key === "Tab") {
      const lista = focaveis($("aviso-dialogo"));
      const atual = lista.indexOf(document.activeElement);
      ev.preventDefault();
      lista[(atual + (ev.shiftKey ? lista.length - 1 : 1)) % lista.length].focus();
    }
    return;
  }
  if (!$("energia-fundo").hidden) {
    if (ev.key === "Escape") { ev.preventDefault(); fecharEnergia(); }
    else if (ev.key === "Tab") {
      const botoes = [$("energia-cancelar"), $("energia-confirmar")].filter((b) => !b.hidden && !b.disabled);
      ev.preventDefault();
      botoes[(botoes.indexOf(document.activeElement) + 1) % botoes.length].focus();
    }
    return;
  }
  // Dentro do terminal toda tecla é do shell (Esc, Tab, setas); F6 sai (teclaTerminal).
  if (ev.target && ev.target.closest && ev.target.closest("#terminal-area")) return;
  if (ev.key === "Escape" && !$("menu-energia").hidden) { ev.preventDefault(); abrirMenuEnergia(false); $("botao-energia").focus(); return; }
  if (ev.key === "Escape" && gavetaAberta()) { ev.preventDefault(); fecharGavetas(true); return; }
  // Alt+1 a Alt+7: espaços de trabalho (as etapas), como no Hyprland.
  if (ev.altKey && !ev.ctrlKey && !ev.metaKey && /^Digit[1-7]$/.test(ev.code) && $("dialogo-fundo").hidden) {
    ev.preventDefault();
    if (redeBloqueia(ESTADO)) { anunciar(T("iniciar.resolva_a_rede_primeiro")); return; }
    ir(ESPACOS[Number(ev.code.slice(5)) - 1]);
    window.setTimeout(() => $("conteudo").focus(), 0);
    return;
  }
  const dialogoAberto = !$("dialogo-fundo").hidden;
  if (ev.key === "Escape") {
    if (dialogoAberto) { fecharAtalhos(); ev.preventDefault(); return; }
    if (ev.target === $("busca") && $("busca").value) { $("busca").value = ""; aplicarFiltro(); ev.preventDefault(); return; }
    if (rotaAtual.startsWith("diagnostico/")) { ir("diagnostico"); ev.preventDefault(); return; }
    if (rotaAtual === "relatorios/ver") { ir("relatorios"); ev.preventDefault(); return; }
    if (rotaAtual === "ia/configurar" && !$("ia-chave-painel").hidden) { fecharChaveIA(); ev.preventDefault(); return; }
    if (rotaAtual === "ia/configurar") { ir("ia"); ev.preventDefault(); return; }
    if (rotaAtual === "rede" && !redeBloqueia(ESTADO)) { ir("inicio"); ev.preventDefault(); return; }
    if (digitando(ev.target)) { ev.target.blur(); ev.preventDefault(); }
    return;
  }
  if (dialogoAberto) {
    if (ev.key === "Tab") { ev.preventDefault(); $("dialogo-fechar").focus(); }
    return;
  }
  if (ev.key === "Tab") { cicloDeTab(ev); return; }
  if (digitando(ev.target) || ev.ctrlKey || ev.altKey || ev.metaKey) return;
  if (prefixoG && Date.now() - prefixoG < 1500) {
    prefixoG = 0;
    const destino = { i: "inicio", d: "diagnostico", r: "relatorios", c: "chat", a: "atualizacoes", t: "terminal", s: "sobre" }[ev.key];
    if (destino) { ev.preventDefault(); ir(destino); window.setTimeout(() => $("conteudo").focus(), 0); }
    return;
  }
  if (ev.key === "g") { prefixoG = Date.now(); return; }
  if (ev.key === "?") { ev.preventDefault(); abrirAtalhos(); return; }
  if (ev.key === "/") {
    ev.preventDefault();
    if (rotaAtual !== "diagnostico") ir("diagnostico");
    window.setTimeout(() => $("busca").focus(), 0);
  }
}

/* ------------------------------------------------------------------ ciclo de atualização */

let temporizador = 0;

async function atualizar() {
  window.clearTimeout(temporizador);
  let estado = null;
  try { estado = await api("estado"); } catch (e) {
    $("indicador-rede-texto").textContent = T("atualizar.sem_contato_com_o_msscan");
    temporizador = window.setTimeout(atualizar, 2000);
    return;
  }
  const bloqueavaAntes = redeBloqueia(ESTADO);
  ESTADO = estado;
  $("sobre-versao").textContent = "v" + estado.versao;
  $("rodape-versao").textContent = T("rodape.versao", estado.versao);
  if (estado.etapa === "erro") $("erro-texto").textContent = T("atualizar.erro_inesperado") + estado.erro;
  renderizarCabecalho(estado);
  renderizarPilulas(estado);
  ajustarLayout();
  renderizarRapido(estado);
  observarEstado(estado);
  renderizarInicio(estado);
  renderizarAcessoIdrac(estado);
  renderizarSSH(estado.ssh);
  renderizarBMC(estado);
  renderizarIA(estado);
  renderizarChat(estado);
  renderizarCelular(estado);
  renderizarAtualizacoes(estado);
  renderizarDiagnostico(estado);
  renderizarRefazer(estado);
  renderizarAplicacao(estado);
  renderizarManual(estado);
  renderizarAcoes(estado);
  renderizarColeta(estado);
  renderizarRelatorios(estado);
  if (rotaAtual === "rede" || redeBloqueia(estado)) renderizarRede();
  if (bloqueavaAntes && !redeBloqueia(estado)) { anunciar(T("atualizar.rede_ok")); ir("inicio"); }
  else aplicarRota(false);
  renderizarEtapas(estado);
  const rapido = ["rede", "deteccao", "diagnostico"].includes(estado.etapa) || (estado.ia && estado.ia.situacao === "enviando")
    || (estado.atualizacoes && estado.atualizacoes.situacao === "verificando")
    || (estado.acoes && ["prechecando", "executando"].includes(estado.acoes.situacao))
    || (estado.chat && ["digitando", "aguardando_rede"].includes(estado.chat.situacao))
    || ((((estado.atualizacoes || {}).manual || {}).pacotes || []).some((p) => ["checagem", "checando"].includes(p.etapa)))
    || !!((((estado.atualizacoes || {}).manual || {}).navegador || {}).aberto)
    || (celularAtivo && estado.ia_celular && ["aguardando", "recebida"].includes(estado.ia_celular.situacao));
  // Streaming (resposta da IA sendo escrita): atualiza mais vezes para o texto correr na tela.
  const escrevendo = (estado.ia && estado.ia.situacao === "enviando" && estado.ia.parcial)
    || (estado.chat && estado.chat.situacao === "digitando" && estado.chat.parcial);
  temporizador = window.setTimeout(atualizar, escrevendo ? 250 : rapido ? 500 : 2000);
}

/* ------------------------------------------------------------------ mostrar menos / mais (CLAUDE.md 15.4) */

/* Cartões e listas longas recolhíveis, com resumo quando recolhido; lembrado na sessão (sessionStorage).
   Cartões de confirmação (.cartao-perigo) e o chat nunca recolhem. */
const LIMITE_LISTA = 10;

function lembrado(chave) { try { return sessionStorage.getItem("msscan-" + chave); } catch (e) { return null; } }
function lembrar(chave, valor) { try { sessionStorage.setItem("msscan-" + chave, valor); } catch (e) { /* sem armazenamento */ } }

/* "2 Falha · 5 OK" a partir do texto das etiquetas de status. */
function resumoStatus(nos) {
  const contagem = new Map();
  for (const no of nos) {
    for (const etq of no.querySelectorAll(".etiqueta")) {
      const texto = etq.textContent.trim();
      contagem.set(texto, (contagem.get(texto) || 0) + 1);
    }
  }
  return Array.from(contagem, ([texto, n]) => n + " " + texto).join(" · ");
}

function chaveCartao(cartao) {
  const cabeca = cartao.firstElementChild;
  return "recolhido-" + (cartao.id || cabeca.id || cabeca.textContent.trim());
}

function prepararRecolhiveis() {
  for (const cartao of document.querySelectorAll(".cartao:not(.cartao-perigo):not(.chat)")) {
    const cabeca = cartao.firstElementChild;
    if (!cabeca || !cabeca.classList.contains("cabeca-cartao")) continue;
    const resumo = el("p", { classe: "cartao-nota texto-sec pequeno resumo-recolhido", "aria-live": "off" });
    const botao = el("button", { type: "button", classe: "botao-icone botao-recolher" });
    botao.addEventListener("click", () => {
      const recolher = !cartao.classList.contains("recolhido");
      lembrar(chaveCartao(cartao), recolher ? "1" : "0");
      aplicarRecolhido(cartao);
    });
    cartao.append(resumo);
    cabeca.after(botao);
    aplicarRecolhido(cartao);
  }
}

function aplicarRecolhido(cartao) {
  // Seções longas e secundárias (data-longa) começam recolhidas, com resumo, até o técnico abrir (pedido de 01/10/2026).
  const guardado = lembrado(chaveCartao(cartao));
  const recolhido = guardado === "1" || (guardado === null && cartao.hasAttribute("data-longa"));
  const botao = cartao.querySelector(":scope > .botao-recolher");
  const titulo = cartao.firstElementChild.textContent.trim();
  cartao.classList.toggle("recolhido", recolhido);
  botao.setAttribute("aria-expanded", recolhido ? "false" : "true");
  botao.setAttribute("aria-label", (recolhido ? T("aplicar_recolhido.mostrar_mais") : T("aplicar_recolhido.mostrar_menos")) + titulo);
  botao.title = recolhido ? T("aplicar_recolhido.mostrar_mais_2") : T("aplicar_recolhido.mostrar_menos_2");
  botao.replaceChildren(icone(recolhido ? "chevron-down" : "chevron-up"));
  if (recolhido) {
    const corpo = Array.from(cartao.children).filter((n) => !n.matches(".cabeca-cartao, .botao-recolher, .resumo-recolhido"));
    const itens = corpo.reduce((n, no) => n + no.querySelectorAll(".lista > li, tbody > tr").length, 0);
    cartao.querySelector(".resumo-recolhido").textContent = T("aplicar_recolhido.recolhido")
      + (cartao.dataset.resumo || resumoStatus(corpo) || (itens ? itens + " item(ns)" : T("aplicar_recolhido.mostrar_mais_para_ver_o")));
  }
}

/* Listas e tabelas longas: só os primeiros LIMITE_LISTA itens visíveis; o resto sob demanda.
   Com filtro ativo mostra tudo o que casou; linha com caixa marcada nunca fica escondida. */
/* Tabelas viram cartões em telas estreitas (app.css): cada célula leva o nome da coluna. */
function rotularTabelas() {
  for (const tabela of document.querySelectorAll("table.tabela")) {
    const nomes = Array.from(tabela.querySelectorAll("thead th"), (th) => th.textContent.trim());
    if (!nomes.length) continue;
    for (const linha of tabela.querySelectorAll("tbody > tr")) {
      Array.from(linha.children).forEach((celula, n) => {
        if (celula.getAttribute("colspan") || celula.dataset.rotulo === nomes[n]) return;
        celula.dataset.rotulo = nomes[n] || "";
      });
    }
  }
}

function aplicarLimites() {
  rotularTabelas();
  for (const cartao of document.querySelectorAll(".cartao.recolhido")) aplicarRecolhido(cartao);
  for (const lista of document.querySelectorAll("ul.lista, table.tabela > tbody")) {
    const dono = lista.id ? lista : lista.closest("[id]");
    if (!dono) continue;
    const chave = "mais-" + dono.id;
    const filtrando = lista.id === "diag-componentes" && $("busca").value.trim() !== "";
    const itens = Array.from(lista.children).filter((i) => !i.hidden);
    const mostrarTudo = filtrando || lembrado(chave) === "1";
    const escondidos = [];
    itens.forEach((item, n) => {
      const esconder = !mostrarTudo && n >= LIMITE_LISTA && !item.querySelector("input:checked");
      item.classList.toggle("alem-limite", esconder);
      if (esconder) escondidos.push(item);
    });
    let botao = lista.msscanBotaoMais;
    const precisa = !filtrando && itens.length > LIMITE_LISTA;
    if (!precisa) { if (botao) botao.hidden = true; continue; }
    if (!botao) {
      botao = el("button", { type: "button", classe: "botao botao-secundario botao-pequeno botao-mais" });
      botao.addEventListener("click", () => { lembrar(chave, lembrado(chave) === "1" ? "0" : "1"); aplicarLimites(); });
      (lista.tagName === "TBODY" ? lista.closest("table") : lista).after(botao);
      lista.msscanBotaoMais = botao;
    }
    botao.hidden = false;
    const resumo = resumoStatus(escondidos);
    const texto = mostrarTudo ? T("aplicar_limites.mostrar_menos") : T("aplicar_limites.mostrar_mais") + escondidos.length + (resumo ? ": " + resumo : "") + ")";
    if (botao.textContent !== texto) botao.replaceChildren(icone(mostrarTudo ? "fold" : "unfold"), texto);
    botao.setAttribute("aria-expanded", mostrarTudo ? "true" : "false");
  }
}

/* ------------------------------------------------------------------ fluidez: fps e modo leve (CLAUDE.md 15.4) */

/* Mede o fps por 2 s depois de carregar (uma vez: rAF contínuo pesaria no console do BMC).
   Abaixo de 40 fps, ou com prefers-reduced-motion, liga o modo leve (sem animações nem transparências). */
const FPS_MINIMO = 40;
function medirFps() {
  const reduzir = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  let quadros = 0, inicio = 0;
  function quadro(t) {
    if (!inicio) inicio = t;
    quadros += 1;
    if (t - inicio < 2000) { window.requestAnimationFrame(quadro); return; }
    const fps = Math.round(1000 * (quadros - 1) / (t - inicio));
    window.msscanFps = fps;
    const leve = reduzir || fps < FPS_MINIMO;
    document.body.classList.toggle("leve", leve);
    $("sobre-fps").textContent = fps + " fps" + (leve ? T("medir_fps.modo_leve_ligado_sem_animacoes") : T("medir_fps.animacoes_ligadas"))
      + (reduzir ? " (sistema pede menos movimento)" : "");
  }
  window.requestAnimationFrame(quadro);
}

/* ------------------------------------------------------------------ desligar / reiniciar (CLAUDE.md 15.5) */

/* Botões do cabeçalho: bloqueados (com o motivo) durante gravação de firmware ou ação no BMC; confirmação com
   "Cancelar" em foco; o servidor faz logout do BMC, sync e desmonta o pendrive antes. */
function motivoEnergia(estado) {
  const energia = (estado && estado.energia) || {};
  if (energia.bloqueio) return energia.bloqueio;
  if (energia.disponivel === false) return T("motivo_energia.modo_replay_o_computador_nao");
  return "";
}

function renderizarEnergia(estado) {
  const motivo = motivoEnergia(estado);
  for (const [id, rotulo] of [["botao-reiniciar", T("renderizar_energia.reiniciar_o_servidor")], ["botao-desligar", T("renderizar_energia.desligar_o_servidor")]]) {
    const botao = $(id);
    botao.setAttribute("aria-disabled", motivo ? "true" : "false");
    botao.title = motivo || rotulo;
    botao.setAttribute("aria-label", rotulo + (motivo ? T("renderizar_energia.indisponivel") + motivo + ")" : ""));
  }
}

let focoAntesDaEnergia = null;
function abrirEnergia(acao) {
  const motivo = motivoEnergia(ESTADO);
  const desligar = acao === "desligar";
  focoAntesDaEnergia = document.activeElement;
  $("energia-titulo").textContent = motivo ? (desligar ? T("abrir_energia.desligar") : T("abrir_energia.reiniciar")) + T("abrir_energia.indisponivel_agora")
    : desligar ? T("abrir_energia.desligar_o_servidor") : T("abrir_energia.reiniciar_o_servidor");
  $("energia-texto").textContent = motivo || T("abrir_energia.antes_o_msscan_encerra_a")
    + T("abrir_energia.seguranca") + (desligar ? T("abrir_energia.o_servidor_sera_desligado") : T("abrir_energia.o_servidor_sera_reiniciado_o"));
  $("energia-confirmar").hidden = Boolean(motivo);
  $("energia-confirmar").disabled = false;
  $("energia-confirmar").textContent = desligar ? T("abrir_energia.desligar_agora") : T("abrir_energia.reiniciar_agora");
  $("energia-confirmar").dataset.acao = acao;
  $("energia-cancelar").textContent = motivo ? T("abrir_energia.fechar") : T("abrir_energia.cancelar");
  $("energia-mensagem").textContent = "";
  $("energia-fundo").hidden = false;
  $("energia-cancelar").focus();
}

function fecharEnergia() {
  $("energia-fundo").hidden = true;
  if (focoAntesDaEnergia && document.contains(focoAntesDaEnergia)) focoAntesDaEnergia.focus();
}

async function confirmarEnergia() {
  const botao = $("energia-confirmar");
  botao.disabled = true;
  $("energia-cancelar").focus();
  try {
    const r = await api("energia", "POST", { acao: botao.dataset.acao, confirmar: true });
    $("energia-mensagem").textContent = r.mensagem;
  } catch (e) {
    $("energia-mensagem").textContent = e.message;
    botao.disabled = false;
  }
  anunciar($("energia-mensagem").textContent);
}

/* ------------------------------------------------------------------ terminal (CLAUDE.md 15.5) */

/* xterm.js do pacote Debian node-xterm, carregado só ao abrir a aba. A saída vem por long-poll (bytes em base64);
   o que se digita vai em pedaços pequenos, na ordem. Só existe no servidor local (127.0.0.1 + token). */
let term = null, termFit = null, termPosicao = 0, termLendo = false, termFila = Promise.resolve();
let termFonte = 15;
try { termFonte = Number(window.localStorage.getItem("msscan-term-fonte")) || 15; } catch (e) { /* sem armazenamento */ }

function mensagemTerminal(texto) { $("term-mensagem").textContent = texto; if (texto) anunciar(texto); }

function carregarScript(src) {
  return new Promise((ok, falha) => {
    const script = el("script", { src: src });
    script.addEventListener("load", ok);
    script.addEventListener("error", () => falha(new Error(src)));
    document.head.append(script);
  });
}

async function criarTerminal() {
  document.head.append(el("link", { rel: "stylesheet", href: "/estaticos/xterm/xterm.css" }));
  await carregarScript("/estaticos/xterm/xterm.js");
  await carregarScript("/estaticos/xterm/xterm-addon-fit.js");
  await carregarScript("/estaticos/xterm/xterm-addon-canvas.js");
  const cor = (nome) => getComputedStyle(document.documentElement).getPropertyValue("--ms-" + nome).trim();
  term = new window.Terminal({
    fontSize: termFonte, fontFamily: "\"DejaVu Sans Mono\", \"Liberation Mono\", monospace", scrollback: 5000,
    theme: { background: cor("preto-0"), foreground: cor("texto"), cursor: cor("azul-link"), selectionBackground: cor("azul-1") },
  });
  termFit = new window.FitAddon.FitAddon();
  term.loadAddon(termFit);
  term.open($("terminal-area"));
  // Renderizador em canvas: mais rápido e não precisa de <style> embutido (a CSP da interface não permite).
  term.loadAddon(new window.CanvasAddon.CanvasAddon());
  term.onData(enviarTerminal);
  term.onResize(({ rows, cols }) => api("terminal/tamanho", "POST", { linhas: rows, colunas: cols }).catch(() => {}));
  term.attachCustomKeyEventHandler(teclaTerminal);
  window.addEventListener("resize", () => { if (rotaAtual === "terminal") termFit.fit(); });
}

async function abrirTerminal() {
  if (!term) {
    try { await criarTerminal(); } catch (e) {
      term = null;
      mensagemTerminal(T("abrir_terminal.terminal_indisponivel_a_biblioteca_do"));
      return;
    }
  }
  try { await api("terminal/abrir", "POST"); } catch (e) {
    $("term-desligado").hidden = e.status !== 423;  // 1.0.0: desligado por padrão
    $("terminal-area").hidden = e.status === 423;
    document.querySelector(".barra-terminal").hidden = e.status === 423;
    if (e.status !== 423) mensagemTerminal(T("abrir_terminal.terminal_indisponivel", e.message));
    return;
  }
  $("term-desligado").hidden = true;
  $("terminal-area").hidden = false;
  document.querySelector(".barra-terminal").hidden = false;
  mensagemTerminal("");
  termFit.fit();
  api("terminal/tamanho", "POST", { linhas: term.rows, colunas: term.cols }).catch(() => {});
  term.focus();
  lerTerminal();
}

async function lerTerminal() {
  if (termLendo) return;
  termLendo = true;
  try {
    while (rotaAtual === "terminal") {
      const r = await api("terminal/ler?desde=" + termPosicao);
      if (r.dados) term.write(Uint8Array.from(window.atob(r.dados), (c) => c.charCodeAt(0)));
      termPosicao = r.fim;
      $("term-novo").hidden = r.ativo;
      if (!r.ativo && !r.dados) await new Promise((ok) => window.setTimeout(ok, 1000));
    }
  } catch (e) {
    mensagemTerminal(T("ler_terminal.sem_contato_com_o_terminal", e.message));
  } finally {
    termLendo = false;
  }
}

/* Pedaços de até 500 caracteres (inteiros: nunca corta um emoji ao meio), na ordem em que foram digitados. */
function enviarTerminal(texto) {
  const letras = Array.from(texto);
  for (let i = 0; i < letras.length; i += 500) {
    const parte = letras.slice(i, i + 500).join("");
    termFila = termFila.then(() => api("terminal/escrever", "POST", { dados: parte })).catch(() => {});
  }
}

function copiarTerminal() {
  const selecao = term ? term.getSelection() : "";
  if (!selecao) { mensagemTerminal(T("copiar_terminal.selecione_um_texto_no_terminal")); return; }
  navigator.clipboard.writeText(selecao).then(() => mensagemTerminal(T("copiar_terminal.copiado")), () => mensagemTerminal(T("copiar_terminal.nao_foi_possivel_copiar")));
}

function colarTerminal() {
  if (!term) return;
  navigator.clipboard.readText().then((texto) => { term.paste(texto); term.focus(); },
    () => mensagemTerminal(T("colar_terminal.nao_foi_possivel_ler_a")));
}

/* Teclas tratadas antes do xterm: false = o xterm não manda ao shell. */
function teclaTerminal(ev) {
  if (ev.type !== "keydown") return true;
  if (ev.key === "F6") { ev.preventDefault(); $("aba-terminal").focus(); return false; }
  if (ev.ctrlKey && ev.shiftKey && (ev.key === "C" || ev.key === "c")) { ev.preventDefault(); copiarTerminal(); return false; }
  if (ev.ctrlKey && ev.shiftKey && (ev.key === "V" || ev.key === "v")) return false; // o navegador cola (evento paste)
  return true;
}

function fonteTerminal(delta) {
  termFonte = Math.min(32, Math.max(10, termFonte + delta));
  try { window.localStorage.setItem("msscan-term-fonte", String(termFonte)); } catch (e) { /* sem armazenamento */ }
  if (term) { term.options.fontSize = termFonte; termFit.fit(); term.focus(); }
  mensagemTerminal(T("fonte_terminal.fonte_do_terminal") + termFonte + " px");
}

/* ------------------------------------------------------------------ início */

function iniciar() {
  try { zoomAjuste = Number(window.localStorage.getItem("msscan-zoom-ajuste")) || 0; } catch (e) { zoomAjuste = 0; }
  aplicarZoom(false);
  window.addEventListener("resize", () => aplicarZoom(false));
  preencherIcones(document);
  prepararRecolhiveis();
  medirFps();
  $("sobre-atalhos").append(tabelaAtalhos());
  $("dialogo-atalhos-corpo").append(tabelaAtalhos());
  for (const caixa of document.querySelectorAll(".modo-detalhado")) {
    caixa.addEventListener("change", () => {
      modoDetalhado = caixa.checked;
      try { sessionStorage.setItem("msscan-detalhado", modoDetalhado ? "1" : "0"); } catch (e) { /* sem armazenamento */ }
      aplicarModoDetalhado();
    });
  }
  aplicarModoDetalhado();
  if (!lerToken()) {
    for (const nome of TELAS) $("tela-" + nome).hidden = nome !== "erro";
    $("erro-texto").textContent = T("iniciar.acesso_sem_token_abra_o");
    return;
  }
  iniciarNovaInterface();
  $("rede-tentar").addEventListener("click", tentarRede);
  $("internet-tentar").addEventListener("click", tentarRede);
  $("internet-detalhes").addEventListener("click", () => ir("rede"));
  $("rede-hora-ajustar").addEventListener("click", ajustarHora);
  $("rede-hora-texto").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); ajustarHora(); } });
  $("rede-voltar").addEventListener("click", () => ir("inicio"));
  $("indicador-rede").addEventListener("click", () => ir("rede"));
  $("botao-atalhos").addEventListener("click", abrirAtalhos);
  $("dialogo-fechar").addEventListener("click", fecharAtalhos);
  $("dialogo-fundo").addEventListener("click", (ev) => { if (ev.target === $("dialogo-fundo")) fecharAtalhos(); });
  $("botao-iniciar").addEventListener("click", iniciarDiagnostico);
  $("tela-zoom-menos").addEventListener("click", () => ajustarZoom(-ZOOM_PASSO));
  $("tela-zoom-mais").addEventListener("click", () => ajustarZoom(ZOOM_PASSO));
  $("tela-zoom-auto").addEventListener("click", () => ajustarZoom(0));
  $("tela-aplicar").addEventListener("click", aplicarResolucao);
  $("tela-manter").addEventListener("click", () => respostaResolucao("tela/manter"));
  $("tela-voltar").addEventListener("click", () => respostaResolucao("tela/voltar"));
  window.addEventListener("hashchange", () => window.setTimeout(vigiarTela, 0));
  window.setTimeout(vigiarTela, 0);
  $("atu-verificar-botao").addEventListener("click", verificarAtualizacoes);
  $("man-abrir").addEventListener("click", () => acaoManual("manual/navegador", {}, ""));
  $("man-fechar").addEventListener("click", () => acaoManual("manual/fechar", {}, T("iniciar.navegador_fechado_login_cookies_e")));
  $("man-procurar").addEventListener("click", async () => {
    await acaoManual("manual/procurar", {}, T("iniciar.lista_atualizada_pasta_do_pendrive"));
    if (!$("man-pasta-area").hidden) carregarPastaManual();
  });
  $("man-pasta-abrir").addEventListener("click", alternarPastaManual);
  for (const radio of document.querySelectorAll('input[name="atu-verificar"]')) radio.addEventListener("change", () => { if (ESTADO) renderizarAtualizacoes(ESTADO); });
  $("ia-ver").addEventListener("click", verPreviaIA);
  $("ia-solicitar").addEventListener("click", solicitarIA);
  $("ia-tentar").addEventListener("click", tentarIA);
  $("chat-tentar").addEventListener("click", tentarChat);
  for (const radio of document.querySelectorAll('input[name="ia-enviar"]')) radio.addEventListener("change", () => { if (ESTADO) renderizarIA(ESTADO); });
  $("bmc-validar").addEventListener("click", enviarCredenciais);
  // Enter só no campo de senha valida (não há <form>: Enter em outros campos nunca envia nada).
  $("bmc-senha").addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && !ev.repeat) { ev.preventDefault(); enviarCredenciais(); }
  });
  $("bmc-pular").addEventListener("click", pularBMC);
  $("bmc-reabrir").addEventListener("click", abrirFormBMC);
  $("idrac-tentar").addEventListener("click", tentarAcessoIdrac);
  $("idrac-credenciais").addEventListener("click", abrirFormBMC);
  $("idrac-ip-fixo").addEventListener("click", abrirIpFixo);
  $("diag-bmc-informar").addEventListener("click", abrirFormBMC);
  $("diag-refazer-botao").addEventListener("click", refazerDiagnostico);
  $("atu-parar").addEventListener("click", async () => {
    try { await api("atualizacoes/parar", "POST", {}); anunciar(T("iniciar.parada_pedida_o_componente_atual")); }
    catch (e) { anunciar(T("iniciar.nao_foi_possivel_pedir_a", e.message)); }
    atualizar();
  });
  $("atu-continuar").addEventListener("click", async () => {
    $("atu-continuar").disabled = true;
    try { await api("atualizacoes/continuar", "POST", {}); anunciar(T("iniciar.continuando_a_atualizacao_de_onde")); }
    catch (e) { anunciar(T("iniciar.nao_foi_possivel_continuar", e.message)); }
    $("atu-continuar").disabled = false;
    atualizar();
  });
  $("atu-aplicar").addEventListener("click", () => { if (!$("atu-aplicar").disabled) abrirAviso("catalogo", itensEscolhidos()); });
  $("atu-baixar").addEventListener("click", baixarEscolhidos);
  for (const id of ["ia-cancelar", "chat-cancelar"]) {
    $(id).addEventListener("click", async () => {
      try { await api("ia/cancelar", "POST"); anunciar(T("iniciar.envio_cancelado")); } catch (e) { anunciar(e.message); }
      atualizar();
    });
  }
  for (const campo of ["atu-confirmacao", "atu-confirmacao-redundancia", "atu-confirmacao-riscos"]) $(campo).addEventListener("input", habilitarAviso);
  $("aviso-assumir").addEventListener("click", assumirRiscos);
  $("aviso-ciente").addEventListener("change", () => {
    api("atualizacoes/aviso/ciente", "POST", { ciente: $("aviso-ciente").checked }).catch(() => {});
    habilitarAviso();
  });
  $("aviso-cancelar").addEventListener("click", () => fecharAviso(true));
  $("aviso-instalar").addEventListener("click", instalarPeloAviso);
  $("acoes-prechecar").addEventListener("click", prechecarAcao);
  $("acoes-executar").addEventListener("click", executarAcao);
  $("acoes-confirmacao").addEventListener("input", atualizarBotaoAcao);
  for (const botao of document.querySelectorAll("[data-exportar-log]")) botao.addEventListener("click", exportarLog);
  $("coleta-botao").addEventListener("click", coletarDados);
  for (const prefixo of ["diag", "diag-geral"]) {
    $(prefixo + "-3d-inicial").addEventListener("click", () => { const v = vistas3d[prefixo]; if (v && v.controle) v.controle.vistaInicial(); });
    $(prefixo + "-vista").addEventListener("click", alternarVista);
  }
  $("acoes-desfazer-ver").addEventListener("click", prechecarDesfazer);
  $("acoes-desfazer-executar").addEventListener("click", executarDesfazer);
  $("acoes-desfazer-confirmacao").addEventListener("input", atualizarBotaoDesfazer);
  $("acoes-ntp-outro").addEventListener("change", aoEscolherNtp);
  for (const radio of document.querySelectorAll('input[name="acoes-querer"]')) radio.addEventListener("change", () => { if (ESTADO) renderizarAcoes(ESTADO); });
  $("detalhe-voltar").addEventListener("click", () => ir("diagnostico"));
  $("rel-fechar").addEventListener("click", () => ir("relatorios"));
  $("ia-config-voltar").addEventListener("click", () => ir("ia"));
  $("ia-testar").addEventListener("click", testarChaveIA);
  $("ia-salvar-chave").addEventListener("click", salvarChaveIA);
  $("ia-chave-fechar").addEventListener("click", fecharChaveIA);
  $("ia-salvar-escolha").addEventListener("click", salvarEscolhaIA);
  $("ia-celular-iniciar").addEventListener("click", iniciarCelular);
  $("ia-celular-aceitar").addEventListener("click", aceitarCelular);
  $("ia-celular-recusar").addEventListener("click", recusarCelular);
  $("ia-celular-parar").addEventListener("click", () => { pararCelular(); $("ia-celular-iniciar").focus(); });
  $("chat-enviar").addEventListener("click", enviarChat);
  $("term-copiar").addEventListener("click", copiarTerminal);
  $("ssh-ativar").addEventListener("click", () => acaoSSH("ssh/ativar", T("iniciar.ativando_o_acesso_ssh")));
  $("term-ativar").addEventListener("click", async () => {
    try { await api("terminal/ativar", "POST"); } catch (e) { mensagemTerminal(T("abrir_terminal.terminal_indisponivel", e.message)); return; }
    abrirTerminal();
  });
  $("ssh-desativar").addEventListener("click", () => acaoSSH("ssh/desativar", T("iniciar.desativando_o_acesso_ssh")));
  $("botao-desligar").addEventListener("click", () => abrirEnergia("desligar"));
  $("botao-reiniciar").addEventListener("click", () => abrirEnergia("reiniciar"));
  $("energia-cancelar").addEventListener("click", fecharEnergia);
  $("energia-confirmar").addEventListener("click", confirmarEnergia);
  $("energia-fundo").addEventListener("click", (ev) => { if (ev.target === $("energia-fundo")) fecharEnergia(); });
  $("term-colar").addEventListener("click", colarTerminal);
  $("term-fonte-menos").addEventListener("click", () => fonteTerminal(-1));
  $("term-fonte-mais").addEventListener("click", () => fonteTerminal(1));
  $("term-novo").addEventListener("click", abrirTerminal);
  $("chat-texto").addEventListener("input", () => { if (ESTADO) renderizarChat(ESTADO); });
  // Enter envia; Shift+Enter quebra a linha (CLAUDE.md 10e).
  $("chat-texto").addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) { ev.preventDefault(); if (!$("chat-enviar").disabled) enviarChat(); }
  });
  window.addEventListener("pagehide", pararCelular);
  $("busca").addEventListener("input", aplicarFiltro);
  $("busca").addEventListener("keydown", (ev) => {
    if (ev.key === "ArrowDown" || ev.key === "Enter") { ev.preventDefault(); if (navComponentes) navComponentes.primeiro(); }
  });
  for (const aba of document.querySelectorAll("#abas [role=tab]")) {
    aba.addEventListener("click", (ev) => { if (redeBloqueia(ESTADO)) { ev.preventDefault(); anunciar(T("iniciar.resolva_a_rede_primeiro")); } });
  }
  abasComSetas($("abas"), (aba) => ir(aba.dataset.tela));
  abasComSetas($("detalhe-abas"), (aba) => ativarAbaDetalhe(aba, false));
  for (const aba of document.querySelectorAll("#detalhe-abas [role=tab]")) aba.addEventListener("click", () => ativarAbaDetalhe(aba, true));
  document.addEventListener("keydown", teclaGlobal);
  /* Troca de tela pelas setas numa aba: o foco fica na aba (padrão ARIA de abas), senão as setas
     seguintes não fazem nada. Nos demais casos (atalhos, links) o foco vai para o conteúdo. */
  window.addEventListener("hashchange", () => {
    const ativo = document.activeElement;
    aplicarRota(!(ativo && ativo.closest && ativo.closest("#abas")));
  });
  aplicarRota(false);
  atualizar();
  api("pronto", "POST").catch(() => {});
}

document.addEventListener("DOMContentLoaded", iniciar);

/* 22.5: acesso SSH/SFTP — faixa fixa enquanto ativo e cartão na aba Terminal (a senha só aparece aqui, enquanto ativo). */
function renderizarSSH(ssh) {
  const ativo = !!(ssh && ssh.ativo);
  $("faixa-ssh").hidden = !ativo;
  $("ssh-dados").hidden = !ativo;
  $("ssh-ativar").hidden = ativo;
  if (!ativo) { $("ssh-senha").textContent = ""; return; }
  const enderecos = (ssh.enderecos || []).map((ip) => ip + ":" + ssh.porta).join(", ");
  const faixa = T("renderizar_ssh.acesso_ssh_ativo") + ssh.usuario + "@" + (ssh.enderecos || []).join(" / ") + " (porta " + ssh.porta + T("renderizar_ssh.desliga_sozinho_ao_reiniciar_ou");
  if ($("faixa-ssh-texto").textContent !== faixa) $("faixa-ssh-texto").textContent = faixa;
  $("ssh-endereco").textContent = enderecos;
  if ($("ssh-senha").textContent !== ssh.senha) $("ssh-senha").textContent = ssh.senha || "";
  $("ssh-chaves").textContent = ssh.chaves ? ssh.chaves + T("renderizar_ssh.de_msscan_config_ssh_authorized") : T("renderizar_ssh.nenhuma_so_senha");
  const conexoes = (ssh.conexoes || []).map((c) => c.horario + T("renderizar_ssh.de") + c.ip + " (" + c.metodo + ")").join("; ");
  $("ssh-conexoes").textContent = conexoes || T("renderizar_ssh.nenhuma_ate_agora");
}

async function acaoSSH(caminho, texto) {
  $("ssh-mensagem").textContent = texto;
  try {
    const r = await api(caminho, "POST", {});
    $("ssh-mensagem").textContent = r.mensagem || "";
  } catch (e) {
    $("ssh-mensagem").textContent = T("acao_ssh.nao_foi_possivel", e.message);
  }
  anunciar($("ssh-mensagem").textContent);
}

/* ====================================================================================================================
   Nova interface (1.0.0, pedido de 09/10/2026): barra superior com espaços de trabalho e pílulas de situação, três
   colunas em mosaico (visão do servidor · janela da etapa · painel rápido) que viram gavetas em telas menores, doca,
   sensores ao vivo e central de notificações. Tudo reorganiza o que já existia; nenhuma ação nova no servidor.
   ==================================================================================================================== */

const ORDEM_TELAS = ["inicio", "diagnostico", "ia", "chat", "atualizacoes", "relatorios", "terminal", "sobre"];
const ESPACOS = ["inicio", "diagnostico", "ia", "chat", "atualizacoes", "relatorios", "terminal"];
const TELA_DA_ETAPA = { rede: "inicio", diagnostico: "diagnostico", ia: "ia", chat: "chat", atualizacoes: "atualizacoes", relatorio: "relatorios" };
let VISAO = null;
let telaAnterior = "";

/* Troca de etapa deslizando na horizontal, no sentido dos espaços de trabalho (só transform/opacity). */
function animarTroca(nomeTela) {
  if (!telaAnterior || telaAnterior === nomeTela) { telaAnterior = nomeTela; return; }
  const sentido = ORDEM_TELAS.indexOf(nomeTela) >= ORDEM_TELAS.indexOf(telaAnterior) ? "entra-direita" : "entra-esquerda";
  telaAnterior = nomeTela;
  const tela = $("tela-" + nomeTela);
  if (!tela) return;
  tela.classList.remove("entra-direita", "entra-esquerda");
  void tela.offsetWidth;
  tela.classList.add(sentido);
  tela.addEventListener("animationend", () => tela.classList.remove(sentido), { once: true });
}

/* Largura útil (já descontado o zoom): três colunas a partir de 1400 px; uma coluna abaixo de 900 px. */
function ajustarLayout() {
  const largura = window.innerWidth / zoomAtual();
  const tres = largura >= 1400;
  document.body.classList.toggle("tres-colunas", tres);
  document.body.classList.toggle("estreito", largura < 900);
  const barra = $("barra");
  document.documentElement.style.setProperty("--topo-gaveta", (barra.offsetTop + barra.offsetHeight + 8) + "px");
  if (tres) fecharGavetas(false);
}

/* ---------------------------------------------------------------- gavetas (colunas laterais em telas menores) */

let focoAntesDaGaveta = null;
function gavetaAberta() { return document.querySelector(".painel-lateral.aberta"); }

function abrirGaveta(id) {
  if (document.body.classList.contains("tres-colunas")) return;
  const painel = $(id);
  const ja = painel.classList.contains("aberta");
  fecharGavetas(false);
  if (ja) { if (focoAntesDaGaveta) focoAntesDaGaveta.focus(); return; }
  focoAntesDaGaveta = document.activeElement;
  painel.classList.add("aberta");
  $("fundo-gaveta").hidden = false;
  for (const botao of document.querySelectorAll(".botao-gaveta")) botao.setAttribute("aria-expanded", botao.getAttribute("aria-controls") === id ? "true" : "false");
  const primeiro = painel.querySelector("button, a[href], input, select, [tabindex='0']");
  window.setTimeout(() => (primeiro || painel).focus(), 30);
  if (id === "painel-rapido") marcarNotificacoesVistas();
}

function fecharGavetas(devolverFoco) {
  const aberta = gavetaAberta();
  for (const painel of document.querySelectorAll(".painel-lateral")) painel.classList.remove("aberta");
  $("fundo-gaveta").hidden = true;
  for (const botao of document.querySelectorAll(".botao-gaveta")) botao.setAttribute("aria-expanded", "false");
  if (aberta && devolverFoco !== false && focoAntesDaGaveta && document.contains(focoAntesDaGaveta)) focoAntesDaGaveta.focus();
}

/* Painel ativo: borda em gradiente azul no painel com o foco (a janela central por padrão). */
function marcarPainelAtivo(ev) {
  const painel = ev.target && ev.target.closest ? ev.target.closest(".painel") : null;
  if (!painel) return;
  for (const p of document.querySelectorAll(".painel.ativo")) if (p !== painel) p.classList.remove("ativo");
  painel.classList.add("ativo");
}

/* ---------------------------------------------------------------- relógio e menu de energia */

function atualizarRelogio() {
  const agora = new Date();
  const dois = (n) => String(n).padStart(2, "0");
  const texto = dois(agora.getHours()) + ":" + dois(agora.getMinutes()) + " · " + dois(agora.getDate()) + "/" + dois(agora.getMonth() + 1);
  const relogio = $("relogio");
  if (relogio.textContent !== texto) { relogio.textContent = texto; relogio.setAttribute("datetime", agora.toISOString()); }
}

function abrirMenuEnergia(abrir) {
  const menu = $("menu-energia");
  const botao = $("botao-energia");
  menu.hidden = !abrir;
  botao.setAttribute("aria-expanded", abrir ? "true" : "false");
  if (abrir) $("botao-reiniciar").focus();
}

function teclaMenuEnergia(ev) {
  const itens = [$("botao-reiniciar"), $("botao-desligar")];
  const i = itens.indexOf(document.activeElement);
  if (ev.key === "Escape") { ev.preventDefault(); ev.stopPropagation(); abrirMenuEnergia(false); $("botao-energia").focus(); }
  else if (ev.key === "ArrowDown" || ev.key === "ArrowUp") { ev.preventDefault(); itens[(i + (ev.key === "ArrowDown" ? 1 : itens.length - 1)) % itens.length].focus(); }
  else if (ev.key === "Tab") abrirMenuEnergia(false);
}

/* ---------------------------------------------------------------- pílulas da barra */

function pilula(alvo, classe, nomeIcone, texto, rotulo) {
  const chave = classe + "|" + nomeIcone + "|" + texto + "|" + rotulo;
  alvo.hidden = false;
  if (alvo.dataset.chave === chave) return;
  alvo.dataset.chave = chave;
  alvo.className = alvo.className.replace(/\b(ok|alerta|falha)\b/g, "").trim() + (classe ? " " + classe : "");
  // Situação também pelo ícone (nunca só pela cor da borda); o texto completo fica no aria-label.
  const estado = { alerta: "alert", falha: "x-circle" }[classe];
  alvo.replaceChildren(icone(nomeIcone), el("span", { texto: texto }), ...(estado ? [icone(estado, "icone-estado")] : []));
  alvo.setAttribute("aria-label", rotulo);
  alvo.title = rotulo;
}

function nivelTemperatura(t) {
  if (!t || t.valor === null || t.valor === undefined) return "";
  if (["cr", "nr"].includes(t.status)) return "falha";
  if (t.status === "nc" || (t.limite && t.valor >= t.limite * 0.85)) return "alerta";
  return "ok";
}

function renderizarPilulas(estado) {
  const sensores = VISAO && VISAO.sensores;
  if (sensores && sensores.temperatura_maxima !== null && sensores.temperatura_maxima !== undefined) {
    const pior = sensores.temperaturas.reduce((a, t) => (!a || t.valor > a.valor ? t : a), null);
    const valor = Math.round(sensores.temperatura_maxima) + " °C";
    pilula($("pilula-temp"), nivelTemperatura(pior), "thermometer", valor, T("barra.temperatura_maxima", valor, pior ? pior.nome : ""));
  }
  const fontes = (estado.configuracao || {}).fontes || ((VISAO && VISAO.servidor) || {}).fontes;
  if (fontes && fontes.instaladas !== null && fontes.instaladas !== undefined) {
    const classe = fontes.com_energia < fontes.instaladas ? "alerta" : "ok";
    pilula($("pilula-fontes"), classe, "battery", fontes.com_energia + "/" + fontes.instaladas, T("barra.fontes", fontes.com_energia, fontes.instaladas));
  }
  const acesso = estado.acesso_idrac;
  const bmc = estado.bmc || {};
  if (acesso || bmc.situacao) {
    const ok = acesso ? ["rede", "racadm"].includes(acesso.situacao) : false;
    const texto = acesso ? (acesso.verificando ? T("barra.idrac_verificando") : acesso.titulo) : T("barra.idrac");
    pilula($("pilula-idrac"), acesso && acesso.verificando ? "" : ok ? "ok" : "alerta", "server-cog", T("barra.idrac"), T("barra.idrac_situacao", texto));
  }
  const ind = (estado.ia && estado.ia.indicador) || null;
  if (ind) {
    const classe = { disponivel: "ok", sem_chave: "", sem_acesso: "falha", limite: "alerta" }[ind.situacao];
    pilula($("pilula-ia"), classe === undefined ? "" : classe, "sparkles", T("barra.ia"), T("barra.ia_situacao", ind.texto));
  }
  const ssh = estado.ssh;
  if (ssh && ssh.ativo) pilula($("pilula-ssh"), "alerta", "terminal", T("barra.ssh"), T("barra.ssh_ativo"));
  else $("pilula-ssh").hidden = true;
}

/* ---------------------------------------------------------------- visão do servidor (coluna esquerda) */

function tempoLegivel(segundos) {
  if (segundos === null || segundos === undefined) return "—";
  const dias = Math.floor(segundos / 86400), horas = Math.floor((segundos % 86400) / 3600), minutos = Math.floor((segundos % 3600) / 60);
  return dias ? T("visao.tempo_dias", dias, horas) : horas ? T("visao.tempo_horas", horas, minutos) : T("visao.tempo_minutos", minutos);
}

function bytesLegiveis(bytes) {
  if (!bytes) return "—";
  const gb = bytes / 1e9;
  return gb >= 1000 ? (gb / 1000).toFixed(1).replace(".", ",") + " TB" : Math.round(gb) + " GB";
}

function kv(rotulo, valor) { return el("div", { classe: "kv" }, el("span", { texto: rotulo }), el("span", { texto: valor === null || valor === undefined || valor === "" ? "—" : String(valor) })); }

function medidor(nome, valor, unidade, maximo, classe, nivel) {
  const proporcao = maximo ? Math.max(0, Math.min(1, valor / maximo)) : 0;
  const barra = el("div", { classe: "barra-valor " + (classe || "") + (nivel && nivel !== "ok" ? " " + nivel : "") }, el("span", {}));
  barra.firstChild.style.setProperty("--v", proporcao.toFixed(3));
  return el("div", { classe: "medidor" }, kv(nome, Math.round(valor) + " " + unidade), barra);
}

function blocoVisao(nomeIcone, titulo, ...filhos) {
  return el("section", { classe: "visao-bloco" }, el("h3", {}, icone(nomeIcone), titulo), ...filhos);
}

function renderizarVisao() {
  const alvo = $("visao-servidor");
  const s = VISAO && VISAO.servidor;
  if (!s) return;
  const sensores = VISAO.sensores || { temperaturas: [], ventoinhas: [], consumo: [] };
  const chave = JSON.stringify([s, sensores.temperaturas, sensores.ventoinhas, sensores.consumo]);
  if (alvo.dataset.chave === chave) return;
  alvo.dataset.chave = chave;
  const idrac = s.idrac ? s.idrac.versao + (s.idrac.abreviada ? T("visao.abreviada") : "") : null;
  const blocos = [
    el("section", { classe: "visao-bloco visao-id" }, icone("server", "icone-servidor"),
      el("div", {}, el("strong", { texto: [s.fabricante, s.modelo].filter(Boolean).join(" ") || T("geral.detectando") }),
        el("span", { texto: s.geracao || "" }))),
    blocoVisao("cpu", T("visao.versoes"), kv(T("visao.bios"), s.bios), kv(s.bmc || T("barra.idrac"), idrac), kv(T("visao.tempo_ligado"), tempoLegivel(s.tempo_ligado_s))),
  ];
  if (s.processadores.length) {
    blocos.push(blocoVisao("cpu", T("visao.processadores", s.processadores.length), ...s.processadores.map((p) =>
      kv(p.soquete || "CPU", (p.modelo || "?").replace(/\(R\)|\(TM\)| CPU/g, "").replace(/\s+/g, " ") + (p.nucleos ? " · " + p.nucleos + "c" : "")))));
  }
  if (s.memoria && s.memoria.slots.length) {
    const ocupados = s.memoria.slots.filter((m) => m.ocupado);
    blocos.push(blocoVisao("memory", T("visao.memoria", s.memoria.total_gb || 0, ocupados.length, s.memoria.slots.length),
      el("div", { classe: "slots", role: "list" }, s.memoria.slots.map((m) => el("span", {
        classe: "slot" + (m.ocupado ? " ocupado" : ""), role: "listitem",
        title: m.slot + ": " + (m.ocupado ? [m.gb + " GB", m.tipo, m.velocidade].filter(Boolean).join(" · ") : T("visao.vazio")),
        "aria-label": m.slot + ": " + (m.ocupado ? m.gb + " GB" : T("visao.vazio")), texto: m.slot }))),
      el("p", { classe: "legenda", texto: T("visao.legenda_slots") })));
  }
  if (s.discos.length) {
    blocos.push(blocoVisao("hard-drive", T("visao.discos", s.discos.length), ...s.discos.map((d) =>
      kv(d.modelo || d.dispositivo, bytesLegiveis(d.capacidade_bytes) + (d.temperatura !== null && d.temperatura !== undefined ? " · " + d.temperatura + " °C" : "")))));
  }
  if (s.fontes) blocos.push(blocoVisao("battery", T("visao.fontes"), kv(T("visao.fontes_instaladas"), s.fontes.instaladas), kv(T("visao.fontes_com_energia"), s.fontes.com_energia)));
  if (sensores.temperaturas.length) {
    blocos.push(blocoVisao("thermometer", T("visao.temperaturas"), ...sensores.temperaturas.map((t) =>
      medidor(t.nome, t.valor, "°C", t.limite || 100, "", nivelTemperatura(t)))));
  }
  if (sensores.ventoinhas.length) {
    const maximo = Math.max(...sensores.ventoinhas.map((v) => v.valor)) * 1.2;
    blocos.push(blocoVisao("fan", T("visao.ventoinhas"), ...sensores.ventoinhas.map((v) => medidor(v.nome, v.valor, "RPM", maximo, "vent", ""))));
  }
  if (sensores.consumo.length) {
    blocos.push(blocoVisao("zap", T("visao.consumo"), ...sensores.consumo.map((c) => medidor(c.nome, c.valor, "W", c.limite || c.valor * 2, "", ""))));
  }
  alvo.replaceChildren(...blocos);
}

/* ---------------------------------------------------------------- sensores ao vivo (Início) */

function grupoSensores(classe, titulo, itens, unidade, maximo) {
  const colunas = el("div", { classe: "colunas" });
  const rotulos = el("div", { classe: "rotulos" });
  for (const s of itens) {
    const barra = el("div", { classe: "coluna-barra" + (classe === "temp" && nivelTemperatura(s) !== "ok" ? " " + nivelTemperatura(s) : "") });
    barra.style.setProperty("--v", Math.max(0.02, Math.min(1, s.valor / maximo)).toFixed(3));
    colunas.append(el("div", { classe: "coluna" }, el("span", { classe: "coluna-valor", texto: Math.round(s.valor) + (unidade === "°C" ? "°" : "") }),
      el("div", { classe: "coluna-trilho" }, barra)));
    rotulos.append(el("span", { texto: s.nome.replace(/^Fan/, ""), title: s.nome }));
  }
  return el("div", { classe: "grupo-sensores " + classe }, el("h3", { texto: titulo }), colunas, rotulos);
}

function renderizarSensores() {
  const sensores = VISAO && VISAO.sensores;
  const cartao = $("sensores-vivo");
  if (!sensores || (!sensores.temperaturas.length && !sensores.ventoinhas.length)) { cartao.hidden = true; return; }
  cartao.hidden = false;
  const quando = sensores.pausa ? T("sensores.pausado", sensores.pausa)
    : sensores.origem === "ao_vivo" && sensores.lido ? T("sensores.atualizado", sensores.lido.slice(11, 19)) : T("sensores.do_diagnostico");
  if ($("sensores-quando").textContent !== quando) $("sensores-quando").textContent = quando;
  const grafico = $("sensores-grafico");
  const nomes = JSON.stringify([sensores.temperaturas.map((t) => t.nome), sensores.ventoinhas.map((v) => v.nome)]);
  const maxTemp = Math.max(60, ...sensores.temperaturas.map((t) => t.limite || 0).filter((l) => l < 130), ...sensores.temperaturas.map((t) => t.valor));
  const maxVent = Math.max(1, ...sensores.ventoinhas.map((v) => v.valor)) * 1.15;
  if (grafico.dataset.nomes !== nomes) {
    grafico.dataset.nomes = nomes;
    grafico.replaceChildren(grupoSensores("temp", T("sensores.temperaturas"), sensores.temperaturas, "°C", maxTemp),
      grupoSensores("vent", T("sensores.ventoinhas"), sensores.ventoinhas, "RPM", maxVent));
  } else {
    // Mesmos sensores: só os valores mudam (transform das barras animado, sem recriar nada).
    const barras = grafico.querySelectorAll(".coluna-barra"), valores = grafico.querySelectorAll(".coluna-valor");
    [...sensores.temperaturas.map((s) => [s, maxTemp, "°"]), ...sensores.ventoinhas.map((s) => [s, maxVent, ""])].forEach(([s, maximo, suf], i) => {
      barras[i].style.setProperty("--v", Math.max(0.02, Math.min(1, s.valor / maximo)).toFixed(3));
      valores[i].textContent = Math.round(s.valor) + suf;
    });
  }
  $("sensores-lista").replaceChildren(...[...sensores.temperaturas.map((t) => t.nome + ": " + t.valor + " °C"),
    ...sensores.ventoinhas.map((v) => v.nome + ": " + v.valor + " RPM")].map((t) => el("li", { texto: t })));
}

let temporizadorVisao = 0;
async function carregarVisao() {
  window.clearTimeout(temporizadorVisao);
  if (document.visibilityState !== "hidden") {
    try {
      VISAO = await api("visao");
      renderizarVisao();
      renderizarSensores();
      if (ESTADO) renderizarPilulas(ESTADO);
    } catch (e) { /* sem contato: tenta de novo no próximo ciclo */ }
  }
  temporizadorVisao = window.setTimeout(carregarVisao, 5000);
}

/* ---------------------------------------------------------------- painel rápido (coluna direita) */

function blocoRapido(classe, nomeIcone, titulo, texto, acao) {
  return el("section", { classe: "bloco-rapido " + (classe || "") },
    el("div", { classe: "cabeca-bloco" }, el("span", { classe: "circulo" }, icone(nomeIcone)), el("strong", { texto: titulo })),
    el("p", { texto: texto }), acao || null);
}

function botaoPequeno(nomeIcone, texto, aoClicar) {
  const botao = el("button", { type: "button", classe: "botao botao-secundario botao-pequeno" }, icone(nomeIcone), texto);
  botao.addEventListener("click", aoClicar);
  return botao;
}

function renderizarRapido(estado) {
  const rede = estado.rede || {};
  const continuo = rede.continuo || {};
  const reconectando = continuo.situacao === "reconectando";
  const redeOk = rede.situacao === "ok" && !reconectando;
  const acesso = estado.acesso_idrac;
  const idracOk = acesso && ["rede", "racadm"].includes(acesso.situacao);
  const ind = (estado.ia && estado.ia.indicador) || {};
  const ssh = estado.ssh || {};
  const chave = JSON.stringify([rede.situacao, continuo.situacao, continuo.motivo, acesso && acesso.titulo, acesso && acesso.verificando,
    ind.situacao, ind.texto, ssh.ativo, (ssh.enderecos || []).join()]);
  const alvo = $("blocos-rapidos");
  if (alvo.dataset.chave === chave) return;
  alvo.dataset.chave = chave;
  alvo.replaceChildren(
    blocoRapido(redeOk ? "ok" : rede.situacao === "falha" || reconectando ? "alerta" : "", reconectando ? "wifi-off" : "network", T("rapido.rede"),
      reconectando ? T("rapido.reconectando", continuo.motivo || "") : $("indicador-rede-texto").textContent,
      botaoPequeno("info", T("rapido.detalhes"), () => { fecharGavetas(false); ir("rede"); })),
    blocoRapido(acesso ? (idracOk ? "ok" : "alerta") : "", "server-cog", T("barra.idrac"),
      acesso ? (acesso.verificando ? T("barra.idrac_verificando") : acesso.titulo) : T("rapido.idrac_aguardando"),
      botaoPequeno("shield-check", T("rapido.acesso"), () => { fecharGavetas(false); ir("inicio"); window.setTimeout(() => { const c = $("idrac-cartao"); if (!c.hidden) { c.scrollIntoView({ block: "start" }); $("idrac-tentar").focus(); } }, 80); })),
    blocoRapido({ disponivel: "ok", sem_acesso: "falha", limite: "alerta" }[ind.situacao] || "", "sparkles", T("barra.ia"), ind.texto || T("rapido.ia_indisponivel"),
      botaoPequeno("gear", T("rapido.configurar"), () => { fecharGavetas(false); ir("ia/configurar"); })),
    blocoRapido(ssh.ativo ? "alerta" : "", "terminal", T("barra.ssh"),
      ssh.ativo ? T("rapido.ssh_ativo", (ssh.enderecos || []).join(" / ")) : T("rapido.ssh_desligado"),
      ssh.ativo ? botaoPequeno("stop", T("rapido.desativar"), () => acaoSSH("ssh/desativar", T("iniciar.desativando_o_acesso_ssh")))
        : botaoPequeno("terminal", T("rapido.ativar"), () => { fecharGavetas(false); ir("terminal"); window.setTimeout(() => $("ssh-ativar").focus(), 80); })),
  );
}

/* ---------------------------------------------------------------- central de notificações */

const NOTIFICACOES = [];
let notifNaoVistas = 0;
let notifSilenciadas = lembrado("notif-silenciadas") === "1";
const observado = {};
const ICONE_NIVEL = { ok: "check-circle", alerta: "alert", falha: "x-circle", info: "info" };

function notificar(nivel, texto) {
  NOTIFICACOES.unshift({ nivel: nivel, texto: texto, hora: new Date() });
  NOTIFICACOES.length = Math.min(NOTIFICACOES.length, 50);
  if (!notifSilenciadas && !($("painel-rapido").classList.contains("aberta") || document.body.classList.contains("tres-colunas"))) notifNaoVistas += 1;
  renderizarNotificacoes();
}

function marcarNotificacoesVistas() { notifNaoVistas = 0; renderizarNotificacoes(); }

function renderizarNotificacoes() {
  const lista = $("notif-lista");
  const dois = (n) => String(n).padStart(2, "0");
  lista.replaceChildren(...(NOTIFICACOES.length ? NOTIFICACOES.map((n) => el("li", { classe: "notificacao " + n.nivel },
    icone(ICONE_NIVEL[n.nivel] || "info"), el("span", { texto: n.texto }),
    el("time", { texto: dois(n.hora.getHours()) + ":" + dois(n.hora.getMinutes()) + ":" + dois(n.hora.getSeconds()) })))
    : [el("li", { classe: "notif-vazia", texto: T("notificacoes.nenhuma") })]));
  $("notif-contagem").textContent = T("notificacoes.contagem", NOTIFICACOES.length);
  const contador = $("contador-notif");
  contador.hidden = !notifNaoVistas;
  contador.textContent = notifNaoVistas ? String(notifNaoVistas) : "";
  $("notif-silenciar").setAttribute("aria-pressed", notifSilenciadas ? "true" : "false");
  $("notif-silenciar").lastElementChild.textContent = notifSilenciadas ? T("notificacoes.silenciadas") : T("notificacoes.silenciar");
}

/* Andamento das tarefas do iDRAC (instalação em curso e fila do iDRAC), sempre ao vivo no topo da central. */
function renderizarTarefasNotif(estado) {
  const atu = estado.atualizacoes || {};
  const aplicacao = atu.aplicacao || {};
  const resultado = aplicacao.resultado || {};
  const itens = ((((typeof resultado._acompanhamento === "object" && resultado._acompanhamento) || resultado.acompanhamento) || {}).itens) || [];
  const jobs = ((atu.jobs_idrac || {}).jobs) || [];
  const partes = [];
  if (aplicacao.situacao === "executando" && itens.length) {
    partes.push(el("div", { classe: "tarefa-idrac" }, el("p", {}, el("strong", { texto: T("notificacoes.instalacao_em_andamento") })),
      ...itens.map((i) => el("div", { classe: "medidor" }, kv(i.componente, (i.percentual || 0) + "%"),
        (() => { const b = el("div", { classe: "barra-valor" }, el("span", {})); b.firstChild.style.setProperty("--v", ((i.percentual || 0) / 100).toFixed(3)); return b; })()))));
  }
  const ativos = jobs.filter((j) => j.orfao || ["ativo", "agendado"].includes(j.grupo));
  if (ativos.length) {
    partes.push(el("div", { classe: "tarefa-idrac" }, el("p", {}, el("strong", { texto: T("notificacoes.fila_do_idrac", ativos.length) })),
      ...ativos.slice(0, 5).map((j) => kv(j.nome || j.id || "", j.orfao ? T("renderizar_jobs_idrac.travada") : j.estado || ""))));
  }
  const chave = JSON.stringify(partes.map((p) => p.textContent));
  if ($("notif-tarefas").dataset.chave !== chave) { $("notif-tarefas").dataset.chave = chave; $("notif-tarefas").replaceChildren(...partes); }
}

/* Gera notificações a partir das mudanças do estado (cada fato uma vez). */
function observarEstado(estado) {
  const muda = (chave, valor) => { const antes = observado[chave]; observado[chave] = valor; return antes !== undefined && antes !== valor; };
  const continuo = (estado.rede || {}).continuo || {};
  if (muda("rede", continuo.situacao || "")) {
    if (continuo.situacao === "reconectando") notificar("alerta", T("notificacoes.rede_caiu", continuo.motivo || ""));
    else if (continuo.situacao === "ok") notificar("ok", T("notificacoes.rede_voltou"));
  }
  // Diagnóstico concluído: notifica na mudança e também ao abrir a interface já com ele pronto (recarga da página).
  const etapaAntes = observado.etapa;
  observado.etapa = estado.etapa;
  if (etapaAntes !== estado.etapa && estado.etapa === "concluido") {
    const comps = (estado.diagnostico || {}).componentes || [];
    const falhas = comps.filter((c) => c.status === "FALHA"), alertas = comps.filter((c) => c.status === "ALERTA");
    notificar(falhas.length ? "falha" : alertas.length ? "alerta" : "ok", (estado.diagnostico || {}).veredito || T("notificacoes.diagnostico_concluido"));  // o mesmo veredito da tela
    for (const c of [...falhas, ...alertas]) notificar(c.status === "FALHA" ? "falha" : "alerta", c.titulo + ": " + c.mensagem);
  }
  const ia = (estado.ia || {}).situacao || "";
  if (muda("ia", ia)) {
    if (ia === "concluida") notificar("ok", T("notificacoes.ia_concluida"));
    else if (ia === "falhou") notificar("falha", T("notificacoes.ia_falhou"));
  }
  const atu = estado.atualizacoes || {};
  if (muda("verificacao", atu.situacao || "") && atu.situacao && atu.situacao !== "verificando" && atu.situacao !== "nao_solicitadas") {
    notificar("info", atu.mensagem || T("notificacoes.atualizacoes_verificadas"));
  }
  const aplicacao = atu.aplicacao || {};
  if (muda("aplicacao", aplicacao.situacao || "") && aplicacao.situacao && aplicacao.situacao !== "executando") {
    notificar(["aplicada", "aguardando_reinicio"].includes(aplicacao.situacao) ? "ok" : ["falhou", "bloqueada"].includes(aplicacao.situacao) ? "falha" : "info",
      aplicacao.mensagem || T("notificacoes.instalacao_terminou"));
  }
  if (muda("ssh", Boolean((estado.ssh || {}).ativo))) notificar(estado.ssh.ativo ? "alerta" : "info", estado.ssh.ativo ? T("notificacoes.ssh_ativado") : T("notificacoes.ssh_desativado"));
  if (muda("acesso", (estado.acesso_idrac || {}).titulo || "") && estado.acesso_idrac && !estado.acesso_idrac.verificando) {
    notificar(["rede", "racadm"].includes(estado.acesso_idrac.situacao) ? "ok" : "alerta", T("renderizar_acesso_idrac.acesso_ao_idrac") + estado.acesso_idrac.titulo);
  }
  renderizarTarefasNotif(estado);
}

/* ---------------------------------------------------------------- doca */

function irEFocar(rota, id) {
  fecharGavetas(false);
  ir(rota);
  window.setTimeout(() => { const alvo = $(id); if (alvo && !alvo.closest("[hidden]")) { alvo.scrollIntoView({ block: "center" }); alvo.focus(); } }, 80);
}

function acoesDaDoca() {
  $("doca-refazer").addEventListener("click", () => {
    if (!ESTADO || ESTADO.etapa !== "concluido") { anunciar(T("doca.diagnostico_primeiro")); irEFocar("inicio", "botao-iniciar"); return; }
    irEFocar("diagnostico", "diag-refazer-escopo");
  });
  $("doca-relatorio").addEventListener("click", () => irEFocar("relatorios", "rel-corpo"));
  $("doca-terminal").addEventListener("click", () => { fecharGavetas(false); ir("terminal"); });
  $("doca-drivers").addEventListener("click", () => {
    if (!$("man-cartao").hidden && !$("man-abrir").hidden) { irEFocar("atualizacoes", "man-abrir"); $("man-abrir").click(); return; }
    anunciar(T("doca.drivers_depois_de_verificar")); irEFocar("atualizacoes", "atu-verificar-botao");
  });
  $("doca-downloads").addEventListener("click", () => {
    if (!$("man-cartao").hidden) { irEFocar("atualizacoes", "man-pasta-abrir"); if ($("man-pasta-area").hidden) alternarPastaManual(); return; }
    anunciar(T("doca.drivers_depois_de_verificar")); irEFocar("atualizacoes", "atu-verificar-botao");
  });
  $("doca-config").addEventListener("click", () => irEFocar("sobre", "tela-zoom-auto"));
}

/* ---------------------------------------------------------------- ligação com o restante */

function iniciarNovaInterface() {
  ajustarLayout();
  window.addEventListener("resize", ajustarLayout);
  atualizarRelogio();
  window.setInterval(atualizarRelogio, 10000);
  document.addEventListener("focusin", marcarPainelAtivo);
  document.addEventListener("pointerdown", marcarPainelAtivo);
  $("gaveta-esq-botao").addEventListener("click", () => abrirGaveta("painel-servidor"));
  $("gaveta-dir-botao").addEventListener("click", () => abrirGaveta("painel-rapido"));
  $("fundo-gaveta").addEventListener("click", () => fecharGavetas(true));
  $("botao-energia").addEventListener("click", () => abrirMenuEnergia($("menu-energia").hidden));
  $("menu-energia").addEventListener("keydown", teclaMenuEnergia);
  for (const id of ["botao-reiniciar", "botao-desligar"]) $(id).addEventListener("click", () => abrirMenuEnergia(false), true);
  document.addEventListener("pointerdown", (ev) => { if (!ev.target.closest(".menu-energia")) abrirMenuEnergia(false); });
  $("notif-silenciar").addEventListener("click", () => {
    notifSilenciadas = !notifSilenciadas; lembrar("notif-silenciadas", notifSilenciadas ? "1" : "0");
    if (notifSilenciadas) notifNaoVistas = 0;
    renderizarNotificacoes();
  });
  $("notif-limpar").addEventListener("click", () => { NOTIFICACOES.length = 0; notifNaoVistas = 0; renderizarNotificacoes(); });
  acoesDaDoca();
  renderizarNotificacoes();
  carregarVisao();
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") carregarVisao(); });
}
