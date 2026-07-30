# ==========================================================
# 【何をする】暗号化されたメッセージを復号して院長DMへ送る（LINEワークス）
# 【いつ動く】GitHub Actions課金ブロック中（2026-07-27〜31）、ローカル手動実行した
#             タスクの結果通知を Claude Code が workflow_dispatch で送るときだけ
# 【なぜ暗号化】このリポジトリは公開のため、患者名を含む通知本文を
#             dispatch入力に平文で載せない。Fernet鍵は Secrets の NOTIFY_ENC_KEY
# 【8月1日以降】無料枠リセットで不要になるので、このファイルとワークフローは削除する
# ==========================================================
import os, time, requests
import jwt as pyjwt
from cryptography.fernet import Fernet

CLIENT_ID = "0cAEPO2Yzau80tSsEhxV"
CLIENT_SECRET = os.environ["LW_CLIENT_SECRET"]
SERVICE_ACCOUNT = "3w266.serviceaccount@ovalcourtdental"
PRIVATE_KEY = os.environ["LW_PRIVATE_KEY"]
USER_ID = "shin@ovalcourtdental"
BOT_ID = os.environ.get("NOTIFY_BOT_ID", "12786833")  # 既定=完了通知Bot


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


def split_lines(message, limit=1800):
    """行の途中で切れないように分割する（1800字を超えるとLINEワークスが400を返す）"""
    chunks, cur, n = [], [], 0
    for line in message.split("\n"):
        if n + len(line) + 1 > limit and cur:
            chunks.append("\n".join(cur))
            cur, n = [], 0
        cur.append(line)
        n += len(line) + 1
    if cur:
        chunks.append("\n".join(cur))
    return chunks


def _post(url, token, text):
    r = requests.post(url,
                      headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                      json={"content": {"type": "text", "text": text}})
    # 本文・宛先はログに出さない（患者名等を含むため）
    print("送信:", r.status_code)
    if r.status_code >= 300:
        raise SystemExit(f"送信失敗: {r.status_code} {r.text[:200]}")


def send(message, users, channels=None):
    """users=個人DM／channels=トークルーム。channelsを使うときBotは12266491のみ可"""
    token = get_token()
    parts = split_lines(message)
    for uid in users or []:
        for p in parts:
            _post(f"https://www.worksapis.com/v1.0/bots/{BOT_ID}/users/{uid}/messages", token, p)
    for ch in channels or []:
        for p in parts:
            _post(f"https://www.worksapis.com/v1.0/bots/{BOT_ID}/channels/{ch}/messages", token, p)


if __name__ == "__main__":
    payload = os.environ["ENCRYPTED_PAYLOAD"]
    plain = Fernet(os.environ["NOTIFY_ENC_KEY"].encode()).decrypt(payload.encode()).decode("utf-8")
    # JSON形式 {"users": [...], "channels": [...], "text": "..."} なら宛先込み。
    # 素のテキストなら院長DMのみ。channels を明示したときは users を既定で足さない
    users, channels, message = [USER_ID], [], plain
    try:
        import json
        obj = json.loads(plain)
        if isinstance(obj, dict) and "text" in obj:
            message = obj["text"]
            channels = obj.get("channels") or []
            users = obj.get("users") or ([] if channels else [USER_ID])
    except ValueError:
        pass
    send(message, users, channels)
    print("完了")
