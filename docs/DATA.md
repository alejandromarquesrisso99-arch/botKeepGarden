# Datos

## Fuente

Binance spot vía `ccxt`, endpoint público de klines. No hace falta clave API para
leer velas. Motivos: histórico largo y gratuito, 24/7, buena liquidez en los
pares principales, y `ccxt` permite cambiar de venue después sin tocar el resto.

## Timeframe

**1 hora, velas cerradas.** Una vela se considera disponible sólo cuando su
`close_time` ya ha pasado. Nunca se opera sobre la vela en curso: es la fuente de
look-ahead más común y más difícil de detectar después.

Se descargan además velas de `4h` y `1d` para los indicadores de contexto
(filtros de régimen multi-timeframe). Su alineación con la vela de 1h se hace
siempre hacia atrás: en la vela de 1h de las 13:00, la vela diaria disponible es
la de **ayer**, no la de hoy en curso.

## Símbolos

Semilla: `BTC/USDT`. La arquitectura es multi-símbolo desde el principio —el
símbolo es parte del `market` del genoma— pero el hito 1 corre con uno solo.
Candidatos para ampliar: `ETH/USDT`, `SOL/USDT`, `BNB/USDT`.

Cuando haya varios símbolos, un bot opera **uno solo**. Un bot que opera varios
mercados a la vez es un ensemble de bots mono-mercado; así la herencia sigue
siendo limpia.

## Almacenamiento

```
state/cache/
  candles/
    BINANCE/
      BTC_USDT/
        1h/
          2019.parquet
          2020.parquet
          ...
          2026.parquet
```

Parquet particionado por año. Columnas: `ts` (int64, ms UTC, apertura de la
vela), `open`, `high`, `low`, `close`, `volume`, `trades`. Índice implícito por
`ts`, estrictamente creciente y sin huecos.

Un manifiesto `state/cache/candles/manifest.json` guarda por cada
`(venue, symbol, timeframe)`: primer ts, último ts, número de velas y hash de
integridad, para arrancar rápido sin releer todo.

## Backfill

```
keepgarden data backfill --symbol BTC/USDT --timeframe 1h --since 2019-01-01
```

- Pagina hacia adelante en bloques de 1000 velas (límite de Binance).
- Respeta el rate limit con el `enableRateLimit` de `ccxt`.
- Es **reanudable**: si se corta, la siguiente ejecución continúa desde la última
  vela en caché.
- Verifica al terminar que no hay huecos ni duplicados.

## Huecos y anomalías

Binance ocasionalmente tiene paradas de mantenimiento. Tras el backfill:

- Un hueco de 1–2 velas se rellena por *forward fill* con volumen 0 y se marca en
  una tabla `data_gaps`.
- Un hueco mayor **no se rellena**: se marca y los backtests que lo atraviesan
  cortan las posiciones abiertas al inicio del hueco y no reabren hasta después.
  Inventar precio en un hueco largo es inventar rentabilidad.
- Velas con `high < low`, `volume < 0` o saltos de precio > 40% en una hora se
  marcan como sospechosas y se registran; no se descartan en silencio.

## Streaming en vivo

El jardín no usa websockets. En el minuto 0 de cada hora (+ unos segundos de
margen configurable) pide por REST la última vela cerrada. Motivos: simplicidad,
robustez ante desconexiones, y con timeframe de 1h no hay ninguna ventaja en el
websocket.

`engine/clock.py`:

```
esperar hasta (próxima hora en punto + settle_delay_seconds)
pedir las últimas K velas
quedarse con las cerradas que aún no estén en caché
si hay más de una (estuvimos caídos) -> modo catch-up, procesarlas en orden
```

## Indicadores

`data/indicators.py` implementa la librería. Reglas:

1. **Vectorizados sobre `numpy`/`pandas`**, porque la incubadora los llama
   millones de veces.
2. **Causales**: `indicador[t]` sólo puede depender de `datos[0..t]`. Prohibido
   `center=True`, `shift(-n)` y cualquier `rolling` centrado.
3. **Cacheados por `(kind, params, symbol, timeframe)`** dentro de una misma
   pasada. Si 40 bots piden `EMA(21)`, se calcula una vez.
4. **Con periodo de calentamiento explícito**: cada indicador declara cuántas
   velas necesita antes de ser válido. El motor no deja operar a un bot hasta que
   todos sus indicadores están calientes.

Catálogo inicial (ver `config/genes.yaml` para rangos):

| Categoría | Indicadores |
|---|---|
| Tendencia | `SMA`, `EMA`, `WMA`, `MACD`, `ADX`, `SUPERTREND`, `SLOPE` |
| Oscilador | `RSI`, `STOCH`, `CCI`, `WILLR`, `ROC`, `ZSCORE` |
| Volatilidad | `ATR`, `ATR_PCT`, `BBANDS`, `BB_WIDTH`, `KELTNER`, `REALIZED_VOL` |
| Rango | `DONCHIAN_HIGH`, `DONCHIAN_LOW`, `HIGHEST`, `LOWEST` |
| Volumen | `OBV`, `VWAP`, `VOL_RATIO`, `MFI` |
| Derivados | `PCT_CHANGE`, `PCT_RANK`, `DIST_TO` (distancia normalizada entre dos features) |

`DIST_TO` merece mención: permite reglas como "el precio está a menos de 0.5 ATR
de la banda inferior" sin necesidad de constantes dependientes de escala. Los
genes que no dependen de la escala de precio generalizan mucho mejor.
