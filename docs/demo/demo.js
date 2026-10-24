/*
 * MSscan — demonstração estática (GitHub Pages). Carregado ANTES do app.js.
 *
 * A interface é a mesma do sistema; aqui o fetch("/api/...") é respondido com estados gravados de uma sessão em
 * replay do R640 anonimizado (dados.js, gerado por demo/gerar.py). Nenhuma chamada de rede, nenhuma chave, nada é
 * executado: diagnóstico, IA, chat e atualizações só trocam de estado gravado; instalar, desligar, ações no BMC e
 * salvar chaves respondem "Demonstração".
 */
"use strict";

(function () {
  const D = window.MSSCAN_DEMO;
  const AVISO = "Demonstração: dados simulados. Nada é executado.";
  // A interface exige o token da sessão no endereço; na demonstração ele é fixo.
  try { if (!window.location.hash.startsWith("#t=") && !sessionStorage.getItem("msscan-token")) window.history.replaceState(null, "", "#t=demo"); }
  catch (e) { window.history.replaceState(null, "", "#t=demo"); }

  const copia = (o) => JSON.parse(JSON.stringify(o));
  const estado = { etapa: "pronto", ia: false, chat: 0, atualizacoes: false, aviso: null };
  // Link direto para uma etapa (?cena=diagnostico|ia|chat|atualizacoes|aviso): usado também nas capturas da documentação.
  const cena = new URLSearchParams(window.location.search).get("cena") || "";
  const CENAS = ["diagnostico", "ia", "chat", "atualizacoes", "aviso"];
  const nivel = CENAS.indexOf(cena);
  if (nivel >= 0) estado.etapa = "diagnostico";
  if (nivel >= 1) estado.ia = true;
  if (nivel >= 2) estado.chat = D.chat.length - 1;
  if (nivel >= 3) estado.atualizacoes = true;
  if (nivel >= 4) estado.aviso = D.avisos.catalogo || null;
  const rotaInicial = new URLSearchParams(window.location.search).get("rota");
  if (rotaInicial) window.addEventListener("load", () => setTimeout(() => { window.location.hash = "#/" + rotaInicial; }, 400));
  // Cena "aviso": abre o plano de atualização como o botão "Instalar atualizações" faria (capturas da documentação).
  if (nivel >= 4 && rotaInicial === "atualizacoes" && estado.aviso) {
    window.addEventListener("load", () => setTimeout(() => abrirAviso("catalogo", estado.aviso.itens.map((i) => i.id)), 1200));
  }
  const gaveta = new URLSearchParams(window.location.search).get("gaveta");
  if (gaveta) window.addEventListener("load", () => setTimeout(() => { const b = document.getElementById(gaveta === "esq" ? "gaveta-esq-botao" : "gaveta-dir-botao"); if (b) b.click(); }, 1500));

  function estadoAtual() {
    const base = copia(estado.etapa === "pronto" ? D.estados.pronto : D.estados.diagnostico);
    if (estado.ia) {
      base.ia = copia(D.ia);
      base.chat = copia(D.chat[estado.chat].estado);
    }
    if (estado.atualizacoes) base.atualizacoes = copia(D.atualizacoes);
    if (base.atualizacoes) base.atualizacoes.aviso = estado.aviso ? copia(estado.aviso) : null;
    if (base.energia) base.energia.disponivel = false;
    return base;
  }

  /* Visão do servidor e sensores: os valores gravados do R640, com variação pequena para o gráfico "ao vivo" se mover
     (a faixa da demonstração já avisa que os dados são simulados). */
  function visaoAtual() {
    const v = copia(estado.etapa === "pronto" ? D.visao.pronto : D.visao.diagnostico);
    const s = v.sensores;
    if (s) {
      for (const t of s.temperaturas) t.valor = Math.round(t.valor + (Math.random() - 0.5) * 2);
      for (const f of s.ventoinhas) f.valor = Math.round(f.valor * (1 + (Math.random() - 0.5) * 0.06) / 120) * 120;
      for (const c of s.consumo) c.valor = Math.round(c.valor * (1 + (Math.random() - 0.5) * 0.08));
      s.temperatura_maxima = Math.max(...s.temperaturas.map((t) => t.valor));
      s.origem = "ao_vivo"; s.lido = new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString(); s.pausa = null;
    }
    return v;
  }

  /* Terminal simulado: alguns comandos respondem com a saída gravada na fixture do R640. */
  const terminal = { buffer: [], aberto: false, linha: "", liberado: false };  // 1.0.0: desligado por padrão
  const prompt = "\x1b[1;34mroot@msscan-demo\x1b[0m:~# ";
  function escreverTerminal(texto) { terminal.buffer.push(...new TextEncoder().encode(texto)); }
  function executarComando(cmd) {
    const c = cmd.trim().replace(/\s+/g, " ");
    if (!c) return "";
    if (c === "help" || c === "ajuda") {
      return "Terminal simulado da demonstração. Comandos com saída gravada do R640 (anonimizada):\r\n  "
        + Object.keys(D.terminal).join("\r\n  ") + "\r\n  clear\r\n";
    }
    if (c === "clear") return "\x1b[2J\x1b[H";
    const saida = D.terminal[c] || D.terminal[c.replace(/'/g, "\"")];
    if (saida !== undefined) return saida.replace(/\n/g, "\r\n");
    return c.split(" ")[0] + ": não disponível na demonstração (digite help)\r\n";
  }
  function teclasTerminal(dados) {
    for (const ch of dados) {
      if (ch === "\r") { escreverTerminal("\r\n" + executarComando(terminal.linha) + prompt); terminal.linha = ""; }
      else if (ch === "\x7f") { if (terminal.linha) { terminal.linha = terminal.linha.slice(0, -1); escreverTerminal("\b \b"); } }
      else if (ch === "\x03") { escreverTerminal("^C\r\n" + prompt); terminal.linha = ""; }
      else if (ch >= " ") { terminal.linha += ch; escreverTerminal(ch); }
    }
  }

  function resposta(corpo, status) {
    return Promise.resolve(new Response(JSON.stringify(corpo), { status: status || 200, headers: { "Content-Type": "application/json" } }));
  }
  const recusa = (texto) => resposta({ erro: texto || AVISO }, 409);
  const ok = () => resposta({ ok: true });

  function get(rota, busca) {
    if (rota === "estado") return resposta(estadoAtual());
    if (rota === "tela") return resposta({ disponivel: false, motivo: "demonstração no navegador" });
    if (rota === "visao") return resposta(visaoAtual());
    if (rota === "ia/config") return resposta(D.config_ia);
    if (rota.startsWith("ia/qr/")) return D.qr[rota.slice(6)] ? resposta(D.qr[rota.slice(6)]) : resposta({ erro: "IA desconhecida" }, 404);
    if (rota === "ia/previa") return resposta(D.previa_ia);
    if (rota === "chat") return resposta(estado.ia ? D.chat[estado.chat].mensagens : { mensagens: [] });
    if (rota === "manual/arquivos") return resposta({ arquivos: [], pasta: "MSSCAN/firmware/manual" });
    if (rota.startsWith("componente/")) {
      const c = D.componentes[decodeURIComponent(rota.slice(11))];
      return c ? resposta(c) : resposta({ erro: "componente ainda não diagnosticado" }, 404);
    }
    if (rota === "terminal/ler") {
      const desde = Number(new URLSearchParams(busca).get("desde")) || 0;
      const dados = terminal.buffer.slice(desde);
      const texto = dados.length ? btoa(String.fromCharCode(...dados)) : "";
      const espera = dados.length ? 0 : 400;
      return new Promise((r) => setTimeout(r, espera)).then(() => resposta({ dados: texto, fim: terminal.buffer.length, ativo: true }));
    }
    return resposta({ erro: "rota inexistente" }, 404);
  }

  function post(rota, corpo) {
    switch (rota) {
      case "pronto": case "rede/tentar": case "bmc/pular": case "ia/celular/parar": case "atualizacoes/aviso/cancelar":
        if (rota === "atualizacoes/aviso/cancelar") estado.aviso = null;
        return ok();
      case "diagnostico/iniciar": case "diagnostico/refazer":
        estado.etapa = "diagnostico";
        return ok();
      case "ia/solicitar":
        estado.ia = true;
        return ok();
      case "chat/enviar": {
        const i = D.chat.findIndex((c) => c.pergunta === (corpo.texto || "").trim());
        if (i < 0) return recusa("Na demonstração, use as sugestões rápidas: as respostas estão gravadas.");
        estado.chat = i;
        return ok();
      }
      case "atualizacoes/verificar":
        estado.atualizacoes = true;
        return ok();
      case "atualizacoes/aviso":
        estado.aviso = D.avisos[corpo.origem] || null;
        return estado.aviso ? ok() : recusa("Nenhum item com atualização disponível na demonstração.");
      case "terminal/ativar":
        terminal.liberado = true;
        return ok();
      case "terminal/abrir":
        if (!terminal.liberado) return resposta({ erro: window.TEXTOS["py_terminal.desligado"] }, 423);
        if (!terminal.aberto) {
          terminal.aberto = true;
          escreverTerminal("\x1b[1mMSscan " + D.versao + " - baseado em Debian GNU/Linux 13 (trixie)\x1b[0m\r\n\r\n" + (D.fetch || "")
            + "\r\nTerminal SIMULADO da demonstração: digite help.\r\n" + prompt);
        }
        return ok();
      case "terminal/escrever":
        teclasTerminal(corpo.dados || "");
        return ok();
      case "terminal/tamanho":
        return ok();
      case "log/exportar":
        return resposta({ ok: true, gravado: false, onde: "demonstração: nada é gravado", nome: "msscan_log_tecnico_demo.json",
          conteudo: JSON.stringify({ msscan: D.versao, aviso: "Log técnico de demonstração (dados simulados).", comandos: [] }, null, 1) });
      case "atualizacoes/aplicar": case "manual/instalar":
        return recusa("Demonstração: nada é instalado. No servidor, a instalação seguiria daqui.");
      case "energia":
        return recusa("Demonstração: o computador não é desligado nem reiniciado.");
      default:
        return recusa();
    }
  }

  const fetchOriginal = window.fetch.bind(window);
  window.fetch = function (url, opcoes) {
    const texto = String(url);
    const i = texto.indexOf("/api/");
    if (i < 0) return fetchOriginal(url, opcoes);
    const [rota, busca] = texto.slice(i + 5).split("?");
    const metodo = ((opcoes && opcoes.method) || "GET").toUpperCase();
    let corpo = {};
    try { corpo = opcoes && opcoes.body ? JSON.parse(opcoes.body) : {}; } catch (e) { corpo = {}; }
    return metodo === "GET" ? get(rota, busca || "") : post(rota, corpo);
  };
})();
