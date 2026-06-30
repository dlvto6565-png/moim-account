#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kis_snapshot.py
한국투자증권(KIS) OpenAPI에서 '조회만' 해서 data.json 을 만드는 스크립트.
- 국내/해외 잔고(보유종목·수익률) + 최근 체결내역(매매내역)을 뽑는다.
- 주문(매수/매도) 함수는 의도적으로 넣지 않았다. 이 파이프라인은 읽기 전용이다.
- App Key/Secret 은 config.json 에만 두고, 웹(data.json)에는 절대 들어가지 않는다.

실행:
    python kis_snapshot.py            # 실제 호출 (config.json 필요)
    python kis_snapshot.py --demo     # 키 없이 샘플 data.json 생성 (대시보드 미리보기용)

주의: KIS 응답 필드명은 한글 약어라 계좌/상품에 따라 다를 수 있다.
      혹시 빈 값이 나오면 NORMALIZE 구간의 키 이름만 공식 문서/깃헙 샘플과 대조해 고치면 된다.
      (koreainvestment/open-trading-api 의 inquire_balance 예제 참고)
"""

import json
import os
import sys
import time
import datetime as dt
import urllib.request
import urllib.parse
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
TOKEN_PATH = os.path.join(HERE, "token.json")           # 로컬 PC 캐시
TOKEN_CACHE_PATH = os.path.join(HERE, "token_cache.json") # GitHub Actions 공유 캐시 (커밋됨)
CONTRIB_PATH = os.path.join(HERE, "contributions.json")  # 모임원 입금 장부 (자동/수동 누적)
RECURRING_PATH = os.path.join(HERE, "recurring.json")    # 정기(매월) 입금 규칙
HISTORY_PATH = os.path.join(HERE, "history.json")        # 월별 추이 누적 기록 (자동)
OUT_PATH = os.path.join(HERE, "data.json")

REAL_BASE = "https://openapi.koreainvestment.com:9443"
VTS_BASE = "https://openapivts.koreainvestment.com:29443"


# ---------------------------------------------------------------------------
# 설정 / 토큰
# ---------------------------------------------------------------------------
def load_config():
    # 1) 클라우드(GitHub Actions 등): 환경변수에 키가 있으면 그걸 사용
    if os.environ.get("KIS_APP_KEY"):
        return {
            "is_real": os.environ.get("KIS_IS_REAL", "true").lower() == "true",
            "app_key": os.environ["KIS_APP_KEY"],
            "app_secret": os.environ["KIS_APP_SECRET"],
            "cano": os.environ["KIS_CANO"],
            "acnt_prdt_cd": os.environ.get("KIS_ACNT_PRDT_CD", "01"),
            "overseas_exchanges": json.loads(
                os.environ.get("KIS_OVERSEAS_EXCHANGES", '["NASD","NYSE","AMEX"]')
            ),
            "usd_krw": fnum(os.environ.get("KIS_USD_KRW"), 0),
            "trade_lookback_days": int(os.environ.get("KIS_TRADE_LOOKBACK_DAYS", "30")),
        }
    # 2) 로컬 PC: config.json 사용
    if not os.path.exists(CONFIG_PATH):
        sys.exit("config.json 이 없습니다. config.example.json 을 복사해서 채워주세요.")
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def base_url(cfg):
    return REAL_BASE if cfg.get("is_real", True) else VTS_BASE


def http(method, url, headers=None, params=None, body=None, retries=3):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    last_err = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            # 서버가 4xx/5xx 응답을 준 경우 — 내용을 그대로 올려 진단
            detail = e.read().decode(errors="ignore")
            raise RuntimeError(f"HTTP {e.code} {url}\n{detail}")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            # 타임아웃·연결 끊김 등 일시적 오류 — 잠깐 쉬고 재시도
            last_err = e
            if attempt < retries - 1:
                wait = 3 * (attempt + 1)
                print(f"[재시도] {url} 연결 실패, {wait}초 후 다시 시도 ({attempt + 1}/{retries})", file=sys.stderr)
                time.sleep(wait)
    raise RuntimeError(f"연결 실패(재시도 {retries}회 초과): {url}\n{last_err}")


def get_token(cfg):
    """접근토큰 발급 — 하루 1회 원칙 준수.
    로컬(token.json) → 저장소 공유 캐시(token_cache.json) 순으로 유효한 토큰을 찾고,
    둘 다 없거나 만료됐을 때만 새로 발급한다.
    GitHub Actions는 매 실행이 새 환경이라 token.json이 안 남으므로,
    token_cache.json 을 저장소에 커밋해 공유한다(update.yml의 git add에 추가).
    """
    # 유효성 확인 (만료 30분 전부터 갱신 준비)
    def _valid(path):
        if not os.path.exists(path):
            return None
        try:
            with open(path, encoding="utf-8") as f:
                c = json.load(f)
            if c.get("expires_at", 0) > time.time() + 1800:
                return c["access_token"]
        except Exception:
            pass
        return None

    tok = _valid(TOKEN_PATH) or _valid(TOKEN_CACHE_PATH)
    if tok:
        print("[토큰] 캐시 재사용 (신규 발급 없음)")
        return tok

    print("[토큰] 새로 발급")
    res = http(
        "POST",
        base_url(cfg) + "/oauth2/tokenP",
        headers={"content-type": "application/json"},
        body={
            "grant_type": "client_credentials",
            "appkey": cfg["app_key"],
            "appsecret": cfg["app_secret"],
        },
    )
    token = res["access_token"]
    ttl = int(res.get("expires_in", 86400))
    payload = {"access_token": token, "expires_at": time.time() + ttl}
    # 두 곳에 저장 (로컬용 + 저장소 공유용)
    for path in [TOKEN_PATH, TOKEN_CACHE_PATH]:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
    return token


def headers(cfg, token, tr_id):
    return {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": cfg["app_key"],
        "appsecret": cfg["app_secret"],
        "tr_id": tr_id,
        "custtype": "P",  # 개인
    }


def fnum(v, default=0.0):
    try:
        return float(str(v).replace(",", "").strip() or 0)
    except (ValueError, AttributeError):
        return default


def get_usd_krw(cfg):
    """USD/KRW 환율 자동 조회. 두 소스를 차례로 시도하고, 다 실패하면 config 값 사용."""
    sources = [
        ("open.er-api.com", "https://open.er-api.com/v6/latest/USD"),
        ("frankfurter.app", "https://api.frankfurter.app/latest?from=USD&to=KRW"),
    ]
    for name, url in sources:
        try:
            res = http("GET", url, headers={"User-Agent": "Mozilla/5.0"})
            rate = res.get("rates", {}).get("KRW")
            if rate:
                rate = round(float(rate), 2)
                print(f"[환율] 자동 조회 성공 ({name}): {rate:,.2f}")
                return rate
        except Exception as e:
            print(f"[환율] {name} 실패 ({e})", file=sys.stderr)
    fb = fnum(cfg.get("usd_krw"), 0)
    print(f"[환율] 자동 조회 모두 실패 → config 값 사용: {fb:,.0f}", file=sys.stderr)
    return fb


# ---------------------------------------------------------------------------
# 국내주식 잔고
# ---------------------------------------------------------------------------
def domestic_balance(cfg, token):
    tr = "TTTC8434R" if cfg.get("is_real", True) else "VTTC8434R"
    url = base_url(cfg) + "/uapi/domestic-stock/v1/trading/inquire-balance"
    params = {
        "CANO": cfg["cano"],
        "ACNT_PRDT_CD": cfg["acnt_prdt_cd"],
        "AFHR_FLPR_YN": "N",
        "OFL_YN": "",
        "INQR_DVSN": "02",
        "UNPR_DVSN": "01",
        "FUND_STTL_ICLD_YN": "N",
        "FNCG_AMT_AUTO_RDPT_YN": "N",
        "PRCS_DVSN": "00",
        "CTX_AREA_FK100": "",
        "CTX_AREA_NK100": "",
    }
    res = http("GET", url, headers=headers(cfg, token, tr), params=params)

    holdings = []
    for it in res.get("output1", []):
        qty = fnum(it.get("hldg_qty"))
        if qty <= 0:
            continue
        # ---- NORMALIZE (국내) ----
        holdings.append({
            "market": "국내",
            "currency": "KRW",
            "name": it.get("prdt_name", ""),
            "code": it.get("pdno", ""),
            "qty": qty,
            "avg_price": fnum(it.get("pchs_avg_pric")),
            "cur_price": fnum(it.get("prpr")),
            "eval_amt": fnum(it.get("evlu_amt")),
            "pl": fnum(it.get("evlu_pfls_amt")),
            "pl_rate": fnum(it.get("evlu_pfls_rt")),
        })

    o2 = (res.get("output2") or [{}])[0]
    purchase = fnum(o2.get("pchs_amt_smtl_amt"))      # 주식 매입금액 합계
    pl = fnum(o2.get("evlu_pfls_smtl_amt"))           # 주식 평가손익 합계
    stock_eval = fnum(o2.get("scts_evlu_amt"))        # 유가증권(주식) 평가금액
    cash = fnum(o2.get("dnca_tot_amt"))               # 예수금
    net = fnum(o2.get("nass_amt"))                    # 순자산(예수금+주식) — 가장 신뢰
    # 순자산이 비어오면 주식평가+예수금으로 보정
    if net <= 0:
        net = stock_eval + cash
    summary = {
        "eval": stock_eval,                           # 주식만의 평가액
        "net": net,                                   # 계좌 총자산(현금 포함)
        "purchase": purchase,
        "pl": pl,
        "pl_rate": round(pl / purchase * 100, 2) if purchase else 0.0,
        "cash": cash,
    }
    return holdings, summary


# ---------------------------------------------------------------------------
# 해외주식 잔고 (거래소별로 조회해서 합친다)
# ---------------------------------------------------------------------------
def _mag2(v):
    s = str(v).strip()
    neg = s.startswith("-")
    s2 = s.lstrip("-").replace(".", "").replace(",", "")
    if not s2.isdigit():
        return f"비숫자('{s[:14]}')"
    d = len(s2.lstrip("0"))
    return "0(비어있음)" if d == 0 else f"{'음수 ' if neg else ''}{d}자리"


def debug_overseas_cash(cfg, token):
    """해외 체결기준 현재잔고 — 달러(외화) 예수금이 어느 항목에 잡히는지 진단."""
    tr = "CTRP6504R" if cfg.get("is_real", True) else "VTRP6504R"
    url = base_url(cfg) + "/uapi/overseas-stock/v1/trading/inquire-present-balance"
    params = {
        "CANO": cfg["cano"],
        "ACNT_PRDT_CD": cfg["acnt_prdt_cd"],
        "WCRC_FRCR_DVSN_CD": "02",  # 외화 기준
        "NATN_CD": "000",
        "TR_MKET_CD": "00",
        "INQR_DVSN_CD": "00",
    }
    try:
        res = http("GET", url, headers=headers(cfg, token, tr), params=params)
    except Exception as e:
        print(f"[DEBUG 해외현금] 조회 실패 ({e})", file=sys.stderr)
        return
    for tag in ["output2", "output3"]:
        block = res.get(tag)
        rows = block if isinstance(block, list) else [block] if block else []
        print(f"===== DEBUG 해외현재잔고 {tag} (자릿수만) =====")
        for row in rows:
            if isinstance(row, dict):
                for k, v in row.items():
                    print(f"  {k} = {_mag2(v)}")
        print("============================================")


def overseas_balance(cfg, token):
    tr = "TTTS3012R" if cfg.get("is_real", True) else "VTTS3012R"
    url = base_url(cfg) + "/uapi/overseas-stock/v1/trading/inquire-balance"
    holdings, eval_usd, purchase_usd, pl_usd = [], 0.0, 0.0, 0.0

    for exch in cfg.get("overseas_exchanges", ["NASD", "NYSE", "AMEX"]):
        params = {
            "CANO": cfg["cano"],
            "ACNT_PRDT_CD": cfg["acnt_prdt_cd"],
            "OVRS_EXCG_CD": exch,
            "TR_CRCY_CD": "USD",
            "CTX_AREA_FK200": "",
            "CTX_AREA_NK200": "",
        }
        try:
            res = http("GET", url, headers=headers(cfg, token, tr), params=params)
        except RuntimeError as e:
            print(f"[해외:{exch}] 조회 건너뜀 -> {e}", file=sys.stderr)
            continue
        time.sleep(0.2)  # 유량(초당20건) 여유

        for it in res.get("output1", []):
            qty = fnum(it.get("ovrs_cblc_qty"))
            if qty <= 0:
                continue
            # ---- NORMALIZE (해외) ----
            ev = fnum(it.get("ovrs_stck_evlu_amt"))
            pl = fnum(it.get("frcr_evlu_pfls_amt"))
            avg = fnum(it.get("pchs_avg_pric"))
            holdings.append({
                "market": "해외",
                "currency": "USD",
                "name": it.get("ovrs_item_name", ""),
                "code": it.get("ovrs_pdno", ""),
                "qty": qty,
                "avg_price": avg,
                "cur_price": fnum(it.get("now_pric2")),
                "eval_amt": ev,
                "pl": pl,
                "pl_rate": fnum(it.get("evlu_pfls_rt")),
            })
            eval_usd += ev
            pl_usd += pl
            purchase_usd += avg * qty

    summary = {
        "eval_usd": round(eval_usd, 2),
        "purchase_usd": round(purchase_usd, 2),
        "pl_usd": round(pl_usd, 2),
        "pl_rate": round(pl_usd / purchase_usd * 100, 2) if purchase_usd else 0.0,
    }
    return holdings, summary


# ---------------------------------------------------------------------------
# 국내 일별 체결내역 (매매내역)
# ---------------------------------------------------------------------------
def domestic_trades(cfg, token, days=30):
    tr = "TTTC8001R" if cfg.get("is_real", True) else "VTTC8001R"
    url = base_url(cfg) + "/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
    end = dt.date.today()
    start = end - dt.timedelta(days=days)
    params = {
        "CANO": cfg["cano"],
        "ACNT_PRDT_CD": cfg["acnt_prdt_cd"],
        "INQR_STRT_DT": start.strftime("%Y%m%d"),
        "INQR_END_DT": end.strftime("%Y%m%d"),
        "SLL_BUY_DVSN_CD": "00",   # 전체
        "INQR_DVSN": "00",
        "PDNO": "",
        "CCLD_DVSN": "01",         # 체결
        "ORD_GNO_BRNO": "",
        "ODNO": "",
        "INQR_DVSN_3": "00",
        "INQR_DVSN_1": "",
        "CTX_AREA_FK100": "",
        "CTX_AREA_NK100": "",
    }
    res = http("GET", url, headers=headers(cfg, token, tr), params=params)
    trades = []
    for it in res.get("output1", []):
        qty = fnum(it.get("tot_ccld_qty"))
        if qty <= 0:
            continue
        d = it.get("ord_dt", "")
        trades.append({
            "date": f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(d) == 8 else d,
            "market": "국내",
            "currency": "KRW",
            "name": it.get("prdt_name", ""),
            "code": it.get("pdno", ""),
            "side": it.get("sll_buy_dvsn_cd_name", ""),  # 매수/매도
            "qty": qty,
            "price": fnum(it.get("avg_prvs")),
            "amount": fnum(it.get("tot_ccld_amt")),
        })
    return trades


# ---------------------------------------------------------------------------
# 월별 추이 기록 (실행할 때마다 그 달 값을 갱신해 history.json 에 누적)
# ---------------------------------------------------------------------------
def update_history(net_asset, contrib):
    hist = []
    if os.path.exists(HISTORY_PATH):
        with open(HISTORY_PATH, encoding="utf-8") as f:
            hist = json.load(f)
    ym = dt.date.today().strftime("%Y-%m")
    pl = net_asset - contrib if contrib else 0
    rec = {
        "ym": ym,
        "net_asset": round(net_asset),
        "contrib": round(contrib),
        "pl": round(pl),
        "pl_rate": round(pl / contrib * 100, 2) if contrib else 0.0,
    }
    hist = [h for h in hist if h.get("ym") != ym]  # 같은 달이면 최신값으로 교체
    hist.append(rec)
    hist.sort(key=lambda h: h["ym"])
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, indent=2)
    return hist


# ---------------------------------------------------------------------------
# 정기 입금 자동 기입 (매월 말일이 지나면 그 달 분을 contributions 에 추가)
# ---------------------------------------------------------------------------
def _month_last_day(y, m):
    nxt = dt.date(y + 1, 1, 1) if m == 12 else dt.date(y, m + 1, 1)
    return nxt - dt.timedelta(days=1)


def apply_recurring(contributions):
    if not os.path.exists(RECURRING_PATH):
        return contributions
    with open(RECURRING_PATH, encoding="utf-8") as f:
        rule = json.load(f)
    members = rule.get("members", [])
    start = rule.get("start_ym")
    if not members or not start:
        return contributions

    today = dt.date.today()
    existing = {(c.get("member"), c.get("date")) for c in contributions}
    y, m = map(int, start.split("-"))
    added = 0
    while (y, m) <= (today.year, today.month):
        last = _month_last_day(y, m)
        if today >= last:  # 그 달 말일이 실제로 지났을 때만 기입
            ymd = last.isoformat()
            for mem in members:
                key = (mem["member"], ymd)
                if key not in existing:
                    contributions.append({
                        "date": ymd, "member": mem["member"],
                        "amount": mem["amount"], "auto": True,
                    })
                    existing.add(key)
                    added += 1
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)

    if added:
        contributions.sort(key=lambda c: c.get("date", ""))
        with open(CONTRIB_PATH, "w", encoding="utf-8") as f:
            json.dump(contributions, f, ensure_ascii=False, indent=2)
        print(f"[정기입금] {added}건 자동 기입")
    return contributions


# ---------------------------------------------------------------------------
# 조립
# ---------------------------------------------------------------------------
def build(cfg):
    token = get_token(cfg)

    dom_h, dom_s = domestic_balance(cfg, token)
    time.sleep(0.2)
    ovs_h, ovs_s = overseas_balance(cfg, token)
    debug_overseas_cash(cfg, token)   # 진단용 — 달러 예수금 위치 확인 후 제거 예정
    time.sleep(0.2)
    try:
        trades = domestic_trades(cfg, token, cfg.get("trade_lookback_days", 30))
    except RuntimeError as e:
        print(f"[체결] 조회 실패 -> {e}", file=sys.stderr)
        trades = []
    trades.sort(key=lambda t: t["date"], reverse=True)

    usd_krw = get_usd_krw(cfg)  # 자동 환율 (실패 시 config 값)
    ovs_eval_krw = ovs_s["eval_usd"] * usd_krw if usd_krw else 0
    ovs_pl_krw = ovs_s["pl_usd"] * usd_krw if usd_krw else 0

    stock_eval = dom_s["eval"] + ovs_eval_krw                 # 주식만의 평가액
    total_pl = dom_s["pl"] + ovs_pl_krw                       # 주식 평가손익
    total_purchase = dom_s["purchase"] + (ovs_s["purchase_usd"] * usd_krw if usd_krw else 0)

    contributions = []
    if os.path.exists(CONTRIB_PATH):
        with open(CONTRIB_PATH, encoding="utf-8") as f:
            contributions = json.load(f)
    contributions = apply_recurring(contributions)  # 정기 입금 자동 기입

    cash = dom_s.get("cash", 0)
    # 현재 총자산 = 한투 순자산(국내 주식+현금) + 해외 주식평가.  ※ 현금은 여기에만 한 번 포함됨
    net_asset = dom_s.get("net", stock_eval + cash) + ovs_eval_krw
    contrib_total = sum(c.get("amount", 0) for c in contributions)
    history = update_history(net_asset, contrib_total)

    return {
        "updated_at": dt.datetime.now().isoformat(timespec="minutes"),
        "usd_krw": usd_krw or None,
        "summary": {
            "domestic": dom_s,
            "overseas": ovs_s,
            "total_krw_net": round(net_asset),     # 현재 총자산 (현금 포함, 화면은 이걸 그대로 사용)
            "total_krw_eval": round(stock_eval),   # 주식만의 평가액
            "total_krw_pl": round(total_pl),
            "total_krw_pl_rate": round(total_pl / total_purchase * 100, 2) if total_purchase else 0.0,
            "cash": round(cash),
        },
        "holdings": dom_h + ovs_h,
        "trades": trades,
        "contributions": contributions,
        "history": history,
    }


# ---------------------------------------------------------------------------
# 데모 데이터 (키 없이 대시보드 확인용)
# ---------------------------------------------------------------------------
def demo():
    return {
        "updated_at": dt.datetime.now().isoformat(timespec="minutes"),
        "usd_krw": 1378,
        "summary": {
            "domestic": {"eval": 8_420_000, "purchase": 7_900_000, "pl": 520_000, "pl_rate": 6.58, "cash": 310_000},
            "overseas": {"eval_usd": 4_120.50, "purchase_usd": 3_650.00, "pl_usd": 470.50, "pl_rate": 12.89},
            "total_krw_eval": 14_098_049,
            "total_krw_pl": 1_168_549,
            "total_krw_pl_rate": 9.04,
        },
        "holdings": [
            {"market": "국내", "currency": "KRW", "name": "삼성전자", "code": "005930", "qty": 80, "avg_price": 71500, "cur_price": 78200, "eval_amt": 6_256_000, "pl": 536_000, "pl_rate": 9.37},
            {"market": "국내", "currency": "KRW", "name": "TIGER 미국S&P500", "code": "360750", "qty": 110, "avg_price": 19700, "cur_price": 19672, "eval_amt": 2_163_920, "pl": -3_080, "pl_rate": -0.14},
            {"market": "해외", "currency": "USD", "name": "APPLE INC", "code": "AAPL", "qty": 12, "avg_price": 188.40, "cur_price": 211.20, "eval_amt": 2_534.40, "pl": 273.60, "pl_rate": 12.10},
            {"market": "해외", "currency": "USD", "name": "NVIDIA CORP", "code": "NVDA", "qty": 9, "avg_price": 162.10, "cur_price": 176.23, "eval_amt": 1_586.07, "pl": 127.17, "pl_rate": 8.72},
        ],
        "trades": [
            {"date": "2026-06-24", "market": "국내", "currency": "KRW", "name": "삼성전자", "code": "005930", "side": "매수", "qty": 30, "price": 77800, "amount": 2_334_000},
            {"date": "2026-06-18", "market": "해외", "currency": "USD", "name": "NVIDIA CORP", "code": "NVDA", "side": "매수", "qty": 4, "price": 168.30, "amount": 673.20},
            {"date": "2026-06-10", "market": "국내", "currency": "KRW", "name": "TIGER 미국S&P500", "code": "360750", "side": "매수", "qty": 50, "price": 19550, "amount": 977_500},
            {"date": "2026-06-03", "market": "해외", "currency": "USD", "name": "APPLE INC", "code": "AAPL", "side": "매도", "qty": 3, "price": 205.10, "amount": 615.30},
        ],
        "contributions": [
            {"date": "2026-01-05", "member": "한결", "amount": 6_000_000},
            {"date": "2026-01-05", "member": "동료A", "amount": 3_500_000},
            {"date": "2026-01-05", "member": "동료B", "amount": 3_000_000},
        ],
        "history": [
            {"ym": "2026-01", "net_asset": 9_780_000,  "contrib": 9_000_000,  "pl": 780_000,   "pl_rate": 8.67},
            {"ym": "2026-02", "net_asset": 11_240_000, "contrib": 10_500_000, "pl": 740_000,   "pl_rate": 7.05},
            {"ym": "2026-03", "net_asset": 11_910_000, "contrib": 11_000_000, "pl": 910_000,   "pl_rate": 8.27},
            {"ym": "2026-04", "net_asset": 12_640_000, "contrib": 11_500_000, "pl": 1_140_000, "pl_rate": 9.91},
            {"ym": "2026-05", "net_asset": 13_420_000, "contrib": 12_000_000, "pl": 1_420_000, "pl_rate": 11.83},
            {"ym": "2026-06", "net_asset": 14_408_049, "contrib": 12_500_000, "pl": 1_908_049, "pl_rate": 15.26},
        ],
    }


def main():
    if "--demo" in sys.argv:
        data = demo()
    else:
        data = build(load_config())
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"data.json 생성 완료 · 보유 {len(data['holdings'])}종목 · 체결 {len(data['trades'])}건")


if __name__ == "__main__":
    main()
