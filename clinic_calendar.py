#!/usr/bin/env python3
"""clinic_calendar.py — 医院の休診日判定（共通部品）

大崎オーバルコート歯科・矯正歯科室は「月〜土 診療・日曜＋祝日 休診」。
朝の通知系（ささっとペイ未登録チェック・インビザライン確認待ち・期限アラート）が
休診日に配信されないよう、各スクリプトの冒頭で休診日なら送信せずに終了する。

判定は2段構え:
  ① clinic_closed_reason(d)      … 日曜＋日本の祝日（jpholiday／振替休日含む）。
                                    ログイン不要で即判定。祝日はこれで確実にスキップ。
  ② apotool_closed_reason(page)  … アポツール /user/shifts/view の rest-all を読む。
     apotool_closed_reason_standalone()  お盆・年末年始・臨時休診など「祝日でない
                                    休診日」もこれで拾える（休診日の正本）。

①だけでも今日のような祝日はスキップできる。②を足すと祝日以外の休診も拾える。
②はアポツールにログインできるスクリプト（ささっとペイ・インビザライン）でのみ使う。
"""
import asyncio
import os
import re
from datetime import datetime, timezone, timedelta

import jpholiday

JST = timezone(timedelta(hours=9))

APOTOOL_LOGIN_URL = "https://user.stransa.co.jp/login"
APOTOOL_BASE      = "https://apo-toolboxes.stransa.co.jp"


# ── ① 日曜＋祝日（ログイン不要）─────────────────────────────────────────────

def clinic_closed_reason(d=None):
    """休診日（日曜・祝日）なら理由の文字列を、診療日なら None を返す。

    d 未指定なら JST の当日で判定する。
    """
    if d is None:
        d = datetime.now(JST).date()
    if d.weekday() == 6:  # 6=日曜
        return "日曜（定休）"
    name = jpholiday.is_holiday_name(d)
    if name:
        return f"祝日（{name}）"
    return None


def is_clinic_closed(d=None) -> bool:
    return clinic_closed_reason(d) is not None


# ── ② アポツール シフトページの rest-all（お盆・年末年始・臨時休診も拾う）──────

async def _apotool_login(page):
    """アポツールにログインする（ささっとペイ等と同じ手順）。"""
    email = os.environ["APOTOOL_EMAIL"]
    password = os.environ["APOTOOL_PASSWORD"]
    await page.goto(APOTOOL_LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
    await asyncio.sleep(2)
    email_sel = page.locator('input[type="email"]')
    if await email_sel.count() > 0:
        await email_sel.fill(email)
    else:
        await page.locator('input[type="text"], input:not([type])').first.fill(email)
    await page.fill('input[type="password"]', password)
    await page.locator('button[type="submit"], button:has-text("ログイン")').first.click()
    try:
        await page.wait_for_url(re.compile(r"stransa\.co\.jp/(?!login)"), timeout=20_000)
    except Exception:
        pass
    if "login" in page.url:
        raise RuntimeError("アポツールログイン失敗")
    # オフィス選択でセッションを apo-toolboxes 側に確立する（これが無いとシフト等が空になる）
    await page.goto("https://user.stransa.co.jp/offices",
                    wait_until="domcontentloaded", timeout=60_000)
    try:
        await page.wait_for_url(re.compile(r"apo-toolboxes\.stransa\.co\.jp"), timeout=20_000)
    except Exception:
        pass
    if "apo-toolboxes" not in page.url:
        await page.goto(f"{APOTOOL_BASE}/calendar/",
                        wait_until="domcontentloaded", timeout=60_000)
    await asyncio.sleep(2)


async def fetch_apotool_closed_days(page, year: int, month: int) -> set:
    """アポツール /user/shifts/view/YYYY/MM の rest-all が付いた日（＝休診日）の集合を返す。

    GBP診療カレンダー(gbp_calendar_post.fetch_schedule)と同じ読み方。日曜・祝日・お盆・
    年末年始・臨時休診がすべて rest-all になる（休診日の正本）。
    """
    url = f"{APOTOOL_BASE}/user/shifts/view/{year}/{month:02d}"
    await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    await asyncio.sleep(2)
    data = await page.evaluate(r"""() => {
        const heads = [...document.querySelectorAll('.shift-day-head')];
        const rest = [];
        heads.forEach(c => {
            const m = (c.className || '').match(/day-col-(\d+)/);
            if (!m) return;
            const d = parseInt(m[1]);
            if (/rest-all/.test(c.className)) rest.push(d);
        });
        return { closed: [...new Set(rest)] };
    }""")
    return set(data.get("closed", []))


async def apotool_closed_reason(page, d=None):
    """アポツールにログイン済みの page を使い、d が休診日(rest-all)なら理由を返す。

    取得失敗の誤スキップを避けるため、月の休診日が3日未満なら「取得失敗の可能性」と
    みなして None（＝診療日扱い）を返す（安全側＝誤って配信を止めない）。
    """
    if d is None:
        d = datetime.now(JST).date()
    closed = await fetch_apotool_closed_days(page, d.year, d.month)
    if len(closed) < 3:
        print(f"[休診チェック] {d.year}/{d.month} の休診日取得が{len(closed)}日のみ "
              f"→ 取得失敗の可能性。休診スキップは行いません")
        return None
    if d.day in closed:
        return "アポツール休診日(rest-all)"
    return None


async def apotool_closed_reason_standalone(d=None):
    """自前でブラウザを起動→アポツールにログイン→d の休診判定まで行う。

    アポツールに常時ログインしていないスクリプト（インビザライン確認待ち等）用。
    休診なら理由文字列、診療日なら None。失敗は呼び出し側で握りつぶす前提で例外を投げる。
    """
    from playwright.async_api import async_playwright
    if d is None:
        d = datetime.now(JST).date()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await (await browser.new_context()).new_page()
        try:
            await _apotool_login(page)
            return await apotool_closed_reason(page, d)
        finally:
            await browser.close()
