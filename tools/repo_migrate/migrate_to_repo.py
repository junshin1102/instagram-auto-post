#!/usr/bin/env python3
"""ローカルPCのプロジェクトフォルダを、安全にGitHubリポジトリ化するための移行ツール。

このPCの中にしか存在しないプロジェクト(ヤフオク自動出品・メルカリ運用・売上レポート・
ラベル発行・集荷自動化など)を、APIキーを漏らさずにGitHubへ退避させることが目的。

使い方は3段階。必ずこの順番で進める。

    1) 棚卸し   python migrate_to_repo.py scan  "C:\\Users\\xxx\\projects"
    2) 下準備   python migrate_to_repo.py prepare "C:\\Users\\xxx\\projects\\yahoo-auction"
    3) 退避     python migrate_to_repo.py prepare "...\\yahoo-auction" --push

prepare は、コミット対象になるファイルだけを対象に鍵をスキャンする。
鍵が1件でも見つかったら、その場で停止して push しない(--force-secrets で解除可能)。
作成されるリポジトリは常に private。
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path

# ---------------------------------------------------------------- 鍵の検出

# 値そのものの形が決まっているもの。これらは誤検出がほぼ無い。
SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("Anthropic APIキー", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("OpenAI APIキー", re.compile(r"sk-(?!ant-)(?:proj-)?[A-Za-z0-9_\-]{32,}")),
    ("Meta/Instagram トークン", re.compile(r"\b(?:IGA|EAA)[A-Za-z0-9]{30,}")),
    ("Google APIキー", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("Google OAuthトークン", re.compile(r"\bya29\.[0-9A-Za-z_\-]{20,}")),
    ("AWS アクセスキー", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub トークン", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}")),
    ("GitHub PAT", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}")),
    ("Slack トークン", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}")),
    ("Cloudinary URL", re.compile(r"cloudinary://[0-9]+:[A-Za-z0-9_\-]+@")),
    ("秘密鍵ファイルの中身", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("接続文字列のパスワード", re.compile(r"(?:mysql|postgres|postgresql|mongodb(?:\+srv)?)://[^\s:@/]+:[^\s:@/]+@")),
]

# 「名前 = 値」の形。こちらは誤検出があり得るので、プレースホルダを除外する。
ASSIGNMENT_PATTERN = re.compile(
    r"(?P<name>[A-Za-z0-9_]*(?:PASSWORD|PASSWD|SECRET|TOKEN|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY|CREDENTIAL)[A-Za-z0-9_]*)"
    r"\s*[=:]\s*"
    r"(?P<quote>['\"]?)(?P<value>[^\s'\"#,;)]{8,})(?P=quote)",
    re.IGNORECASE,
)

# 値がこれらを含む/これらで始まる場合は、実物ではなくサンプルとみなす。
PLACEHOLDER_MARKERS = (
    "your_", "your-", "yourkey", "xxx", "...", "<", "changeme", "change_me",
    "dummy", "sample", "example", "placeholder", "todo", "here", "abcdef",
    "password", "secret", "token", "apikey", "api_key", "none", "null", "true", "false",
    "os.environ", "process.env", "getenv", "${", "%(", "{{",
)

# 中身を読まないファイル(バイナリ・巨大ファイル)
SKIP_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".svg",
    ".mp4", ".mov", ".m4v", ".avi", ".mp3", ".wav", ".m4a",
    ".zip", ".gz", ".tar", ".7z", ".rar", ".pdf", ".xlsx", ".xls", ".docx",
    ".ttf", ".otf", ".woff", ".woff2", ".exe", ".dll", ".so", ".dylib", ".pyc",
}
MAX_SCAN_BYTES = 2 * 1024 * 1024        # これを超えるテキストは中身を見ない
LARGE_FILE_BYTES = 50 * 1024 * 1024     # GitHubが警告を出す目安

# ---------------------------------------------------------------- .gitignore

# instagram-auto-post で実際に使っている .gitignore をそのまま雛形にしている。
GITIGNORE_BLOCK = """\
# --- 移行ツールが追加(鍵の流出防止) ---
.env
.env.*
!.env.example
*.pem
*.key
*.p12
*.pfx
id_rsa
id_rsa.*
id_ed25519
id_ed25519.*
credentials
credentials.*
credentials.json
secrets
secrets.*
token.json
token.pickle
.netrc
.npmrc
.pypirc
.git-credentials
service-account*.json
serviceAccount*.json
client_secret*.json
.claude/settings.local.json
.claude/history/
.mcp.local.json
__pycache__/
*.pyc
node_modules/
.venv/
venv/
dist/
build/
.next/
.DS_Store
Thumbs.db
.idea/
.vscode/
"""
GITIGNORE_MARKER = "# --- 移行ツールが追加(鍵の流出防止) ---"


def width(text: str) -> int:
    """全角文字を2桁として数えた表示幅。表の桁を揃えるために使う。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def pad(text: str, size: int) -> str:
    return text + " " * max(0, size - width(text))


