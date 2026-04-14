"""
ems_epex.py  –  EPEX Hourly Analysis
=====================================
Pure Python (no AppDaemon / HA dependencies).

Provides enrich_hourly(): window stats + TP detection + band-based block labeling
for the hourly EPEX forecast.  Called every hour from ems_forecasts._publish_epex().

Block labels
------------
  LABEL     PRICE   TP       SCORE         MEANING
  negative  <0      –        –             negative price; absorb everything
  neutral   >0      no       –             gap / transition hour
  peak      >0      yes/peak >PEAK_MIN     strong price peak
  crest     >0      yes/peak ≤PEAK_MIN     moderate price peak
  dip       >0      yes/val  ≥TROUGH_MAX   moderate price valley
  trough    >0      yes/val  <TROUGH_MAX   deep price valley

Every TP that passes the amplitude filter gets a label (dip/trough or crest/peak).
The score only determines how extreme it is within its type.

TP detection
------------
An hour is a turning point when:
  1. Both neighbours are strictly lower (peak) or strictly higher (valley),
     with at least one strictly so (plateaus excluded).
  2. Amplitude > MIN_AMP of the window spread vs the nearest opposing TP.

Block expansion
---------------
Each TP gets a band based on its amplitude vs the nearest opposing TP:
  band = amplitude × BAND_MAX × spread
Large TP pairs (e.g. deep trough before a strong peak) get a wide band.
Small TP pairs (nearby, similar prices) get a narrow band.
Adjacent hours within the band receive the same label as the TP.
Remaining hours get 'neutral' (or 'negative' when price < 0).
"""

from ems_base import forecast_window_stats

# ── Constants ─────────────────────────────────────────────────────────────────

BAND_MAX = 0.15   # band fraction: band = amplitude × BAND_MAX × spread
                  # amplitude = abs(TP_score − nearest_opposing_TP_score)
                  # → narrow band for close TP pairs, wide for extreme pairs
MIN_AMP  = 0.20   # TP must differ > 20 % of spread from nearest opposing TP

TROUGH_MAX = 0.20  # valley TP: score below this → trough, above → dip
PEAK_MIN   = 0.80  # peak TP:   score above this → peak,   below → crest

# Price tier — numeric index for logical comparisons in automations / dashboards
LABEL_TIER = {
    "negative": 0,
    "trough":   1,
    "dip":      2,
    "neutral":  3,
    "crest":    4,
    "peak":     5,
}


# ── Public API ─────────────────────────────────────────────────────────────────

def enrich_hourly(prices: list, past_values: list = None) -> tuple:
    """
    Analyse EPEX hourly prices: window stats + TP detection + block labeling.

    Parameters
    ----------
    prices      : list[float]  forecast prices starting at the current hour
    past_values : list[float]  up to 5 historical prices preceding prices[0],
                               oldest-first.  Shifts the current hour off the
                               window boundary to prevent trivial-TP artefacts.

    Returns
    -------
    (entries, stats) where
      entries : list[dict]  {pct, is_tp, label} aligned with prices[];
                            neutral defaults for entries beyond the window.
      stats   : dict        {min, max, avg, median, horizon};
                            all-None when window < 12 hours.
    """
    _invalid = {"min": None, "max": None, "avg": None, "median": None, "horizon": None}
    n        = len(prices)
    _neutral = {"pct": 0.5, "is_tp": False, "label": "neutral", "tier": 3}

    if n == 0:
        return [], _invalid

    stats, _ = forecast_window_stats(prices, past_values=past_values)

    if stats["horizon"] is None:
        return [dict(_neutral) for _ in range(n)], stats

    past    = list(past_values) if past_values else []
    n_past  = len(past)
    analysis = past + prices[:stats["horizon"] - n_past]
    w        = len(analysis)

    spread = (stats["max"] - stats["min"]) or 1e-9
    pcts   = [max(0.001, round((v - stats["min"]) / spread, 3)) for v in analysis]

    is_tp, tp_types, amplitudes = _detect_tps(analysis, spread)
    labels                      = _expand_labels(analysis, is_tp, tp_types,
                                                 amplitudes, pcts, spread)

    result = []
    for i in range(n):
        fi = n_past + i
        if fi < w:
            lbl = labels[fi]
            result.append({"pct": pcts[fi], "is_tp": is_tp[fi],
                           "label": lbl, "tier": LABEL_TIER[lbl]})
        else:
            result.append(dict(_neutral))

    return result, stats


# ── Internal helpers ───────────────────────────────────────────────────────────

