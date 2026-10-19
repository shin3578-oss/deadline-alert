# -*- coding: utf-8 -*-
"""
やることリスト 期限アラートBot
毎週1回（原則 月曜。月曜が休診なら**その週の最初の診療日**に繰り越す）
期限が近いタスク（期限切れ含む）をLINEワークスに通知
"""

import json, os, time, random, unicodedata
import httplib2
import jwt as pyjwt
import requests
from datetime import datetime, timezone, timedelta

from clinic_calendar import closed_reason, is_first_open_day_of_week
from google.oauth2 import service_account
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build

JST = timezone(timedelta(hours=9))

SPREADSHEET_ID = "1tXsGbPoJjQ65Xh3pJZwCZikeaUL_WDt5xNaMUug_wEs"
SHEET_GID      = 1576278866
ALERT_DAYS     = int(os.environ.get("ALERT_DAYS", "7"))

GOOGLE_CREDS       = os.environ["GOOGLE_CREDENTIALS_JSON"]
LW_CLIENT_ID       = "0cAEPO2Yzau80tSsEhxV"
LW_CLIENT_SECRET   = os.environ.get("LW_CLIENT_SECRET", "")   # 2026-09-23: 公開リポジトリに直書きされていた
LW_SERVICE_ACCOUNT = "3w266.serviceaccount@ovalcourtdental"
LW_BOT_ID          = "12266491"
LW_PRIVATE_KEY     = os.environ.get("LW_PRIVATE_KEY", "")
# テスト: "shin@ovalcourtdental"（個人宛）、本番: チャンネルID
LW_TARGET      = os.environ.get("LW_TARGET", "shin@ovalcourtdental")
LW_TARGET_TYPE = os.environ.get("LW_TARGET_TYPE", "user")  # "user" or "channel"
DRY_RUN        = os.environ.get("DRY_RUN", "false").lower() == "true"
# 週1回の縛り（今週の最初の診療日だけ送る）を外して今すぐ送る＝手動確認用
FORCE_SEND     = os.environ.get("FORCE_SEND", "false").lower() == "true"
# 進捗確認モード（2026-10-19〜）: off=従来どおり全員分を1通でチャンネルへ ／
#   test=全員分のDMを院長だけに送る（シートは書き換えない）／ live=担当者ごとにDMで聞く
PROGRESS_MODE  = os.environ.get("PROGRESS_MODE", "off").lower()
OWNER_ID       = "shin@ovalcourtdental"   # 院長（まとめの送り先）


# ========================
# LINE WORKS 送信
# ========================

def get_lw_access_token():
    now = int(time.time())
    token = pyjwt.encode(
        {"iss": LW_CLIENT_ID, "sub": LW_SERVICE_ACCOUNT, "iat": now, "exp": now + 3600},
        LW_PRIVATE_KEY, algorithm="RS256"
    )
    r = requests.post(
        "https://auth.worksmobile.com/oauth2/v2.0/token",
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": token,
            "client_id": LW_CLIENT_ID,
            "client_secret": LW_CLIENT_SECRET,
            "scope": "bot",
        },
        timeout=30
    )
    r.raise_for_status()
    return r.json()["access_token"]


def split_message(message, limit=1900):
    """2,000文字制限に合わせて行単位で分割する"""
    chunks = []
    current = []
    current_len = 0
    for line in message.split("\n"):
        line_len = len(line) + 1  # +1 は改行分
        if current_len + line_len > limit and current:
            chunks.append("\n".join(current))
            current = [line]
            current_len = line_len
        else:
            current.append(line)
            current_len += line_len
    if current:
        chunks.append("\n".join(current))
    return chunks


_LW_TOKEN = None

def send_to_lineworks(message, target=None, target_type=None):
    global _LW_TOKEN
    target = target or LW_TARGET
    target_type = target_type or LW_TARGET_TYPE
    if _LW_TOKEN is None:
        _LW_TOKEN = get_lw_access_token()
    headers = {"Authorization": f"Bearer {_LW_TOKEN}", "Content-Type": "application/json"}

    if target_type == "user":
        url = f"https://www.worksapis.com/v1.0/bots/{LW_BOT_ID}/users/{target}/messages"
    else:
        url = f"https://www.worksapis.com/v1.0/bots/{LW_BOT_ID}/channels/{target}/messages"

    chunks = split_message(message)
    print(f"送信分割数: {len(chunks)}通")
    for i, chunk in enumerate(chunks, 1):
        r = requests.post(url, headers=headers,
                          json={"content": {"type": "text", "text": chunk}},
                          timeout=30)
        r.raise_for_status()
        print(f"LINE WORKS送信完了 ({i}/{len(chunks)}) → {target_type}:{target}")
        if len(chunks) > 1:
            time.sleep(1)  # 連続送信の間隔


