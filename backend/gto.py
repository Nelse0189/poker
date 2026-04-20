"""Heads-up single-street CFR+ solver for NLHE subgames.

This is a *real* counterfactual-regret solver — converges toward Nash
equilibrium for the subgame it models. It is intentionally small:

Action abstraction (per street):
    - when no outstanding bet: {check, bet 66% pot}
    - when facing a bet:       {fold, call, raise to 2.5×}
    - at most 3 raises per street; then forced call/fold.
    - stack-limited (all-in is absorbed into "raise" when capped).

Future streets are abstracted as equity runout: at the end of the
current street's betting we resolve the remaining board with Monte-Carlo
showdown equity vs. the specific villain combo. This misses multi-street
bluffs but correctly models bet/raise/bluff-catch frequencies **on the
current street**, which is what users actually need to see.

Solver:
    * External-sampling MCCFR: sample one villain combo per traversal.
    * CFR+ (positive regret matching + linear averaging).
    * Progress reported via a callback so the API can stream updates.
"""

from __future__ import annotations

import random
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

from pokerkit import (
    Card,
    Deck,
    StandardHighHand,
    calculate_equities,
    parse_range,
)


Action = str  # 'check', 'bet', 'fold', 'call', 'raise'
ACTIONS_NO_BET: tuple[Action, ...] = ("check", "bet")
ACTIONS_VS_BET: tuple[Action, ...] = ("fold", "call", "raise")

MAX_RAISES_PER_STREET = 3
BET_FRACTION_OF_POT = 0.66
RAISE_MULTIPLIER = 2.5


@dataclass
class SubgameConfig:
    hero_hand: str            # e.g. "AhKs"
    board: str                # 0/3/4/5 card string (no spaces)
    villain_range: str        # pokerkit range notation
    pot: float                # starting pot at root
    to_call: float            # hero faces this (0 if villain checked)
    hero_stack: float         # remaining stack behind
    villain_stack: float      # remaining stack behind
    hero_first_to_act: bool   # true if no bet outstanding & hero first
    equity_samples: int = 400  # MC per (hero, villain) matchup for showdown
    equity_cache: dict[str, float] = field(default_factory=dict, repr=False)


@dataclass
class NodeKey:
    """Abstract history string.

    For the traverser (hero), hands are implicit (they have one).
    For the opponent, we include their sampled hand so separate hands
    get separate strategies (external-sampling MCCFR).
    """
    player: str          # 'hero' or 'villain'
    history: tuple[str, ...]
    extra: str = ""      # villain hand label, for villain infosets

    def key(self) -> str:
        h = ",".join(self.history)
        return f"{self.player}|{self.extra}|{h}"


@dataclass
class CFRNode:
    actions: tuple[Action, ...]
    regret_sum: list[float] = field(default_factory=list)
    strategy_sum: list[float] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.regret_sum:
            self.regret_sum = [0.0] * len(self.actions)
            self.strategy_sum = [0.0] * len(self.actions)

    def current_strategy(self) -> list[float]:
        pos = [r if r > 0 else 0.0 for r in self.regret_sum]
        s = sum(pos)
        if s > 0:
            return [p / s for p in pos]
        n = len(self.actions)
        return [1.0 / n] * n

    def average_strategy(self) -> list[float]:
        s = sum(self.strategy_sum)
        if s > 0:
            return [x / s for x in self.strategy_sum]
        n = len(self.actions)
        return [1.0 / n] * n


# ----------------------------------------------------------------------------
# Game state
# ----------------------------------------------------------------------------


