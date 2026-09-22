/* Front del dashboard — hito 6.
 *
 * Sin framework y sin build: se abre en local, no necesita tooling.
 * Todo el estado vive en memoria; nada de localStorage.
 *
 *   api(path)            fetch a /api con manejo de error
 *   render.garden()      tarjetas + equity del jardín vs benchmark
 *   render.lineage()     el DAG genealógico + slider temporal
 *   render.breeding()    la cría: qué se concibe, qué pasa la criba, qué dura
 *   render.generations() fitness por generación, demografía, diversidad
 *   render.species()     scatter genético + heatmap de correlación
 *   render.journal()     entradas del diario con sus propuestas
 *   render.bot(id)       ficha individual
 *
 * El slider temporal pide /api/lineage/graph?until_generation=N y redibuja,
 * así que reproducir la evolución es sólo mover el slider.
 */

const css = (name) =>
  getComputedStyle(document.documentElement).getPropertyValue(name).trim();

const FAMILY_COLORS = {
  TREND:          css('--fam-trend')          || '#4ea36b',
  MEAN_REVERSION: css('--fam-mean-reversion') || '#d98a3a',
  BREAKOUT:       css('--fam-breakout')       || '#4a8fd4',
  MOMENTUM:       css('--fam-momentum')       || '#b566c7',
  VOLATILITY:     css('--fam-volatility')     || '#d4c04a',
  MICROSTRUCTURE: css('--fam-micro')          || '#4ac7bd',
  HYBRID:         css('--fam-hybrid')         || '#9aa3a8',
};

const OPERATOR_COLORS = {
  SEED:      '#6f7c74',
  MUTATE:    FAMILY_COLORS.TREND,
  CROSSOVER: FAMILY_COLORS.BREAKOUT,
  FUSION:    FAMILY_COLORS.MOMENTUM,
  GRAFT:     FAMILY_COLORS.VOLATILITY,
};

const CAUSE_COLORS = {
  LOW_FITNESS:      '#d4645a',
  DRAWDOWN_BREAKER: '#b03a30',
  IDLE:             '#8c968f',
  CLONE:            '#d98a3a',
  GARDENER:         '#4a8fd4',
  ORPHAN_ENSEMBLE:  '#b566c7',
  FAILED_INCUBATION:'#5d6560',
  DESCONOCIDA:      '#3d4440',
};

const TEXT = css('--text') || '#dfe5e0';
const MUTED = css('--muted') || '#8c968f';
const LINE = css('--line') || '#262c27';

const state = {
  view: 'garden',
  summary: null,
  generations: null,
  graphs: {},        // until_generation → grafo ya pedido
  maxGen: 0,
  until: null,
  playing: false,
  hovered: null,
  selected: null,
  filters: { family: '', status: '', lineage: '' },
  charts: {},
  loaded: {},
  live: true,        // refrescarse solo mientras el jardín se mueva
  pulse: null,       // firma del último pulso visto
};

//: Cada cuánto se pregunta al jardín si se ha movido. Tres segundos es
//: bastante para una vela por hora en vivo y para un dry-run a 20 velas/s, y
//: el endpoint /api/pulse son tres claves de garden_meta y un recuento.
const PULSO_MS = 3000;

// --------------------------------------------------------------------------- //
// Utilidades                                                                   //
// --------------------------------------------------------------------------- //

async function api(path) {
  const res = await fetch(`/api${path}`);
  if (!res.ok) throw new Error(`${path}: ${res.status}`);
  return res.json();
}

const esc = (s) =>
  String(s == null ? '' : s).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const num = (v, d = 2) => (v == null || Number.isNaN(v) ? '—' : Number(v).toFixed(d));
const sgn = (v, d = 3) => (v == null ? '—' : (v >= 0 ? '+' : '') + Number(v).toFixed(d));
const pct = (v, d = 1) => (v == null ? '—' : (100 * Number(v)).toFixed(d) + ' %');
const money = (v) =>
  v == null ? '—' : Number(v).toLocaleString('es-ES', { maximumFractionDigits: 2 });
const famColor = (f) => FAMILY_COLORS[f] || FAMILY_COLORS.HYBRID;
const shortId = (id) => String(id || '').replace(/^(bot|gen|lin)_/, '');
const shortLineage = (l) => String(l || '').replace(/^lin_/, '').replace(/_[0-9a-f]{6}$/, '');

const signClass = (v) => (v == null ? '' : v >= 0 ? 'pos' : 'neg');

function statusTag(status) {
  const cls = { ALIVE: 'alive', CULLED: 'culled', RETIRED: 'retired' }[status] || '';
  return `<span class="tag ${cls}">${esc(status)}</span>`;
}

function botLink(id, name) {
  return `<span class="bot-link" data-bot="${esc(id)}">${esc(name || shortId(id))}</span>`;
}

function familyDot(family) {
  return `<span class="dot" style="background:${famColor(family)}"></span>`;
}

/** Un gráfico de ECharts, o un aviso si la CDN no ha cargado. */
function chart(id, option) {
  const nodo = document.getElementById(id);
  if (!nodo) return null;
  if (!window.echarts) {
    nodo.innerHTML =
      '<div class="chart-fallback">ECharts no ha cargado (¿sin red?). ' +
      'Las tablas de esta vista siguen funcionando.</div>';
    return null;
  }
  let c = state.charts[id];
  if (!c || c.getDom() !== nodo) {
    c = echarts.init(nodo, null, { renderer: 'canvas' });
    state.charts[id] = c;
  }
  c.setOption(option, { notMerge: true });
  return c;
}

const BASE_AXIS = {
  axisLine: { lineStyle: { color: LINE } },
  axisLabel: { color: MUTED },
  splitLine: { lineStyle: { color: LINE, opacity: 0.45 } },
  nameTextStyle: { color: MUTED },
};

const BASE_OPTION = {
  animation: false,
  textStyle: { color: TEXT, fontFamily: 'system-ui, sans-serif' },
  tooltip: {
    backgroundColor: '#12160f',
    borderColor: LINE,
    textStyle: { color: TEXT, fontSize: 12 },
  },
  legend: { textStyle: { color: MUTED }, top: 0 },
};

function tabla(cabeceras, filas, opciones = {}) {
  if (!filas.length) return `<p class="empty">${esc(opciones.empty || 'Sin datos todavía.')}</p>`;
  const th = cabeceras
    .map((c) => `<th class="${c.num ? 'num' : ''}">${esc(c.label)}</th>`)
    .join('');
  const tr = filas
    .map(
      (f) =>
        '<tr>' +
        f.map((c, i) => `<td class="${cabeceras[i].num ? 'num' : ''}">${c}</td>`).join('') +
        '</tr>'
    )
    .join('');
  const cuerpo = `<table><thead><tr>${th}</tr></thead><tbody>${tr}</tbody></table>`;
  return opciones.scroll ? `<div class="scroll">${cuerpo}</div>` : cuerpo;
}

function tarjeta(label, value, sub) {
  return `<div class="card"><div class="label">${esc(label)}</div>
    <div class="value">${value}</div>
    ${sub ? `<div class="sub">${sub}</div>` : ''}</div>`;
}

// --------------------------------------------------------------------------- //
// Vistas                                                                       //
// --------------------------------------------------------------------------- //

const render = {};

