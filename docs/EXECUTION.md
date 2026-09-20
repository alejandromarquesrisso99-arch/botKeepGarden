# Ejecución y simulación

## Modos

```yaml
execution:
  mode: paper        # paper | live
```

- **`paper`** (único permitido ahora): órdenes simuladas contra las velas reales,
  con fricción. Capital simulado.
- **`live`**: existe la interfaz `BrokerAdapter` para que el día de mañana se
  enchufe un broker real, pero `config.py` **aborta** si `mode: live`. El
  desbloqueo es una decisión humana explícita, no una opción de configuración.

## Cómo se rellena una orden

Regla de oro: **la decisión se toma con la vela `t` cerrada; el fill ocurre en la
apertura de la vela `t+1`.**

```
vela t: cierra 08:00-09:00  → el bot decide a las 09:00
vela t+1: abre 09:00        → la orden se rellena a open[t+1] + slippage
```

Nunca se rellena al cierre de la misma vela que generó la señal. Eso es
look-ahead disfrazado y es lo que hace que los backtests malos parezcan buenos.

### Stops y take profits

Se evalúan **dentro** de la vela, con la convención pesimista:

- Si en una vela el `low` toca el stop **y** el `high` toca el take profit, se
  asume que saltó el **stop** primero. Siempre el peor caso.
- El fill del stop es `stop_price - slippage`, no el `low` de la vela.
- Un gap de apertura que abra más allá del stop rellena en `open`, no en el stop.
  Los gaps existen y cuestan dinero.

## Fricción

```yaml
frictions:
  taker_fee_bps: 10.0       # 0.10 % — comisión taker de Binance spot
  maker_fee_bps: 10.0       # se asume taker siempre: somos tomadores de precio
  slippage_model: atr       # fixed_bps | atr | volume_aware
  slippage_bps: 3.0         # usado si slippage_model = fixed_bps
  slippage_atr_frac: 0.05   # usado si slippage_model = atr
  min_notional: 10.0        # USDT, mínimo de Binance
  price_precision: 2
  amount_precision: 6
```

**Modelo de slippage por defecto (`atr`)**: el deslizamiento es
`slippage_atr_frac * ATR(14)` en la dirección adversa. Es más honesto que un
número fijo de puntos básicos, porque el coste real de cruzar el spread escala
con la volatilidad.

`volume_aware` añade un término proporcional a `tamaño_orden / volumen_vela`,
relevante sólo si algún día el capital crece. Con capital pequeño es ruido.

## Contabilidad

- Capital inicial por bot: `garden.initial_capital_per_bot` (por defecto 1.000
  USDT simulados).
- Cada bot tiene su **propia cartera aislada**. No comparten capital. Esto es
  deliberado: hace las comparaciones limpias y evita que un bot le robe capacidad
  a otro.
- El "capital del jardín" que se muestra en el dashboard es la suma de las
  carteras, más una cartera virtual de referencia que aplica *buy & hold* sobre
  el mismo símbolo, para tener siempre un espejo honesto.

### PnL

```
pnl_bruto   = (precio_salida - precio_entrada) * cantidad * dirección
comisiones  = (notional_entrada + notional_salida) * fee_rate
pnl_neto    = pnl_bruto - comisiones
```

Sin apalancamiento. Sin cortos reales (el corto en spot no existe): si
`allow_short` está activo, se simula como una posición sintética y **se marca**
como tal, para no confundir un backtest de spot con uno de futuros. En el hito 1
`allow_short` está desactivado por defecto.

### Impuestos

No se modelan dentro del motor. El jardín compara estrategias entre sí, y el
impuesto es aproximadamente un factor común. Cuando se llegue a dinero real se
añadirá una capa de informe fiscal aparte; meterlo dentro del fitness ahora sólo
añadiría ruido a la comparación.

## Circuit breakers

Independientes del fitness. Se comprueban en cada tick:

| Breaker | Efecto |
|---|---|
| Drawdown del bot > `risk.hard_max_drawdown` (35%) | Poda inmediata del bot |
| Drawdown del jardín > `risk.garden_max_drawdown` (25%) | Se pausan los nacimientos y se sube la presión de poda |
| Vela con salto > 40% o datos sospechosos | Se congela el tick, no se opera, se registra el evento |
| Fallo del venue N veces seguidas | El jardín entra en `DEGRADED`: no opera, sigue registrando |

## Idempotencia

La tabla `orders` tiene índice único sobre `(bot_id, candle_ts, kind)`. Si el
jardín reprocesa una vela tras un reinicio, el `INSERT OR IGNORE` evita
duplicados. Un tick reprocesado debe dejar la base exactamente igual.
