Elder Triple Screen  2026-01-01T00:00:00+00:00 .. 2026-07-01T00:00:00+00:00
params a7fad8ae  ROI +0.030  90% [-0.071, +0.140]  max DD 0.034  objective -0.071  feasible yes
Objective is the 90% ROI lower bound when max equity drawdown is at most 20%. The week bootstrap compounds trades sequentially and approximates concurrent compounding. Censored open shares are not in equity. E[R] excludes censored trades.

Overall
  ROI +0.030  90% [-0.071, +0.140]  max DD 0.034  objective -0.071  feasible yes
  max open 6  mean open 0.49
  closed 27  censored 0  trades/week 1.04  weeks 25.86
  P(T1) 0.481  90% [0.332, 0.634]  (13/27)
  P(10R) 0.000  90% [0.000, 0.091]  (0/27)
  P(stop before T1) 0.444  90% [0.299, 0.600]  (12/27)
  E[R] +0.118  90% [-0.269, +0.523]  PF 1.24  max DD R 4.14R
  hold hours mean 78.2  median 55.5
  R quantiles  p10 -1.05  p25 -1.04  p50 -0.46  p75 +1.12  p90 +1.70
  R histogram  <=-1 12  (-1,0] 2  (0,1] 5  (1,2] 6  (2,5] 2  (5,10] 0  >10 0
  funnel  arms 36  fills 29  expired 7  cancelled 0  entries 27  skipped_opposite 0  skipped_max_open 0  skipped_overlap 0  skipped_total 0  trades 27
  drops  stop_wrong_side 0  stop_too_close_atr 1  costs_over_cap 1  no_wave_context 0
  min-distance drop rate 0.069  (stop_too_close_atr + costs_over_cap) / fills

Sub-windows
  jan_apr
    ROI +0.001  90% [-0.066, +0.074]  max DD 0.028  objective -0.066  feasible yes
    max open 6  mean open 0.42
    closed 17  censored 0  trades/week 0.99  weeks 17.14
    P(T1) 0.471  90% [0.290, 0.660]  (8/17)
    P(10R) 0.000  90% [0.000, 0.137]  (0/17)
    P(stop before T1) 0.412  90% [0.241, 0.607]  (7/17)
    E[R] +0.012  90% [-0.412, +0.440]  PF 1.02  max DD R 3.15R
    hold hours mean 71.6  median 46.0
    R quantiles  p10 -1.06  p25 -1.04  p50 -0.46  p75 +0.86  p90 +1.39
    R histogram  <=-1 7  (-1,0] 2  (0,1] 4  (1,2] 3  (2,5] 1  (5,10] 0  >10 0
    funnel  arms 23  fills 18  expired 5  cancelled 0  entries 17  skipped_opposite 0  skipped_max_open 0  skipped_overlap 0  skipped_total 0  trades 17
    drops  stop_wrong_side 0  stop_too_close_atr 1  costs_over_cap 0  no_wave_context 0
    min-distance drop rate 0.056  (stop_too_close_atr + costs_over_cap) / fills
  may_jun
    ROI +0.028  90% [-0.042, +0.109]  max DD 0.034  objective -0.042  feasible yes
    max open 3  mean open 0.61
    closed 10  censored 0  trades/week 1.15  weeks 8.71
    P(T1) 0.500  90% [0.269, 0.731]  (5/10)
    P(10R) 0.000  90% [0.000, 0.213]  (0/10)
    P(stop before T1) 0.500  90% [0.269, 0.731]  (5/10)
    E[R] +0.300  90% [-0.436, +1.084]  PF 1.58  max DD R 4.14R
    hold hours mean 89.5  median 59.2
    R quantiles  p10 -1.04  p25 -1.03  p50 -0.23  p75 +1.48  p90 +1.99
    R histogram  <=-1 5  (-1,0] 0  (0,1] 1  (1,2] 3  (2,5] 1  (5,10] 0  >10 0
    funnel  arms 13  fills 11  expired 2  cancelled 0  entries 10  skipped_opposite 0  skipped_max_open 0  skipped_overlap 0  skipped_total 0  trades 10
    drops  stop_wrong_side 0  stop_too_close_atr 0  costs_over_cap 1  no_wave_context 0
    min-distance drop rate 0.091  (stop_too_close_atr + costs_over_cap) / fills

Per coin
  BTCUSDT  n 3  pnl +0.0061  max open 1  closed 3  censored 0  trades/week 0.12  P(T1) 0.667  E[R] +0.209 [-0.545, +0.963]
  ETHUSDT  n 4  pnl +0.0139  max open 1  closed 4  censored 0  trades/week 0.15  P(T1) 0.750  E[R] +0.354 [-0.245, +0.923]
  SOLUSDT  n 2  pnl +0.0190  max open 1  closed 2  censored 0  trades/week 0.08  P(T1) 0.500  E[R] +0.965 [-1.056, +2.985]
  BNBUSDT  n 3  pnl -0.0248  max open 1  closed 3  censored 0  trades/week 0.12  P(T1) 0.000  E[R] -0.841 [-1.038, -0.645]
  XRPUSDT  n 5  pnl +0.0016  max open 1  closed 5  censored 0  trades/week 0.19  P(T1) 0.400  E[R] +0.042 [-0.796, +0.887]
  DOGEUSDT  n 2  pnl -0.0084  max open 1  closed 2  censored 0  trades/week 0.08  P(T1) 0.500  E[R] -0.417 [-1.034, +0.199]
  ADAUSDT  n 1  pnl -0.0104  max open 1  closed 1  censored 0  trades/week 0.04  P(T1) 0.000  E[R] -1.033 n/a
  AVAXUSDT  n 3  pnl -0.0317  max open 1  closed 3  censored 0  trades/week 0.12  P(T1) 0.000  E[R] -1.048 [-1.055, -1.041]
  LINKUSDT  n 2  pnl +0.0287  max open 1  closed 2  censored 0  trades/week 0.08  P(T1) 1.000  E[R] +1.463 [+1.352, +1.574]
  TRXUSDT  n 2  pnl +0.0356  max open 1  closed 2  censored 0  trades/week 0.08  P(T1) 1.000  E[R] +1.813 [+1.441, +2.185]

Baselines
  no_trade  ROI +0.000  max DD 0.000  E[R] +0.000  trades 0
  random_tide  seeds 20  mean ROI +0.007  min -0.108  median +0.012  max +0.099  share ROI >= strategy 0.45  mean DD 0.074  mean E[R] +0.024  share E[R] >= strategy 0.45

Censored (0; closed_part_r is not in E[R])
  none
