# Phase 5 Validation Checklist — Required Gates Before Live Consideration

**Status:** Phase 5 was declared "Production Ready" prematurely by Anti-Gravity. This checklist documents the required validation that MUST be completed before Phase 6 (Live trading) can begin.

**Reference:** `AI_IMPLEMENTATION_MASTER_PLAN.md` Section 9.6 (Phase 5) and Section 11 (Validation Matrix)

---

## Current State (as of 2026-08-02)

| Component | Status | Notes |
|---|---|---|
| Phase 1 (Foundation) | ✅ Verified | Config, protections, baseline backtest, lookahead/recursive analysis |
| Phase 2 (Risk Stack) | ✅ Verified | Leverage, custom_stoploss, custom_stake_amount, confirm_trade_entry |
| Phase 3 (FreqAI) | ⚠️ Partial | LightGBM integrated but config deviates from master plan (DEFECT-11) |
| Phase 4 (Confidence Engine) | ❌ DEFECTIVE | 10 critical defects identified — see `PHASE_4_5_AUDIT_REPORT.md` |
| Phase 5 (Validation) | ❌ SKIPPED | No walk-forward, no Monte Carlo, no CPCV, no L7 KPI measurement |

---

## Required Actions Before Live

### Priority 1 — Fix Confidence Engine (BLOCKING)

**Status:** Fix files provided in `/home/z/my-project/download/`

| File | Action |
|---|---|
| `confidence_engine.py` | Replace `strategies/confidence_engine.py` with corrected version |
| `FuturesTrendStrategy.py` | Replace `strategies/FuturesTrendStrategy.py` with corrected version |
| `config.demo.json` | Replace `config/config.demo.json` with corrected version (FreqAI block) |

**Verification:**
1. Run `freqtrade test-strategy --strategy CTFuturesTrendStrategy --config config/config.demo.json` — must exit 0
2. Verify the new modules import correctly:
   ```bash
   python -c "from confidence_engine import (
       MultiSignalConfidenceCombiner, IsotonicCalibrator,
       ConfidenceIntervalCalculator, EVComputer, TradeFilter,
       UnifiedDriftDetector, CalibrationValidator, RiskScorer
   ); print('OK')"
   ```
3. Run a 30-day backtest and verify:
   - Trade count drops vs Phase 4 (confidence engine should REJECT more trades, not fewer)
   - Profit factor improves vs Phase 4
   - `confidence_observations` table is being populated

### Priority 2 — Run Phase 5 Validation (BLOCKING)

**Tool required:** `titouannwtt/freqtrade-ultimate` (drop-in fork with walk-forward, Monte Carlo, CPCV)

**Install:**
```bash
cd /home/rai/Projects/claude-trade
git clone https://github.com/titouannwtt/freqtrade-ultimate.git ../freqtrade-ultimate
cd ../freqtrade-ultimate
./setup.sh -i
# Point it at your existing user_data/ and tradesv3.sqlite
```

#### 2.1 Walk-Forward Analysis
```bash
freqtrade walk-forward \
    --strategy CTFuturesTrendStrategy \
    --config config/config.backtest.json \
    --mode anchored \
    --timerange 20250801-20260731
```

**Required KPIs (all must be green):**
- [ ] WF-K1: IS/OOS ratio 3:1–4:1
- [ ] WF-K2: OOS Sharpe > 1.0
- [ ] WF-K3: OOS profit factor > 1.3
- [ ] WF-K4: OOS max DD < 15%
- [ ] WF-K5: IS vs OOS Sharpe degradation < 50%
- [ ] WF-K6: ≥6 windows analyzed
- [ ] WF-K7: Positive window rate > 70%

#### 2.2 Monte Carlo Simulation
```bash
freqtrade monte-carlo \
    --strategy CTFuturesTrendStrategy \
    --config config/config.backtest.json \
    --simulations 1000
```

