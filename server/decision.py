"""
decision.py
Adaptive Decision Engine for the Network-Aware Adaptive IoT Communication System.

Inputs (per reading):
    rttMs, lossPct, rssi, throughputKbps, currentInterval, currentPayloadSize

Outputs (per reading):
    state          -> "Good" | "Moderate" | "Poor"   (human-readable label)
    interval       -> next transmission interval in milliseconds
    packetSize     -> next payload size in bytes
    dataRateHint   -> "low" | "normal" | "high" (informational)
    confidence     -> 0.0 .. 1.0 (samples held in current state / saturation)
    score          -> 0.0 .. 1.0 (composite network-health score)
    rttSmoothed    -> smoothed RTT in ms (EWMA)
    jitterMs       -> smoothed RTT mean-deviation in ms (Jacobson RTTVAR)

Methodology
-----------
1. **EWMA smoothing** on each input metric (RTT, loss, RSSI) so the engine
   reacts to the *link* rather than a single noisy reading. Same family of
   estimator that TCP uses for RTT (Jacobson/Karels), tuned to alpha = 0.50
   for a snappy demo response on our 1-8 s sampling cadence.

2. **Jitter** is computed as an EWMA of |new_rtt - smoothed_rtt| (Jacobson
   RTTVAR), and contributes to the composite score. High variance is an
   early-warning indicator of congestion; loss usually follows it.

3. **Composite score** = weighted sum of normalised goodness for
       loss (0.40) + RTT (0.25) + jitter (0.15) + RSSI (0.20)
   on the *smoothed* values. Higher is better; bounded [0, 1].

4. **State label** is the classification of the score with fixed cut-points
   (0.65 / 0.45). It uses **hysteresis** (N consecutive agreeing samples
   before switching) so the dashboard badge does not flap.

5. **Strict factor-of-2 AIMD** — every tick adjusts (interval, payload) by
   exactly a factor of 2:
       score >= 0.65 -> interval halves, payload doubles  (push more data)
       score <= 0.45 -> interval doubles, payload halves  (back off cleanly)
       in between    -> hold
   The interval cap is kept tight (MAX_INTERVAL = 8 s) so the user-visible
   "freshness" never collapses to tens of seconds even under heavy congestion;
   the bulk of the back-off is absorbed by payload shrinkage, which directly
   reduces airtime contention without sacrificing transmission cadence.

6. **Online self-calibration of thresholds** — the engine maintains a rolling
   window of the last HISTORY_SIZE smoothed samples of each metric. Once it
   has at least MIN_SAMPLES_FOR_LEARN observations, the GOOD edges of RTT
   and jitter are recomputed every tick as the 25th percentile of their
   respective windows (with sanity caps at 0.5x and 3x the hardcoded default).
   Loss, RSSI, and every POOR edge stay hardcoded — they are objective
   bounds the engine must never normalise away. So the engine self-calibrates
   to whatever the link delivers (LAN, WiFi, cellular hotspot) while still
   flagging objectively bad conditions. Same principle as production
   anomaly-detection systems (best-observed baseline + interquartile range);
   no training step, no external library, no GPU.

The engine is side-effect-free (no I/O, no globals), so it stays unit-
testable and could be swapped for an ML estimator without touching app.py.
"""

from __future__ import annotations

from collections import deque
from typing import Dict, Optional


