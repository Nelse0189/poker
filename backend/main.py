"""FastAPI backend for the Poker Optimal Play app.

Decisions blend three layers to get closer to professional play:

1. **Preflop charts** by position and facing action (instant).
2. **Postflop Monte Carlo equity** vs. an *inferred* villain range from
   position + action — not vs. "random" unless the user asks for it.
3. **Decision layer**: pot odds + SPR-aware sizing + fold-equity for
   raises + implied-odds bump for clear draws + made-hand awareness.
"""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

from pokerkit import (
    Card,
    Deck,
    StandardHighHand,
    calculate_equities,
    parse_range,
)

from strategy import (
    FacingAction,
    Position,
    POSITIONS,
    VILLAIN_ANY_RANGE,
    classify_hand,
    infer_villain_range,
    preflop_decision,
)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    executor = ProcessPoolExecutor(max_workers=2)
    app.state.executor = executor
    try:
        yield
    finally:
        executor.shutdown(wait=True)


app = FastAPI(title="Poker Optimal Play API", version="2.0.0", lifespan=_lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


Street = Literal["preflop", "flop", "turn", "river"]
Action = Literal[
    "fold", "check", "call", "bet", "raise", "all_in",
    "open_raise", "three_bet", "four_bet", "five_bet_shove",
]


class AnalyzeRequest(BaseModel):
    hero_cards: str = Field(..., min_length=4, max_length=4)
    board: str = Field("")
    num_opponents: int = Field(1, ge=1, le=8)
    opponent_range: str = Field(
        "auto",
        description=(
            "Villain range: 'auto' infers from position+action, 'random' "
            "uses any-two, or a custom pokerkit range like '22+,AJs+'."
        ),
    )
    pot: float = Field(..., ge=0)
    to_call: float = Field(0, ge=0)
    hero_stack: float = Field(..., gt=0)
    effective_stack: float | None = Field(
        None,
        description=(
            "Effective stack (usually min(hero_stack, villain_stack)). "
            "Used for SPR and implied odds. Defaults to hero_stack."
        ),
    )
    big_blind: float = Field(
        1.0, gt=0, description="Used to express chart sizing in bb."
    )
    hero_position: Position | None = None
    aggressor_position: Position | None = None
    facing_action: FacingAction = "none"
    sample_count: int = Field(1200, ge=200, le=20000)

    @field_validator("hero_cards", "board")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip().replace(" ", "")

    @field_validator("board")
    @classmethod
    def _valid_board(cls, v: str) -> str:
        if len(v) not in (0, 6, 8, 10):
            raise ValueError(
                "board must be empty, or exactly 3/4/5 cards (6/8/10 chars)"
            )
        return v


class AnalyzeResponse(BaseModel):
    street: Street
    equity: float
    win_probability: float
    pot_odds: float
    required_equity: float
    expected_value_call: float
    action: Action
    sizing: float | None
    reasoning: list[str]
    hand_class: str | None = None
    outs_to_improve: int | None = None
    spr: float | None = None
    villain_range_used: str | None = None
    source: Literal["preflop_chart", "postflop_mc"]
    confidence: Literal["high", "mixed", "low"] = "high"


def _street_from_board(board: str) -> Street:
    n = len(board) // 2
    return {0: "preflop", 3: "flop", 4: "turn", 5: "river"}[n]


def _parse_cards(cards: str) -> list[Card]:
    if not cards:
        return []
    return list(Card.parse(cards))


def _validate_card_uniqueness(hero: str, board: str) -> None:
    seen: set[str] = set()
    for chunk in (hero, board):
        for i in range(0, len(chunk), 2):
            c = chunk[i : i + 2]
            if c in seen:
                raise HTTPException(
                    status_code=400, detail=f"Duplicate card in input: {c}"
                )
            seen.add(c)


def _resolve_villain_range(
    opponent_range: str,
    hero_position: Position | None,
    facing_action: FacingAction,
    aggressor_position: Position | None,
) -> str:
    text = opponent_range.strip().lower()
    if text in {"random", "any", "*"}:
        return VILLAIN_ANY_RANGE
    if text == "auto" or text == "":
        if hero_position is None:
            return VILLAIN_ANY_RANGE
        return infer_villain_range(hero_position, facing_action, aggressor_position)
    return opponent_range


def _build_hole_ranges(
    hero_cards: str,
    num_opponents: int,
    opponent_range_str: str,
) -> list[Any]:
    hero_range = parse_range(hero_cards)
    try:
        opp_range = parse_range(opponent_range_str)
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid opponent range '{opponent_range_str}': {e}",
        )
    if not opp_range:
        raise HTTPException(
            status_code=400,
            detail=f"Opponent range '{opponent_range_str}' is empty.",
        )
    return [hero_range] + [opp_range] * num_opponents