**Required KPIs:**
- [ ] MC-K1: 1000 simulations completed
- [ ] MC-K2: 5th-percentile max DD < 15%
- [ ] MC-K3: 5th-percentile profit factor > 1.3
- [ ] MC-K4: 5th-percentile final equity > starting equity
- [ ] MC-K5: Probability of ruin = 0%
- [ ] MC-K6: 95th-percentile max DD < 30%

#### 2.3 CPCV (Combinatorial Purged Cross-Validation)
```bash
freqtrade walk-forward \
    --strategy CTFuturesTrendStrategy \
    --config config/config.backtest.json \
    --mode cpcv
```

**Required KPIs:**
- [ ] CV-K1: PBO < 0.10
- [ ] CV-K2: Verdict A or B
- [ ] CV-K3: ≥100 combinatorial paths tested
- [ ] CV-K4: Path Sharpe std-dev < 0.5
- [ ] CV-K5: Path profit factor std-dev < 0.4

#### 2.4 NFI Benchmark Comparison
```bash
# Copy NFI X6 strategy file to strategies/
# Set is_futures_mode = True, futures_mode_leverage = 5.0
STRATEGY_LIST="CTFuturesTrendStrategy NostalgiaForInfinityX6" ./scripts/backtest.sh
```

**Required KPI:**
- [ ] L3-K8: CTFutures Sharpe not significantly worse than NFI (difference < 0.3)

#### 2.5 Hyperopt with WalkForwardLoss
```bash
freqtrade hyperopt \
    --strategy CTFuturesTrendStrategy \
    --hyperopt-loss WalkForwardLoss \
    --spaces roi stoploss trailing protection \
    --epochs 500 \
    --enable-protections \
    --timeframe-detail 1m \
    --timerange 20250801-20260731 \
    --random-state 42
```

**Required KPIs:**
- [ ] HO-K1: ≥500 epochs run
- [ ] HO-K3: Best epoch Sharpe > 1.0
- [ ] HO-K4: Best epoch max DD < 15%
- [ ] HO-K5: `--enable-protections` flag set
- [ ] HO-K7: IS vs OOS Sharpe ratio: OOS > 50% of IS

### Priority 3 — Verify L7 KPIs (BLOCKING)

**Tool:** `validate_phase5.py` (provided in `/home/z/my-project/download/`)

**Copy to project:**
```bash
cp /home/z/my-project/download/validate_phase5.py /home/rai/Projects/claude-trade/
```

**Run after a 30+ day backtest:**
```bash
cd /home/rai/Projects/claude-trade
python validate_phase5.py --backtest-result user_data/backtest_results/<latest>.json
# OR
python validate_phase5.py --db user_data/tradesv3.sqlite
```

**Required KPIs (all 22 must be green for 7 consecutive days):**

#### Critical Anti-Clustering KPIs (the user's original concern)

| KPI | Metric | Target | Why it matters |
|---|---|---|---|
| **L7-K6** | Confidence std-dev | > 0.10 | **THE anti-clustering test.** If std-dev < 0.05, predictions are clustered at 45–55% — the original problem is NOT fixed. |
| **L7-K7** | Confidence 90th percentile | > 0.65 | Verifies high-confidence predictions exist. If < 0.55, model is too cautious. |
| **L7-K8** | Confidence 10th percentile | < 0.40 | Verifies low-confidence predictions exist. If > 0.50, no discrimination. |
| **L7-K14** | Win rate Q4 vs Q1 | Q4 > Q1 + 5pp | **The ultimate test:** does confidence actually predict outcomes? If Q4 ≤ Q1, confidence is uninformative. |

#### Calibration Quality KPIs

| KPI | Metric | Target |
|---|---|---|
| L7-K1 | Post-cal ECE per regime | < 0.08 |
| L7-K2 | Post-cal Brier per regime | < 0.20 |
| L7-K3 | Reliability diagram deviation | < 5% per bucket |
| L7-K4 | CI coverage | > 80% |
| L7-K5 | CI width mean | 0.10–0.25 |

