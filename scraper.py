# -*- coding: utf-8 -*-
"""
住宅ローン金利トラッカー — データ収集スクリプト

2つの系列を組み立てて data/history.json と data/history.js を出力する。

  1) 変動金利(店頭)  … 日本銀行が公表する「短期プライムレート(最頻値)」の
                         変更履歴をスクレイピングし、店頭変動金利 = 短プラ + 1.0% で算出。
                         2009年以降ずっと 2.475% だった実態と一致する、堅牢な実データ。
  2) フラット35(最頻金利) … flat35.com のトップページから当月の最頻金利を自動取得。
                         過去分は seed_flat35.json(編集可能な参考値)で補う。

設計方針(壊れにくさ):
  - 各ソースは try/except で隔離。片方が失敗しても、もう片方と前回の値で動く。
  - ネットワーク不通・サイト構造変更時は、既存の history.json の値を温存する。
  - 変動金利は毎回フル履歴を組み直す(自己修復)。フラット35は seed + 当月実測を蓄積。
"""

import json
import re
import sys
import datetime
from pathlib import Path

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("必要なライブラリが未インストールです。次を実行してください:")
    print("    pip install requests beautifulsoup4 lxml")
    sys.exit(1)

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
DATA.mkdir(exist_ok=True)

START_MONTH = "2010-01"     # グラフの開始月
HENDO_SPREAD = 1.0          # 店頭変動金利 = 短期プライムレート最頻値 + 1.0%
UA = {"User-Agent": "Mozilla/5.0 (kinri-tracker; personal use)"}

BOJ_URL = "https://www.boj.or.jp/statistics/dl/loan/prime/prime.htm"
FLAT35_URL = "https://www.flat35.com/"
# フラット35 最頻金利の月次推移表(借入21年以上・融資率9割以下)。プレーンHTMLで全履歴を掲載。
FLAT35_HISTORY_URL = "https://lifeplan-fp.com/flat35.html"


# --------------------------------------------------------------------------
# 汎用ヘルパー
# --------------------------------------------------------------------------
def z2h(s: str) -> str:
    """全角数字を半角へ。"""
    return s.translate(str.maketrans("０１２３４５６７８９．", "0123456789."))


def month_iter(start: str, end: str):
    """'YYYY-MM' 区間を月単位で列挙。"""
    y, m = map(int, start.split("-"))
    ey, em = map(int, end.split("-"))
    while (y, m) <= (ey, em):
        yield f"{y:04d}-{m:02d}"
        m += 1
        if m > 12:
            m = 1
            y += 1


def current_month() -> str:
    t = datetime.date.today()
    return f"{t.year:04d}-{t.month:02d}"


# --------------------------------------------------------------------------
# ソース1: 日銀 短期プライムレート → 店頭変動金利
# --------------------------------------------------------------------------
def fetch_hendo_changes():
    """
    日銀の短期プライムレート表から、最頻値の変更履歴を返す。
    戻り値: [(effective_month 'YYYY-MM', short_prime_rate float), ...] 昇順
    """
    r = requests.get(BOJ_URL, headers=UA, timeout=25)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    soup = BeautifulSoup(r.text, "lxml")
    table = soup.find("table")
    if table is None:
        raise RuntimeError("日銀ページに表が見つかりません(構造変更の可能性)")

    changes = []
    for tr in table.find_all("tr"):
        cells = [z2h(c.get_text(strip=True)) for c in tr.find_all(["th", "td"])]
        if len(cells) < 2:
            continue
        d = re.search(r"(\d{4})\D+?(\d{1,2})月\s*(\d{1,2})日", cells[0])
        if not d:
            continue
        # 列1 = 短期プライムレート最頻値。先頭に数値があれば「変更あり」。
        m = re.match(r"\s*(\d+\.\d+)", cells[1])
        if m:
            eff = f"{int(d.group(1)):04d}-{int(d.group(2)):02d}"
            changes.append((eff, float(m.group(1))))

    if not changes:
        raise RuntimeError("短期プライムレートの変更履歴を抽出できませんでした")
    changes.sort()
    return changes


