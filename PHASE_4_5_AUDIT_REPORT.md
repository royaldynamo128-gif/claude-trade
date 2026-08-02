# Phase 4/5 Audit Report — Critical Defects Identified

**Audit date:** 2026-08-02
**Audited project:** `claude-trade-main (1).zip` (uploaded 2026-08-02 12:38)
**Reference:** `AI_IMPLEMENTATION_MASTER_PLAN.md` Sections 6, 7, 9, 11
**Status:** **Phase 5 "Production Ready" declaration is PREMATURE. Multiple critical defects must be fixed before Live consideration.**

---

## Executive Summary

Anti-Gravity executed Phases 1–5 and produced phase reports claiming success. However, a code-level audit reveals that **Phase 4 (Confidence Engine) was implemented incorrectly** — it violates the master plan's most critical design principles, the same principles specifically designed to prevent the user's original problem of predictions clustering at 45–55%.

**The evidence that something is wrong:** Phase 4 made performance WORSE than Phase 3.

| Metric | Phase 3 (ML only) | Phase 4 (Confidence Engine) | Change |
|---|---|---|---|
| Total trades | 94 | 138 | +47% (more trades, not fewer) |
| Net loss | -4.885 USDT | -9.27 USDT | +90% worse |
| Max DD | 0.51% | 0.93% | +82% worse |
| Profit factor | 0.32 | not reported | — |

A correctly-implemented confidence engine should REDUCE trade count (by rejecting low-confidence trades) and IMPROVE profit factor (by accepting only high-EV trades). The opposite happened. This is the signature of an additive-averaging combiner that fails to discriminate — exactly the failure mode the master plan was designed to prevent.

**Phase 5 is incomplete:** The Phase 5 report claims "Production Ready" but NONE of the required Phase 5 validation was performed:
- ❌ No walk-forward analysis (WF-K1 through WF-K7)
- ❌ No Monte Carlo simulation (MC-K1 through MC-K6)
- ❌ No CPCV (CV-K1 through CV-K5)
- ❌ No NFI benchmark comparison (L3-K8)
- ❌ No hyperopt with WalkForwardLoss (HO-K1 through HO-K7)
- ❌ No 4–6 week dry-run (just verified dry-run starts)
- ❌ No L7 KPI measurements (confidence std-dev, ECE, Brier, quartile analysis)

---

## Critical Defects in `strategies/confidence_engine.py`

### DEFECT-1: Additive averaging instead of multiplicative gating (RC-1 NOT FIXED)

**Master plan requirement (Section 6.6, P1):**
> "Critical design choice: this is NOT a simple weighted average... the combiner uses a multiplicative gating structure... Why multiplicative, not additive: Multiplicative gating means a single strong disagreement can veto a trade. Additive averaging means disagreements cancel out and the score regresses to the mean."

**Actual implementation (line 70–77 of confidence_engine.py):**
```python
composite = (
    self.weights["ml_signal"] * ml_score        # 0.40
    + self.weights["trend_strength"] * adx_score # 0.20
    + self.weights["rsi_momentum"] * rsi_score   # 0.15
    + self.weights["volume_rvol"] * rvol_score   # 0.15
    + self.weights["bb_position"] * bb_score     # 0.10
)
```

**Impact:** This is EXACTLY the additive averaging that caused the original 45–55% clustering. When 5 signals disagree (which is most of the time on noisy crypto data), the weighted average regresses to ~0.50. This is the #1 root cause (RC-1) documented in the master plan Section 6.1.

**Fix:** Replace with multiplicative gating (see fixed `confidence_engine.py`).

---

### DEFECT-2: Calibrator never actually fits (RC-2 NOT FIXED)

**Master plan requirement (Section 6.4, invariant INV-8):**
> "Every consumer of `df['&-s-direction_prob']` MUST route through `IsotonicCalibrator.calibrate()` first. Direct consumption is a defect."

**Actual implementation:**
- `IsotonicCalibrator.fit()` exists but is NEVER CALLED anywhere in the strategy code.
- `calibrate()` falls back to a sigmoid stretch when not fit:
  ```python
  if self.is_fitted:
      calibrated = self.calibrator.predict(raw_scores.values)
  else:
      k = 6.0
      stretched = 1.0 / (1.0 + np.exp(-k * (raw_scores - 0.5)))
  ```
- The sigmoid with k=6.0 maps 0.5 → 0.5 (NO CHANGE at the center). It does not use historical outcomes at all.