#### Predictive Power KPIs

| KPI | Metric | Target |
|---|---|---|
| L7-K9 | Confidence ↔ win correlation | > 0.20 |
| L7-K10 | EV ↔ PnL correlation | > 0.30 |
| L7-K11 | Mean EV of accepted trades | > $0.50 |
| L7-K15 | PF Q4 vs Q1 | Q4 > Q1 + 0.3 |
| L7-K22 | EV − PnL over 50 trades | < $0.30 |

#### Operational KPIs

| KPI | Metric | Target |
|---|---|---|
| L7-K12 | Rejection rate (overall) | 60–85% |
| L7-K13 | Per-gate rejection rate | < 30% per gate |
| L7-K16 | Calibrator refit frequency | every 20 obs |
| L7-K17 | Calibrator is_fit per regime | True for all active |
| L7-K18 | Feature drift PSI mean | < 0.10 |
| L7-K19 | Prediction drift PSI | < 0.08 |
| L7-K20 | Label drift PSI | < 0.10 |
| L7-K21 | Calibration trend (ECE over 24h) | flat or decreasing |

### Priority 4 — Extended Dry-Run (BLOCKING)

**Requirement:** 4–6 weeks of dry-run trading with daily KPI monitoring.

**Setup:**
```bash
cd /home/rai/Projects/claude-trade
./venv/bin/freqtrade trade \
    --strategy CTFuturesTrendStrategy \
    --freqaimodel LightGBMClassifier \
    -c config/config.demo.json \
    --dry-run
```

**Daily checks:**
- [ ] Bot running without crashes (L14-K1)
- [ ] Telegram alerts received for any threshold breaches (L14-K2, L14-K7)
- [ ] FreqUI accessible (L14-K1)
- [ ] Daily report: trade count, win rate, profit factor, max DD
- [ ] L7 KPIs green (run `validate_phase5.py --db user_data/tradesv3.sqlite` daily)
- [ ] Dry-run vs. backtest divergence < 30% for the same period

**Weekly checks:**
- [ ] Walk-forward OOS Sharpe > 1.0 (WF-K2)
- [ ] Calibration trend flat or decreasing (L7-K21)
- [ ] No drift PSI > 0.10 sustained for >2 hours (L7-K18)
- [ ] Champion/Challenger comparison (if Phase 4+ promoted)

**4–6 week gate:**
- [ ] All L7 KPIs green for 7 consecutive days (the final gate)
- [ ] Dry-run profitable (profit factor > 1.3)
- [ ] Dry-run max DD < 10%
- [ ] User's explicit go/no-go decision recorded

---

## Phase 6 (Live) Prerequisites Checklist

Before starting Phase 6 (Live trading with reduced $500 capital), ALL of the following must be true:

- [ ] Priority 1 complete: Confidence engine defects fixed and verified
- [ ] Priority 2 complete: Walk-forward, Monte Carlo, CPCV, NFI benchmark, hyperopt all green
- [ ] Priority 3 complete: All 22 L7 KPIs green for 7 consecutive days
- [ ] Priority 4 complete: 4–6 weeks of profitable dry-run with <30% divergence from backtest
- [ ] User's explicit go/no-go decision recorded
- [ ] Live Binance Futures API keys configured in `config.live.json` (NOT `config.demo.json`)
- [ ] `config.live.json` has: `dry_run: false`, `max_open_trades: 4`, `stake_amount` cap $20, leverage cap 3x
- [ ] Starting capital: $500 (reduced from $1000 target)
- [ ] Telegram alerts configured and tested

---

## What to Do If Validation Fails

### If L7-K6 fails (confidence std-dev < 0.05 — clustering collapse)

This means the multiplicative gating fix is not working. Investigate:
1. Verify `MultiSignalConfidenceCombiner.combine()` is being called (not the old additive combiner)
2. Check that the calibrator is actually fitting (L7-K17: `is_fit` should be True)
3. Look at the raw ML probability distribution — if it's already clustered at 0.50, the model itself is the problem (not the combiner)
4. Consider switching from `LightGBMRegressor` to `LightGBMClassifier` (3-class direction) — the regressor's continuous z-score output is harder to calibrate than a classifier's probability