def hendo_value_for_month(changes, month):
    """指定月に適用されている店頭変動金利(= 最新の短プラ最頻値 + spread)。"""
    val = None
    for eff, rate in changes:
        if eff <= month:
            val = rate
        else:
            break
    if val is None:
        return None
    return round(val + HENDO_SPREAD, 3)


# --------------------------------------------------------------------------
# ソース2: flat35.com → フラット35 最頻金利(当月)
# --------------------------------------------------------------------------
def fetch_flat35_current():
    """
    flat35.com トップの最頻金利ブロックから当月の金利を取得。
    戻り値: dict {month, rate, all_numbers, hatsu5_rate} / 失敗時 None
    サイトが「当初5年引下げ(子育てプラス等)」の演出表示のため、
    標準となる『6年目以降・最も多い金利』を採用する。
    """
    r = requests.get(FLAT35_URL, headers=UA, timeout=25)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    soup = BeautifulSoup(r.text, "lxml")

    node = soup.find(string=re.compile("最頻金利"))
    if node is None:
        raise RuntimeError("flat35: 『最頻金利』ブロックが見つかりません")

    # ブロックを含む親要素のテキストから金利数値を抽出
    container = node
    for _ in range(6):
        if container.parent is None:
            break
        container = container.parent
    text = z2h(container.get_text(" ", strip=True))

    # 対象月(例: 2026年7月)。取れなければ実行時の当月。
    md = re.search(r"(\d{4})年\s*(\d{1,2})月", text)
    month = f"{int(md.group(1)):04d}-{int(md.group(2)):02d}" if md else current_month()

    nums = [float(x) for x in re.findall(r"(?<!\d)(\d\.\d{2})(?!\d)", text)]
    nums = [n for n in nums if 0.3 <= n <= 6.0]
    if not nums:
        raise RuntimeError("flat35: 金利数値を抽出できませんでした")

    # 表示順: [当初5年(最も多い), 6年目以降(最も多い), 当初5年(最低), ...]
    # 標準金利 = 6年目以降(最も多い) = 2番目。無ければ先頭を採用。
    rate = nums[1] if len(nums) >= 2 else nums[0]
    hatsu5 = nums[0]
    return {"month": month, "rate": rate, "all_numbers": nums, "hatsu5_rate": hatsu5}


def fetch_flat35_history():
    """
    フラット35 最頻金利の月次推移表(借入21年以上・融資率9割以下)を丸ごと取得。
    プレーンHTMLの表(列: 借入年月 / 金利範囲 / 最頻金利)から全履歴を抽出。
    戻り値: dict {'YYYY-MM': 最頻金利(float)}
    """
    r = requests.get(FLAT35_HISTORY_URL, headers=UA, timeout=25)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    soup = BeautifulSoup(r.text, "lxml")
    table = soup.find("table")
    if table is None:
        raise RuntimeError("flat35推移表: 表が見つかりません(構造変更の可能性)")

    hist = {}
    for tr in table.find_all("tr"):
        cells = [z2h(c.get_text(" ", strip=True)) for c in tr.find_all(["th", "td"])]
        if len(cells) < 3:
            continue
        d = re.search(r"(\d{4})\D+?(\d{1,2})\D*月", cells[0])
        m = re.search(r"(\d+\.\d{2})", cells[2])   # 3列目 = 最頻金利
        if d and m:
            val = float(m.group(1))
            if 0.3 <= val <= 6.0:
                hist[f"{int(d.group(1)):04d}-{int(d.group(2)):02d}"] = val

    if len(hist) < 12:
        raise RuntimeError(f"flat35推移表: 抽出件数が少なすぎます({len(hist)}件)")
    return hist


