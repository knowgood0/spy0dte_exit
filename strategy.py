from dataclasses import dataclass
from math import sqrt
from datetime import time
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


@dataclass
class Bar:
    timestamp: object
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


def rma(x, n):
    o = [None] * len(x)

    if len(x) < n:
        return o

    p = sum(x[:n]) / n
    o[n - 1] = p

    for i in range(n, len(x)):
        p = ((n - 1) * p + x[i]) / n
        o[i] = p

    return o


def atr(bs, n):
    tr = []
    pc = None

    for b in bs:
        if pc is None:
            tr.append(b.high - b.low)
        else:
            tr.append(
                max(
                    b.high - b.low,
                    abs(b.high - pc),
                    abs(b.low - pc),
                )
            )

        pc = b.close

    return rma(tr, n)


def stdev(x, n):
    if len(x) < n:
        return None

    a = x[-n:]
    m = sum(a) / n

    return sqrt(
        sum((v - m) ** 2 for v in a) / n
    )


def kalman(x, q, r):
    e = x[0]
    err = 1.0
    out = []

    for v in x:
        pred = err + q
        g = pred / (pred + r)

        e = e + g * (v - e)
        err = (1 - g) * pred

        out.append(e)

    return out


def adx(bs, di=14, al=14):
    tr = []
    p = []
    m = []

    ph = None
    pl = None
    pc = None

    for b in bs:
        if pc is None:
            tr.append(b.high - b.low)
            p.append(0)
            m.append(0)
        else:
            tr.append(
                max(
                    b.high - b.low,
                    abs(b.high - pc),
                    abs(b.low - pc),
                )
            )

            up = b.high - ph
            dn = pl - b.low

            p.append(
                up
                if up > dn and up > 0
                else 0
            )

            m.append(
                dn
                if dn > up and dn > 0
                else 0
            )

        ph = b.high
        pl = b.low
        pc = b.close

    at = rma(tr, di)
    pr = rma(p, di)
    mr = rma(m, di)

    dx = []

    for a, pp, mm in zip(at, pr, mr):
        if a is None:
            dx.append(0)
            continue

        pi = 100 * pp / a
        mi = 100 * mm / a

        dx.append(
            0
            if pi + mi == 0
            else 100 * abs(pi - mi) / (pi + mi)
        )

    return rma(dx, al)


def vwma(bs, n):
    a = bs[-n:]
    v = sum(b.volume for b in a)

    if v:
        return sum(
            b.close * b.volume
            for b in a
        ) / v

    return sum(
        b.close for b in a
    ) / len(a)


def analyze(bs):
    """
    Analyze the complete available bar series INCLUDING the
    currently forming 5-minute bar.

    The final bar is intentionally treated as live/intrabar.
    Its current high, low, close and volume are used immediately.

    This means the signal can change during the current candle
    instead of waiting for that candle to close.
    """

    if len(bs) < 60:
        raise ValueError(
            "Need at least 60 SPY bars"
        )

    c = [
        b.close
        for b in bs
    ]

    k = kalman(
        c,
        0.01,
        0.2,
    )

    a = atr(
        bs,
        7,
    )

    ad = adx(bs)

    up = None
    dn = None
    trend = 1

    hist = []

    for i, b in enumerate(bs):
        if a[i] is None:
            hist.append(trend)
            continue

        ub = k[i] - 2 * a[i]
        db = k[i] + 2 * a[i]

        if up is None:
            up = ub
        else:
            previous_close = c[i - 1]

            if previous_close > up:
                up = max(ub, up)
            else:
                up = ub

        if dn is None:
            dn = db
        else:
            previous_close = c[i - 1]

            if previous_close < dn:
                dn = min(db, dn)
            else:
                dn = db

        if i == 0:
            prev_up = up
            prev_dn = dn
        else:
            prev_up = old_up
            prev_dn = old_dn

        # IMPORTANT:
        # b.close is the CURRENT live close when this is the
        # currently forming bar. Therefore these transitions
        # can occur intrabar.
        if trend == -1 and b.close > prev_dn:
            trend = 1

        elif trend == 1 and b.close < prev_up:
            trend = -1

        old_up = up
        old_dn = dn

        hist.append(trend)

    # ------------------------------------------------------------
    # INTRABAR SIGNAL
    # ------------------------------------------------------------
    #
    # Compare the current live trend with the previous bar's
    # trend. The current bar does NOT need to close.
    #
    long = (
        hist[-1] == 1
        and hist[-2] == -1
    )

    short = (
        hist[-1] == -1
        and hist[-2] == 1
    )

    eq = vwma(
        bs,
        50,
    )

    widths = []

    for i in range(len(bs)):
        s = stdev(
            c[: i + 1],
            20,
        )

        av = ad[i]

        if s is None or av is None:
            widths.append(0)

        else:
            widths.append(
                s
                * 1.5
                * (1 + 0.8 * av / 100)
            )

    smoothed_widths = rma(
        widths,
        10,
    )

    w = smoothed_widths[-1]

    upper = (
        eq + 2 * w
        if w
        else None
    )

    lower = (
        eq - 2 * w
        if w
        else None
    )

    recent = [
        x
        for x in smoothed_widths[-50:]
        if x is not None
    ]

    comp = False

    if recent and max(recent) > min(recent):
        comp = (
            w - min(recent)
        ) <= (
            max(recent) - min(recent)
        ) * 0.3

    # ------------------------------------------------------------
    # REGULAR TRADING HOURS
    # ------------------------------------------------------------

    et = bs[-1].timestamp.astimezone(
        ET
    ).time()

    rth = (
        time(9, 30)
        <= et
        <= time(16, 0)
    )

    if not rth:
        long = False
        short = False

    return {
        "signal": (
            "CALL"
            if long
            else "PUT"
            if short
            else None
        ),
        "trend": trend,
        "close": c[-1],
        "atr": a[-1],
        "adx": ad[-1],
        "upper": upper,
        "lower": lower,
        "compressed": comp,
        "bar_time": bs[-1].timestamp.isoformat(),
    }


def levels(pos):
    e = pos["entry_underlying"]
    a = pos["entry_atr"]
    side = pos["side"]

    if side == "CALL":
        return (
            e - a * 1.5,
            e + a * 2,
            e + a,
        )

    return (
        e + a * 1.5,
        e - a * 2,
        e - a,
    )
