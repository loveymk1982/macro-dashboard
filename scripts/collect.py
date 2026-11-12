#!/usr/bin/env python3
"""매일 실행되어 data.json 을 갱신합니다. 표준 라이브러리만 사용합니다."""
import os, io, csv, json, time, datetime as dt
import urllib.request, urllib.error

KST = dt.timezone(dt.timedelta(hours=9))
NOW = dt.datetime.now(KST)
TODAY = NOW.date()
START = TODAY - dt.timedelta(days=400)   # 화면은 최근 1년만 사용, 여유분 포함
OUT = "data.json"
UA = {"User-Agent": "macro-dashboard/1.0"}


def get(url, tries=2, timeout=20):
    err = None
    host = url.split("/")[2]
    for i in range(tries):
        try:
            t0 = time.time()
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
                body = r.read().decode("utf-8")
            print(f"  요청 성공 {host} ({time.time() - t0:.1f}초)", flush=True)
            return body
        except Exception as e:  # URL(키 포함 가능)을 출력하지 않도록 유형만 기록
            err = e
            print(f"  요청 실패 {host}: {type(e).__name__} {getattr(e, 'code', '')}", flush=True)
            time.sleep(2)
    code = getattr(err, "code", "")
    raise RuntimeError(f"{type(err).__name__} {code}".strip())


def fx_rates():
    syms = "KRW,MXN,CNY,INR,EUR,GBP,JPY,CAD,SEK,CHF"
    out, s = {}, START
    while s <= TODAY:
        e = min(s + dt.timedelta(days=80), TODAY)
        a = min(s, TODAY - dt.timedelta(days=6)) if e == TODAY else s
        j = json.loads(get(f"https://api.frankfurter.dev/v1/{a}..{e}?base=USD&symbols={syms}"))
        out.update(j.get("rates", {}))
        s = e + dt.timedelta(days=1)
    return out