@dataclass
class BettingState:
    pot: float
    to_call: float
    hero_stack: float
    villain_stack: float
    hero_contrib_round: float     # chips hero has put in THIS round of betting
    villain_contrib_round: float
    hero_to_act: bool
    raises_this_street: int
    history: tuple[str, ...]
    terminal: bool = False
    folder: str | None = None     # 'hero' or 'villain' if folded
    all_in_called: bool = False   # both players are in for the rest

    def legal_actions(self) -> tuple[Action, ...]:
        outstanding = self.to_call > 1e-9
        actor_stack = self.hero_stack if self.hero_to_act else self.villain_stack
        if outstanding:
            acts: list[Action] = ["fold", "call"]
            if self.raises_this_street < MAX_RAISES_PER_STREET and actor_stack > 0:
                acts.append("raise")
            return tuple(acts)
        acts = ["check"]
        if actor_stack > 0 and self.raises_this_street < MAX_RAISES_PER_STREET:
            acts.append("bet")
        return tuple(acts)

    def apply(self, action: Action) -> "BettingState":
        s = BettingState(
            pot=self.pot,
            to_call=self.to_call,
            hero_stack=self.hero_stack,
            villain_stack=self.villain_stack,
            hero_contrib_round=self.hero_contrib_round,
            villain_contrib_round=self.villain_contrib_round,
            hero_to_act=not self.hero_to_act,
            raises_this_street=self.raises_this_street,
            history=self.history + (action,),
            terminal=False,
            folder=None,
            all_in_called=self.all_in_called,
        )
        actor_is_hero = self.hero_to_act

        if action == "fold":
            s.terminal = True
            s.folder = "hero" if actor_is_hero else "villain"
            return s

        if action == "check":
            # If both players have now checked (two checks in a row, no bet) → end.
            if len(s.history) >= 2 and s.history[-2] == "check":
                s.terminal = True
                return s
            return s

        if action == "call":
            pay = min(self.to_call, self.hero_stack if actor_is_hero else self.villain_stack)
            if actor_is_hero:
                s.hero_stack -= pay
                s.hero_contrib_round += pay
            else:
                s.villain_stack -= pay
                s.villain_contrib_round += pay
            s.pot += pay
            s.to_call = 0.0
            # Call closes the action for this street.
            s.terminal = True
            if min(s.hero_stack, s.villain_stack) <= 1e-9:
                s.all_in_called = True
            return s

        if action == "bet":
            bet = max(1.0, round(self.pot * BET_FRACTION_OF_POT, 2))
            bet = min(bet, self.hero_stack if actor_is_hero else self.villain_stack)
            if actor_is_hero:
                s.hero_stack -= bet
                s.hero_contrib_round += bet
            else:
                s.villain_stack -= bet
                s.villain_contrib_round += bet
            s.pot += bet
            s.to_call = bet
            s.raises_this_street += 1
            return s

        if action == "raise":
            target = max(self.to_call * RAISE_MULTIPLIER, self.to_call + 1.0)
            actor_round = self.hero_contrib_round if actor_is_hero else self.villain_contrib_round
            actor_stack = self.hero_stack if actor_is_hero else self.villain_stack
            opp_round = self.villain_contrib_round if actor_is_hero else self.hero_contrib_round
            target_total = min(actor_round + actor_stack, target + (opp_round - actor_round))
            delta = target_total - actor_round
            delta = min(delta, actor_stack)
            if actor_is_hero:
                s.hero_stack -= delta
                s.hero_contrib_round += delta
            else:
                s.villain_stack -= delta
                s.villain_contrib_round += delta
            s.pot += delta
            s.to_call = (s.hero_contrib_round - s.villain_contrib_round) if not actor_is_hero \
                else (s.villain_contrib_round - s.hero_contrib_round)
            s.to_call = abs(s.to_call)
            s.raises_this_street += 1
            return s

        raise ValueError(f"Unknown action: {action}")


# ----------------------------------------------------------------------------
# Equity cache
# ----------------------------------------------------------------------------


def _combo_to_str(combo: frozenset[Card]) -> str:
    return "".join(str(c) for c in sorted(combo, key=lambda c: (c.rank, c.suit)))


def _equity(
    hero_hand_str: str,
    villain_combo: frozenset[Card],
    board: str,
    samples: int,
    cache: dict[str, float],
    executor: ProcessPoolExecutor | None,
) -> float:
    key = f"{hero_hand_str}|{_combo_to_str(villain_combo)}|{board}"
    if key in cache:
        return cache[key]
    hero_range = parse_range(hero_hand_str)
    villain_single = {villain_combo}
    board_cards = list(Card.parse(board)) if board else []
    try:
        eq = calculate_equities(
            [hero_range, villain_single],
            board_cards,
            2,
            5,
            Deck.STANDARD,
            (StandardHighHand,),
            sample_count=samples,
            executor=executor,
        )
    except Exception:
        eq = calculate_equities(
            [hero_range, villain_single],
            board_cards,
            2,
            5,
            Deck.STANDARD,
            (StandardHighHand,),
            sample_count=samples,
        )
    val = float(eq[0])
    cache[key] = val
    return val


