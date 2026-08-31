# ==========================================================
# ※これは公開リポジトリ側の複製です（2026-08-31）。本体は apotool/lw_notify.py。
#   このリポジトリでの使い道は2つだけ:
#     ① 期限アラート（main.py が直接この中の送信処理を使うのではなく、独立して送る）
#     ② GitHub Actions 無料枠アラート（quota_alert.yml）
#   ★ここに「好きな文面を送る」汎用の入口（workflow_dispatch の message 入力）を作らないこと。
#     ワークフローの入力は実行ログの env 一覧にそのまま印字され、公開リポジトリでは誰でも読める。
#     患者氏名・金額・認証コードを含む通知は、必ず非公開の apotool 側から送る
#     （2026-08-10に院長判断で却下・2026-08-31に実測で再確認）。
#   本体を直すときは apotool/lw_notify.py と両方を直すこと。
# ==========================================================
# ==========================================================
# 【何をする】LINEワークスへの通知部品（全ワークフローの完了・失敗通知から使われる）
# 【失敗通知の自動整形】NOTIFY_MESSAGE が「❌ <タスク名> 失敗 …」の形なら、
#   院長向けのわかりやすい定型文（何が起きた・どうすればいい・ログ直リンク）に組み替える。
#   ✅完了通知やその他のメッセージはそのまま送る。
# 【宛先】NOTIFY_USER_ID で送信先を切り替えられる（省略時は院長）。
#   スタッフ個人DMに使う場合はBotを 12266491（既存Bot）にすること。
#   新Bot4つ（12786821/12786828/12786833/12789558）は院長のみ使用権限あり。
#   スタッフのアカウントID一覧はメモリ reference_lw_staff_ids.md が正本。
# 【分割】1通1800文字を超えると LINEワークスが 400 を返すため、行単位で自動分割する。
# ==========================================================
import os, re, time, sys, requests
import jwt as pyjwt

CLIENT_ID       = "0cAEPO2Yzau80tSsEhxV"
CLIENT_SECRET = os.environ["LW_CLIENT_SECRET"]
SERVICE_ACCOUNT = "3w266.serviceaccount@ovalcourtdental"
BOT_ID          = os.environ.get("NOTIFY_BOT_ID", "12266491")  # 省略時は既存Bot（要対応・チャンネル用）。失敗通知は12789558
USER_ID         = os.environ.get("NOTIFY_USER_ID", "shin@ovalcourtdental")
# NOTIFY_CHANNEL_ID を渡すとチャンネルへ投稿する（省略時は個人DM）。
# チャンネル送信は既存Bot 12266491 のみ可（新Bot4つはトークルームに参加できない）。
CHANNEL_ID      = os.environ.get("NOTIFY_CHANNEL_ID", "").strip()
PRIVATE_KEY     = os.environ["LW_PRIVATE_KEY"]
MESSAGE         = os.environ.get("NOTIFY_MESSAGE", "📷 ワイズデントから写真が届きました")
SPLIT_LIMIT     = 1800  # LINEワークス1通の上限（超えると400）


def build_run_url():
    """失敗したrunのログURLを返す。

    別リポジトリ（oval-seo-loop等）から lw_notify.yml を dispatch して通知する場合、
    GITHUB_RUN_ID は「通知ワークフロー自身」のIDになり、失敗したrunに飛べない。
    そのため呼び出し側が NOTIFY_RUN_URL を渡してきたらそちらを優先する。
    （2026-08-05: SEOループの失敗通知が lw_notify.yml のrunを指していたため修正）
    """
    given = os.environ.get("NOTIFY_RUN_URL", "").strip()
    if given:
        return given
    server = os.environ.get("GITHUB_SERVER_URL", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    if server and repo and run_id:
        return f"{server}/{repo}/actions/runs/{run_id}"
    return ""


def format_failure(message):
    """「❌ <タスク名> 失敗 …」を院長向けの定型文に組み替える。該当しなければそのまま返す"""
    m = re.match(r"^❌\s*(.+?)\s*失敗\s*(.*)$", message, re.S)
    if not m:
        return message
    task = m.group(1).strip()
    rest = m.group(2).strip()
    # 「— GitHub(Actions)(で)ログを確認してください」の定型尻尾は捨てる。それ以外の補足は残す
    rest = re.sub(r"^[—\-ー\s]*GitHub[^\n]*ログ[^\n]*確認[^\n]*$", "", rest).strip()
    lines = [f"【{task}】❌ 自動実行が失敗しました",
             "今回の処理は途中で止まっています。"]
    if rest:
        lines.append(rest)
    lines.append(f"▶ 対処: AIに「{task}のログ見て」と伝えてください（原因調査から修正まで対応します）")
    url = build_run_url()
    if url:
        lines.append(f"詳細ログ: {url}")
    return "\n".join(lines)


MESSAGE = format_failure(MESSAGE)


def split_message(text, limit=SPLIT_LIMIT):
    """1通が limit 文字を超えないよう行単位で分割する。2通目以降は「（続き）」を付ける。
    1行だけで limit を超える場合はその行をそのまま送る（欠落させない）。"""
    if len(text) <= limit:
        return [text]
    chunks, cur = [], []
    for ln in text.split("\n"):
        head = "（続き）\n" if chunks else ""
        candidate = head + "\n".join(cur + [ln])
        if len(candidate) > limit and cur:
            chunks.append(("（続き）\n" if chunks else "") + "\n".join(cur))
            cur = [ln]
        else:
            cur.append(ln)
    if cur:
        chunks.append(("（続き）\n" if chunks else "") + "\n".join(cur))
    return chunks


now = int(time.time())
token = pyjwt.encode(
    {"iss": CLIENT_ID, "sub": SERVICE_ACCOUNT, "iat": now, "exp": now + 3600},
    PRIVATE_KEY, algorithm="RS256"
)
r = requests.post(
    "https://auth.worksmobile.com/oauth2/v2.0/token",
    data={
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": token,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "scope": "bot"
    }
)
access_token = r.json().get("access_token")
if not access_token:
    print("Token error:", r.text)
    sys.exit(1)

if CHANNEL_ID:
    endpoint = f"https://www.worksapis.com/v1.0/bots/{BOT_ID}/channels/{CHANNEL_ID}/messages"
    label = f"channel:{CHANNEL_ID}"
else:
    endpoint = f"https://www.worksapis.com/v1.0/bots/{BOT_ID}/users/{USER_ID}/messages"
    label = USER_ID

chunks = split_message(MESSAGE)
failed = False
for i, chunk in enumerate(chunks, 1):
    r2 = requests.post(
        endpoint,
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        json={"content": {"type": "text", "text": chunk}}
    )
    print(f"Status[{i}/{len(chunks)}] -> {label}:", r2.status_code, r2.text)
    if r2.status_code >= 300:
        failed = True
# 送れていないのに成功扱いにしない（サイレント失敗の防止）
if failed:
    sys.exit(1)
