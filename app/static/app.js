import { FilesetResolver, HandLandmarker } from "/static/vendor/vision_bundle.mjs";

const $ = (id) => document.getElementById(id);
const video = $("video"), overlay = $("overlay"), ctx = overlay.getContext("2d");
const visor = $("visor"), mensaje = $("mensaje");

// Igual que app/geometry.py: (nombre, MCP, PIP, DIP, TIP).
const DEDOS = [["indice", 5, 6, 7, 8], ["medio", 9, 10, 11, 12], ["anular", 13, 14, 15, 16], ["menique", 17, 18, 19, 20]];
const CONEXIONES = [[0, 1], [1, 2], [2, 3], [3, 4], [0, 5], [5, 9], [9, 13], [13, 17], [0, 17],
  ...DEDOS.flatMap((d) => [[d[1], d[2]], [d[2], d[3]], [d[3], d[4]]])];
const VENTANA_PICO_MS = 2000; // historial de enfoque para capturar en el pico del autofoco
// Envío automático continuo: una sola petición a la vez; entre una y otra se elige el cuadro más nítido.
const ELECCION_MS = 350;      // cuadros "enviables" que se comparan antes de mandar el mejor
const PAUSA_MS = 500;         // respiro tras cada respuesta antes de juntar el siguiente candidato
const CANDIDATO_VIGENTE_MS = 600; // un candidato más viejo ya no representa la posición actual de la mano
const auto = { libreDesde: 0, cand: null, candDesde: 0, errores: 0 };
const candCanvas = document.createElement("canvas");
const candCtx = candCanvas.getContext("2d");
// Modo sesión: /captura/<token> (lo abre un proyecto cliente). Sin token es la página de demostración.
const TOKEN = location.pathname.match(/^\/captura\/([^/]+)/)?.[1] || null;
let sesion = null;
// Abierta desde el QR de la computadora: al terminar, la computadora continúa el trámite (no el celular).
const MODO_QR = new URLSearchParams(location.search).get("modo") === "qr";
// Resolución automática: 1080p si el equipo procesa con fluidez, 720p si no.
const CLAVE_RES = "huellas.resolucionAuto";
const MUESTRAS_CALENTAMIENTO = 30, MUESTRAS_MEDICION = 90;
// Solo cuenta el costo de procesamiento: los fps de la cámara bajan con poca luz aunque el equipo sea rápido.
const MAX_MS_POR_CUADRO = 40;
const rendimiento = { decidido: false, motivo: "", muestras: [], vistos: 0, inicio: 0 };

const estado = {
  landmarker: null, stream: null, track: null, caps: {}, espejo: false,
  ultimoTiempo: -1, ultimoResultado: null, puntosPrevios: null,
  capturando: false, armado: true, intentos: 0, mejor: null,
  historialEnfoque: [], ultimoPOI: 0, malEnfoqueDesde: 0, linterna: false,
  fps: 0, ultimoFrame: performance.now(), json: null,
};
const cfg = {
  anchoMin: () => +$("rngAncho").value,
  enfoqueMin: () => +$("rngEnfoque").value,
  calidadObjetivo: () => +$("rngCalidad").value,
};

// ---------------- Geometría (espejo de geometry.py) ----------------
const sub = (a, b) => [a[0] - b[0], a[1] - b[1]];
const norma = (a) => Math.hypot(a[0], a[1]);

function regionesDedos(pts) {
  const mcps = DEDOS.map((d) => pts[d[1]]);
  return DEDOS.map(([nombre, , , dip, tip], i) => {
    const pDip = pts[dip], pTip = pts[tip];
    const seg = norma(sub(pTip, pDip)) || 1;
    const eje = [(pTip[0] - pDip[0]) / seg, (pTip[1] - pDip[1]) / seg];
    const vec = [i - 1, i + 1].filter((j) => j >= 0 && j < 4).map((j) => norma(sub(mcps[i], mcps[j])));
    const media = vec.reduce((a, b) => a + b, 0) / vec.length;
    const ancho = Math.min(Math.max(media * 0.92, 0.75 * seg), 1.6 * seg);
    const inicio = [pDip[0] - 0.15 * seg * eje[0], pDip[1] - 0.15 * seg * eje[1]];
    const fin = [pTip[0] + 0.45 * seg * eje[0], pTip[1] + 0.45 * seg * eje[1]];
    return { nombre, inicio, fin, ancho, eje, largo: norma(sub(fin, inicio)) };
  });
}

function esquinas(r, margen = 1) {
  const d = [-r.eje[1] * r.ancho * margen / 2, r.eje[0] * r.ancho * margen / 2];
  return [[r.fin[0] - d[0], r.fin[1] - d[1]], [r.fin[0] + d[0], r.fin[1] + d[1]],
    [r.inicio[0] + d[0], r.inicio[1] + d[1]], [r.inicio[0] - d[0], r.inicio[1] - d[1]]];
}

function signoPalma(p) {
  const u = sub(p[9], p[0]), v = sub(p[5], p[17]);
  return u[0] * v[1] - u[1] * v[0];
}

function dedoExtendido(p, d) {
  const a = sub(p[d[2]], p[d[1]]), b = sub(p[d[4]], p[d[3]]);
  return (a[0] * b[0] + a[1] * b[1]) / (norma(a) * norma(b) + 1e-9) > 0.75;
}

