# ローカルプロジェクト退避ツール

このPCの中にしか存在しないプロジェクトを、**APIキーを漏らさずに**GitHubへ退避させるためのツールです。

## なぜ必要か

2026年9月時点で、GitHubにあるリポジトリは3つだけです。

```
junshin1102/instagram-auto-post   (public)
junshin1102/twitter-auto-post     (private)
junshin1102/junshin-size-search   (public)
```

一方で、作業してきたプロジェクトはこれだけあります。

> ヤフオク自動出品 / 各EC自動新規出品・内容修正 / 各EC自動取込出品取下 / ヤフオク運用 /
> メルカリ運用 / BASE運用 / 各EC監視システム / 全EC日次・月次売上レポート /
> ヤマト集荷自動化 / ヤマト・佐川ラベル発行 / SNS動画制作 / Googleドライブ整理 /
> チャットワーク制限回避 / 薪ストーブLP

**これらはどのリポジトリにも入っていません。PCが壊れたら消えます。**

ただし、そのまま `git push` するのは危険です。各ECやヤマト・チャットワークのAPIキーが
コードに直接書かれていると、それごと公開されます。instagram-auto-post は public なので、
同じ手順を踏むと実際に起こり得ます。

このツールは、**鍵が含まれたままの push を機械的に止めます。**

## 使い方

Python 3 と git が入っていれば動きます。`migrate_to_repo.py` をPCの適当な場所に置いてください。

### 1. 棚卸し — どれが未退避か一覧にする

```
python migrate_to_repo.py scan "C:\Users\あなた\projects"
```

```
フォルダ                           git    リモート   .env       概算サイズ
------------------------------------------------------------------------------
yahoo-auction                      なし   -          .env       12.4 MB
mercari-ops                        あり   なし       .env       3.1 MB
label-print                        なし   -          -          0.8 MB

GitHubに退避されていないフォルダ: 3件
```

### 2. 下準備 — 1つずつ、まだ push しない

```
python migrate_to_repo.py prepare "C:\Users\あなた\projects\yahoo-auction"
```

この段階でやること:

- `git init`(まだリポジトリでなければ)
- `.gitignore` に鍵の除外設定を**追記**する(既存の行は消しません)
- **コミット対象になるファイルだけ**を鍵スキャンにかける
- 鍵が1件でも見つかったら**その場で停止**し、対処法を表示する

鍵が見つかった場合は、ファイルがgit追跡済みかどうかで案内が変わります。

- 未追跡 → 鍵を `.env` に移す
- 追跡済み → `git rm --cached` してから `.env` に移す（この場合、過去のコミットにも
  残っている可能性があるため、**鍵の再発行を勧めます**）

### 3. 退避 — 問題がなければ push

```
python migrate_to_repo.py prepare "C:\Users\あなた\projects\yahoo-auction" --push
```

- リポジトリは常に **private** で作成されます
- `gh` コマンドがあれば作成から push まで自動
- 無ければ、最後の2手だけコマンドを表示するので手で実行してください

## 検出できる鍵

Anthropic / OpenAI / Meta・Instagram / Google / AWS / GitHub / Slack / Cloudinary の
各トークン、秘密鍵ファイルの中身、DB接続文字列のパスワード、および
`〜PASSWORD` `〜SECRET` `〜TOKEN` `〜API_KEY` などへの実値の代入。

`your_api_key_here` のようなサンプル値や `os.environ.get(...)` は誤検出しません。

検出は万能ではありません。**止まらなかったからといって安全とは限らない**ので、
push 前に表示されるファイル一覧には目を通してください。

## 優先順位

全部を一度にやる必要はありません。事業の本線から先に退避させてください。

1. ヤフオク自動出品・各EC出品系（売上に直結）
2. ヤマト・佐川ラベル発行、集荷自動化（止まると出荷が止まる）
3. 全EC売上レポート
4. その他
