# Evolución

## Qué es una generación aquí

En un algoritmo genético clásico, una generación reemplaza a la anterior. Aquí
no. Una **generación** es una *ventana de evaluación* del jardín vivo: 168 ticks
(una semana de velas de 1h). Al cerrarla:

- todos los bots vivos reciben métricas de esa ventana,
- se calcula el fitness y se reparten cupos de reproducción,
- nacen unos cuantos, mueren unos cuantos,
- y el resto sigue viviendo, con una generación más de edad.

Un bot puede vivir 40 generaciones. La `generation` de su `meta` es la generación
en la que **nació**, no la actual.

## Operadores

### SEED — nacimiento espontáneo

Muestreo aleatorio del catálogo de genes, condicionado a una familia. Se usa para
la población inicial y para inyectar sangre nueva cuando la diversidad cae por
debajo del umbral (`evolution.diversity_floor`).

Un seed no es uniforme: el catálogo define plantillas por familia (`templates` en
`config/genes.yaml`) que sesgan el muestreo hacia combinaciones que al menos
tienen sentido. Un muestreo totalmente uniforme genera basura el 99% del tiempo.

### MUTATE — variación sobre un padre

Se elige 1 padre y se aplican de 1 a 3 mutaciones puntuales:

| Mutación | Peso | Qué hace |
|---|---|---|
| `TWEAK_PARAM` | 0.35 | Mueve un parámetro de indicador dentro de su rango (paso gaussiano, σ = 15% del rango) |
| `TWEAK_THRESHOLD` | 0.20 | Mueve una constante de una regla |
| `TWEAK_RISK` | 0.15 | Cambia stop, take profit, trailing o `risk_per_trade` |
| `ADD_CONDITION` | 0.10 | Añade una hoja a un árbol (si no supera el límite) |
| `DROP_CONDITION` | 0.10 | Quita una hoja (si queda al menos una) |
| `SWAP_INDICATOR` | 0.05 | Sustituye un indicador por otro de la misma categoría |
| `TOGGLE_REGIME` | 0.03 | Activa o desactiva el filtro de régimen |
| `TOGGLE_SHORT` | 0.02 | Permite o prohíbe cortos |

La **tasa de mutación es adaptativa**: si la mediana de fitness del jardín lleva
`k` generaciones estancada, sube; si mejora rápido, baja. Rango
`[0.5x, 3x]` sobre la base. Esto evita los dos fallos clásicos: convergencia
prematura y ruido perpetuo.

### CROSSOVER — cruce de dos padres

Cruce **por bloques**, no por bits. Los bloques son: `features`, `entry_long`,
`exit_long`, `entry_short`, `exit_short`, `risk`, `regime`. Para cada bloque se
elige el del padre A o el del padre B (uniforme, sesgado hacia el padre con mejor
fitness: 0.6/0.4).

Después hay que **reparar**: si `entry_long` viene de A y referencia el feature
`ema_fast` que sólo existía en A, ese feature se arrastra al hijo. Si dos features
heredados tienen el mismo `id` pero distinta definición, se renombra uno y se
reescriben sus referencias. `genome/validate.py` debe garantizar que ningún
genoma sale del cruce con referencias rotas.

Sólo se cruzan padres con `distance(a, b) > crossover_min_distance`. Cruzar dos
casi-clones es gastar un nacimiento.

### FUSION — la fusión de bots

Es el operador que define este proyecto y merece su propia sección.

Se seleccionan de 2 a 4 padres **vivos, sanos y poco correlacionados** (la
correlación es entre sus curvas de equity, no entre sus genomas). Se crea un hijo
con `ensemble` en vez de reglas propias:

```
señal_hijo = combine(señal_p1, señal_p2, ..., pesos, umbral)
```

Los pesos iniciales se reparten proporcionalmente al Sortino de cada padre en la
ventana, normalizados. El gen de riesgo del hijo se hereda del padre con mejor
ratio de Calmar.

Por qué funciona: dos estrategias mediocres pero descorrelacionadas producen, al
combinarse, una curva con menos drawdown que cualquiera de las dos. Eso es la
única comida gratis que hay en esto. El sistema la busca de forma explícita.

Reglas de la fusión:
- Los padres **siguen vivos**. La fusión no los consume.
- Un bot fusionado puede volver a fusionarse: se admiten ensembles de ensembles,
  hasta `fusion.max_depth` niveles (por defecto 2), para que no degeneren en
  promedios de todo el jardín.
- Si un padre muere, el ensemble redistribuye su peso entre los demás y lo
  registra como evento. Si le quedan menos de 2 miembros vivos, el ensemble se
  jubila.

### GRAFT — el injerto del jardinero

El único operador que no es automático. El jardinero (tú, Claude, en tu papel de
jardinero) propone un gen o un bloque concreto —un filtro de régimen nuevo, un
indicador del catálogo aún no usado, una idea— y el motor lo injerta en un linaje
existente, creando un hijo. Está descrito en `docs/GARDENER_PROTOCOL.md`.

