#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sequoia-service/server.py — Sequoia-X 伴侣服务 HTTP API

把 Sequoia-X 的 DataEngine + 7 个选股策略封装成一个本地 HTTP 服务，
供 astock-analyst (Node) 调用。仅复用 Sequoia-X 已有依赖（pandas / pydantic-settings
/ python-dotenv / rich / baostock），不引入 Web 框架。

启动：python server.py
默认端口 8791，可用环境变量 SEQUOIA_PORT / SEQUOIA_DB 覆盖。

接口：
  GET  /api/health               -> 服务与数据健康状态
  GET  /api/strategies           -> 策略元信息列表
  POST /api/scan?name=all|<key>  -> 运行策略，返回命中（含最新价/涨跌/耗时），缓存 scan_results.json
  GET  /api/results              -> 最近一次扫描缓存
  POST /api/backtest?days=&hold=&sample= -> 轻量回测（后台），算各策略 胜率/均收益
  GET  /api/backtest/results     -> 回测结果缓存
  POST /api/backfill             -> 触发 baostock 全市场回填（后台长任务）

仅本地访问，不做鉴权。
"""
import os
import sys
import json
import time
import threading
import sqlite3
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# Sequoia-X 的 Settings 必填 feishu_webhook_url；伴侣服务不需要飞书，给占位
os.environ.setdefault("FEISHU_WEBHOOK_URL", "http://localhost/none")
# 伴侣服务默认离线快扫：跳过海龟策略的实时 baostock 流通市值查询
os.environ.setdefault("SEQUOIA_SKIP_LIVE_CAPS", "1")

from sequoia_x.core.config import Settings
from sequoia_x.data.engine import DataEngine
from sequoia_x.strategy.base import BaseStrategy  # noqa: F401
from sequoia_x.strategy.turtle_trade import TurtleTradeStrategy
from sequoia_x.strategy.ma_volume import MaVolumeStrategy
from sequoia_x.strategy.high_tight_flag import HighTightFlagStrategy
from sequoia_x.strategy.limit_up_shakeout import LimitUpShakeoutStrategy
from sequoia_x.strategy.uptrend_limit_down import UptrendLimitDownStrategy
from sequoia_x.strategy.rps_breakout import RpsBreakoutStrategy
from sequoia_x.strategy.private_placement import PrivatePlacementStrategy

PORT = int(os.environ.get("SEQUOIA_PORT", "8791"))
DB_PATH = os.environ.get("SEQUOIA_DB", os.path.join(HERE, "data", "sequoia_v2.db"))
DATA_DIR = os.path.dirname(DB_PATH)
RESULTS_PATH = os.path.join(DATA_DIR, "scan_results.json")
PROGRESS_PATH = os.path.join(DATA_DIR, "backfill_progress.json")
BACKTEST_PATH = os.path.join(DATA_DIR, "backtest_results.json")

# 策略注册表： (key, class, 显示名, 分组, 说明)
STRATEGY_REGISTRY = [
    ("ma_volume", MaVolumeStrategy, "均线放量突破", "趋势与突破", "均线多头 + 放量突破"),
    ("turtle", TurtleTradeStrategy, "海龟突破", "趋势与突破", "20日新高 + 成交额过亿 + 阳线防诱多"),
    ("high_tight_flag", HighTightFlagStrategy, "高窄旗形", "趋势与突破", "高而窄的旗形整理突破"),
    ("limit_up_shakeout", LimitUpShakeoutStrategy, "涨停洗盘", "趋势与突破", "涨停洗盘回踩确认"),
    ("uptrend_limit_down", UptrendLimitDownStrategy, "上升跌停反包", "超跌与反转", "上升趋势中的跌停反包"),
    ("rps_breakout", RpsBreakoutStrategy, "欧奈尔RPS突破", "趋势与突破", "RPS相对强度突破"),
    ("private_placement", PrivatePlacementStrategy, "定增套利", "超跌与反转", "定向增发事件驱动"),
]

_lock = threading.Lock()

settings = Settings(db_path=DB_PATH, feishu_webhook_url="http://localhost/none")
engine = DataEngine(settings)

from scheduler import Scheduler  # noqa: E402

SCHEDULER = Scheduler()


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def get_quote(symbol):
    """返回最新价、昨收、涨跌幅、最新日期"""
    try:
        df = engine.get_ohlcv(symbol)
        if df is None or len(df) < 2:
            return None
        last = df.iloc[-1]
        prev = df.iloc[-2]
        close = float(last["close"])
        pclose = float(prev["close"])
        pct = (close - pclose) / pclose * 100 if pclose else 0.0
        return {
            "symbol": symbol,
            "last": round(close, 2),
            "prev_close": round(pclose, 2),
            "pct": round(pct, 2),
            "date": str(last["date"]),
        }
    except Exception:
        return None


_NAME_CACHE = None


def load_names():
    """代码 -> 名称映射（symbol_meta 表；真实回填库没有该表时返回空字典）"""
    global _NAME_CACHE
    if _NAME_CACHE is not None:
        return _NAME_CACHE
    _NAME_CACHE = {}
    try:
        con = sqlite3.connect(DB_PATH)
        for sym, nm in con.execute("SELECT symbol, name FROM symbol_meta"):
            if nm:
                _NAME_CACHE[str(sym)] = str(nm)
        con.close()
    except Exception:
        pass
    return _NAME_CACHE


# 「宽松模式」只对条件过严的策略生效：默认参数（Sequoia-X 原版）保持不动，
# 用户主动点「放宽」才用这组阈值，页面上会标明放宽了什么。
RELAX_PRESET = {
    "high_tight_flag": {
        "momentum_ratio": 1.45,      # 40日高低比 1.6 -> 1.45
        "consolidation_ratio": 1.18,  # 10日振幅 1.15 -> 1.18
        "high_level_ratio": 0.62,     # 高位 0.80 -> 0.62
        "shrink_ratio": 0.85,         # 缩量 0.60 -> 0.85
    },
}


def run_one(strategy_cls, overrides=None):
    st = strategy_cls(engine=engine, settings=settings)
    for k, v in (overrides or {}).items():
        try:
            setattr(st, k, v)
        except Exception:
            pass
    t0 = time.time()
    selected = st.run()
    cost = round(time.time() - t0, 2)
    names = load_names()
    picks = []
    for sym in selected:
        nm = names.get(str(sym), "")
        q = get_quote(sym)
        if q:
            picks.append({"symbol": sym, "name": nm, "last": q["last"], "pct": q["pct"], "date": q["date"]})
        else:
            picks.append({"symbol": sym, "name": nm, "last": None, "pct": None, "date": None})
    return picks, cost


def do_scan(name="all", relaxed=False):
    result = {"time": _now(), "mode": name, "relaxed": bool(relaxed), "strategies": [], "total": 0}
    if name == "all":
        targets = [(m[0], m[1]) for m in STRATEGY_REGISTRY]
    else:
        targets = [(m[0], m[1]) for m in STRATEGY_REGISTRY if m[0] == name]
    for key, cls in targets:
        meta = next(m for m in STRATEGY_REGISTRY if m[0] == key)
        ov = RELAX_PRESET.get(key) if relaxed else None
        try:
            picks, cost = run_one(cls, ov)
            item = {
                "key": key, "name": meta[2], "group": meta[3], "desc": meta[4],
                "count": len(picks), "cost": cost, "picks": picks,
            }
            if ov:
                item["relaxed_params"] = ov
            result["strategies"].append(item)
            result["total"] += len(picks)
        except Exception as exc:
            result["strategies"].append({
                "key": key, "name": meta[2], "group": meta[3], "desc": meta[4],
                "count": 0, "cost": 0, "picks": [], "error": str(exc),
            })
    with _lock:
        with open(RESULTS_PATH, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
    return result


def _sample_dates(n=15):
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT DISTINCT date FROM stock_daily ORDER BY date DESC LIMIT ?",
            (n * 3,),
        ).fetchall()
    dates = [r[0] for r in rows]
    if len(dates) > n:
        step = len(dates) // n
        dates = dates[::step][:n]
    return sorted(dates)


def do_backtest(days=60, hold=5, sample=15):
    dates = _sample_dates(sample)
    if not dates:
        return {"error": "no data"}
    with sqlite3.connect(DB_PATH) as conn:
        full = conn.execute("SELECT symbol, date, close FROM stock_daily").fetchall()
    hist = {}
    for sym, d, c in full:
        hist.setdefault(sym, []).append((d, float(c)))
    for sym in hist:
        hist[sym].sort()

    out = {"time": _now(), "sample_dates": len(dates), "hold": hold, "strategies": []}
    for key, cls, disp, group, desc in STRATEGY_REGISTRY:
        wins = 0
        total = 0
        sum_ret = 0.0
        err = None
        for cutoff in dates:
            tmp = os.path.join(tempfile.gettempdir(), f"seq_tmp_{key}_{cutoff}.db")
            if os.path.exists(tmp):
                os.remove(tmp)
            try:
                with sqlite3.connect(DB_PATH) as src, sqlite3.connect(tmp) as dst:
                    dst.execute(
                        "CREATE TABLE stock_daily (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                        "symbol TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, "
                        "volume REAL, turnover REAL, UNIQUE(symbol,date))"
                    )
                    cur = src.execute(
                        "SELECT symbol,date,open,high,low,close,volume,turnover "
                        "FROM stock_daily WHERE date<=?",
                        (cutoff,),
                    )
                    dst.executemany(
                        "INSERT OR IGNORE INTO stock_daily"
                        "(symbol,date,open,high,low,close,volume,turnover) VALUES(?,?,?,?,?,?,?,?)",
                        cur.fetchall(),
                    )
                eng = DataEngine(Settings(db_path=tmp, feishu_webhook_url="x"))
                st = cls(engine=eng, settings=settings)
                sig = st.run()
            except Exception as exc:
                err = str(exc)
                sig = []
            finally:
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except Exception:
                        pass
            for sym in sig:
                series = hist.get(sym)
                if not series:
                    continue
                future = [c for (d, c) in series if d > cutoff]
                if len(future) <= hold:
                    continue
                base = future[0]
                target = future[hold]
                ret = (target - base) / base * 100
                total += 1
                sum_ret += ret
                if ret > 0:
                    wins += 1
        win_rate = round(wins / total * 100, 1) if total else None
        avg_ret = round(sum_ret / total, 2) if total else None
        out["strategies"].append({
            "key": key, "name": disp, "group": group,
            "win_rate": win_rate, "avg_return": avg_ret, "samples": total,
            "error": err,
        })
    with _lock:
        with open(BACKTEST_PATH, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
    return out


def backfill():
    try:
        syms = engine.get_all_symbols()
        engine.backfill(syms)
        print(f"[sequoia-service] 回填完成，共 {len(syms)} 只")
    except Exception as exc:
        print(f"[sequoia-service] 回填失败: {exc}")


# ---- HTTP handler ----
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = urlparse(self.path)
        path = p.path
        if path in ("/api/health", "/api/status"):
            self._send(200, health())
        elif path == "/api/strategies":
            self._send(200, [{"key": m[0], "name": m[2], "group": m[3], "desc": m[4]} for m in STRATEGY_REGISTRY])
        elif path == "/api/results":
            self._send(200, load_json(RESULTS_PATH))
        elif path == "/api/backtest/results":
            self._send(200, load_json(BACKTEST_PATH))
        elif path == "/api/scheduler/status":
            st = SCHEDULER.status()
            st["active_runner"] = hasattr(SCHEDULER, "_scan_runner")
            self._send(200, st)
        elif path == "/api/backfill/progress":
            prog = load_json(PROGRESS_PATH)
            if not prog or not prog.get("phase"):
                prog = {"phase": "idle"}
            with sqlite3.connect(DB_PATH) as conn:
                try:
                    n = conn.execute("SELECT COUNT(*) FROM stock_daily").fetchone()[0]
                    syms = conn.execute("SELECT COUNT(DISTINCT symbol) FROM stock_daily").fetchone()[0]
                    prog["db_rows"] = n
                    prog["db_symbols"] = syms
                except Exception:
                    pass
            self._send(200, prog)
        elif path == "/api/kline/latest":
            # 本地 K 线最新收盘：供 Node 侧做「本地库 vs 线上」新鲜度校验
            codes = [c.strip() for c in parse_qs(p.query).get("codes", [""])[0].split(",") if c.strip()]
            rows = []
            try:
                with sqlite3.connect(DB_PATH, timeout=30) as conn:
                    for c in codes[:50]:
                        r = conn.execute(
                            "SELECT date, close FROM stock_daily WHERE symbol=? ORDER BY date DESC LIMIT 1",
                            (c,)).fetchone()
                        if r:
                            rows.append({"code": c, "date": r[0], "close": float(r[1])})
                self._send(200, {"ok": True, "rows": rows,
                                 "source": "本地SQLite（腾讯前复权）", "count": len(rows)})
            except Exception as e:
                self._send(200, {"ok": False, "rows": [], "message": str(e)})
        elif path == "/api/bs/quota":
            # baostock 访问额度与黑名单状态（官方：≤5 万次/日、禁止并发）
            try:
                import bs_guard
                self._send(200, dict(bs_guard.Guard().status(), ok=True))
            except Exception as e:
                self._send(200, {"ok": False, "message": "额度台账不可用：%s" % e})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        p = urlparse(self.path)
        qs = parse_qs(p.query)
        if p.path == "/api/scan":
            name = qs.get("name", ["all"])[0]
            relaxed = qs.get("relaxed", ["0"])[0].lower() in ("1", "true", "yes")
            self._send(200, do_scan(name, relaxed))
        elif p.path == "/api/backtest":
            hold = int(qs.get("hold", ["5"])[0])
            sample = int(qs.get("sample", ["15"])[0])
            threading.Thread(target=do_backtest, args=(60, hold, sample), daemon=True).start()
            self._send(202, {"status": "accepted", "message": "回测已在后台启动"})
        elif p.path == "/api/backfill":
            threading.Thread(target=backfill, daemon=True).start()
            self._send(202, {"status": "accepted", "message": "回填已在后台启动（baostock）"})
        elif p.path == "/api/scheduler/run":
            job = qs.get("job", ["sync"])[0]
            # 三个任务统一在这里登记：新增任务只加一行，不要在旁边再写一个 elif，
            # 否则「job 拼错」会静默跑到 sync 上去（原来 else 分支就是这样）。
            jobs = {
                "sync": (SCHEDULER.run_sync, "增量同步已在后台启动"),
                "scan": (SCHEDULER.run_scan, "扫描已在后台启动"),
                "factor": (lambda: SCHEDULER.run_factor(trigger="manual"),
                           "因子重算已在后台启动（全链路约 11 分钟）"),
            }
            if job not in jobs:
                self._send(400, {"ok": False, "busy": dict(SCHEDULER.busy),
                                 "message": "未知任务 %r；可选 sync / scan / factor" % job})
                return
            fn, msg = jobs[job]
            accepted = not bool(SCHEDULER.busy.get(job))
            if accepted:
                threading.Thread(target=fn, daemon=True).start()
            self._send(202, {
                "status": "accepted" if accepted else "busy",
                "accepted": accepted,
                "job": job,
                "message": msg if accepted else "%s 已在执行" % job,
                "busy": dict(SCHEDULER.busy),
            })
        elif p.path == "/api/scheduler/config":
            body = self._read_json()
            if body is None:
                self._send(400, {"ok": False, "message": "请求体需为合法 JSON"})
            else:
                cfg = SCHEDULER.save_cfg(body)
                self._send(200, {"ok": True, "config": cfg})
        else:
            self._send(404, {"error": "not found"})

    def _read_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8", "ignore"))
        except Exception:
            return None

    def log_message(self, *a):
        pass


def health():
    syms, latest, mode, src = [], None, "unknown", None
    try:
        syms = engine.get_local_symbols()
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute("SELECT MAX(date) FROM stock_daily").fetchone()
            latest = row[0] if row else None
            # 真实来源由 bulk_backfill 写入 meta.data_source（tencent / baostock）；
            # 没有 meta 表说明是早期 seed_demo 造的合成数据。
            try:
                r = conn.execute(
                    "SELECT value FROM meta WHERE key='data_source'"
                ).fetchone()
                src = r[0] if r else None
            except Exception:
                src = None
        if src in ("tencent", "baostock"):
            mode = "real"
        elif src:
            mode = src
        else:
            mode = "demo"
    except Exception:
        syms, latest = [], None
    return {
        "ok": True,
        "service": "sequoia-service",
        "db_path": DB_PATH,
        "symbols": len(syms),
        "latest_date": latest,
        "has_data": len(syms) > 0,
        "data_mode": mode,
        "data_source": src,
    }


def load_json(path):
    if not os.path.exists(path):
        return {"time": None, "strategies": [], "total": 0}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"time": None, "strategies": [], "total": 0}


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)

    # 定时调度：收盘后增量补数据 + 自动扫描
    try:
        SCHEDULER.set_scan_runner(do_scan)
        SCHEDULER.start()
    except Exception as exc:
        print(f"[sequoia-service] 调度器启动失败：{exc}")

    print(f"[sequoia-service] 监听 http://127.0.0.1:{PORT}  db={DB_PATH}")
    print(f"[sequoia-service] 已装策略: {', '.join(m[2] for m in STRATEGY_REGISTRY)}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        SCHEDULER.stop()
        srv.shutdown()


if __name__ == "__main__":
    main()