**Impact:** The "calibration" is not calibration. It's a deterministic mathematical transformation that doesn't learn from data. A 0.55 raw score becomes 0.586; a 0.60 becomes 0.646. These numbers look calibrated but are completely disconnected from realized win rates.

**Fix:** Implement auto-fitting calibrator with observation recording (see fixed `confidence_engine.py`).

---

### DEFECT-3: No per-regime calibration (P7 VIOLATED)

**Master plan requirement (Section 6.4, P7):**
> "Per-regime calibration. Calibration is fit per-regime (TRENDING / RANGING / HIGH_VOLATILITY / etc.), not globally. A 0.60 confidence in TRENDING_STRONG might historically win 65%; the same 0.60 in HIGH_VOLATILITY might win 52%. Separate calibrators per regime."

**Actual implementation:** Two calibrators exist (`calibrator_long` and `calibrator_short`) but neither is per-regime. Both are global per-side.

**Fix:** Maintain a dict of calibrators keyed by `(side, regime)`.

---

### DEFECT-4: EVComputer uses point estimate, not lower bound (INV-9 VIOLATED)

**Master plan requirement (Section 6.8, P6, invariant INV-9):**
> "EV uses the lower bound of the confidence interval, not the point estimate... Every EV computation uses lower bound of confidence interval."

**Actual implementation (line 141–150):**
```python
def calculate_ev(win_prob, reward_pct=0.03, risk_pct=0.015):
    ev = (win_prob * reward_pct) - ((1.0 - win_prob) * risk_pct)
    return ev
```

`win_prob` is the point estimate (`calibrated_long_prob`), NOT the lower bound (`confidence_lower_long`).

**Impact:** The system is overconfident by design. A trade with calibrated probability 0.60 and lower bound 0.45 should be rejected (lower-bound EV is negative), but the current code accepts it (point-estimate EV is positive).

**Fix:** `EVComputer.calculate_ev()` must accept a `confidence_interval` object and use `.lower`.

---

### DEFECT-5: EVComputer uses fixed reward/risk, not ATR-scaled

**Master plan requirement (Section 6.8):**
> "reward_usd = expected_move_pct × notional × tp_ratio; risk_usd = stop_distance_pct × notional"

**Actual implementation:** Fixed `reward_pct=0.03` (3%) and `risk_pct=0.015` (1.5%) regardless of ATR, regime, or pair volatility.

**Impact:** A high-volatility pair with 5% ATR gets the same reward/risk assumptions as a low-vol pair with 0.5% ATR. This makes EV comparisons across pairs meaningless.

**Fix:** `EVComputer.calculate_ev()` must accept `atr` and `close` and compute `reward_pct = 1.5 × atr / close`, `risk_pct = 1.5 × atr / close` (matching the stoploss distance).

---

### DEFECT-6: Missing UnifiedDriftDetector module entirely

**Master plan requirement (Section 6.7):**
> "Module 4: UnifiedDriftDetector — PSI/KS monitoring on features, predictions, and labels."

**Actual implementation:** Not implemented at all. No drift detection exists.

**Impact:** The system has no way to detect when the model's predictions start drifting from reality. A model trained on a calm market will silently miscalibrate when volatility spikes, and nothing flags it.

**Fix:** Implement `UnifiedDriftDetector` with PSI on feature/prediction/label distributions.

---

### DEFECT-7: Missing CalibrationValidator module entirely

**Master plan requirement (Section 6.10):**
> "Module 7: CalibrationValidator — continuous Brier score, ECE, reliability diagram, log-loss monitoring."

**Actual implementation:** Not implemented. No Brier, no ECE, no reliability diagram, no log-loss, no AUC-ROC, no confidence distribution stats.

**Impact:** The Phase 4 report claims "Resolution of 45–60% Clustering Collapse" but provides ZERO EVIDENCE. L7-K6 (confidence std-dev > 0.10) was never measured. L7-K1 (ECE < 0.08) was never measured. L7-K14 (Q4 win rate > Q1 by 5pp) was never measured. The claim is unverified and likely false given the additive-averaging defect.

**Fix:** Implement `CalibrationValidator` that computes all L7 metrics hourly.

---

### DEFECT-8: Missing TradeFilter module (7 gates not implemented)

**Master plan requirement (Section 6.9):**
> "Module 6: TradeFilter — Apply the 7 confidence/risk gates."

The 7 gates:
- G1: `do_predict == 1`
- G2: `DI_values < 1.0`
- G3: `confidence_interval.width <= 0.40`
- G4: `ev_usd >= min_ev_usd` ($0.20)
- G5: `risk_score < 0.80`
- G6: regime not in {NEWS_DRIVEN, CHOPPY}
- G7: ML direction matches rule direction