## Selección

### Fitness

Ver `docs/METRICS.md`. En resumen: un escalar compuesto y penalizado, más un
frente de Pareto sobre (retorno, riesgo, diversidad).

### Presión selectiva

1. **Elitismo**: los `elite_count` mejores del jardín están protegidos de la poda
   y se reproducen seguro.
2. **Fitness compartido por especie**: el fitness de un bot se divide por el
   tamaño de su especie. Una especie que domina se penaliza a sí misma. Es lo que
   impide que el jardín entero acabe siendo variaciones de la misma EMA.
3. **Torneo dentro de especie**: tamaño 3, se elige el mejor.
4. **Cupos por especie** proporcionales al fitness compartido agregado, con un
   mínimo de 1 para toda especie que tenga al menos un bot por encima de la
   mediana global.

### Poda

Un bot muere si se cumple cualquiera:

- Está en el peor `cull_fraction` por fitness **y** ha vivido al menos
  `min_age_generations` (no se mata a un recién nacido por una mala semana).
- Su drawdown desde máximos supera `risk.hard_max_drawdown` (poda inmediata, no
  espera a fin de generación).
- Lleva `idle_generations_limit` generaciones sin abrir ni una posición.
- Es clon de otro bot vivo con mejor fitness.
- El jardinero lo jubila explícitamente.

La élite y los bots con menos de `min_age_generations` están exentos de las dos
primeras.

## Mantener el jardín diverso

Tres mecanismos, porque la pérdida de diversidad es la forma en que estos
sistemas mueren:

1. **Especiación** por distancia genética, con fitness compartido.
2. **Suelo de diversidad**: si la distancia genética media del jardín cae por
   debajo de `diversity_floor`, la siguiente cosecha se llena de `SEED` en lugar
   de descendencia.
3. **Cuota de familias**: ninguna familia de ideas puede superar
   `max_family_share` de la población viva. Si lo supera, sus nacimientos se
   bloquean esa generación.

## Anti-sobreajuste

Lo más importante del documento. Un sistema que genera miles de estrategias y se
queda con la mejor **va a encontrar oro falso** si no se defiende.

1. **Partición temporal con embargo.**
   ```
   |------------ TRAIN ------------|--emb--|---- VALIDATION ----|--emb--|- HOLDOUT -|
                                                                          (intocable)
   ```
   El embargo (por defecto 5 días) evita fugas por indicadores con ventana larga.

2. **Walk-forward anclado.** La incubadora no evalúa sobre un único split:
   entrena y valida sobre ventanas deslizantes y el fitness es la **mediana** de
   los folds, no la media. La mediana castiga a los bots que dependen de un único
   periodo afortunado.

3. **Penalización por multiplicidad.** Cuantos más genomas se prueban, más alto
   debe ser el listón. Se aplica un ajuste tipo *deflated Sharpe*: el umbral de
   aceptación sube con `log(n_pruebas_de_la_cosecha)`.

4. **Parsimonia.** El fitness resta un término proporcional a la complejidad del
   genoma (nº de features + nº de hojas). Entre dos bots equivalentes gana el
   simple, que es el que suele sobrevivir fuera de muestra.

5. **Mínimo de operaciones.** Un bot con menos de `min_trades` operaciones en la
   ventana tiene fitness indefinido, no fitness alto. Se le da otra generación
   antes de juzgarlo.

6. **El holdout decide el ascenso, no la selección.** Se mira **una sola vez**
   por bot, al ascender de la incubadora al jardín vivo. El resultado se guarda.
   Si un bot se degrada en holdout, no asciende y se penaliza a su familia.

7. **El jardín vivo es el árbitro final.** El fitness efectivo de un bot es
   `0.3 * fitness_incubadora + 0.7 * fitness_jardín_vivo` en cuanto tiene al
   menos 2 generaciones vividas. El backtest sólo sirve para llegar a la puerta.

## Bucle completo, en pseudocódigo

```python
while True:
    candle = clock.wait_for_next_closed_candle()      # ~1h
    features = indicators.update(candle)
    for bot in population.alive():
        signal = bot.decide(features)
        order  = risk.size(signal, bot, portfolio)
        broker.submit(order)                           # se ejecuta en t+1
    broker.settle(candle)
    portfolio.mark_to_market(candle)
    storage.persist_tick(...)
    reflexes.check_circuit_breakers(population)

    if clock.generation_closed():
        metrics    = evaluation.evaluate(population, window)
        fitness    = evaluation.fitness(metrics, population)
        species    = evolution.speciate(population)
        parents    = evolution.select(species, fitness)
        offspring  = evolution.breed(parents)           # mutate/crossover/fusion
        newborns   = incubator.screen(offspring)        # walk-forward + holdout
        population.admit(newborns)
        population.cull(fitness)
        storage.persist_generation(...)

        if clock.gardener_due():
            report = gardener.build_report(...)
            storage.queue_gardener_session(report)      # esperando a Claude
```
