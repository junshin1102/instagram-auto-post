#!/usr/bin/env python3
"""各自動化システムが「昨日ちゃんと動いたか」を毎朝1回だけ確認し、LINEに1通で報告する。

作った仕組みが静かに止まっていても気づけない、という問題を潰すためのもの。
投稿や出品そのものはしない。見るだけ。

    python health_check.py                 # 確認してLINEに送る
    python health_check.py --dry-run       # 送らずに画面に出すだけ
    python health_check.py --init          # checks.json の雛形を作る

異常が1件でもあれば終了コード1を返すので、Codexなどに後続処理をさせられる。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

try:
    import requests
except ImportError:
    requests = None

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "checks.json"
RESULT_PATH = HERE / "last_result.json"

TEMPLATE = {
    "line_channel_access_token_env": "LINE_CHANNEL_ACCESS_TOKEN",
    "checks": [
        {
            "name": "Instagram自動投稿",
            "type": "heartbeat",
            "path": "C:\\path\\to\\instagram-auto-post\\state\\heartbeat.json",
            "max_age_hours": 30,
        },
        {
            "name": "ヤフオク自動出品",
            "type": "file_mtime",
            "path": "C:\\path\\to\\yahoo-auction\\log.txt",
            "max_age_hours": 30,
        },
        {
            "name": "全EC売上レポート",
            "type": "git_commit",
            "path": "C:\\path\\to\\sales-report",
            "max_age_hours": 30,
        },
    ],
}


def now_jst() -> datetime:
    return datetime.utcnow() + timedelta(hours=9)


def age_text(delta: timedelta) -> str:
    hours = delta.total_seconds() / 3600
    if hours < 1:
        return f"{int(delta.total_seconds() / 60)}分前"
    if hours < 48:
        return f"{hours:.0f}時間前"
    return f"{hours / 24:.0f}日前"


# ------------------------------------------------------------ 個別チェック

def check_heartbeat(path: Path, max_age: timedelta) -> tuple[bool, str]:
    """post_to_instagram.py が書く state/heartbeat.json を読む。"""
    if not path.exists():
        return False, "実行記録が見つかりません(一度も動いていない可能性)"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        last = datetime.fromisoformat(record["at"])
    except (ValueError, KeyError, OSError) as exc:
        return False, f"実行記録を読めません: {exc}"

    delta = now_jst() - last
    status = record.get("status", "unknown")
    detail = record.get("detail", "")

    if delta > max_age:
        return False, f"{age_text(delta)}から動いていません(最後: {status})"
    if status == "error":
        return False, f"{age_text(delta)}にエラー: {detail}"
    if status == "skipped":
        return True, f"{age_text(delta)}に実行(投稿対象なし)"
    return True, f"{age_text(delta)}に実行({detail})"


def check_file_mtime(path: Path, max_age: timedelta) -> tuple[bool, str]:
    """ログファイルの更新時刻で、動いたかどうかを判断する。"""
    if not path.exists():
        return False, "ログファイルが見つかりません"
    last = datetime.fromtimestamp(path.stat().st_mtime)
    delta = datetime.now() - last
    if delta > max_age:
        return False, f"ログが{age_text(delta)}から更新されていません"
    return True, f"{age_text(delta)}に更新"


def check_git_commit(path: Path, max_age: timedelta) -> tuple[bool, str]:
    """リポジトリの最終コミット時刻を見る。"""
    if not (path / ".git").is_dir():
        return False, "gitリポジトリではありません(GitHubへ未退避)"
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%cI"], cwd=path,
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"git実行に失敗: {exc}"
    if result.returncode != 0 or not result.stdout.strip():
        return False, "コミットがありません"

    last = datetime.fromisoformat(result.stdout.strip()).replace(tzinfo=None)
    delta = datetime.now() - last
    if delta > max_age:
        return False, f"最終コミットが{age_text(delta)}"
    return True, f"{age_text(delta)}にコミット"


CHECKERS = {
    "heartbeat": check_heartbeat,
    "file_mtime": check_file_mtime,
    "git_commit": check_git_commit,
}


# ------------------------------------------------------------ 本体

def run_checks(config: dict) -> list[dict]:
    results = []
    for check in config.get("checks", []):
        name = check.get("name", "(名前なし)")
        kind = check.get("type", "")
        checker = CHECKERS.get(kind)
        if checker is None:
            results.append({"name": name, "ok": False, "message": f"不明な種別: {kind}"})
            continue
        max_age = timedelta(hours=float(check.get("max_age_hours", 30)))
        try:
            ok, message = checker(Path(check.get("path", "")), max_age)
        except Exception as exc:                       # 1件の失敗で全体を止めない
            ok, message = False, f"確認中にエラー: {type(exc).__name__}: {exc}"
        results.append({"name": name, "ok": ok, "message": message})
    return results


def build_report(results: list[dict]) -> str:
    ng = [r for r in results if not r["ok"]]
    header = (
        f"🟢 自動化システム 全{len(results)}件 正常"
        if not ng else
        f"🔴 自動化システム {len(ng)}/{len(results)}件 に問題があります"
    )
    lines = [header, f"({now_jst():%m/%d %H:%M} 時点)", ""]
    for r in results:
        lines.append(f"{'✅' if r['ok'] else '❌'} {r['name']}")
        lines.append(f"　 {r['message']}")
    return "\n".join(lines)


def send_line(message: str, token: str | None) -> bool:
    if not token:
        print("LINEトークンが設定されていないため送信しません。", file=sys.stderr)
        return False
    if requests is None:
        print("requests が入っていないため送信できません(pip install requests)", file=sys.stderr)
        return False
    try:
        response = requests.post(
            "https://api.line.me/v2/bot/message/broadcast",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            json={"messages": [{"type": "text", "text": message}]},
            timeout=15,
        )
        if response.status_code >= 300:
            print(f"LINE送信に失敗: {response.status_code} {response.text[:200]}", file=sys.stderr)
            return False
        return True
    except Exception as exc:
        print(f"LINE送信に失敗: {exc}", file=sys.stderr)
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="自動化システムの生存確認")
    parser.add_argument("--dry-run", action="store_true", help="LINEに送らず画面に出すだけ")
    parser.add_argument("--init", action="store_true", help="checks.json の雛形を作る")
    parser.add_argument("--config", default=str(CONFIG_PATH), help="設定ファイルの場所")
    args = parser.parse_args()

    config_path = Path(args.config)

    if args.init:
        if config_path.exists():
            print(f"すでに存在します: {config_path}")
            return 1
        config_path.write_text(
            json.dumps(TEMPLATE, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"雛形を作りました: {config_path}")
        print("path を実際のフォルダに書き換えてから、--dry-run で試してください。")
        return 0

    if not config_path.exists():
        print(f"設定ファイルがありません: {config_path}", file=sys.stderr)
        print("まず --init で雛形を作ってください。", file=sys.stderr)
        return 1

    config = json.loads(config_path.read_text(encoding="utf-8"))
    results = run_checks(config)
    report = build_report(results)
    print(report)

    RESULT_PATH.write_text(
        json.dumps(
            {"at": now_jst().isoformat(timespec="seconds"), "results": results},
            ensure_ascii=False, indent=2,
        ) + "\n",
        encoding="utf-8",
    )

    if not args.dry_run:
        token_name = config.get("line_channel_access_token_env", "LINE_CHANNEL_ACCESS_TOKEN")
        send_line(report, os.environ.get(token_name))

    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
