# Guía de usuario

Esto es un **jardín de bots de trading**. No se configura una estrategia y se
la deja correr: se siembra una población, se la deja competir sobre velas
reales y se poda. Esta guía va de cómo se hace eso, en orden, desde un
ordenador en blanco.

Todos los comandos son de PowerShell en Windows, que es donde vive esto.
Dinero simulado siempre: el modo `live` está bloqueado por código.

---

## 1. Preparar el entorno (una sola vez)

```powershell
cd C:\ruta\a\botKeepGarden
.\scripts\bootstrap.ps1
```

Crea el entorno virtual en `.venv`, instala las dependencias y deja disponible
el comando `keepgarden`. Si tienes varias versiones de Python y elige la que no
es:

```powershell
.\scripts\bootstrap.ps1 -PythonExe "C:\Python312\python.exe"
```

En las sesiones siguientes basta con activar el entorno:

```powershell
.\.venv\Scripts\Activate.ps1
keepgarden --help
```

Comprueba que la configuración se carga y es coherente:

```powershell
keepgarden config
```

Si algo no cuadra, el jardín **no arranca** y te dice qué parámetro es. Es
deliberado: más vale no arrancar que evolucionar contra una configuración
incoherente durante tres semanas.

---

## 2. Traer las velas

Nada funciona sin histórico. Se descarga de Binance, público y sin claves:

```powershell
keepgarden data backfill --symbol BTC/USDT --timeframe 1h --since 2019-01-01
```

Tarda un rato la primera vez (siete años de velas de una hora son ~60.000
filas). **Es reanudable**: si lo matas a mitad y lo relanzas, sigue donde
estaba. Con `--context` se traen además las series de 4h y 1d, que algunos
genomas usan como filtro de régimen.

Para ver qué hay en la caché y si tiene agujeros:

```powershell
keepgarden data status
```

Lo que tiene que decir es `0 huecos` en las series que vayas a usar. Un hueco
largo no es un error del programa: es que el venue estuvo caído. El motor lo
marca y **no opera atravesándolo**, porque inventar precio es inventar
rentabilidad.

Si vas a correr varios mercados, descárgalos todos:

```powershell
keepgarden data backfill --symbol ETH/USDT --timeframe 1h --since 2019-01-01
keepgarden data backfill --symbol SOL/USDT --timeframe 1h --since 2020-08-01
```

---

## 3. Sembrar el jardín

```powershell
keepgarden garden seed --size 60
```

Crea 60 bots nuevos muestreados del catálogo de genes, repartidos entre las seis
familias de ideas (tendencia, reversión a la media, rupturas, momento,
volatilidad y microestructura). Te dirá la **diversidad genética** de la
siembra: si sale por debajo de `0.4` es que ha salido una camada demasiado
parecida, y conviene volver a sembrar con otra semilla.

Multi-mercado:

```powershell
keepgarden garden seed --size 60 --symbols "BTC/USDT,ETH/USDT,SOL/USDT"
```

Reparte la población entre los tres. Cada bot opera **su** mercado; el jardín
es uno solo y el capital se compara contra un espejo que compra lo mismo que
compran ellos.

> `--reset` borra el jardín anterior. Es destructivo y no pregunta dos veces.

---

## 4. Dos formas de hacerlo crecer

### La incubadora: evolución rápida sobre histórico

```powershell
keepgarden incubate --generations 20
```

Cada generación cría candidatos (mutación, cruce, fusión, semilla), los criba
contra un walk-forward de cinco pliegues y sólo deja nacer a los que pasan.
No simula cartera: es el reloj rápido, para llenar el jardín de ideas que al
menos no son basura. Veinte generaciones sobre siete años de BTC son unos
dos minutos.

### El jardín vivo: el bucle de verdad

```powershell
# ensayo: recorre los últimos 6 meses de histórico como si fuera vivo
keepgarden run --dry-run

# y en vivo, esperando cada vela
keepgarden run
```

En vivo, cada hora en punto (más unos segundos de margen) el jardín pide la
vela cerrada, cada bot decide, las órdenes se rellenan en la apertura de la
vela siguiente y cada 168 velas —una semana— se cierra una generación: se
mide, se cría, se poda.

Para dejarlo corriendo de forma desatendida:

```powershell
.\scripts\run_garden.ps1
```

Ese script lo relanza si se cae. **Reanudar es seguro**: el estado vive en
SQLite, cada vela se escribe en una transacción y las velas perdidas se
recuperan solas. Matar el proceso y volver a arrancarlo da el mismo jardín,
operación a operación.

Opciones útiles del dry-run:

