# El genoma

## Principio

**Un bot no es código: es una estructura de datos.** Esto no es un detalle de
implementación, es lo que hace posible todo lo demás. Porque el bot es datos:

- se puede mutar sin generar ni ejecutar código nuevo,
- se puede cruzar con otro mezclando bloques,
- se puede serializar, versionar, diffear y mostrar en el dashboard,
- se puede evaluar miles de veces por segundo,
- y no hay ninguna superficie para que una mutación produzca algo peligroso.

El genoma se *compila* a una función de señal vectorizada (`genome/compile.py`),
pero la compilación es determinista y cerrada: sólo puede producir combinaciones
del catálogo de genes.

## Estructura

```
Genome
├── id, version
├── family          : familia de ideas (TREND, MEAN_REVERSION, BREAKOUT, ...)
├── market          : símbolo + timeframe
├── features[]      : los indicadores que este bot mira
├── entry_long      : árbol de reglas booleanas
├── exit_long       : árbol de reglas booleanas
├── entry_short     : árbol o None
├── exit_short      : árbol o None
├── risk            : sizing, stop, take profit, trailing, límites
├── regime          : filtro de régimen de mercado (opcional)
├── ensemble        : sólo si el bot es una FUSIÓN de varios padres
└── meta            : generación, padres, operador que lo creó, nota del jardinero
```

### features[] — los sentidos del bot

```json
{ "id": "ema_fast", "kind": "EMA",  "source": "close", "params": {"period": 21} }
{ "id": "rsi",      "kind": "RSI",  "source": "close", "params": {"period": 14} }
{ "id": "atr",      "kind": "ATR",  "source": "hlc",   "params": {"period": 14} }
```

El `id` es local al genoma y es cómo las reglas se refieren al indicador. Dos
genomas pueden usar `ema_fast` para cosas distintas; no hay colisión.

El catálogo de indicadores disponibles y sus rangos de parámetros válidos vive en
`genome/catalog.py` y `config/genes.yaml`. **Una mutación nunca puede salirse del
catálogo.**

### Árboles de reglas

Un árbol de reglas es un booleano compuesto:

```json
{
  "op": "AND",
  "children": [
    { "op": "GT",          "left": {"ref": "ema_fast"}, "right": {"ref": "ema_slow"} },
    { "op": "CROSS_ABOVE", "left": {"ref": "rsi"},      "right": {"const": 55} },
    { "op": "NOT", "children": [
        { "op": "GT", "left": {"ref": "atr_pct"}, "right": {"const": 0.06} }
    ]}
  ]
}
```

Nodos lógicos: `AND`, `OR`, `NOT`.
Nodos de comparación: `GT`, `GTE`, `LT`, `LTE`, `CROSS_ABOVE`, `CROSS_BELOW`,
`RISING`, `FALLING`, `BETWEEN`, `PCT_RANK_GT`, `PCT_RANK_LT`.
Operandos: `{"ref": "<feature_id>"}`, `{"const": <número>}`, `{"price": "close"}`.

Límites duros (en config): profundidad máxima 4, máximo 6 hojas por árbol. Sin
esos límites la evolución produce monstruos que sobreajustan cualquier cosa.

### risk — cómo arriesga

```json
{
  "sizing": "ATR_RISK",
  "risk_per_trade": 0.01,
  "max_concurrent_positions": 1,
  "max_exposure": 0.95,
  "stop":        {"kind": "ATR_MULT", "value": 2.5},
  "take_profit": {"kind": "R_MULTIPLE", "value": 3.0},
  "trailing":    {"kind": "ATR_MULT", "value": 3.0, "activate_at_r": 1.0},
  "max_holding_bars": 240,
  "allow_short": false,
  "cooldown_bars": 2
}
```

`sizing`:
- `FIXED_FRACTION` — fracción fija del capital.
- `ATR_RISK` — tamaño tal que el stop equivalga a `risk_per_trade` del capital.
  Es el sensato por defecto.
- `VOL_TARGET` — tamaño inversamente proporcional a la volatilidad realizada.

### regime — cuándo ni lo intenta

Un filtro opcional que apaga el bot en condiciones que no son las suyas. Es uno
de los genes más valiosos evolutivamente: permite que estrategias especialistas
sobrevivan sin sangrar el resto del tiempo.

```json
{ "enabled": true, "rule": { "op": "GT", "left": {"ref": "adx"}, "right": {"const": 20} } }
```

### ensemble — el gen de la fusión

Sólo presente en bots creados por el operador `FUSION`. El bot no tiene reglas
propias: combina las señales de sus padres.

```json
{
  "members": ["bot_0f3a", "bot_91cc", "bot_2de7"],
  "weights": [0.5, 0.3, 0.2],
  "combine": "WEIGHTED",
  "threshold": 0.55
}
```

`combine`: `VOTE` (mayoría simple), `WEIGHTED` (suma ponderada contra umbral),
`UNANIMOUS` (todos de acuerdo, muy selectivo), `ANY` (cualquiera dispara).

El gen de riesgo del bot fusionado **sí es propio** y se hereda del padre con
mejor Sortino o se recombina. Un ensemble hereda las señales pero gestiona su
propio riesgo.

### meta — el pedigrí

```json
{
  "born_at": "2026-09-14T08:00:00Z",
  "generation": 12,
  "parents": ["bot_0f3a", "bot_91cc"],
  "operator": "CROSSOVER",
  "root_lineage": "lin_donchian_a",
  "gardener_note": "injerto de filtro de régimen sobre el linaje de rupturas"
}
```

## Familias de ideas

Cada genoma pertenece a una familia. Las familias existen para dos cosas:
mantener diversidad obligatoria y dar al jardinero un vocabulario con el que
razonar.

| Familia | Idea | Indicadores típicos |
|---|---|---|
| `TREND` | seguir la dirección dominante | EMA, MACD, ADX, Supertrend |
| `MEAN_REVERSION` | apostar a la vuelta al centro | RSI, Bollinger, Z-score, Keltner |
| `BREAKOUT` | entrar cuando se rompe un rango | Donchian, ATR, máximos de N velas |
| `MOMENTUM` | comprar lo que sube más rápido | ROC, RSI de largo plazo, aceleración |
| `VOLATILITY` | operar el régimen, no la dirección | ATR, desviación realizada, BB width |
| `MICROSTRUCTURE` | volumen y presión compradora | OBV, VWAP, volumen relativo |
| `HYBRID` | resultado de cruzar familias distintas | — |

Cuando un cruce mezcla padres de familias distintas, el hijo es `HYBRID` y
conserva las familias de origen en `meta` para poder rastrear qué mezclas
funcionan.

## Serialización

`genome/serialize.py` garantiza round-trip exacto:

```python
g2 = genome_from_dict(genome_to_dict(g1))
assert genome_to_dict(g1) == genome_to_dict(g2)
```

El hash del genoma (`genome_hash`) es el SHA-256 de su JSON canónico **sin
`meta`**. Dos bots con el mismo hash son clones: el detector de clones lo usa
para no llenar el jardín de copias.

## Distancia genética

Necesaria para la especiación. `distance(a, b) ∈ [0, 1]`, combinación de:

- Jaccard sobre el conjunto de `(kind, params_binned)` de los features (0.35)
- Jaccard sobre las hojas normalizadas de los árboles de reglas (0.35)
- Distancia normalizada entre los genes de riesgo (0.20)
- 1 si las familias difieren, 0 si coinciden (0.10)

Dos genomas con distancia < `speciation.clone_threshold` se consideran el mismo
organismo y uno de los dos se descarta.
