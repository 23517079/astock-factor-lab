#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bulk_backfill.py — 全市场真实行情回填（断点续传 + 增量 + 额度闸门）

设计要点：
  * 清单来源：新浪行情中心一次拿 5000+ 只（15s），失败才退回 baostock.query_stock_basic。
  * 两个数据源，口径不同，不能混用：
      - tencent（默认）：前复权，无需登录，0.3s/只，多进程并发安全；
      - baostock（备源）：后复权 + 真实成交额，**必须单进程串行**，且受 5 万次/日额度限制。
  * baostock 禁止并发（官方「访问限制」明确写了），并发会直接进黑名单（10001011）。
    因此 --source baostock 时强制 workers=1，并通过 bs_guard 做节流与额度记账；
    一旦命中黑名单，立刻停止并写下解封时间，不再做任何重试。
  * 断点续传：按 symbol 的 MAX(date) 判断，中断后重跑只补缺口。
  * 增量：--incremental 只补每只票最新日期之后的部分（每日更新用这个）。
  * 长历史：--since YYYY-MM-DD 把起点前移（例如 --since 2021-01-01 拿约 5 年）。
    腾讯单次上限 660 条，长区间会自动分段拉取再合并去重（见 _tencent_segments）。

用法：
    python bulk_backfill.py --reset                     # 全量回填（腾讯源，4 分钟）
    python bulk_backfill.py --incremental               # 每日增量（腾讯源）
    python bulk_backfill.py --since 2021-01-01 --limit 3    # 长历史（5 年）小样本验证
    python bulk_backfill.py --probe                     # 只探活：baostock 是否已解封（1 次登录）
    python bulk_backfill.py --source baostock --limit 50   # 小批量验证 baostock 口径
    python bulk_backfill.py --source baostock --incremental --workers 8  # 会自动降到 1
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "data", "sequoia_v2.db")
PROGRESS = os.path.join(HERE, "data", "backfill_progress.json")

START_DATE = "2025-06-01"          # 约 370 个交易日：覆盖 RPS(120日+移位) 与海龟(20日) 需求，留足缓冲
# 腾讯 kline 接口单次最多返回 660 条，而 3 年约 730 个交易日，会截断。
# 因此分页按 2 年一段（约 490 个交易日）切，留足安全边界；
# 若某段仍返回满 660 条（极端情况，比如数据源多给），再对半拆重拉（见 _collect_segment）。
TENCENT_SEGMENT_DAYS = 730
TENCENT_MAX_BARS = 660
# --since 判定容差：库里的 MIN(date) 比 --since 晚几天是正常的（元旦/春节休市），
# 只要没晚出这个天数就不算「缺前段历史」，没必要为了几天差异把 5 年整段重拉。
SINCE_TOLERANCE_DAYS = 15

CREATE_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS stock_daily ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, date TEXT, "
    "open REAL, high REAL, low REAL, close REAL, volume REAL, turnover REAL, "
    "UNIQUE(symbol,date))"
)


# ── worker：模块级函数，Windows spawn 模式可 pickle ──
_BS_STATE = {"ready": False, "blacklist": False, "blacklist_msg": ""}


def _bs_login_once():
    """登录一次并复用。

    铁律（2026-11-13 用 8 进程并发登录把账号打进黑名单后立的）：
      · 全进程只允许一个 baostock 连接，绝不并发；
      · 登录失败最多再试 1 次，不做「4 次连打」——那等于自己刷接口；
      · 一旦看到 10001011（黑名单），立刻置位并返回，后续调用直接短路。
    """
    if _BS_STATE["ready"]:
        return True
    if _BS_STATE["blacklist"]:
        return False
    import socket
    import baostock as bs
    # baostock 自身没有超时机制，网络挂起会永久阻塞（实测卡死 20 分钟以上）。
    socket.setdefaulttimeout(40)

    for i in range(2):
        try:
            lg = bs.login()
        except Exception as exc:
            _BS_STATE["blacklist_msg"] = str(exc)[:200]
            if i == 0:
                time.sleep(3)
                continue
            return False
        code = getattr(lg, "error_code", "")
        if code == "0":
            _BS_STATE["ready"] = True
            return True
        if code == "10001011":
            _BS_STATE["blacklist"] = True
            _BS_STATE["blacklist_msg"] = getattr(lg, "error_msg", "黑名单用户")
            return False
        _BS_STATE["blacklist_msg"] = "%s %s" % (code, getattr(lg, "error_msg", ""))
        if i == 0:
            time.sleep(3)
    return False