| Opción | Para qué |
|---|---|
| `--bars 4380` | cuántas velas recorrer (por defecto, ~6 meses) |
| `--since 2024-01-01` | empezar en una fecha concreta |
| `--speed 1000` | frenarlo a 1.000 velas por segundo para verlo moverse en el dashboard |
| `--max-ticks 500` | parar después de N velas |

---

## 5. Mirarlo

```powershell
keepgarden dashboard      # http://localhost:8756
```

Se abre solo en el navegador. Lee la base en **sólo lectura**: puedes tenerlo
abierto con el jardín corriendo al lado sin riesgo, y si el motor está parado
enseña igual el último estado.

Las ocho vistas, y qué mirar en cada una:

**Jardín.** La foto de ahora: capital, alfa contra el benchmark, bots vivos,
especies, diversidad. El gráfico grande es el capital del jardín contra una
cartera espejo que ha recibido exactamente las mismas entradas y salidas de
dinero. Si el alfa es positivo pero el capital baja, estás perdiendo menos que
el mercado, que no es lo mismo que ganar.

**Genealogía.** La vista insignia. Cada punto es un bot: eje X la generación en
que nació, eje Y su linaje, tamaño el fitness, color la familia de ideas.
Relleno sólido = vivo, hueco = muerto, anillo = nació de una fusión. Las
aristas punteadas y gruesas son fusiones. **Pasa el ratón por un nodo y se
resalta su linaje entero**, hacia arriba y hacia abajo. Y el slider de arriba
reproduce la evolución generación a generación: dale a reproducir y mira cómo
se puebla el jardín.

**Cría.** Lo que no llegó a nacer, que es la mitad de la historia. El embudo
concebidos → aprobados → nacidos → vivos, por qué se descarta cada candidato
(clon, opera poco, sortino bajo, freno de drawdown) y qué operador está criando
bots que duran. Si "clon de un vivo" domina la tabla de rechazos, el jardín se
está quedando sin ideas nuevas.

**Generaciones.** Fitness por generación con su banda, demografía (nacimientos
por operador hacia arriba, muertes por causa hacia abajo), diversidad genética
con su suelo marcado y el reparto por familia a lo largo del tiempo.

**Especies.** El mapa genético de la población viva y el heatmap de correlación
entre curvas. Juntos responden a la única pregunta que importa: *¿este jardín
tiene ideas distintas o cincuenta copias de la misma?*

**Diario.** Tus sesiones de jardinero y la historia completa de eventos.

**Salud.** Si el jardín late, si le llegan las velas y si va sobrado de tiempo.
Míralo antes de irte de fin de semana.

Clic en cualquier bot, en cualquier vista, abre su ficha: el genoma **en
castellano legible** (`EMA(21) > EMA(89) Y RSI(14) cruza por encima de 55`), su
curva, sus operaciones, sus padres y sus hijos, y su paso por la incubadora.

---

## 6. Tu papel: el jardinero

El motor evolutivo es ciego: explora bien pero no entiende nada. Cada cuatro
generaciones —aproximadamente un mes de jardín vivo— toca entrar a mirar. Ese
papel lo hace Claude, y el ciclo es siempre el mismo:

```powershell
# 1. el informe
keepgarden report --generation latest
```

Escribe `state\reports\gen_<n>.md` con nueve secciones: resumen, demografía,
rendimiento, diversidad, los diez mejores con su genoma en prosa, los muertos y
por qué murieron, qué operador está funcionando, alertas, y **las decisiones
anteriores con su efecto medido**. Ese último punto no es adorno: es lo que
impide repetir el mismo consejo cada mes.

El informe está pensado para decidir sin abrir la base de datos. Si alguna vez
tienes que consultar algo que no está ahí, el fallo es del informe.

```powershell
# 2. las propuestas, en un JSON
keepgarden gardener apply --file propuestas.json --dry-run
```

Un ejemplo real:

```json
{
  "proposals": [
    {
      "kind": "SEED_FAMILY",
      "payload": { "family": "VOLATILITY", "count": 4 },
      "rationale": "No queda ningún bot de VOLATILITY vivo y HYBRID ocupa el 48% de la población: nadie está mirando el régimen actual.",
      "expected_effect": "La cuota de HYBRID baja del 48% y la diversidad sube de 0.67 en cuatro generaciones.",
      "review_in_generations": 4
    },
    {
      "kind": "PROTECT",
      "payload": { "bots": ["bot_11da8665"], "generations": 3 },
      "rationale": "Acaba de nacer con el mejor fitness de incubadora y novedad 0.65; con 3 operaciones no puede defenderse de la poda.",
      "expected_effect": "Sigue vivo en la generación 54 y para entonces ha operado 10 veces.",
      "review_in_generations": 3
    }
  ]
}
```