render.garden = async function () {
  const raiz = document.getElementById('view-garden');
  const s = (state.summary = await api('/garden/summary'));
  const equity = await api('/garden/equity');

  const alertas = s.alerts.length
    ? s.alerts
        .map(
          (a) =>
            `<div class="alert ${a.kind === 'GARDEN_DRAWDOWN' ? 'critical' : ''}">
               <strong>${esc(a.kind)}</strong> · ${num(a.value, 3)} frente a ${num(a.threshold, 3)}
               ${a.detail ? ' · ' + esc(a.detail) : ''}</div>`
        )
        .join('')
    : '<p class="empty">Sin alertas abiertas.</p>';

  raiz.innerHTML = `
    <div class="cards">
      ${tarjeta('Generación', s.generation, `${esc(s.symbol)} ${esc(s.timeframe)} · ${esc(s.venue)}`)}
      ${tarjeta('Bots vivos', s.n_alive, `${s.n_total} han existido · ${s.n_dead} muertos`)}
      ${tarjeta('Especies', s.n_species, `diversidad ${num(s.genetic_diversity, 3)} (suelo ${s.diversity_floor})`)}
      ${tarjeta('Capital del jardín', money(s.capital), `invertido ${money(s.capital_invested)}`)}
      ${tarjeta('Alfa vs benchmark', s.garden_alpha == null ? '—' : `<span class="${signClass(s.garden_alpha)}">${pct(s.garden_alpha)}</span>`, 'el jardín vivo lo rellena')}
      ${tarjeta('Fitness', sgn(s.fitness_median), `mejor ${sgn(s.fitness_best)}`)}
    </div>

    <h2>Capital del jardín contra el benchmark</h2>
    <div class="panel"><div id="chart-garden-equity" class="chart"></div></div>

    <div class="grid2">
      <div>
        <h2>Los mejores vivos</h2>
        <div class="panel">${tabla(
          [
            { label: 'Bot' },
            { label: 'Familia' },
            { label: 'Operador' },
            { label: 'Gen', num: true },
            { label: 'Fitness', num: true },
            { label: 'Ops', num: true },
          ],
          s.top_bots.map((b) => [
            botLink(b.bot_id, b.name),
            familyDot(b.family) + esc(b.family),
            esc(b.operator),
            b.born_generation,
            `<span class="${signClass(b.fitness)}">${sgn(b.fitness)}</span>`,
            b.total_trades,
          ]),
          { empty: 'El jardín todavía no tiene bots vivos con fitness.' }
        )}</div>
      </div>
      <div>
        <h2>Estado</h2>
        <div class="panel">
          ${alertas}
          <p class="hint" style="margin-top:12px">
            Semilla ${esc(s.seed)} · creado ${esc(s.created_at || '—')} ·
            ${s.n_born} nacimientos desde la siembra ·
            ${s.pending_gardener_sessions} sesión(es) de jardinero abiertas.
          </p>
        </div>
        <h2>Reparto por familia</h2>
        <div class="panel"><div id="chart-families" class="chart" style="height:260px"></div></div>
      </div>
    </div>`;

  if (equity.empty) {
    document.getElementById('chart-garden-equity').innerHTML =
      '<div class="chart-fallback">El jardín vivo todavía no ha corrido: no hay curva de capital.<br>' +
      'Las cosechas de incubadora no simulan cartera, sólo criban genomas.</div>';
  } else {
    chart('chart-garden-equity', {
      ...BASE_OPTION,
      grid: { left: 60, right: 20, top: 24, bottom: 40 },
      tooltip: { ...BASE_OPTION.tooltip, trigger: 'axis' },
      legend: { ...BASE_OPTION.legend, data: ['jardín', 'benchmark'] },
      xAxis: { ...BASE_AXIS, type: 'time' },
      yAxis: { ...BASE_AXIS, type: 'value', scale: true },
      series: [
        {
          name: 'jardín', type: 'line', showSymbol: false, smooth: false,
          lineStyle: { width: 1.6, color: FAMILY_COLORS.TREND },
          data: equity.points.map((p) => [p.ts, p.equity]),
        },
        {
          name: 'benchmark', type: 'line', showSymbol: false,
          lineStyle: { width: 1.2, color: MUTED, type: 'dashed' },
          data: equity.points.map((p) => [p.ts, p.benchmark]),
        },
      ],
    });
  }

  const familias = Object.entries(s.families);
  chart('chart-families', {
    ...BASE_OPTION,
    legend: { ...BASE_OPTION.legend, show: false },
    series: [
      {
        type: 'pie',
        radius: ['45%', '72%'],
        itemStyle: { borderColor: css('--panel') || '#171b18', borderWidth: 2 },
        label: { color: MUTED, fontSize: 11, formatter: '{b}  {c}' },
        data: familias.map(([f, n]) => ({
          name: f, value: n, itemStyle: { color: famColor(f) },
        })),
      },
    ],
  });
};

// -- genealogía ------------------------------------------------------------- //

render.lineage = async function () {
  const raiz = document.getElementById('view-lineage');
  if (!state.loaded.lineage) {
    raiz.innerHTML = `
      <div class="toolbar">
        <button class="action" id="play">▶ reproducir</button>
        <input type="range" id="slider" min="0" max="0" value="0" />
        <span class="gen-label" id="gen-label">generación —</span>
        <label>familia</label><select id="f-family"><option value="">todas</option></select>
        <label>estado</label>
        <select id="f-status">
          <option value="">todos</option>
          <option value="ALIVE">vivos</option>
          <option value="CULLED">muertos</option>
          <option value="RETIRED">jubilados</option>
        </select>
        <label>linaje</label><select id="f-lineage"><option value="">todos</option></select>
        <button class="action" id="reset">ver todo</button>
      </div>
      <div class="panel" style="padding:6px"><div id="lineage-graph"></div></div>
      <div class="legend">
        <span class="item"><span class="dot" style="background:${TEXT}"></span>vivo</span>
        <span class="item"><span class="ring"></span>muerto</span>
        <span class="item"><span class="dot" style="background:${MUTED}"></span>jubilado</span>
        <span class="item">carril y color = familia de ideas · radio ∝ fitness</span>
        <span class="item">— mutación y cruce · - - fusión (varios padres)</span>
        <span class="item">pasa el ratón por un nodo: se resalta su linaje entero; clic abre la ficha</span>
      </div>
      <p class="hint" id="lineage-info"></p>`;
    state.loaded.lineage = true;

    document.getElementById('slider').addEventListener('input', (e) => {
      pararPlayback();
      dibujarHasta(Number(e.target.value));
    });
    document.getElementById('play').addEventListener('click', reproducir);
    document.getElementById('reset').addEventListener('click', () => {
      state.filters = { family: '', status: '', lineage: '' };
      ['f-family', 'f-status', 'f-lineage'].forEach((id) => (document.getElementById(id).value = ''));
      state.selected = null;
      dibujarHasta(state.maxGen);
      document.getElementById('slider').value = state.maxGen;
    });
    ['family', 'status', 'lineage'].forEach((k) => {
      document.getElementById(`f-${k}`).addEventListener('change', (e) => {
        state.filters[k] = e.target.value;
        pintarGrafo(state.graphs[state.until]);
      });
    });
  }

  const completo = await grafo(null);
  state.maxGen = completo.generations.max;
  const slider = document.getElementById('slider');
  slider.min = completo.generations.min;
  slider.max = state.maxGen;
  if (state.until == null) state.until = state.maxGen;
  slider.value = state.until;

  const selFam = document.getElementById('f-family');
  if (selFam.options.length <= 1) {
    completo.families.forEach((f) => selFam.add(new Option(f, f)));
    const selLin = document.getElementById('f-lineage');
    completo.lineages.forEach((l) => selLin.add(new Option(shortLineage(l), l)));
  }
  dibujarHasta(state.until);
};

async function grafo(until) {
  const clave = until == null ? 'all' : until;
  if (!state.graphs[clave]) {
    const q = until == null ? '' : `?until_generation=${until}`;
    state.graphs[clave] = await api(`/lineage/graph${q}`);
  }
  return state.graphs[clave];
}

async function dibujarHasta(n) {
  state.until = n;
  document.getElementById('gen-label').textContent = `generación ${n} / ${state.maxGen}`;
  const g = await grafo(n);
  pintarGrafo(g);
}

/** Índices padre→hijo e hijo→padre del grafo que se está viendo. */
function parentesco(g) {
  const padres = new Map();
  const hijos = new Map();
  g.edges.forEach((e) => {
    if (!padres.has(e.target)) padres.set(e.target, []);
    if (!hijos.has(e.source)) hijos.set(e.source, []);
    padres.get(e.target).push(e.source);
    hijos.get(e.source).push(e.target);
  });
  return { padres, hijos };
}

function linajeCompleto(g, id) {
  const { padres, hijos } = parentesco(g);
  const vistos = new Set([id]);
  const subir = [id];
  while (subir.length) {
    const cur = subir.pop();
    (padres.get(cur) || []).forEach((p) => {
      if (!vistos.has(p)) { vistos.add(p); subir.push(p); }
    });
  }
  const bajar = [id];
  while (bajar.length) {
    const cur = bajar.pop();
    (hijos.get(cur) || []).forEach((c) => {
      if (!vistos.has(c)) { vistos.add(c); bajar.push(c); }
    });
  }
  return vistos;
}