def _tencent_segments(start, end):
    """把 [start, end] 切成若干段，每段不超过 TENCENT_SEGMENT_DAYS 天。

    腾讯 kline 的 date 区间参数形如 param=sh600519,day,2021-01-01,2022-06-30,660,qfq，
    实测可用；但单次最多 660 条，所以区间长度必须小于 660 个交易日，
    否则早段数据会被静默丢掉（这是最危险的一种「看起来成功但数据缺一段」）。
    """
    segs = []
    cur = date.fromisoformat(start)
    stop = date.fromisoformat(end)
    if cur > stop:
        return [(start, end)]
    while cur <= stop:
        try:
            seg_stop = cur + timedelta(days=TENCENT_SEGMENT_DAYS - 1)
        except OverflowError:                       # 理论上的日期越界兜底
            seg_stop = stop
        if seg_stop > stop:
            seg_stop = stop
        segs.append((cur.strftime("%Y-%m-%d"), seg_stop.strftime("%Y-%m-%d")))
        cur = seg_stop + timedelta(days=1)
    return segs


def _tencent_get_segment(sym, seg_start, seg_end):
    """拉取单个区间的原始 K 线项（腾讯格式），返回 (items, err)。

    items 元素形如 [date, open, close, high, low, volume(手), ...]；
    三个 host/接口轮换容错（301xxx 在带 web 前缀的 host 上会返回 501）。
    """
    import json
    import urllib.request

    urls = [
        "https://ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,%s,%s,%d,qfq"
        % (sym, seg_start, seg_end, TENCENT_MAX_BARS),
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,%s,%s,%d,qfq"
        % (sym, seg_start, seg_end, TENCENT_MAX_BARS),
        "https://ifzq.gtimg.cn/appstock/app/kline/kline?param=%s,day,%s,%s,%d"
        % (sym, seg_start, seg_end, TENCENT_MAX_BARS),
    ]
    last_err = "no data"
    for attempt in range(3):
        for url in urls:
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                    "Referer": "https://gu.qq.com/",
                })
                raw = urllib.request.urlopen(req, timeout=15).read()
                d = json.loads(raw.decode("utf-8", "ignore"))
                node = (d.get("data") or {}).get(sym) or {}
                k = node.get("qfqday") or node.get("day") or []
                if not k:
                    continue
                return k, None
            except Exception as exc:
                last_err = str(exc)[:120]
                continue
        time.sleep(1 + attempt)
    return [], last_err