// ---------------- Análisis de cada yema ----------------
const roiCanvas = document.createElement("canvas");
roiCanvas.width = 96; roiCanvas.height = 128;
const roiCtx = roiCanvas.getContext("2d", { willReadFrequently: true });
const yemas = [...document.querySelectorAll("#yemas canvas")];
const yemasTxt = [...document.querySelectorAll("#yemas figcaption b")];

/** Recorta la yema alineada (punta arriba), mide enfoque/luz y pinta la vista previa realzada. */
function analizarYema(region, i) {
  const W = roiCanvas.width, H = roiCanvas.height;
  // Solo la parte central de la yema (0.8 del ancho), donde están las crestas útiles.
  const [tl, tr, , bl] = esquinas(region, 0.8);
  const ux = [(tr[0] - tl[0]) / W, (tr[1] - tl[1]) / W];
  const vy = [(bl[0] - tl[0]) / H, (bl[1] - tl[1]) / H];
  const det = ux[0] * vy[1] - vy[0] * ux[1];
  // Transformación inversa: píxel de video -> píxel del recorte.
  const a = vy[1] / det, b = -ux[1] / det, c = -vy[0] / det, d = ux[0] / det;
  roiCtx.setTransform(a, b, c, d, -(a * tl[0] + c * tl[1]), -(b * tl[0] + d * tl[1]));
  roiCtx.drawImage(video, 0, 0);
  roiCtx.setTransform(1, 0, 0, 1, 0, 0);
  const datos = roiCtx.getImageData(0, 0, W, H);
  const px = datos.data;

  const g = new Float32Array(W * H);
  let suma = 0, saturados = 0;
  for (let k = 0, j = 0; k < g.length; k++, j += 4) {
    const v = 0.25 * px[j] + 0.6 * px[j + 1] + 0.15 * px[j + 2];
    g[k] = v; suma += v; if (v > 245) saturados++;
  }
  const media = suma / g.length;

  // Enfoque: desviación del laplaciano en la zona central, normalizada por el brillo.
  let s1 = 0, s2 = 0, n = 0;
  for (let y = Math.floor(H * 0.12); y < H * 0.85; y++) {
    for (let x = Math.floor(W * 0.15); x < W * 0.85; x++) {
      const k = y * W + x;
      const l = 4 * g[k] - g[k - 1] - g[k + 1] - g[k - W] - g[k + W];
      s1 += l; s2 += l * l; n++;
    }
  }
  const enfoque = Math.sqrt(Math.max(s2 / n - (s1 / n) ** 2, 0)) / (media + 1) * 100;

  // Vista previa: paso-alto (g - media local) estirado, para ver las crestas en vivo.
  const integral = new Float64Array((W + 1) * (H + 1));
  for (let y = 0; y < H; y++) {
    let fila = 0;
    for (let x = 0; x < W; x++) {
      fila += g[y * W + x];
      integral[(y + 1) * (W + 1) + x + 1] = integral[y * (W + 1) + x + 1] + fila;
    }
  }
  const R = 4, hp = new Float32Array(W * H);
  let var2 = 0;
  for (let y = 0; y < H; y++) {
    const y0 = Math.max(0, y - R), y1 = Math.min(H, y + R + 1);
    for (let x = 0; x < W; x++) {
      const x0 = Math.max(0, x - R), x1 = Math.min(W, x + R + 1);
      const area = (y1 - y0) * (x1 - x0);
      const s = integral[y1 * (W + 1) + x1] - integral[y0 * (W + 1) + x1] - integral[y1 * (W + 1) + x0] + integral[y0 * (W + 1) + x0];
      const v = g[y * W + x] - s / area;
      hp[y * W + x] = v; var2 += v * v;
    }
  }
  const k = 55 / (Math.sqrt(var2 / hp.length) + 1e-3);
  for (let p = 0, j = 0; p < hp.length; p++, j += 4) {
    const v = Math.max(0, Math.min(255, 128 + hp[p] * k));
    px[j] = px[j + 1] = px[j + 2] = v; px[j + 3] = 255;
  }
  yemas[i].getContext("2d").putImageData(datos, 0, 0);
  return { enfoque, brillo: media, saturacion: saturados / g.length };
}