# ========================
# Google Sheets 読み取り
# ========================

def open_sheet(write=False):
    """(service, シート名) を返す。write=True のときだけ書き込み権限を取る"""
    scope = "https://www.googleapis.com/auth/spreadsheets" if write         else "https://www.googleapis.com/auth/spreadsheets.readonly"
    creds = service_account.Credentials.from_service_account_info(
        json.loads(GOOGLE_CREDS), scopes=[scope]
    )
    authorized_http = AuthorizedHttp(creds, http=httplib2.Http(timeout=30))
    service = build("sheets", "v4", http=authorized_http)

    # GID からシート名を取得
    spreadsheet = service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute(num_retries=3)
    sheet_name = None
    for sheet in spreadsheet["sheets"]:
        if sheet["properties"]["sheetId"] == SHEET_GID:
            sheet_name = sheet["properties"]["title"]
            break

    if not sheet_name:
        raise ValueError(f"シートGID {SHEET_GID} が見つかりません")

    print(f"シート名: {sheet_name}")
    return service, sheet_name


def get_sheet_data(write=False):
    service, sheet_name = open_sheet(write)
    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{sheet_name}!A:M"
    ).execute(num_retries=3)
    return result.get("values", []), service, sheet_name


# ========================
# 期限チェック
# ========================

def parse_deadline(date_str):
    for fmt in ["%Y/%m/%d", "%Y-%m-%d", "%m/%d/%Y"]:
        try:
            return datetime.strptime(date_str.strip(), fmt).date()
        except ValueError:
            continue
    return None


def check_deadlines(rows):
    today = datetime.now(JST).date()
    alert_limit = today + timedelta(days=ALERT_DAYS)
    alerts = []

    for i, row in enumerate(rows):
        if i == 0:  # ヘッダー行スキップ
            continue
        if not row or not row[0].strip():
            continue

        task_name    = row[0] if len(row) > 0 else ""   # A列: タスク
        assignee     = row[2] if len(row) > 2 else ""   # C列: 担当者
        deadline_str = row[4] if len(row) > 4 else ""   # E列: 期限
        status       = row[5] if len(row) > 5 else ""   # F列: ステータス

        if not task_name:
            continue

        # 完了済みはスキップ
        if status in ("完了", "ブロック済み"):
            continue

        if deadline_str:
            deadline = parse_deadline(deadline_str)
            if deadline:
                days_left = (deadline - today).days
                alerts.append({
                    "task":      task_name,
                    "assignee":  assignee or "未設定",
                    "deadline":  deadline.strftime("%Y/%m/%d"),
                    "days_left": days_left,
                })
            else:
                # 期限の書式が読めない行を黙って落とさない（期限なし扱いで一覧に載せる）
                alerts.append({
                    "task":      f"{task_name}（⚠️期限『{deadline_str.strip()}』が読み取れません）",
                    "assignee":  assignee or "未設定",
                    "deadline":  "",
                    "days_left": None,
                })
        else:
            alerts.append({
                "task":      task_name,
                "assignee":  assignee or "未設定",
                "deadline":  "",
                "days_left": None,
            })

    return alerts


# ========================
# メッセージ組み立て
# ========================

SHEET_URL = "https://docs.google.com/spreadsheets/d/1tXsGbPoJjQ65Xh3pJZwCZikeaUL_WDt5xNaMUug_wEs/edit?gid=1576278866#gid=1576278866"

KAOMOJI_LIST = [
    "(´∀｀*)ﾉ",
    "(＾▽＾)/",
    "(*´ω｀*)",
    "ヽ(´▽`)/",
    "(o^^o)",
    "(＊˘︶˘＊)",
    "(*´∀｀*)",
    "٩(ˊᗜˋ*)و",
    "(≧▽≦)/",
    "(*^▽^*)",
    "('Д')",
    "( ﾟДﾟ)",
    "(・∀・)",
    "( ﾟДﾟ)y─┛~~",
]

