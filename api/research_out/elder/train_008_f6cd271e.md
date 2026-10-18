Elder Triple Screen  2026-01-01T00:00:00+00:00 .. 2026-07-01T00:00:00+00:00
params f6cd271e  ROI +0.342  90% [-0.002, +1.077]  max DD 0.038  objective -0.002  feasible yes
Objective is the 90% ROI lower bound when max equity drawdown is at most 20%. The week bootstrap compounds trades sequentially and approximates concurrent compounding. Censored open shares are not in equity. E[R] excludes censored trades.

Overall
  ROI +0.342  90% [-0.002, +1.077]  max DD 0.038  objective -0.002  feasible yes
  max open 8  mean open 1.28
  closed 54  censored 1  trades/week 2.13  weeks 25.86
  P(T1) 0.537  90% [0.426, 0.644]  (29/54)
  P(10R) 0.019  90% [0.004, 0.079]  (1/54)
  P(stop before T1) 0.296  90% [0.206, 0.406]  (16/54)
  E[R] +0.570  90% [+0.205, +0.928]  PF 2.58  max DD R 5.24R
  hold hours mean 101.6  median 92.0
  R quantiles  p10 -1.06  p25 -1.03  p50 +0.13  p75 +1.64  p90 +2.35
  R histogram  <=-1 16  (-1,0] 9  (0,1] 9  (1,2] 9  (2,5] 10  (5,10] 1  >10 0
  funnel  arms 134  fills 118  expired 16  cancelled 0  entries 61  skipped_opposite 0  skipped_max_open 0  skipped_overlap 6  skipped_total 6  trades 55
  drops  stop_wrong_side 0  stop_too_close_atr 53  costs_over_cap 4  no_wave_context 0
  min-distance drop rate 0.483  (stop_too_close_atr + costs_over_cap) / fills

Sub-windows
  jan_apr
    ROI +0.320  90% [+0.015, +0.975]  max DD 0.026  objective +0.015  feasible yes
    max open 8  mean open 1.52
    closed 39  censored 0  trades/week 2.27  weeks 17.14
    P(T1) 0.641  90% [0.509, 0.754]  (25/39)
    P(10R) 0.000  90% [0.000, 0.065]  (0/39)
    P(stop before T1) 0.231  90% [0.140, 0.357]  (9/39)
    E[R] +0.764  90% [+0.380, +1.139]  PF 3.71  max DD R 5.24R
    hold hours mean 112.1  median 113.0
    R quantiles  p10 -1.06  p25 -0.40  p50 +0.67  p75 +1.97  p90 +2.37
    R histogram  <=-1 9  (-1,0] 5  (0,1] 7  (1,2] 8  (2,5] 10  (5,10] 0  >10 0
    funnel  arms 91  fills 80  expired 11  cancelled 0  entries 42  skipped_opposite 0  skipped_max_open 0  skipped_overlap 3  skipped_total 3  trades 39
    drops  stop_wrong_side 0  stop_too_close_atr 36  costs_over_cap 2  no_wave_context 0
    min-distance drop rate 0.475  (stop_too_close_atr + costs_over_cap) / fills
  may_jun
    ROI +0.017  90% [-0.063, +0.116]  max DD 0.032  objective -0.063  feasible yes
    max open 5  mean open 0.82
    closed 15  censored 1  trades/week 1.84  weeks 8.71
    P(T1) 0.267  90% [0.126, 0.479]  (4/15)
    P(10R) 0.067  90% [0.015, 0.251]  (1/15)
    P(stop before T1) 0.467  90% [0.277, 0.667]  (7/15)
    E[R] +0.066  90% [-0.635, +1.026]  PF 1.12  max DD R 3.42R
    hold hours mean 74.3  median 29.5
    R quantiles  p10 -1.07  p25 -1.05  p50 -0.57  p75 +0.02  p90 +1.35
    R histogram  <=-1 7  (-1,0] 4  (0,1] 2  (1,2] 1  (2,5] 0  (5,10] 1  >10 0
    funnel  arms 43  fills 38  expired 5  cancelled 0  entries 19  skipped_opposite 0  skipped_max_open 0  skipped_overlap 3  skipped_total 3  trades 16
    drops  stop_wrong_side 0  stop_too_close_atr 17  costs_over_cap 2  no_wave_context 0
    min-distance drop rate 0.500  (stop_too_close_atr + costs_over_cap) / fills

Per coin
  BTCUSDT  n 9  pnl +0.0471  max open 2  closed 9  censored 0  trades/week 0.35  P(T1) 0.556  E[R] +0.508 [-0.277, +1.335]
  ETHUSDT  n 6  pnl +0.1521  max open 1  closed 6  censored 0  trades/week 0.23  P(T1) 0.833  E[R] +2.031 [+0.654, +3.930]
  SOLUSDT  n 4  pnl +0.0445  max open 1  closed 4  censored 0  trades/week 0.15  P(T1) 0.750  E[R] +1.091 [-0.100, +2.105]
  BNBUSDT  n 6  pnl +0.0403  max open 2  closed 5  censored 1  trades/week 0.23  P(T1) 0.400  E[R] +0.647 [-0.460, +2.245]
  XRPUSDT  n 5  pnl +0.0599  max open 1  closed 5  censored 0  trades/week 0.19  P(T1) 0.800  E[R] +1.036 [+0.239, +1.865]
  DOGEUSDT  n 1  pnl +0.0089  max open 1  closed 1  censored 0  trades/week 0.04  P(T1) 1.000  E[R] +0.674 n/a
  ADAUSDT  n 3  pnl -0.0378  max open 1  closed 3  censored 0  trades/week 0.12  P(T1) 0.000  E[R] -1.038 [-1.049, -1.028]
  AVAXUSDT  n 9  pnl +0.0001  max open 1  closed 9  censored 0  trades/week 0.35  P(T1) 0.444  E[R] +0.080 [-0.572, +0.775]
  LINKUSDT  n 7  pnl -0.0212  max open 1  closed 7  censored 0  trades/week 0.27  P(T1) 0.286  E[R] -0.163 [-0.809, +0.541]
  TRXUSDT  n 5  pnl +0.0485  max open 2  closed 5  censored 0  trades/week 0.19  P(T1) 0.600  E[R] +0.825 [-0.081, +1.728]

Baselines
  no_trade  ROI +0.000  max DD 0.000  E[R] +0.000  trades 0
  random_tide  seeds 20  mean ROI +0.150  min -0.148  median +0.144  max +0.581  share ROI >= strategy 0.10  mean DD 0.117  mean E[R] +0.273  share E[R] >= strategy 0.10

Censored (1; closed_part_r is not in E[R])
  BNBUSDT dir -1  2026-06-27T08:00:00+00:00  open_share 0.60  mark_r +2.004  end_of_period  closed_part_r +0.773