/** Coloca los nodos: X = generación de nacimiento, Y = carril por familia.
 *
 * Un carril por linaje era la forma obvia y no funcionaba: un jardín de 60
 * semillas tiene 60 linajes, casi todos de un solo bot, así que el lienzo eran
 * sesenta carriles vacíos con los fundadores apilados en una columna a la
 * izquierda y un eje Y sin etiquetas. Las familias de ideas son siete como
 * mucho, todas se pueden etiquetar y agrupan por lo que de verdad se quiere
 * comparar: qué clase de estrategia está ganando.
 *
 * El orden es **alfabético a propósito**, no por tamaño. Ordenar por población
 * hace que los carriles se reordenen solos al mover el slider del tiempo, y la
 * reproducción de la evolución se convierte en un baile de filas en el que no
 * se puede seguir nada.
 */
function disponer(g) {
  // La colocación sólo depende del grafo y se recalcula en cada redibujado del
  // resaltado: con dos mil nodos, memorizarla se nota al pasar el ratón.
  if (g._layout) return g._layout;
  const cuenta = new Map();
  const linajes = new Set();
  g.nodes.forEach((n) => {
    cuenta.set(n.family, (cuenta.get(n.family) || 0) + 1);
    linajes.add(n.lineage);
  });
  const carriles = [...cuenta.keys()].sort((a, b) => a.localeCompare(b));
  const idx = new Map(carriles.map((l, i) => [l, i]));

  // Los que comparten carril y generación se reparten en vertical dentro del
  // carril, ordenados por fitness: la columna de fundadores deja de ser una
  // pila y se lee como un degradado de quién arrancó mejor.
  const porId = new Map(g.nodes.map((n) => [n.id, n]));
  const celdas = new Map();
  g.nodes.forEach((n) => {
    const k = `${n.family}|${n.generation}`;
    if (!celdas.has(k)) celdas.set(k, []);
    celdas.get(k).push(n.id);
  });
  // Una sola pasada de orden por celda, y la posición dentro de la celda se
  // memoriza: buscar el índice con indexOf por cada nodo sería cuadrático, y
  // esto se recalcula en cada redibujado del resaltado.
  const posicion = new Map();
  celdas.forEach((ids) => {
    ids.sort((a, b) => {
      const fa = porId.get(a).fitness, fb = porId.get(b).fitness;
      if (fa == null && fb == null) return a.localeCompare(b);
      if (fa == null) return -1;
      if (fb == null) return 1;
      return fa - fb || a.localeCompare(b);
    });
    ids.forEach((id, i) => posicion.set(id, i));
  });

  const puntos = g.nodes.map((n) => {
    const total = celdas.get(`${n.family}|${n.generation}`).length;
    const i = posicion.get(n.id);
    // Hasta 0.62 de carril: deja un pasillo visible entre familias, que es
    // lo que hace que se lean como seis grupos y no como una columna.
    const ancho = total > 1 ? Math.min(0.62, 0.07 * total) : 0;
    const desplazamiento = total === 1 ? 0 : (i / (total - 1) - 0.5) * ancho;
    // El eje Y crece hacia arriba, así que se invierte el índice para que el
    // primer carril quede arriba y el orden se lea como la leyenda.
    const carril = carriles.length - 1 - idx.get(n.family);
    return { ...n, x: n.generation, y: carril + desplazamiento };
  });
  g._layout = { carriles, puntos, cuenta, linajes: linajes.size };
  return g._layout;
}

function pintarGrafo(g) {
  if (!g) return;
  const { carriles, puntos, cuenta, linajes } = disponer(g);
  const info = document.getElementById('lineage-info');
  const resaltado = state.hovered || state.selected;
  const linaje = resaltado ? linajeCompleto(g, resaltado) : null;

  const fits = puntos.map((p) => p.fitness).filter((v) => v != null);
  const lo = fits.length ? Math.min(...fits) : 0;
  const hi = fits.length ? Math.max(...fits) : 1;
  const tam = (f) => (f == null ? 6 : 7 + 15 * ((f - lo) / (hi - lo || 1)));

  const visible = (n) => {
    const f = state.filters;
    if (f.family && n.family !== f.family) return false;
    if (f.status && n.status_at !== f.status) return false;
    if (f.lineage && n.lineage !== f.lineage) return false;
    return true;
  };

  const indices = new Map(puntos.map((p, i) => [p.id, i]));
  const datos = puntos.map((n) => {
    const color = famColor(n.family);
    const muerto = n.status_at === 'CULLED';
    const jubilado = n.status_at === 'RETIRED';
    let opacidad = visible(n) ? (muerto ? 0.5 : 1) : 0.05;
    if (linaje && !linaje.has(n.id)) opacidad = Math.min(opacidad, 0.07);
    const estilo = muerto
      ? { color: 'transparent', borderColor: color, borderWidth: 1.4, opacity: opacidad }
      : jubilado
      ? { color: MUTED, borderColor: color, borderWidth: 1, opacity: opacidad }
      : { color, borderColor: color, opacity: opacidad };
    if (n.is_ensemble) {
      // Anillo: los bots de fusión se reconocen de un vistazo.
      estilo.borderColor = TEXT;
      estilo.borderWidth = 2;
    }
    return {
      id: n.id, name: n.name, value: [n.x, n.y],
      symbolSize: tam(n.fitness), itemStyle: estilo, raw: n,
    };
  });

  const enlaces = g.edges
    .filter((e) => indices.has(e.source) && indices.has(e.target))
    .map((e) => {
      const fusion = e.operator === 'FUSION';
      let opacidad = fusion ? 0.75 : 0.45;
      if (linaje && !(linaje.has(e.source) && linaje.has(e.target))) opacidad = 0.05;
      return {
        source: indices.get(e.source),
        target: indices.get(e.target),
        lineStyle: {
          color: fusion ? FAMILY_COLORS.MOMENTUM : MUTED,
          width: fusion ? 1.8 : 1,
          type: fusion ? 'dashed' : 'solid',
          opacity: opacidad,
          curveness: 0.08,
        },
      };
    });

  const vivos = puntos.filter((n) => n.status_at === 'ALIVE').length;
  info.innerHTML =
    `${puntos.length} bots hasta la generación ${state.until} · ${vivos} vivos en ese momento · ` +
    `${enlaces.length} vínculos de parentesco · ${carriles.length} familias · ` +
    `${linajes} linajes` +
    (g.collapsed && g.collapsed.length
      ? ` · ${g.collapsed.length} grupos de linajes extintos plegados (límite ${g.node_limit} nodos)`
      : '') +
    (resaltado ? ` · resaltado el linaje de ${esc(resaltado)}` : '');

  const c = chart('lineage-graph', {
    ...BASE_OPTION,
    grid: { left: 150, right: 26, top: 18, bottom: 46 },
    tooltip: {
      ...BASE_OPTION.tooltip,
      formatter: (p) => {
        if (p.dataType !== 'node') return '';
        const n = p.data.raw;
        return `<strong>${esc(n.name)}</strong><br>${esc(n.family)} · ${esc(n.operator || '')}<br>
                nacido en la generación ${n.generation}${n.died_generation != null ? `, muerto en la ${n.died_generation} (${esc(n.death_cause || '')})` : ''}<br>
                fitness ${sgn(n.fitness)} · profundidad evolutiva ${n.depth}<br>
                linaje ${esc(shortLineage(n.lineage))}${n.is_ensemble ? ' · fusión' : ''}`;
      },
    },
    xAxis: {
      ...BASE_AXIS, type: 'value', name: 'generación de nacimiento',
      nameLocation: 'middle', nameGap: 28,
      min: -0.6, max: state.maxGen + 0.6, minInterval: 1,
    },
    yAxis: {
      // Los límites son enteros a propósito: con un mínimo fraccionario ECharts
      // pone las marcas en -0.7, 0.3, 1.3… y ninguna cae sobre un carril, así
      // que el eje se queda sin etiquetas.
      ...BASE_AXIS, type: 'value', min: -1, max: carriles.length, interval: 1,
      splitLine: { lineStyle: { color: LINE, opacity: 0.3 } },
      axisLabel: {
        color: MUTED, fontSize: 11, hideOverlap: true,
        // Ahora se etiquetan todos: siete familias caben y cada una dice algo.
        formatter: (v) => {
          const familia = carriles[carriles.length - 1 - v];
          if (!Number.isInteger(v) || !familia) return '';
          return `${familia.toLowerCase()}  ${cuenta.get(familia)}`;
        },
      },
    },
    dataZoom: [
      { type: 'inside', xAxisIndex: 0, filterMode: 'none' },
      { type: 'inside', yAxisIndex: 0, filterMode: 'none' },
    ],
    series: [
      {
        type: 'graph', coordinateSystem: 'cartesian2d', layout: 'none',
        data: datos, links: enlaces,
        edgeSymbol: ['none', 'arrow'], edgeSymbolSize: 5,
        emphasis: { scale: false, focus: 'none' },
        labelLayout: { hideOverlap: true },
        label: { show: false },
      },
    ],
  });

  if (c && !c._kgBound) {
    c._kgBound = true;
    c.on('click', (p) => {
      if (p.dataType === 'node') abrirBot(p.data.id);
    });
    c.on('mouseover', (p) => {
      if (p.dataType === 'node' && state.hovered !== p.data.id) {
        state.hovered = p.data.id;
        pintarGrafo(state.graphs[state.until]);
      }
    });
    c.on('mouseout', () => {
      if (state.hovered) {
        state.hovered = null;
        pintarGrafo(state.graphs[state.until]);
      }
    });
  }
}