**Actual implementation:** Gating is inline in `populate_entry_trend` and only implements G1, G2, and a partial G4 (EV > 0.002 instead of $0.20). G3, G5, G6, G7 are missing.

**Fix:** Implement `TradeFilter` module with all 7 gates.

---

### DEFECT-9: No confidence_observations recording (INV-13 VIOLATED)

**Master plan requirement (invariant INV-13):**
> "Every closed trade records (predicted_conf, actual_outcome) to calibration DB."

**Actual implementation:** No recording logic exists. Closed trades are not feeding back into the calibrator.

**Impact:** The calibrator can never fit because it never receives observations. This compounds DEFECT-2.

**Fix:** Add a `record_observation()` call in `confirm_trade_exit` or trade-close hook.

---

### DEFECT-10: Wilson score interval is wrong for this use case

**Master plan requirement (Section 6.5):**
> "Interval width sources: (1) Base calibration error — historical Brier score at this probability bucket. (2) Drift state — PSI from detector. (3) Sample size penalty. (4) DI value."

**Actual implementation (line 119–131):**
```python
@staticmethod
def compute_bounds(p, n=50, z=1.96):
    denominator = 1.0 + (z**2) / n
    center = (p + (z**2) / (2 * n)) / denominator
    spread = z * np.sqrt((p * (1.0 - p) + (z**2) / (4 * n)) / n) / denominator
    ...
```

This is the Wilson score interval for a binomial proportion — appropriate for computing confidence intervals of an observed proportion, NOT for computing prediction uncertainty. It uses a fixed `n=50` which doesn't reflect actual sample size, calibration error, drift state, or DI.

**Fix:** Replace with the master plan's interval calculator that derives width from calibration error + drift + sample size + DI.

---

## Critical Defects in `config/config.demo.json` (FreqAI block)

### DEFECT-11: FreqAI config deviates from master plan in 7 ways

| Parameter | Master plan (§5.2) | Actual | Impact |
|---|---|---|---|
| `train_period_days` | 30 | 10 | Model trained on 1/3 the data → underfit |
| `backtest_period_days` | 7 | 5 | Slightly shorter OOS window |
| `live_retrain_hours` | 1 | 0 | **NO live retraining!** Model never refreshes |
| `use_svm_to_remove_outliers` | true | false | Outliers pollute training |
| `noise_standard_deviation` | 0.05 | not set | No regularization |
| `n_estimators` | 1000 | 300 | 70% fewer trees → underfit |
| `learning_rate` | 0.01 | 0.03 | 3× faster → overfits |
| Model class | LightGBMClassifier | LightGBMRegressor | Regressor predicts continuous z-score, not 3-class direction |

**Most critical:** `live_retrain_hours: 0` means the model NEVER retrains in live mode. The master plan explicitly required hourly retraining to track distribution drift. Without it, the model goes stale within days.

**Fix:** Update config to match master plan Section 5.2 exactly.

---

## Critical Defects in Phase 5 Validation

### DEFECT-12: Phase 5 skipped ALL required validation

The master plan Section 9.6 (Phase 5) requires:

| Required | Status | Impact |
|---|---|---|
| Walk-forward analysis (6+ windows, 12 months) | ❌ Not done | No evidence system works OOS |
| Monte Carlo simulation (1000 runs) | ❌ Not done | No drawdown distribution |
| CPCV (PBO score, A–F verdict) | ❌ Not done | No overfitting check |
| NFI benchmark comparison | ❌ Not done | No reference baseline |
| Hyperopt with WalkForwardLoss (500 epochs) | ❌ Not done | Parameters un-optimized |
| 4–6 week dry-run | ❌ Not done (just verified start) | No live behavior evidence |
| L7 KPI measurements (22 metrics) | ❌ Not done | Confidence engine unvalidated |

**Impact:** The Phase 5 report's "Production Ready" declaration is unsupported by evidence. The system could be running on pure overfit noise.

---

## Performance Decline Analysis: Why Phase 4 is worse than Phase 3

The performance decline from Phase 3 to Phase 4 is the direct consequence of DEFECT-1 (additive averaging):

1. **Phase 3** used raw ML predictions (`&-target`) as a filter. The ML model's regression output is at least informed by training data, even if uncalibrated. Trade count dropped to 94 (from 314) because the ML filter rejected most low-conviction signals.

2. **Phase 4** added the "confidence engine" on top. But because the combiner uses additive averaging, the composite score clusters around 0.50 (the mathematical consequence of averaging 5 noisy signals). The 0.55 threshold then accepts trades that the raw ML filter would have rejected — increasing trade count to 138 while accepting lower-quality signals.