def build_message(alerts):
    today = datetime.now(JST).strftime("%Y/%m/%d")
    lines = [f"小林からのタスク依頼リスト　未完了タスク一覧（{today}）"]

    # 担当者ごとにグループ化（期限あり→期限なし の順でソート）
    by_assignee = {}
    for a in sorted(alerts, key=lambda x: (x["days_left"] is None, x["days_left"] or 0)):
        name = a["assignee"]
        by_assignee.setdefault(name, []).append(a)

    for assignee, tasks in by_assignee.items():
        lines.append(f"\n【{assignee}】")
        for a in tasks:
            if a["days_left"] is None:
                label = "📌 期限なし"
                lines.append(f"・{a['task']}")
                lines.append(f"　（{label}）")
            elif a["days_left"] < 0:
                label = f"🔴 {abs(a['days_left'])}日超過"
                lines.append(f"・{a['task']}")
                lines.append(f"　{a['deadline']}（{label}）")
            elif a["days_left"] == 0:
                label = "⚠️ 今日"
                lines.append(f"・{a['task']}")
                lines.append(f"　{a['deadline']}（{label}）")
            else:
                label = f"⚠️ あと{a['days_left']}日"
                lines.append(f"・{a['task']}")
                lines.append(f"　{a['deadline']}（{label}）")

    kaomoji = random.choice(KAOMOJI_LIST)
    lines.append(f"\n【小林からのタスク依頼】\n{SHEET_URL}\n\n※スプレッドシートにコメントできます、ステータス変更や完了報告、マイルストーン記入など、変更希望あればコメントお願いします {kaomoji}")
    return "\n".join(lines)


# ========================
# 進捗確認（担当者ごとに個別DMで聞く）
# ========================
# 回答欄は依頼リストの J〜M 列。J=進捗（終わった／終わっていない） K=終わる見込み
# L=単発／ルーティン M=詰まり・質問。担当者の宛先は同じスプレッドシートの「担当者名簿」タブ
# （コードに人の名前とIDを書かない＝公開リポジトリのため／異動の度に直さないため）。
ANS_COLS = (9, 10, 11, 12)          # J〜M の列番号（0始まり）


def has_answer(t):
    """毎週空にするのは J・K・M。L（単発／ルーティン）は仕事の性質なので残す"""
    return bool(t["ans"][0] or t["ans"][1] or t["ans"][3])

ROSTER_TAB  = "担当者名簿"
HISTORY_TAB = "回答履歴"


def norm(name):
    """名字の表記ゆれ（全角半角・空白）をそろえる"""
    return unicodedata.normalize("NFKC", name or "").replace(" ", "").strip()


def load_roster(service):
    r = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID, range=f"{ROSTER_TAB}!A2:D200"
    ).execute(num_retries=3)
    roster = {}
    for row in r.get("values", []):
        if len(row) >= 3 and row[0].strip() and row[2].strip():
            key = norm(row[0])
            if key in roster:
                raise ValueError(f"「{ROSTER_TAB}」に同じ担当者名が2行あります: {key}")
            call = row[3].strip() if len(row) > 3 and row[3].strip() else f"{key}さん"
            roster[key] = {"name": row[1].strip(), "id": row[2].strip(), "call": call}
    if not roster:
        raise ValueError(f"シート「{ROSTER_TAB}」から宛先が1件も読めません")
    return roster


def parse_rows(rows):
    """全行を {row, task, assignee, deadline, days_left, status, ans} にする。row は実際の行番号"""
    today = datetime.now(JST).date()
    out = []
    for i, row in enumerate(rows):
        if i == 0 or not row or not row[0].strip():
            continue
        row = list(row) + [""] * (13 - len(row))
        dl = parse_deadline(row[4]) if row[4].strip() else None
        out.append({
            "row": i + 1,
            "task": row[0].strip(),
            "assignee": norm(row[2]),
            "deadline_str": row[4].strip(),
            "deadline": dl,
            "days_left": (dl - today).days if dl else None,
            "status": row[5].strip(),
            "ans": [row[c].strip() for c in ANS_COLS],
        })
    return out


def is_open(t):
    return t["status"] not in ("完了", "ブロック済み")


def deadline_label(t):
    if t["deadline"] is None:
        if t["deadline_str"]:
            return f"期限『{t['deadline_str']}』が読み取れません"
        return "期限なし"
    d = t["deadline"].strftime("%Y/%m/%d")
    n = t["days_left"]
    if n < 0:
        return f"期限 {d}（🔴 {abs(n)}日超過）"
    if n == 0:
        return f"期限 {d}（⚠️ 今日）"
    return f"期限 {d}（あと{n}日）"