function pararPlayback() {
  state.playing = false;
  const b = document.getElementById('play');
  if (b) b.textContent = '▶ reproducir';
}

async function reproducir() {
  const boton = document.getElementById('play');
  if (state.playing) return pararPlayback();
  state.playing = true;
  boton.textContent = '⏸ pausa';
  let n = state.until >= state.maxGen ? 0 : state.until;
  while (state.playing && n <= state.maxGen) {
    document.getElementById('slider').value = n;
    await dibujarHasta(n);
    await new Promise((r) => setTimeout(r, 650));
    n += 1;
  }
  pararPlayback();
}

// -- cría ------------------------------------------------------------------- //

render.breeding = async function () {
  const raiz = document.getElementById('view-breeding');
  const b = await api('/breeding');

  const totales = b.by_generation.reduce(
    (acc, g) => ({
      candidates: acc.candidates + g.candidates,
      passed: acc.passed + g.passed,
      born: acc.born + g.born,
      alive: acc.alive + g.alive,
    }),
    { candidates: 0, passed: 0, born: 0, alive: 0 }
  );

  const motivos = Object.entries(b.reject_reasons);
  const gens = b.by_generation.map((g) => g.generation);

  raiz.innerHTML = `
    <p class="hint">La cría del jardín, de principio a fin: cada generación concibe candidatos,
      la incubadora los criba contra el walk-forward y sólo los que pasan nacen. Lo que
      <em>no</em> nació también es información: dice si los operadores están produciendo
      ideas nuevas o clones del mismo bot.</p>

    <div class="cards">
      ${tarjeta('Concebidos', totales.candidates, 'candidatos pasados por la incubadora')}
      ${tarjeta('Aprobados', totales.passed, `${pct(totales.candidates ? totales.passed / totales.candidates : null)} de la criba`)}
      ${tarjeta('Nacidos', totales.born, 'entraron al jardín')}
      ${tarjeta('Siguen vivos', totales.alive, `${pct(totales.born ? totales.alive / totales.born : null)} de los nacidos`)}
      ${tarjeta('Fusiones', b.fusions.length, 'bots con varios padres')}
    </div>

    <div class="grid2">
      <div>
        <h2>El embudo, generación a generación</h2>
        <div class="panel"><div id="chart-funnel" class="chart"></div></div>
      </div>
      <div>
        <h2>Por qué se descartan</h2>
        <div class="panel"><div id="chart-rejects" class="chart"></div></div>
      </div>
    </div>

    <h2>Qué operador está criando mejor</h2>
    <p class="hint">Cupo configurado: mutación ${pct(b.shares.mutation, 0)} · cruce
      ${pct(b.shares.crossover, 0)} · fusión ${pct(b.shares.fusion, 0)} · semilla
      ${pct(b.shares.seed, 0)}. La supervivencia mide qué fracción de los hijos de cada
      operador aguanta tres generaciones.</p>
    <div class="panel">${tabla(
      [
        { label: 'Operador' },
        { label: 'Nacidos', num: true },
        { label: 'Vivos', num: true },
        { label: 'Supervivencia 3 gen', num: true },
        { label: 'Edad media', num: true },
        { label: 'Fitness medio', num: true },
      ],
      b.by_operator.map((o) => [
        `<span class="dot" style="background:${OPERATOR_COLORS[o.operator] || MUTED}"></span>${esc(o.operator)}`,
        o.nacidos,
        o.vivos,
        b.survival_rates[o.operator] == null ? '—' : pct(b.survival_rates[o.operator], 0),
        num(o.edad_media, 1),
        `<span class="${signClass(o.fitness_medio)}">${sgn(o.fitness_medio)}</span>`,
      ]),
      { empty: 'Todavía no ha nacido nadie.' }
    )}</div>

    <div class="grid2">
      <div>
        <h2>Fusiones</h2>
        <p class="hint">Un hijo con varios padres. La fusión sólo vale si descorrelaciona:
          por eso el jardín exige correlación baja entre los padres.</p>
        <div class="panel">${tabla(
          [
            { label: 'Bot' },
            { label: 'Padres', num: true },
            { label: 'Gen', num: true },
            { label: 'Estado' },
            { label: 'Fitness', num: true },
          ],
          b.fusions.map((f) => [
            botLink(f.bot_id, f.name),
            f.n_padres,
            f.born_generation,
            statusTag(f.status),
            `<span class="${signClass(f.fitness_effective)}">${sgn(f.fitness_effective)}</span>`,
          ]),
          { empty: 'Ninguna fusión todavía.', scroll: true }
        )}</div>
      </div>
      <div>
        <h2>Quién está poblando el jardín</h2>
        <p class="hint">Los bots con más descendencia directa. Si uno solo domina esta
          tabla, el jardín está convergiendo hacia su idea.</p>
        <div class="panel">${tabla(
          [
            { label: 'Bot' },
            { label: 'Familia' },
            { label: 'Hijos', num: true },
            { label: 'Estado' },
          ],
          b.most_prolific.map((p) => [
            botLink(p.bot_id, p.name),
            familyDot(p.family) + esc(p.family),
            p.hijos,
            statusTag(p.status),
          ]),
          { empty: 'Nadie ha tenido descendencia todavía.', scroll: true }
        )}</div>
      </div>
    </div>

    <h2>La criba, candidato a candidato</h2>
    <div class="toolbar">
      <label>generación</label>
      <select id="inc-gen">${gens
        .slice()
        .reverse()
        .map((g) => `<option value="${g}">${g}</option>`)
        .join('')}</select>
      <span id="inc-resumen" class="gen-label" style="min-width:auto"></span>
    </div>
    <div class="panel" id="inc-tabla"></div>`;

  chart('chart-funnel', {
    ...BASE_OPTION,
    grid: { left: 50, right: 20, top: 30, bottom: 36 },
    tooltip: { ...BASE_OPTION.tooltip, trigger: 'axis', axisPointer: { type: 'shadow' } },
    legend: { ...BASE_OPTION.legend, data: ['concebidos', 'aprobados', 'nacidos', 'siguen vivos'] },
    xAxis: { ...BASE_AXIS, type: 'category', data: gens, name: 'gen', nameGap: 22 },
    yAxis: { ...BASE_AXIS, type: 'value' },
    series: [
      { name: 'concebidos', type: 'bar', itemStyle: { color: '#2f3a33' },
        data: b.by_generation.map((g) => g.candidates) },
      { name: 'aprobados', type: 'bar', itemStyle: { color: FAMILY_COLORS.BREAKOUT },
        data: b.by_generation.map((g) => g.passed) },
      { name: 'nacidos', type: 'line', symbol: 'circle', symbolSize: 5,
        lineStyle: { color: FAMILY_COLORS.TREND, width: 1.6 },
        itemStyle: { color: FAMILY_COLORS.TREND },
        data: b.by_generation.map((g) => g.born) },
      { name: 'siguen vivos', type: 'line', symbol: 'circle', symbolSize: 4,
        lineStyle: { color: FAMILY_COLORS.VOLATILITY, width: 1.2, type: 'dashed' },
        itemStyle: { color: FAMILY_COLORS.VOLATILITY },
        data: b.by_generation.map((g) => g.alive) },
    ],
  });

  chart('chart-rejects', {
    ...BASE_OPTION,
    grid: { left: 180, right: 30, top: 16, bottom: 32 },
    tooltip: { ...BASE_OPTION.tooltip, trigger: 'item' },
    legend: { show: false },
    xAxis: { ...BASE_AXIS, type: 'value' },
    yAxis: {
      ...BASE_AXIS, type: 'category',
      data: motivos.map(([k]) => k).reverse(),
      axisLabel: { color: MUTED, fontSize: 11.5 },
    },
    series: [
      {
        type: 'bar',
        data: motivos
          .map(([k, v]) => ({
            value: v,
            itemStyle: {
              color: k.startsWith('clon') ? FAMILY_COLORS.MEAN_REVERSION : '#3f5a4a',
            },
          }))
          .reverse(),
        label: { show: true, position: 'right', color: MUTED, fontSize: 11 },
      },
    ],
  });

  const selector = document.getElementById('inc-gen');
  const pintarIncubacion = async () => {
    const g = Number(selector.value);
    const inc = await api(`/incubation/${g}`);
    document.getElementById('inc-resumen').textContent =
      `${inc.candidates} candidatos · ${inc.passed} aprobados (${pct(inc.pass_rate, 0)}) · ` +
      `umbral de sortino ${num(inc.threshold_used)}`;
    document.getElementById('inc-tabla').innerHTML = tabla(
      [
        { label: 'Genoma' },
        { label: 'Familia' },
        { label: 'Operador' },
        { label: 'Padres' },
        { label: 'Sortino', num: true },
        { label: 'Drawdown', num: true },
        { label: 'Ops/pliegue', num: true },
        { label: 'Decay', num: true },
        { label: 'Resultado' },
      ],
      inc.runs.map((r) => [
        `<code>${esc(shortId(r.genome_id))}</code>`,
        familyDot(r.family) + esc(r.family),
        esc(r.operator),
        (r.parents || []).map((p) => `<code>${esc(shortId(p))}</code>`).join(' ') || '—',
        num(r.median_sortino),
        pct(r.median_drawdown),
        num(r.median_trades, 0),
        num(r.oos_decay),
        r.passed
          ? '<span class="tag pass">nace</span>'
          : `<span class="tag fail">${esc(r.reject_category)}</span> <span style="color:var(--muted)">${esc(r.reject_reason)}</span>`,
      ]),
      { empty: 'Esta generación no concibió nada.', scroll: true }
    );
  };
  selector.addEventListener('change', pintarIncubacion);
  if (gens.length) await pintarIncubacion();
};

