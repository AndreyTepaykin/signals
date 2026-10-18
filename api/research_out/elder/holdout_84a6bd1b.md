# Train vs holdout
train  ROI +0.710  90% [+0.029, +3.018]  max DD 0.152  objective +0.029  feasible yes
train no_trade  ROI +0.000  max DD 0.000
train random_tide  mean ROI +0.172  min -0.041  median +0.128  max +0.417  share >= strategy 0.00  mean DD 0.296
holdout  ROI -0.315  90% [-0.404, -0.211]  max DD 0.319  objective n/a  feasible no
holdout no_trade  ROI +0.000  max DD 0.000
holdout random_tide  mean ROI -0.171  min -0.293  median -0.186  max -0.054  share >= strategy 1.00  mean DD 0.214

# Train

Elder Triple Screen  2026-01-01T00:00:00+00:00 .. 2026-07-01T00:00:00+00:00
params 84a6bd1b  ROI +0.710  90% [+0.029, +3.018]  max DD 0.152  objective +0.029  feasible yes
Objective is the 90% ROI lower bound when max equity drawdown is at most 20%. The week bootstrap compounds trades sequentially and approximates concurrent compounding. Censored open shares are not in equity. E[R] excludes censored trades.

Overall
  ROI +0.710  90% [+0.029, +3.018]  max DD 0.152  objective +0.029  feasible yes
  max open 11  mean open 3.10
  closed 120  censored 3  trades/week 4.76  weeks 25.86
  P(T1) 0.425  90% [0.353, 0.500]  (51/120)
  P(10R) 0.075  90% [0.044, 0.125]  (9/120)
  P(stop before T1) 0.425  90% [0.353, 0.500]  (51/120)
  E[R] +0.549  90% [+0.203, +0.921]  PF 2.09  max DD R 15.79R
  hold hours mean 109.7  median 60.2
  R quantiles  p10 -1.07  p25 -1.05  p50 -0.29  p75 +1.14  p90 +3.28
  R histogram  <=-1 51  (-1,0] 24  (0,1] 13  (1,2] 12  (2,5] 11  (5,10] 9  >10 0
  funnel  arms 319  fills 285  expired 34  cancelled 0  entries 162  skipped_opposite 0  skipped_max_open 0  skipped_overlap 39  skipped_total 39  trades 123
  drops  stop_wrong_side 0  stop_too_close_atr 105  costs_over_cap 18  no_wave_context 0
  min-distance drop rate 0.432  (stop_too_close_atr + costs_over_cap) / fills

Sub-windows
  jan_apr
    ROI +0.544  90% [-0.029, +2.083]  max DD 0.080  objective -0.029  feasible yes
    max open 11  mean open 3.11
    closed 77  censored 0  trades/week 4.49  weeks 17.14
    P(T1) 0.494  90% [0.402, 0.586]  (38/77)
    P(10R) 0.065  90% [0.032, 0.127]  (5/77)
    P(stop before T1) 0.338  90% [0.256, 0.430]  (26/77)
    E[R] +0.674  90% [+0.276, +1.104]  PF 2.63  max DD R 10.49R
    hold hours mean 117.9  median 106.0
    R quantiles  p10 -1.06  p25 -1.04  p50 -0.16  p75 +1.44  p90 +2.86
    R histogram  <=-1 26  (-1,0] 18  (0,1] 9  (1,2] 10  (2,5] 9  (5,10] 5  >10 0
    funnel  arms 204  fills 183  expired 21  cancelled 0  entries 99  skipped_opposite 0  skipped_max_open 0  skipped_overlap 22  skipped_total 22  trades 77
    drops  stop_wrong_side 0  stop_too_close_atr 71  costs_over_cap 13  no_wave_context 0
    min-distance drop rate 0.459  (stop_too_close_atr + costs_over_cap) / fills
  may_jun
    ROI +0.108  90% [-0.169, +0.610]  max DD 0.152  objective -0.169  feasible yes
    max open 10  mean open 2.99
    closed 43  censored 3  trades/week 5.28  weeks 8.71
    P(T1) 0.302  90% [0.202, 0.426]  (13/43)
    P(10R) 0.093  90% [0.042, 0.192]  (4/43)
    P(stop before T1) 0.581  90% [0.456, 0.697]  (25/43)
    E[R] +0.325  90% [-0.303, +1.015]  PF 1.49  max DD R 15.79R
    hold hours mean 95.1  median 46.0
    R quantiles  p10 -1.07  p25 -1.05  p50 -1.03  p75 +0.14  p90 +4.54
    R histogram  <=-1 25  (-1,0] 6  (0,1] 4  (1,2] 2  (2,5] 2  (5,10] 4  >10 0
    funnel  arms 115  fills 102  expired 13  cancelled 0  entries 63  skipped_opposite 0  skipped_max_open 0  skipped_overlap 17  skipped_total 17  trades 46
    drops  stop_wrong_side 0  stop_too_close_atr 34  costs_over_cap 5  no_wave_context 0
    min-distance drop rate 0.382  (stop_too_close_atr + costs_over_cap) / fills