# ----------------------------------------------------------------------------
# Terminal utility (from hero's perspective, in chips)
# ----------------------------------------------------------------------------


def _terminal_utility(
    state: BettingState,
    cfg: SubgameConfig,
    villain_combo: frozenset[Card],
    hero_paid_before_root: float,
    villain_paid_before_root: float,
    executor: ProcessPoolExecutor | None,
) -> float:
    """Return hero's utility in chips vs this particular villain combo.

    The "pot" at the root already includes prior streets' money; we only
    add chips put in during this subgame. Hero's utility = hero's share
    of pot minus hero's chips put in during this subgame.
    """
    hero_invested = hero_paid_before_root + state.hero_contrib_round
    villain_invested = villain_paid_before_root + state.villain_contrib_round
    pot = state.pot  # already reflects both contribs

    if state.folder == "villain":
        return pot - hero_invested - (0 if False else 0) + 0 \
            - state.hero_contrib_round  # hero wins pot minus what *this subgame* cost
    if state.folder == "hero":
        return -state.hero_contrib_round

    # Showdown (check-check, or all-in called, or call).
    hero_eq = _equity(
        cfg.hero_hand,
        villain_combo,
        cfg.board,
        cfg.equity_samples,
        cfg.equity_cache,
        executor,
    )
    expected_share = hero_eq * pot
    return expected_share - (hero_invested)  # hero's net vs villain for this whole hand


# Simplify: we'll compute utility as (pot_share_hero) - (hero_contrib_this_subgame).
# Ignoring money from prior streets is fine because it's a constant.


def _utility_subgame(
    state: BettingState,
    cfg: SubgameConfig,
    villain_combo: frozenset[Card],
    executor: ProcessPoolExecutor | None,
) -> float:
    if state.folder == "villain":
        return state.pot - cfg.pot  # hero wins everything put in this subgame
    if state.folder == "hero":
        return -state.hero_contrib_round

    hero_eq = _equity(
        cfg.hero_hand,
        villain_combo,
        cfg.board,
        cfg.equity_samples,
        cfg.equity_cache,
        executor,
    )
    # Hero's net = hero_eq * final_pot - hero_contrib_this_subgame
    # final_pot = state.pot, hero_contrib = state.hero_contrib_round
    return hero_eq * state.pot - state.hero_contrib_round


# ----------------------------------------------------------------------------
# CFR+ core
# ----------------------------------------------------------------------------


@dataclass
class SolveResult:
    root_strategy: dict[Action, float]
    iterations_done: int
    root_actions: list[Action]
    exploitability_proxy: float  # average absolute regret at root, for feedback
    villain_combos_used: int
    notes: list[str] = field(default_factory=list)


def _root_state(cfg: SubgameConfig) -> BettingState:
    return BettingState(
        pot=cfg.pot,
        to_call=cfg.to_call,
        hero_stack=cfg.hero_stack,
        villain_stack=cfg.villain_stack,
        hero_contrib_round=0.0,
        villain_contrib_round=0.0,
        hero_to_act=(cfg.hero_first_to_act or cfg.to_call > 1e-9),
        raises_this_street=1 if cfg.to_call > 1e-9 else 0,
        history=("_root",) if cfg.to_call > 1e-9 else (),
    )


