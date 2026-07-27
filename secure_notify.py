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


def send(message):
    token = get_token()
    for i in range(0, len(message), 1800):
        r = requests.post(
            f"https://www.worksapis.com/v1.0/bots/{BOT_ID}/users/{USER_ID}/messages",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"content": {"type": "text", "text": message[i:i+1800]}})
        # 本文はログに出さない（患者名を含むため）
        print("送信:", r.status_code)
        if r.status_code >= 300:
            raise SystemExit(f"送信失敗: {r.status_code} {r.text[:200]}")


if __name__ == "__main__":
    payload = os.environ["ENCRYPTED_PAYLOAD"]
    message = Fernet(os.environ["NOTIFY_ENC_KEY"].encode()).decrypt(payload.encode()).decode("utf-8")
    send(message)
    print("完了")
