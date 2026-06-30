#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kis_snapshot.py
한국투자증권(KIS) OpenAPI에서 '조회만' 해서 data.json 을 만드는 스크립트.

핵심 수정사항:
1. HTTP 요청 타임아웃/일시 실패 재시도 추가
2. token_cache.json 재사용 구조 유지
3. 국내 현금 표시 방식 수정
   - KIS 원본 예수금(dnca_tot_amt)은 raw_cash로 보관
   - 화면 표시용 현금(cash)은 순자산 - 주식평가액으로 계산
4. total_krw_eval은 기존 프론트 호환을 위해 "현금 포함 총자산"으로 유지
5. stock_krw_eval을 별도로 추가해서 주식만의 평가액도 제공
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
TOKEN_PATH = os.path.join(HERE, "token.json")
TOKEN_CACHE_PATH = os.path.join(HERE, "token_cache.json")
CONTRIB_PATH = os.path.join(HERE, "contributions.json")
HISTORY_PATH = os.path.join(HERE, "history.json")
OUT_PATH = os.path.join(HERE, "data.json")

REAL_BASE = "https://openapi.koreainvestment.com:9443"
VTS_BASE = "https://openapivts.koreainvestment.com:29443"


# ---------------------------------------------------------------------------
# 공통 유틸
# ---------------------------------------------------------------------------
def fnum(v, default=0.0):
    try:
        return float(str(v).replace(",", "").strip() or 0)
    except (ValueError, AttributeError, TypeError):
        return default


def now_iso():
    return dt.datetime.now().isoformat(timespec="minutes")


def safe_round(v):
    return round(fnum(v))


# ---------------------------------------------------------------------------
# 설정 / HTTP / 토큰
# ---------------------------------------------------------------------------
def load_config():
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

    if not os.path.exists(CONFIG_PATH):
        sys.exit("config.json 이 없습니다. config.example.json 을 복사해서 채워주세요.")

    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def base_url(cfg):
    return REAL_BASE if cfg.get("is_real", True) else VTS_BASE


def http(method, url, headers=None, params=None, body=None, retries=3, timeout=40):
    """
    한국투자 API 또는 환율 API가 순간적으로 느릴 때를 대비해 재시도한다.
    HTTP 4xx/5xx는 그대로 에러 처리하고,
    네트워크 타임아웃/URLError만 재시도한다.
    """
    if params:
        url = url + "?" + urllib.parse.urlencode(params)

    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})

    last_err = None

    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode()
                return json.loads(raw)

        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="ignore")
            raise RuntimeError(f"HTTP {e.code} {url}\n{detail}")

        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            print(f"[HTTP] 요청 실패 {attempt}/{retries}: {e}", file=sys.stderr)

            if attempt < retries:
                time.sleep(5 * attempt)

    raise RuntimeError(f"HTTP 요청 최종 실패: {url}\n{last_err}")


def get_token(cfg):
    """
    접근토큰 발급/재사용.

    GitHub Actions는 매번 새 환경이라 token.json은 유지되지 않는다.
    따라서 token_cache.json을 저장소에 커밋해서 다음 실행에서 재사용한다.
    update.yml의 git add에 token_cache.json이 포함되어 있어야 한다.
    """
    def _valid(path):
        if not os.path.exists(path):
            return None

        try:
            with open(path, encoding="utf-8") as f:
                c = json.load(f)

            expires_at = fnum(c.get("expires_at"), 0)
            token = c.get("access_token")

            # 만료 30분 전부터는 새로 발급 준비
            if token and expires_at > time.time() + 1800:
                return token

        except Exception as e:
            print(f"[토큰] 캐시 읽기 실패: {path} / {e}", file=sys.stderr)

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
        retries=3,
        timeout=40,
    )

    token = res["access_token"]
    ttl = int(res.get("expires_in", 86400))

    payload = {
        "access_token": token,
        "expires_at": time.time() + ttl,
        "created_at": now_iso(),
    }

    for path in [TOKEN_PATH, TOKEN_CACHE_PATH]:
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[토큰] 저장 실패: {path} / {e}", file=sys.stderr)

    return token


def headers(cfg, token, tr_id):
    return {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": cfg["app_key"],
        "appsecret": cfg["app_secret"],
        "tr_id": tr_id,
        "custtype": "P",
    }


