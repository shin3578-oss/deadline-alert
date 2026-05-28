# -*- coding: utf-8 -*-
"""
やることリスト 期限アラートBot
毎朝8:00 JST に実行
3日以内に期限が来るタスク（期限切れ含む）をLINEワークスに通知
"""

import json, os, time
import jwt as pyjwt
import requests
from datetime import datetime, timezone, timedelta
from google.oauth2 import service_account
from googleapiclient.discovery import build

JST = timezone(timedelta(hours=9))

SPREADSHEET_ID = "1tXsGbPoJjQ65Xh3pJZwCZikeaUL_WDt5xNaMUug_wEs"
SHEET_GID      = 1576278866
ALERT_DAYS     = int(os.environ.get("ALERT_DAYS", "3"))

GOOGLE_CREDS       = os.environ["GOOGLE_CREDENTIALS_JSON"]
LW_CLIENT_ID       = "0cAEPO2Yzau80tSsEhxV"
LW_CLIENT_SECRET   = "d7WfxxO2t1"
LW_SERVICE_ACCOUNT = "3w266.serviceaccount@ovalcourtdental"
LW_BOT_ID          = "12266491"
LW_PRIVATE_KEY     = os.environ["LW_PRIVATE_KEY"]
# テスト: "shin@ovalcourtdental"（個人宛）、本番: チャンネルID
LW_TARGET      = os.environ.get("LW_TARGET", "shin@ovalcourtdental")
LW_TARGET_TYPE = os.environ.get("LW_TARGET_TYPE", "user")  # "user" or "channel"


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


def send_to_lineworks(message):
    access_token = get_lw_access_token()
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}

    if LW_TARGET_TYPE == "user":
        url = f"https://www.worksapis.com/v1.0/bots/{LW_BOT_ID}/users/{LW_TARGET}/messages"
    else:
        url = f"https://www.worksapis.com/v1.0/bots/{LW_BOT_ID}/channels/{LW_TARGET}/messages"

    r = requests.post(url, headers=headers,
                      json={"content": {"type": "text", "text": message}},
                      timeout=30)
    r.raise_for_status()
    print(f"LINE WORKS送信完了 → {LW_TARGET_TYPE}:{LW_TARGET}")


# ========================
# Google Sheets 読み取り
# ========================

def get_sheet_data():
    creds = service_account.Credentials.from_service_account_info(
        json.loads(GOOGLE_CREDS),
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
    )
    service = build("sheets", "v4", credentials=creds)

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
        if len(row) < 6:
            continue

        task_name    = row[0] if len(row) > 0 else ""   # A列: タスク
        assignee     = row[2] if len(row) > 2 else ""   # C列: 担当者
        deadline_str = row[4] if len(row) > 4 else ""   # E列: 期限
        status       = row[5] if len(row) > 5 else ""   # F列: ステータス

        if not task_name or not deadline_str:
            continue

        # 完了済みはスキップ
        if status == "完了":
            continue

        deadline = parse_deadline(deadline_str)
        if not deadline:
            continue

        days_left = (deadline - today).days
        if days_left <= ALERT_DAYS:  # 3日以内 or 期限切れ
            alerts.append({
                "task":      task_name,
                "assignee":  assignee or "未設定",
                "deadline":  deadline.strftime("%Y/%m/%d"),
                "days_left": days_left,
            })

    return alerts


# ========================
# メッセージ組み立て
# ========================

def build_message(alerts):
    today = datetime.now(JST).strftime("%Y/%m/%d")
    lines = [f"📋 タスク期限アラート（{today}）"]

    # 担当者ごとにグループ化（登場順を保持）
    by_assignee = {}
    for a in sorted(alerts, key=lambda x: x["days_left"]):
        name = a["assignee"]
        by_assignee.setdefault(name, []).append(a)

    for assignee, tasks in by_assignee.items():
        lines.append(f"\n【{assignee}】")
        for a in tasks:
            if a["days_left"] < 0:
                label = f"🔴 {abs(a['days_left'])}日超過"
            elif a["days_left"] == 0:
                label = "⚠️ 今日"
            else:
                label = f"⚠️ あと{a['days_left']}日"
            lines.append(f"・{a['task']}")
            lines.append(f"　{a['deadline']}（{label}）")

    return "\n".join(lines)


# ========================
# Main
# ========================

def main():
    now_jst = datetime.now(JST)
    print(f"期限アラートBot 開始: {now_jst.strftime('%Y-%m-%d %H:%M:%S')} JST")

    rows = get_sheet_data()
    print(f"取得行数: {len(rows)}")

    alerts = check_deadlines(rows)
    print(f"アラート対象: {len(alerts)}件")

    if not alerts:
        print("アラート対象なし。送信スキップ。")
        return

    message = build_message(alerts)
    print(f"送信メッセージ:\n{message}")

    send_to_lineworks(message)


if __name__ == "__main__":
    main()
