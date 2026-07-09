# -*- coding: utf-8 -*-
"""
台股訊號每日更新腳本
====================
條件一：近5個交易日外資買超前10名 + 先前連續賣超>=3日後轉買 + 週線由下轉彎向上
條件二：00991A(主動復華未來50) 較前一日加碼(含新增)的持股 + 週線由下轉彎向上

資料來源：
- 外資買賣超：證交所 T86 (上市)  https://www.twse.com.tw/rwd/zh/fund/T86
- 股價(算週線)：證交所 STOCK_DAY (逐月)；上櫃股票嘗試 TPEx API
- 00991A 持股：復華投信網站(需在 config 設定端點，見 README)，或 data/manual_holdings.csv 手動模式

輸出：data/latest.json （供 index.html 讀取）
"""
VERSION = "v7"

import json
import os
import re
import sys
import time
import urllib.request
import urllib.parse
import http.cookiejar
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------- 基本設定
TZ_TAIPEI = timezone(timedelta(hours=8))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
T86_DIR = os.path.join(DATA_DIR, "t86")
PRICE_DIR = os.path.join(DATA_DIR, "prices")
HOLD_DIR = os.path.join(DATA_DIR, "holdings")

WEEK_WINDOW = 5          # 「每週」= 近 5 個交易日累計
SELL_STREAK_MIN = 3      # 轉買前需連續賣超天數
LOOKBACK_DAYS = 14       # 追蹤外資買賣超的交易日數(需 > 週窗口+賣超天數)
TOP_N = 10               # 外資週買超前 N 名
PRICE_MONTHS = 3         # 抓幾個月股價來算週線
REQ_DELAY = 0.6          # 對交易所 API 的禮貌延遲(秒)

# ---- 追蹤的主動式ETF清單 ----------------------------------------
# 00991A(主動復華未來50)：復華 PCF API，pcfDate 每天自動帶入
# 00993A(主動安聯台灣)：安聯 POST API，Date=null 自動回傳最新資料
PCF_TEMPLATE = ("https://www.fhtrust.com.tw/api/ETFPcf"
                "?fundID=ETF23&pcfDate={date}")
PCF_URL = os.environ.get("PCF_URL", "").strip()          # 00991A 覆寫用
ALLIANZ_API = "https://etf.allianzgi.com.tw/webapi/api/Fund/GetFundTradeInfo"

FUNDS = [
    {"id": "00991A", "label": "主動復華未來50", "source": "fh",
     "unit": "股/基數"},
    {"id": "00993A", "label": "主動安聯台灣", "source": "allianz",
     "fund_no": "E0002", "unit": "股"},
]
MANUAL_HOLDINGS = os.path.join(DATA_DIR, "manual_holdings.csv")

UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-TW,zh;q=0.9",
}
LAST_HTTP_ERROR = {"msg": ""}
COOKIES = http.cookiejar.CookieJar()
OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(COOKIES))


def http_get(url, retries=3, referer=None, xhr=False):
    headers = dict(UA)
    if referer:
        headers["Referer"] = referer
    if xhr:
        headers["X-Requested-With"] = "XMLHttpRequest"
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with OPENER.open(req, timeout=30) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception as e:  # noqa
            last = e
            time.sleep(2 + i * 2)
    LAST_HTTP_ERROR["msg"] = f"GET {url} -> {last}"
    print(f"[warn] GET 失敗 {url}: {last}")
    return None


def http_post_json(url, payload, retries=3, referer=None, extra_headers=None):
    """以 JSON body 發送 POST(安聯 API 使用)"""
    body = json.dumps(payload).encode("utf-8")
    headers = dict(UA)
    headers["Content-Type"] = "application/json;charset=UTF-8"
    if extra_headers:
        headers.update(extra_headers)
    if referer:
        headers["Referer"] = referer
        headers["Origin"] = re.match(r"https?://[^/]+", referer).group(0)
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, data=body, headers=headers,
                                         method="POST")
            with OPENER.open(req, timeout=30) as r:
                return r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")[:150]
            except Exception:
                pass
            last = f"HTTP {e.code} {detail}"
            print(f"[diag] POST {payload} -> {last}")
            if e.code in (400, 403, 404, 415):
                break  # 格式問題重試無用，換下一種格式
            time.sleep(2 + i * 2)
        except Exception as e:  # noqa
            last = e
            time.sleep(2 + i * 2)
    LAST_HTTP_ERROR["msg"] = f"POST {url} -> {last}"
    return None