# --------------------------------------------------------------------------
# メイン
# --------------------------------------------------------------------------
def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def main():
    end_month = current_month()
    labels = list(month_iter(START_MONTH, end_month))

    # 前回の出力(フォールバック用)
    prev = load_json(DATA / "history.json", {})
    prev_series = {s["id"]: s for s in prev.get("series", [])}

    status = {}

    # --- 変動金利 ---------------------------------------------------------
    try:
        changes = fetch_hendo_changes()
        hendo_data = [hendo_value_for_month(changes, mth) for mth in labels]
        hendo_changes = [{"month": e, "short_prime": v,
                          "hendo": round(v + HENDO_SPREAD, 3)} for e, v in changes]
        status["hendo"] = f"OK ({len(changes)}件の変更点, 最新 {hendo_data[-1]}%)"
    except Exception as e:
        status["hendo"] = f"取得失敗のため前回値を使用: {e}"
        old = prev_series.get("hendo", {})
        # 前回labelsに揃え直す
        old_map = dict(zip(prev.get("labels", []), old.get("data", [])))
        hendo_data = [old_map.get(mth) for mth in labels]
        hendo_changes = old.get("changes", [])

    # --- フラット35(月次) ------------------------------------------------
    # 土台: シード(オフライン/障害時のフォールバック用の月次スナップショット)
    seed = load_json(BASE / "seed_flat35.json", {"rates": {}}).get("rates", {})
    flat_map = {k: float(v) for k, v in seed.items()}
    flat_meta = {}

    # (a) 推移表から月次フル履歴を取得して上書き(毎回組み直す=自己修復)
    try:
        hist = fetch_flat35_history()
        flat_map.update(hist)
        status["flat35_history"] = f"OK (月次推移表 {len(hist)}件, 最新 {max(hist)})"
    except Exception as e:
        status["flat35_history"] = f"推移表の取得に失敗(シード+前回値を使用): {e}"
        old = prev_series.get("flat35", {})   # 前回値を温存
        for mth, v in zip(prev.get("labels", []), old.get("data", [])):
            if v is not None and mth not in flat_map:
                flat_map[mth] = v

    # (b) 当月がまだ推移表に無ければ flat35.com の当月値で補完(最速反映)
    cm = current_month()
    if cm not in flat_map:
        try:
            cur = fetch_flat35_current()
            # 直近既知値から極端に外れていなければ採用(誤抽出ガード)
            recent = [flat_map[k] for k in sorted(flat_map)[-3:]] if flat_map else []
            ref = recent[-1] if recent else cur["rate"]
            if abs(cur["rate"] - ref) <= 1.0:
                flat_map[cur["month"]] = cur["rate"]
                flat_meta = {"scraped_month": cur["month"], "scraped_rate": cur["rate"]}
                status["flat35_current"] = f"OK (当月 {cur['month']} = {cur['rate']}% をflat35.comから補完)"
            else:
                status["flat35_current"] = f"当月値 {cur['rate']}% は直近{ref}%と乖離のため不採用"
        except Exception as e:
            status["flat35_current"] = f"当月の即時補完に失敗(推移表待ち): {e}"

    flat_data = [flat_map.get(mth) for mth in labels]

    # --- 出力 -------------------------------------------------------------
    out = {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "labels": labels,
        "series": [
            {
                "id": "hendo",
                "label": "変動金利(店頭)",
                "color": "#e8590c",
                "source": "日本銀行『短期プライムレート(最頻値)』+ 1.0%",
                "url": BOJ_URL,
                "note": "店頭変動金利の目安。実際の適用金利は各行の優遇幅で下がります。",
                "data": hendo_data,
                "changes": hendo_changes,
            },
            {
                "id": "flat35",
                "label": "フラット35(最頻金利)",
                "color": "#1c7ed6",
                "source": "フラット35 最頻金利の月次推移(借入21年以上・融資率9割以下)",
                "url": FLAT35_HISTORY_URL,
                "note": ("借入21年以上・融資率9割以下の最頻金利(月次)。"
                         "推移表から毎回全履歴を取り込み、当月は必要に応じ flat35.com で補完。"),
                "data": flat_data,
                "meta": flat_meta,
            },
        ],
        "status": status,
    }

    (DATA / "history.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    (DATA / "history.js").write_text(
        "window.RATE_DATA = " + json.dumps(out, ensure_ascii=False) + ";",
        encoding="utf-8")

    print("=== 住宅ローン金利トラッカー: 更新完了 ===")
    for k, v in status.items():
        print(f"  [{k}] {v}")
    print(f"  出力: {DATA/'history.json'}")
    print(f"        {DATA/'history.js'}")


if __name__ == "__main__":
    main()