### If L7-K14 fails (Q4 win rate ≤ Q1 — confidence is uninformative)

This means confidence doesn't predict outcomes. Investigate:
1. Check feature importance — are the features actually predictive?
2. Run `freqtrade recursive-analysis` — is there lookahead bias?
3. Check the label generation in `set_freqai_targets` — is the forward window appropriate?
4. Consider that the strategy may not have a real edge — no amount of confidence engineering can fix a strategy with no alpha

### If walk-forward OOS Sharpe < 0.5 (WF-K2 fails)

This means severe overfitting. Investigate:
1. Reduce `n_estimators` from 1000 to 500
2. Increase `min_data_in_leaf` from 50 to 100
3. Increase `noise_standard_deviation` from 0.05 to 0.10
4. Reduce feature count (drop redundant features per L5-K5)
5. Use `WalkForwardLoss` for hyperopt (not `SharpeHyperOptLoss`)

### If Monte Carlo 5th-percentile max DD > 15% (MC-K2 fails)

This means position sizing is too aggressive. Investigate:
1. Reduce `max_open_trades` from 4 to 2
2. Reduce `risk_per_trade_pct` from 1% to 0.5%
3. Reduce leverage cap from 5x to 3x
4. Tighten the portfolio notional cap from 3× to 2× equity

### If CPCV PBO > 0.10 (CV-K1 fails)

This means hyperopt overfit. Investigate:
1. Reduce hyperopt epochs from 500 to 200
2. Use `PlateauSampler` instead of TPE
3. Simplify the feature set (drop ~30% of features)
4. Accept that the strategy may not be robust enough for live

---

## Estimated Timeline

| Step | Duration | Cumulative |
|---|---|---|
| Apply Priority 1 fixes | 1 day | Day 1 |
| Run 30-day backtest with fixed confidence engine | 1 day | Day 2 |
| Run L7 KPI validation | 1 hour | Day 2 |
| Install freqtrade-ultimate | 1 day | Day 3 |
| Run walk-forward analysis | 1 day | Day 4 |
| Run Monte Carlo simulation | 0.5 day | Day 4 |
| Run CPCV | 0.5 day | Day 5 |
| Run NFI benchmark | 0.5 day | Day 5 |
| Run hyperopt with WalkForwardLoss | 2 days | Day 7 |
| Apply best hyperopt params + rerun validation | 1 day | Day 8 |
| Start extended dry-run | Day 8 | — |
| Dry-run duration | 4–6 weeks | Week 6–8 |
| L7 KPI 7-day green gate | 7 days (within dry-run) | Week 7–8 |
| **Earliest Phase 6 (Live) start** | — | **Week 8–9** |

**If any gate fails, add 1–2 weeks for investigation and re-validation.**

---

## Final Reminder

The user's original concern was predictions clustering at 45–55%. The Phase 4 implementation did NOT fix this — it used additive averaging (the exact failure mode the master plan was designed to prevent) and an unfitted calibrator (a sigmoid stretch disguised as calibration).

The corrected `confidence_engine.py` provided in `/home/z/my-project/download/` implements the master plan's design correctly:
- **Multiplicative gating** (not additive averaging)
- **Auto-fitting calibrator** with observation recording
- **Per-regime calibration**
- **Lower-bound EV** (conservative)
- **Drift detection** and **calibration validation**

But correctness of implementation does not guarantee correctness of outcome. The L7 KPIs must be MEASURED, not assumed. Run `validate_phase5.py` and verify L7-K6 (std-dev > 0.10) and L7-K14 (Q4 > Q1 + 5pp) before even considering Live deployment.

**No backtest is a prediction. No "Production Ready" declaration is valid without the validation gates being green.**

---

**End of checklist.**