// ---------------- Evaluación por cuadro ----------------
function evaluar(res, ahora) {
  const vw = video.videoWidth, vh = video.videoHeight;
  const ev = { checks: {}, mensaje: "", nivel: "mal", regiones: null, yemas: null, pts: null, listo: false,
    enviable: false, puntaje: 0 };
  const c = ev.checks;
  if (!res || !res.landmarks || !res.landmarks.length) {
    estado.puntosPrevios = null;
    ev.mensaje = "Coloca tu mano frente a la cámara: palma al frente y 4 dedos estirados";
    return ev;
  }
  const pts = res.landmarks[0].map((p) => [p.x * vw, p.y * vh]);
  ev.pts = pts;
  c.mano = "ok";

  const manoSel = $("selMano").value;
  const manoGeo = signoPalma(pts) > 0 ? "derecha" : "izquierda";
  c.palma = manoSel === "auto" || manoSel === manoGeo ? "ok" : "mal";

  c.dedos = DEDOS.every((d) => dedoExtendido(pts, d)) ? "ok" : "mal";

  const regiones = regionesDedos(pts);
  ev.regiones = regiones;
  const margen = 4;
  const dentro = regiones.every((r) => esquinas(r, 0.9).every(([x, y]) => x > margen && y > margen && x < vw - margen && y < vh - margen));
  c.encuadre = dentro ? "ok" : "mal";

  const anchoMedio = regiones.reduce((s, r) => s + r.ancho, 0) / 4;
  const anchoMin = cfg.anchoMin();
  c.distancia = anchoMedio >= anchoMin ? "ok" : anchoMedio >= anchoMin * 0.75 ? "medio" : "mal";
  $("detDistancia").textContent = `${Math.round(anchoMedio)} / ${anchoMin} px`;

  let juntos = false;
  for (let i = 0; i < 3; i++) {
    const dist = norma(sub(pts[DEDOS[i][4]], pts[DEDOS[i + 1][4]]));
    if (dist < 0.6 * (regiones[i].ancho + regiones[i + 1].ancho) / 2) juntos = true;
  }
  c.separacion = juntos ? "medio" : "ok";

  // Movimiento de las 4 puntas respecto al cuadro anterior, relativo al ancho del dedo.
  let mov = 0;
  if (estado.puntosPrevios) {
    mov = DEDOS.reduce((s, d) => s + norma(sub(pts[d[4]], estado.puntosPrevios[d[4]])), 0) / 4 / anchoMedio;
  }
  estado.puntosPrevios = pts;
  c.quieto = estado.puntosPrevios && mov < 0.035 ? "ok" : "mal";

  if (dentro) {
    ev.yemas = regiones.map((r, i) => analizarYema(r, i));
    const brillo = ev.yemas.reduce((s, y) => s + y.brillo, 0) / 4;
    const sat = Math.max(...ev.yemas.map((y) => y.saturacion));
    c.luz = brillo < 60 ? "mal" : sat > 0.08 ? "medio" : brillo > 225 ? "medio" : "ok";
    $("detLuz").textContent = brillo < 60 ? "poca luz" : sat > 0.08 ? "reflejo" : `${Math.round(brillo)}`;

    const enfoques = ev.yemas.map((y) => y.enfoque);
    const minimo = Math.min(...enfoques);
    const medio = enfoques.reduce((a, b) => a + b, 0) / 4;
    estado.historialEnfoque.push([ahora, medio]);
    estado.historialEnfoque = estado.historialEnfoque.filter(([t]) => ahora - t < VENTANA_PICO_MS);
    const pico = Math.max(...estado.historialEnfoque.map(([, v]) => v));
    const umbral = cfg.enfoqueMin();
    // Se exige superar el umbral y estar cerca del mejor enfoque reciente (el autofoco puede seguir buscando).
    c.enfoque = minimo >= umbral && medio >= 0.8 * pico ? "ok" : minimo >= umbral * 0.7 ? "medio" : "mal";
    // Para elegir el mejor cuadro entre envíos: el dedo menos nítido manda, castigado por el movimiento.
    ev.puntaje = minimo / (1 + 20 * mov);
    $("detEnfoque").textContent = `${medio.toFixed(1)} (mín ${minimo.toFixed(1)} / ${umbral})`;
    enfoques.forEach((v, i) => {
      yemas[i].className = v >= umbral ? "ok" : v >= umbral * 0.7 ? "medio" : "mal";
      yemasTxt[i].textContent = v.toFixed(1);
    });
  } else {
    c.luz = c.enfoque = "mal";
    estado.historialEnfoque = [];
  }

  const orden = [
    ["palma", "Muestra la palma (yemas) hacia la cámara con la mano " + manoSel],
    ["dedos", "Estira los 4 dedos"],
    ["encuadre", "Centra los 4 dedos dentro de la imagen"],
    ["distancia", "Acerca la mano a la cámara"],
    ["separacion", "Separa ligeramente los dedos"],
    ["luz", c.luz === "mal" ? "Necesitas más luz: enciende la linterna" : "Hay reflejo: inclina un poco la mano"],
    ["quieto", "Mantén la mano quieta"],
    ["enfoque", "Enfocando… si no enfoca, aleja un poco la mano o usa zoom"],
  ];
  // Suficiente para intentar en segundo plano: la mano bien puesta aunque falte afinar enfoque o quietud.
  ev.enviable = ["palma", "dedos", "encuadre"].every((k) => c[k] === "ok") &&
    ["distancia", "luz", "enfoque"].every((k) => c[k] !== "mal");
  const fallo = orden.find(([k]) => c[k] !== "ok" && !(k === "separacion" && c[k] === "medio"));
  if (fallo) {
    ev.mensaje = fallo[1];
    ev.nivel = c[fallo[0]] === "medio" ? "medio" : "mal";
  } else {
    ev.mensaje = "¡Perfecto! No te muevas…";
    ev.nivel = "ok";
    ev.listo = true;
  }
  return ev;
}

