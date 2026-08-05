"""
Junshin (@junshin_industry) 用 Instagram 自動投稿スクリプト。

images/queue/ にある画像を1枚選び、Claude(Vision)でキャプションと
ハッシュタグを生成し、Instagram Graph API で投稿する。
投稿済みの画像は images/posted/ に移動し、posted_log.csv に記録する。

想定実行環境: GitHub Actions の cron (.github/workflows/daily-post.yml)
"""

import base64
import csv
import hashlib
import json
import mimetypes
import os
import random
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import requests
from anthropic import Anthropic
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont

load_dotenv()

# Windows環境で絵文字入りのキャプションをprintすると
# コンソールの既定エンコーディング(cp932等)でエラーになるため、UTF-8に固定する
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

ROOT_DIR = Path(__file__).parent
QUEUE_DIR = ROOT_DIR / "images" / "queue"
POSTED_DIR = ROOT_DIR / "images" / "posted"
LOG_PATH = POSTED_DIR / "posted_log.csv"
POSTED_AUCTION_IDS_PATH = POSTED_DIR / "posted_auction_ids.txt"
VIDEO_QUEUE_DIR = ROOT_DIR / "videos" / "queue"
VIDEO_POSTED_DIR = ROOT_DIR / "videos" / "posted"
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v"}

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
GRAPH_API_VERSION = os.environ.get("GRAPH_API_VERSION", "v21.0")
MAX_CAPTION_LENGTH = 2200
MAX_CAROUSEL_IMAGES = 10
URL_PATTERN = re.compile(r"https?://\S+")
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
SELLER_URL = os.environ.get(
    "YAHOO_SELLER_URL",
    "https://auctions.yahoo.co.jp/seller/7F3TQFS83hRevxWX9wK4z2ZvPzj3t?user_type=c",
)
LOGO_PATH = ROOT_DIR / "assets" / "logo_watermark.png"
FONT_PATH = ROOT_DIR / "assets" / "fonts" / "NotoSansJP.ttf"
BOLD_FONT_PATH = ROOT_DIR / "assets" / "fonts" / "NotoSansJP-Bold.ttf"
WATERMARK_MAX_IMAGES = 9
WATERMARK_WIDTH_RATIO = 0.22
WATERMARK_MARGIN_RATIO = 0.03
LABEL_FONT_SIZE_RATIO = 0.035
LABEL_MARGIN_RATIO = 0.03


def add_watermark(image_path: Path) -> None:
    """画像の右下にロゴ(assets/logo_watermark.png)を合成する。"""
    with Image.open(image_path) as base:
        base = base.convert("RGBA")
        with Image.open(LOGO_PATH) as logo:
            logo = logo.convert("RGBA")
            logo_width = int(base.width * WATERMARK_WIDTH_RATIO)
            logo_height = int(logo.height * (logo_width / logo.width))
            logo = logo.resize((logo_width, logo_height))

            margin = int(base.width * WATERMARK_MARGIN_RATIO)
            position = (
                base.width - logo_width - margin,
                base.height - logo_height - margin,
            )
            base.paste(logo, position, mask=logo)

        base.convert("RGB").save(image_path, quality=95)


def add_label(image_path: Path, text: str) -> None:
    """画像の左上に、樹種名・商品番号などのラベルを、視認性のための
    半透明の白背景付きで描画する。"""
    with Image.open(image_path) as base:
        base = base.convert("RGBA")
        overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        font_size = max(24, int(base.width * LABEL_FONT_SIZE_RATIO))
        font = ImageFont.truetype(str(FONT_PATH), font_size)
        axis_names = {axis["name"] for axis in font.get_variation_axes()}
        if b"Weight" in axis_names:
            font.set_variation_by_axes([700])  # Bold

        margin = int(base.width * LABEL_MARGIN_RATIO)
        padding = int(font_size * 0.4)
        text_bbox = draw.textbbox((0, 0), text, font=font)
        text_width = text_bbox[2] - text_bbox[0]
        text_height = text_bbox[3] - text_bbox[1]

        box = (
            margin,
            margin,
            margin + text_width + padding * 2,
            margin + text_height + padding * 2,
        )
        draw.rectangle(box, fill=(255, 255, 255, 235))
        draw.text(
            (margin + padding - text_bbox[0], margin + padding - text_bbox[1]),
            text,
            font=font,
            fill=(0, 0, 0, 255),
        )

        combined = Image.alpha_composite(base, overlay)
        combined.convert("RGB").save(image_path, quality=95)


def extract_item_code(title: str) -> str | None:
    """商品タイトルから商品番号(例: SKR-529)を取り出す。
    「送料無料！」のような接頭辞が商品番号の前に付いている場合もあるため、
    先頭固定ではなく全体から探す。"""
    match = re.search(r"[A-Za-z]{1,5}-\d+", title)
    return match.group(0) if match else None