def _calculate_equity_batch(
    hole_ranges: list[Any],
    board_cards: list[Card],
    sample_count: int,
    executor: ProcessPoolExecutor | None,
) -> float:
    try:
        equities = calculate_equities(
            hole_ranges,
            board_cards,
            2,
            5,
            Deck.STANDARD,
            (StandardHighHand,),
            sample_count=sample_count,
            executor=executor,
        )
    except Exception:
        equities = calculate_equities(
            hole_ranges,
            board_cards,
            2,
            5,
            Deck.STANDARD,
            (StandardHighHand,),
            sample_count=sample_count,
        )
    return float(equities[0])


_AMBIGUITY_MARGIN = 0.04
_RAISE_THRESHOLDS: dict[Street, float] = {
    "preflop": 0.60,
    "flop": 0.62,
    "turn": 0.65,
    "river": 0.68,
}


def _equity_needs_refinement(
    equity: float,
    to_call: float,
    pot: float,
    street: Street,
) -> bool:
    raise_thr = _RAISE_THRESHOLDS[street]
    pot_after_call = pot + to_call
    required_equity = to_call / pot_after_call if pot_after_call > 0 else 0.0
    if to_call == 0:
        return abs(equity - raise_thr) < _AMBIGUITY_MARGIN
    fold_line = required_equity - 0.02
    if abs(equity - fold_line) < _AMBIGUITY_MARGIN:
        return True
    if equity >= fold_line and abs(equity - raise_thr) < _AMBIGUITY_MARGIN:
        return True
    return False


def _run_equity_adaptive(
    hero_cards: str,
    board: str,
    num_opponents: int,
    opponent_range_str: str,
    sample_count: int,
    executor: ProcessPoolExecutor | None,
    to_call: float,
    pot: float,
    street: Street,
) -> float:
    hole_ranges = _build_hole_ranges(hero_cards, num_opponents, opponent_range_str)
    board_cards = _parse_cards(board)
    fast_cap = 750
    n1 = min(fast_cap, sample_count)
    e1 = _calculate_equity_batch(hole_ranges, board_cards, n1, executor)
    if n1 >= sample_count or not _equity_needs_refinement(e1, to_call, pot, street):
        return e1
    n2 = sample_count - n1
    e2 = _calculate_equity_batch(hole_ranges, board_cards, n2, executor)
    return (e1 * n1 + e2 * n2) / (n1 + n2)


# ---------- Postflop decision with SPR, fold equity, implied odds ------------


def _estimate_fold_equity(villain_range_tightness: str, bet_fraction_pot: float) -> float:
    """Rough guess: tighter villain ranges and bigger bets generate more folds."""
    base = 0.25 if villain_range_tightness == "wide" else 0.18
    base += min(0.35, bet_fraction_pot * 0.45)
    return max(0.05, min(0.70, base))


def _range_tightness(range_str: str) -> str:
    # Heuristic: count comma-separated tokens; more tokens ≈ wider.
    n = range_str.count(",")
    return "wide" if n >= 8 else "tight"


