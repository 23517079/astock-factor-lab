#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
seed_demo.py — 生成「真实代码 + 合成K线」的演示数据，便于本地联调（无需 baostock）。

关键点：**代码必须是真实存在的 A 股**，否则策略扫出来的标的点进个股分析会查不到。
做法：先从新浪取全市场真实清单（代码+名称），失败则退回内置真实清单；
K 线仍是随机游走（演示用），并额外写入 symbol_meta 表供前端显示股票名称。

用法：
    python seed_demo.py            # 200 只
    python seed_demo.py --n 100    # 指定只数

真实历史数据请用 baostock 回填：POST /api/backfill 或 python main.py --backfill
"""
import argparse
import json
import os
import random
import sqlite3
import sys
import urllib.request
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "data", "sequoia_v2.db")
random.seed(20261113)

# 新浪取不到时的兜底：200 只确定存在的 A 股（代码, 名称）
FALLBACK = [
    ("600519", "贵州茅台"), ("000001", "平安银行"), ("000002", "万科A"), ("600036", "招商银行"),
    ("601318", "中国平安"), ("000858", "五粮液"), ("002594", "比亚迪"), ("300750", "宁德时代"),
    ("600030", "中信证券"), ("601888", "中国中免"), ("000651", "格力电器"), ("000333", "美的集团"),
    ("600887", "伊利股份"), ("601166", "兴业银行"), ("600000", "浦发银行"), ("601398", "工商银行"),
    ("601288", "农业银行"), ("601988", "中国银行"), ("600016", "民生银行"), ("601939", "建设银行"),
    ("600104", "上汽集团"), ("601633", "长城汽车"), ("000625", "长安汽车"), ("600660", "福耀玻璃"),
    ("601012", "隆基绿能"), ("600438", "通威股份"), ("002129", "TCL中环"), ("300274", "阳光电源"),
    ("688981", "中芯国际"), ("688111", "金山办公"), ("002415", "海康威视"), ("000063", "中兴通讯"),
    ("600050", "中国联通"), ("601728", "中国电信"), ("600941", "中国移动"), ("600585", "海螺水泥"),
    ("601668", "中国建筑"), ("601390", "中国中铁"), ("600028", "中国石化"), ("601857", "中国石油"),
    ("601088", "中国神华"), ("600900", "长江电力"), ("600276", "恒瑞医药"), ("000538", "云南白药"),
    ("600196", "复星医药"), ("300760", "迈瑞医疗"), ("300124", "汇川技术"), ("002050", "三花智控"),
    ("600690", "海尔智家"), ("000100", "TCL科技"), ("002230", "科大讯飞"), ("300059", "东方财富"),
    ("601211", "国泰君安"), ("600837", "海通证券"), ("601601", "中国太保"), ("601628", "中国人寿"),
    ("600048", "保利发展"), ("001979", "招商蛇口"), ("600009", "上海机场"), ("601111", "中国国航"),
    ("600115", "中国东航"), ("601006", "大秦铁路"), ("600018", "上港集团"), ("601899", "紫金矿业"),
    ("600111", "北方稀土"), ("603993", "洛阳钼业"), ("601600", "中国铝业"), ("600362", "江西铜业"),
    ("000983", "山西焦煤"), ("601225", "陕西煤业"), ("600188", "兖矿能源"), ("002460", "赣锋锂业"),
    ("002466", "天齐锂业"), ("300014", "亿纬锂能"), ("002074", "国轩高科"), ("601127", "赛力斯"),
    ("600733", "北汽蓝谷"), ("000725", "京东方A"), ("002241", "歌尔股份"), ("002475", "立讯精密"),
    ("300433", "蓝思科技"), ("002916", "深南电路"), ("603501", "韦尔股份"), ("688008", "澜起科技"),
    ("688036", "传音控股"), ("688012", "中微公司"), ("300661", "圣邦股份"), ("603986", "兆易创新"),
    ("002371", "北方华创"), ("300223", "北京君正"), ("600703", "三安光电"), ("002049", "紫光国微"),
    ("600745", "闻泰科技"), ("000977", "浪潮信息"), ("603019", "中科曙光"), ("002439", "启明星辰"),
    ("300454", "深信服"), ("688169", "石头科技"), ("603833", "欧派家居"), ("000895", "双汇发展"),
    ("603288", "海天味业"), ("600298", "安琪酵母"), ("000568", "泸州老窖"), ("600809", "山西汾酒"),
    ("000596", "古井贡酒"), ("603369", "今世缘"), ("600600", "青岛啤酒"), ("600132", "重庆啤酒"),
    ("603711", "香飘飘"), ("002507", "涪陵榨菜"), ("600872", "中炬高新"), ("603517", "绝味食品"),
    ("002714", "牧原股份"), ("000876", "新希望"), ("300498", "温氏股份"), ("600598", "北大荒"),
    ("000998", "隆平高科"), ("600354", "敦煌种业"), ("601952", "苏垦农发"), ("600486", "扬农化工"),
    ("600096", "云天化"), ("000792", "盐湖股份"), ("600309", "万华化学"), ("002001", "新和成"),
    ("600426", "华鲁恒升"), ("600346", "恒力石化"), ("601233", "桐昆股份"), ("000301", "东方盛虹"),
    ("600985", "淮北矿业"), ("601898", "中煤能源"), ("600011", "华能国际"), ("600025", "华能水电"),
    ("600886", "国投电力"), ("601985", "中国核电"), ("601727", "上海电气"), ("600875", "东方电气"),
    ("601766", "中国中车"), ("600031", "三一重工"), ("000157", "中联重科"), ("600761", "安徽合力"),
    ("601100", "恒立液压"), ("002008", "大族激光"), ("300450", "先导智能"), ("688006", "杭可科技"),
    ("600893", "航发动力"), ("600760", "中航沈飞"), ("000768", "中航西飞"), ("002179", "中航光电"),
    ("600038", "中直股份"), ("600118", "中国卫星"), ("002025", "航天电器"), ("688396", "华润微"),
    ("600089", "特变电工"), ("002028", "思源电气"), ("601877", "正泰电器"), ("300207", "欣旺达"),
    ("002812", "恩捷股份"), ("300073", "当升科技"), ("688005", "容百科技"), ("002709", "天赐材料"),
    ("600884", "杉杉股份"), ("601865", "福莱特"), ("002459", "晶澳科技"), ("688599", "天合光能"),
    ("600732", "爱旭股份"), ("300118", "东方日升"), ("002506", "协鑫集成"), ("601615", "明阳智能"),
    ("002202", "金风科技"), ("300772", "运达股份"), ("600875", "东方电气"), ("601016", "节能风电"),
    ("600905", "三峡能源"), ("000155", "川能动力"), ("600795", "国电电力"), ("600236", "桂冠电力"),
    ("000539", "粤电力A"), ("600642", "申能股份"), ("000027", "深圳能源"), ("601991", "大唐发电"),
]


def fetch_real_list(need):
    """从新浪行情中心拉取真实 A 股清单（排除北交所 / ST / 退市）。"""
    out, seen = [], set()
    for page in range(1, 9):
        if len(out) >= need:
            break
        url = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
               "Market_Center.getHQNodeData?page=%d&num=100&sort=symbol&asc=1&node=hs_a" % page)
        req = urllib.request.Request(url, headers={
            "Referer": "https://finance.sina.com.cn",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        })
        try:
            with urllib.request.urlopen(req, timeout=12) as r:
                rows = json.loads(r.read().decode("utf-8", "ignore") or "[]")
        except Exception as e:
            print("  新浪第 %d 页失败：%s" % (page, e))
            break
        if not rows:
            break
        for x in rows:
            sym = str(x.get("symbol", ""))
            code = str(x.get("code", ""))
            name = str(x.get("name", ""))
            if not code or len(code) != 6:
                continue
            if sym.startswith("bj"):          # 北交所先跳过（腾讯行情覆盖不全）
                continue
            if "ST" in name.upper() or "退" in name:
                continue
            if code in seen:
                continue
            seen.add(code)
            out.append((code, name))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=200)
    args = ap.parse_args()

    codes = fetch_real_list(args.n)
    src = "新浪全市场清单"
    if len(codes) < 50:
        codes, src = FALLBACK[:args.n], "内置真实清单（新浪不可用）"
    codes = codes[:args.n]
    print("代码池来源：%s，共 %d 只（真实代码）" % (src, len(codes)))

    end = date(2026, 11, 12)
    dates = []
    d = end
    while len(dates) < 260:
        if d.weekday() < 5:
            dates.append(d.strftime("%Y-%m-%d"))
        d -= timedelta(days=1)
    dates = sorted(dates)

    os.makedirs(os.path.dirname(DB), exist_ok=True)
    conn = sqlite3.connect(DB)
    # 不删文件（部分环境禁止删除），直接重建表
    conn.execute("DROP TABLE IF EXISTS stock_daily")
    conn.execute("DROP TABLE IF EXISTS symbol_meta")
    conn.execute(
        "CREATE TABLE stock_daily (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "symbol TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, "
        "volume REAL, turnover REAL, UNIQUE(symbol,date))"
    )
    conn.execute(
        "CREATE TABLE symbol_meta (symbol TEXT PRIMARY KEY, name TEXT, source TEXT)"
    )
    conn.execute("CREATE INDEX idx_symbol ON stock_daily(symbol, date)")

    for code, name in codes:
        price = random.uniform(5, 80)
        drift = random.uniform(-0.0005, 0.0016)
        for dt in dates:
            change = random.gauss(drift, 0.02)
            open_ = price
            close = max(0.5, price * (1 + change))
            high = max(open_, close) * (1 + abs(random.gauss(0, 0.01)))
            low = min(open_, close) * (1 - abs(random.gauss(0, 0.01)))
            volume = random.uniform(1e6, 5e7)
            turnover = max(close * volume, random.uniform(0.5e8, 5e9))
            conn.execute(
                "INSERT OR IGNORE INTO stock_daily"
                "(symbol,date,open,high,low,close,volume,turnover) VALUES(?,?,?,?,?,?,?,?)",
                (code, dt, round(open_, 2), round(high, 2), round(low, 2),
                 round(close, 2), round(volume, 0), round(turnover, 2)),
            )
            price = close
        conn.execute("INSERT OR REPLACE INTO symbol_meta(symbol,name,source) VALUES(?,?,?)",
                     (code, name, src))

    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM stock_daily").fetchone()[0]
    print("seed done: %d 行, %d 只真实代码, %s ~ %s" % (n, len(codes), dates[0], dates[-1]))
    print("样例：", "、".join("%s %s" % (c, nm) for c, nm in codes[:6]))
    conn.close()


if __name__ == "__main__":
    main()