def extract_species_name(title: str) -> str | None:
    """商品タイトルから、商品番号の直後・寸法の手前にある樹種名を1つ取り出す。
    出品によって全角/半角スペースが混在するため、空白全般で区切る。"""
    segments = title.split()
    code_index = next(
        (i for i, seg in enumerate(segments) if re.search(r"[A-Za-z]{1,5}-\d+", seg)),
        -1,
    )
    for seg in segments[code_index + 1:]:
        if re.search(r"\d+mm[×xX]", seg):
            break
        return seg
    return None


def extract_dimensions(title: str) -> str | None:
    match = re.search(r"\d+mm[×xX][^\s　【]+mm[×xX][^\s　【]+mm", title)
    return match.group(0) if match else None


def find_next_video() -> Path | None:
    """videos/queue/ の中から、次に投稿する動画(Reels)を返す。"""
    candidates = sorted(
        p for p in VIDEO_QUEUE_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
    )
    return candidates[0] if candidates else None


def probe_video_dimensions(video_path: Path) -> tuple[int, int]:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "json", str(video_path),
        ],
        capture_output=True, text=True, check=True,
    )
    info = json.loads(result.stdout)["streams"][0]
    return info["width"], info["height"]


def process_video(video_path: Path, label_lines: list[str]) -> Path:
    """動画の右下にロゴを、(あれば)左上に樹種・寸法ラベルを合成する。"""
    width, _height = probe_video_dimensions(video_path)

    font_size = max(24, int(width * LABEL_FONT_SIZE_RATIO))
    margin = int(width * LABEL_MARGIN_RATIO)
    line_gap = int(font_size * 0.3)
    logo_width = int(width * WATERMARK_WIDTH_RATIO)
    logo_margin = int(width * WATERMARK_MARGIN_RATIO)

    font_rel = os.path.relpath(BOLD_FONT_PATH, ROOT_DIR).replace("\\", "/")
    output_path = video_path.with_name(f"{video_path.stem}_labeled.mp4")

    with tempfile.TemporaryDirectory(dir=ROOT_DIR) as tmpdir:
        tmp_rel = os.path.relpath(tmpdir, ROOT_DIR).replace("\\", "/")

        filters = []
        current = "0:v"
        for i, line in enumerate(label_lines):
            line_file = Path(tmpdir) / f"line{i}.txt"
            line_file.write_text(line, encoding="utf-8")
            y = margin + i * (font_size + line_gap)
            next_label = f"v{i}"
            filters.append(
                f"[{current}]drawtext=fontfile='{font_rel}':"
                f"textfile='{tmp_rel}/line{i}.txt':"
                f"fontcolor=black:fontsize={font_size}:box=1:boxcolor=white@0.85:"
                f"boxborderw={int(font_size * 0.3)}:x={margin}:y={y}[{next_label}]"
            )
            current = next_label

        filters.append(f"[1:v]scale={logo_width}:-1[logo]")
        filters.append(f"[{current}][logo]overlay=W-w-{logo_margin}:H-h-{logo_margin}[vout]")

        cmd = [
            "ffmpeg", "-y",
            "-i", str(video_path),
            "-i", str(LOGO_PATH),
            "-filter_complex", ";".join(filters),
            "-map", "[vout]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            str(output_path),
        ]
        subprocess.run(cmd, cwd=ROOT_DIR, capture_output=True, text=True, check=True)

    return output_path


def extract_video_thumbnail(video_path: Path) -> Path:
    """Claudeでのキャプション生成用に、動画から1枚静止画を切り出す。"""
    thumbnail_path = video_path.with_suffix(".jpg")
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(video_path),
            "-ss", "00:00:00.5", "-frames:v", "1", str(thumbnail_path),
        ],
        capture_output=True, text=True, check=True,
    )
    return thumbnail_path


BOTTOM_CTA_PHRASES = [
    "気になる方はプロフィールのリンクからどうぞ",
    "ヤフオクで購入できます(プロフィールのリンクへ)",
    "DIYにも業者様の仕入れにも対応しています",
    "1点から購入OK、お気軽にどうぞ",
    "詳しくはプロフィールのリンクをチェック",
]


def derive_label_lines(raw_metadata: str | None) -> list[str]:
    """メモに含まれるヤフオクのURLから、樹種名・商品番号・寸法のラベル行を作る。"""
    if not raw_metadata:
        return []
    urls = URL_PATTERN.findall(raw_metadata)
    if not urls:
        return []
    item = fetch_yahoo_auction_item(urls[0])
    if not item:
        return []
    species = extract_species_name(item["title"])
    code = extract_item_code(item["title"])
    dimensions = extract_dimensions(item["title"])
    return [
        line for line in (
            "  ".join(part for part in (species, code) if part),
            dimensions,
        ) if line
    ]