Los tres campos `rationale`, `expected_effect` y `review_in_generations` son
**obligatorios**. Sin ellos la propuesta se rechaza. Un jardinero que no puede
decir qué espera que pase no está diagnosticando, está adivinando.

```powershell
# 3. aplicarlas, con la entrada de diario
keepgarden gardener apply --file propuestas.json --journal "Generación 51. El linaje de rupturas domina el 52% del jardín y sus tres fusiones tienen correlación 1.000 entre sí: no están descorrelacionando nada..."
```

Qué esperar:

- Una propuesta que viola un límite **se rechaza con el motivo** y las demás
  siguen su curso.
- Una sesión que se pasa de un límite acumulativo —jubilar a más del 20 % de la
  población, demasiadas fusiones— **no se aplica entera**. Corriges y repites.
- Lo que el jardinero cría entra por la puerta de todos: siembras, injertos y
  fusiones pasan por la incubadora. **Si no pasan, no nacen.** Es normal
  proponer cuatro siembras y que no nazca ninguna.
- Un `TUNE` no toca `config\garden.yaml`: vive en la base y se deshace.

Lo que el jardinero **no puede** hacer, por diseño: mirar el holdout, subir el
capital, activar `live`, vaciar el jardín, mover un parámetro más de un 50 % de
golpe, o escribir un genoma a mano.

---

## 7. Comprobar que un bot no es un espejismo

Un bot que gana en el backtest no dice gran cosa. Uno que sigue ganando cuando
le mueves el suelo, sí:

```powershell
keepgarden robustness --top 3
```

Escribe `state\reports\robustez.md` con, para cada bot:

- **Fricción al doble y al triple.** El listón es el doble. Un bot que vive de
  un margen menor que su propio coste de transacción sólo existe en la hoja de
  cálculo.
- **Cuatro arranques distintos.** Una estrategia que depende de haber entrado
  justo ese martes no es una estrategia.
- **Monte Carlo sobre el orden de las operaciones.** El dinero final no cambia
  —sumar es conmutativo— pero el drawdown sí, y muchísimo. Si el drawdown que
  de verdad tuvo cae en el percentil 10, tuvo suerte con el orden y el próximo
  trimestre puede doler el triple.
- **Bootstrap**: y si le hubieran tocado otras operaciones parecidas, ¿cuánto
  habría ganado? Con su probabilidad de acabar en pérdidas.

`--bot <id>` para uno concreto, `--stdout` para verlo por pantalla.

---

## 8. El día a día

**Cada día (30 segundos).** Vista *Salud* del dashboard. Que el estado sea
`RUNNING`, que la última vela esté al día y que no haya alertas nuevas.

**Cada semana (5 minutos).** Vista *Generaciones*: ¿sube la mediana de fitness?
¿se mantiene la diversidad por encima del suelo? Vista *Cría*: ¿siguen naciendo
bots o la incubadora rechaza todo?

**Cada mes (media hora, con Claude).** Sesión de jardinero completa: informe,
diagnóstico, propuestas, diario.

**Cada trimestre.** `keepgarden robustness` sobre los mejores. Y mirar si el
mejor linaje sigue siendo el mismo de hace tres meses: si lo es y el jardín no
descubre nada nuevo, hay que abrir la mano (más mutación, más siembra) antes
de que se fosilice.

Señales de que algo va mal, por orden de gravedad:

| Lo que ves | Lo que significa |
|---|---|
| Diversidad cayendo varias generaciones seguidas | El jardín converge y va a dejar de descubrir |
| "clon de un vivo" domina los rechazos | Los operadores ya no producen ideas, sólo copias |
| Una familia por encima del 40 % | Monocultivo: una idea se está comiendo el jardín |
| Correlación máxima cerca de 1.000 entre vivos | Tienes un bot, no cincuenta |
| Nadie con fitness definido | Ver la sección 10 |
| Nacen 0 bots varias generaciones | El listón de la incubadora es más alto que lo que el jardín sabe criar |

---

## 9. Cuando algo va mal

**`no hay velas de BTC/USDT en caché`** — falta el backfill (sección 2).

**`no hay jardín todavía`** — falta `keepgarden garden seed`.

**El jardín entra en `DEGRADED`** — el venue lleva cinco fallos seguidos. Sigue
registrando y no opera. Se recupera solo cuando Binance vuelve; en la vista
*Salud* verás los frenazos de las últimas 24 horas.

**El dashboard no pinta los gráficos** — ECharts se carga de un CDN. Sin
internet, las tablas siguen funcionando y cada gráfico lo avisa en su hueco.

