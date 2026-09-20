# Dashboard

`keepgarden dashboard` levanta FastAPI + uvicorn en `http://localhost:8756`.

Abre `state/db/garden.db` en **sólo lectura** (`file:...?mode=ro`, WAL). El
dashboard nunca escribe en el jardín. Si el motor no está corriendo, el dashboard
funciona igual mostrando el último estado.

Front: HTML + JS vanilla, sin build. ECharts desde CDN para los gráficos. Sin
framework: esto se mira en local, no necesita tooling.

## Vistas

### 1. Jardín (portada)

La foto del ecosistema ahora mismo.

- Tarjetas: capital total del jardín, alfa contra buy & hold, bots vivos,
  especies, diversidad genética, generación actual, edad del jardín.
- Gráfico principal: **equity del jardín contra el benchmark**, desde el inicio.
- Franja de estado: alertas activas, si hay sesión de jardinero pendiente.
- Mini-tabla: los 5 mejores bots vivos ahora mismo.

### 2. Genealogía  ← *la vista insignia*

Un DAG de todos los bots que han existido.

- **Eje X**: generación de nacimiento. **Eje Y**: agrupado por linaje raíz.
- **Nodo**: un bot. Radio ∝ fitness. Color = familia de ideas.
- **Relleno**: vivo (sólido), muerto (hueco), jubilado (gris), fusionado (anillo).
- **Aristas**: de padre a hijo. Sólidas para mutación/cruce, punteadas y más
  gruesas para fusión (varios padres convergiendo en un nodo).
- **Interacción**: clic en un nodo abre la ficha; hover resalta todo su linaje
  ascendente y descendente; filtros por familia, por linaje y por estado; slider
  temporal que reproduce la evolución del jardín generación a generación.

El slider temporal es lo que convierte la vista en algo que se *mira*: darle a
reproducir y ver cómo el jardín se puebla, converge, se diversifica y muta.

### 3. Generaciones

- Gráfico de fitness por generación: mejor, p75, mediana, p25, peor (banda).
- Barras apiladas de demografía: nacimientos por operador, muertes por causa.
- Línea de diversidad genética con el suelo marcado.
- Área apilada del reparto por familia de ideas a lo largo del tiempo.
- Tabla de generaciones, ordenable, con enlace al informe de cada una.

### 4. Ficha de bot

- Cabecera: nombre, id, estado, familia, edad, padres e hijos (navegables).
- **Genoma en forma legible**: las reglas traducidas a texto
  (`EMA(21) > EMA(89) Y RSI(14) cruza por encima de 55`), no JSON crudo. El JSON
  debajo, plegado.
- Curva de capital del bot contra el benchmark.
- Marcadores de entradas y salidas sobre el gráfico de precio.
- Tabla de métricas por generación vivida.
- Tabla de operaciones.
- Su historia: eventos (nacimiento, mutaciones que lo produjeron, fusiones en las
  que participó, protecciones, avisos de circuit breaker).

### 5. Especies

- Scatter 2D de la población proyectada por distancia genética (MDS o UMAP sobre
  la matriz de distancias). Color por familia, tamaño por fitness.
- Heatmap de correlación entre curvas de equity de los bots vivos.
- Tabla de especies: tamaño, fitness medio y compartido, edad media, cupo de
  reproducción asignado.

El scatter genético y el heatmap de correlación juntos responden a la pregunta
que importa: *¿este jardín tiene ideas distintas o cincuenta copias de la misma?*

### 6. Diario del jardinero

Las entradas del diario en orden inverso, cada una con las propuestas que
generó, su estado (aplicada / rechazada / pendiente de revisión) y el resultado
medido frente al efecto esperado.

## API

Sólo lectura. Todo bajo `/api`.

```
GET  /api/garden/summary
GET  /api/garden/equity?from=&to=
GET  /api/generations
GET  /api/generations/{n}
GET  /api/bots?status=&family=&lineage=&limit=
GET  /api/bots/{id}
GET  /api/bots/{id}/equity
GET  /api/bots/{id}/trades
GET  /api/bots/{id}/genome
GET  /api/lineage/graph?until_generation=
GET  /api/species
GET  /api/species/correlation
GET  /api/journal
GET  /api/events?type=&limit=
GET  /api/alerts
```

`/api/lineage/graph` acepta `until_generation` precisamente para alimentar el
slider temporal reproduciendo el pasado.

## Rendimiento

Con miles de bots históricos el DAG se hace pesado. Medidas:

- El grafo se sirve ya agregado: nodos y aristas, sin métricas, y la ficha se
  carga bajo demanda al hacer clic.
- Por encima de `dashboard.graph_node_limit` (2.000) se colapsan los linajes
  extintos en un nodo-resumen por linaje y generación, expandible.
- Las series de equity se decimean en servidor (LTTB) a ~2.000 puntos.
