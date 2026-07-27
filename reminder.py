# ==========================================================
# 【何をする】GitHub Actions課金ブロック中（2026-07-27〜31）の手動実行リマインドを院長DMへ送る
# 【いつ動く】cybozu-bot（30分ごと・公開リポジトリなので無料枠外で稼働中）から
#             7:00＝その日の一覧／19:00＝アポツールの直前リマインド として dispatch される
# 【なぜここに】LINEワークスの鍵を持ち、かつ無料で動く公開リポジトリのアラートBotだから
# 8月1日に無料枠がリセットされたら、このファイルとワークフローは削除してよい
# ==========================================================
import os, time, datetime, requests
import jwt as pyjwt

JST = datetime.timezone(datetime.timedelta(hours=9))
CLIENT_ID = "0cAEPO2Yzau80tSsEhxV"
CLIENT_SECRET = os.environ["LW_CLIENT_SECRET"]
SERVICE_ACCOUNT = "3w266.serviceaccount@ovalcourtdental"
PRIVATE_KEY = os.environ["LW_PRIVATE_KEY"]
BOT_ID = "12266491"   # 既存Bot＝要対応（院長の対応が必要な通知はこのBot）
USER_ID = "shin@ovalcourtdental"
MODE = os.environ.get("REMINDER_MODE", "morning")

# (表示名, 実行時刻, 対象曜日=月0〜日6, 重要度, 補足)
TASKS = [
    ("アポツール（キャンセルリスク患者の処理）", "19:49", {0,1,2,3,4,5}, "★最重要", "検出・アイコン付与・欄外移動。問診票来院アラートも同時"),
    ("ささっとペイ未登録チェック", "9:00",  {0,1,2,3,4,5}, "中", "クリンチェック確認待ち通知も同時"),
    ("LINE公式 空き枠配信（本日分）", "9:00", {1,3,4}, "—", "※7月は月間配信上限で元々停止中。実行不要"),
    ("GBP空き枠自動投稿（翌日分）", "14:30", {6,0,1,2,3,4}, "中", "Googleビジネスプロフィールへの集患投稿"),
    ("コンサルスプシ同期＋面談記録Bot", "21:00", {0,1,2,3,4,5}, "低", "月次で回収できるので4日空いても実害小"),
    ("朝のアシスタント＋Gmail掃除", "5:30", {0,1,2,3,4,5,6}, "低", "予定・TODO・未読メールの配信"),
]


def get_token():
    now = int(time.time())
    assertion = pyjwt.encode(
        {"iss": CLIENT_ID, "sub": SERVICE_ACCOUNT, "iat": now, "exp": now + 3600},
        PRIVATE_KEY, algorithm="RS256")
    r = requests.post("https://auth.worksmobile.com/oauth2/v2.0/token", data={
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": assertion, "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET, "scope": "bot"})
    token = r.json().get("access_token")
    if not token:
        raise SystemExit(f"LW認証失敗: {r.text}")
    return token


def send(message):
    token = get_token()
    for i in range(0, len(message), 1800):
        r = requests.post(
            f"https://www.worksapis.com/v1.0/bots/{BOT_ID}/users/{USER_ID}/messages",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"content": {"type": "text", "text": message[i:i+1800]}})
        print("送信:", r.status_code, r.text[:200])


def build():
    now = datetime.datetime.now(JST)
    wd = now.weekday()
    today = [t for t in TASKS if wd in t[2]]
    today.sort(key=lambda t: int(t[1].split(":")[0]) * 60 + int(t[1].split(":")[1]))
    wd_ja = "月火水木金土日"[wd]

    if MODE == "evening":
        lines = ["【手動実行リマインド】まもなくアポツールの時間です",
                 "",
                 f"{now.month}月{now.day}日（{wd_ja}）19:49 アポツール（キャンセルリスク患者の処理）",
                 "",
                 "GitHubの無料枠が復活する8月1日までは自動で動きません。",
                 "実行したいときは Claude Code に「アポツール動かして」と伝えてください。",
                 "今日はやらなくてよい、という判断でも問題ありません（翌日以降にまとめて拾えます）。"]
        return "\n".join(lines)

    lines = [f"【手動実行リスト】{now.month}月{now.day}日（{wd_ja}）",
             "",
             "GitHubの自動実行が止まっているため、今日は下記が動きません。",
             "実行したいものを Claude Code に伝えてください（例:「アポツール動かして」）。",
             ""]
    for name, hhmm, _days, pri, note in today:
        lines.append(f"◆ {hhmm}　{name}")
        lines.append(f"　 重要度: {pri}／{note}")
    lines += ["",
              "※8月1日に無料枠がリセットされ、すべて自動で元どおりになります。",
              "※この間、失敗メールが毎日届きますが中身は全部「課金でブロック」なので無視してかまいません。"]
    return "\n".join(lines)


if __name__ == "__main__":
    msg = build()
    print(msg)
    send(msg)