# ---------------------------------------------------------------------------
# 환율
# ---------------------------------------------------------------------------
def get_usd_krw(cfg):
    sources = [
        ("open.er-api.com", "https://open.er-api.com/v6/latest/USD"),
        ("frankfurter.app", "https://api.frankfurter.app/latest?from=USD&to=KRW"),
    ]

    for name, url in sources:
        try:
            res = http("GET", url, headers={"User-Agent": "Mozilla/5.0"}, retries=2, timeout=20)
            rate = res.get("rates", {}).get("KRW")

            if rate:
                rate = round(float(rate), 2)
                print(f"[환율] 자동 조회 성공 ({name}): {rate:,.2f}")
                return rate

        except Exception as e:
            print(f"[환율] {name} 실패 ({e})", file=sys.stderr)

    fb = fnum(cfg.get("usd_krw"), 0)

    if fb > 0:
        print(f"[환율] 자동 조회 모두 실패 → config/env 값 사용: {fb:,.2f}", file=sys.stderr)
        return fb

    print("[환율] 자동 조회 모두 실패 → 0 처리", file=sys.stderr)
    return 0


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

        eval_amt = fnum(it.get("evlu_amt"))

        holdings.append({
            "market": "국내",
            "currency": "KRW",
            "name": it.get("prdt_name", ""),
            "code": it.get("pdno", ""),
            "qty": qty,
            "avg_price": fnum(it.get("pchs_avg_pric")),
            "cur_price": fnum(it.get("prpr")),
            "eval_amt": eval_amt,
            "eval_amt_krw": eval_amt,
            "pl": fnum(it.get("evlu_pfls_amt")),
            "pl_krw": fnum(it.get("evlu_pfls_amt")),
            "pl_rate": fnum(it.get("evlu_pfls_rt")),
        })

    o2 = (res.get("output2") or [{}])[0]

    purchase = fnum(o2.get("pchs_amt_smtl_amt"))
    pl = fnum(o2.get("evlu_pfls_smtl_amt"))
    stock_eval = fnum(o2.get("scts_evlu_amt"))

    # KIS 원본 예수금.
    # 국내주식은 D+2 정산 구조 때문에 매수 직후에도 이 값이 그대로 보일 수 있음.
    raw_cash = fnum(o2.get("dnca_tot_amt"))

    # 순자산 후보값들.
    # nass_amt가 가장 의도에 맞지만, 계좌/상품에 따라 비어있을 수 있어서 보조 필드도 시도.
    net = fnum(o2.get("nass_amt"))

    if net <= 0:
        net = fnum(o2.get("tot_evlu_amt"))

    if net <= 0:
        net = fnum(o2.get("asst_icdc_amt"))

    # 최후 보정: 순자산이 전혀 안 오면 주식평가 + 원본예수금
    if net <= 0:
        net = stock_eval + raw_cash

    # 핵심 수정:
    # 화면에 보여줄 현금은 KIS 원본 예수금이 아니라
    # 포트폴리오 관점의 남은 현금 = 순자산 - 주식평가액.
    cash = max(0.0, net - stock_eval)

    print(
        "[국내] "
        f"주식평가={stock_eval:,.0f} / "
        f"순자산={net:,.0f} / "
        f"표시현금={cash:,.0f} / "
        f"KIS원본예수금={raw_cash:,.0f}"
    )

    summary = {
        "eval": stock_eval,
        "net": net,
        "purchase": purchase,
        "pl": pl,
        "pl_rate": round(pl / purchase * 100, 2) if purchase else 0.0,

        # 화면 표시용 현금
        "cash": cash,

        # 참고용 원본 예수금
        "raw_cash": raw_cash,
    }

    return holdings, summary


# ---------------------------------------------------------------------------
# 해외주식 잔고
# ---------------------------------------------------------------------------
def overseas_balance(cfg, token):
    tr = "TTTS3012R" if cfg.get("is_real", True) else "VTTS3012R"
    url = base_url(cfg) + "/uapi/overseas-stock/v1/trading/inquire-balance"

    holdings = []
    eval_usd = 0.0
    purchase_usd = 0.0
    pl_usd = 0.0

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

        time.sleep(0.2)

        for it in res.get("output1", []):
            qty = fnum(it.get("ovrs_cblc_qty"))

            if qty <= 0:
                continue

            ev = fnum(it.get("ovrs_stck_evlu_amt"))
            item_pl = fnum(it.get("frcr_evlu_pfls_amt"))
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
                "pl": item_pl,
                "pl_rate": fnum(it.get("evlu_pfls_rt")),
            })

            eval_usd += ev
            pl_usd += item_pl
            purchase_usd += avg * qty

    print(
        "[해외] "
        f"평가USD={eval_usd:,.2f} / "
        f"매입USD={purchase_usd:,.2f} / "
        f"손익USD={pl_usd:,.2f}"
    )

    summary = {
        "eval_usd": round(eval_usd, 2),
        "purchase_usd": round(purchase_usd, 2),
        "pl_usd": round(pl_usd, 2),
        "pl_rate": round(pl_usd / purchase_usd * 100, 2) if purchase_usd else 0.0,
    }

    return holdings, summary