// -- generaciones ----------------------------------------------------------- //

render.generations = async function () {
  const raiz = document.getElementById('view-generations');
  const gens = (state.generations = await api('/generations'));
  const x = gens.map((g) => g.generation);

  const operadores = [...new Set(gens.flatMap((g) => Object.keys(g.births_by_operator)))].sort();
  const causas = [...new Set(gens.flatMap((g) => Object.keys(g.deaths_by_cause)))].sort();
  const familias = [...new Set(gens.flatMap((g) => Object.keys(g.family_shares)))].sort();

  raiz.innerHTML = `
    <h2>Fitness por generación</h2>
    <p class="hint">La banda va del peor al mejor; la línea es la mediana. Si la banda se
      estrecha y la mediana sube, el jardín converge. Si se estrecha y no sube, se está
      quedando sin ideas.</p>
    <div class="panel"><div id="chart-fitness" class="chart tall"></div></div>

    <div class="grid2">
      <div>
        <h2>Demografía</h2>
        <p class="hint">Nacimientos por operador hacia arriba, muertes por causa hacia abajo.</p>
        <div class="panel"><div id="chart-demografia" class="chart"></div></div>
      </div>
      <div>
        <h2>Diversidad genética</h2>
        <p class="hint">Con el suelo marcado: por debajo, la cosecha se llena de semillas nuevas.</p>
        <div class="panel"><div id="chart-diversidad" class="chart"></div></div>
      </div>
    </div>

    <h2>Reparto por familia de ideas</h2>
    <div class="panel"><div id="chart-familias-gen" class="chart"></div></div>

    <h2>Tabla de generaciones</h2>
    <div class="panel">${tabla(
      [
        { label: 'Gen', num: true },
        { label: 'Población', num: true },
        { label: 'Nacen', num: true },
        { label: 'Mueren', num: true },
        { label: 'Fusiones', num: true },
        { label: 'Descartados', num: true },
        { label: 'Especies', num: true },
        { label: 'Diversidad', num: true },
        { label: 'Fitness mediana', num: true },
        { label: 'Mejor', num: true },
        { label: 'Mejor bot' },
      ],
      gens
        .slice()
        .reverse()
        .map((g) => [
          g.generation,
          g.population_size,
          g.births,
          g.deaths,
          g.fusions,
          g.discarded,
          g.n_species,
          num(g.genetic_diversity, 3),
          `<span class="${signClass(g.fitness_median)}">${sgn(g.fitness_median)}</span>`,
          sgn(g.fitness_best),
          g.best_bot_id ? botLink(g.best_bot_id) : '—',
        ]),
      { empty: 'Todavía no se ha cerrado ninguna generación.', scroll: true }
    )}</div>`;

  chart('chart-fitness', {
    ...BASE_OPTION,
    grid: { left: 56, right: 24, top: 30, bottom: 40 },
    tooltip: { ...BASE_OPTION.tooltip, trigger: 'axis' },
    legend: { ...BASE_OPTION.legend, data: ['peor→mejor', 'p25→p75', 'mediana'] },
    xAxis: { ...BASE_AXIS, type: 'category', data: x, name: 'generación', nameGap: 24 },
    yAxis: { ...BASE_AXIS, type: 'value', scale: true },
    series: [
      { name: 'peor→mejor', type: 'line', stack: 'ext', symbol: 'none',
        lineStyle: { opacity: 0 }, areaStyle: { color: 'transparent' },
        data: gens.map((g) => g.fitness_worst) },
      { name: 'peor→mejor', type: 'line', stack: 'ext', symbol: 'none',
        lineStyle: { opacity: 0 },
        areaStyle: { color: FAMILY_COLORS.TREND, opacity: 0.1 },
        data: gens.map((g) => (g.fitness_best == null || g.fitness_worst == null ? null : g.fitness_best - g.fitness_worst)) },
      { name: 'p25→p75', type: 'line', stack: 'iqr', symbol: 'none',
        lineStyle: { opacity: 0 }, areaStyle: { color: 'transparent' },
        data: gens.map((g) => g.fitness_p25) },
      { name: 'p25→p75', type: 'line', stack: 'iqr', symbol: 'none',
        lineStyle: { opacity: 0 },
        areaStyle: { color: FAMILY_COLORS.TREND, opacity: 0.22 },
        data: gens.map((g) => (g.fitness_p75 == null || g.fitness_p25 == null ? null : g.fitness_p75 - g.fitness_p25)) },
      { name: 'mediana', type: 'line', symbol: 'none',
        lineStyle: { color: FAMILY_COLORS.TREND, width: 2 },
        data: gens.map((g) => g.fitness_median) },
    ],
  });

  chart('chart-demografia', {
    ...BASE_OPTION,
    grid: { left: 46, right: 20, top: 34, bottom: 36 },
    tooltip: { ...BASE_OPTION.tooltip, trigger: 'axis', axisPointer: { type: 'shadow' } },
    legend: { ...BASE_OPTION.legend, type: 'scroll', textStyle: { color: MUTED, fontSize: 11 } },
    xAxis: { ...BASE_AXIS, type: 'category', data: x },
    yAxis: { ...BASE_AXIS, type: 'value', axisLabel: { color: MUTED, formatter: (v) => Math.abs(v) } },
    series: [
      ...operadores.map((op) => ({
        name: op, type: 'bar', stack: 'nacen',
        itemStyle: { color: OPERATOR_COLORS[op] || MUTED },
        data: gens.map((g) => g.births_by_operator[op] || 0),
      })),
      ...causas.map((c) => ({
        name: c, type: 'bar', stack: 'mueren',
        itemStyle: { color: CAUSE_COLORS[c] || '#5d6560' },
        data: gens.map((g) => -(g.deaths_by_cause[c] || 0)),
      })),
    ],
  });

  const suelo = state.summary ? state.summary.diversity_floor : null;
  chart('chart-diversidad', {
    ...BASE_OPTION,
    grid: { left: 50, right: 20, top: 24, bottom: 36 },
    tooltip: { ...BASE_OPTION.tooltip, trigger: 'axis' },
    legend: { show: false },
    xAxis: { ...BASE_AXIS, type: 'category', data: x },
    yAxis: { ...BASE_AXIS, type: 'value', min: 0, max: 1 },
    series: [
      {
        type: 'line', symbol: 'none',
        lineStyle: { color: FAMILY_COLORS.MICROSTRUCTURE, width: 1.8 },
        areaStyle: { color: FAMILY_COLORS.MICROSTRUCTURE, opacity: 0.12 },
        data: gens.map((g) => g.genetic_diversity),
        markLine: suelo == null ? undefined : {
          silent: true, symbol: 'none',
          lineStyle: { color: css('--bad') || '#d4645a', type: 'dashed' },
          // Dentro del área: pegada al borde se recortaba a "su", porque el
          // margen derecho del grid son 20 px.
          label: {
            color: MUTED, position: 'insideEndTop',
            formatter: `suelo ${suelo}`,
          },
          data: [{ yAxis: suelo }],
        },
      },
    ],
  });

  chart('chart-familias-gen', {
    ...BASE_OPTION,
    grid: { left: 46, right: 20, top: 30, bottom: 36 },
    tooltip: { ...BASE_OPTION.tooltip, trigger: 'axis' },
    legend: { ...BASE_OPTION.legend, type: 'scroll', textStyle: { color: MUTED, fontSize: 11 } },
    xAxis: { ...BASE_AXIS, type: 'category', data: x, boundaryGap: false },
    yAxis: { ...BASE_AXIS, type: 'value', max: 1, axisLabel: { color: MUTED, formatter: (v) => `${Math.round(v * 100)} %` } },
    series: familias.map((f) => ({
      name: f, type: 'line', stack: 'fam', symbol: 'none',
      lineStyle: { width: 0 }, areaStyle: { color: famColor(f), opacity: 0.75 },
      data: gens.map((g) => g.family_shares[f] || 0),
    })),
  });
};