def build_slideshow_video(
    image_paths: list[Path],
    output_path: Path,
    top_text: str | None = None,
    bottom_text: str | None = None,
    seconds_per_image: float = 1.8,
) -> Path:
    """複数枚の画像(ロゴ・ラベル合成済み)から、無音のスライドショー動画を作る。
    上下の白い余白部分に、樹種・商品番号(上)と購入を促す一言(下)を重ねる。"""
    with tempfile.TemporaryDirectory(dir=ROOT_DIR) as tmpdir:
        list_path = Path(tmpdir) / "concat_list.txt"
        lines = []
        for p in image_paths:
            lines.append(f"file '{p.resolve().as_posix()}'")
            lines.append(f"duration {seconds_per_image}")
        # concatデムクサーの仕様上、最後のファイルのdurationは無視されるため、
        # 最後の画像をもう一度(duration指定なしで)追記する
        lines.append(f"file '{image_paths[-1].resolve().as_posix()}'")
        list_path.write_text("\n".join(lines), encoding="utf-8")

        vf_parts = [
            "scale=1080:1920:force_original_aspect_ratio=decrease",
            "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=white",
            "fps=30",
        ]

        font_rel = os.path.relpath(BOLD_FONT_PATH, ROOT_DIR).replace("\\", "/")

        if top_text:
            top_file = Path(tmpdir) / "top.txt"
            top_file.write_text(top_text, encoding="utf-8")
            top_rel = os.path.relpath(top_file, ROOT_DIR).replace("\\", "/")
            vf_parts.append(
                f"drawtext=fontfile='{font_rel}':textfile='{top_rel}':"
                f"fontcolor=black:fontsize=52:x=(w-text_w)/2:y=320"
            )

        if bottom_text:
            bottom_file = Path(tmpdir) / "bottom.txt"
            bottom_file.write_text(bottom_text, encoding="utf-8")
            bottom_rel = os.path.relpath(bottom_file, ROOT_DIR).replace("\\", "/")
            vf_parts.append(
                f"drawtext=fontfile='{font_rel}':textfile='{bottom_rel}':"
                f"fontcolor=black:fontsize=46:x=(w-text_w)/2:y=h-th-320"
            )

        cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(list_path),
            "-vf", ",".join(vf_parts),
            "-pix_fmt", "yuv420p",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-movflags", "+faststart",
            str(output_path),
        ]
        subprocess.run(cmd, cwd=ROOT_DIR, capture_output=True, text=True, check=True)

    return output_path


def upload_video_to_cloudinary(video_path: Path) -> str:
    """動画をCloudinaryにアップロードし、Instagram Graph APIから取得可能な
    公開URL(secure_url)を返す。"""
    cloud_name = os.environ["CLOUDINARY_CLOUD_NAME"]
    api_key = os.environ["CLOUDINARY_API_KEY"]
    api_secret = os.environ["CLOUDINARY_API_SECRET"]

    timestamp = str(int(time.time()))
    signature = hashlib.sha1(
        f"timestamp={timestamp}{api_secret}".encode("utf-8")
    ).hexdigest()

    url = f"https://api.cloudinary.com/v1_1/{cloud_name}/video/upload"
    with video_path.open("rb") as f:
        resp = requests.post(
            url,
            data={
                "api_key": api_key,
                "timestamp": timestamp,
                "signature": signature,
            },
            files={"file": (video_path.name, f, "video/mp4")},
            timeout=300,
        )
    payload = resp.json()
    if "secure_url" not in payload:
        raise RuntimeError(f"Cloudinaryへの動画アップロードに失敗: {payload}")
    return payload["secure_url"]


def notify_line(message: str) -> None:
    """LINE公式アカウント(Messaging API)経由で、自分宛てに通知を送る。
    通知の失敗は投稿処理自体を止めないよう、例外を握りつぶす。"""
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
    if not token:
        return
    try:
        requests.post(
            "https://api.line.me/v2/bot/message/broadcast",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            json={"messages": [{"type": "text", "text": message}]},
            timeout=15,
        )
    except Exception:
        pass

FIXED_HASHTAGS = """\
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
#gallery"""

BRAND_CONTEXT = """\
アカウント: @junshin_industry (森からつくる木の生活道具 Junshin -潤森-)
所在地: 新潟県阿賀野市の山林に囲まれた小さな木工房 (2014年creation)
事業内容: 森つくり / 庭木伐採 / 薪の製造・販売 / 木材販売(業販・DIY向け) / \
木製品制作(食器・雑貨などの生活道具)
販売方法: 投稿する木材はすべてヤフオク(Yahoo!オークション)に出品しており、\
プロフィールのリンクから購入できる。個人のDIYユーザーから、業者のまとめ買い・\
仕入れまで幅広く対応している。
トーン: 森や木、職人の手仕事への愛情が伝わる、温かみのある丁寧な言葉づかい。\
派手な煽り文句は避け、素材やものづくりの背景が伝わる説明を大切にする。
想定フォロワー: 木工・DIY・林業・ナチュラルな暮らしに関心がある人、\
資材を探している業者。
"""


