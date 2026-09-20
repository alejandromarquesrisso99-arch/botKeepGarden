# Métricas y fitness

## Métricas por bot y ventana

Todas se calculan sobre una ventana concreta (una generación del jardín vivo, o
un fold de la incubadora). Nunca "desde siempre" salvo cuando se diga.

### De retorno

| Métrica | Definición |
|---|---|
| `total_return` | `equity_final / equity_inicial - 1` |
| `cagr` | Retorno anualizado compuesto |
| `avg_trade_return` | Retorno medio por operación, neto |
| `expectancy` | `win_rate * avg_win - (1 - win_rate) * avg_loss` |

### De riesgo

| Métrica | Definición |
|---|---|
| `max_drawdown` | Máxima caída desde un máximo de equity, en fracción |
| `ulcer_index` | Raíz de la media de los drawdowns al cuadrado. Castiga los drawdowns *largos*, no sólo los profundos |
| `downside_dev` | Desviación típica sólo de los retornos negativos |
| `time_in_market` | Fracción de velas con posición abierta |
| `worst_trade` | Peor operación individual |

### Ajustadas por riesgo

| Métrica | Definición |
|---|---|
| `sharpe` | `mean(r) / std(r) * sqrt(periodos_por_año)` |
| `sortino` | Igual pero con `downside_dev`. **Es la métrica principal** |
| `calmar` | `cagr / max_drawdown` |
| `martin` | `cagr / ulcer_index` |
| `profit_factor` | `suma_ganancias / abs(suma_pérdidas)` |

Con velas de 1h, `periodos_por_año = 24 * 365 = 8760`.

### De comportamiento

| Métrica | Para qué |
|---|---|
| `n_trades` | Sin operaciones no hay evidencia |
| `turnover` | Notional operado / capital. Detecta sobreoperación |
| `avg_holding_bars` | Caracteriza al bot; también se usa para agrupar especies |
| `fee_drag` | Comisiones pagadas / PnL bruto. Si es > 0.5 el bot vive para pagar comisiones |
| `consistency` | Fracción de subventanas (días) con retorno positivo |

### Relacionales

| Métrica | Para qué |
|---|---|
| `corr_to_population` | Correlación media de sus retornos diarios con los del resto de bots vivos |
| `corr_to_benchmark` | Correlación con buy & hold del símbolo |
| `novelty` | Distancia genética media a los 10 bots vivos más cercanos |

`corr_to_population` es clave para la fusión: se buscan padres con correlación
baja entre sí.

## Fitness

### Principio

El fitness no es "cuánto gana". Un bot que gana mucho con drawdowns del 60%, 11
operaciones y correlación 0.98 con el resto del jardín es **peor** para el
ecosistema que uno que gana la mitad de forma estable y descorrelacionada.

### Fórmula

Se normalizan las métricas dentro de la población viva con *z-score robusto*
(mediana y MAD, no media y desviación, porque los outliers son constantes aquí),
se recortan a `[-3, +3]`, y se combinan:

```
base =   0.35 * z(sortino)
       + 0.20 * z(calmar)
       + 0.15 * z(-ulcer_index)
       + 0.15 * z(profit_factor)
       + 0.15 * z(consistency)

penalizaciones =
         p_trades      : si n_trades < min_trades  → fitness indefinido (NaN)
       + p_complejidad : 0.02 * (n_features + n_hojas - 4), mínimo 0
       + p_correlacion : 0.30 * max(0, corr_to_population - 0.6)
       + p_fees        : 0.25 * max(0, fee_drag - 0.30)
       + p_turnover    : 0.15 * max(0, z(turnover) - 1.5)

bonificaciones =
         b_novedad     : 0.10 * z(novelty)
       + b_edad        : 0.05 * min(1, generaciones_vividas / 8)

fitness = base - penalizaciones + bonificaciones
```

El bono de edad es pequeño pero importante: premia a los bots que han sobrevivido
mucho tiempo en condiciones reales, que es la única evidencia que no se puede
falsificar con un backtest.

### Fitness efectivo

```
si generaciones_vividas < 2:
    fitness_efectivo = fitness_incubadora
si no:
    fitness_efectivo = 0.3 * fitness_incubadora + 0.7 * fitness_jardín_vivo
```

El backtest sirve para entrar por la puerta. Después manda la realidad.

### Frente de Pareto

Además del escalar, se calcula el frente no dominado sobre tres objetivos:

1. maximizar `sortino`
2. minimizar `ulcer_index`
3. maximizar `novelty`

Los bots del primer frente están **protegidos de la poda** durante una
generación aunque su escalar sea mediocre. Es lo que mantiene vivos a los raros
que todavía no han demostrado su valor, y de los que suelen salir los saltos.

## Umbrales de la incubadora

Para que un genoma nazca:

```
mediana_folds(sortino)         >= incubator.min_sortino          (0.8)
mediana_folds(max_drawdown)    <= incubator.max_drawdown         (0.30)
mediana_folds(n_trades)        >= incubator.min_trades_per_fold  (15)
degradación_out_of_sample      <= incubator.max_oos_decay        (0.50)
distancia_al_vivo_más_cercano  >= speciation.clone_threshold     (0.15)
```

donde

```
degradación_oos = 1 - sortino_validation / max(sortino_train, ε)
```

El umbral de `min_sortino` se **infla con el número de pruebas** de la cosecha:

```
min_sortino_efectivo = min_sortino * (1 + 0.08 * log10(max(1, n_genomas_probados)))
```

Probar 10.000 genomas y quedarse con el mejor no es selección: es dragado de
datos. Esto lo compensa de forma explícita.

## Métricas del jardín

Se calculan por generación y son lo que pinta el dashboard:

| Métrica | Qué cuenta |
|---|---|
| `population_size` | Bots vivos |
| `n_species` | Especies distintas |
| `genetic_diversity` | Distancia genética media entre pares |
| `family_shares` | Reparto de la población por familia de ideas |
| `fitness_best / median / p25 / worst` | Distribución de fitness |
| `garden_equity` | Suma de las carteras |
| `benchmark_equity` | Buy & hold de referencia |
| `garden_alpha` | `garden_equity / benchmark_equity - 1` |
| `births / deaths / fusions` | Dinámica demográfica |
| `oldest_bot_age` | Generaciones del bot más veterano |
| `lineage_shares` | Reparto por linaje raíz |

`genetic_diversity` cayendo de forma sostenida es la **alerta temprana** más
importante: significa que el jardín está convergiendo y va a dejar de descubrir.