3. **The "calibration" is a sigmoid stretch**, not real calibration. It doesn't use historical outcomes. A 0.50 composite becomes 0.50 (sigmoid maps 0.5 → 0.5). A 0.55 becomes 0.586. These look like calibrated probabilities but are deterministic transformations disconnected from reality.

4. **The EV gate is meaningless** because it uses the point estimate of an uncalibrated probability with fixed reward/risk assumptions. Every trade that passes the 0.55 threshold also passes the EV > 0.002 gate (because 0.55 × 0.03 − 0.45 × 0.015 = 0.00975 > 0.002).

**Net effect:** Phase 4 added complexity without value. It accepts more trades of lower quality and loses more money. This is the exact opposite of what a confidence engine should do.

---

## Required Fixes (Priority Order)

### Priority 1 — Fix the confidence engine (blocking)

1. **Rewrite `confidence_engine.py`** with:
   - Multiplicative gating in `MultiSignalConfidenceCombiner` (fixes DEFECT-1)
   - Auto-fitting `IsotonicCalibrator` with observation recording (fixes DEFECT-2, DEFECT-9)
   - Per-regime calibrators (fixes DEFECT-3)
   - `EVComputer` using lower bound of interval + ATR-scaled reward/risk (fixes DEFECT-4, DEFECT-5)
   - `UnifiedDriftDetector` module (fixes DEFECT-6)
   - `CalibrationValidator` module (fixes DEFECT-7)
   - `TradeFilter` module with 7 gates (fixes DEFECT-8)
   - `ConfidenceIntervalCalculator` based on calibration error + drift + DI (fixes DEFECT-10)

2. **Update `config.demo.json`** FreqAI block to match master plan (fixes DEFECT-11).

3. **Update `FuturesTrendStrategy.py`** to wire the new confidence engine properly:
   - Call `record_observation()` on every closed trade
   - Use `TradeFilter` for entry gating
   - Use lower-bound EV in `custom_stake_amount`
   - Run `CalibrationValidator` hourly in `bot_loop_start`

### Priority 2 — Run Phase 5 validation (blocking before Live)

4. **Install `freqtrade-ultimate`** for walk-forward/Monte Carlo/CPCV.
5. **Run walk-forward analysis** (6+ windows, 12 months).
6. **Run Monte Carlo simulation** (1000 runs).
7. **Run CPCV** (PBO score).
8. **Run NFI benchmark backtest** for comparison.
9. **Run hyperopt with `WalkForwardLoss`** (500 epochs).
10. **Start 4–6 week extended dry-run** with all L7 KPIs monitored.

### Priority 3 — Verify L7 KPIs (blocking before Live)

11. **Measure L7-K6** (confidence std-dev > 0.10) — the anti-clustering test.
12. **Measure L7-K1** (ECE < 0.08 per regime).
13. **Measure L7-K14** (Q4 win rate > Q1 by 5pp).
14. **Measure L7-K10** (EV vs. PnL correlation > 0.30).
15. **All 22 L7 KPIs green for 7 consecutive days** before Phase 6 consideration.

---

## Files Provided in This Audit

The following files are provided in `/home/z/my-project/download/` to fix the defects:

1. **`PHASE_4_5_AUDIT_REPORT.md`** — this document.
2. **`confidence_engine.py`** — rewritten confidence engine with all Priority 1 fixes.
3. **`config.demo.json`** — updated FreqAI config matching master plan.
4. **`FuturesTrendStrategy.py`** — updated strategy wiring the new confidence engine.
5. **`validate_phase5.py`** — script to measure L7 KPIs from backtest results.
6. **`PHASE_5_VALIDATION_CHECKLIST.md`** — required gates before Live consideration.

---

## Recommendation

**Do NOT proceed to Phase 6 (Live trading) until:**

1. All Priority 1 fixes are applied and verified.
2. All Priority 2 validation is run and passes.
3. All Priority 3 L7 KPIs are green for 7 consecutive days.
4. The system demonstrates positive expectancy in walk-forward OOS testing (Sharpe > 1.0, profit factor > 1.3, max DD < 15%).

The current state — Phase 4 with additive averaging and an unfit calibrator — is **worse than Phase 3** (raw ML filter without confidence engine). If deployed live as-is, the system will lose money faster than the Phase 3 baseline.

The user's original concern ("predictions stayed around 45–55%") has NOT been resolved. The Phase 4 report's claim is unverified and the code evidence suggests it is false.

---

**End of audit report.**
