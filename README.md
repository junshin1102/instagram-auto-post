# Junshin Instagram 自動投稿

@junshin_industry 用の、毎日自動でInstagramに投稿する仕組みです。

## 仕組み

**基本的に何も手を動かさなくても、毎日自動で投稿されます。**

1. 毎日 **7:00 / 12:00 / 20:00 (JST)** の3回、GitHub Actions が起動する
2. `images/queue/` に手動で追加した画像があればそれを使う(通常は空でOK)
3. キューが空の場合、[ヤフオクの出品者ページ](https://auctions.yahoo.co.jp/seller/7F3TQFS83hRevxWX9wK4z2ZvPzj3t?user_type=c)(現在459件)から、\
   **まだ投稿していない商品をランダムに1つ選ぶ**
4. その商品の画像(最大10枚)と、タイトル・寸法・価格などの情報を出品ページから自動取得する
5. 取得した画像のうち **1〜9枚目の右下にロゴ([assets/logo_watermark.png](assets/logo_watermark.png))を合成する**(10枚目にはロゴを入れない)
6. 画像を Cloudinary にアップロードして公開URLを取得
7. Claude(画像認識)が、取得した正確な情報をもとにキャプション本文を自動生成
8. 本文の後に、固定のハッシュタグ(後述)を自動で付与する
9. Instagram Graph API で投稿(画像が2枚以上ある場合は複数枚投稿=カルーセル)
10. 投稿済みの商品IDは `images/posted/posted_auction_ids.txt` に記録され、次回以降は選ばれない
11. 手動追加した画像を使った場合は、`images/posted/` に移動し `posted_log.csv` に記録

出品中の商品をすべて投稿し終えると、その回は「投稿対象なし」としてスキップされます(在庫を増やせば、また自動的に選ばれるようになります)。1日3回投稿するため、在庫459件でも約153日分で一周する計算です。

### ハッシュタグ

すべての投稿で、以下のハッシュタグが本文の後に固定で付きます(AIによる生成ではなく、コード側で毎回同じ文言を付与しています)。変更したい場合は [post_to_instagram.py](post_to_instagram.py) 内の `FIXED_HASHTAGS` を編集してください。

```
#新潟で木の伐採　お見積り致します
#林業　放置された里山を再生
#材木屋　広葉樹木材を大量在庫・一枚から販売
#新潟のギャラリー　林業×材木屋×木工房
#木工　家具食器雑貨を作ってます
#木のある暮らし　日常に木製品を
#山暮らし　木に囲まれる生活
#木の生活道具　木を伐るところから製作
#木製カトラリー　木のあたたかみを感じてほしい
#ギャラリー　新潟では少ない木工家のお店
#木が好きな人と繋がりたい
#木工が好きな人と繋がりたい
#暮らしの道具
#木の雑貨
#DIY
#woodworking
#woodcrafts
#handmade
#handcrafted
#gallery
```

### 投稿時間について

7:00(通勤・通学前)・12:00(お昼休み)・20:00(夕食後〜就寝前)は、いずれもInstagramの利用が伸びやすいとされる一般的な時間帯です。ただし、これはあくまで一般論の目安です。

**本当に「一番アクセス数の多い時間帯」を知るには、@junshin_industryの実際のインサイトデータを見るのが一番確実です。**

1. Instagramアプリで自分のプロフィールを開く
2. 右上のメニュー→「インサイト」→「オーディエンス」を開く
3. 「最もアクティブな時間帯」を確認する(フォロワーが一定数ついてから、1〜2週間分のデータが溜まると精度が上がります)

そこで判明した時間帯に合わせて、`.github/workflows/daily-post.yml` の `cron` の値を変更してください(UTC表記なので、JSTから9時間引いた値にする必要があります。例: JST 21:00 → UTC 12:00 → `cron: "0 12 * * *"`)。分かる範囲の時間帯を教えていただければ、cronの書き換えはこちらで対応します。

---

## セットアップ手順

### 1. このフォルダをGitHubリポジトリにする

```bash
git init
git add .
git commit -m "init"
git branch -M main
git remote add origin https://github.com/<あなたのユーザー名>/instagram-auto-post.git
git push -u origin main
```

Cloudinary経由で画像をアップロードするため、リポジトリは **Public/Privateどちらでも構いません**(Privateを推奨)。

### 2. Meta for Developers でアプリを作成し、アクセストークンを取得

1. https://developers.facebook.com/ でログイン→「アプリを作成」
2. ユースケースは **「Instagramでメッセージとコンテンツを管理」** を選択(投稿の公開に必要な権限が含まれています)
3. アプリ名を入力してアプリを作成
4. 左メニューの「アプリの役割」で自分が管理者になっていることを確認
5. https://business.facebook.com/settings/ で、Instagramアカウント(@junshin_industry)と連携するFacebookページの両方が登録されており、自分が「フルコントロール」を持っていることを確認
6. アプリのダッシュボードから、追加した「Instagram API」の設定画面を開き、**「アクセストークンを生成」**
   - 初回はInstagram側でこのアプリを「テスター」として承認する必要がある場合があります(Instagramアプリ→設定→アプリとウェブサイト、で招待を承認)
7. 生成された **アクセストークン** と **Instagramユーザー ID** をそれぞれ控える(`IG_ACCESS_TOKEN` / `IG_USER_ID`)

このアプリで発行されるトークンは `IGA...` から始まる形式で、通常60日間有効な長期トークンです。有効期限が近づいたら、以下のエンドポイントで新しいトークンに更新できます(GETリクエスト)。

```
https://graph.instagram.com/refresh_access_token?grant_type=ig_refresh_token&access_token=<現在のIG_ACCESS_TOKEN>
```

返ってきた新しい `access_token` の値で、`.env` および GitHub Secrets の `IG_ACCESS_TOKEN` を更新してください。

### 3. Anthropic APIキーを取得

https://console.anthropic.com/ でAPIキーを発行します(`ANTHROPIC_API_KEY`)。

### 4. Cloudinaryアカウントを作成

https://cloudinary.com/ の無料プランに登録し、ダッシュボードから以下を控えます。

- Cloud name → `CLOUDINARY_CLOUD_NAME`
- API Key → `CLOUDINARY_API_KEY`
- API Secret → `CLOUDINARY_API_SECRET`

### 5. LINE通知を設定する(任意)

投稿の成功・失敗をLINEに通知したい場合は設定してください。LINE Notifyは2025年に終了したため、後継の「LINE Messaging API」を使います。

1. https://www.linebiz.com/jp/entry/ からLINE公式アカウントを新規作成(無料)
2. https://developers.line.biz/console/ を開き、作成した公式アカウントに対応するプロバイダーを選択
3. 「Messaging API」チャネルが自動的に作られているので、それを開く
4. 「Messaging API設定」タブの一番下、「チャネルアクセストークン(長期)」で **発行** をクリックして値を控える(`LINE_CHANNEL_ACCESS_TOKEN`)
5. 同じ画面に表示されているQRコードを、**ご自身のLINEアプリで友だち追加**する(これをしないと通知が届きません)

### 6. GitHub Secretsに登録

リポジトリの `Settings > Secrets and variables > Actions > New repository secret` から、以下を登録します。

- `IG_USER_ID`
- `IG_ACCESS_TOKEN`
- `ANTHROPIC_API_KEY`
- `CLOUDINARY_CLOUD_NAME`
- `CLOUDINARY_API_KEY`
- `CLOUDINARY_API_SECRET`
- `LINE_CHANNEL_ACCESS_TOKEN` (LINE通知を使う場合のみ)

### 7. 画像は基本的に何もしなくてOK

`images/queue/` が空であれば、毎日自動でヤフオクの出品(https://auctions.yahoo.co.jp/seller/7F3TQFS83hRevxWX9wK4z2ZvPzj3t?user_type=c)からランダムに1商品選んで投稿します。出品者ページのURLが変わった場合は、[post_to_instagram.py](post_to_instagram.py)内の `SELLER_URL` を書き換えてください。

#### 特定の商品を優先して投稿したい場合(任意)

`images/queue/` に `.txt` ファイルを1つ置き、中にヤフオクの商品URLを1行書いてください(画像は不要、自動でダウンロードされます)。キューに何かがあれば、そちらが自動選択より優先されます。

```
images/queue/01.txt の中身:
https://page.auctions.yahoo.co.jp/jp/auction/xxxxxxxxxx
```

複数予約したい場合は、投稿したい順にファイル名を連番にしてください(`01.txt`, `02.txt`, ...)。

#### 手元の写真を直接使いたい場合(任意)

画像ファイル(`.jpg` / `.jpeg` / `.png`)を直接 `images/queue/` に置くこともできます。この場合、樹種・寸法をAIが断定しないよう曖昧な表現になります。正確な情報を使いたい場合は、同じファイル名で拡張子だけ `.txt` にしたメモ(樹種・寸法・ヤフオクURLなど)を添えてください。

```
images/queue/01_kobo.jpg
images/queue/01_kobo.txt   ← 「ケヤキ、長さ280cm、耳付き」のように一言メモ
```

### 8. 動作確認(手動実行)

GitHubリポジトリの `Actions > Daily Instagram Post > Run workflow` から、cronを待たずに手動実行できます。まずは1枚テスト用の画像をキューに入れて手動実行し、実際に投稿されるか確認することをおすすめします。

---

## ローカルでのテスト

```bash
pip install -r requirements.txt
cp .env.example .env   # .env を編集して各キーを入力
python post_to_instagram.py
```

---

## 注意点

- **投稿頻度**: 現在は1日1投稿の設定です。1日に複数回投稿したい場合は、cronの行を追加するか `post_to_instagram.py` を複数回呼び出してください。
- **著作権・肖像権**: 自分で撮影した画像のみキューに入れてください。人物が写っている場合は本人の許可を確認してください。
- **アクセストークンの期限**: 60日で失効します。失効するとワークフローが失敗するので、Actionsのメール通知で気づけます。
- **Instagramの自動化ポリシー**: 本ツールは公式のInstagram Graph APIのみを使用しています。ブラウザ操作の自動化(BOTツール等)は規約違反でアカウント凍結リスクがあるため使用していません。
- **ヤフオクからの自動取得について**: 出品者ページのHTML構造を解析して情報を取得しているため、ヤフオクのサイト構造が大きく変わると取得に失敗する可能性があります。失敗した場合はGitHub Actionsの実行ログにエラーが表示されるので、気づいたら教えてください。