def find_next_entry() -> list[Path] | None:
    """images/queue/ の中から、次に投稿する画像(1枚、またはカルーセル用の複数枚)
    を返す。画像ファイルがまだなく、ヤフオクURLを書いた.txtだけが置かれている
    場合は、出品ページの画像(最大 MAX_CAROUSEL_IMAGES 枚)を自動ダウンロードする。"""
    stems = sorted({
        entry_stem(p) if p.suffix.lower() in IMAGE_EXTENSIONS else p.stem
        for p in QUEUE_DIR.iterdir()
        if p.is_file() and p.name != ".gitkeep"
        and (p.suffix.lower() in IMAGE_EXTENSIONS or p.suffix.lower() == ".txt")
    })

    for stem in stems:
        images = existing_images_for_stem(stem)
        if images:
            return images

        txt_path = QUEUE_DIR / f"{stem}.txt"
        if txt_path.exists():
            urls = URL_PATTERN.findall(txt_path.read_text(encoding="utf-8"))
            if urls:
                item = fetch_yahoo_auction_item(urls[0])
                if item:
                    downloaded = download_auction_images(item, stem)
                    if downloaded:
                        return downloaded

    return None


NUMBERED_SUFFIX_PATTERN = re.compile(r"^(?P<stem>.+)-(?P<index>\d+)$")


def entry_stem(image_path: Path) -> str:
    """自動ダウンロードした連番画像(例: 01-3.jpg)から、共通のstem(例: 01)を取り出す。
    連番でない通常のファイル名(例: 01.jpg)は、そのままstemとして返す。"""
    match = NUMBERED_SUFFIX_PATTERN.match(image_path.stem)
    return match.group("stem") if match else image_path.stem


def existing_images_for_stem(stem: str) -> list[Path]:
    # 手動で追加された単一の画像(例: 01.jpg)
    for ext in IMAGE_EXTENSIONS:
        candidate = QUEUE_DIR / f"{stem}{ext}"
        if candidate.exists():
            return [candidate]

    # 自動ダウンロード済みの連番画像(例: 01-1.jpg, 01-2.jpg, ..., 01-10.jpg)
    # 文字列順だと "-10" が "-2" より前に来てしまうため、数値として並べ替える
    numbered = sorted(
        (p for p in QUEUE_DIR.iterdir() if p.is_file() and entry_stem(p) == stem and p.stem != stem),
        key=lambda p: int(NUMBERED_SUFFIX_PATTERN.match(p.stem).group("index")),
    )
    return numbered


def find_metadata(image_paths: list[Path]) -> str | None:
    """画像(群)と同名の.txtファイル(例: 01.jpg → 01.txt)があれば、
    生のメモ内容(ヤフオクURLや手書きの補足など)をそのまま返す。
    ログでの追跡用に、加工前の内容を保持しておく。"""
    stem = entry_stem(image_paths[0])
    metadata_path = QUEUE_DIR / f"{stem}.txt"
    if metadata_path.exists():
        raw = metadata_path.read_text(encoding="utf-8").strip()
        return raw or None
    return None


def resolve_caption_facts(raw_metadata: str | None) -> str | None:
    """メモの中にヤフオクのURLがあれば、出品ページから樹種・寸法・価格などを
    自動取得してキャプション生成用の事実情報にする(URL自体はここで消費し、
    キャプションには含めない)。取得に失敗した場合はメモをそのまま使う。"""
    if not raw_metadata:
        return None

    urls = URL_PATTERN.findall(raw_metadata)
    if not urls:
        return raw_metadata

    notes = URL_PATTERN.sub("", raw_metadata).strip()
    item = fetch_yahoo_auction_item(urls[0])
    facts = format_auction_facts(item) if item else None
    if facts is None:
        return raw_metadata

    return f"{facts}\n補足メモ: {notes}" if notes else facts


def fetch_yahoo_auction_item(url: str) -> dict | None:
    """ヤフオクの商品ページの内部データ(タイトル・商品説明・価格・画像URL等)を
    取得する。取得や解析に失敗した場合はNoneを返す。"""
    try:
        resp = requests.get(url, headers={"User-Agent": BROWSER_USER_AGENT}, timeout=15)
        resp.raise_for_status()
        match = re.search(
            r'__NEXT_DATA__"\s*type="application/json">(.*?)</script>',
            resp.text,
            re.S,
        )
        if not match:
            return None
        data = json.loads(match.group(1))
        return data["props"]["pageProps"]["initialState"]["item"]["detail"]["item"]
    except Exception:
        return None


def format_auction_facts(item: dict) -> str:
    title = item["title"]
    lines = [f"商品タイトル: {title}"]

    code = extract_item_code(title)
    if code:
        lines.append(f"商品番号: {code}")

    dimensions = extract_dimensions(title)
    if dimensions:
        lines.append(f"寸法: {dimensions}")

    description = "\n".join(item.get("description") or [])
    if description:
        lines.append(f"商品説明: {description}")
    if item.get("price"):
        lines.append(f"価格: {item['price']}円")
    return "\n".join(lines)


