# Protocolo del jardinero

## Quién es el jardinero

El jardinero es Claude, entrando periódicamente en un jardín que ya lleva un
tiempo corriendo solo. No opera. No escribe estrategias a mano. No toca el
capital. Lo que hace es lo que hace un jardinero de verdad: **mirar, diagnosticar
y decidir dónde poner la presión.**

El motor evolutivo es ciego: explora bien pero no entiende nada. El jardinero
entiende pero no puede evaluar diez mil genomas. La división del trabajo es esa.

## Cadencia

Cada `gardener.every_generations` generaciones (por defecto 4, es decir ~1 mes de
jardín vivo) el motor genera un informe y encola una **sesión de jardinero**. El
jardín no se detiene esperando: sigue corriendo con las reglas actuales hasta que
la sesión se resuelve.

También se encola una sesión fuera de turno si se dispara una alerta:

- `genetic_diversity` por debajo del suelo,
- drawdown del jardín por encima del límite blando,
- `fitness_median` estancada `gardener.stagnation_generations` generaciones,
- una familia superando su cuota,
- más del 40% de los ascensos de la última cosecha degradándose en holdout.

## El informe

`keepgarden report --generation latest` produce un Markdown en
`state/reports/gen_<n>.md` con, en este orden:

1. **Resumen de una línea**: estado del jardín y el cambio más importante.
2. **Demografía**: población, nacimientos, muertes, fusiones, edades, especies.
3. **Rendimiento**: equity del jardín contra el benchmark, distribución de
   fitness, mejores y peores.
4. **Diversidad**: distancia genética media, reparto por familia, reparto por
   linaje, matriz de correlación resumida.
5. **Los 10 mejores**, cada uno con: genoma en forma legible, métricas, edad,
   padres, y qué operador lo creó.
6. **Los muertos de esta generación** y por qué murieron.
7. **Qué ha funcionado**: tasa de éxito por operador (mutación / cruce / fusión /
   seed / injerto) y por familia de ideas, medida como *fracción de hijos que
   sobreviven 3 generaciones*. Es la métrica que dice si la evolución está
   funcionando o sólo generando ruido.
8. **Alertas activas**.
9. **Decisiones anteriores del jardinero y su resultado**: cada propuesta pasada
   con su efecto medido. El jardinero se lee a sí mismo y aprende de sus propias
   decisiones.

El punto 9 no es adorno: es lo que impide que el jardinero repita el mismo
consejo cada mes.

## Qué puede proponer el jardinero

Sólo estas acciones. Están tipadas en `gardener/proposals.py` y validadas antes
de aplicarse.

| Propuesta | Efecto |
|---|---|
| `GRAFT` | Injertar un bloque concreto (feature, regla, filtro de régimen, gen de riesgo) en un linaje o bot concreto, creando descendencia dirigida |
| `SEED_FAMILY` | Sembrar `n` bots nuevos de una familia concreta, opcionalmente con plantilla sugerida |
| `FUSE` | Fusionar bots concretos, con `combine` y pesos sugeridos |
| `RETIRE` | Jubilar un bot o un linaje entero |
| `PROTECT` | Proteger de la poda a un bot durante `n` generaciones (para dar tiempo a un raro prometedor) |
| `TUNE` | Ajustar un parámetro del motor dentro de límites: tasa de mutación, cupos, presión de poda, cuotas de familia |
| `ADD_GENE` | Proponer un indicador o tipo de regla nuevo para el catálogo — **requiere código**, así que genera una tarea de ingeniería, no un cambio en caliente |
| `REBALANCE_QUOTAS` | Cambiar `max_family_share` o los cupos por especie |
| `NOTE` | Dejar una observación en el diario sin efecto mecánico |

Cada propuesta lleva obligatoriamente: `rationale` (por qué), `expected_effect`
(qué espera ver, en métricas concretas) y `review_in_generations` (cuándo se
comprueba). Sin esos tres campos la propuesta se rechaza.

## Límites del jardinero

Existen para que el jardinero no pueda destruir el jardín ni engañarse a sí
mismo:

1. **No puede tocar el holdout.** Ni mirarlo, ni pedir que se mire.
2. **No puede subir el capital ni activar `live`.**
3. **No puede jubilar a más del `gardener.max_retire_share` (20%) de la población
   en una sesión.** Un jardinero pesimista podría vaciar el jardín.
4. **No puede mover un parámetro del motor más de un 50% de su valor por sesión**,
   ni salirse de los rangos de `config/garden.yaml`.
5. **No puede crear un genoma a mano.** Puede injertar bloques del catálogo en un
   linaje existente; el motor construye el hijo y lo pasa por la incubadora como
   a cualquier otro. Un injerto del jardinero no tiene privilegios: si no pasa el
   filtro, no nace.
6. **Todo queda escrito.** Propuesta, razonamiento, resultado esperado y
   resultado real van a la tabla `gardener_decisions` y salen en el dashboard.

## El diario

`gardener/journal.py` mantiene el diario del jardín: una entrada por sesión, en
prosa, escrita por el jardinero. No es un log de máquina, es la narración de lo
que está pasando en el ecosistema. Sirve para dos cosas: que Alex pueda leer la
historia del jardín como una historia, y que el propio jardinero recupere
contexto rápido al volver dentro de un mes.

Una entrada típica:

> **Generación 28.** El linaje de rupturas de Donchian, que dominaba desde la 14,
> se ha derrumbado con la compresión de volatilidad de las últimas dos semanas:
> cuatro de sus seis descendientes murieron por inactividad, no por pérdidas.
> Mientras tanto, la fusión `bot_7a21` (Donchian + reversión a la media, pesos
> 0.6/0.4) aguanta con un drawdown de la mitad que cualquiera de sus padres, que
> es exactamente lo que se esperaba de una fusión descorrelacionada. He sembrado
> seis bots de `VOLATILITY` porque el jardín no tiene a nadie mirando el régimen
> actual, y he protegido tres generaciones a `bot_9f04`, que lleva dos semanas
> con fitness mediocre pero correlación 0.08 con todo lo demás.

## Cómo se ejecuta una sesión (para Claude Code)

```
1. keepgarden report --generation latest      → escribe el informe
2. leer state/reports/gen_<n>.md
3. leer las últimas 3 entradas del diario
4. escribir las propuestas como JSON validado contra ProposalSchema
5. keepgarden gardener apply --file <propuestas.json>
   → valida límites, aplica, registra en gardener_decisions y events
6. escribir la entrada del diario
```

El paso 5 rechaza propuestas que violen los límites y explica por qué. El
jardinero corrige y reintenta; no se saltan los límites.

## Sobre la "consciencia" del bot

Idea que surgió antes en este proyecto: dar a los bots una especie de pulsión de
supervivencia. Aquí está resuelta de forma estructural y sin antropomorfismo: un
bot que no genera rendimiento **muere de verdad** —deja de existir en la
población— y sus genes desaparecen del acervo salvo que haya dejado descendencia.
La presión de supervivencia no hace falta programarla dentro del bot: es el
entorno. Es lo que la hace real en vez de decorativa.