class AdaptiveDecisionEngine:
    # ---- Parameter bounds (MUST match the ESP32 firmware) ----
    # Interval cap is intentionally tight: even under sustained congestion
    # the device never stops reporting for more than 8 seconds. Back-off
    # is primarily absorbed by payload shrinkage rather than rate collapse.
    MIN_INTERVAL = 1000
    MAX_INTERVAL = 8000
    MIN_PAYLOAD  = 32
    MAX_PAYLOAD  = 1024

    # ---- Goodness thresholds for each metric (good edge -> poor edge) ----
    # Tuned for phone-hotspot deployment: cellular backhaul commonly adds
    # 200-500 ms baseline RTT, which is not actually congestion. Tightening
    # these thresholds was making the engine call a healthy hotspot "Moderate"
    # forever and never allowing payload recovery.
    RTT_GOOD_MS    = 200.0
    RTT_POOR_MS    = 800.0
    RSSI_GOOD_DBM  = -60
    RSSI_POOR_DBM  = -85
    LOSS_GOOD_PCT  = 1.0
    LOSS_POOR_PCT  = 10.0
    JITTER_GOOD_MS = 50.0
    JITTER_POOR_MS = 500.0

    # ---- Composite-score weights (must sum to 1.0) ----
    W_LOSS   = 0.40
    W_RTT    = 0.25
    W_JITTER = 0.15
    W_RSSI   = 0.20

    # ---- State classification cut-points & hysteresis (badge only) ----
    SCORE_GOOD             = 0.65
    SCORE_MODERATE         = 0.45
    MIN_SAMPLES_FOR_SWITCH = 2
    SATURATION_SAMPLES     = 5

    # ---- AIMD trigger thresholds ----
    # Aligned with the state cut-points so the badge and AIMD agree.
    AIMD_INCREASE_ABOVE = 0.65
    AIMD_DECREASE_BELOW = 0.45

    # ---- AIMD step rule: strict factor-of-2 in both directions ----
    # Every "good" tick:  interval >>= 1,  payload <<= 1
    # Every "bad"  tick:  interval <<= 1,  payload >>= 1
    # No fractional steps. Within tight interval bounds, the AIMD saturates
    # in only 3 ticks each way, giving a snappy response that preserves
    # transmission cadence and lets payload absorb most of the back-off.
    AIMD_FACTOR = 2

    # ---- EWMA smoothing constants ----
    ALPHA_METRIC = 0.50   # weight for newest sample of rtt/loss/rssi
    BETA_JITTER  = 0.25   # standard TCP value for RTTVAR

    # ---- Online adaptive-threshold learning ----
    # Maintain a rolling window of the last HISTORY_SIZE smoothed samples of
    # each metric. Once we have at least MIN_SAMPLES_FOR_LEARN observations,
    # the "good" / "poor" edges of every metric are recomputed every tick
    # from the distribution of that window (see _adaptive_thresholds for the
    # exact rule). The hard-coded defaults above are only the cold-start
    # fallback. No training step, no external library, no GPU.
    HISTORY_SIZE          = 100
    MIN_SAMPLES_FOR_LEARN = 20

    def __init__(self) -> None:
        self.state: str = "Moderate"
        self.samples_in_state: int = 0
        self._proposed_history: deque[str] = deque(maxlen=self.MIN_SAMPLES_FOR_SWITCH)

        # EWMA accumulators — initialised lazily on the first reading.
        self._rtt_ewma:    Optional[float] = None
        self._loss_ewma:   Optional[float] = None
        self._rssi_ewma:   Optional[float] = None
        self._jitter_ewma: Optional[float] = None

        # AIMD-controlled parameters — start mid-range so factor-of-2 steps
        # can grow or shrink from a neutral starting point.
        self._current_interval: int = 2000
        self._current_payload:  int = 256

        # Rolling histories of smoothed metrics used to learn the goodness
        # thresholds online (percentile-based; see _learned_threshold).
        self._rtt_history:    deque[float] = deque(maxlen=self.HISTORY_SIZE)
        self._loss_history:   deque[float] = deque(maxlen=self.HISTORY_SIZE)
        self._jitter_history: deque[float] = deque(maxlen=self.HISTORY_SIZE)
        self._rssi_history:   deque[float] = deque(maxlen=self.HISTORY_SIZE)

    # ------------------------------------------------------------------
    # EWMA update (Jacobson-style for both mean and deviation)
    # ------------------------------------------------------------------
    def _update_ewma(self, reading: Dict) -> None:
        rtt  = float(reading["rttMs"])
        loss = float(reading["lossPct"])
        rssi = float(reading["rssi"])

        if self._rtt_ewma is None:
            # First-ever sample: seed every smoother.
            self._rtt_ewma    = rtt
            self._loss_ewma   = loss
            self._rssi_ewma   = rssi
            self._jitter_ewma = rtt / 2.0   # Jacobson initialisation
            return

        # Jitter must be updated BEFORE rtt_ewma so it uses the previous mean.
        deviation = abs(rtt - self._rtt_ewma)
        self._jitter_ewma = ((1 - self.BETA_JITTER) * self._jitter_ewma
                             + self.BETA_JITTER * deviation)

        a = self.ALPHA_METRIC
        self._rtt_ewma  = (1 - a) * self._rtt_ewma  + a * rtt
        self._loss_ewma = (1 - a) * self._loss_ewma + a * loss
        self._rssi_ewma = (1 - a) * self._rssi_ewma + a * rssi

    # ------------------------------------------------------------------
    # Normalisation helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _norm_lower_is_better(value: float, good: float, poor: float) -> float:
        """RTT / loss / jitter: clamp to [0, 1] where good -> 1.0, poor -> 0.0."""
        if poor == good:
            return 0.5
        if value <= good:
            return 1.0
        if value >= poor:
            return 0.0
        return 1.0 - (value - good) / (poor - good)

    @staticmethod
    def _norm_higher_is_better(value: float, good: float, poor: float) -> float:
        """RSSI: clamp to [0, 1] where good -> 1.0, poor -> 0.0."""
        if good == poor:
            return 0.5
        if value >= good:
            return 1.0
        if value <= poor:
            return 0.0
        return (value - poor) / (good - poor)

    # ------------------------------------------------------------------
    # Online learning of metric thresholds
    # ------------------------------------------------------------------
    def _record_history(self) -> None:
        """Append the latest smoothed metric values to the learning windows."""
        if self._rtt_ewma is None:
            return
        self._rtt_history.append(self._rtt_ewma)
        self._loss_history.append(self._loss_ewma)
        self._jitter_history.append(self._jitter_ewma)
        self._rssi_history.append(self._rssi_ewma)

    @staticmethod
    def _percentile(history: deque, pct: float) -> float:
        """Nearest-rank percentile of `history` (no numpy dependency)."""
        s = sorted(history)
        idx = max(0, min(len(s) - 1, int(round(len(s) * pct / 100.0)) - 1))
        return s[idx]

    def _adaptive_thresholds(self) -> Dict[str, float]:
        """
        Online-learned goodness thresholds.

        Principle: **only RTT and jitter are learned, and only their good edges.**
        Loss and RSSI stay on the hardcoded values because they are *objective*
        measures of link health -- 30% loss is bad on any link, anywhere; -85 dBm
        is weak on any radio. Letting those drift causes the engine to normalise
        to a degraded link and stop flagging it (classic relative-threshold
        failure mode).

        For RTT/jitter the GOOD edge is the 25th percentile of recent history
        (typical-good for this link), clamped so it cannot get stricter than
        half the default nor more lenient than 3x the default. The POOR edge
        is always the hardcoded value -- our objective "this is bad" threshold
        cannot be argued away by the link itself.
        """
        if len(self._rtt_history) < self.MIN_SAMPLES_FOR_LEARN:
            return {
                "rtt_good":    self.RTT_GOOD_MS,
                "rtt_poor":    self.RTT_POOR_MS,
                "loss_good":   self.LOSS_GOOD_PCT,
                "loss_poor":   self.LOSS_POOR_PCT,
                "jitter_good": self.JITTER_GOOD_MS,
                "jitter_poor": self.JITTER_POOR_MS,
                "rssi_good":   self.RSSI_GOOD_DBM,
                "rssi_poor":   self.RSSI_POOR_DBM,
            }

        def learn_good(history, default_good, default_poor):
            """Learn the GOOD edge for an RTT/jitter-style metric (lower-is-better)."""
            learned = self._percentile(history, 25)
            lo  = default_good * 0.5
            hi  = min(default_good * 3.0, default_poor * 0.75)
            return max(lo, min(hi, learned))

        rtt_good    = learn_good(self._rtt_history,    self.RTT_GOOD_MS,    self.RTT_POOR_MS)
        jitter_good = learn_good(self._jitter_history, self.JITTER_GOOD_MS, self.JITTER_POOR_MS)

        return {
            "rtt_good":    rtt_good,
            "rtt_poor":    self.RTT_POOR_MS,        # objective: hardcoded
            "loss_good":   self.LOSS_GOOD_PCT,      # objective: hardcoded
            "loss_poor":   self.LOSS_POOR_PCT,      # objective: hardcoded
            "jitter_good": jitter_good,
            "jitter_poor": self.JITTER_POOR_MS,     # objective: hardcoded
            "rssi_good":   self.RSSI_GOOD_DBM,      # physical: hardcoded
            "rssi_poor":   self.RSSI_POOR_DBM,      # physical: hardcoded
        }

    # ------------------------------------------------------------------
    # Composite score on smoothed metrics + jitter (with learned thresholds)
    # ------------------------------------------------------------------
    def _composite_score(self) -> float:
        t = self._adaptive_thresholds()
        loss_s   = self._norm_lower_is_better(self._loss_ewma,   t["loss_good"],   t["loss_poor"])
        rtt_s    = self._norm_lower_is_better(self._rtt_ewma,    t["rtt_good"],    t["rtt_poor"])
        jitter_s = self._norm_lower_is_better(self._jitter_ewma, t["jitter_good"], t["jitter_poor"])
        rssi_s   = self._norm_higher_is_better(self._rssi_ewma,  t["rssi_good"],   t["rssi_poor"])
        return (self.W_LOSS   * loss_s
              + self.W_RTT    * rtt_s
              + self.W_JITTER * jitter_s
              + self.W_RSSI   * rssi_s)

    # ------------------------------------------------------------------
    # State label with hysteresis (drives the dashboard badge only)
    # ------------------------------------------------------------------
    def _classify(self, score: float) -> str:
        if score >= self.SCORE_GOOD:
            return "Good"
        if score >= self.SCORE_MODERATE:
            return "Moderate"
        return "Poor"

    def _update_state_label(self, score: float) -> None:
        proposed = self._classify(score)
        self._proposed_history.append(proposed)

        if proposed != self.state:
            recent = list(self._proposed_history)
            consistent = (
                len(recent) >= self.MIN_SAMPLES_FOR_SWITCH
                and all(s == proposed for s in recent[-self.MIN_SAMPLES_FOR_SWITCH:])
            )
            if consistent:
                self.state = proposed
                self.samples_in_state = 1
            # else: keep previous label
        else:
            self.samples_in_state += 1

    # ------------------------------------------------------------------
    # Strict factor-of-2 AIMD on (interval, payload)
    # ------------------------------------------------------------------
    def _aimd_step(self, score: float) -> None:
        if score >= self.AIMD_INCREASE_ABOVE:
            # Good conditions -> send more, send more often.
            self._current_interval //= self.AIMD_FACTOR
            self._current_payload  *= self.AIMD_FACTOR

        elif score <= self.AIMD_DECREASE_BELOW:
            # Congestion -> halve the offered load on both axes.
            self._current_interval *= self.AIMD_FACTOR
            self._current_payload  //= self.AIMD_FACTOR
        # else: hold

        # Always clamp.
        self._current_interval = max(self.MIN_INTERVAL,
                                     min(self.MAX_INTERVAL, self._current_interval))
        self._current_payload  = max(self.MIN_PAYLOAD,
                                     min(self.MAX_PAYLOAD,  self._current_payload))

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    def decide(self, reading: Dict) -> Dict:
        self._update_ewma(reading)
        self._record_history()             # feed the learning windows
        score = self._composite_score()
        self._update_state_label(score)
        self._aimd_step(score)

        confidence = min(1.0, self.samples_in_state / float(self.SATURATION_SAMPLES))
        hint = {"Good": "high", "Moderate": "normal", "Poor": "low"}.get(self.state, "normal")

        thresholds = self._adaptive_thresholds()
        warm = len(self._rtt_history) >= self.MIN_SAMPLES_FOR_LEARN

        return {
            "state":        self.state,
            "score":        round(score, 3),
            "interval":     self._current_interval,
            "packetSize":   self._current_payload,
            "dataRateHint": hint,
            "confidence":   round(confidence, 2),
            "rttSmoothed":  round(self._rtt_ewma, 1)    if self._rtt_ewma    is not None else None,
            "jitterMs":     round(self._jitter_ewma, 1) if self._jitter_ewma is not None else None,
            "learning": {
                "warm":            warm,
                "samples":         len(self._rtt_history),
                "samplesNeeded":   self.MIN_SAMPLES_FOR_LEARN,
                "rttGoodMs":       round(thresholds["rtt_good"], 1),
                "rttPoorMs":       round(thresholds["rtt_poor"], 1),
                "jitterGoodMs":    round(thresholds["jitter_good"], 1),
                "jitterPoorMs":    round(thresholds["jitter_poor"], 1),
                "lossGoodPct":     round(thresholds["loss_good"], 2),
                "lossPoorPct":     round(thresholds["loss_poor"], 2),
                "rssiGoodDbm":     round(thresholds["rssi_good"], 1),
                "rssiPoorDbm":     round(thresholds["rssi_poor"], 1),
            },
        }


# ----------------------------------------------------------------------------
# Smoke test (run `python decision.py` to verify behavior without the server)
# ----------------------------------------------------------------------------
if __name__ == "__main__":
    eng = AdaptiveDecisionEngine()
    scenarios = [
        ("ideal",     {"rttMs": 40,  "lossPct": 0.0,  "rssi": -55}),
        ("moderate",  {"rttMs": 120, "lossPct": 3.0,  "rssi": -70}),
        ("poor",      {"rttMs": 350, "lossPct": 18.0, "rssi": -88}),
        ("recovery",  {"rttMs": 50,  "lossPct": 0.5,  "rssi": -58}),
    ]
    for label, s in scenarios:
        # Feed each scenario several times so EWMA / hysteresis can settle.
        for _ in range(8):
            out = eng.decide(s)
        print(f"{label:9s} -> "
              f"state={out['state']:8s}  score={out['score']:.2f}  "
              f"interval={out['interval']:5d}ms  payload={out['packetSize']:4d}B  "
              f"smoothedRTT={out['rttSmoothed']:.0f}ms  jitter={out['jitterMs']:.0f}ms")