// ---------------- Dibujo ----------------
function dibujar(ev) {
  const vw = video.videoWidth, vh = video.videoHeight;
  if (overlay.width !== vw || overlay.height !== vh) { overlay.width = vw; overlay.height = vh; }
  ctx.clearRect(0, 0, vw, vh);
  const lw = Math.max(2, vw / 500);

  if (!ev.pts) {
    // Guía: silueta de 4 dedos (meñique más corto) donde deben ir las yemas.
    const lado = Math.min(vw, vh);
    const ancho = lado * 0.15, sep = ancho * 0.25;
    const x0 = (vw - (4 * ancho + 3 * sep)) / 2, base = vh * 0.62;
    // Mano derecha con la palma a la cámara: meñique a la izquierda, índice a la derecha.
    const largos = [0.36, 0.47, 0.5, 0.42].map((f) => f * lado);
    if ($("selMano").value === "izquierda") largos.reverse();
    ctx.setLineDash([lw * 5, lw * 4]);
    ctx.strokeStyle = "rgba(255,255,255,.7)";
    ctx.lineWidth = lw * 1.5;
    largos.forEach((l, i) => {
      ctx.beginPath();
      ctx.roundRect(x0 + i * (ancho + sep), base - l, ancho, l, [ancho / 2, ancho / 2, 0, 0]);
      ctx.stroke();
    });
    ctx.setLineDash([]);
    return;
  }
  ctx.strokeStyle = "rgba(255,255,255,.55)";
  ctx.lineWidth = lw;
  ctx.beginPath();
  for (const [a, b] of CONEXIONES) { ctx.moveTo(...ev.pts[a]); ctx.lineTo(...ev.pts[b]); }
  ctx.stroke();

  const umbral = cfg.enfoqueMin();
  ev.regiones.forEach((r, i) => {
    const e = ev.yemas?.[i]?.enfoque ?? 0;
    ctx.strokeStyle = ev.listo ? "#3ddc84" : e >= umbral ? "#3ddc84" : e >= umbral * 0.7 ? "#ffb300" : "#ff5252";
    ctx.lineWidth = lw * 2;
    const q = esquinas(r);
    ctx.beginPath();
    ctx.moveTo(...q[0]); q.slice(1).forEach((p) => ctx.lineTo(...p)); ctx.closePath();
    ctx.stroke();
  });
}

function pintarChecklist(ev) {
  for (const li of document.querySelectorAll("#checklist li")) {
    li.className = ev.checks[li.dataset.k] || "";
  }
  mensaje.textContent = ev.mensaje;
  mensaje.className = "mensaje " + (ev.nivel === "ok" ? "ok" : ev.nivel === "medio" ? "medio" : "");
}

// ---------------- Control de cámara (enfoque, linterna, zoom) ----------------
async function aplicar(constraints) {
  try { await estado.track.applyConstraints({ advanced: [constraints] }); return true; } catch { return false; }
}

/** En Android/Chrome: dirige el autofoco a las yemas y fuerza un re-enfoque si sigue borroso. */
async function guiarEnfoque(ev, ahora) {
  const caps = estado.caps;
  if (!ev.regiones || !estado.track) return;
  if (caps.pointsOfInterest !== undefined && ahora - estado.ultimoPOI > 1200) {
    estado.ultimoPOI = ahora;
    const cx = ev.regiones.reduce((s, r) => s + (r.inicio[0] + r.fin[0]) / 2, 0) / 4 / video.videoWidth;
    const cy = ev.regiones.reduce((s, r) => s + (r.inicio[1] + r.fin[1]) / 2, 0) / 4 / video.videoHeight;
    aplicar({ pointsOfInterest: [{ x: cx, y: cy }] });
  }
  const borroso = ev.checks.enfoque === "mal" && ev.checks.encuadre === "ok";
  if (!borroso) { estado.malEnfoqueDesde = 0; return; }
  if (!estado.malEnfoqueDesde) estado.malEnfoqueDesde = ahora;
  const modos = caps.focusMode || [];
  if (ahora - estado.malEnfoqueDesde > 1800 && modos.includes("single-shot")) {
    estado.malEnfoqueDesde = ahora;
    await aplicar({ focusMode: "single-shot" });
    setTimeout(() => modos.includes("continuous") && aplicar({ focusMode: "continuous" }), 900);
  }
}

function leerResGuardada() {
  try { return localStorage.getItem(CLAVE_RES); } catch { return null; }
}
function guardarRes(valor) {
  try { localStorage.setItem(CLAVE_RES, valor); } catch { /* modo privado: se vuelve a medir */ }
}

function resolucionPedida() {
  const sel = $("selResolucion").value;
  if (sel !== "auto") return sel.split("x").map(Number);
  Object.assign(rendimiento, { muestras: [], vistos: 0, inicio: 0 });
  const guardada = leerResGuardada();
  if (guardada) {
    const motivo = rendimiento.decidido && rendimiento.motivo ? rendimiento.motivo : "medida antes en este equipo";
    Object.assign(rendimiento, { decidido: true, motivo });
    return guardada.split("x").map(Number);
  }
  // Pista previa: equipos con pocos núcleos o poca RAM empiezan directo en 720p.
  const nucleos = navigator.hardwareConcurrency || 8, memoria = navigator.deviceMemory || 8;
  if (nucleos <= 4 || memoria <= 2) {
    Object.assign(rendimiento, { decidido: true, motivo: `equipo modesto (${nucleos} núcleos)` });
    guardarRes("1280x720");
    return [1280, 720];
  }
  Object.assign(rendimiento, { decidido: false, motivo: "midiendo…" });
  return [1920, 1080];
}

/** Mide el costo real por cuadro (detección + análisis) y baja a 720p si el equipo no da. */
function medirRendimiento(msCuadro, ahora) {
  if (rendimiento.decidido || $("selResolucion").value !== "auto" || estado.capturando) return;
  if (++rendimiento.vistos <= MUESTRAS_CALENTAMIENTO) { rendimiento.inicio = ahora; return; }
  rendimiento.muestras.push(msCuadro);
  if (rendimiento.muestras.length < MUESTRAS_MEDICION) return;
  const orden = [...rendimiento.muestras].sort((a, b) => a - b);
  const mediana = orden[orden.length >> 1];
  const fps = (MUESTRAS_MEDICION * 1000) / (ahora - rendimiento.inicio);
  rendimiento.decidido = true;
  if (mediana > MAX_MS_POR_CUADRO) {
    rendimiento.motivo = `bajó a 720p (${mediana.toFixed(0)} ms/cuadro, ${fps.toFixed(0)} fps)`;
    guardarRes("1280x720");
    iniciarCamara();
  } else {
    rendimiento.motivo = `fluido (${mediana.toFixed(0)} ms/cuadro, ${fps.toFixed(0)} fps)`;
    guardarRes("1920x1080");
  }
}