**El dashboard no arranca: "falta una dependencia"** — vuelve a pasar el
bootstrap.

**Quiero volver atrás** — el jardín es un archivo. En `state\db\snapshots\`
hay una copia por generación (`garden_0050_gen50.db`) que el motor guarda cada
diez. Paras el jardín, copias la que quieras sobre `state\db\garden.db` y
arrancas: tendrás el jardín exactamente como estaba en esa generación.

**Quiero empezar de cero sin perder las velas** — borra `state\db\garden.db`.
La caché de velas (`state\cache\`) es independiente y no hace falta volver a
descargar nada.

---

## 10. Una cosa que deberías saber

Hay una tensión real entre dos parámetros de la configuración, y se nota en
cuanto el jardín corre en vivo:

- `garden.ticks_per_generation: 168` — una generación dura una semana.
- `fitness.min_trades: 10` — por debajo de diez operaciones no hay fitness.

Sobre seis meses de jardín medidos, la mediana de operaciones por bot y semana
es **2,3**, y sólo 5 de 532 pares (bot, generación) llegan a diez. Resultado: el
fitness vivo casi nunca se define, la selección se queda con la nota que el bot
sacó en la incubadora el día que nació, y el jardín evoluciona mucho más despacio
de lo que debería.

No lo he cambiado porque los dos valores son decisiones tuyas. Las salidas, de
menos a más intrusiva:

1. Bajar `fitness.min_trades` a 3 o 4. Una línea. Riesgo: juzgar con poca
   evidencia, que es justo lo que ese parámetro evita.
2. Subir `ticks_per_generation` a 500-700 (un mes). Generaciones más lentas,
   evidencia de verdad. Cambia el ritmo de todo el jardín.
3. Medir el fitness vivo sobre una ventana deslizante de varias generaciones en
   vez de sólo la última. La correcta, y la que más código toca.

Está escrito con detalle en `docs/DECISIONS.md`, entrada **D-030**.

---

## 11. Referencia rápida

```powershell
# datos
keepgarden data backfill --symbol BTC/USDT --timeframe 1h --since 2019-01-01
keepgarden data status

# jardín
keepgarden garden seed --size 60 [--symbols "BTC/USDT,ETH/USDT"] [--reset]
keepgarden garden status
keepgarden incubate --generations 20
keepgarden run [--dry-run] [--bars N] [--since FECHA] [--speed N] [--max-ticks N]

# mirarlo
keepgarden dashboard [--port 8756] [--no-browser]

# jardinero
keepgarden report --generation latest [--stdout]
keepgarden gardener apply --file propuestas.json [--dry-run] [--journal "..."]

# comprobaciones
keepgarden robustness [--top 3] [--bot <id>] [--stdout]
keepgarden backtest --genome examples\genome_trend.json
keepgarden genome sample [--family TREND]
keepgarden genome show --genome <archivo>
keepgarden catalog [<indicador>]
keepgarden config
```

Dónde está cada cosa:

```
config\garden.yaml     todo lo que gobierna el jardín
config\genes.yaml      sesgo del catálogo de indicadores
state\db\garden.db     el jardín entero, un archivo
state\cache\           las velas, en Parquet por año
state\db\snapshots\   copias del jardín, una por cada diez generaciones
state\reports\         informes de generación y de robustez
docs\                  por qué cada cosa es como es
```

Los parámetros que más cambian el resultado, por si tocas algo:

| Parámetro | Qué hace |
|---|---|
| `garden.ticks_per_generation` | El ritmo de todo. 168 = una semana |
| `garden.births_per_generation` | Cuántos candidatos se conciben por generación |
| `evolution.mutation_rate` | Cuánto explora. Sube solo si el jardín se estanca |
| `evolution.diversity_floor` | Por debajo, la cosecha se llena de semillas nuevas |
| `incubator.min_sortino` | El listón para nacer. Subirlo vacía el jardín |
| `risk.hard_max_drawdown` | Muerte inmediata del bot. No lo toques a la ligera |
| `frictions.taker_fee_bps` | La comisión real. Ponerla baja es mentirse |

---

## 12. Lo que esto no hace

- **No mueve dinero real.** `execution.mode: live` aborta con un error
  explícito. Desbloquearlo es una decisión humana en el código, no una opción
  de configuración.
- **No hay apalancamiento ni cortos reales.** Es spot.
- **No modela impuestos.** El jardín compara estrategias entre sí y el impuesto
  es aproximadamente un factor común.
- **No promete nada.** Que un linaje aguante siete años de histórico y el doble
  de fricción es una evidencia buena; no es una garantía sobre el mes que viene.