def _postflop_recommendation(
    hero_cards: str,
    board: str,
    equity: float,
    pot: float,
    to_call: float,
    hero_stack: float,
    effective_stack: float,
    street: Street,
    villain_range_str: str,
) -> tuple[Action, float | None, list[str], float, float, float, dict[str, Any]]:
    reasoning: list[str] = []
    pot_after_call = pot + to_call
    pot_odds = to_call / pot_after_call if pot_after_call > 0 else 0.0
    ev_call = equity * (pot + to_call) - (1 - equity) * to_call

    spr = effective_stack / pot if pot > 0 else float("inf")
    hand = classify_hand(hero_cards, board)

    reasoning.append(
        f"Equity vs inferred range ≈ {equity * 100:.1f}% "
        f"(range: {villain_range_str[:60]}{'…' if len(villain_range_str) > 60 else ''})."
    )
    reasoning.append(
        f"Hand: {hand.kind.replace('_', ' ')}"
        + (f" (kicker {hand.kicker_rank})" if hand.kicker_rank else "")
        + (", flush draw" if hand.has_flush_draw else "")
        + (", straight draw" if hand.has_straight_draw else "")
        + (", gutshot" if hand.has_gutshot else "")
        + f" · SPR {spr:.1f}."
    )

    # Implied-odds adjustment: with a clear draw and depth behind, we need
    # slightly less raw equity to profitably call because we win more when
    # we hit a hidden draw on later streets.
    implied_bump = 0.0
    if street in ("flop", "turn") and (hand.has_flush_draw or hand.has_straight_draw):
        if spr > 3:
            implied_bump = 0.04
        elif spr > 1.5:
            implied_bump = 0.02
    required_equity = max(0.0, pot_odds - implied_bump)

    if to_call == 0:
        reasoning.append("No bet to face.")
    else:
        reasoning.append(
            f"Pot odds need {pot_odds * 100:.1f}%; implied-odds adj {implied_bump * 100:.1f}% "
            f"→ threshold {required_equity * 100:.1f}%."
        )
        reasoning.append(
            f"EV of calling ≈ {ev_call:+.1f} "
            f"(equity × pot − (1−equity) × call)."
        )

    raise_thr = _RAISE_THRESHOLDS[street]
    extras: dict[str, Any] = {
        "hand_class": hand.kind,
        "outs_to_improve": hand.outs_to_improve,
        "spr": round(spr, 2) if spr != float("inf") else None,
    }

    # --- No bet to face -------------------------------------------------------
    if to_call == 0:
        # Value bet with strong made hand or strong draw (semi-bluff) at low SPR.
        strong_value = hand.kind in (
            "overpair", "top_pair", "two_pair", "trips", "straight",
            "flush", "full_house", "quads", "straight_flush",
        ) and equity >= raise_thr
        strong_semibluff = (
            (hand.has_flush_draw and hand.has_straight_draw)
            and spr < 4
        )
        if strong_value or strong_semibluff:
            size_frac = 0.66 if spr > 3 else 0.5
            size = round(min(pot * size_frac, hero_stack), 2)
            reasoning.append(
                f"{'Value' if strong_value else 'Semi-bluff'} bet ~{int(size_frac * 100)}% pot "
                f"({size:.0f})."
            )
            return "bet", size, reasoning, pot_odds, required_equity, ev_call, extras

        if hand.kind in ("pair_mid", "pair_low") and equity < 0.55 and spr > 2:
            reasoning.append("Marginal made hand, deep SPR → check for pot control.")
            return "check", None, reasoning, pot_odds, required_equity, ev_call, extras

        reasoning.append("Not strong enough to value-bet; check.")
        return "check", None, reasoning, pot_odds, required_equity, ev_call, extras

    # --- Facing a bet ---------------------------------------------------------

    # Fold if equity clearly short of threshold and no implied-odds rescue.
    if equity < required_equity - 0.02:
        reasoning.append("Equity below threshold → fold.")
        return "fold", None, reasoning, pot_odds, required_equity, ev_call, extras

    # Consider a raise with value OR strong semi-bluff + fold equity.
    tightness = _range_tightness(villain_range_str)
    bet_frac = to_call / pot if pot > 0 else 1.0
    fold_eq = _estimate_fold_equity(tightness, bet_frac)

    raise_size = round(min(to_call * 2.5 + pot * 0.5, hero_stack), 2)
    # EV of raise (simple model): villain folds → win pot; otherwise assume
    # we see showdown with current equity against their bet + raise called.
    raise_cost = raise_size - to_call  # extra chips beyond calling
    pot_if_called = pot + 2 * raise_size
    ev_raise = (
        fold_eq * (pot + to_call)  # they fold, we pick up pot + their bet
        + (1 - fold_eq) * (equity * pot_if_called - (1 - equity) * raise_size)
    )
    reasoning.append(
        f"Raise-to-{raise_size:.0f}: est. fold-equity {fold_eq * 100:.0f}%, "
        f"EV ≈ {ev_raise:+.1f} vs call EV {ev_call:+.1f}."
    )

    can_all_in = raise_size >= hero_stack * 0.95

    if equity >= raise_thr:
        if can_all_in:
            reasoning.append("Strong equity and stack pressure → all-in for value.")
            return "all_in", hero_stack, reasoning, pot_odds, required_equity, ev_call, extras
        reasoning.append("Strong equity → raise for value.")
        return "raise", raise_size, reasoning, pot_odds, required_equity, ev_call, extras

    semibluff = (
        hand.has_flush_draw or hand.has_straight_draw
    ) and spr > 1.5 and ev_raise > ev_call + 0.05 * pot
    if semibluff:
        reasoning.append("Semi-bluff raise +EV (fold equity + strong draw).")
        return "raise", raise_size, reasoning, pot_odds, required_equity, ev_call, extras

    # Otherwise, call if EV positive.
    if ev_call >= 0:
        reasoning.append("Equity clears threshold, raise not +EV → call.")
        return "call", to_call, reasoning, pot_odds, required_equity, ev_call, extras

    reasoning.append("EV of calling slightly negative → fold.")
    return "fold", None, reasoning, pot_odds, required_equity, ev_call, extras