function esCelularVertical() {
  return matchMedia("(orientation: portrait)").matches && matchMedia("(pointer: coarse)").matches;
}

/** Ajusta solo la presentación: la cámara sigue capturando el cuadro completo en su modo normal. */
function ajustarVisor() {
  const vw = video.videoWidth, vh = video.videoHeight;
  if (!vw || !vh) return;
  // En celular vertical con video horizontal se muestra un recuadro más alto con la parte central
  // del video (object-fit: cover en video y overlay, así los recuadros siguen alineados).
  const recorte = esCelularVertical() && vw > vh;
  visor.classList.toggle("recorte", recorte);
  visor.style.aspectRatio = recorte ? "4 / 5" : `${vw} / ${vh}`;
  visor.style.maxWidth = recorte ? "" : `calc(var(--visor-alto) * ${vw / vh})`;
}

async function iniciarCamara() {
  if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
    mensaje.textContent = "La cámara requiere HTTPS (o localhost). Ver README: scripts/run_https.sh";
    return;
  }
  estado.stream?.getTracks().forEach((t) => t.stop());
  const [w, h] = resolucionPedida();
  const id = $("selCamara").value;
  const video_c = { width: { ideal: w }, height: { ideal: h }, frameRate: { ideal: 30 } };
  if (id) video_c.deviceId = { exact: id }; else video_c.facingMode = { ideal: "environment" };
  try {
    estado.stream = await navigator.mediaDevices.getUserMedia({ audio: false, video: video_c });
  } catch (e) {
    mensaje.textContent = "No se pudo abrir la cámara: " + e.message;
    return;
  }
  estado.track = estado.stream.getVideoTracks()[0];
  estado.caps = estado.track.getCapabilities?.() || {};
  const caps = estado.caps;
  if (caps.focusMode?.includes("continuous")) await aplicar({ focusMode: "continuous" });
  if (caps.exposureMode?.includes("continuous")) await aplicar({ exposureMode: "continuous" });
  if (caps.whiteBalanceMode?.includes("continuous")) await aplicar({ whiteBalanceMode: "continuous" });

  $("btnLinterna").hidden = !caps.torch;
  estado.linterna = false;
  $("btnLinterna").classList.remove("activo");
  configurarRango("lblZoom", "rngZoom", caps.zoom, estado.track.getSettings().zoom, (v) => aplicar({ zoom: v }));
  configurarRango("lblFoco", "rngFoco", caps.focusDistance, estado.track.getSettings().focusDistance,
    (v) => aplicar({ focusMode: "manual", focusDistance: v }));

  const ajustes = estado.track.getSettings();
  estado.espejo = ajustes.facingMode === "user" || (!ajustes.facingMode && !/back|rear|trasera|environment/i.test(estado.track.label));
  visor.classList.toggle("espejo", estado.espejo);
  video.srcObject = estado.stream;
  await video.play().catch(() => {});
  await new Promise((r) => (video.readyState >= 2 ? r() : video.addEventListener("loadeddata", r, { once: true })));
  ajustarVisor();
  await listarCamaras(ajustes.deviceId);
}

function configurarRango(lblId, rngId, cap, actual, alCambiar) {
  const lbl = $(lblId), rng = $(rngId);
  lbl.hidden = !cap || cap.max === cap.min;
  if (lbl.hidden) return;
  Object.assign(rng, { min: cap.min, max: cap.max, step: cap.step || (cap.max - cap.min) / 100 });
  rng.value = actual ?? cap.min;
  rng.oninput = () => alCambiar(+rng.value);
}

async function listarCamaras(actual) {
  const sel = $("selCamara");
  const camaras = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === "videoinput");
  sel.innerHTML = camaras.map((c, i) => `<option value="${c.deviceId}">${c.label || "Cámara " + (i + 1)}</option>`).join("");
  if (actual) sel.value = actual;
}

// ---------------- Captura y envío ----------------
const landmarksDe = (res) => res?.landmarks?.[0]?.map((p) => ({ x: p.x, y: p.y, z: p.z })) || null;

/** Guarda el cuadro actual como candidato a enviar (solo se copia cuando mejora al anterior). */
function guardarCandidato(ev, ahora) {
  if (candCanvas.width !== video.videoWidth || candCanvas.height !== video.videoHeight) {
    candCanvas.width = video.videoWidth; candCanvas.height = video.videoHeight;
  }
  candCtx.drawImage(video, 0, 0);
  auto.cand = { puntaje: ev.puntaje, landmarks: landmarksDe(estado.ultimoResultado), t: ahora };
}

/** Envío automático continuo: mientras la mano esté bien puesta, manda (de una en una) la mejor toma
 *  reciente hasta que una alcance la calidad objetivo. Nunca hay más de una petición en curso. */
