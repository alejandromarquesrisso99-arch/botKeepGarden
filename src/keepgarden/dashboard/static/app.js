/* Esqueleto del front — hito 6.
 *
 * Sin framework y sin build: se abre en local, no necesita tooling.
 * Todo el estado vive en memoria; nada de localStorage.
 *
 * Estructura prevista:
 *   api(path)            fetch a /api con manejo de error
 *   render.garden()      tarjetas + equity del jardín vs benchmark
 *   render.lineage()     el DAG genealógico + slider temporal
 *   render.generations() fitness por generación, demografía, diversidad
 *   render.species()     scatter genético + heatmap de correlación
 *   render.journal()     entradas del diario con sus propuestas
 *   render.bot(id)       ficha individual
 *
 * El slider temporal pide /api/lineage/graph?until_generation=N y redibuja,
 * así que reproducir la evolución es sólo mover el slider.
 */

const FAMILY_COLORS = {
  TREND:          getComputedStyle(document.documentElement).getPropertyValue('--fam-trend').trim(),
  MEAN_REVERSION: getComputedStyle(document.documentElement).getPropertyValue('--fam-mean-reversion').trim(),
  BREAKOUT:       getComputedStyle(document.documentElement).getPropertyValue('--fam-breakout').trim(),
  MOMENTUM:       getComputedStyle(document.documentElement).getPropertyValue('--fam-momentum').trim(),
  VOLATILITY:     getComputedStyle(document.documentElement).getPropertyValue('--fam-volatility').trim(),
  MICROSTRUCTURE: getComputedStyle(document.documentElement).getPropertyValue('--fam-micro').trim(),
  HYBRID:         getComputedStyle(document.documentElement).getPropertyValue('--fam-hybrid').trim(),
};

async function api(path) {
  const res = await fetch(`/api${path}`);
  if (!res.ok) throw new Error(`${path}: ${res.status}`);
  return res.json();
}

// TODO(hito 6): implementar las vistas.
document.querySelectorAll('#nav button').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('#nav button').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById(`view-${btn.dataset.view}`).classList.add('active');
  });
});