def fetch_seller_auction_ids(seller_url: str) -> list[str]:
    """出品者ページから、出品中の全オークションIDを取得する(ページネーション対応)。"""
    base_url = seller_url.split("?")[0]
    ids: list[str] = []
    offset = 1

    while True:
        try:
            resp = requests.get(
                base_url,
                params={"user_type": "c", "b": offset},
                headers={"User-Agent": BROWSER_USER_AGENT},
                timeout=15,
            )
            resp.raise_for_status()
            match = re.search(
                r'__NEXT_DATA__"\s*type="application/json">(.*?)</script>',
                resp.text,
                re.S,
            )
            if not match:
                break
            data = json.loads(match.group(1))
            listing = data["props"]["pageProps"]["initialState"]["search"]["items"]["listing"]
            page_ids = [item["auctionId"] for item in listing.get("items", [])]
        except Exception:
            break

        if not page_ids:
            break
        ids.extend(page_ids)

        total = listing.get("totalResultsAvailable", len(ids))
        offset += 50
        if offset > total:
            break

    return ids


def load_posted_auction_ids() -> set[str]:
    if POSTED_AUCTION_IDS_PATH.exists():
        return set(POSTED_AUCTION_IDS_PATH.read_text(encoding="utf-8").split())
    return set()


def mark_auction_id_posted(auction_id: str) -> None:
    POSTED_DIR.mkdir(parents=True, exist_ok=True)
    with POSTED_AUCTION_IDS_PATH.open("a", encoding="utf-8") as f:
        f.write(auction_id + "\n")


def pick_random_unposted_auction(seller_url: str) -> str | None:
    """出品者ページの全出品から、まだ投稿していない商品のURLをランダムに1つ選ぶ。
    候補がなければNoneを返す。"""
    all_ids = fetch_seller_auction_ids(seller_url)
    posted_ids = load_posted_auction_ids()
    candidates = [aid for aid in all_ids if aid not in posted_ids]
    if not candidates:
        return None
    chosen = random.choice(candidates)
    return f"https://auctions.yahoo.co.jp/jp/auction/{chosen}"


def download_auction_images(item: dict, stem: str) -> list[Path]:
    """ヤフオク出品ページの画像を、最大 MAX_CAROUSEL_IMAGES 枚まで
    images/queue/{stem}-1.jpg, {stem}-2.jpg, ... としてダウンロードする。"""
    images = (item.get("img") or [])[:MAX_CAROUSEL_IMAGES]

    downloaded = []
    for index, image in enumerate(images, start=1):
        image_url = image["image"]
        try:
            resp = requests.get(image_url, headers={"User-Agent": BROWSER_USER_AGENT}, timeout=30)
            resp.raise_for_status()
        except Exception:
            continue
        ext = Path(image_url.split("?")[0]).suffix or ".jpg"
        dest = QUEUE_DIR / f"{stem}-{index}{ext}"
        dest.write_bytes(resp.content)
        if index <= WATERMARK_MAX_IMAGES:
            add_watermark(dest)
        downloaded.append(dest)
    return downloaded


def upload_to_cloudinary(image_path: Path) -> str:
    """画像をCloudinaryにアップロードし、Instagram Graph APIから取得可能な
    公開URL(secure_url)を返す。"""
    cloud_name = os.environ["CLOUDINARY_CLOUD_NAME"]
    api_key = os.environ["CLOUDINARY_API_KEY"]
    api_secret = os.environ["CLOUDINARY_API_SECRET"]

    timestamp = str(int(time.time()))
    signature = hashlib.sha1(
        f"timestamp={timestamp}{api_secret}".encode("utf-8")
    ).hexdigest()

    url = f"https://api.cloudinary.com/v1_1/{cloud_name}/image/upload"
    with image_path.open("rb") as f:
        resp = requests.post(
            url,
            data={
                "api_key": api_key,
                "timestamp": timestamp,
                "signature": signature,
            },
            files={"file": (image_path.name, f, mimetypes.guess_type(image_path.name)[0])},
            timeout=60,
        )
    payload = resp.json()
    if "secure_url" not in payload:
        raise RuntimeError(f"Cloudinaryへのアップロードに失敗: {payload}")
    return payload["secure_url"]