function autoCaptura(ev, ahora) {
  if (estado.capturando || ahora < auto.libreDesde) return;
  if (auto.cand && ahora - auto.cand.t > CANDIDATO_VIGENTE_MS) auto.cand = null;
  if (!ev.enviable) return;
  if (!auto.cand) auto.candDesde = ahora;
  if (!auto.cand || ev.puntaje > auto.cand.puntaje) guardarCandidato(ev, ahora);
  // Todo en verde: se manda ya; si no, se espera un momento para quedarse con el cuadro más nítido.
  if (ev.listo || ahora - auto.candDesde >= ELECCION_MS) {
    const { landmarks } = auto.cand;
    auto.cand = null;
    capturar("auto", landmarks);
  }
}

async function capturar(motivo, landmarksCand) {
  if (estado.capturando || !video.videoWidth) return;
  estado.capturando = true;
  const deCandidato = motivo === "auto" && landmarksCand !== undefined;
  let blob, conLandmarks = deCandidato ? landmarksCand : landmarksDe(estado.ultimoResultado);
  if (motivo !== "auto") {
    $("flash").classList.add("activo");
    setTimeout(() => $("flash").classList.remove("activo"), 60);
  }
  try {
    if ($("chkFoto").checked && "ImageCapture" in window) {
      // La foto puede tener otra relación de aspecto: el servidor vuelve a detectar la mano.
      blob = await new ImageCapture(estado.track).takePhoto();
      conLandmarks = null;
    }
  } catch { blob = null; }
  if (!blob) {
    let c = candCanvas;
    if (!deCandidato) {
      c = document.createElement("canvas");
      c.width = video.videoWidth; c.height = video.videoHeight;
      c.getContext("2d").drawImage(video, 0, 0);
    }
    blob = await new Promise((r) => c.toBlob(r, "image/jpeg", 0.95));
  }
  if (motivo !== "auto") mensaje.textContent = "Procesando huellas…";
  pintarEstadoAuto(motivo === "auto" ? "Analizando huellas…" : "");
  await enviar(blob, conLandmarks, motivo);
  estado.capturando = false;
  $("progreso").hidden = true;
}

function pintarEstadoAuto(texto) {
  const el = $("estadoAuto");
  el.hidden = !texto;
  el.textContent = texto;
  $("progreso").hidden = !estado.capturando;
}

/** Texto bajo el visor mientras se intenta solo: invita a seguir acomodando la mano. */
function textoEsperaAuto() {
  const mejor = estado.mejor?.calidad_global;
  const base = "Ajusta despacio la mano hasta que los 4 cuadros estén en verde";
  return mejor ? `${base} · mejor hasta ahora: ${mejor.puntuacion}/${cfg.calidadObjetivo()}` : base;
}

async function enviar(blob, landmarks, motivo) {
  const fd = new FormData();
  fd.append("imagen", blob, "captura.jpg");
  if (landmarks) fd.append("landmarks", JSON.stringify(landmarks));
  fd.append("mano", $("selMano").value);
  if (motivo === "auto") fd.append("automatico", "true");
  try {
    const url = TOKEN ? `/api/v1/captura/${TOKEN}/intentos` : "/api/v1/huellas/captura";
    const r = await fetch(url, { method: "POST", body: fd });
    const respuesta = await r.json().catch(() => ({}));
    if (!r.ok) {
      const error = new Error(respuesta.detail || r.statusText);
      error.codigo = r.status;
      throw error;
    }
    const datos = TOKEN ? respuesta.resultado : respuesta;
    estado.intentos++;
    auto.errores = 0;
    const cal = datos.calidad_global.puntuacion;
    if (!estado.mejor || cal > estado.mejor.calidad_global.puntuacion) {
      estado.mejor = datos;
      mostrarResultado(estado.mejor);
    }
    if (TOKEN) {
      sesion = respuesta.sesion;
      $("btnFinalizar").hidden = false;
      if (sesion.estado !== "pendiente") { terminar(); return; }
    }
    const suficiente = cal >= cfg.calidadObjetivo() && datos.calidad_global.dedos_validos === 4;
    if (motivo === "auto" && !suficiente && $("chkReintentar").checked) {
      auto.libreDesde = performance.now() + PAUSA_MS;
      pintarEstadoAuto(textoEsperaAuto());
    } else {
      estado.armado = false;
      $("btnReiniciar").hidden = false;
      pintarEstadoAuto("");
      if (suficiente) mensaje.textContent = "¡Listo! Huellas capturadas";
    }
  } catch (e) {
    if (motivo !== "auto") { mensaje.textContent = "Error: " + e.message; return; }
    if (e.codigo === 422) {
      // El servidor no encontró la mano en esa toma: es normal mientras el usuario se acomoda.
      auto.libreDesde = performance.now() + PAUSA_MS;
      pintarEstadoAuto(textoEsperaAuto());
    } else if (e.codigo === 409 || e.codigo === 429) {
      estado.armado = false;
      pintarEstadoAuto(e.message);
    } else {
      // Red o servidor con problemas: espera cada vez más (1 s, 2 s, 4 s… hasta 10 s) para no saturarlo.
      auto.errores++;
      auto.libreDesde = performance.now() + Math.min(10000, 1000 * 2 ** (auto.errores - 1));
      pintarEstadoAuto(`Sin conexión con el servidor, reintentando… (${e.message})`);
    }
  }
}

function reiniciar() {
  Object.assign(estado, { armado: true, intentos: 0, mejor: null });
  Object.assign(auto, { libreDesde: 0, cand: null, errores: 0 });
  $("btnReiniciar").hidden = true;
  pintarEstadoAuto("");
}