def _collect_segment(sym, seg_start, seg_end, collected, depth=0):
    """递归收集一个区间的 K 线到 collected（day -> item），返回错误信息或 None。

    如果返回条数达到 660 上限，说明该区间被截断，对半拆开重拉（最多拆 3 层）。
    """
    items, err = _tencent_get_segment(sym, seg_start, seg_end)
    if not items:
        return err
    for it in items:
        if it and len(it) >= 6:
            collected[str(it[0])] = it
    if len(items) >= TENCENT_MAX_BARS and depth < 3:
        d0 = date.fromisoformat(seg_start)
        d1 = date.fromisoformat(seg_end)
        if d1 > d0:
            mid = d0 + timedelta(days=(d1 - d0).days // 2)
            left = (seg_start, mid.strftime("%Y-%m-%d"))
            right = ((mid + timedelta(days=1)).strftime("%Y-%m-%d"), seg_end)
            for s, e in (left, right):
                if s <= e:
                    sub_err = _collect_segment(sym, s, e, collected, depth + 1)
                    if sub_err:
                        return sub_err
    return None


def fetch_one_tencent(task):
    """腾讯前复权日 K（无需登录，0.3s/只，比 baostock 快 20 倍）。

    返回字段顺序与 baostock 版本对齐：
      [date, open, high, low, close, volume(股), amount(估算), turn(不可用)]
    注意：腾讯接口只给 date/open/close/high/low/volume(手)，没有成交额，
    这里用「成交量 × 当日均价(高+低+收)/3」估算 turnover，误差通常在 5% 以内，
    仅用于「成交额过亿」这类粗筛；真实成交额需 baostock（--source baostock）。

    长区间（--since）自动分页：见 _tencent_segments / _collect_segment，
    分段结果按日期合并去重，因此重复区间、段间重叠都不会产生重复行。
    """
    symbol, code6, start, end = task
    sym = ("sh" if code6.startswith("6") else "sz") + code6

    collected = {}
    last_err = "no data"
    for seg_start, seg_end in _tencent_segments(start, end):
        err = _collect_segment(sym, seg_start, seg_end, collected)
        if err:
            last_err = err

    if not collected:
        return symbol, [], last_err

    rows = []
    for day in sorted(collected.keys()):
        it = collected[day]
        if day < start or day > end:
            continue
        o, c, h, l, v = it[1], it[2], it[3], it[4], it[5]
        try:
            o, c, h, l, v = float(o), float(c), float(h), float(l), float(v)
        except Exception:
            continue
        vol_shares = v * 100                              # 手 -> 股
        avg_price = (h + l + c) / 3.0 if (h and l and c) else c
        amount = vol_shares * avg_price                   # 成交额估算
        rows.append([day, "%.4f" % o, "%.4f" % h, "%.4f" % l, "%.4f" % c,
                     "%.0f" % vol_shares, "%.2f" % amount, ""])
    return symbol, rows, None



def fetch_one_baostock(task):
    """baostock 单只拉取（后复权 + 真实成交额）。

    只能在**串行**流程里调用，且每次调用前由 bs_guard 记账/节流。
    任何形式的并发（多进程/多线程）都会触发官方风控——这是硬规则，不是建议。
    """
    symbol, bs_code, start, end = task
    import baostock as bs
    for attempt in range(2):
        if not _bs_login_once():
            return symbol, [], "__BLACKLIST__" if _BS_STATE["blacklist"] else "login_failed"
        try:
            rs = bs.query_history_k_data_plus(
                bs_code,
                "date,open,high,low,close,volume,amount,turn",
                start_date=start,
                end_date=end,
                frequency="d",
                adjustflag="1",          # 后复权
            )
        except Exception as exc:
            # 连接断了：置未登录，重连一次（这次重连也算一次请求）
            _BS_STATE["ready"] = False
            if attempt == 0:
                time.sleep(2)
                continue
            return symbol, [], str(exc)[:120]

        code = getattr(rs, "error_code", "0")
        if code == "10001011":
            _BS_STATE["blacklist"] = True
            return symbol, [], "__BLACKLIST__"
        if code != "0":
            return symbol, [], "%s %s" % (code, getattr(rs, "error_msg", ""))
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
        return symbol, rows, None
    return symbol, [], "retry_exhausted"


class _SerialIter(object):
    """把生成器包装成带 .next(timeout) 的迭代器，复用主循环的取数逻辑。"""

    def __init__(self, gen):
        self.gen = gen

    def next(self, timeout=None):
        return next(self.gen)


def iter_baostock(tasks):
    """串行拉取生成器：节流 → 记账 → 取数 → 命中黑名单立刻收工。

    产出 (symbol, rows, err)；err == "__BLACKLIST__" 时主循环会终止整轮。
    """
    import bs_guard
    g = bs_guard.Guard()
    try:
        g.require_can_run(len(tasks))
    except bs_guard.QuotaError as e:
        print("      拒绝运行：%s" % e, flush=True)
        write_progress(phase="refused", total=len(tasks), done=0, success=0, failed=0,
                       message=str(e), finished_at=datetime.now().isoformat(timespec="seconds"))
        return
    print("      串行模式：间隔 %.2fs，预计 %.0f 分钟；今日剩余额度 %s 次"
          % (bs_guard.MIN_INTERVAL, len(tasks) * (bs_guard.MIN_INTERVAL + 3) / 60.0,
             f"{g.remaining():,}"), flush=True)

    for task in tasks:
        if g.blacklist_until():
            yield task[0], [], "__BLACKLIST__"
            return
        if g.remaining() <= 0:
            print("      今日额度用尽，主动停止（已写入的数据保留，明天可续）", flush=True)
            return
        time.sleep(bs_guard.MIN_INTERVAL)
        g.spend(1)
        symbol, rows, err = fetch_one_baostock(task)
        if err == "__BLACKLIST__":
            until, hours = g.note_blacklist(_BS_STATE.get("blacklist_msg", ""))
            print("      命中黑名单（10001011）：%s" % _BS_STATE.get("blacklist_msg", ""), flush=True)
            print("      baostock 冻结至 %s（%.1f 小时），本轮终止"
                  % (until.strftime("%Y-%m-%d %H:%M"), hours), flush=True)
            yield symbol, [], "__BLACKLIST__"
            return
        if err:
            g.note_failure()
        yield symbol, rows, err


def write_progress(**kw):
    try:
        with open(PROGRESS, "w", encoding="utf-8") as f:
            json.dump(kw, f, ensure_ascii=False)
    except Exception:
        pass


UNIVERSE_CACHE = os.path.join(HERE, "data", "universe.json")


def get_universe(max_age_hours=20):
    """全市场上市 A 股清单（纯代码, 名称, baostock码）。

    清单走新浪行情中心（5000+ 只约 15s，比 baostock query_stock_basic 的 80s+ 快且稳），
    北交所先跳过（baostock 覆盖不全、策略也不用）。失败才退回 baostock。
    带 20 小时本地缓存。
    """
    try:
        if os.path.exists(UNIVERSE_CACHE):
            age_h = (time.time() - os.path.getmtime(UNIVERSE_CACHE)) / 3600.0
            if age_h < max_age_hours:
                with open(UNIVERSE_CACHE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if data and len(data) > 1000:
                    print("      使用本地清单缓存（%d 只，%.1f 小时前更新）"
                          % (len(data), age_h), flush=True)
                    return [tuple(x) for x in data]
    except Exception:
        pass

    out = _universe_from_sina()
    if len(out) < 1000:
        print("      新浪清单异常，退回 baostock…", flush=True)
        out = _universe_from_baostock()

    try:
        os.makedirs(os.path.dirname(UNIVERSE_CACHE), exist_ok=True)
        with open(UNIVERSE_CACHE, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)
    except Exception:
        pass
    return out


def _universe_from_sina():
    import urllib.request
    seen, out = set(), []
    for page in range(1, 70):
        url = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
               "Market_Center.getHQNodeData?page=%d&num=100&sort=symbol&asc=1&node=hs_a" % page)
        req = urllib.request.Request(url, headers={
            "Referer": "https://finance.sina.com.cn",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        })
        try:
            rows = json.loads(urllib.request.urlopen(req, timeout=12).read().decode("utf-8", "ignore") or "[]")
        except Exception:
            break
        if not rows:
            break
        for x in rows:
            sym = str(x.get("symbol", ""))
            name = str(x.get("name", ""))
            if len(sym) < 8:
                continue
            mkt, pure = sym[:2], sym[2:]
            if mkt not in ("sh", "sz"):       # 跳过北交所
                continue
            if not pure.isdigit() or len(pure) != 6 or pure in seen:
                continue
            if "ST" in name.upper() or "退" in name:
                continue
            seen.add(pure)
            out.append((pure, name, "%s.%s" % (mkt, pure)))
    return out


def _universe_from_baostock():
    """兜底清单（慢，80s+）。同样必须过额度闸门与黑名单判断。"""
    import bs_guard
    g = bs_guard.Guard()
    if g.blacklist_until():
        print("      baostock 仍在黑名单期内，跳过兜底清单", flush=True)
        return []
    if not _bs_login_once():
        if _BS_STATE["blacklist"]:
            g.note_blacklist(_BS_STATE.get("blacklist_msg", ""))
        return []
    g.note_login()
    g.spend(1)
    import baostock as bs
    out = []
    try:
        rs = bs.query_stock_basic(code_name="", code="")
        if getattr(rs, "error_code", "0") == "10001011":
            g.note_blacklist()
            return []
        while rs.next():
            row = rs.get_row_data()
            code, name = row[0], row[1]
            status = row[4] if len(row) > 4 else "1"
            stype = row[5] if len(row) > 5 else "1"
            if status != "1" or stype != "1" or not code.startswith(("sh.", "sz.")):
                continue
            out.append((code.split(".")[1], name, code))
    finally:
        try:
            bs.logout()
        except Exception:
            pass
    return out


def probe_baostock():
    """只做一次登录探活，不查任何数据（1 次请求）。

    用于黑名单解封后确认「真的能用了」，而不是直接跑 5000 只把额度又打穿。
    """
    import bs_guard
    g = bs_guard.Guard()
    st = g.status()
    print("baostock 探活 —— 今日已用 %s 次 / 剩余 %s 次"
          % (f'{st["used"]:,}', f'{st["remaining"]:,}'))
    if st["blacklist_until"]:
        print("  本地记录：黑名单至 %s（还剩 %.1f 小时），本年累计 %d 次"
              % (st["blacklist_until"], st["blacklist_remaining_hours"], st["blacklist_count_year"]))
        print("  仍可能被封，继续探测请谨慎（登录本身也算一次请求）。")
    ok = _bs_login_once()
    g.note_login()
    if ok:
        if g.clear_blacklist():
            print("  本地冻结记录已清除（探测确认可正常登录）。")
        print("  结果：登录成功，可以正常使用 baostock。")
        try:
            import baostock as bs
            bs.logout()
        except Exception:
            pass
        return 0
    if _BS_STATE["blacklist"]:
        until, hours = g.note_blacklist(_BS_STATE.get("blacklist_msg", ""))
        print("  结果：仍被黑名单控制（10001011 %s）" % _BS_STATE.get("blacklist_msg", ""))
        print("  官方规则：限制时长 = 本年累计次数 × 6 小时；本地记账冻结至 %s（%.1f 小时后）"
              % (until.strftime("%Y-%m-%d %H:%M"), hours))
        return 2
    print("  结果：登录失败（非黑名单）：%s" % (_BS_STATE.get("blacklist_msg") or "未知"))
    return 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="清空旧数据（含演示数据）后全量回填")
    ap.add_argument("--incremental", action="store_true", help="只补增量")
    ap.add_argument("--workers", type=int, default=8,
                    help="腾讯源并发进程数；baostock 源强制 1（官方禁止并发）")
    ap.add_argument("--source", choices=["tencent", "baostock"], default="tencent",
                    help="tencent=前复权无登录(默认，快)；baostock=后复权+真实成交额(需登录，有额度限制)")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 只（调试用）")
    ap.add_argument("--since", default=None,
                    help="回填起点 YYYY-MM-DD（如 2021-01-01 拿长历史）；不传则沿用 %s" % START_DATE)
    ap.add_argument("--probe", action="store_true", help="只探测 baostock 是否已解封，不拉数据")
    args = ap.parse_args()

    if args.since:
        try:
            since_date = date.fromisoformat(args.since)
        except ValueError:
            raise SystemExit("--since 需要 YYYY-MM-DD 格式，收到：%r" % args.since)
        if since_date > date.today():
            raise SystemExit("--since 不能晚于今天（%s）" % date.today().isoformat())
        args.since = since_date.strftime("%Y-%m-%d")

    if args.probe:
        raise SystemExit(probe_baostock())

    if args.source == "baostock":
        if args.workers != 1:
            print("      [安全] baostock 禁止并发，--workers=%d 已强制降为 1" % args.workers, flush=True)
            args.workers = 1
        import bs_guard
        try:
            bs_guard.Guard().require_can_run(0)
        except bs_guard.QuotaError as e:
            raise SystemExit("拒绝运行：%s" % e)

    t_all = time.time()
    end_date = date.today().strftime("%Y-%m-%d")

    print("[1/4] 获取全市场股票清单（新浪行情中心，失败回退 baostock）…", flush=True)
    universe = get_universe()
    if not universe:
        raise SystemExit("未取到股票清单，检查网络或 baostock 状态")
    print("      全市场上市 A 股：%d 只" % len(universe), flush=True)
    if args.limit:
        universe = universe[: args.limit]
        print("      --limit 生效，仅处理 %d 只" % len(universe), flush=True)

    os.makedirs(os.path.dirname(DB), exist_ok=True)
    conn = sqlite3.connect(DB, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(CREATE_TABLE_SQL)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS symbol_meta (symbol TEXT PRIMARY KEY, name TEXT, source TEXT)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_symbol_date ON stock_daily(symbol,date)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"
    )

    if args.reset:
        n0 = conn.execute("SELECT COUNT(*) FROM stock_daily").fetchone()[0]
        conn.execute("DELETE FROM stock_daily")
        conn.execute("DELETE FROM symbol_meta")
        conn.commit()
        print("      已清空旧数据 %d 行（含此前的演示数据）" % n0, flush=True)

    # 写入代码与名称
    conn.executemany(
        "INSERT OR REPLACE INTO symbol_meta(symbol,name,source) VALUES(?,?,?)",
        [(s, nm, args.source) for s, nm, _ in universe],
    )
    conn.commit()

    print("[2/4] 计算每只票的缺口（断点续传 / 增量）…", flush=True)
    last = dict(conn.execute("SELECT symbol, MAX(date) FROM stock_daily GROUP BY symbol").fetchall())
    first = dict(conn.execute("SELECT symbol, MIN(date) FROM stock_daily GROUP BY symbol").fetchall())
    base_start = args.since or START_DATE
    if args.since:
        print("      --since 生效：起点 %s（已有更早数据则整段重拉，INSERT OR REPLACE 幂等）"
              % args.since, flush=True)
    tasks = []
    skipped = 0
    for pure, name, bs_code in universe:
        ld = last.get(pure)
        fd = first.get(pure)
        # 是否需要「向前补历史」：库里有数据，但起点明显晚于 --since。
        # 晚几天（元旦/春节休市）不算缺段，用 SINCE_TOLERANCE_DAYS 容差吸收，
        # 否则每次带 --since 重跑都会把整段 5 年再拉一遍。
        # 真正需要补历史时整段拉 base_start~今天，靠 INSERT OR REPLACE 覆盖重叠（幂等）。
        need_history = False
        if args.since and fd:
            gap_days = (date.fromisoformat(fd) - date.fromisoformat(args.since)).days
            need_history = gap_days > SINCE_TOLERANCE_DAYS
        if ld and ld >= end_date and not need_history:
            skipped += 1
            continue
        if ld and not need_history:
            start = (date.fromisoformat(ld) + timedelta(days=1)).strftime("%Y-%m-%d")
        else:
            start = base_start
        if start > end_date:
            skipped += 1
            continue
        tasks.append((pure, bs_code if args.source == "baostock" else pure, start, end_date))
    print("      待拉取 %d 只，已最新 %d 只" % (len(tasks), skipped), flush=True)

    if not tasks:
        print("      全部已是最新，无需回填。", flush=True)
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('data_source',?)", (args.source,))
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('last_sync',?)", (datetime.now().isoformat(timespec="seconds"),))
        conn.commit()
        conn.close()
        write_progress(phase="done", total=0, done=0, success=0, failed=0,
                       message="全部已是最新", finished_at=datetime.now().isoformat(timespec="seconds"))
        return

    total = len(tasks)
    done = success = failed = 0
    inserted = 0
    failed_list = []
    t0 = time.time()
    write_progress(phase="running", total=total, done=0, success=0, failed=0,
                   started_at=datetime.now().isoformat(timespec="seconds"))

    buf = []
    last_flush = [time.time()]
    insert_sql = (
        "INSERT OR REPLACE INTO stock_daily"
        "(symbol,date,open,high,low,close,volume,turnover) VALUES(?,?,?,?,?,?,?,?)"
    )

    def flush(force=False):
        nonlocal buf, inserted
        due = (time.time() - last_flush[0]) > 30
        if len(buf) >= 5000 or (force and buf) or (due and buf):
            conn.executemany(insert_sql, buf)
            conn.commit()
            inserted += len(buf)
            buf = []
            last_flush[0] = time.time()

    last_progress = [time.time()]
    last_done = [0]

    if args.source == "baostock":
        print("[3/4] 串行拉取（baostock 禁止并发，1 进程 + 节流）…", flush=True)
        it = _SerialIter(iter_baostock(tasks))
        pool = None
    else:
        from multiprocessing import Pool
        print("[3/4] 并发拉取（%d 进程，腾讯源）…" % args.workers, flush=True)
        pool = Pool(processes=args.workers)
        it = pool.imap_unordered(fetch_one_tencent, tasks, chunksize=1)

    from multiprocessing import TimeoutError as MPTimeout
    blacklisted = False
    try:
        stalls = 0
        while True:
            try:
                symbol, rows, err = it.next(timeout=90)
            except MPTimeout:
                # 腾讯源偶发慢请求，靠超时自愈；连续多次无结果说明整体已废，主动退出保留断点。
                stalls += 1
                print("      警告：90s 无结果，判定有 worker 卡住（第 %d 次）" % stalls, flush=True)
                if stalls >= 6:
                    print("      连续 6 次无结果，终止本轮；已写入数据保留，重跑可续传", flush=True)
                    break
                continue
            except StopIteration:
                break
            stalls = 0
            done += 1
            if err == "__BLACKLIST__":
                blacklisted = True
                failed += 1
                failed_list.append({"symbol": symbol, "error": "baostock 黑名单，已停止"})
                break
            if err:
                failed += 1
                failed_list.append({"symbol": symbol, "error": err[:120]})
            elif rows:
                success += 1
                for r in rows:
                    # r = date,open,high,low,close,volume,amount,turn
                    if len(r) < 8 or not r[4]:
                        continue
                    try:
                        buf.append((symbol, r[0], float(r[1]), float(r[2]), float(r[3]),
                                    float(r[4]), float(r[5] or 0), float(r[6] or 0)))
                    except Exception:
                        continue
            else:
                success += 1          # 无新数据（停牌等），算成功
            flush()
            if done % 50 == 0 or done == total:
                elapsed = time.time() - t0
                rate = done / elapsed if elapsed else 0
                eta = (total - done) / rate if rate else 0
                write_progress(phase="running", total=total, done=done, success=success,
                               failed=failed, inserted=inserted + len(buf),
                               rate=round(rate, 2), eta_sec=round(eta),
                               started_at=datetime.fromtimestamp(t0).isoformat(timespec="seconds"))
                print("      %d/%d  成功 %d 失败 %d  写入 %s 行  %.1f 只/秒  剩余 %.0f 分钟"
                      % (done, total, success, failed, f"{inserted + len(buf):,}",
                         rate, eta / 60), flush=True)
    finally:
        if pool is not None:
            try:
                pool.terminate()
                pool.join()
            except Exception:
                pass
    flush(force=True)

    n = conn.execute("SELECT COUNT(*) FROM stock_daily").fetchone()[0]
    syms = conn.execute("SELECT COUNT(DISTINCT symbol) FROM stock_daily").fetchone()[0]
    dates = conn.execute("SELECT MIN(date), MAX(date) FROM stock_daily").fetchone()
    conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('data_source',?)", (args.source,))
    conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('last_sync',?)",
                 (datetime.now().isoformat(timespec="seconds"),))
    conn.commit()
    conn.close()

    cost = time.time() - t_all
    if blacklisted:
        print("[4/4] 中止：命中 baostock 黑名单（10001011），本轮只写入 %s 行" % f"{n:,}", flush=True)
        print("      已写入的数据保留，解封后重跑可续传；期间请用腾讯源 --source tencent", flush=True)
    else:
        print("[4/4] 完成：%d 只 / %s 行 / %s ~ %s，耗时 %.1f 分钟"
              % (syms, f"{n:,}", dates[0], dates[1], cost / 60), flush=True)
    if failed_list:
        print("      失败 %d 只（可重跑续传）：%s" % (len(failed_list),
                                                 ", ".join(x["symbol"] for x in failed_list[:15])), flush=True)
    write_progress(phase="aborted" if blacklisted else "done",
                   total=total, done=done, success=success, failed=failed,
                   inserted=n, symbols=syms, date_range=list(dates), cost_sec=round(cost),
                   failed_samples=failed_list[:50],
                   message="baostock 黑名单，已停止" if blacklisted else None,
                   finished_at=datetime.now().isoformat(timespec="seconds"))
    if blacklisted:
        raise SystemExit(3)


if __name__ == "__main__":
    main()