def _detect_tps(analysis: list, spread: float) -> tuple:
    """
    Return (is_tp, tp_types, amplitudes) for every hour in the analysis window.

    Rule 1: local extremum — both neighbours lower (peak) or higher (valley),
            with at least one strictly so.
    Rule 2: amplitude > MIN_AMP of spread vs the nearest opposing TP.

    tp_types   : "valley", "peak", or "" (not a TP).
    amplitudes : abs(TP_score − nearest_opposing_TP_score); 0.0 for non-TPs.
    """
    w          = len(analysis)
    is_tp      = [False] * w
    tp_types   = [""]    * w
    amplitudes = [0.0]   * w

    for i in range(1, w - 1):
        p    = analysis[i]
        prev = analysis[i - 1]
        nxt  = analysis[i + 1]
        if p >= prev and p >= nxt and (p > prev or p > nxt):
            is_tp[i] = True;  tp_types[i] = "peak"
        elif p <= prev and p <= nxt and (p < prev or p < nxt):
            is_tp[i] = True;  tp_types[i] = "valley"

    # Amplitude filter — also store best_amp for band sizing
    tp_order = [i for i in range(w) if is_tp[i]]
    for k, tp_i in enumerate(tp_order):
        opposing = "peak" if tp_types[tp_i] == "valley" else "valley"
        amp_left  = None
        amp_right = None
        for j in range(k - 1, -1, -1):
            if tp_types[tp_order[j]] == opposing:
                amp_left = abs(analysis[tp_i] - analysis[tp_order[j]]) / spread
                break
        for j in range(k + 1, len(tp_order)):
            if tp_types[tp_order[j]] == opposing:
                amp_right = abs(analysis[tp_i] - analysis[tp_order[j]]) / spread
                break
        # Beide beschikbare zijden moeten > MIN_AMP zijn.
        # Een ontbrekende zijde (rand van het window) telt niet mee als blokkade.
        amps = [a for a in (amp_left, amp_right) if a is not None]
        if not amps or max(amps) < MIN_AMP:
            is_tp[tp_i] = False;  tp_types[tp_i] = ""
        # Een lokaal maximum bij een negatieve prijs is ruis, geen strategische TP.
        # Alleen valley-TPs zijn zinvol in de negatieve zone (diepste punt).
        elif tp_types[tp_i] == "peak" and analysis[tp_i] < 0:
            is_tp[tp_i] = False;  tp_types[tp_i] = ""
        else:
            amplitudes[tp_i] = min(amps)

    return is_tp, tp_types, amplitudes


def _expand_labels(analysis: list, is_tp: list, tp_types: list,
                   amplitudes: list, pcts: list, spread: float) -> list:
    """
    Assign a label to every hour in the analysis window.

    Each TP hour gets a label from _classify_label() using its tp_type.
    The band is amplitude × BAND_MAX × spread, so extreme TP pairs (large
    amplitude) get a wider band than closely-spaced TP pairs.
    Adjacent hours within the band receive the same label as the TP.
    Remaining hours fall back to neutral / negative.
    """
    w      = len(analysis)
    labels = [""] * w

    for i in range(w):
        if not is_tp[i]:
            continue
        tp_price = analysis[i]
        tp_label = _classify_label(tp_price, pcts[i], True, tp_types[i])
        labels[i] = tp_label

        # Negatieve uren: label = price < 0, geen band-expansie nodig.
        # Alle omliggende uren krijgen hun eigen label via de fallback hieronder.
        if tp_label == "negative":
            continue

        band = amplitudes[i] * BAND_MAX * spread

        j = i - 1
        while j >= 0 and abs(analysis[j] - tp_price) <= band:
            if labels[j] == "":
                labels[j] = tp_label
            j -= 1

        j = i + 1
        while j < w and abs(analysis[j] - tp_price) <= band:
            if labels[j] == "":
                labels[j] = tp_label
            j += 1

    for i in range(w):
        if labels[i] == "":
            labels[i] = _classify_label(analysis[i], pcts[i], False)

    return labels


def _classify_label(price: float, score: float,
                    is_tp: bool, tp_type: str = "") -> str:
    """
    Assign a label from price level, TP type, and relative score.

    Every TP that passes the amplitude filter gets a meaningful label —
    the score only distinguishes how extreme it is within its type:

      valley TP → trough (score < TROUGH_MAX) or dip (score ≥ TROUGH_MAX)
      peak   TP → peak   (score > PEAK_MIN)   or crest (score ≤ PEAK_MIN)
      no TP     → neutral (or negative when price < 0)

    'price' is the TP price for TP hours, or the hour's own price for gaps.
    """
    if price < 0:
        return "negative"
    if not is_tp:
        return "neutral"
    if tp_type == "valley":
        return "trough" if score < TROUGH_MAX else "dip"
    if tp_type == "peak":
        return "peak" if score > PEAK_MIN else "crest"
    return "neutral"