// -- especies --------------------------------------------------------------- //

render.species = async function () {
  const raiz = document.getElementById('view-species');
  const [especies, scatter, corr] = await Promise.all([
    api('/species'),
    api('/species/scatter'),
    api('/species/correlation'),
  ]);

  raiz.innerHTML = `
    <p class="hint">El scatter genético y el heatmap de correlación juntos responden a la
      pregunta que importa: ¿este jardín tiene ideas distintas o cincuenta copias de la misma?</p>
    <div class="grid2">
      <div>
        <h2>Mapa genético de la población viva</h2>
        <div class="panel"><div id="chart-scatter" class="chart tall"></div>
          <p class="hint" id="scatter-info"></p></div>
      </div>
      <div>
        <h2>Correlación entre curvas de capital</h2>
        <div class="panel"><div id="chart-corr" class="chart tall"></div></div>
      </div>
    </div>

    <h2>Especies de la generación ${especies.length ? especies[0].generation : '—'}</h2>
    <div class="panel">${tabla(
      [
        { label: 'Especie' },
        { label: 'Familia dominante' },
        { label: 'Tamaño', num: true },
        { label: 'Fitness medio', num: true },
        { label: 'Fitness compartido', num: true },
        { label: 'Cupo de cría', num: true },
        { label: 'Edad media', num: true },
        { label: 'Representante' },
      ],
      especies.map((e) => [
        `<code>${esc(e.species_id)}</code>`,
        familyDot(e.dominant_family) + esc(e.dominant_family || '—'),
        e.size,
        `<span class="${signClass(e.mean_fitness)}">${sgn(e.mean_fitness)}</span>`,
        sgn(e.shared_fitness),
        e.breeding_quota,
        num(e.mean_age, 1),
        e.representative_id ? botLink(e.representative_id, e.representative_name) : '—',
      ]),
      { empty: 'Todavía no hay especies registradas.', scroll: true }
    )}</div>`;

  if (scatter.empty) {
    document.getElementById('chart-scatter').innerHTML =
      '<div class="chart-fallback">Hacen falta al menos dos bots vivos para proyectar el mapa genético.</div>';
  } else {
    document.getElementById('scatter-info').textContent =
      `MDS clásico sobre la matriz de distancias · ${pct(scatter.explained)} de la varianza en dos ejes · ` +
      `distancia media ${num(scatter.mean_distance, 3)} (clon por debajo de ${scatter.clone_threshold}, ` +
      `misma especie por debajo de ${scatter.species_threshold})` +
      (scatter.truncated
        ? ` · se proyectan los ${scatter.points.length} mejores de ${scatter.n_alive} vivos: la matriz es O(n²)`
        : '');
    const fits = scatter.points.map((p) => p.fitness).filter((v) => v != null);
    const lo = fits.length ? Math.min(...fits) : 0;
    const hi = fits.length ? Math.max(...fits) : 1;
    chart('chart-scatter', {
      ...BASE_OPTION,
      grid: { left: 46, right: 24, top: 20, bottom: 36 },
      tooltip: {
        ...BASE_OPTION.tooltip,
        formatter: (p) =>
          `<strong>${esc(p.data.raw.name)}</strong><br>${esc(p.data.raw.family)}<br>
           fitness ${sgn(p.data.raw.fitness)} · especie ${esc(p.data.raw.species_id || '—')}`,
      },
      legend: { show: false },
      xAxis: { ...BASE_AXIS, type: 'value', scale: true, axisLabel: { show: false } },
      yAxis: { ...BASE_AXIS, type: 'value', scale: true, axisLabel: { show: false } },
      series: [
        {
          type: 'scatter',
          data: scatter.points.map((p) => ({
            value: [p.x, p.y],
            raw: p,
            symbolSize: p.fitness == null ? 8 : 8 + 16 * ((p.fitness - lo) / (hi - lo || 1)),
            itemStyle: { color: famColor(p.family), opacity: 0.85 },
          })),
        },
      ],
    });
  }

  if (corr.empty) {
    document.getElementById('chart-corr').innerHTML =
      '<div class="chart-fallback">No hay curvas de capital que correlacionar.<br>' +
      'El heatmap se llena cuando el jardín vivo lleva unos cuantos ticks.</div>';
  } else {
    const nombres = corr.bots.map((b) => b.name);
    const datos = [];
    corr.matrix.forEach((fila, i) => fila.forEach((v, j) => datos.push([j, i, v])));
    chart('chart-corr', {
      ...BASE_OPTION,
      // El margen derecho deja sitio a la escala de color: en horizontal y
      // abajo se montaba sobre las etiquetas rotadas del eje X.
      grid: { left: 110, right: 92, top: 20, bottom: 90 },
      tooltip: {
        ...BASE_OPTION.tooltip,
        formatter: (p) =>
          `${esc(nombres[p.value[1]])} · ${esc(nombres[p.value[0]])}<br>correlación ${num(p.value[2])}`,
      },
      legend: { show: false },
      xAxis: { ...BASE_AXIS, type: 'category', data: nombres,
               axisLabel: { color: MUTED, rotate: 60, fontSize: 10 } },
      yAxis: { ...BASE_AXIS, type: 'category', data: nombres,
               axisLabel: { color: MUTED, fontSize: 10 } },
      visualMap: {
        min: -1, max: 1, calculable: true, orient: 'vertical',
        right: 8, top: 'middle', itemHeight: 140,
        textStyle: { color: MUTED },
        inRange: { color: ['#4a8fd4', '#171b18', '#d4645a'] },
      },
      series: [{ type: 'heatmap', data: datos, progressive: 2000 }],
    });
  }
};

// -- diario ----------------------------------------------------------------- //

