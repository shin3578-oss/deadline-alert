# -*- coding: utf-8 -*-
"""
やることリスト 期限アラートBot
毎朝8:00 JST に実行
3日以内に期限が来るタスク（期限切れ含む）をLINEワークスに通知
"""

import json, os, time, random
import httplib2
import jpholiday
import jwt as pyjwt
import requests
from datetime import datetime, timezone, timedelta
from google.oauth2 import service_account
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build

JST = timezone(timedelta(hours=9))

SPREADSHEET_ID = "1tXsGbPoJjQ65Xh3pJZwCZikeaUL_WDt5xNaMUug_wEs"
SHEET_GID      = 1576278866
ALERT_DAYS     = int(os.environ.get("ALERT_DAYS", "7"))

GOOGLE_CREDS       = os.environ["GOOGLE_CREDENTIALS_JSON"]
LW_CLIENT_ID       = "0cAEPO2Yzau80tSsEhxV"
LW_CLIENT_SECRET   = "d7WfxxO2t1"
LW_SERVICE_ACCOUNT = "3w266.serviceaccount@ovalcourtdental"
LW_BOT_ID          = "12266491"
LW_PRIVATE_KEY     = os.environ.get("LW_PRIVATE_KEY", "")
# テスト: "shin@ovalcourtdental"（個人宛）、本番: チャンネルID
LW_TARGET      = os.environ.get("LW_TARGET", "shin@ovalcourtdental")
LW_TARGET_TYPE = os.environ.get("LW_TARGET_TYPE", "user")  # "user" or "channel"
DRY_RUN        = os.environ.get("DRY_RUN", "false").lower() == "true"


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


def send_to_lineworks(message):
    access_token = get_lw_access_token()
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}

    if LW_TARGET_TYPE == "user":
        url = f"https://www.worksapis.com/v1.0/bots/{LW_BOT_ID}/users/{LW_TARGET}/messages"
    else:
        url = f"https://www.worksapis.com/v1.0/bots/{LW_BOT_ID}/channels/{LW_TARGET}/messages"

    chunks = split_message(message)
    print(f"送信分割数: {len(chunks)}通")
    for i, chunk in enumerate(chunks, 1):
        r = requests.post(url, headers=headers,
                          json={"content": {"type": "text", "text": chunk}},
                          timeout=30)
        r.raise_for_status()
        print(f"LINE WORKS送信完了 ({i}/{len(chunks)}) → {LW_TARGET_TYPE}:{LW_TARGET}")
        if len(chunks) > 1:
            time.sleep(1)  # 連続送信の間隔


# ========================
# Google Sheets 読み取り
# ========================

def get_sheet_data():
    creds = service_account.Credentials.from_service_account_info(
        json.loads(GOOGLE_CREDS),
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
    )
    authorized_http = AuthorizedHttp(creds, http=httplib2.Http(timeout=30))
    service = build("sheets", "v4", http=authorized_http)

    # GID からシート名を取得
    spreadsheet = service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    sheet_name = None
    for sheet in spreadsheet["sheets"]:
        if sheet["properties"]["sheetId"] == SHEET_GID:
            sheet_name = sheet["properties"]["title"]
            break

    if not sheet_name:
        raise ValueError(f"シートGID {SHEET_GID} が見つかりません")

    print(f"シート名: {sheet_name}")

    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{sheet_name}!A:K"
    ).execute()
    return result.get("values", [])


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
# Main
# ========================

def clinic_closed_reason(d):
    """休診日（日曜・祝日）なら理由文字列、診療日なら None を返す。

    医院は月〜土 診療・日曜＋祝日 休診。祝日は jpholiday で判定（振替休日含む）。
    お盆・年末年始・臨時休診など「祝日でない休診日」は拾えない（既知の限界）。
    """
    if d.weekday() == 6:  # 6=日曜
        return "日曜（定休）"
    name = jpholiday.is_holiday_name(d)
    if name:
        return f"祝日（{name}）"
    return None


def main():
    now_jst = datetime.now(JST)
    print(f"期限アラートBot 開始: {now_jst.strftime('%Y-%m-%d %H:%M:%S')} JST")

    # ── 休診日（日曜・祝日）は配信しない ──
    closed_reason = clinic_closed_reason(now_jst.date())
    if closed_reason:
        print(f"本日は{closed_reason} → 休診日のため配信をスキップします")
        return

    for attempt in range(3):
        try:
            rows = get_sheet_data()
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