def fred(sid):
    rows = list(csv.reader(io.StringIO(get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}&cosd={START}", tries=1, timeout=25))))
    return [[r[0], float(r[1])] for r in rows[1:] if len(r) > 1 and r[1] not in ("", ".")]


_TR = {}


def treasury(col):
    """미 재무부 일별 국채수익률(공식, 키 불필요)."""
    pts = []
    for y in range(START.year, TODAY.year + 1):
        if y not in _TR:
            url = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
                   f"daily-treasury-rates.csv/{y}/all?type=daily_treasury_yield_curve&field_tdr_date_value={y}&page&_format=csv")
            _TR[y] = list(csv.DictReader(io.StringIO(get(url, tries=2, timeout=25))))
        for r in _TR[y]:
            v = (r.get(col) or "").strip()
            if v and v.upper() != "N/A":
                m, d, yy = r["Date"].split("/")
                pts.append([f"{yy}-{m}-{d}", float(v)])
    return sorted(pts)


def nyfed_target():
    """뉴욕연은 EFFR 응답의 연방기금금리 목표 상단."""
    j = json.loads(get(f"https://markets.newyorkfed.org/api/rates/unsecured/effr/search.json?startDate={START}&endDate={TODAY}", tries=2, timeout=25))
    return sorted([r["effectiveDate"], float(r["targetRateTo"])] for r in j["refRates"] if r.get("targetRateTo") is not None)


def first_ok(*pairs):
    def run():
        errs = []
        for label, fn in pairs:
            try:
                p = fn()
                if p:
                    run.used = label
                    return p
            except Exception as e:
                errs.append(f"{label}: {e}")
        raise RuntimeError(" | ".join(errs))
    return run


def ecos_item(key, stat, must):
    j = json.loads(get(f"https://ecos.bok.or.kr/api/StatisticItemList/{key}/json/kr/1/200/{stat}/"))
    if "StatisticItemList" not in j:
        raise RuntimeError("ECOS 항목조회 실패: " + str(j.get("RESULT", {}).get("CODE", "")))
    rows = j["StatisticItemList"]["row"]
    for r in rows:
        if must in r["ITEM_NAME"]:
            return r["ITEM_CODE"]
    raise RuntimeError(f"ECOS 항목 없음({must}); 목록: " + ", ".join(r["ITEM_NAME"] for r in rows[:15]))


def ecos(key, stat, must, cycle="D"):
    code = ecos_item(key, stat, must)
    s, e = START.strftime("%Y%m%d"), TODAY.strftime("%Y%m%d")
    j = json.loads(get(f"https://ecos.bok.or.kr/api/StatisticSearch/{key}/json/kr/1/1000/{stat}/{cycle}/{s}/{e}/{code}"))
    if "StatisticSearch" not in j:
        raise RuntimeError("ECOS 조회 실패: " + str(j.get("RESULT", {}).get("CODE", "")))
    return [[f"{r['TIME'][:4]}-{r['TIME'][4:6]}-{r['TIME'][6:8]}", float(r["DATA_VALUE"])]
            for r in j["StatisticSearch"]["row"] if r.get("DATA_VALUE") not in (None, "")]


def main():
    prev = {}
    if os.path.exists(OUT):
        try:
            prev = json.load(open(OUT, encoding="utf-8")).get("series", {})
        except Exception:
            prev = {}
    cfg = json.load(open("config.json", encoding="utf-8"))
    series, errors = {}, []

    def put(key, name, unit, source, fn):
        print(f"[{key}] 수집 시작", flush=True)
        try:
            pts = [p for p in fn() if p[0] >= str(START)]
            source = getattr(fn, "used", source)
            if not pts:
                raise RuntimeError("데이터 없음")
            series[key] = {"name": name, "unit": unit, "source": source, "asof": pts[-1][0], "points": pts}
        except Exception as e:
            errors.append(f"{key}: {e}")
            if key in prev:
                series[key] = dict(prev[key], stale=True)

    print("수집 시작", NOW.isoformat(timespec="minutes"), flush=True)
    R = {}
    try:
        R = fx_rates()
    except Exception as e:
        errors.append(f"환율(Frankfurter): {e}")

    def from_rates(f):
        def run():
            pts = []
            for d, r in sorted(R.items()):
                try:
                    pts.append([d, round(f(r), 4)])
                except Exception:
                    pass
            return pts
        return run

    ecb = "ECB 기준환율(Frankfurter API)"
    put("usdkrw", "달러/원", "원", ecb, from_rates(lambda r: r["KRW"]))
    put("usdmxn", "달러/페소", "MXN", ecb, from_rates(lambda r: r["MXN"]))
    put("mxnkrw", "페소/원", "원", ecb + ", USD 환율로 교차 계산", from_rates(lambda r: r["KRW"] / r["MXN"]))
    put("usdcny", "달러/위안", "CNY", ecb, from_rates(lambda r: r["CNY"]))
    put("cnykrw", "위안/원", "원", ecb + ", USD 환율로 교차 계산", from_rates(lambda r: r["KRW"] / r["CNY"]))
    put("usdinr", "달러/루피", "INR", ecb, from_rates(lambda r: r["INR"]))
    put("inrkrw", "루피/원", "원", ecb + ", USD 환율로 교차 계산", from_rates(lambda r: r["KRW"] / r["INR"]))
    put("dxy", "달러인덱스(산식 산출)", "pt", "ECB 기준환율에 DXY 공식 산식 적용(ICE 공식 지수 아님, 소폭 차이 가능)",
        from_rates(lambda r: 50.14348112 * r["EUR"] ** 0.576 * r["JPY"] ** 0.136 * r["GBP"] ** 0.119
                   * r["CAD"] ** 0.091 * r["SEK"] ** 0.042 * r["CHF"] ** 0.036))

    TRL = "미 재무부 일별 국채수익률(Daily Treasury Par Yield Curve)"
    put("us2y", "미국 국채 2년", "%", TRL, first_ok((TRL, lambda: treasury("2 Yr")), ("FRED DGS2", lambda: fred("DGS2"))))
    put("us10y", "미국 국채 10년", "%", TRL, first_ok((TRL, lambda: treasury("10 Yr")), ("FRED DGS10", lambda: fred("DGS10"))))
    put("usbase", "미국 기준금리(상단)", "%", "연방기금금리 목표 상단",
        first_ok(("뉴욕연은 EFFR 목표금리 상단", nyfed_target), ("FRED DFEDTARU(목표 상단)", lambda: fred("DFEDTARU"))))

    key = os.environ.get("ECOS_API_KEY", "").strip()
    if key:
        put("kr2y", "한국 국채 2년", "%", "한국은행 ECOS 817Y002", lambda: ecos(key, "817Y002", "국고채(2년)"))
        put("kr10y", "한국 국채 10년", "%", "한국은행 ECOS 817Y002", lambda: ecos(key, "817Y002", "국고채(10년)"))
        put("krbase", "한국 기준금리", "%", "한국은행 ECOS 722Y001", lambda: ecos(key, "722Y001", "기준금리"))
    else:
        errors.append("ECOS_API_KEY 가 설정되지 않았습니다(저장소 Secrets 확인).")
        for k in ("kr2y", "kr10y", "krbase"):
            if k in prev:
                series[k] = dict(prev[k], stale=True)

    out = {"generated_at": NOW.isoformat(timespec="minutes"), "plan": cfg.get("plan", {}),
           "notes": cfg.get("notes", []), "errors": errors, "series": series}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print(f"수집 완료: 시리즈 {len(series)}개, 오류 {len(errors)}건")
    for e in errors:
        print(" -", e)


if __name__ == "__main__":
    main()