render.journal = async function () {
  const raiz = document.getElementById('view-journal');
  const [diario, eventos] = await Promise.all([api('/journal'), api('/events?limit=200')]);

  const entradas = diario.length
    ? diario
        .map(
          (s) => `
      <div class="panel">
        <h3>Generación ${s.generation} · ${esc(s.trigger)}</h3>
        <p>${esc(s.journal_entry || '')}</p>
        ${tabla(
          [
            { label: 'Propuesta' },
            { label: 'Objetivo' },
            { label: 'Estado' },
            { label: 'Razón' },
            { label: 'Efecto esperado' },
            { label: 'Veredicto' },
          ],
          s.decisions.map((d) => [
            esc(d.kind),
            esc(d.target || '—'),
            esc(d.status),
            esc(d.rationale),
            esc(d.expected_effect),
            esc(d.verdict || '—'),
          ]),
          { empty: 'Sin propuestas en esta sesión.' }
        )}
      </div>`
        )
        .join('')
    : `<p class="empty">Todavía no hay sesiones de jardinero. Las escribe
        <code>keepgarden gardener apply</code> (hito 7).</p>`;

  raiz.innerHTML = `
    <h2>Diario del jardinero</h2>
    ${entradas}
    <h2>Historia del jardín</h2>
    <p class="hint">Los últimos eventos registrados. El dashboard no recalcula historia: la lee.</p>
    <div class="panel">${tabla(
      [
        { label: 'Gen', num: true },
        { label: 'Tipo' },
        { label: 'Bot' },
        { label: 'Resumen' },
      ],
      eventos.map((e) => [
        e.generation == null ? '—' : e.generation,
        esc(e.type),
        e.bot_id ? botLink(e.bot_id) : '—',
        esc(e.summary),
      ]),
      { empty: 'Sin eventos.', scroll: true }
    )}</div>`;
};

// -- salud del sistema -------------------------------------------------------- //

render.health = async function () {
  const raiz = document.getElementById('view-health');
  const h = await api('/health');

  const minutos = (ms) => (ms == null ? '—' : `${Math.round(ms / 60000)} min`);
  const estado = h.status === 'RUNNING' ? 'pos' : h.status === 'DEGRADED' ? 'neg' : '';
  const fresco = h.fresh
    ? '<span class="pos">al día</span>'
    : `<span class="neg">${minutos(h.lag_ms)} por detrás</span>`;
  const presupuesto = Math.round(h.tick_budget_ms / 60000);
  const holgura =
    h.tick_ms == null ? '—' : `${num(h.tick_ms, 1)} ms`;

  raiz.innerHTML = `
    <p class="hint">Lo que hay que poder responder de un vistazo antes de dejar esto
      corriendo treinta días: ¿sigue latiendo?, ¿le llegan las velas?, ¿va sobrado de
      tiempo entre una vela y la siguiente?</p>

    <div class="cards">
      ${tarjeta('Estado', `<span class="${estado}">${esc(h.status)}</span>`, `generación ${h.generation}`)}
      ${tarjeta('Última vela', fresco, h.last_tick_ts ? new Date(h.last_tick_ts).toISOString().slice(0, 16).replace('T', ' ') : 'nunca')}
      ${tarjeta('Tiempo por vela', holgura, `de ${presupuesto} min entre vela y vela`)}
      ${tarjeta('Latencia del venue', h.venue_latency_ms == null ? '—' : `${num(h.venue_latency_ms, 0)} ms`, `${h.venue_failures_24h} frenazos en 24 h`)}
      ${tarjeta('Alertas abiertas', h.alerts.length, h.alerts.length ? h.alerts.map((a) => esc(a.kind)).join(' · ') : 'ninguna')}
    </div>

    <div class="grid2">
      <div>
        <h2>Mercados</h2>
        <div class="panel">${tabla(
          [{ label: 'Mercado' }, { label: 'Bots', num: true }, { label: 'Vivos', num: true }],
          h.symbols.map((s) => [esc(s.symbol), s.bots, s.vivos]),
          { empty: 'El jardín todavía no tiene bots.' }
        )}</div>

        <h2>Copias del jardín</h2>
        <p class="hint">Un jardín es un archivo: copiarlo es todo el respaldo que
          necesita. El motor guarda una cada tantas generaciones
          (<code>storage.snapshot_every_generations</code>).</p>
        <div class="panel">${tabla(
          [{ label: 'Copia' }, { label: 'Tamaño', num: true }],
          h.snapshots.map((s) => [`<code>${esc(s.name)}</code>`, `${s.size_mb} MB`]),
          { empty: 'Todavía no se ha guardado ninguna copia.' }
        )}</div>
      </div>
      <div>
        <h2>Calidad de los datos</h2>
        <div class="panel">${tabla(
          [
            { label: 'Serie' },
            { label: 'Huecos', num: true },
            { label: 'Velas perdidas', num: true },
            { label: 'Rellenados', num: true },
          ],
          h.data_gaps.map((g) => [
            `${esc(g.symbol)} ${esc(g.timeframe)}`,
            g.huecos, g.velas_perdidas, g.rellenados,
          ]),
          { empty: 'Sin huecos registrados: la caché está completa.' }
        )}</div>

        <h2>Anomalías de mercado</h2>
        <p class="hint">Velas con saltos imposibles, OHLC incoherente o volumen negativo.
          El motor no opera sobre ellas.</p>
        <div class="panel">${tabla(
          [{ label: 'Tipo' }, { label: 'Veces', num: true }],
          h.data_anomalies.map((a) => [esc(a.kind), a.n]),
          { empty: 'Ninguna anomalía detectada.' }
        )}</div>
      </div>
    </div>

    <h2>Alertas abiertas</h2>
    <div class="panel">${
      h.alerts.length
        ? h.alerts
            .map(
              (a) =>
                `<div class="alert ${a.kind === 'GARDEN_DRAWDOWN' ? 'critical' : ''}">
                   <strong>${esc(a.kind)}</strong> · ${num(a.value, 3)} frente a ${num(a.threshold, 3)}
                   · desde la generación ${a.raised_gen}${a.detail ? ' · ' + esc(a.detail) : ''}</div>`
            )
            .join('')
        : '<p class="empty">Ninguna.</p>'
    }</div>`;
};

// -- ficha de bot ------------------------------------------------------------ //

async function abrirBot(id) {
  activar('bot');
  await render.bot(id);
}