def num(s):
    """'1,234' -> 1234.0；空字串/-- -> None"""
    if s is None:
        return None
    s = str(s).replace(",", "").replace("+", "").strip()
    if s in ("", "--", "-", "N/A", "X"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


# ---------------------------------------------------------------- T86 外資買賣超
def fetch_t86(date_str):
    """抓某日上市全部個股外資買賣超；回傳 {code: {"name":..,"net":股數}} 或 None(非交易日)"""
    cache = os.path.join(T86_DIR, f"{date_str}.json")
    if os.path.exists(cache):
        with open(cache, encoding="utf-8") as f:
            return json.load(f)
    url = ("https://www.twse.com.tw/rwd/zh/fund/T86"
           f"?date={date_str}&selectType=ALLBUT0999&response=json")
    raw = http_get(url)
    time.sleep(REQ_DELAY)
    if not raw:
        return None
    try:
        j = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if j.get("stat") != "OK" or not j.get("data"):
        return None  # 非交易日或尚未公布
    fields = j["fields"]
    try:
        i_code = fields.index("證券代號")
        i_name = fields.index("證券名稱")
        i_net = fields.index("外陸資買賣超股數(不含外資自營商)")
    except ValueError:
        i_code, i_name, i_net = 0, 1, 4
    out = {}
    for row in j["data"]:
        code = str(row[i_code]).strip()
        if not re.fullmatch(r"\d{4}", code):   # 只留普通股(4碼)，排除ETF/權證等
            continue
        n = num(row[i_net])
        if n is None:
            continue
        out[code] = {"name": str(row[i_name]).strip(), "net": n}
    with open(cache, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    return out


def collect_t86_history(today):
    """往回抓到 LOOKBACK_DAYS 個有效交易日，回傳 [(date_str, {code:{name,net}})] 新->舊"""
    days = []
    d = today
    tries = 0
    while len(days) < LOOKBACK_DAYS and tries < 40:
        ds = d.strftime("%Y%m%d")
        data = fetch_t86(ds)
        if data:
            days.append((ds, data))
        d -= timedelta(days=1)
        tries += 1
    return days


# ---------------------------------------------------------------- 股價 -> 週線
def fetch_month_prices_twse(code, ym):
    """證交所 STOCK_DAY，回傳 [(date, close)]，含快取"""
    cache = os.path.join(PRICE_DIR, f"{code}_{ym}.json")
    # 當月資料會持續變動，不快取當月
    is_cur = ym == datetime.now(TZ_TAIPEI).strftime("%Y%m")
    if os.path.exists(cache) and not is_cur:
        with open(cache, encoding="utf-8") as f:
            return json.load(f)
    url = ("https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY"
           f"?date={ym}01&stockNo={code}&response=json")
    raw = http_get(url)
    time.sleep(REQ_DELAY)
    rows = []
    if raw:
        try:
            j = json.loads(raw)
            if j.get("stat") == "OK":
                for r in j.get("data", []):
                    # 日期為民國年 114/07/01
                    y, m, dd = r[0].split("/")
                    date = f"{int(y)+1911:04d}{m}{dd}"
                    c = num(r[6])
                    if c:
                        rows.append([date, c])
        except Exception:
            pass
    if rows and not is_cur:
        with open(cache, "w", encoding="utf-8") as f:
            json.dump(rows, f)
    return rows


def fetch_month_prices_tpex(code, ym):
    """上櫃股價(TPEx)。API 版本較常變動，失敗就回空list"""
    y, m = ym[:4], ym[4:6]
    url = ("https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock"
           f"?code={code}&date={y}/{m}/01&response=json")
    raw = http_get(url)
    time.sleep(REQ_DELAY)
    rows = []
    if raw:
        try:
            j = json.loads(raw)
            tables = j.get("tables") or []
            data = tables[0].get("data", []) if tables else j.get("aaData", [])
            for r in data:
                y2, m2, d2 = str(r[0]).split("/")
                date = f"{int(y2)+1911:04d}{m2}{d2}"
                c = num(r[6])
                if c:
                    rows.append([date, c])
        except Exception:
            pass
    return rows


def get_daily_closes(code):
    """近 PRICE_MONTHS 個月日收盤，[(yyyymmdd, close)] 舊->新"""
    now = datetime.now(TZ_TAIPEI)
    months = []
    y, m = now.year, now.month
    for _ in range(PRICE_MONTHS):
        months.append(f"{y:04d}{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    months.reverse()
    rows = []
    for ym in months:
        r = fetch_month_prices_twse(code, ym)
        if not r:
            r = fetch_month_prices_tpex(code, ym)
        rows.extend(r)
    rows.sort(key=lambda x: x[0])
    return rows


def weekly_closes(daily):
    """依 ISO 週取每週最後收盤，回傳 [(iso_week_label, close)] 舊->新"""
    weeks = {}
    order = []
    for date, close in daily:
        dt = datetime.strptime(date, "%Y%m%d")
        key = "{0}-W{1:02d}".format(*dt.isocalendar()[:2])
        if key not in weeks:
            order.append(key)
        weeks[key] = close
    return [(k, weeks[k]) for k in order]


def weekly_turn_up(daily):
    """
    週線由下轉彎向上：
      本週(最新)收盤 > 上週收盤，且 上週收盤 <= 上上週收盤 (V 轉)
    回傳 (bool, 最近6週收盤list, 說明)
    """
    wc = weekly_closes(daily)
    tail = [round(c, 2) for _, c in wc[-6:]]
    if len(wc) < 3:
        return False, tail, "週資料不足"
    w1, w2, w3 = wc[-1][1], wc[-2][1], wc[-3][1]
    ok = (w1 > w2) and (w2 <= w3)
    desc = f"上上週{w3:g} → 上週{w2:g} → 本週{w1:g}"
    return ok, tail, desc


# ---------------------------------------------------------------- 條件一
def condition1(t86_days):
    """t86_days: [(date,{code:{name,net}})] 新->舊"""
    if len(t86_days) < WEEK_WINDOW + SELL_STREAK_MIN:
        return [], [], "外資買賣超歷史資料不足，需累積 %d 個交易日" % (WEEK_WINDOW + SELL_STREAK_MIN)

    week = t86_days[:WEEK_WINDOW]
    sums, names = {}, {}
    for _, day in week:
        for code, v in day.items():
            sums[code] = sums.get(code, 0) + v["net"]
            names[code] = v["name"]
    top10 = sorted(((c, s) for c, s in sums.items() if s > 0),
                   key=lambda x: -x[1])[:TOP_N]

    results, raw = [], []
    for code, wsum in top10:
        # 逐日外資買賣超序列(新->舊)
        seq = []
        for _, day in t86_days:
            seq.append(day.get(code, {}).get("net", 0))
        # 最近的連續買超天數
        buy_streak = 0
        i = 0
        while i < len(seq) and seq[i] > 0:
            buy_streak += 1
            i += 1
        # 買超之前的連續賣超天數
        sell_streak = 0
        while i < len(seq) and seq[i] < 0:
            sell_streak += 1
            i += 1
        flip = buy_streak >= 1 and sell_streak >= SELL_STREAK_MIN

        item = {
            "code": code, "name": names[code],
            "weekly_net_lots": round(wsum / 1000),      # 張
            "buy_streak": buy_streak, "sell_streak": sell_streak,
            "flip": flip, "turn": None, "weekly_closes": [], "turn_desc": "",
        }
        if flip:
            daily = get_daily_closes(code)
            ok, tail, desc = weekly_turn_up(daily)
            item["turn"], item["weekly_closes"], item["turn_desc"] = ok, tail, desc
            if ok:
                results.append(item)
        raw.append(item)
    return results, raw, None


# ---------------------------------------------------------------- 條件二：00991A
def parse_holdings(text):
    """
    盡量通吃各種格式：JSON(list/dict) 或 HTML 表格 或 CSV。
    回傳 {code: {"name":.., "shares": 股數}}
    """
    out = {}
    text = text.strip()
    # 1) JSON
    try:
        j = json.loads(text)
        def walk(o):
            if isinstance(o, dict):
                keys = {k.lower(): k for k in o.keys()}
                code_k = next((keys[k] for k in keys if k in
                               ("code", "stockcode", "stkcode", "股票代號", "證券代號", "stock_id")), None)
                if code_k:
                    code = str(o[code_k]).strip()
                    if re.fullmatch(r"\d{4,6}[A-Z]?", code):
                        name_k = next((keys[k] for k in keys if "name" in k or "名稱" in k), None)
                        sh_k = next((keys[k] for k in keys if "share" in k or "股數" in k
                                     or k in ("qty", "quantity", "amount", "units")), None)
                        shares = num(o.get(sh_k)) if sh_k else None
                        if shares:
                            out[code] = {"name": str(o.get(name_k, "")).strip(), "shares": shares}
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)
        walk(j)
        if out:
            return out
    except Exception:
        pass
    # 2) CSV：代號,名稱,股數 或 代號,股數
    if "," in text and "<" not in text:
        for line in text.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 2 and re.fullmatch(r"\d{4,6}[A-Z]?", parts[0]):
                shares = num(parts[-1])
                name = parts[1] if len(parts) >= 3 else ""
                if shares:
                    out[parts[0]] = {"name": name, "shares": shares}
        if out:
            return out
    # 3) HTML 表格
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", text, re.S | re.I)
    for row in rows:
        cells = [re.sub(r"<[^>]+>", "", c).strip()
                 for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S | re.I)]
        if len(cells) >= 2 and re.fullmatch(r"\d{4,6}[A-Z]?", cells[0]):
            nums = [num(c) for c in cells[1:]]
            nums = [n for n in nums if n and n > 1000]  # 股數通常很大
            if nums:
                out[cells[0]] = {"name": cells[1] if not num(cells[1]) else "",
                                 "shares": max(nums)}
    return out


def parse_fh_pcf(text):
    """
    復華 PCF 格式(容許外層再包一層 result 清單)：
      {...,"postDate":"2026/07/10","result":[{"aType":"股票","id":"2330 TT",
       "name":"台積電...","share":1234}, ...]}
    遞迴走訪整份 JSON，收集所有 id 為「XXXX TT」的台股。
    回傳 ({code:{name,shares}}, postDate字串或None)
    """
    try:
        j = json.loads(text)
    except Exception:
        return {}, None
    out = {}
    post = {"v": None}

    def walk(o):
        if isinstance(o, dict):
            if not post["v"] and o.get("postDate"):
                m = re.match(r"(\d{4})/(\d{2})/(\d{2})", str(o["postDate"]))
                if m:
                    post["v"] = "".join(m.groups())
            raw_id = str(o.get("id", "")).strip()
            m = re.match(r"^(\d{4,6}[A-Z]?)\s+TT$", raw_id)
            if m and o.get("aType") in (None, "股票"):
                shares = num(o.get("share"))
                if shares and shares > 0:
                    out[m.group(1)] = {"name": str(o.get("name", "")).strip(),
                                       "shares": shares}
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(j)
    return out, post["v"]


def fill_date_placeholders(url):
    """支援 {date}=2026/07/10、{date_dash}=2026-07-10、{yyyymmdd}=20260710"""
    now = datetime.now(TZ_TAIPEI)
    return (url.replace("{date}", now.strftime("%Y/%m/%d"))
               .replace("{date_dash}", now.strftime("%Y-%m-%d"))
               .replace("{yyyymmdd}", now.strftime("%Y%m%d")))


def fetch_holdings_env(env_name):
    """由環境變數提供的持股 JSON/HTML 網址(如安聯 00993A)"""
    url = os.environ.get(env_name, "").strip()
    if not url:
        return {}, "not_configured", None
    url = fill_date_placeholders(url)
    raw = http_get(url)
    if not raw:
        return {}, "fetch_failed", None
    h, post = parse_allianz_pcf(raw)     # 先試安聯表格式格式
    if not h:
        h, post = parse_fh_pcf(raw)      # 再試復華式格式
    if not h:
        h = parse_holdings(raw)          # 最後用通用 JSON/CSV/HTML 解析
        post = None
    return (h, "env_url", post) if h else ({}, "parse_failed", None)


def extract_csrf_tokens(html, cookies):
    """從頁面HTML與cookie中收集可能的anti-forgery token"""
    tokens = {}
    if html:
        m = re.search(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"',
                      html)
        if m:
            tokens["RequestVerificationToken"] = m.group(1)
        m = re.search(r'<meta[^>]+name="csrf-token"[^>]+content="([^"]+)"',
                      html, re.I)
        if m:
            tokens["X-CSRF-TOKEN"] = m.group(1)
    for c in cookies:
        if "XSRF" in c.name.upper():
            tokens["X-XSRF-TOKEN"] = c.value
        elif "CSRF" in c.name.upper():
            tokens.setdefault("X-CSRF-TOKEN", c.value)
    return tokens


def fetch_holdings_allianz(fund_no):
    """00993A：安聯 POST API。先暖身取cookie/token，再輪試多種參數格式"""
    override = os.environ.get("ALLIANZ_PCF_URL", "").strip()
    url = fill_date_placeholders(override) if override else ALLIANZ_API
    ref = "https://etf.allianzgi.com.tw/list-trade"
    page = http_get(ref)               # cookie 暖身
    time.sleep(REQ_DELAY)
    tokens = extract_csrf_tokens(page, COOKIES)
    print(f"[diag] 安聯cookie: {[c.name for c in COOKIES]} token: {list(tokens)}")
    today = datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d")
    variants = [
        {"Date": None, "FundNo": fund_no},
        {"date": None, "fundNo": fund_no},
        {"Date": "", "FundNo": fund_no},
        {"Date": today, "FundNo": fund_no},
    ]
    for payload in variants:
        raw = http_post_json(url, payload, referer=ref, extra_headers=tokens)
        time.sleep(REQ_DELAY)
        if raw:
            h, post = parse_allianz_pcf(raw)
            if h:
                return h, "allianz_api", post
            print(f"[diag] 安聯回應無持股: {raw.strip()[:120]}")
    return {}, "fetch_failed", None


def fetch_holdings_for(fund):
    """依基金設定取得持股，優先讀 data/manual_holdings_<id>.csv 手動檔"""
    manual = os.path.join(DATA_DIR, f"manual_holdings_{fund['id']}.csv")
    if os.path.exists(manual):
        with open(manual, encoding="utf-8") as f:
            h = parse_holdings(f.read())
        if h:
            return h, "manual_csv", None
    if fund["source"] == "fh":
        return fetch_holdings_fh()
    if fund["source"] == "allianz":
        return fetch_holdings_allianz(fund["fund_no"])
    if fund["source"] == "env":
        return fetch_holdings_env(fund["env"])
    return {}, "unknown_source", None


def migrate_old_snapshots():
    """把舊版存在 holdings/ 根目錄的 00991A 快照搬進子資料夾"""
    dst = os.path.join(HOLD_DIR, "00991A")
    os.makedirs(dst, exist_ok=True)
    for f in os.listdir(HOLD_DIR):
        p = os.path.join(HOLD_DIR, f)
        if f.endswith(".json") and os.path.isfile(p):
            os.replace(p, os.path.join(dst, f))


def track_fund(fund, today_str):
    """
    比對某檔主動式ETF前後兩份持股快照，找出加碼(含新進場)且週線翻揚者。
    share 若為每申購基數股數，比例仍正確——增加即代表權重提升(加碼)。
    """
    holdings, source, post_date = fetch_holdings_for(fund)
    status = {"source": source, "count": len(holdings)}
    out = {"id": fund["id"], "label": fund["label"], "unit": fund["unit"],
           "matched": [], "status": status, "error": None}
    if not holdings:
        if source == "not_configured":
            out["error"] = (f"尚未設定 {fund.get('env','')} 資料網址，"
                            "請見 README 設定後即可開始追蹤")
        else:
            detail = LAST_HTTP_ERROR["msg"][:160]
            out["error"] = (f"無法取得 {fund['id']} 持股資料，明日將自動重試"
                            + (f"（{detail}）" if detail else ""))
        return out
    snap_date = post_date or today_str
    status["pcf_date"] = snap_date
    fund_dir = os.path.join(HOLD_DIR, fund["id"])
    os.makedirs(fund_dir, exist_ok=True)
    with open(os.path.join(fund_dir, f"{snap_date}.json"), "w",
              encoding="utf-8") as f:
        json.dump(holdings, f, ensure_ascii=False)
    prev_files = sorted(f for f in os.listdir(fund_dir)
                        if f.endswith(".json") and f < f"{snap_date}.json")
    if not prev_files:
        out["error"] = "首次建立持股快照，下個交易日起開始比對加碼"
        return out
    with open(os.path.join(fund_dir, prev_files[-1]), encoding="utf-8") as f:
        prev = json.load(f)
    status["compare_with"] = prev_files[-1].replace(".json", "")

    results = []
    for code, v in holdings.items():
        if not re.fullmatch(r"\d{4}", code):
            continue
        before = prev.get(code, {}).get("shares", 0)
        change = v["shares"] - before
        if change <= 0:
            continue
        add_pct = round(change / before * 100, 1) if before else None
        daily = get_daily_closes(code)
        ok, tail, desc = weekly_turn_up(daily)
        item = {"code": code, "name": v["name"],
                "prev_shares": int(before), "now_shares": int(v["shares"]),
                "add_shares": int(change), "add_pct": add_pct,
                "is_new": before == 0,
                "turn": ok, "weekly_closes": tail, "turn_desc": desc}
        if ok:
            results.append(item)
    results.sort(key=lambda x: (-(x["add_pct"] if x["add_pct"] is not None else 9999),))
    out["matched"] = results
    return out



# ---------------------------------------------------------------- main
def main():
    for d in (T86_DIR, PRICE_DIR, HOLD_DIR):
        os.makedirs(d, exist_ok=True)
    migrate_old_snapshots()
    now = datetime.now(TZ_TAIPEI)
    today_str = now.strftime("%Y%m%d")

    print("== 抓取外資買賣超歷史 ==")
    t86_days = collect_t86_history(now)
    trade_date = t86_days[0][0] if t86_days else None
    print(f"取得 {len(t86_days)} 個交易日，最新 {trade_date}")

    print("== 條件一：外資轉買 + 週線翻揚 ==")
    c1, c1_raw, c1_err = condition1(t86_days)

    funds_out = []
    for fund in FUNDS:
        print(f"== {fund['id']} {fund['label']} 加碼 + 週線翻揚 ==")
        funds_out.append(track_fund(fund, trade_date or today_str))

    out = {
        "version": VERSION,
        "generated_at": now.strftime("%Y-%m-%d %H:%M"),
        "trade_date": trade_date,
        "params": {"week_window": WEEK_WINDOW, "sell_streak_min": SELL_STREAK_MIN,
                   "top_n": TOP_N},
        "condition1": {"matched": c1, "top10": c1_raw, "error": c1_err},
        "funds": funds_out,
    }
    with open(os.path.join(DATA_DIR, "latest.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("已寫入 data/latest.json")
    print(f"條件一符合 {len(c1)} 檔；" +
          "；".join(f"{f['id']} 符合 {len(f['matched'])} 檔" for f in funds_out))


if __name__ == "__main__":
    sys.exit(main())