// ---------------- Resultado ----------------
const ETAPAS = [["original", "Original"], ["contraste", "Contraste"], ["realce_gabor", "Realce Gabor"], ["binaria", "Binaria"], ["minucias", "Minucias"]];
const NOMBRES = { indice: "Índice", medio: "Medio", anular: "Anular", menique: "Meñique" };

function mostrarResultado(d) {
  $("vacio").hidden = true;
  $("resultado").hidden = false;
  const g = d.calidad_global;
  const ins = $("insignia");
  ins.hidden = false;
  ins.className = "insignia n" + g.nivel;
  ins.textContent = `${d.estado === "ok" ? "Aceptada" : "Calidad insuficiente"} · ${g.etiqueta}`;
  $("resumen").innerHTML = [
    ["Calidad global", `${g.puntuacion}/100`], ["Nivel (1-5)", g.nivel], ["Mano", d.mano],
    ["Dedos válidos", `${g.dedos_validos}/4`], ["Minucias", d.dedos.reduce((s, x) => s + (x.minucias_total || 0), 0)],
    ["Proceso", `${d.tiempo_ms} ms`],
  ].map(([k, v]) => `<div><span>${k}</span><b>${v}</b></div>`).join("");
  $("advertencias").innerHTML = d.advertencias.map((a) => `<li>${a}</li>`).join("");
  $("imgMano").src = d.imagen_anotada || "";
  $("tablaDedos").innerHTML = d.dedos.map((f) => {
    if (f.estado !== "ok") {
      return `<div class="dedo"><div class="dedo-cab"><h3>${NOMBRES[f.dedo]}</h3><small>${f.estado}</small></div></div>`;
    }
    const q = f.calidad;
    const term = f.minucias.filter((m) => m.tipo === "terminacion").length;
    return `<div class="dedo">
      <div class="dedo-cab">
        <h3>${NOMBRES[f.dedo]} <small>· NIST ${f.posicion_nist}</small></h3>
        <span class="insignia n${q.nivel}">${q.puntuacion} · ${q.etiqueta}</span>
      </div>
      <div class="etapas">${ETAPAS.map(([k, t]) => `<figure data-titulo="${NOMBRES[f.dedo]} · ${t}"><img src="${f.imagenes[k]}" alt="${t}"><figcaption>${t}</figcaption></figure>`).join("")}</div>
      <div class="metricas">
        ${metrica("Coherencia", q.coherencia, q.componentes.coherencia)}
        ${metrica("Nitidez", q.nitidez, q.componentes.nitidez)}
        ${metrica("Contraste", q.contraste, q.componentes.contraste)}
        ${metrica("Área útil", q.area_util, q.componentes.area)}
        ${metrica("PPI nativo", q.ppi_nativo_estimado, q.componentes.resolucion)}
        <div><span>Periodo crestas</span><br><b>${q.periodo_crestas_px} px</b></div>
        <div><span>Minucias</span><br><b>${f.minucias_total}</b> <small>(${term} term. / ${f.minucias_total - term} bif.)</small></div>
      </div>
    </div>`;
  }).join("");
  estado.json = d;
  pintarJson();
  $("btnCopiar").disabled = $("btnDescargar").disabled = false;
}

function metrica(nombre, valor, comp) {
  return `<div><span>${nombre}</span><br><b>${valor}</b><div class="barra-cal"><i style="width:${Math.round(comp * 100)}%"></i></div></div>`;
}

function pintarJson() {
  if (!estado.json) return;
  const completo = $("chkBase64").checked;
  const texto = JSON.stringify(estado.json, (k, v) =>
    !completo && typeof v === "string" && v.startsWith("data:image")
      ? `${v.slice(0, 40)}… (${Math.round(v.length * 0.75 / 1024)} KB)` : v, 2);
  const esc = texto.replace(/&/g, "&amp;").replace(/</g, "&lt;");
  $("json").innerHTML = esc.replace(/("(?:\\.|[^"\\])*")(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d+)?(?:e[+-]?\d+)?/gi, (m, s, dosPuntos, b) =>
    s ? (dosPuntos ? `<span class="j-k">${s}</span>${dosPuntos}` : `<span class="j-s">${s}</span>`)
      : b ? `<span class="j-b">${m}</span>` : `<span class="j-n">${m}</span>`);
}

// ---------------- Bucle principal ----------------
function bucle() {
  const ahora = performance.now();
  if (estado.landmarker && video.readyState >= 2 && video.currentTime !== estado.ultimoTiempo) {
    if (overlay.width !== video.videoWidth || overlay.height !== video.videoHeight) ajustarVisor();
    estado.ultimoTiempo = video.currentTime;
    const t0 = performance.now();
    const res = estado.landmarker.detectForVideo(video, ahora);
    estado.ultimoResultado = res;
    const ev = evaluar(res, ahora);
    dibujar(ev);
    if (!estado.capturando || estado.armado) pintarChecklist(ev);
    guiarEnfoque(ev, ahora);
    medirRendimiento(performance.now() - t0, ahora);

    if ($("chkAuto").checked && estado.armado) autoCaptura(ev, ahora);
    estado.fps = 0.9 * estado.fps + 0.1 * (1000 / (ahora - estado.ultimoFrame));
    estado.ultimoFrame = ahora;
    const modoRes = $("selResolucion").value === "auto" ? ` (auto: ${rendimiento.motivo})` : "";
    $("telemetria").textContent = `${video.videoWidth}×${video.videoHeight}${modoRes} · ${estado.fps.toFixed(0)} fps · ` +
      `autofoco: ${estado.caps.pointsOfInterest !== undefined ? "dirigido a yemas" : (estado.caps.focusMode || ["no controlable"]).join("/")} · ` +
      `linterna: ${estado.caps.torch ? "sí" : "no"} · zoom: ${estado.caps.zoom ? "sí" : "no"}`;
  }
  requestAnimationFrame(bucle);
}

