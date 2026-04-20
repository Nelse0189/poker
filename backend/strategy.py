"""Strategy helpers for closer-to-pro NLHE recommendations.

This module provides:

* **Preflop charts** (6-max) for RFI, facing an open, and facing a 3-bet.
  These are solver-inspired heuristics, not perfect GTO, but they are
  instant to evaluate and far closer to professional play than equity
  vs. random alone.
* **Villain range inference** from position + facing action so users
  don't have to hand-craft ranges.
* **Made-hand & draw classification** used to enrich reasoning and to
  tune sizing/implied-odds on postflop streets.

All inputs use pokerkit's 2-char card notation (e.g. ``AhKs``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pokerkit import Card, parse_range

Position = Literal["UTG", "MP", "CO", "BTN", "SB", "BB"]
FacingAction = Literal[
    "none",          # hero opens the action (RFI / open-raise spot)
    "limp",          # limped to hero
    "open_raise",    # hero faces a single open raise
    "three_bet",     # hero's open was 3-bet
    "four_bet",      # hero's 3-bet was 4-bet
    "check",         # postflop: checked to hero
    "bet",           # postflop: villain bet into hero
    "raise",         # postflop: villain raised
]

POSITIONS: tuple[Position, ...] = ("UTG", "MP", "CO", "BTN", "SB", "BB")


# ---------- Preflop chart ranges (6-max) -------------------------------------
# Ranges are expressed in pokerkit notation. Values are solver-inspired
# approximations; enough to recommend sensible lines, not to replicate a
# perfect equilibrium.

RFI_RANGES: dict[Position, str] = {
    "UTG": "55+,ATs+,KTs+,QTs+,JTs,T9s,98s,AJo+,KQo",
    "MP":  "33+,A9s+,KTs+,QTs+,J9s+,T9s,98s,87s,ATo+,KJo+,QJo",
    "CO":  "22+,A2s+,K7s+,Q8s+,J8s+,T8s+,97s+,86s+,75s+,65s,54s,A9o+,KTo+,QTo+,JTo",
    "BTN": "22+,A2s+,K2s+,Q5s+,J7s+,T7s+,96s+,85s+,74s+,64s+,53s+,43s,A2o+,K8o+,Q9o+,J9o+,T9o,98o,87o,76o",
    "SB":  "22+,A2s+,K6s+,Q8s+,J8s+,T8s+,97s+,86s+,76s,65s,54s,A8o+,KTo+,QTo+,JTo",
    # BB "RFI" n/a; handled separately when no raise is in front.
    "BB":  "22+,A2s+,K2s+,Q2s+,J6s+,T6s+,96s+,85s+,74s+,63s+,53s+,43s,A2o+,K8o+,Q9o+,J9o+,T9o,98o,87o",
}

# Value 3-bet ranges when facing an open from a given position.
THREE_BET_VALUE: dict[Position, str] = {
    "UTG": "QQ+,AKs,AKo",
    "MP":  "QQ+,AKs,AKo",
    "CO":  "JJ+,AQs+,AKo",
    "BTN": "TT+,AQs+,AKo",
    "SB":  "TT+,AQs+,AKo",
    "BB":  "JJ+,AQs+,AKo",
}

# Light 3-bet / bluff component (used for mixing and to widen hero's range).
THREE_BET_BLUFF: dict[Position, str] = {
    "UTG": "A5s",
    "MP":  "A5s,A4s",
    "CO":  "A5s-A2s,KJs",
    "BTN": "A5s-A2s,KJs,QJs",
    "SB":  "A5s-A2s,KJs,QJs",
    "BB":  "A5s-A2s,KJs,QJs,JTs,T9s",
}

# Call (flat) vs. an open.
CALL_VS_OPEN: dict[Position, str] = {
    "UTG": "99-JJ,AQs,AJs,KQs",
    "MP":  "77-JJ,AQs,AJs,ATs,KQs,KJs,QJs",
    "CO":  "55-TT,AQs,AJs,ATs,KQs,KJs,KTs,QJs,JTs",
    "BTN": "22-TT,AQs-ATs,A9s,A5s-A2s,KQs-KTs,QJs,QTs,JTs,T9s,98s,87s,AJo,KQo",
    "SB":  "77-TT,AQs,AJs,ATs,KQs,KJs,QJs",
    "BB":  # BB defend vs open is very wide; this is a rough heuristic.
           "22-JJ,A2s+,K7s+,Q8s+,J8s+,T8s+,97s+,86s+,75s+,65s,54s,ATo+,KTo+,QTo+,JTo,T9o,98o",
}

# 4-bet-for-value range when hero's 3-bet gets 4-bet.
FOUR_BET_VALUE = "KK+,AKs"


# Assumed villain ranges (for postflop MC) by their action line.
VILLAIN_OPEN_RANGES = RFI_RANGES  # reuse opener RFI by position
VILLAIN_3BET_RANGES: dict[Position, str] = {
    p: f"{THREE_BET_VALUE[p]},{THREE_BET_BLUFF[p]}" for p in POSITIONS
}
# Generic wide / tight fallbacks.
VILLAIN_LIMP_RANGE = "22-JJ,A2s+,K7s+,Q9s+,J9s+,T9s,98s,87s,76s,ATo+,KTo+,QJo,JTo"
VILLAIN_ANY_RANGE = "22+,A2+,K2+,Q2+,J2+,T2+,92+,82+,72+,62+,52+,42+,32"


# ---------- Hand / range utilities -------------------------------------------

def _canonical_hand(cards: str) -> frozenset[Card]:
    return frozenset(Card.parse(cards))


def hero_in_range(hero_cards: str, range_str: str) -> bool:
    if not range_str:
        return False
    try:
        rng = parse_range(range_str)
    except Exception:
        return False
    return _canonical_hand(hero_cards) in rng


def infer_villain_range(
    hero_position: Position,
    facing_action: FacingAction,
    aggressor_position: Position | None,
) -> str:
    """Map (hero position, villain's action) to a realistic villain range."""
    if facing_action in ("open_raise", "bet", "raise") and aggressor_position is not None:
        return VILLAIN_OPEN_RANGES.get(aggressor_position, VILLAIN_ANY_RANGE)
    if facing_action == "three_bet" and aggressor_position is not None:
        return VILLAIN_3BET_RANGES.get(aggressor_position, "QQ+,AKs,AKo")
    if facing_action == "four_bet":
        return "KK+,AKs"
    if facing_action == "limp":
        return VILLAIN_LIMP_RANGE
    if facing_action in ("check", "none"):
        # Mostly used preflop limp-pots or post-flop checks; fall back to
        # a wide-but-not-infinite range.
        return VILLAIN_LIMP_RANGE
    return VILLAIN_ANY_RANGE


# ---------- Preflop chart decision -------------------------------------------

PreflopPlay = Literal[
    "fold", "call", "open_raise", "three_bet", "four_bet", "five_bet_shove",
]


@dataclass
class PreflopDecision:
    play: PreflopPlay
    sizing_bb: float | None
    reasons: list[str] = field(default_factory=list)
    confidence: Literal["high", "mixed", "low"] = "high"


def _pos_gap(opener: Position, hero: Position) -> int:
    """Positive if hero acts after opener in the same orbit."""
    order = list(POSITIONS)
    return order.index(hero) - order.index(opener)


def preflop_decision(
    hero_cards: str,
    hero_position: Position,
    facing_action: FacingAction,
    aggressor_position: Position | None,
    pot_bb: float,
    to_call_bb: float,
    effective_stack_bb: float,
) -> PreflopDecision:
    """Chart-based preflop recommendation in 6-max NLHE (100bb default assumption)."""
    reasons: list[str] = []

    if facing_action in ("none", "limp"):
        rfi = RFI_RANGES.get(hero_position, "")
        if hero_in_range(hero_cards, rfi):
            reasons.append(
                f"{hero_position}: hand is inside the RFI opening range → open-raise."
            )
            size = 2.5 if hero_position != "SB" else 3.0
            return PreflopDecision("open_raise", size, reasons)
        reasons.append(
            f"{hero_position}: hand is outside the RFI opening range → fold."
        )
        return PreflopDecision("fold", None, reasons)

    if facing_action == "open_raise":
        if not aggressor_position:
            aggressor_position = "CO"
        value = THREE_BET_VALUE.get(hero_position, "")
        bluff = THREE_BET_BLUFF.get(hero_position, "")
        call_rng = CALL_VS_OPEN.get(hero_position, "")

        if hero_in_range(hero_cards, value):
            reasons.append(
                f"vs {aggressor_position} open: in value 3-bet range → 3-bet for value."
            )
            size = to_call_bb * 3.0 if hero_position != "BB" else to_call_bb * 3.5
            return PreflopDecision("three_bet", round(size, 2), reasons)
        if hero_in_range(hero_cards, bluff):
            reasons.append(
                f"vs {aggressor_position} open: in 3-bet bluff range → mix 3-bet (~30%)."
            )
            size = to_call_bb * 3.0
            return PreflopDecision("three_bet", round(size, 2), reasons, "mixed")
        if hero_in_range(hero_cards, call_rng):
            reasons.append(
                f"vs {aggressor_position} open: in flat-call range → call."
            )
            return PreflopDecision("call", to_call_bb, reasons)
        reasons.append(
            f"vs {aggressor_position} open: not in call/3-bet range → fold."
        )
        return PreflopDecision("fold", None, reasons)

    if facing_action == "three_bet":
        # Hero opened, villain 3-bet. Use 4-bet-value + strong calls.
        if hero_in_range(hero_cards, FOUR_BET_VALUE):
            reasons.append("Facing a 3-bet with a premium hand → 4-bet for value.")
            size = to_call_bb * 2.3
            if size >= effective_stack_bb * 0.45:
                return PreflopDecision(
                    "five_bet_shove", effective_stack_bb, reasons
                )
            return PreflopDecision("four_bet", round(size, 2), reasons)
        strong_call = "QQ,JJ,AKo,AQs,AJs,KQs"
        if hero_in_range(hero_cards, strong_call):
            reasons.append("Facing a 3-bet with a strong hand → call to see flop.")
            return PreflopDecision("call", to_call_bb, reasons, "mixed")
        reasons.append("Facing a 3-bet without a 4-bet/strong-call hand → fold.")
        return PreflopDecision("fold", None, reasons)

    if facing_action == "four_bet":
        if hero_in_range(hero_cards, FOUR_BET_VALUE):
            reasons.append("Facing a 4-bet with KK+/AKs → 5-bet shove.")
            return PreflopDecision("five_bet_shove", effective_stack_bb, reasons)
        reasons.append("Facing a 4-bet without KK+ → fold.")
        return PreflopDecision("fold", None, reasons)

    return PreflopDecision("fold", None, ["No preflop chart match; folding."], "low")


# ---------- Postflop hand classification & draws -----------------------------

MadeHandKind = Literal[
    "high_card", "pair_low", "pair_mid", "top_pair", "overpair",
    "two_pair", "trips", "straight", "flush", "full_house", "quads",
    "straight_flush",
]

RANK_ORDER = "23456789TJQKA"


def _rank_value(r: str) -> int:
    return RANK_ORDER.index(r) + 2


@dataclass
class HandSummary:
    kind: MadeHandKind
    kicker_rank: str | None
    has_flush_draw: bool
    has_straight_draw: bool  # OESD (8 outs)
    has_gutshot: bool        # 4 outs
    outs_to_improve: int


def _rank_counts(ranks: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in ranks:
        counts[r] = counts.get(r, 0) + 1
    return counts


def _suit_counts(suits: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for s in suits:
        counts[s] = counts.get(s, 0) + 1
    return counts


def _has_straight(values: list[int]) -> bool:
    v = sorted(set(values))
    if 14 in v:
        v = [1, *v]
    v.sort()
    run = 1
    for i in range(1, len(v)):
        if v[i] == v[i - 1] + 1:
            run += 1
            if run >= 5:
                return True
        elif v[i] != v[i - 1]:
            run = 1
    return False


def _straight_draw_kind(values: list[int]) -> tuple[bool, bool]:
    """Return (oesd, gutshot) ignoring completed straights."""
    if _has_straight(values):
        return False, False
    uniq = sorted(set(values))
    if 14 in uniq:
        uniq = [1, *uniq]
    oesd = False
    gut = False
    for lo in range(1, 11):
        window = [lo + i for i in range(5)]
        present = sum(1 for x in window if x in uniq)
        if present == 4:
            # distinguish oesd vs gutshot by which card is missing
            missing = [x for x in window if x not in uniq][0]
            if missing == window[0] or missing == window[-1]:
                # could be either; treat as gutshot unless we have the
                # open-ended shape (4 consecutive). Check consecutiveness.
                consec = [x for x in uniq if x in window]
                consec.sort()
                if len(consec) == 4 and consec[-1] - consec[0] == 3:
                    oesd = True
                else:
                    gut = True
            else:
                gut = True
    return oesd, gut


def classify_hand(hero_cards: str, board_cards: str) -> HandSummary:
    all_cards = hero_cards + board_cards
    ranks = [all_cards[i] for i in range(0, len(all_cards), 2)]
    suits = [all_cards[i] for i in range(1, len(all_cards), 2)]

    hero_ranks = [hero_cards[0], hero_cards[2]] if len(hero_cards) >= 4 else []
    board_ranks = (
        [board_cards[i] for i in range(0, len(board_cards), 2)] if board_cards else []
    )

    rcount = _rank_counts(ranks)
    scount = _suit_counts(suits)
    values = [_rank_value(r) for r in ranks]

    pair_hero_board = set(hero_ranks) & set(board_ranks)
    pocket_pair = len(hero_ranks) == 2 and hero_ranks[0] == hero_ranks[1]
    top_board = max((_rank_value(r) for r in board_ranks), default=0)

    kind: MadeHandKind = "high_card"
    kicker_rank: str | None = None

    trips_ranks = [r for r, c in rcount.items() if c >= 3]
    pairs_ranks = [r for r, c in rcount.items() if c == 2]
    quads_ranks = [r for r, c in rcount.items() if c >= 4]

    has_flush = any(c >= 5 for c in scount.values())
    has_straight_now = _has_straight(values)

    if quads_ranks:
        kind = "quads"
    elif trips_ranks and pairs_ranks:
        kind = "full_house"
    elif has_flush and has_straight_now:
        kind = "straight_flush"
    elif has_flush:
        kind = "flush"
    elif has_straight_now:
        kind = "straight"
    elif trips_ranks:
        kind = "trips"
    elif len(pairs_ranks) >= 2:
        kind = "two_pair"
    elif pair_hero_board:
        paired_rank = next(iter(pair_hero_board))
        if _rank_value(paired_rank) == top_board:
            kind = "top_pair"
            kicker_rank = [r for r in hero_ranks if r != paired_rank][0]
        elif _rank_value(paired_rank) >= 10:
            kind = "pair_mid"
        else:
            kind = "pair_low"
    elif pocket_pair:
        hr = hero_ranks[0]
        if _rank_value(hr) > top_board:
            kind = "overpair"
        elif _rank_value(hr) >= 10:
            kind = "pair_mid"
        else:
            kind = "pair_low"

    # Draws (only meaningful if we don't already have the made hand).
    flush_draw = any(c == 4 for c in scount.values()) and not has_flush
    oesd, gut = _straight_draw_kind(values)

    outs = 0
    if flush_draw:
        outs += 9
    if oesd:
        outs += 8
    elif gut:
        outs += 4
    # Overcards to board (only preflop pairs matter less here; rough heuristic).
    hero_val = [_rank_value(r) for r in hero_ranks]
    overcards = sum(1 for v in hero_val if v > top_board) if kind == "high_card" else 0
    if kind == "high_card" and not flush_draw and not oesd and not gut:
        outs += 3 * overcards  # approximate

    return HandSummary(
        kind=kind,
        kicker_rank=kicker_rank,
        has_flush_draw=flush_draw,
        has_straight_draw=oesd,
        has_gutshot=gut and not oesd,
        outs_to_improve=outs,
    )