Per coin
  BTCUSDT  n 13  pnl +0.0824  max open 2  closed 13  censored 0  trades/week 0.50  P(T1) 0.462  E[R] +0.737 [-0.320, +1.949]
  ETHUSDT  n 16  pnl +0.3277  max open 3  closed 16  censored 0  trades/week 0.62  P(T1) 0.500  E[R] +1.475 [+0.285, +2.837]
  SOLUSDT  n 15  pnl +0.1507  max open 2  closed 14  censored 1  trades/week 0.58  P(T1) 0.429  E[R] +0.913 [-0.309, +2.291]
  BNBUSDT  n 13  pnl -0.0356  max open 2  closed 12  censored 1  trades/week 0.50  P(T1) 0.333  E[R] -0.170 [-0.797, +0.684]
  XRPUSDT  n 11  pnl +0.0154  max open 2  closed 11  censored 0  trades/week 0.43  P(T1) 0.545  E[R] +0.138 [-0.378, +0.692]
  DOGEUSDT  n 8  pnl +0.1590  max open 3  closed 8  censored 0  trades/week 0.31  P(T1) 0.500  E[R] +1.465 [-0.301, +3.381]
  ADAUSDT  n 9  pnl -0.0418  max open 1  closed 8  censored 1  trades/week 0.35  P(T1) 0.375  E[R] -0.330 [-0.835, +0.224]
  AVAXUSDT  n 15  pnl -0.0137  max open 2  closed 15  censored 0  trades/week 0.58  P(T1) 0.333  E[R] +0.173 [-0.598, +1.200]
  LINKUSDT  n 16  pnl -0.0060  max open 2  closed 16  censored 0  trades/week 0.62  P(T1) 0.312  E[R] +0.193 [-0.559, +1.165]
  TRXUSDT  n 7  pnl +0.0718  max open 2  closed 7  censored 0  trades/week 0.27  P(T1) 0.571  E[R] +0.806 [-0.092, +1.694]

Baselines
  no_trade  ROI +0.000  max DD 0.000  E[R] +0.000  trades 0
  random_tide  seeds 20  mean ROI +0.172  min -0.041  median +0.128  max +0.417  share ROI >= strategy 0.00  mean DD 0.296  mean E[R] +0.219  share E[R] >= strategy 0.00

Censored (3; closed_part_r is not in E[R])
  SOLUSDT dir 1  2026-06-28T08:00:00+00:00  open_share 0.70  mark_r +1.363  end_of_period  closed_part_r +0.585
  BNBUSDT dir -1  2026-06-27T08:00:00+00:00  open_share 0.70  mark_r +2.004  end_of_period  closed_part_r +0.579
  ADAUSDT dir -1  2026-06-25T12:00:00+00:00  open_share 1.00  mark_r +0.548  end_of_period  closed_part_r +0.000

# Holdout

Elder Triple Screen  2026-07-01T00:00:00+00:00 .. 2026-10-18T11:00:00+00:00
params 84a6bd1b  ROI -0.315  90% [-0.404, -0.211]  max DD 0.319  objective n/a  feasible no
Objective is the 90% ROI lower bound when max equity drawdown is at most 20%. The week bootstrap compounds trades sequentially and approximates concurrent compounding. Censored open shares are not in equity. E[R] excludes censored trades.