def run_git(folder: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=folder, check=check,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def is_placeholder(value: str) -> bool:
    lowered = value.lower()
    return any(marker in lowered for marker in PLACEHOLDER_MARKERS)


def scan_file(path: Path) -> list[tuple[int, str, str]]:
    """1ファイルを走査して (行番号, 種別, 抜粋) のリストを返す。"""
    if path.suffix.lower() in SKIP_SUFFIXES:
        return []
    try:
        if path.stat().st_size > MAX_SCAN_BYTES:
            return []
        raw = path.read_bytes()
    except OSError:
        return []
    if b"\x00" in raw[:8192]:      # バイナリ
        return []

    text = raw.decode("utf-8", errors="replace")
    findings: list[tuple[int, str, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if len(line) > 4000:
            continue
        for label, pattern in SECRET_PATTERNS:
            match = pattern.search(line)
            if match:
                findings.append((lineno, label, mask(match.group(0))))
        match = ASSIGNMENT_PATTERN.search(line)
        if match and not is_placeholder(match.group("value")):
            findings.append(
                (lineno, f"{match.group('name')} に実値らしき文字列", mask(match.group("value")))
            )
    return findings


def mask(value: str) -> str:
    """見つけた値をそのまま出力しない。先頭4文字だけ残す。"""
    if len(value) <= 8:
        return value[:2] + "*" * (len(value) - 2)
    return value[:4] + "*" * 8 + f"(全{len(value)}文字)"


# ---------------------------------------------------------------- scan

def cmd_scan(root: Path) -> int:
    if not root.is_dir():
        print(f"フォルダが見つかりません: {root}", file=sys.stderr)
        return 1

    children = sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))
    if not children:
        print(f"{root} の直下にフォルダがありません。")
        return 1

    print(f"棚卸し: {root}")
    print(f"{pad('フォルダ', 34)} {pad('git', 6)} {pad('リモート', 10)} {pad('.env', 10)} 概算サイズ")
    print("-" * 78)

    未退避: list[str] = []
    for child in children:
        has_git = (child / ".git").is_dir()
        remote = ""
        if has_git:
            result = run_git(child, "remote", "get-url", "origin", check=False)
            remote = "あり" if result.returncode == 0 and result.stdout.strip() else "なし"
        env_files = [p.name for p in child.glob(".env*") if p.name != ".env.example"]
        size = sum(f.stat().st_size for f in child.rglob("*") if f.is_file()) if child.is_dir() else 0

        print(
            f"{pad(child.name, 34)} {pad('あり' if has_git else 'なし', 6)} "
            f"{pad(remote or '-', 10)} {pad(','.join(env_files) or '-', 10)} "
            f"{size / 1024 / 1024:.1f} MB"
        )
        if not has_git or remote == "なし":
            未退避.append(child.name)

    print()
    if 未退避:
        print(f"GitHubに退避されていないフォルダ: {len(未退避)}件")
        for name in 未退避:
            print(f"  - {name}")
        print()
        print("次は1つずつ下準備します(まだpushしません):")
        print(f'  python migrate_to_repo.py prepare "{root / 未退避[0]}"')
    else:
        print("すべてGitHubに退避済みです。")
    return 0


# ---------------------------------------------------------------- prepare

def ensure_gitignore(folder: Path) -> None:
    """既存の .gitignore は消さず、足りない行だけ末尾に足す。"""
    path = folder / ".gitignore"
    existing = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    if GITIGNORE_MARKER in existing:
        print("  .gitignore: 追記済み(変更なし)")
        return
    separator = "" if not existing or existing.endswith("\n") else "\n"
    path.write_text(existing + separator + "\n" + GITIGNORE_BLOCK, encoding="utf-8")
    print("  .gitignore: 鍵の除外設定を追記しました" if existing else "  .gitignore: 新規作成しました")


def tracked_candidates(folder: Path) -> list[Path]:
    """.gitignore を適用した上で、実際にコミット対象になるファイル一覧。"""
    result = run_git(folder, "ls-files", "--cached", "--others", "--exclude-standard")
    return [folder / line for line in result.stdout.splitlines() if line.strip()]


def cmd_prepare(folder: Path, push: bool, repo_name: str | None, force_secrets: bool) -> int:
    if not folder.is_dir():
        print(f"フォルダが見つかりません: {folder}", file=sys.stderr)
        return 1
    if shutil.which("git") is None:
        print("git が見つかりません。https://git-scm.com/ からインストールしてください。", file=sys.stderr)
        return 1

    print(f"対象: {folder}")

    if not (folder / ".git").is_dir():
        run_git(folder, "init", "-b", "main")
        print("  git: 新しくリポジトリにしました")
    else:
        print("  git: すでにリポジトリです")

    ensure_gitignore(folder)

    files = tracked_candidates(folder)
    if not files:
        print("  コミット対象のファイルがありません。中身を確認してください。")
        return 1
    print(f"  コミット対象: {len(files)}件")

    tracked = {p.resolve() for p in files}
    excluded = [
        p.relative_to(folder)
        for pattern in (".env", ".env.*", "*.pem", "*.key", "credentials*", "token.json", "*.p12")
        for p in folder.rglob(pattern)
        if p.is_file() and p.resolve() not in tracked
    ]
    for name in sorted({str(e) for e in excluded}):
        print(f"  除外(コミットされません): {name}")

    # --- 鍵のスキャン ---
    print("\n鍵をスキャンしています...")
    findings: list[tuple[Path, int, str, str]] = []
    for path in files:
        for lineno, label, excerpt in scan_file(path):
            findings.append((path, lineno, label, excerpt))

    large = [p for p in files if p.is_file() and p.stat().st_size > LARGE_FILE_BYTES]
    if large:
        print(f"\n[注意] 50MBを超えるファイルが {len(large)} 件あります。")
        for path in large[:10]:
            print(f"  {path.relative_to(folder)}  {path.stat().st_size / 1024 / 1024:.0f} MB")
        print("  動画や画像なら .gitignore に足して除外することを勧めます。")

    if findings:
        print(f"\n[停止] 鍵らしき文字列が {len(findings)} 件見つかりました。")
        print("このまま push すると、その鍵は公開されたものとして扱う必要があります。\n")
        for path, lineno, label, excerpt in findings[:40]:
            print(f"  {path.relative_to(folder)}:{lineno}  {label}  {excerpt}")
        if len(findings) > 40:
            print(f"  ... 他 {len(findings) - 40} 件")
        print()
        # すでにgitが追跡しているファイルは .gitignore では外れないため、案内を変える。
        tracked_names = set(run_git(folder, "ls-files", "--cached").stdout.splitlines())
        offenders = {str(path.relative_to(folder)).replace("\\", "/") for path, *_ in findings}
        already_tracked = sorted(offenders & tracked_names)
        untracked = sorted(offenders - tracked_names)

        print("対処:")
        if untracked:
            print("  [まだコミットされていないファイル]")
            for name in untracked:
                print(f"    {name}")
            print("    鍵を .env に移し、コード側は os.environ.get(\"名前\") で読むようにする")
            print("    (.env は .gitignore で除外済みなのでコミットされません)")
        if already_tracked:
            print("  [すでにgitが追跡しているファイル] .gitignore では外れません")
            for name in already_tracked:
                print(f"    git rm --cached \"{name}\"")
            print("    上を実行してから、鍵を .env に移してください。")
            if run_git(folder, "log", "-1", "--oneline", check=False).stdout.strip():
                print()
                print("  [重要] 過去のコミットにも鍵が残っている可能性があります。")
                print("    その場合、履歴を消しても漏れた前提で扱うのが安全です。")
                print("    該当サービスの管理画面で鍵を再発行(ローテート)してください。")
        print("  最後にもう一度 prepare を実行してください。")
        print()
        print("サンプル値で誤検出している場合のみ --force-secrets を付けて続行できます。")
        if not force_secrets:
            return 2
        print("--force-secrets が指定されたので続行します。")
    else:
        print("  鍵らしき文字列は見つかりませんでした。")

    if not push:
        print("\nここまでが下準備です。まだ何もコミット・pushしていません。")
        print("内容に問題がなければ、同じコマンドに --push を付けて実行してください:")
        print(f'  python migrate_to_repo.py prepare "{folder}" --push')
        return 0

    # --- コミット ---
    run_git(folder, "add", "-A")
    staged = run_git(folder, "diff", "--cached", "--name-only")
    if staged.stdout.strip():
        run_git(folder, "-c", "user.name=junshin", "-c", "user.email=factory.kiyadesign@gmail.com",
                "commit", "-m", "chore: ローカルのみに存在していたプロジェクトをGitHubへ退避")
        print("\n  コミットしました。")
    else:
        print("\n  コミットする変更はありませんでした。")

    # --- push ---
    name = repo_name or folder.name
    remote = run_git(folder, "remote", "get-url", "origin", check=False)
    if remote.returncode == 0 and remote.stdout.strip():
        print(f"  リモート: {remote.stdout.strip()}")
        result = run_git(folder, "push", "-u", "origin", "HEAD", check=False)
        print(result.stdout or result.stderr)
        return 0 if result.returncode == 0 else 1

    if shutil.which("gh"):
        print(f"  private リポジトリ {name} を作成して push します...")
        result = subprocess.run(
            ["gh", "repo", "create", name, "--private", "--source", ".", "--push"],
            cwd=folder, capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        print(result.stdout or result.stderr)
        return result.returncode

    print("\n  gh コマンドが無いため、最後の2手だけ手動でお願いします。")
    print(f"  1. https://github.com/new で private リポジトリ「{name}」を作る")
    print("  2. このフォルダで次を実行:")
    print(f"     git remote add origin https://github.com/junshin1102/{name}.git")
    print("     git push -u origin main")
    return 0


def cmd_all(root: Path, push: bool) -> int:
    """親フォルダの下にあるプロジェクトを、まとめて退避させる。

    鍵が見つかったものは push せずに飛ばし、最後にまとめて報告する。
    1件の失敗で全体を止めないのが狙い。"""
    if not root.is_dir():
        print(f"フォルダが見つかりません: {root}", file=sys.stderr)
        return 1

    children = sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))
    if not children:
        print(f"{root} の直下にフォルダがありません。")
        return 1

    print(f"対象 {len(children)} 件をまとめて処理します。")
    print("(鍵が見つかったものは push せず、最後に一覧で報告します)\n")

    done: list[str] = []
    blocked: list[str] = []
    failed: list[str] = []

    for index, child in enumerate(children, start=1):
        print("=" * 70)
        print(f"[{index}/{len(children)}] {child.name}")
        print("=" * 70)
        code = cmd_prepare(child, push, None, force_secrets=False)
        if code == 0:
            done.append(child.name)
        elif code == 2:
            blocked.append(child.name)
        else:
            failed.append(child.name)
        print()

    print("=" * 70)
    print("まとめ")
    print("=" * 70)
    verb = "退避しました" if push else "下準備できました"
    print(f"  ✅ {verb}: {len(done)}件")
    for name in done:
        print(f"      {name}")
    if blocked:
        print(f"  ⛔ 鍵が見つかったため保留: {len(blocked)}件")
        for name in blocked:
            print(f"      {name}")
        print("      上のログに対処法が出ています。直してから個別に実行してください:")
        print(f'        python migrate_to_repo.py prepare "{root / blocked[0]}" --push')
    if failed:
        print(f"  ⚠️ 処理できませんでした: {len(failed)}件")
        for name in failed:
            print(f"      {name}")
    if not push and done:
        print()
        print("  内容に問題がなければ、--push を付けて本番実行してください:")
        print(f'    python migrate_to_repo.py all "{root}" --push')
    return 0 if not failed else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="ローカルにしか無いプロジェクトを、鍵を漏らさずGitHubへ退避させる",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="フォルダを棚卸しして、未退避のものを一覧表示する")
    scan.add_argument("folder", help="プロジェクトが並んでいる親フォルダ")

    prepare = sub.add_parser("prepare", help="1つのプロジェクトを退避できる状態にする")
    prepare.add_argument("folder", help="退避したいプロジェクトのフォルダ")
    prepare.add_argument("--push", action="store_true", help="実際にコミットしてGitHubへpushする")
    prepare.add_argument("--name", dest="repo_name", help="リポジトリ名(既定はフォルダ名)")
    prepare.add_argument("--force-secrets", action="store_true",
                         help="鍵の検出が誤検出だと確認できた場合のみ使う")

    every = sub.add_parser("all", help="親フォルダ配下を、まとめて退避させる")
    every.add_argument("folder", help="プロジェクトが並んでいる親フォルダ")
    every.add_argument("--push", action="store_true", help="実際にコミットしてGitHubへpushする")

    args = parser.parse_args()
    if args.command == "all":
        return cmd_all(Path(args.folder).expanduser().resolve(), args.push)
    if args.command == "scan":
        return cmd_scan(Path(args.folder).expanduser().resolve())
    return cmd_prepare(
        Path(args.folder).expanduser().resolve(), args.push, args.repo_name, args.force_secrets,
    )


if __name__ == "__main__":
    sys.exit(main())
