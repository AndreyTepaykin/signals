Elder Triple Screen  2026-01-01T00:00:00+00:00 .. 2026-07-01T00:00:00+00:00
params 0cef0f75  ROI +0.827  90% [-0.013, +4.784]  max DD 0.154  objective -0.013  feasible yes
Objective is the 90% ROI lower bound when max equity drawdown is at most 20%. The week bootstrap compounds trades sequentially and approximates concurrent compounding. Censored open shares are not in equity. E[R] excludes censored trades.

Overall
  ROI +0.827  90% [-0.013, +4.784]  max DD 0.154  objective -0.013  feasible yes
  max open 14  mean open 4.18
  closed 177  censored 6  trades/week 7.08  weeks 25.86
  P(T1) 0.424  90% [0.364, 0.486]  (75/177)
  P(10R) 0.062  90% [0.038, 0.099]  (11/177)
  P(stop before T1) 0.435  90% [0.375, 0.497]  (77/177)
  E[R] +0.435  90% [+0.182, +0.693]  PF 1.87  max DD R 18.00R
  hold hours mean 98.1  median 55.0
  R quantiles  p10 -1.07  p25 -1.05  p50 -0.30  p75 +1.06  p90 +2.87
  R histogram  <=-1 77  (-1,0] 23  (0,1] 31  (1,2] 20  (2,5] 15  (5,10] 11  >10 0
  funnel  arms 467  fills 414  expired 53  cancelled 0  entries 257  skipped_opposite 0  skipped_max_open 0  skipped_overlap 74  skipped_total 74  trades 183
  drops  stop_wrong_side 0  stop_too_close_atr 133  costs_over_cap 24  no_wave_context 0
  min-distance drop rate 0.379  (stop_too_close_atr + costs_over_cap) / fills

Sub-windows
  jan_apr
    ROI +0.548  90% [-0.135, +3.201]  max DD 0.137  objective -0.135  feasible yes
    max open 14  mean open 3.93
    closed 117  censored 0  trades/week 6.83  weeks 17.14
    P(T1) 0.470  90% [0.396, 0.546]  (55/117)
    P(10R) 0.051  90% [0.027, 0.096]  (6/117)
    P(stop before T1) 0.368  90% [0.298, 0.443]  (43/117)
    E[R] +0.487  90% [+0.202, +0.800]  PF 2.12  max DD R 18.00R
    hold hours mean 98.1  median 76.5
    R quantiles  p10 -1.07  p25 -1.04  p50 -0.12  p75 +1.21  p90 +2.45
    R histogram  <=-1 43  (-1,0] 18  (0,1] 22  (1,2] 17  (2,5] 11  (5,10] 6  >10 0
    funnel  arms 297  fills 264  expired 33  cancelled 0  entries 164  skipped_opposite 0  skipped_max_open 0  skipped_overlap 47  skipped_total 47  trades 117
    drops  stop_wrong_side 0  stop_too_close_atr 85  costs_over_cap 15  no_wave_context 0
    min-distance drop rate 0.379  (stop_too_close_atr + costs_over_cap) / fills
  may_jun
    ROI +0.180  90% [-0.166, +0.919]  max DD 0.154  objective -0.166  feasible yes
    max open 11  mean open 4.55
    closed 60  censored 6  trades/week 7.57  weeks 8.71
    P(T1) 0.333  90% [0.242, 0.439]  (20/60)
    P(10R) 0.083  90% [0.041, 0.161]  (5/60)
    P(stop before T1) 0.567  90% [0.461, 0.667]  (34/60)
    E[R] +0.336  90% [-0.150, +0.838]  PF 1.53  max DD R 17.35R
    hold hours mean 98.2  median 46.8
    R quantiles  p10 -1.07  p25 -1.05  p50 -1.03  p75 +0.76  p90 +4.03
    R histogram  <=-1 34  (-1,0] 5  (0,1] 9  (1,2] 3  (2,5] 4  (5,10] 5  >10 0
    funnel  arms 170  fills 150  expired 20  cancelled 0  entries 93  skipped_opposite 0  skipped_max_open 0  skipped_overlap 27  skipped_total 27  trades 66
    drops  stop_wrong_side 0  stop_too_close_atr 48  costs_over_cap 9  no_wave_context 0
    min-distance drop rate 0.380  (stop_too_close_atr + costs_over_cap) / fills

Per coin
  BTCUSDT  n 18  pnl +0.1430  max open 2  closed 18  censored 0  trades/week 0.70  P(T1) 0.500  E[R] +0.876 [-0.008, +1.876]
  ETHUSDT  n 21  pnl +0.3285  max open 3  closed 20  censored 1  trades/week 0.81  P(T1) 0.500  E[R] +1.125 [+0.239, +2.144]
  SOLUSDT  n 21  pnl +0.0901  max open 2  closed 20  censored 1  trades/week 0.81  P(T1) 0.350  E[R] +0.437 [-0.377, +1.352]
  BNBUSDT  n 20  pnl -0.0330  max open 2  closed 19  censored 1  trades/week 0.77  P(T1) 0.316  E[R] +0.018 [-0.686, +0.851]
  XRPUSDT  n 18  pnl -0.0395  max open 2  closed 18  censored 0  trades/week 0.70  P(T1) 0.389  E[R] -0.091 [-0.511, +0.357]
  DOGEUSDT  n 16  pnl +0.2215  max open 3  closed 16  censored 0  trades/week 0.62  P(T1) 0.562  E[R] +1.035 [+0.114, +2.040]
  ADAUSDT  n 14  pnl +0.0771  max open 2  closed 12  censored 2  trades/week 0.54  P(T1) 0.417  E[R] +0.555 [-0.333, +1.662]
  AVAXUSDT  n 20  pnl -0.1054  max open 2  closed 20  censored 0  trades/week 0.77  P(T1) 0.350  E[R] -0.298 [-0.617, +0.048]
  LINKUSDT  n 24  pnl +0.0276  max open 2  closed 23  censored 1  trades/week 0.93  P(T1) 0.391  E[R] +0.250 [-0.338, +0.939]
  TRXUSDT  n 11  pnl +0.1168  max open 3  closed 11  censored 0  trades/week 0.43  P(T1) 0.545  E[R] +0.760 [-0.096, +1.685]

Baselines
  no_trade  ROI +0.000  max DD 0.000  E[R] +0.000  trades 0
  random_tide  seeds 20  mean ROI +0.133  min -0.159  median +0.075  max +0.588  share ROI >= strategy 0.00  mean DD 0.381  mean E[R] +0.128  share E[R] >= strategy 0.00

Censored (6; closed_part_r is not in E[R])
  ETHUSDT dir -1  2026-06-22T16:00:00+00:00  open_share 0.60  mark_r +3.291  end_of_period  closed_part_r +0.784
  SOLUSDT dir 1  2026-06-28T08:00:00+00:00  open_share 0.60  mark_r +1.363  end_of_period  closed_part_r +0.780
  BNBUSDT dir -1  2026-06-27T08:00:00+00:00  open_share 0.60  mark_r +2.004  end_of_period  closed_part_r +0.773
  ADAUSDT dir -1  2026-06-20T08:00:00+00:00  open_share 0.60  mark_r +4.409  end_of_period  closed_part_r +0.766
  ADAUSDT dir -1  2026-06-25T12:00:00+00:00  open_share 1.00  mark_r +0.548  end_of_period  closed_part_r +0.000
  LINKUSDT dir -1  2026-06-30T00:00:00+00:00  open_share 1.00  mark_r +0.836  end_of_period  closed_part_r +0.000