Overall
  ROI -0.315  90% [-0.404, -0.211]  max DD 0.319  objective n/a  feasible no
  max open 7  mean open 1.47
  closed 66  censored 5  trades/week 4.54  weeks 15.64
  P(T1) 0.136  90% [0.081, 0.220]  (9/66)
  P(10R) 0.000  90% [0.000, 0.039]  (0/66)
  P(stop before T1) 0.621  90% [0.520, 0.713]  (41/66)
  E[R] -0.601  90% [-0.743, -0.440]  PF 0.17  max DD R 39.66R
  hold hours mean 50.1  median 21.0
  R quantiles  p10 -1.09  p25 -1.07  p50 -1.05  p75 -0.17  p90 +0.31
  R histogram  <=-1 41  (-1,0] 13  (0,1] 9  (1,2] 1  (2,5] 2  (5,10] 0  >10 0
  funnel  arms 193  fills 169  expired 24  cancelled 0  entries 86  skipped_opposite 0  skipped_max_open 0  skipped_overlap 15  skipped_total 15  trades 71
  drops  stop_wrong_side 0  stop_too_close_atr 57  costs_over_cap 26  no_wave_context 0
  min-distance drop rate 0.491  (stop_too_close_atr + costs_over_cap) / fills

Per coin
  BTCUSDT  n 6  pnl -0.0316  max open 1  closed 6  censored 0  trades/week 0.38  P(T1) 0.167  E[R] -0.646 [-1.088, -0.197]
  ETHUSDT  n 6  pnl -0.0350  max open 1  closed 6  censored 0  trades/week 0.38  P(T1) 0.167  E[R] -0.733 [-1.069, -0.395]
  SOLUSDT  n 6  pnl -0.0166  max open 1  closed 6  censored 0  trades/week 0.38  P(T1) 0.333  E[R] -0.349 [-0.871, +0.221]
  BNBUSDT  n 10  pnl -0.0513  max open 2  closed 8  censored 2  trades/week 0.64  P(T1) 0.000  E[R] -0.854 [-1.074, -0.627]
  XRPUSDT  n 13  pnl -0.0699  max open 2  closed 12  censored 1  trades/week 0.83  P(T1) 0.083  E[R] -0.700 [-1.038, -0.211]
  DOGEUSDT  n 10  pnl -0.0446  max open 2  closed 8  censored 2  trades/week 0.64  P(T1) 0.125  E[R] -0.825 [-1.009, -0.619]
  ADAUSDT  n 7  pnl +0.0131  max open 1  closed 7  censored 0  trades/week 0.45  P(T1) 0.429  E[R] +0.298 [-0.392, +1.038]
  AVAXUSDT  n 4  pnl -0.0300  max open 1  closed 4  censored 0  trades/week 0.26  P(T1) 0.000  E[R] -0.877 [-1.061, -0.693]
  LINKUSDT  n 9  pnl -0.0489  max open 1  closed 9  censored 0  trades/week 0.58  P(T1) 0.000  E[R] -0.671 [-0.918, -0.407]
  TRXUSDT  n 0  pnl +0.0000  max open 0  closed 0  censored 0  trades/week 0.00  P(T1) n/a  E[R] n/a n/a

Baselines
  no_trade  ROI +0.000  max DD 0.000  E[R] +0.000  trades 0
  random_tide  seeds 20  mean ROI -0.171  min -0.293  median -0.186  max -0.054  share ROI >= strategy 1.00  mean DD 0.214  mean E[R] -0.263  share E[R] >= strategy 1.00

Censored (5; closed_part_r is not in E[R])
  BNBUSDT dir -1  2026-10-13T21:00:00+00:00  open_share 0.70  mark_r +1.756  end_of_period  closed_part_r +0.576
  BNBUSDT dir -1  2026-10-16T00:30:00+00:00  open_share 1.00  mark_r +0.540  end_of_period  closed_part_r +0.000
  XRPUSDT dir -1  2026-10-09T08:30:00+00:00  open_share 0.70  mark_r +3.277  end_of_period  closed_part_r +0.588
  DOGEUSDT dir -1  2026-10-13T21:00:00+00:00  open_share 0.70  mark_r +2.707  end_of_period  closed_part_r +0.585
  DOGEUSDT dir -1  2026-10-16T00:00:00+00:00  open_share 0.70  mark_r +0.821  end_of_period  closed_part_r +0.588