def build_ask_message(call, tasks):
    first_row = tasks[0]["row"]
    lines = [f"{call}、小林からのタスク依頼の進捗を教えてください（{len(tasks)}件）", ""]
    for n, t in enumerate(tasks, 1):
        lines.append(f"{n}. 【{t['row']}行目】{t['task']}")
        lines.append(f"　{deadline_label(t)}")
    lines += [
        "",
        "▼ 下のシートを開き、上の行番号の行の J〜M 列に入力してください（J・L はプルダウン）",
        "J 進捗：終わった／終わっていない",
        "K 終わっていない場合、いつ頃終わりそうか（例：10/31、来週中）",
        "L 単発の依頼か、ルーティン（ずっと続ける仕事）にするか",
        "M 詰まっていること・質問（なければ空欄でOK）",
        f"{SHEET_URL.split('#')[0]}#gid={SHEET_GID}&range=J{first_row}",
        "",
        "分からないことは事務スタッフに聞いてください。",
    ]
    return "\n".join(lines)


def build_owner_digest(parsed, open_asked, no_owner, failed):
    """院長宛: 前回の回答のまとめ。open_asked＝いま聞く対象（未回答の数え方に使う）"""
    today = datetime.now(JST).strftime("%Y/%m/%d")
    answered = [t for t in parsed if has_answer(t)]
    lines = [f"タスク依頼 進捗の回答まとめ（{today}）"]
    if not answered:
        lines.append("前回の回答は1件もありません。")
    else:
        stuck = [t for t in answered if t["ans"][3]]
        done = [t for t in answered if t["ans"][0] == "終わった"]
        going = [t for t in answered if t["ans"][0] != "終わった"]
        lines.append(f"回答あり {len(answered)}件")
        if stuck:
            lines.append("\n💬 詰まり・質問")
            for t in stuck:
                lines.append(f"・{t['assignee']}｜{t['task']}（{t['row']}行目）")
                lines.append(f"　→ {t['ans'][3]}")
        if done:
            lines.append("\n✅ 「終わった」と回答（ステータスを完了にしてよいか確認）")
            for t in done:
                lines.append(f"・{t['assignee']}｜{t['task']}（{t['row']}行目・いまのステータス：{t['status'] or '空欄'}）")
        if going:
            lines.append("\n⏳ 終わっていない／途中")
            for t in going:
                bits = []
                if t["ans"][0]:
                    bits.append(t["ans"][0])
                if t["ans"][1]:
                    bits.append(f"見込み：{t['ans'][1]}")
                if t["ans"][2]:
                    bits.append(t["ans"][2])
                lines.append(f"・{t['assignee']}｜{t['task']}（{t['row']}行目）")
                if bits:
                    lines.append("　" + "／".join(bits))
    # 未回答（いま未完了で、回答欄が空の件数を担当者別に）
    blank = {}
    for t in open_asked:
        if not t["ans"][0]:
            blank[t["assignee"]] = blank.get(t["assignee"], 0) + 1
    if answered and blank:
        lines.append("\n❓ 未回答（件数）")
        for a, n in sorted(blank.items(), key=lambda x: -x[1]):
            lines.append(f"・{a}：{n}件")
    if no_owner:
        lines.append("\n⚠️ 担当が空欄、または名簿にない担当者の未完了（担当を決めてください）")
        for t in no_owner:
            who = t["assignee"] or "担当空欄"
            lines.append(f"・{who}｜{t['task']}（{t['row']}行目）")
    if failed:
        lines.append("\n🔴 DMを送れなかった担当者: " + "、".join(failed))
    lines.append(f"\n{SHEET_URL}")
    return "\n".join(lines)


def archive_and_clear(service, sheet_name, answered, digest):
    """前回の回答を「回答履歴」に控え → 院長にまとめを送り → 回答欄を空にする。
    控えと院長まとめが済むまで消さない（消した後で落ちて、まとめが二度と出ない事故を防ぐ）"""
    today = datetime.now(JST).strftime("%Y/%m/%d")
    if not answered:
        send_to_lineworks(digest, OWNER_ID, "user")
        return
    values = [[today, t["assignee"], t["row"], t["task"], *t["ans"]] for t in answered]
    service.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID, range=f"{HISTORY_TAB}!A:H",
        valueInputOption="RAW", insertDataOption="INSERT_ROWS", body={"values": values}
    ).execute(num_retries=3)
    print(f"回答履歴に控えました: {len(values)}件")
    send_to_lineworks(digest, OWNER_ID, "user")
    service.spreadsheets().values().batchClear(
        spreadsheetId=SPREADSHEET_ID,
        body={"ranges": [f"{sheet_name}!{c}{t['row']}" for t in answered for c in ("J", "K", "M")]}
    ).execute(num_retries=3)
    print("回答欄（J・K・M）を空にしました")


