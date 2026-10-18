Elder Triple Screen  2026-01-01T00:00:00+00:00 .. 2026-07-01T00:00:00+00:00
params 5cef96ee  ROI +0.121  90% [-0.146, +0.558]  max DD 0.091  objective -0.146  feasible yes
Objective is the 90% ROI lower bound when max equity drawdown is at most 20%. The week bootstrap compounds trades sequentially and approximates concurrent compounding. Censored open shares are not in equity. E[R] excludes censored trades.

Overall
  ROI +0.121  90% [-0.146, +0.558]  max DD 0.091  objective -0.146  feasible yes
  max open 9  mean open 1.99
  closed 71  censored 1  trades/week 2.78  weeks 25.86
  P(T1) 0.366  90% [0.279, 0.464]  (26/71)
  P(10R) 0.028  90% [0.009, 0.082]  (2/71)
  P(stop before T1) 0.507  90% [0.411, 0.603]  (36/71)
  E[R] +0.188  90% [-0.144, +0.541]  PF 1.32  max DD R 10.67R
  hold hours mean 119.8  median 56.5
  R quantiles  p10 -1.06  p25 -1.04  p50 -1.02  p75 +1.21  p90 +2.17
  R histogram  <=-1 36  (-1,0] 7  (0,1] 9  (1,2] 10  (2,5] 7  (5,10] 2  >10 0
  funnel  arms 100  fills 86  expired 14  cancelled 0  entries 73  skipped_opposite 0  skipped_max_open 0  skipped_overlap 1  skipped_total 1  trades 72
  drops  stop_wrong_side 0  stop_too_close_atr 9  costs_over_cap 4  no_wave_context 0
  min-distance drop rate 0.151  (stop_too_close_atr + costs_over_cap) / fills

Sub-windows
  jan_apr
    ROI +0.068  90% [-0.102, +0.312]  max DD 0.072  objective -0.102  feasible yes
    max open 9  mean open 1.51
    closed 40  censored 0  trades/week 2.33  weeks 17.14
    P(T1) 0.350  90% [0.239, 0.480]  (14/40)
    P(10R) 0.050  90% [0.017, 0.140]  (2/40)
    P(stop before T1) 0.475  90% [0.351, 0.602]  (19/40)
    E[R] +0.180  90% [-0.282, +0.707]  PF 1.33  max DD R 8.69R
    hold hours mean 112.4  median 46.2
    R quantiles  p10 -1.06  p25 -1.04  p50 -0.58  p75 +0.64  p90 +1.97
    R histogram  <=-1 19  (-1,0] 5  (0,1] 8  (1,2] 4  (2,5] 2  (5,10] 2  >10 0
    funnel  arms 56  fills 48  expired 8  cancelled 0  entries 40  skipped_opposite 0  skipped_max_open 0  skipped_overlap 0  skipped_total 0  trades 40
    drops  stop_wrong_side 0  stop_too_close_atr 6  costs_over_cap 2  no_wave_context 0
    min-distance drop rate 0.167  (stop_too_close_atr + costs_over_cap) / fills
  may_jun
    ROI +0.050  90% [-0.158, +0.402]  max DD 0.073  objective -0.158  feasible yes
    max open 9  mean open 2.83
    closed 31  censored 1  trades/week 3.67  weeks 8.71
    P(T1) 0.387  90% [0.258, 0.534]  (12/31)
    P(10R) 0.000  90% [0.000, 0.080]  (0/31)
    P(stop before T1) 0.548  90% [0.403, 0.686]  (17/31)
    E[R] +0.197  90% [-0.272, +0.695]  PF 1.32  max DD R 7.30R
    hold hours mean 129.2  median 72.0
    R quantiles  p10 -1.05  p25 -1.04  p50 -1.02  p75 +1.47  p90 +3.23
    R histogram  <=-1 17  (-1,0] 2  (0,1] 1  (1,2] 6  (2,5] 5  (5,10] 0  >10 0
    funnel  arms 44  fills 38  expired 6  cancelled 0  entries 33  skipped_opposite 0  skipped_max_open 0  skipped_overlap 1  skipped_total 1  trades 32
    drops  stop_wrong_side 0  stop_too_close_atr 3  costs_over_cap 2  no_wave_context 0
    min-distance drop rate 0.132  (stop_too_close_atr + costs_over_cap) / fills

Per coin
  BTCUSDT  n 8  pnl -0.0275  max open 1  closed 8  censored 0  trades/week 0.31  P(T1) 0.375  E[R] -0.286 [-0.851, +0.327]
  ETHUSDT  n 8  pnl -0.0241  max open 2  closed 8  censored 0  trades/week 0.31  P(T1) 0.250  E[R] -0.286 [-0.817, +0.283]
  SOLUSDT  n 7  pnl +0.0705  max open 2  closed 7  censored 0  trades/week 0.27  P(T1) 0.571  E[R] +1.008 [-0.082, +2.211]
  BNBUSDT  n 8  pnl -0.0576  max open 1  closed 8  censored 0  trades/week 0.31  P(T1) 0.125  E[R] -0.665 [-0.972, -0.277]
  XRPUSDT  n 13  pnl +0.0402  max open 2  closed 13  censored 0  trades/week 0.50  P(T1) 0.385  E[R] +0.334 [-0.510, +1.351]
  DOGEUSDT  n 5  pnl -0.0129  max open 2  closed 5  censored 0  trades/week 0.19  P(T1) 0.400  E[R] -0.257 [-1.037, +0.526]
  ADAUSDT  n 5  pnl +0.0123  max open 1  closed 4  censored 1  trades/week 0.19  P(T1) 0.250  E[R] +0.296 [-1.036, +2.162]
  AVAXUSDT  n 5  pnl -0.0553  max open 1  closed 5  censored 0  trades/week 0.19  P(T1) 0.000  E[R] -1.038 [-1.049, -1.028]
  LINKUSDT  n 6  pnl +0.0675  max open 1  closed 6  censored 0  trades/week 0.23  P(T1) 0.500  E[R] +1.111 [-0.506, +2.976]
  TRXUSDT  n 7  pnl +0.1078  max open 2  closed 7  censored 0  trades/week 0.27  P(T1) 0.714  E[R] +1.492 [+0.369, +2.549]

Baselines
  no_trade  ROI +0.000  max DD 0.000  E[R] +0.000  trades 0
  random_tide  seeds 20  mean ROI +0.174  min -0.058  median +0.160  max +0.402  share ROI >= strategy 0.75  mean DD 0.142  mean E[R] +0.260  share E[R] >= strategy 0.75

Censored (1; closed_part_r is not in E[R])
  ADAUSDT dir -1  2026-06-25T00:30:00+00:00  open_share 1.00  mark_r +0.371  end_of_period  closed_part_r +0.000