def generate_caption(image_path: Path, metadata: str | None = None) -> str:
    client = Anthropic()
    media_type = mimetypes.guess_type(image_path.name)[0] or "image/jpeg"
    image_b64 = base64.standard_b64encode(image_path.read_bytes()).decode("utf-8")

    if metadata:
        fact_section = f"""【この画像の事実情報(必ず正確に使うこと)】
{metadata}

- 上記に書かれている樹種・寸法・状態などの情報は、この通りに使ってよい
- 上記に書かれていないことについては、樹種名や正確な寸法を断定しないこと
- 「なかなか出ない」「珍しい」「うちでも多くない」のような希少性・頻度に関する
  主張は、上記の事実情報に明記されていない限り書かないこと\
  (実際にはよくあるサイズ・樹種かもしれないため、根拠のない誇張は厳禁)

【必ず含める情報(絶対厳守)】
- 上記の事実情報に「商品番号」が書かれている場合は、その番号(例: SKR-529)を\
  本文中に必ずそのまま記載すること(「商品番号 SKR-529」のように自然な形で組み込む)
- 上記の事実情報に「寸法」が書かれている場合は、その数値をそのまま本文中に必ず記載すること
- どちらも省略せず、本文のどこかに含めること

【樹種の特徴について】
- 上記の事実情報(商品タイトルなど)から樹種名が分かる場合、その樹種について\
  一般的に知られている特徴(木目や色合いの傾向、硬さ・耐久性、香り、\
  よく使われる用途など)を、あなたの知識をもとに1〜2文程度加えること
- 樹種名がはっきり分からない場合や、その樹種について確実な知識がない場合は、\
  無理に特徴を書かず、見た目から分かる範囲の描写にとどめること"""
    else:
        fact_section = """【この画像の事実情報】
- 事実情報のメモは提供されていない
- 樹種名(ケヤキ、ヒノキ等)や正確な寸法(◯m、◯cm等)を断定して書かないこと。
  代わりに「広葉樹の一枚板」「長さのある板」のような、画像から確実に分かる
  範囲の表現にとどめること
- 「なかなか出ない」「珍しい」「うちでも多くない」のような希少性・頻度に関する
  主張も、根拠がないので書かないこと"""

    prompt = f"""あなたは@junshin_industryの中の人(職人自身)になったつもりで、
今日の投稿キャプションを1件書いてください。

【ブランド情報】
{BRAND_CONTEXT}

{fact_section}

【文章のトーン(最重要)】
- ものづくり系・暮らし系で人気のインスタグラマーが書くような、勢いと個性のある文章にする。
  ただし嘘くさい煽りではなく、実際に木工房で働いている人が書いた実感のある投稿にすること
- 詩的な比喩、抽象的な言い回しは避ける
  (NG例: 「木の生きてきた時間そのもの」「光や風の記憶」「想像するだけで手が動き出す」
  のような、AIが書いたと分かる大げさな表現は使わない)
- 1行目は「見た人が指を止める」フック。断定・数字・問いかけなど、
  人気アカウントがよく使う型を1つ選んで使う(例:「〇〇な板、入荷しました。」
  「今日はちょっと自慢したい一枚。」「こういう表情、好きな人多いと思います。」など、
  画像に合わせて内容は毎回変える。ただし希少性・限定性を煽る表現は避ける)
- 画像に写っている色・質感・状態など、見た目から確実に言えることは具体的に描写する
  (樹種名・正確な寸法は上記「事実情報」のルールに従うこと)。
  そのうえで一人称の感情や本音を一言添える(「地味に嬉しい」「正直ちょっと悩み中」など)
- 気取らず、たまには「〜です」だけでなく「〜でした」「〜なんです」のような
  話し言葉に近い語尾も混ぜて、単調にならないようにする
- 毎回同じ結び方(「どんなものを作りたいですか?」等)を繰り返さない。
  日によって、近況の一言・小さな失敗談・作業のこぼれ話・問いかけ・
  「保存推奨」「フォローして次の投稿もチェックしてください」的な
  さりげないCTAなど、パターンを変化させる

【販促(必須)】
- この投稿の木材はヤフオクで購入できることを、最後の1〜2文で自然に伝えること
  (押し売り感を出さず、「気になる方はプロフィールのリンクからどうぞ」
  「ヤフオクに出品しています、詳しくはプロフィールへ」のように、\
  日によって言い回しを変える)
- DIYで少量欲しい個人と、まとめて仕入れたい業者の両方に向けて、\
  「個人の方でも業者様でも」「1点からでも、まとめてでも」のような\
  一言を混ぜられるとなお良い(毎回でなくてよい)

【構成】
- 1行目: 上記のフックとなる一文(単独で改行し目立たせる)
- 本文3〜5行程度、適度に改行を入れてテンポよく読めるようにする
- 絵文字は1〜3個程度、要所で使って視線の誘導・強弱をつける
- 最後の一文は上記トーンに沿って、その日ごとに変化をつける

【ハッシュタグについて】
- ハッシュタグは書かないこと。投稿する全てのハッシュタグは固定文言として\
  別途システム側で自動的に追加されるため、あなたはキャプション本文だけを書けばよい

【その他】
- キャプション全体で1500文字を超えないこと(ハッシュタグは別途追加されるため、\
  本文だけでこの文字数に収めること)
- 出力はキャプション本文のみ。説明や前置き、ハッシュタグは一切書かないこと
"""

    response = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=1024,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": image_b64,
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    )
    text_block = next(block for block in response.content if block.type == "text")
    body = text_block.text.strip()
    return enforce_limits(f"{body}\n\n{FIXED_HASHTAGS}")


def enforce_limits(caption: str) -> str:
    if len(caption) > MAX_CAPTION_LENGTH:
        caption = caption[:MAX_CAPTION_LENGTH].rstrip()
    return caption


def create_media_container(image_url: str, caption: str) -> str:
    ig_user_id = os.environ["IG_USER_ID"]
    url = f"https://graph.instagram.com/{GRAPH_API_VERSION}/{ig_user_id}/media"
    resp = requests.post(
        url,
        data={
            "image_url": image_url,
            "caption": caption,
            "access_token": os.environ["IG_ACCESS_TOKEN"],
        },
        timeout=30,
    )
    payload = resp.json()
    if "error" in payload:
        raise RuntimeError(f"メディア作成に失敗: {payload['error']}")
    return payload["id"]