def _traverse(
    state: BettingState,
    cfg: SubgameConfig,
    villain_combo: frozenset[Card],
    villain_label: str,
    traverser: str,  # 'hero' or 'villain'
    nodes: dict[str, CFRNode],
    executor: ProcessPoolExecutor | None,
    iter_weight: float,
) -> float:
    """Return utility for `traverser` from this state (external-sampling MCCFR)."""
    if state.terminal:
        u = _utility_subgame(state, cfg, villain_combo, executor)
        return u if traverser == "hero" else -u

    actions = state.legal_actions()
    actor = "hero" if state.hero_to_act else "villain"
    key = NodeKey(
        player=actor,
        history=state.history,
        extra=villain_label if actor == "villain" else "",
    ).key()
    node = nodes.get(key)
    if node is None:
        node = CFRNode(actions=actions)
        nodes[key] = node

    strategy = node.current_strategy()

    if actor == traverser:
        # Traverser explores all actions; opponent samples one.
        util_each: list[float] = [0.0] * len(actions)
        node_util = 0.0
        for i, a in enumerate(actions):
            next_state = state.apply(a)
            util_each[i] = _traverse(
                next_state, cfg, villain_combo, villain_label, traverser, nodes, executor, iter_weight
            )
            node_util += strategy[i] * util_each[i]
        # CFR+ regret update: clip regrets at 0.
        for i in range(len(actions)):
            node.regret_sum[i] = max(0.0, node.regret_sum[i] + (util_each[i] - node_util))
            node.strategy_sum[i] += iter_weight * strategy[i]
        return node_util

    # Opponent: sample one action and recurse.
    r = random.random()
    cum = 0.0
    chosen = len(actions) - 1
    for i, p in enumerate(strategy):
        cum += p
        if r < cum:
            chosen = i
            break
    a = actions[chosen]
    next_state = state.apply(a)
    # Track strategy averaging for opponent too (so we learn their response).
    for i in range(len(actions)):
        node.strategy_sum[i] += iter_weight * strategy[i]
    return _traverse(next_state, cfg, villain_combo, villain_label, traverser, nodes, executor, iter_weight)


def solve(
    cfg: SubgameConfig,
    iterations: int = 600,
    seed: int | None = None,
    progress_cb: Callable[[float, str], None] | None = None,
    executor: ProcessPoolExecutor | None = None,
) -> SolveResult:
    rng = random.Random(seed)

    villain_raw = parse_range(cfg.villain_range)
    # Remove combos that collide with hero's cards or the board.
    used: set[Card] = set()
    for piece in (cfg.hero_hand, cfg.board):
        for i in range(0, len(piece), 2):
            try:
                used.update(Card.parse(piece[i : i + 2]))
            except Exception:
                pass
    villain_combos: list[frozenset[Card]] = [
        c for c in villain_raw if not (set(c) & used)
    ]
    if not villain_combos:
        raise ValueError("No legal villain combos after removing card collisions.")

    # Warm the equity cache in parallel-ish by precomputing at root-sample
    # pace; we'll compute on demand inside _equity.

    nodes: dict[str, CFRNode] = {}

    # Determine who is "traverser" at the root and the label for the key.
    # We alternate traversers each iteration (standard MCCFR trick).
    report_every = max(1, iterations // 20)

    for it in range(1, iterations + 1):
        villain_combo = rng.choice(villain_combos)
        villain_label = _combo_to_str(villain_combo)
        # Alternate traverser for symmetric updates.
        traverser = "hero" if it % 2 == 1 else "villain"
        # Seed the sampler deterministically across traversers so they see
        # similar matchups; nothing fancy needed.
        random.seed(rng.random())
        _traverse(
            _root_state(cfg), cfg, villain_combo, villain_label, traverser, nodes, executor, float(it)
        )
        if progress_cb and (it % report_every == 0 or it == iterations):
            progress_cb(it / iterations, f"iter {it}/{iterations}")

    # Extract root strategy for hero.
    root_state = _root_state(cfg)
    root_actions = list(root_state.legal_actions())
    root_key = NodeKey(
        player="hero" if root_state.hero_to_act else "villain",
        history=root_state.history,
    ).key()
    root_node = nodes.get(root_key)
    if root_node is None:
        # Hero didn't act at root (villain did); seek hero's first decision instead.
        # Fall back to uniform.
        strat = {a: 1.0 / len(root_actions) for a in root_actions}
        exploit = 0.0
    else:
        avg = root_node.average_strategy()
        strat = {a: p for a, p in zip(root_node.actions, avg)}
        exploit = sum(root_node.regret_sum) / max(1, iterations)

    return SolveResult(
        root_strategy=strat,
        iterations_done=iterations,
        root_actions=root_actions,
        exploitability_proxy=exploit,
        villain_combos_used=len(villain_combos),
    )


# ----------------------------------------------------------------------------
# Convenience: pick an action from the solver strategy.
# ----------------------------------------------------------------------------


def top_action(strategy: dict[Action, float]) -> tuple[Action, float]:
    if not strategy:
        return ("check", 1.0)
    best = max(strategy.items(), key=lambda kv: kv[1])
    return best
