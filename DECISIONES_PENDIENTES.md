## ADX threshold = 45 vs ADX actual = 87 sostenido

Estado actual: el filtro bloquea casi todas las entries en mercado over-trended.

Hipótesis: ADX > 60 indica trend agotado, no fortaleza. Subir el threshold (ej. 30) podría ser contraproducente — quizá la decisión correcta sea CAMBIAR de "ADX > THRESHOLD = enter" a "ADX < THRESHOLD = enter", o usar ADX en otro timeframe (5m / 15m).

Datos para revisar: paper test n=3, WR 33%. Insuficiente para concluir.

Acción: esperar n>=20 trades antes de tocar este parámetro. Mientras tanto, dejar el filtro como está.

## Divergencia paper test vs backtest V6

Backtest V6 esperaba WR ≈ 64% en BTC. Paper lleva 33% sobre n=3. Sample chico, no concluyente.

Posibles causas: (1) regime change post-backtest, (2) bias del backtest (look-ahead, cherry-picked period), (3) fees/slippage en paper distintos al backtest, (4) n=3 es ruido.

Acción: continuar paper hasta n>=20. Si WR<50% sostenido a n=20, redo del backtest con datos posteriores a 2026-04-25.

NO pasar a mainnet con criterio 6 (paper ±20%) aún en 0/4 gates.