def create_carousel_item_container(image_url: str) -> str:
    ig_user_id = os.environ["IG_USER_ID"]
    url = f"https://graph.instagram.com/{GRAPH_API_VERSION}/{ig_user_id}/media"
    resp = requests.post(
        url,
        data={
            "image_url": image_url,
            "is_carousel_item": "true",
            "access_token": os.environ["IG_ACCESS_TOKEN"],
        },
        timeout=30,
    )
    payload = resp.json()
    if "error" in payload:
        raise RuntimeError(f"カルーセル用メディア作成に失敗: {payload['error']}")
    return payload["id"]


def create_carousel_container(children_ids: list[str], caption: str) -> str:
    ig_user_id = os.environ["IG_USER_ID"]
    url = f"https://graph.instagram.com/{GRAPH_API_VERSION}/{ig_user_id}/media"
    resp = requests.post(
        url,
        data={
            "media_type": "CAROUSEL",
            "children": ",".join(children_ids),
            "caption": caption,
            "access_token": os.environ["IG_ACCESS_TOKEN"],
        },
        timeout=30,
    )
    payload = resp.json()
    if "error" in payload:
        raise RuntimeError(f"カルーセル作成に失敗: {payload['error']}")
    return payload["id"]


def create_reels_container(video_url: str, caption: str) -> str:
    ig_user_id = os.environ["IG_USER_ID"]
    url = f"https://graph.instagram.com/{GRAPH_API_VERSION}/{ig_user_id}/media"
    resp = requests.post(
        url,
        data={
            "media_type": "REELS",
            "video_url": video_url,
            "caption": caption,
            "access_token": os.environ["IG_ACCESS_TOKEN"],
        },
        timeout=30,
    )
    payload = resp.json()
    if "error" in payload:
        raise RuntimeError(f"Reelsメディア作成に失敗: {payload['error']}")
    return payload["id"]


def wait_until_ready(creation_id: str, attempts: int = 10, interval_sec: int = 3) -> None:
    url = f"https://graph.instagram.com/{GRAPH_API_VERSION}/{creation_id}"
    for _ in range(attempts):
        resp = requests.get(
            url,
            params={
                "fields": "status_code",
                "access_token": os.environ["IG_ACCESS_TOKEN"],
            },
            timeout=30,
        )
        status = resp.json().get("status_code")
        if status == "FINISHED":
            return
        if status == "ERROR":
            raise RuntimeError("メディアの処理中にエラーが発生しました")
        time.sleep(interval_sec)


def publish_media(creation_id: str) -> str:
    ig_user_id = os.environ["IG_USER_ID"]
    url = f"https://graph.instagram.com/{GRAPH_API_VERSION}/{ig_user_id}/media_publish"
    resp = requests.post(
        url,
        data={
            "creation_id": creation_id,
            "access_token": os.environ["IG_ACCESS_TOKEN"],
        },
        timeout=30,
    )
    payload = resp.json()
    if "error" in payload:
        raise RuntimeError(f"公開に失敗: {payload['error']}")
    return payload["id"]


def move_to_posted(image_paths: list[Path]) -> list[Path]:
    POSTED_DIR.mkdir(parents=True, exist_ok=True)
    destinations = []
    for image_path in image_paths:
        destination = POSTED_DIR / image_path.name
        image_path.rename(destination)
        destinations.append(destination)

    stem = entry_stem(image_paths[0])
    metadata_path = QUEUE_DIR / f"{stem}.txt"
    if metadata_path.exists():
        metadata_path.rename(POSTED_DIR / f"{stem}.txt")

    return destinations


def log_post(image_name: str, media_id: str, caption: str, metadata: str | None = None) -> None:
    is_new_file = not LOG_PATH.exists()
    with LOG_PATH.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new_file:
            writer.writerow(["posted_at", "image", "media_id", "caption_preview", "metadata"])
        writer.writerow([
            datetime.utcnow().isoformat(),
            image_name,
            media_id,
            caption[:80].replace("\n", " "),
            (metadata or "").replace("\n", " "),
        ])