async function cargarModelo() {
  const fileset = await FilesetResolver.forVisionTasks("/static/vendor/wasm");
  const opciones = (delegate) => ({
    baseOptions: { modelAssetPath: "/static/vendor/hand_landmarker.task", delegate },
    runningMode: "VIDEO", numHands: 1,
    minHandDetectionConfidence: 0.5,
    // Valores bajos para no perder el seguimiento cuando la mano está muy cerca y la palma sale del cuadro.
    minHandPresenceConfidence: 0.3, minTrackingConfidence: 0.3,
  });
  try {
    return await HandLandmarker.createFromOptions(fileset, opciones("GPU"));
  } catch {
    return HandLandmarker.createFromOptions(fileset, opciones("CPU"));
  }
}

// ---------------- Eventos ----------------
for (const [rng, out] of [["rngAncho", "outAncho"], ["rngEnfoque", "outEnfoque"], ["rngCalidad", "outCalidad"]]) {
  $(rng).addEventListener("input", () => { $(out).textContent = $(rng).value; });
}
$("btnCapturar").onclick = () => { reiniciar(); estado.armado = false; capturar("manual"); };
$("btnReiniciar").onclick = reiniciar;
$("btnLinterna").onclick = async () => {
  estado.linterna = !estado.linterna;
  if (await aplicar({ torch: estado.linterna })) $("btnLinterna").classList.toggle("activo", estado.linterna);
};
$("selCamara").onchange = iniciarCamara;
video.addEventListener("resize", ajustarVisor);
addEventListener("orientationchange", () => setTimeout(ajustarVisor, 300));
addEventListener("resize", ajustarVisor);
$("selResolucion").onchange = () => {
  // Elegir "Automática" de nuevo borra la decisión anterior y vuelve a medir.
  if ($("selResolucion").value === "auto") { try { localStorage.removeItem(CLAVE_RES); } catch { /* sin almacenamiento */ } }
  iniciarCamara();
};
$("archivo").onchange = async (e) => {
  const f = e.target.files[0];
  if (!f) return;
  reiniciar();
  estado.armado = false;
  mensaje.textContent = "Procesando foto…";
  await enviar(f, null, "archivo");
  e.target.value = "";
};
$("chkBase64").onchange = pintarJson;
$("btnCopiar").onclick = () => navigator.clipboard.writeText(JSON.stringify(estado.json, null, 2));
$("btnDescargar").onclick = () => {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([JSON.stringify(estado.json, null, 2)], { type: "application/json" }));
  a.download = `huellas_${estado.json.id}.json`;
  a.click();
};
$("tablaDedos").addEventListener("click", (e) => {
  const fig = e.target.closest("figure");
  if (!fig) return;
  const vg = $("visorGrande");
  vg.querySelector("img").src = fig.querySelector("img").src;
  vg.querySelector("p").textContent = fig.dataset.titulo;
  vg.hidden = false;
});
$("visorGrande").onclick = () => { $("visorGrande").hidden = true; };
$("btnFinalizar").onclick = async () => {
  const r = await fetch(`/api/v1/captura/${TOKEN}/finalizar`, { method: "POST" });
  const datos = await r.json();
  if (r.ok) terminar(); else mensaje.textContent = datos.detail;
};

// ---------------- Modo sesión ----------------
async function cargarSesion() {
  const r = await fetch(`/api/v1/captura/${TOKEN}${MODO_QR ? "?modo=qr" : ""}`);
  const datos = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(datos.detail || "Enlace de captura inválido");
  sesion = datos;
  document.body.classList.add("modo-sesion");
  $("subtitulo").textContent = `${sesion.proyecto} · captura de huellas`;
  $("selMano").value = sesion.mano;
  $("selMano").disabled = sesion.mano !== "auto";
  $("rngCalidad").value = sesion.calidad_minima;
  estado.intentos = sesion.intentos;
}

/** Fin de la sesión: apaga la cámara y pasa a la vista final, reemplazando esta página en el historial
 *  para que "atrás" o recargar no vuelvan a abrir la cámara. La vista final avisa o regresa al proyecto. */
function terminar() {
  estado.armado = false;
  estado.capturando = true;
  estado.stream?.getTracks().forEach((t) => t.stop());
  location.replace(`/fin/${TOKEN}${MODO_QR ? "?modo=qr" : ""}`);
}

(async () => {
  if (TOKEN) {
    try {
      await cargarSesion();
    } catch (e) {
      mensaje.textContent = e.message;
      return;
    }
    if (sesion.estado !== "pendiente") { terminar(); return; }
  }
  mensaje.textContent = "Cargando modelo de manos…";
  try {
    estado.landmarker = await cargarModelo();
  } catch (e) {
    mensaje.textContent = "No se pudo cargar el modelo: " + e.message;
    return;
  }
  await iniciarCamara();
  requestAnimationFrame(bucle);
})();