# ---------------------------------------------------------------------------
# 국내 일별 체결내역
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
        "SLL_BUY_DVSN_CD": "00",
        "INQR_DVSN": "00",
        "PDNO": "",
        "CCLD_DVSN": "01",
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
            "side": it.get("sll_buy_dvsn_cd_name", ""),
            "qty": qty,
            "price": fnum(it.get("avg_prvs")),
            "amount": fnum(it.get("tot_ccld_amt")),
        })

    return trades


# ---------------------------------------------------------------------------
# 입금 장부
# ---------------------------------------------------------------------------
def load_contributions():
    if not os.path.exists(CONTRIB_PATH):
        return []

    try:
        with open(CONTRIB_PATH, encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, list):
            return data

    except Exception as e:
        print(f"[입금장부] 읽기 실패: {e}", file=sys.stderr)

    return []


# ---------------------------------------------------------------------------
# 월별 추이 기록
# ---------------------------------------------------------------------------
def update_history(net_asset, contrib):
    hist = []

    if os.path.exists(HISTORY_PATH):
        try:
            with open(HISTORY_PATH, encoding="utf-8") as f:
                hist = json.load(f)
        except Exception as e:
            print(f"[히스토리] 기존 history.json 읽기 실패: {e}", file=sys.stderr)
            hist = []

    ym = dt.date.today().strftime("%Y-%m")
    pl = net_asset - contrib if contrib else 0

    rec = {
        "ym": ym,
        "net_asset": safe_round(net_asset),
        "contrib": safe_round(contrib),
        "pl": safe_round(pl),
        "pl_rate": round(pl / contrib * 100, 2) if contrib else 0.0,
    }

    hist = [h for h in hist if h.get("ym") != ym]
    hist.append(rec)
    hist.sort(key=lambda h: h["ym"])

    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, indent=2)

    return hist


