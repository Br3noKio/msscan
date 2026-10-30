/* MSscan — modelo 3D do chassi na localização física (CLAUDE.md 18.1).
   three.js e OrbitControls do pacote Debian libjs-three (r111, licença MIT), servidos pelo próprio MSscan (sem CDN).
   Peças vêm prontas do servidor (msscan/modelo3d.py): aqui só o desenho, a interação e o desempenho.
   - mouse: arrastar gira 360°, rodinha dá zoom, botão direito move; teclado: setas giram, + e - dão zoom;
   - redesenha só durante a interação (e a pulsação do problema atual, em ~8 quadros por segundo);
   - sem WebGL ou com fps baixo: devolve false / chama aoFalhar() para a tela voltar ao mapa 2D. */
(function () {
  "use strict";

  // Estado da peça (msscan/modelo3d.py) → preenchimento e aresta (redesign v3, 29/10/2026): peça escura com a aresta na cor da
  // situação; atenção e problema também preenchidos na cor. Lambert, não Standard: o vídeo dos servidores desenha por software.
  const CORES = {
    ok: { preenchimento: 0x26282d, aresta: 0x34e39a, opacidade: 0.55 },
    atencao: { preenchimento: 0xb8892a, aresta: 0xffc53d, opacidade: 1 },
    problema: { preenchimento: 0xb3303f, aresta: 0xff5c72, opacidade: 1 },
    nao_verificado: { preenchimento: 0x232429, aresta: 0xb4bdd6, opacidade: 1 },
  };
  const FPS_MINIMO = 20;

  function scripts(lista, pronto, falhou) {
    if (window.THREE && window.THREE.OrbitControls) return pronto();
    let i = 0;
    const proximo = () => {
      if (i >= lista.length) return pronto();
      const s = document.createElement("script");
      s.src = lista[i++];
      s.onload = proximo;
      s.onerror = () => falhou("biblioteca 3D não encontrada (" + s.src + ")");
      document.head.appendChild(s);
    };
    proximo();
  }

  function webglDisponivel() {
    try {
      const c = document.createElement("canvas");
      return !!(window.WebGLRenderingContext && (c.getContext("webgl") || c.getContext("experimental-webgl")));
    } catch (e) { return false; }
  }

  function rotulo(THREE, texto, escala) {
    const canvas = document.createElement("canvas");
    const ctx = canvas.getContext("2d");
    canvas.width = 256; canvas.height = 64;
    ctx.fillStyle = "rgba(14,14,16,0.92)"; ctx.fillRect(0, 0, 256, 64);
    ctx.fillStyle = "#ededef"; ctx.font = "bold 34px sans-serif"; ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillText(texto, 128, 34);
    const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: new THREE.CanvasTexture(canvas), depthTest: false }));
    sprite.scale.set(escala * 4, escala, 1);
    sprite.renderOrder = 10;
    return sprite;
  }

  function curto(peca) {
    if (peca.tipo === "dimm" || peca.tipo === "soquete_vazio") return peca.id.replace("dimm_", "");
    if (peca.tipo === "cpu") return peca.id.toUpperCase();
    if (peca.tipo === "fonte") return peca.id.toUpperCase();
    if (peca.tipo === "slot_pcie") return "PCIe " + peca.id.replace("slot_pcie", "");
    return "";
  }

  /* Vista inicial: mesmo ângulo (de cima, pela frente esquerda), centralizada no que aparece na tela e na menor distância
     em que os 8 cantos do chassi e o rótulo FRENTE cabem com margem (busca binária; serve para qualquer chassi).
     Devolve {posicao, alvo} da câmera. */
  function enquadrar(THREE, camera, centro, dim) {
    const direcao = new THREE.Vector3(-520, 560, 620).normalize();
    const pontos = [new THREE.Vector3(-40, dim.altura + 10, centro.z)];
    for (const x of [0, dim.profundidade]) for (const y of [0, dim.altura]) for (const z of [0, dim.largura]) pontos.push(new THREE.Vector3(x, y, z));
    const alvo = centro.clone();
    const posicionar = (d) => { camera.position.copy(alvo).addScaledVector(direcao, d); camera.lookAt(alvo); camera.updateMatrixWorld(); };
    const projetados = () => pontos.map((p) => p.clone().project(camera));
    let d = 4000;
    for (let passo = 0; passo < 2; passo++) {
      // Entre a meia-diagonal (câmera fora do chassi) e 4000 mm (antes do plano de corte de 5000 mm da câmera).
      let perto = Math.hypot(dim.profundidade, dim.largura, dim.altura) / 2, longe = 4000;
      for (let i = 0; i < 30; i++) {
        const meio = (perto + longe) / 2;
        posicionar(meio);
        if (projetados().every((v) => Math.abs(v.x) <= 0.9 && Math.abs(v.y) <= 0.9)) longe = meio; else perto = meio;
      }
      d = longe;
      posicionar(d);
      if (passo === 0) {
        const v = projetados(), xs = v.map((p) => p.x), ys = v.map((p) => p.y);
        const profundidade = alvo.clone().project(camera).z;
        alvo.copy(new THREE.Vector3((Math.min(...xs) + Math.max(...xs)) / 2, (Math.min(...ys) + Math.max(...ys)) / 2, profundidade).unproject(camera));
      }
    }
    return { posicao: camera.position.clone(), alvo: alvo };
  }

  /* montar(container, dados, opcoes) → controle {vistaInicial, destruir} ou chama opcoes.aoFalhar(motivo). */
  function montar(container, dados, opcoes) {
    const aoFalhar = opcoes.aoFalhar || function () {};
    if (!webglDisponivel()) { aoFalhar("este navegador ou placa de vídeo não tem WebGL"); return null; }
    const controle = { destruir: function () {}, vistaInicial: function () {} };
    scripts(["/estaticos/three/three.min.js", "/estaticos/three/OrbitControls.js"], function () {
      try { Object.assign(controle, criar(container, dados, opcoes)); }
      catch (e) { aoFalhar("não foi possível desenhar em 3D (" + e.message + ")"); }
    }, aoFalhar);
    return controle;
  }

  function criar(container, dados, opcoes) {
    const THREE = window.THREE;
    const dim = dados.dimensoes;
    const largura = Math.max(320, container.clientWidth || 640), altura = Math.round(largura * 0.56);
    const renderer = new THREE.WebGLRenderer({ antialias: false, alpha: true, powerPreference: "low-power" });
    renderer.setPixelRatio(1);
    renderer.setSize(largura, altura);
    renderer.domElement.setAttribute("tabindex", "0");
    renderer.domElement.setAttribute("role", "img");
    renderer.domElement.setAttribute("aria-label", "Modelo 3D do " + (dados.modelo || "servidor") + ": setas giram, + e - dão zoom, Enter mostra a peça em foco na lista");
    container.replaceChildren(renderer.domElement);

    const cena = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(40, largura / altura, 5, 5000);
    cena.add(new THREE.HemisphereLight(0xdfe8ff, 0x101114, 0.85));
    const luz = new THREE.DirectionalLight(0xffffff, 0.7);
    luz.position.set(-300, 600, 400);
    cena.add(luz);

    // Eixos do servidor → three.js: x = profundidade (frente em x=0), y = altura, z = largura (esquerda vista da frente em z=0).
    const centro = new THREE.Vector3(dim.profundidade / 2, dim.altura / 2, dim.largura / 2);
    const chassi = new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.BoxBufferGeometry(dim.profundidade, dim.altura, dim.largura)),
      new THREE.LineBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0.22 }));
    chassi.position.copy(centro);
    cena.add(chassi);
    const grade = new THREE.GridHelper(Math.max(dim.profundidade, dim.largura) * 2, 40, 0x24262b, 0x16171a);
    grade.position.set(centro.x, -1, centro.z);
    cena.add(grade);
    const fundo = new THREE.Mesh(new THREE.PlaneBufferGeometry(dim.profundidade, dim.largura),
      new THREE.MeshLambertMaterial({ color: 0x141518, side: THREE.DoubleSide }));
    fundo.rotation.x = -Math.PI / 2;
    fundo.position.set(centro.x, 0.5, centro.z);
    cena.add(fundo);
    const frente = rotulo(THREE, "FRENTE", 22);
    frente.position.set(-40, dim.altura + 10, centro.z);
    cena.add(frente);

    const clicaveis = [], pulsando = [];
    for (const peca of dados.pecas) {
      const c = peca.caixa;
      const geo = new THREE.BoxBufferGeometry(Math.max(c.w, 1), Math.max(c.h, 1), Math.max(c.d, 1));
      const pos = new THREE.Vector3(c.x + c.w / 2, c.z + c.h / 2, c.y + c.d / 2);
      let objeto;
      const cor = CORES[peca.cor] || CORES.nao_verificado;
      const destaque = peca.cor === "problema" || peca.cor === "atencao";
      if (peca.estado === "presente" || peca.problema) {
        objeto = new THREE.Mesh(geo, new THREE.MeshLambertMaterial({ color: cor.preenchimento, emissive: destaque ? cor.preenchimento : 0x000000,
          emissiveIntensity: destaque ? 0.35 : 0, transparent: peca.estado !== "presente", opacity: peca.estado === "presente" ? 1 : 0.55 }));
        objeto.add(new THREE.LineSegments(new THREE.EdgesGeometry(geo),
          new THREE.LineBasicMaterial({ color: cor.aresta, transparent: cor.opacidade < 1, opacity: cor.opacidade })));
        if (peca.problema === "atual") pulsando.push(objeto);
      } else {
        // Ausente: vazio (só o contorno branco bem apagado); presença não verificada: contorno na cor de "não verificado".
        objeto = new THREE.LineSegments(new THREE.EdgesGeometry(geo), peca.estado === "ausente"
          ? new THREE.LineBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0.16 })
          : new THREE.LineBasicMaterial({ color: cor.aresta }));
      }
      objeto.position.copy(pos);
      objeto.userData.peca = peca;
      cena.add(objeto);
      clicaveis.push(objeto);
      const texto = curto(peca);
      if (texto) {
        const r = rotulo(THREE, texto, peca.tipo === "dimm" || peca.tipo === "soquete_vazio" ? 7 : 12);
        r.position.set(pos.x - c.w / 2 - 8, c.z + c.h + 6, pos.z);
        cena.add(r);
      }
    }

    const controles = new THREE.OrbitControls(camera, renderer.domElement);
    controles.enableKeys = false;  // setas: giro (abaixo), não deslocamento
    const vista = enquadrar(THREE, camera, centro, dim);
    controles.target.copy(vista.alvo);
    camera.position.copy(vista.posicao);
    controles.update();
    controles.saveState();

    // Desempenho: só redesenha quando algo muda; mede o fps durante a interação.
    let quadros = 0, inicioMedida = 0, desligado = false, pulso = null;
    function desenhar() {
      if (desligado) return;
      const t0 = performance.now();
      renderer.render(cena, camera);
      if (inicioMedida) {
        quadros++;
        const passado = performance.now() - inicioMedida;
        if (quadros >= 20 && passado > 0) {
          const fps = (quadros * 1000) / passado;
          if (opcoes.aoMedir) opcoes.aoMedir(fps);
          inicioMedida = 0;
          if (fps < FPS_MINIMO && opcoes.aoFalhar) { destruir(); opcoes.aoFalhar("desenho 3D lento neste computador (" + fps.toFixed(0) + " quadros por segundo)"); }
        }
      }
      return performance.now() - t0;
    }
    let agendado = false;
    function pedirDesenho() { if (!agendado) { agendado = true; requestAnimationFrame(() => { agendado = false; desenhar(); }); } }
    controles.addEventListener("start", () => { quadros = 0; inicioMedida = performance.now(); });
    controles.addEventListener("change", pedirDesenho);

    // Primeiro quadro compila os shaders (lento no llvmpipe); mede o segundo: se nem ele sai rápido, o 2D é melhor.
    desenhar();
    const segundo = desenhar();
    if (segundo > 250 && opcoes.aoFalhar) { destruir(); opcoes.aoFalhar("desenho 3D lento neste computador"); return {}; }

    // Pulsação do problema atual (sem animação com prefers-reduced-motion: fica vermelho fixo).
    const semMovimento = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (pulsando.length && !semMovimento) {
      pulso = setInterval(() => {
        if (document.hidden || !renderer.domElement.isConnected) return;
        const k = 0.25 + 0.5 * (0.5 + 0.5 * Math.sin(performance.now() / 260));
        for (const m of pulsando) m.material.emissiveIntensity = k;
        desenhar();
      }, 125);
    }

    // Teclado: setas giram, + e - dão zoom.
    const esferico = new THREE.Spherical();
    renderer.domElement.addEventListener("keydown", (ev) => {
      const offset = camera.position.clone().sub(controles.target);
      esferico.setFromVector3(offset);
      let tratado = true;
      if (ev.key === "ArrowLeft") esferico.theta -= 0.12;
      else if (ev.key === "ArrowRight") esferico.theta += 0.12;
      else if (ev.key === "ArrowUp") esferico.phi = Math.max(0.05, esferico.phi - 0.1);
      else if (ev.key === "ArrowDown") esferico.phi = Math.min(Math.PI - 0.05, esferico.phi + 0.1);
      else if (ev.key === "+" || ev.key === "=") esferico.radius = Math.max(150, esferico.radius * 0.85);
      else if (ev.key === "-" || ev.key === "_") esferico.radius = Math.min(3000, esferico.radius / 0.85);
      else tratado = false;
      if (!tratado) return;
      ev.preventDefault();
      ev.stopPropagation();
      camera.position.copy(controles.target).add(new THREE.Vector3().setFromSpherical(esferico));
      controles.update();
    });

    // Clique (sem arrastar) numa peça → diagnóstico dela.
    const raio = new THREE.Raycaster(), mouse = new THREE.Vector2();
    let baixo = null;
    renderer.domElement.addEventListener("pointerdown", (ev) => { baixo = [ev.clientX, ev.clientY]; });
    renderer.domElement.addEventListener("pointerup", (ev) => {
      if (!baixo || Math.hypot(ev.clientX - baixo[0], ev.clientY - baixo[1]) > 4 || ev.button !== 0) return;
      const r = renderer.domElement.getBoundingClientRect();
      mouse.set(((ev.clientX - r.left) / r.width) * 2 - 1, -((ev.clientY - r.top) / r.height) * 2 + 1);
      raio.setFromCamera(mouse, camera);
      const acerto = raio.intersectObjects(clicaveis, false)[0];
      if (acerto && opcoes.aoClicar) opcoes.aoClicar(acerto.object.userData.peca);
    });

    function vistaInicial() { controles.reset(); pedirDesenho(); }
    function destruir() {
      if (desligado) return;
      desligado = true;
      if (pulso) clearInterval(pulso);
      controles.dispose();
      renderer.dispose();
      renderer.domElement.remove();
    }
    return { vistaInicial: vistaInicial, destruir: destruir };
  }

  window.Modelo3D = { montar: montar, webgl: webglDisponivel };
})();
