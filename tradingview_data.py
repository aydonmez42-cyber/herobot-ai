import json
import random
import re
import string
import time
from datetime import datetime, timezone

import pandas as pd
import websocket

TV_WS_URL = "wss://data.tradingview.com/socket.io/websocket"
TV_TOKEN = "unauthorized_user_token"
DEFAULT_TIMEOUT = 20


def _session(prefix):
    return prefix + "_" + "".join(random.choice(string.ascii_lowercase) for _ in range(12))


def _frame(method, params):
    payload = json.dumps({"m": method, "p": params}, separators=(",", ":"))
    return f"~m~{len(payload)}~m~{payload}"


def _frames(text):
    out = []
    pos = 0
    while pos < len(text):
        if text.startswith("~m~", pos):
            m = re.match(r"~m~(\d+)~m~", text[pos:])
            if not m:
                break
            n = int(m.group(1))
            start = pos + m.end()
            payload = text[start:start + n]
            pos = start + n
            if payload == "":
                continue
            try:
                out.append(json.loads(payload))
            except json.JSONDecodeError:
                pass
        elif text.startswith("~protocol_error~", pos):
            break
        else:
            nxt = text.find("~m~", pos)
            if nxt < 0:
                break
            pos = nxt
    return out


def _extract_rows(message):
    """Extract TradingView chart bars from timescale_update/du messages."""
    if not isinstance(message, dict):
        return []
    method = message.get("m")
    if method not in ("timescale_update", "du"):
        return []
    p = message.get("p") or []
    if len(p) < 2 or not isinstance(p[1], dict):
        return []
    container = p[1]
    rows = []
    # A plausible unix timestamp (seconds) for any real market bar: 2001-09-09 .. 2100-01-01.
    TS_MIN, TS_MAX = 1_000_000_000, 4_102_444_800
    for series in container.values():
        if not isinstance(series, dict):
            continue
        for item in series.get("s", []) or []:
            v = item.get("v") if isinstance(item, dict) else None
            if not isinstance(v, list) or len(v) < 5:
                continue
            # TradingView bar payloads are normally [time, open, high, low,
            # close, volume] with NO leading "bar index" inside v (the bar
            # index, when present, lives in the sibling "i" key which we
            # don't need). Assuming a fixed leading index column shifts
            # every field by one -> open gets read as the timestamp and
            # volume gets read as the close price, producing impossible
            # "1970" candle times and multi-million "prices". Instead,
            # locate the real timestamp by magnitude, wherever it sits.
            ts_idx = None
            for idx in (0, 1):
                try:
                    if idx < len(v) and TS_MIN <= float(v[idx]) <= TS_MAX:
                        ts_idx = idx
                        break
                except (TypeError, ValueError):
                    continue
            if ts_idx is None:
                continue
            vals = v[ts_idx:]
            if len(vals) < 5:
                continue
            ts, op, hi, lo, cl = vals[:5]
            vol = vals[5] if len(vals) > 5 else 0.0
            try:
                ts = float(ts)
                rows.append({"timestamp": pd.Timestamp(ts, unit="s", tz="UTC"),
                             "open": float(op), "high": float(hi), "low": float(lo),
                             "close": float(cl), "volume": float(vol) if vol is not None else 0.0})
            except (TypeError, ValueError):
                continue
    return rows


def fetch_tv_bars(symbol, interval="240", bars=300, timeout=DEFAULT_TIMEOUT, retries=2):
    """Fetch native TradingView chart bars. interval 240 = 4H."""
    full = symbol if ":" in symbol else f"BIST:{symbol}"
    last_error = None
    for attempt in range(retries + 1):
        ws = None
        try:
            ws = websocket.create_connection(
                TV_WS_URL,
                timeout=timeout,
                origin="https://www.tradingview.com",
                header=["User-Agent: Mozilla/5.0"],
            )
            cs = _session("cs")
            qs = _session("qs")
            ws.send(_frame("set_auth_token", [TV_TOKEN]))
            ws.send(_frame("chart_create_session", [cs, ""]))
            ws.send(_frame("quote_create_session", [qs]))
            ws.send(_frame("resolve_symbol", [
                cs, "sds_sym_1",
                "=" + json.dumps({"symbol": full, "adjustment": "splits"}, separators=(",", ":"))
            ]))
            ws.send(_frame("create_series", [cs, "sds_1", "s1", "sds_sym_1", interval, int(bars), ""]))

            rows = []
            deadline = time.time() + timeout
            completed = False
            while time.time() < deadline:
                raw = ws.recv()
                if not raw:
                    continue
                # TradingView heartbeat is ~m~N~m~~h~...; echo it back.
                if "~h~" in raw:
                    for h in re.findall(r"~m~\d+~m~~h~\d+", raw):
                        try:
                            ws.send(h)
                        except Exception:
                            pass
                for msg in _frames(raw):
                    method = msg.get("m") if isinstance(msg, dict) else None
                    if method in ("symbol_error", "series_error", "critical_error", "protocol_error"):
                        raise RuntimeError(f"TradingView {method}: {msg.get('p')}")
                    rows.extend(_extract_rows(msg))
                    if method == "series_completed":
                        completed = True
                if completed and rows:
                    break
            if not rows:
                raise RuntimeError("TradingView 4H veri döndürmedi")
            df = pd.DataFrame(rows).drop_duplicates(subset=["timestamp"]).sort_values("timestamp")
            return df.reset_index(drop=True)
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(0.8 * (attempt + 1))
        finally:
            try:
                if ws:
                    ws.close()
            except Exception:
                pass
    raise RuntimeError(f"TradingView veri hatası {full}: {last_error}")


def add_close_time(df, hours=4):
    x = df.copy()
    # Chart timestamps are bar-open timestamps. For BIST native 4H bars,
    # 10:00→14:00 and 14:00→18:00 session bars are therefore closed +4h later.
    x["close_time"] = x["timestamp"] + pd.Timedelta(hours=hours)
    return x