def run_progress_flow():
    live = PROGRESS_MODE == "live"
    print(f"進捗確認モード: {PROGRESS_MODE}")
    rows, service, sheet_name = get_sheet_data(write=live)
    print(f"取得行数: {len(rows)}")
    roster = load_roster(service)
    parsed = parse_rows(rows)

    open_tasks = [t for t in parsed if is_open(t)]
    asked, no_owner = {}, []
    for t in open_tasks:
        if t["assignee"] in roster:
            asked.setdefault(t["assignee"], []).append(t)
        else:
            no_owner.append(t)
    answered = [t for t in parsed if has_answer(t)]
    print(f"未完了 {len(open_tasks)}件 ／ 聞く相手 {len(asked)}人 ／ 担当不明 {len(no_owner)}件 ／ 前回の回答 {len(answered)}件")

    open_asked = [t for v in asked.values() for t in v]
    asks = {s: build_ask_message(roster[s]["call"], ts) for s, ts in asked.items()}

    if DRY_RUN:
        # 公開リポジトリのログは誰でも読めるため、文面・タスク名は出さず件数だけにする
        print(f"[DRY_RUN] 質問DM {len(asks)}人分／院長まとめ1通。送信とシート更新をスキップしました")
        return

    today = datetime.now(JST).strftime("%Y/%m/%d")
    if live:
        last = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID, range=f"{HISTORY_TAB}!J1").execute(num_retries=3)
        if (last.get("values") or [[""]])[0][0] == today and not FORCE_SEND:
            print("本日すでに実行済み（二重に聞かないため終了）。やり直すときは force_send=true")
            return
        archive_and_clear(service, sheet_name, answered,
                          digest=build_owner_digest(parsed, open_asked, no_owner, []))
        service.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID, range=f"{HISTORY_TAB}!J1", valueInputOption="RAW",
            body={"values": [[today]]}).execute(num_retries=3)

    failed = []
    for s, m in asks.items():
        try:
            if live:
                send_to_lineworks(m, roster[s]["id"], "user")
            else:
                send_to_lineworks(f"【テスト送信・本来の宛先：{roster[s]['name']}】\n{m}", OWNER_ID, "user")
        except Exception as e:
            print(f"送信失敗 {s}: {e}")
            failed.append(s)
        time.sleep(1)

    if PROGRESS_MODE == "test":
        send_to_lineworks("【テスト送信】\n" + build_owner_digest(parsed, open_asked, no_owner, failed), OWNER_ID, "user")
    elif failed:
        send_to_lineworks("🔴 進捗確認のDMを送れなかった担当者: " + "、".join(failed), OWNER_ID, "user")
    if failed:
        raise RuntimeError(f"DMを送れなかった担当者: {failed}")


# ========================
# Main
# ========================

def main():
    now_jst = datetime.now(JST)
    print(f"期限アラートBot 開始: {now_jst.strftime('%Y-%m-%d %H:%M:%S')} JST")

    # ── 休診日は配信せず、その週の最初の診療日に繰り越す（院長指示 2026-08-12）──
    # 以前は「休診日ならスキップ」だけだったので、月曜が祝日やお盆に当たった週は
    # その週の期限アラートが丸ごと消えていた。いまは月曜〜土曜のうち
    # **その週で最初の診療日**に1回だけ送る（通常の週は従来どおり月曜）。
    #
    # 休診日の判定はアポツールの rest-all を書き出した公開カレンダー
    # （clinic_calendar.py）。お盆・年末年始・臨時休診は祝日ではないので
    # jpholiday だけでは拾えない。
    if not FORCE_SEND:
        reason = closed_reason(now_jst.date())
        if reason:
            print(f"本日は{reason} → 休診日のため配信しません"
                  "（今週の最初の診療日に繰り越します）")
            return
        if not is_first_open_day_of_week(now_jst.date()):
            print("今週はすでに診療日がありました（その日に配信済み）→ 今日は送りません")
            return

    if PROGRESS_MODE in ("test", "live"):
        run_progress_flow()
        return

    for attempt in range(3):
        try:
            rows, _service, _sheet_name = get_sheet_data()
            break
        except Exception as e:
            print(f"スプレッドシート取得失敗 ({attempt+1}/3): {e}")
            if attempt == 2:
                raise
            time.sleep(10)
    print(f"取得行数: {len(rows)}")

    alerts = check_deadlines(rows)
    print(f"アラート対象: {len(alerts)}件")

    if not alerts:
        print("アラート対象なし。送信スキップ。")
        return

    message = build_message(alerts)
    print(f"送信メッセージ:\n{message}")

    if DRY_RUN:
        print("[DRY_RUN] LINEワークス送信をスキップしました")
        return

    send_to_lineworks(message)


if __name__ == "__main__":
    main()