# ---------------------------------------------------------------------------
# 조립
# ---------------------------------------------------------------------------
def build(cfg):
    token = get_token(cfg)

    dom_h, dom_s = domestic_balance(cfg, token)

    time.sleep(0.2)

    ovs_h, ovs_s = overseas_balance(cfg, token)

    time.sleep(0.2)

    try:
        trades = domestic_trades(cfg, token, cfg.get("trade_lookback_days", 30))
    except RuntimeError as e:
        print(f"[체결] 조회 실패 -> {e}", file=sys.stderr)
        trades = []

    trades.sort(key=lambda t: t.get("date", ""), reverse=True)

    usd_krw = get_usd_krw(cfg)

    ovs_eval_krw = ovs_s["eval_usd"] * usd_krw if usd_krw else 0.0
    ovs_pl_krw = ovs_s["pl_usd"] * usd_krw if usd_krw else 0.0
    ovs_purchase_krw = ovs_s["purchase_usd"] * usd_krw if usd_krw else 0.0

    # 해외 보유종목에도 원화 평가액 추가
    ovs_h_krw = []
    for h in ovs_h:
        hh = dict(h)
        hh["eval_amt_krw"] = round(fnum(h.get("eval_amt")) * usd_krw, 2) if usd_krw else 0
        hh["pl_krw"] = round(fnum(h.get("pl")) * usd_krw, 2) if usd_krw else 0
        ovs_h_krw.append(hh)

    holdings = dom_h + ovs_h_krw

    domestic_stock_eval = dom_s["eval"]
    domestic_net = dom_s["net"]
    domestic_cash = dom_s["cash"]

    stock_krw_eval = domestic_stock_eval + ovs_eval_krw
    total_krw_net = domestic_net + ovs_eval_krw

    total_pl = dom_s["pl"] + ovs_pl_krw
    total_purchase = dom_s["purchase"] + ovs_purchase_krw

    contributions = load_contributions()
    contrib_total = sum(fnum(c.get("amount")) for c in contributions)

    history = update_history(total_krw_net, contrib_total)

    # 종목비중용 데이터.
    # 프론트에서 이걸 쓰면 현금 100%, 현금+종목 비중을 더 안정적으로 표시 가능.
    allocation = []

    for h in holdings:
        val = fnum(h.get("eval_amt_krw"))

        if val > 0:
            allocation.append({
                "name": h.get("name", ""),
                "code": h.get("code", ""),
                "market": h.get("market", ""),
                "type": "stock",
                "value": round(val),
            })

    if domestic_cash > 0:
        allocation.append({
            "name": "현금",
            "code": "CASH",
            "market": "현금",
            "type": "cash",
            "value": round(domestic_cash),
        })

    print(
        "[전체] "
        f"주식평가={stock_krw_eval:,.0f} / "
        f"현금={domestic_cash:,.0f} / "
        f"총자산={total_krw_net:,.0f} / "
        f"입금합계={contrib_total:,.0f}"
    )

    return {
        "updated_at": now_iso(),
        "usd_krw": usd_krw or None,

        "summary": {
            "domestic": dom_s,
            "overseas": ovs_s,

            # 신규 명확한 필드
            "total_krw_net": safe_round(total_krw_net),
            "stock_krw_eval": safe_round(stock_krw_eval),
            "cash": safe_round(domestic_cash),

            # 기존 프론트 호환용:
            # total_krw_eval을 현금 포함 총자산으로 둔다.
            # 기존 화면에서 총평가액을 이 값으로 읽는 경우가 많기 때문.
            "total_krw_eval": safe_round(total_krw_net),

            "total_krw_pl": safe_round(total_pl),
            "total_krw_pl_rate": round(total_pl / total_purchase * 100, 2) if total_purchase else 0.0,

            "total_purchase": safe_round(total_purchase),
            "contrib_total": safe_round(contrib_total),
        },

        "holdings": holdings,
        "allocation": allocation,
        "trades": trades,
        "contributions": contributions,
        "history": history,
    }


# ---------------------------------------------------------------------------
# 데모 데이터
# ---------------------------------------------------------------------------
def demo():
    return {
        "updated_at": now_iso(),
        "usd_krw": 1378,
        "summary": {
            "domestic": {
                "eval": 2_000_000,
                "net": 3_700_000,
                "purchase": 2_000_000,
                "pl": 0,
                "pl_rate": 0,
                "cash": 1_700_000,
                "raw_cash": 3_700_000,
            },
            "overseas": {
                "eval_usd": 0,
                "purchase_usd": 0,
                "pl_usd": 0,
                "pl_rate": 0,
            },
            "total_krw_net": 3_700_000,
            "stock_krw_eval": 2_000_000,
            "cash": 1_700_000,
            "total_krw_eval": 3_700_000,
            "total_krw_pl": 0,
            "total_krw_pl_rate": 0,
            "total_purchase": 2_000_000,
            "contrib_total": 3_700_000,
        },
        "holdings": [
            {
                "market": "국내",
                "currency": "KRW",
                "name": "SK하이닉스",
                "code": "000660",
                "qty": 10,
                "avg_price": 200000,
                "cur_price": 200000,
                "eval_amt": 2_000_000,
                "eval_amt_krw": 2_000_000,
                "pl": 0,
                "pl_krw": 0,
                "pl_rate": 0,
            }
        ],
        "allocation": [
            {
                "name": "SK하이닉스",
                "code": "000660",
                "market": "국내",
                "type": "stock",
                "value": 2_000_000,
            },
            {
                "name": "현금",
                "code": "CASH",
                "market": "현금",
                "type": "cash",
                "value": 1_700_000,
            },
        ],
        "trades": [],
        "contributions": [
            {"date": "2026-06-29", "member": "한결", "amount": 3_700_000}
        ],
        "history": [
            {
                "ym": "2026-06",
                "net_asset": 3_700_000,
                "contrib": 3_700_000,
                "pl": 0,
                "pl_rate": 0,
            }
        ],
    }


def main():
    if "--demo" in sys.argv:
        data = demo()
    else:
        data = build(load_config())

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(
        f"data.json 생성 완료 · "
        f"보유 {len(data.get('holdings', []))}종목 · "
        f"체결 {len(data.get('trades', []))}건"
    )


if __name__ == "__main__":
    main()
