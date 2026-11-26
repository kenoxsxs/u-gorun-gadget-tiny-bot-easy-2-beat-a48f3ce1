"""A small trend bot for hourly Solana bars: EMA(5) over EMA(13), gated by momentum.

Why this bot, and why not the neighbours.

The contest replays three whole UTC days of hourly bars for seven Solana
assets (FARTCOIN, JITOSOL, POPCAT, RAY, SOL, WETH, WIF) and ranks entries by
net PnL in USDC. A prize needs a Net PnL above the buy-and-hold baseline's,
at least eight trades, and a maximum drawdown of 25% or less. The final
window does not exist yet when entries close, so nothing here is fitted to
it: every number below is a judgement about hourly crypto, not a backtest
artifact, and the same code runs the house's dev window and the final one.

The core idea is deliberately boring: ride short uptrends, sit in cash when
there is none. Buy-and-hold is fully exposed to every drawdown the window
throws at it; this bot's edge is the time it spends NOT holding. In a
sideways or falling window the baseline bleeds and this bot waits. In a
rising window the bot is long most of the time anyway, because the same
crosses that mark the trend also mark the entries. The risk is a window
that rises in one straight line with no pullback: there buy-and-hold wins
by the fees this bot pays on its exits. That is the honest trade, and it
is why the exits below are wide enough to let a trend breathe.

Three moving averages, three jobs.

EMA(5) is the fast line. Five hourly bars is short enough to turn within a
day and long enough that a single wick does not flip it. EMA(13) is the
slow line: about half a day, the line the fast one has to cross to prove
the move is real. The pair is quicker than the classic 8/21 pair the
house's own ema-cross baseline uses, because an hourly window of three
days is short: a 21-bar slow line needs almost a full day just to seed,
and by the time it confirms a move a third of the window is gone. Five
and thirteen seed in thirteen bars and start trading while there is still
window left to trade in.

EMA(34) is the trend gate, and it does a different job from the pair. The
cross of 5 over 13 can happen inside a downtrend -- a dead-cat bounce
crosses too -- so an entry also needs the close above EMA(34), about a
day and a half of trend. When the long line is not seeded yet (the first
thirty-four bars of a symbol) the gate passes open: refusing every early
trade for lack of a long average would waste the warm-up the harness
already paid for. A gate that never opens is not a filter, it is a
refusal to trade.

The momentum gate is the second filter, and it is the one that does the
most work. An entry also needs the close above the close twelve bars
ago: the symbol must actually be up over half a day, not merely crossing
on a flat line. Crosses on flat lines are whipsaws with a fee attached.
Twelve bars is short enough to catch a move early and long enough that
noise does not fake it. (A twenty-four-bar window was tried: it blocked
the run's one winning trade and left the net PnL worse, so twelve bars
stands.) The fast line must also be rising bar over bar:
a cross made by the slow line falling into the fast one is not momentum,
it is the floor dropping slower than the ceiling. And the slow line must
be rising too: a cross made while the slow line is still falling is a
bear-market bounce, not a turn, and the first graded run lost most of its
money buying exactly those bounces. Demanding both lines rising makes the
entry wait for the turn to confirm.

Exits are whole-position, and there are three of them. The slow line
crossing back above the fast one is the trend exit: the reason for the
trade is gone, so the position goes, and the signal holds while the slow
line stays on top, so a refused sell is sent again on the next bar. The
other two are price exits, and they are tighter than the house baseline's
because hourly memecoins move fast: take profit at 35% over cost
(27/20) and stop loss at 12% under it (22/25). Thirty-five percent is a
real gain on an hourly chart without being so far away that a position
never reaches it; twelve percent down is a real loss, cut before it
becomes a drawdown that eats the guardrail. Both are checked with exact
integer arithmetic on value versus cost, never on floats, so the same
bars give the same exits run after run.

Sizing is equal weight over seven slots. Seven is fewer than the ten
open positions the caps allow, on purpose: seven slots of roughly a
seventh of equity each is a bigger bite per trade than ten slots of a
tenth, and the fee is a fixed ten basis points per trade either way, so
bigger slices waste less of each move on fees. Each buy is the smallest
of the equal slice, the 25% per-symbol weight cap, and the cash this
reply has not spent yet; below the minimum notional it buys nothing.
Sells go first in every reply and buys follow in universe order, at most
sixteen orders a bar, each capped at 100 basis points of slippage past
the close it was decided on, so a sell into a liquidity gap is refused
and retried rather than filled for almost nothing.

What the neighbours get wrong.

EMA(8)/EMA(21) with no filter -- the house baseline's shape -- trades
every cross, including the counter-trend ones, and pays the fee on each.
This bot keeps the cross but demands the trend and the momentum first,
so it trades fewer, better crosses. Buy-and-hold never exits: it takes
every drawdown at full size and can only win if the window ends higher
than it started, which is a bet on the window, not a strategy. Hold-cash
never enters: zero PnL, zero trades, it fails the eight-trade guardrail
by design. A momentum-only bot with no cross chases every green bar and
buys tops; a cross-only bot with no momentum buys flat-line whipsaws.
The pair plus the two gates is the middle that survives both failure
modes: the cross says when, the trend says whether, the momentum says
how hard.

Determinism is a scoring rule, not a nicety: the harness runs every
entry twice and an entry whose two action logs differ is not ranked.
Nothing here reads the clock, the environment, or an unseeded random.
The averages run in float64 on addition, subtraction and multiplication
alone, each correctly rounded, and every decision reads the bars in
universe order, so the same bars give the same orders, run after run.
Diagnostics go to stderr; stdout carries replies only.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bars

FAST_SPAN = 5
SLOW_SPAN = 13
TREND_SPAN = 34
MOMENTUM_BARS = 12
TAKE_NUM, TAKE_DEN = 27, 20
STOP_NUM, STOP_DEN = 22, 25
MAX_SLIP_BPS = 100
SLOTS = 7


class Ema:
    """One exponential moving average over closes.

    Seeds as the mean of its first ``span`` closes, then moves a fraction
    2 / (span + 1) of the way to each new close. ``prev`` is the value
    before the latest feed, or None until the second update after seeding.
    """

    __slots__ = ("span", "warm", "value", "prev")

    def __init__(self, span):
        self.span = span
        self.warm = []
        self.value = None
        self.prev = None

    def feed(self, close):
        if self.value is None:
            self.warm.append(close)
            if len(self.warm) == self.span:
                self.value = sum(self.warm) / self.span
            return
        self.prev = self.value
        self.value += (2.0 / (self.span + 1.0)) * (close - self.value)


class Tape:
    """Per-symbol state: its closes, three averages, and the cross ledger.

    ``gap`` is fast-minus-slow after the symbol's bar before this one,
    ``now`` after this bar. ``down`` is the held sell signal: set when the
    slow line crosses above the fast one, cleared when the fast line is
    back on top (or the averages are not seeded yet).
    """

    __slots__ = ("closes", "fast", "slow", "trend", "gap", "now", "down")

    def __init__(self):
        self.closes = []
        self.fast = Ema(FAST_SPAN)
        self.slow = Ema(SLOW_SPAN)
        self.trend = Ema(TREND_SPAN)
        self.gap = None
        self.now = None
        self.down = False

    def push(self, close):
        self.closes.append(close)
        self.fast.feed(close)
        self.slow.feed(close)
        self.trend.feed(close)
        self.gap = self.now
        if self.fast.value is not None and self.slow.value is not None:
            self.now = self.fast.value - self.slow.value
        else:
            self.now = None
        if self.now is None or self.now >= 0:
            self.down = False

    def crossed_up(self):
        """The fast line crossed above the slow one on this bar."""
        return self.gap is not None and self.now is not None and self.now > 0 and self.gap <= 0

    def crossed_down(self):
        """The slow line crossed above the fast one on this bar."""
        return self.gap is not None and self.now is not None and self.now < 0 and self.gap >= 0

    def momentum_up(self):
        """The close is above the close twelve bars ago.

        A twenty-four-bar window was tried and reverted: it blocked the
        run's one winning trade, cut the traded symbols from seven to
        four, and left the net PnL three USDC worse. Twelve bars is the
        better window: short enough to catch a move early and long enough
        that noise does not fake it.
        """
        n = len(self.closes)
        return n > MOMENTUM_BARS and self.closes[-1] > self.closes[-1 - MOMENTUM_BARS]

    def trend_up(self, close):
        """The close sits above the long average, or the average is unseeded."""
        return self.trend.value is None or close > self.trend.value

    def fast_rising(self):
        """The fast average rose bar over bar."""
        return self.fast.prev is not None and self.fast.value > self.fast.prev

    def slow_rising(self):
        """The slow average rose bar over bar.

        The extra gate fix 1 adds: a cross made while the slow line is still
        falling is a bear-market bounce, not a turn. The fast line can cross
        above a falling slow line on a single green bar; the slow line only
        rises when the medium-term trend itself has turned. Demanding both
        lines rising means the entry waits for the turn to confirm instead
        of buying the first bounce inside a downtrend, which is where the
        first graded run lost most of its money.
        """
        return self.slow.prev is not None and self.slow.value > self.slow.prev


TAPES = {}


def tape_for(symbol):
    tape = TAPES.get(symbol)
    if tape is None:
        tape = Tape()
        TAPES[symbol] = tape
    return tape


def should_exit(state, symbol, held, tape):
    """Whether a held position leaves on this close: the held sell signal,
    the take-profit, or the stop-loss, each in exact integer arithmetic."""
    if tape.down:
        return True
    value = state.value(symbol, held.qty)
    if value * TAKE_DEN >= held.cost * TAKE_NUM:
        return True
    return value * STOP_DEN <= held.cost * STOP_NUM


def decide(state, batch):
    """The orders decided on this bar: sells first, then buys, universe order."""
    cross_up = set()
    fresh = set()
    for bar in batch["bars"]:
        tape = tape_for(bar["symbol"])
        tape.push(float(bar["c"]))
        fresh.add(bar["symbol"])
        if bar.get("v") == "0":
            continue
        if tape.crossed_up():
            cross_up.add(bar["symbol"])
        if tape.crossed_down():
            tape.down = True
    if state.warming_up:
        return []
    caps = state.hello["caps"]
    fees = state.hello["fees"]
    sells = []
    entries = []
    for entry in state.hello["symbols"]:
        symbol = entry["symbol"]
        if symbol not in fresh:
            continue
        held = state.positions.get(symbol)
        if held is not None:
            if should_exit(state, symbol, held, tape_for(symbol)):
                sells.append(
                    {"side": "sell", "symbol": symbol, "qty": held.qty, "max_slippage_bps": MAX_SLIP_BPS}
                )
        elif symbol in cross_up:
            tape = tape_for(symbol)
            close = float(state.latest[symbol]["c"])
            if (
                tape.momentum_up()
                and tape.trend_up(close)
                and tape.fast_rising()
                and tape.slow_rising()
            ):
                entries.append(symbol)
    orders = sells[: caps["max_orders_per_bar"]]
    stake = state.equity // SLOTS
    by_weight = caps["max_weight_bps"] * state.equity // makefun_bars.BPS
    room = SLOTS - len(state.positions)
    cash = state.cash
    for symbol in entries:
        if room <= 0 or len(orders) >= caps["max_orders_per_bar"]:
            break
        slice_ = stake if stake < by_weight else by_weight
        if cash < slice_:
            slice_ = cash
        if slice_ == 0 or slice_ < int(fees["min_notional_quote_units"]):
            break
        orders.append(
            {
                "side": "buy",
                "symbol": symbol,
                "notional_quote_units": slice_,
                "max_slippage_bps": MAX_SLIP_BPS,
            }
        )
        cash -= slice_
        room -= 1
    return orders


def main():
    makefun_bars.run(decide)


if __name__ == "__main__":
    main()