render.bot = async function (id) {
  const raiz = document.getElementById('view-bot');
  raiz.innerHTML = '<p class="empty">Cargando…</p>';
  const b = await api(`/bots/${encodeURIComponent(id)}`);
  const [equity, trades] = await Promise.all([
    api(`/bots/${encodeURIComponent(id)}/equity`),
    api(`/bots/${encodeURIComponent(id)}/trades?limit=200`),
  ]);

  const chips = (lista) =>
    lista.length
      ? lista
          .map(
            (p) =>
              `${botLink(p.bot_id, p.name)}${p.operator ? ` <span class="tag">${esc(p.operator)}${p.weight != null && p.weight !== 1 ? ' ' + pct(p.weight, 0) : ''}</span>` : ''}`
          )
          .join(' · ')
      : '—';

  const folds = (b.incubation[0] && b.incubation[0].fold_metrics) || [];

  raiz.innerHTML = `
    <div class="toolbar">
      <button class="action" id="volver">← volver a la genealogía</button>
      <span class="gen-label" style="min-width:auto"><code>${esc(b.bot_id)}</code></span>
    </div>

    <h2>${familyDot(b.family)}${esc(b.name)} ${statusTag(b.status)}
      ${b.is_ensemble ? '<span class="tag">fusión</span>' : ''}
      ${b.is_elite ? '<span class="tag">élite</span>' : ''}</h2>

    <div class="cards">
      ${tarjeta('Familia', esc(b.family), `linaje ${esc(shortLineage(b.root_lineage))}`)}
      ${tarjeta('Nacido en', `gen ${b.born_generation}`, `por ${esc(b.operator)}`)}
      ${tarjeta('Edad', `${b.generations_alive} gen`, b.died_generation != null ? `murió en la ${b.died_generation} · ${esc(b.death_cause || '')}` : 'sigue vivo')}
      ${tarjeta('Fitness', `<span class="${signClass(b.fitness_effective)}">${sgn(b.fitness_effective)}</span>`, `incubadora ${sgn(b.fitness_incubator)} · vivo ${sgn(b.fitness_live)}`)}
      ${tarjeta('Capital', money(b.equity), `pico ${money(b.peak_equity)}`)}
      ${tarjeta('Complejidad', b.complexity, `${b.n_features} features · profundidad ${b.max_depth}`)}
    </div>

    <div class="panel">
      <strong>Padres:</strong> ${chips(b.parents)}<br>
      <strong>Hijos:</strong> ${chips(b.children)}
    </div>

    <h2>El genoma, en legible</h2>
    <div class="panel">
      <pre>${esc(b.genome.describe)}</pre>
      <details><summary>JSON crudo</summary>
        <pre>${esc(JSON.stringify(b.genome.raw, null, 2))}</pre></details>
    </div>

    <h2>Curva de capital</h2>
    <div class="panel"><div id="chart-bot-equity" class="chart"></div></div>

    ${folds.length ? `
      <h2>Su paso por la incubadora</h2>
      <p class="hint">Un pliegue por tramo del walk-forward: lo que se midió antes de
        dejarle nacer. El holdout no aparece aquí y no puede aparecer.</p>
      <div class="panel">${tabla(
        [
          { label: 'Pliegue', num: true },
          { label: 'Sortino train', num: true },
          { label: 'Sortino validación', num: true },
          { label: 'Drawdown', num: true },
          { label: 'Ops', num: true },
          { label: 'Profit factor', num: true },
        ],
        folds.map((f) => [
          num(f.fold, 0),
          num(f.train_sortino),
          num(f.sortino),
          pct(f.max_drawdown),
          num(f.n_trades, 0),
          num(f.profit_factor),
        ])
      )}</div>` : ''}

    <h2>Métricas por generación</h2>
    <div class="panel">${tabla(
      [
        { label: 'Gen', num: true },
        { label: 'Ámbito' },
        { label: 'Retorno', num: true },
        { label: 'Sortino', num: true },
        { label: 'Calmar', num: true },
        { label: 'Drawdown', num: true },
        { label: 'Ops', num: true },
        { label: 'Fee drag', num: true },
        { label: 'Fitness', num: true },
      ],
      b.metrics.map((m) => [
        m.generation,
        esc(m.scope),
        pct(m.total_return),
        num(m.sortino),
        num(m.calmar),
        pct(m.max_drawdown),
        num(m.n_trades, 0),
        pct(m.fee_drag),
        `<span class="${signClass(m.fitness)}">${sgn(m.fitness)}</span>`,
      ]),
      { empty: 'Sin métricas todavía.', scroll: true }
    )}</div>

    <h2>Operaciones</h2>
    <div class="panel">${tabla(
      [
        { label: 'Apertura' },
        { label: 'Cierre' },
        { label: 'Lado' },
        { label: 'Salida' },
        { label: 'PnL neto', num: true },
        { label: 'Retorno', num: true },
        { label: 'R', num: true },
      ],
      trades.map((t) => [
        new Date(t.open_ts).toISOString().slice(0, 16).replace('T', ' '),
        t.close_ts ? new Date(t.close_ts).toISOString().slice(0, 16).replace('T', ' ') : '—',
        esc(t.side),
        esc(t.exit_kind || '—'),
        `<span class="${signClass(t.pnl_net)}">${money(t.pnl_net)}</span>`,
        pct(t.return_pct),
        num(t.r_multiple),
      ]),
      { empty: 'Este bot todavía no ha operado en el jardín vivo.', scroll: true }
    )}</div>

    <h2>Su historia</h2>
    <div class="panel">${tabla(
      [{ label: 'Gen', num: true }, { label: 'Tipo' }, { label: 'Resumen' }],
      b.events.map((e) => [e.generation == null ? '—' : e.generation, esc(e.type), esc(e.summary)]),
      { empty: 'Sin eventos.' }
    )}</div>`;

  document.getElementById('volver').addEventListener('click', () => activar('lineage'));

  if (equity.empty) {
    document.getElementById('chart-bot-equity').innerHTML =
      '<div class="chart-fallback">Sin curva de capital: este bot no ha vivido todavía una generación del jardín vivo.</div>';
  } else {
    chart('chart-bot-equity', {
      ...BASE_OPTION,
      grid: { left: 60, right: 20, top: 20, bottom: 36 },
      tooltip: { ...BASE_OPTION.tooltip, trigger: 'axis' },
      legend: { show: false },
      xAxis: { ...BASE_AXIS, type: 'time' },
      yAxis: { ...BASE_AXIS, type: 'value', scale: true },
      series: [
        {
          type: 'line', showSymbol: false,
          lineStyle: { width: 1.6, color: famColor(b.family) },
          data: equity.points.map((p) => [p.ts, p.equity]),
        },
      ],
    });
  }
};

// --------------------------------------------------------------------------- //
// Navegación                                                                   //
// --------------------------------------------------------------------------- //

async function activar(vista) {
  state.view = vista;
  document.querySelectorAll('#nav button').forEach((b) =>
    b.classList.toggle('active', b.dataset.view === vista)
  );
  document.querySelectorAll('.view').forEach((v) => v.classList.remove('active'));
  document.getElementById(`view-${vista}`).classList.add('active');
  if (vista === 'bot') return;
  try {
    await render[vista]();
  } catch (err) {
    document.getElementById(`view-${vista}`).innerHTML =
      `<p class="empty">No se ha podido cargar la vista: ${esc(err.message)}</p>`;
  }
  Object.values(state.charts).forEach((c) => c && c.resize());
}

document.querySelectorAll('#nav button').forEach((btn) => {
  btn.addEventListener('click', () => activar(btn.dataset.view));
});

document.addEventListener('click', (e) => {
  const enlace = e.target.closest('[data-bot]');
  if (enlace) abrirBot(enlace.dataset.bot);
});

window.addEventListener('resize', () => {
  Object.values(state.charts).forEach((c) => c && c.resize());
});

/** ¿Se ha movido el jardín desde la última mirada? */
async function latido() {
  const p = await api('/pulse');
  return {
    firma: `${p.last_tick_ts}|${p.generation}|${p.n_alive}`,
    estado: p.status,
  };
}

/** Refresca la vista activa sin tirar lo que el usuario está mirando.
 *
 * Las cachés del cliente se vacían porque el pasado sí ha cambiado: hay
 * generaciones nuevas. El slider de la genealogía sólo salta al presente si ya
 * estaba en el presente; si lo has movido atrás para mirar algo, se queda.
 */
async function refrescar() {
  const anterior = state.maxGen;
  state.graphs = {};
  state.generations = null;
  if (state.until != null && state.until === anterior) state.until = null;
  await activar(state.view);
  // La portada ya pide el resumen al redibujarse, así que se reaprovecha: es
  // la consulta más cara del visor —recorre bots, generaciones y alertas— y
  // pedirla dos veces cada tres segundos se nota. Las demás vistas sí la
  // necesitan, porque la cabecera vive fuera de la vista.
  await cabecera(state.view === 'garden' ? state.summary : null);
}

function pintarIndicador() {
  const el = document.getElementById('live');
  if (!el) return;
  el.textContent = state.live ? '● en vivo' : '⏸ pausado';
  el.className = state.live ? 'live on' : 'live';
  el.title = state.live
    ? 'el visor se refresca solo; pulsa para congelarlo'
    : 'congelado; pulsa para volver a seguir al jardín';
}

async function vigilar() {
  for (;;) {
    await new Promise((r) => setTimeout(r, PULSO_MS));
    // Congelado a mano, reproduciendo la genealogía o con una ficha abierta:
    // redibujar por debajo sería quitarle al usuario lo que está mirando.
    if (!state.live || state.playing || state.view === 'bot') continue;
    try {
      const { firma } = await latido();
      if (state.pulse !== null && firma !== state.pulse) await refrescar();
      state.pulse = firma;
    } catch {
      // Un pulso fallido no es nada: el jardín puede estar cerrando.
    }
  }
}

async function cabecera(conocido) {
  try {
    const s = conocido || (state.summary = await api('/garden/summary'));
    document.getElementById('status').innerHTML =
      `generación ${s.generation} · ${s.n_alive} vivos · ${s.n_species} especies` +
      (s.alerts.length ? ` · <span style="color:var(--warn)">${s.alerts.length} alerta(s)</span>` : '') +
      `<br><span style="opacity:.7">${esc(s.symbol)} ${esc(s.timeframe)} · semilla ${esc(s.seed)}</span>`;
  } catch (err) {
    document.getElementById('status').textContent = `sin conexión con el jardín: ${err.message}`;
  }
}

async function arrancar() {
  await cabecera();
  try {
    state.pulse = (await latido()).firma;
  } catch {
    state.live = false;
  }
  const boton = document.getElementById('live');
  if (boton) {
    boton.addEventListener('click', () => {
      state.live = !state.live;
      pintarIndicador();
      if (state.live) refrescar();
    });
  }
  pintarIndicador();
  activar('garden');
  vigilar();
}

arrancar();