def handle_video_post(video_path: Path) -> None:
    """videos/queue/ にある動画をReelsとして投稿する。"""
    print(f"投稿対象(動画): {video_path.name}")
    raw_metadata = find_metadata([video_path])
    caption_facts = resolve_caption_facts(raw_metadata)
    if caption_facts:
        print(f"事実情報を読み込みました:\n{caption_facts}")

    label_lines = derive_label_lines(raw_metadata)

    processed_path = process_video(video_path, label_lines)
    thumbnail_path = extract_video_thumbnail(processed_path)

    video_url = upload_video_to_cloudinary(processed_path)
    print(f"Cloudinaryへ動画アップロード完了: {video_url}")

    caption = generate_caption(thumbnail_path, caption_facts)
    print("キャプションを生成しました:")
    print(caption)

    creation_id = create_reels_container(video_url, caption)
    wait_until_ready(creation_id, attempts=60, interval_sec=5)
    media_id = publish_media(creation_id)
    print(f"投稿完了: media_id={media_id}")

    VIDEO_POSTED_DIR.mkdir(parents=True, exist_ok=True)
    destination = VIDEO_POSTED_DIR / video_path.name
    video_path.rename(destination)
    processed_path.unlink(missing_ok=True)
    thumbnail_path.unlink(missing_ok=True)

    metadata_path = video_path.with_suffix(".txt")
    if metadata_path.exists():
        metadata_path.rename(VIDEO_POSTED_DIR / metadata_path.name)

    log_post(destination.name, media_id, caption, raw_metadata)
    print(f"動画を {destination} に移動し、ログを記録しました。")

    first_line = caption.strip().splitlines()[0]
    notify_line(
        f"✅ Instagramに投稿しました(Reels)\n\n{first_line}\n\n"
        f"https://www.instagram.com/junshin_industry/"
    )


def main() -> None:
    video_path = find_next_video()
    if video_path:
        handle_video_post(video_path)
        return

    image_paths = find_next_entry()
    auction_id = None

    if image_paths:
        print(f"投稿対象(キュー): {[p.name for p in image_paths]}")
        raw_metadata = find_metadata(image_paths)
        caption_facts = resolve_caption_facts(raw_metadata)
    else:
        auction_url = pick_random_unposted_auction(SELLER_URL)
        if not auction_url:
            print("images/queue/ に画像がなく、出品中の商品もすべて投稿済みのようです。処理をスキップします。")
            return

        print(f"出品者ページからランダムに選びました: {auction_url}")
        item = fetch_yahoo_auction_item(auction_url)
        if not item:
            print(f"商品情報の取得に失敗しました: {auction_url}", file=sys.stderr)
            return

        auction_id = item["auctionId"]
        image_paths = download_auction_images(item, f"auto-{auction_id}")
        if not image_paths:
            print(f"画像のダウンロードに失敗しました: {auction_url}", file=sys.stderr)
            return

        raw_metadata = auction_url
        caption_facts = format_auction_facts(item)

    if caption_facts:
        print(f"事実情報を読み込みました:\n{caption_facts}")

    caption = generate_caption(image_paths[0], caption_facts)
    print("キャプションを生成しました:")
    print(caption)

    # ヤフオクから自動ダウンロードした複数枚画像(auto-xxx-1.jpg等)は、
    # カルーセルではなくスライドショーのReelsとして投稿する
    is_auction_batch = len(image_paths) > 1 and bool(
        NUMBERED_SUFFIX_PATTERN.match(image_paths[0].stem)
    )

    if is_auction_batch:
        label_lines = derive_label_lines(raw_metadata)
        top_text = "\n".join(label_lines) if label_lines else None
        bottom_text = random.choice(BOTTOM_CTA_PHRASES)

        slideshow_path = image_paths[0].with_name(
            f"{entry_stem(image_paths[0])}_slideshow.mp4"
        )
        build_slideshow_video(image_paths, slideshow_path, top_text, bottom_text)
        print(f"スライドショー動画を作成しました: {slideshow_path.name}")

        video_url = upload_video_to_cloudinary(slideshow_path)
        print(f"Cloudinaryへ動画アップロード完了: {video_url}")

        creation_id = create_reels_container(video_url, caption)
        wait_until_ready(creation_id, attempts=60, interval_sec=5)
        slideshow_path.unlink(missing_ok=True)
        media_count_label = f"スライドショー({len(image_paths)}枚)"
    else:
        image_urls = [upload_to_cloudinary(p) for p in image_paths]
        print(f"Cloudinaryへアップロード完了: {len(image_urls)}枚")

        if len(image_urls) == 1:
            creation_id = create_media_container(image_urls[0], caption)
            wait_until_ready(creation_id)
        else:
            children_ids = []
            for image_url in image_urls:
                child_id = create_carousel_item_container(image_url)
                wait_until_ready(child_id)
                children_ids.append(child_id)
            creation_id = create_carousel_container(children_ids, caption)
            wait_until_ready(creation_id)
        media_count_label = f"{len(image_urls)}枚"

    media_id = publish_media(creation_id)
    print(f"投稿完了: media_id={media_id}")

    posted_paths = move_to_posted(image_paths)
    log_name = posted_paths[0].name if len(posted_paths) == 1 else f"{entry_stem(image_paths[0])} ({len(posted_paths)}枚)"
    log_post(log_name, media_id, caption, raw_metadata)

    if auction_id:
        mark_auction_id_posted(auction_id)

    print(f"画像を images/posted/ に移動し、ログを記録しました。")

    first_line = caption.strip().splitlines()[0]
    notify_line(
        f"✅ Instagramに投稿しました\n\n{first_line}\n\n{media_count_label}\n"
        f"https://www.instagram.com/junshin_industry/"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        notify_line(f"⚠️ Instagram自動投稿でエラーが発生しました\n\n{exc}")
        sys.exit(1)