# ---------- Routes -----------------------------------------------------------


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/positions")
def list_positions() -> dict[str, list[str]]:
    return {
        "positions": list(POSITIONS),
        "facing_actions": [
            "none", "limp", "open_raise", "three_bet", "four_bet",
            "check", "bet", "raise",
        ],
    }


def _chart_play_to_action(play: str) -> Action:
    return {
        "fold": "fold",
        "call": "call",
        "open_raise": "open_raise",
        "three_bet": "three_bet",
        "four_bet": "four_bet",
        "five_bet_shove": "five_bet_shove",
    }.get(play, "fold")  # type: ignore[return-value]


@app.post("/analyze", response_model=AnalyzeResponse)
def analyze(req: AnalyzeRequest, request: Request) -> AnalyzeResponse:
    if len(req.hero_cards) != 4:
        raise HTTPException(status_code=400, detail="hero_cards must be 4 chars (e.g. AhKs).")
    _validate_card_uniqueness(req.hero_cards, req.board)

    street = _street_from_board(req.board)
    eff_stack = req.effective_stack or req.hero_stack
    bb = req.big_blind

    # ---- Preflop: chart ----
    if street == "preflop" and req.hero_position is not None:
        decision = preflop_decision(
            hero_cards=req.hero_cards,
            hero_position=req.hero_position,
            facing_action=req.facing_action,
            aggressor_position=req.aggressor_position,
            pot_bb=req.pot / bb,
            to_call_bb=req.to_call / bb,
            effective_stack_bb=eff_stack / bb,
        )
        sizing_chips: float | None = None
        if decision.sizing_bb is not None:
            sizing_chips = round(min(decision.sizing_bb * bb, eff_stack), 2)

        # Pot odds context for UI even on chart plays.
        pot_after_call = req.pot + req.to_call
        pot_odds = req.to_call / pot_after_call if pot_after_call > 0 else 0.0

        return AnalyzeResponse(
            street=street,
            equity=0.0,
            win_probability=0.0,
            pot_odds=pot_odds,
            required_equity=pot_odds,
            expected_value_call=0.0,
            action=_chart_play_to_action(decision.play),
            sizing=sizing_chips,
            reasoning=decision.reasons,
            hand_class=None,
            outs_to_improve=None,
            spr=round(eff_stack / req.pot, 2) if req.pot > 0 else None,
            villain_range_used=None,
            source="preflop_chart",
            confidence=decision.confidence,
        )

    # ---- Postflop (or preflop without position): Monte Carlo + decision layer ----
    villain_range_str = _resolve_villain_range(
        req.opponent_range,
        req.hero_position,
        req.facing_action,
        req.aggressor_position,
    )

    executor = getattr(request.app.state, "executor", None)
    equity = _run_equity_adaptive(
        req.hero_cards,
        req.board,
        req.num_opponents,
        villain_range_str,
        req.sample_count,
        executor,
        req.to_call,
        req.pot,
        street,
    )

    if street == "preflop":
        # Fallback (no position supplied): legacy-style equity decision.
        action, sizing, reasoning, pot_odds, req_eq, ev_call, extras = _postflop_recommendation(
            hero_cards=req.hero_cards,
            board=req.board,
            equity=equity,
            pot=req.pot,
            to_call=req.to_call,
            hero_stack=req.hero_stack,
            effective_stack=eff_stack,
            street=street,
            villain_range_str=villain_range_str,
        )
    else:
        action, sizing, reasoning, pot_odds, req_eq, ev_call, extras = _postflop_recommendation(
            hero_cards=req.hero_cards,
            board=req.board,
            equity=equity,
            pot=req.pot,
            to_call=req.to_call,
            hero_stack=req.hero_stack,
            effective_stack=eff_stack,
            street=street,
            villain_range_str=villain_range_str,
        )

    return AnalyzeResponse(
        street=street,
        equity=equity,
        win_probability=equity,
        pot_odds=pot_odds,
        required_equity=req_eq,
        expected_value_call=ev_call,
        action=action,
        sizing=sizing,
        reasoning=reasoning,
        hand_class=extras.get("hand_class"),
        outs_to_improve=extras.get("outs_to_improve"),
        spr=extras.get("spr"),
        villain_range_used=villain_range_str,
        source="postflop_mc",
        confidence="high",
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
