"""把仓库数据画成一张卡片。只用 Pillow，不依赖 AstrBot，可以单独跑测试。

对外只有一个入口：render_card(data, avatar_bytes=None, font_path=None) -> bytes(PNG)
"""

from __future__ import annotations

import io
import re
from datetime import datetime, timezone
from typing import Any

from PIL import Image, ImageDraw, ImageFont

W, H = 960, 440
PAD = 40
AVATAR = 96

BG_TOP = (13, 17, 23)
BG_BOTTOM = (28, 33, 40)
BORDER = (48, 54, 61)
FG = (230, 237, 243)
DIM = (139, 148, 158)
FAINT = (110, 118, 129)
ACCENT = (88, 166, 255)
DIVIDER = (33, 38, 45)

# 字体候选：先 Windows，再常见 Linux 中文字体，最后兜底
_REGULAR = [
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyhl.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]
_BOLD = [
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]

# 常见语言配色，画语言圆点用
LANG_COLORS = {
    "python": "#3572A5", "javascript": "#f1e05a", "typescript": "#3178c6",
    "go": "#00ADD8", "rust": "#dea584", "java": "#b07219", "c": "#555555",
    "c++": "#f34b7d", "c#": "#178600", "php": "#4F5D95", "ruby": "#701516",
    "swift": "#F05138", "kotlin": "#A97BFF", "html": "#e34c26", "css": "#563d7c",
    "shell": "#89e051", "vue": "#41b883", "dart": "#00B4AB", "scala": "#c22d40",
    "lua": "#000080", "r": "#198CE7", "perl": "#0298c3", "elixir": "#6e4a7e",
    "haskell": "#5e5086", "julia": "#a270ba", "zig": "#ec915c", "nim": "#ffc200",
    "clojure": "#db5855", "erlang": "#B83998", "objective-c": "#438eff",
    "assembly": "#6E4C13", "powershell": "#012456", "tex": "#3D6117",
    "markdown": "#083fa1", "dockerfile": "#384d54", "makefile": "#427819",
    "vim script": "#199f4b", "svelte": "#ff3e00", "astro": "#ff5a03",
}

_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-'\.\+#/&:]+|\s+|[^\sA-Za-z0-9]")

_font_cache: dict[tuple[str, int], Any] = {}


def _load_font(size: int, bold: bool = False, custom: str | None = None):
    """按候选顺序找字体；找不到就用 Pillow 自带字体兜底。"""
    key = ("custom" if custom else ("b" if bold else "r"), size)
    if key in _font_cache:
        return _font_cache[key]

    candidates: list[str] = []
    if custom:
        candidates.append(custom)
    candidates.extend(_BOLD if bold else _REGULAR)
    for path in candidates:
        try:
            font = ImageFont.truetype(path, size)
            _font_cache[key] = font
            return font
        except Exception:
            continue
    try:
        font = ImageFont.load_default(size=size)
    except TypeError:  # 老版本 Pillow
        font = ImageFont.load_default()
    _font_cache[key] = font
    return font


def _has_glyph(font, ch: str) -> bool:
    if ch.isspace():
        return True
    try:
        return font.getmask(ch).getbbox() is not None
    except Exception:
        return False


def _safe_text(text: str, font) -> str:
    """去掉当前字体画不出来的字，避免整屏豆腐块。"""
    if not text:
        return ""
    return "".join(ch for ch in text if _has_glyph(font, ch))


def _text_w(draw, text: str, font) -> int:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0]


def _wrap(draw, text: str, font, max_w: int, max_lines: int) -> list[str]:
    """贪心换行，中英文都吃得下；超出行数就截断加省略号。"""
    tokens = _TOKEN_RE.findall(text)
    lines: list[str] = []
    cur = ""
    for token in tokens:
        trial = cur + token
        if _text_w(draw, trial.rstrip(), font) <= max_w:
            cur = trial
            continue
        if cur.strip():
            lines.append(cur.rstrip())
            cur = "" if token.isspace() else token
        else:
            cur = token
        # 单个 token 本身就超宽（超长英文单词），按字符硬切
        while _text_w(draw, cur, font) > max_w and len(cur) > 1:
            cut = len(cur)
            while cut > 1 and _text_w(draw, cur[:cut], font) > max_w:
                cut -= 1
            lines.append(cur[:cut])
            cur = cur[cut:]
            if len(lines) >= max_lines:
                break
        if len(lines) >= max_lines:
            break
    if cur.strip() and len(lines) < max_lines:
        lines.append(cur.rstrip())

    if len(lines) > max_lines:
        lines = lines[:max_lines]

    # 判断是否还有没放下的内容，补省略号
    used = "".join(lines)
    if len(used.replace(" ", "")) < len(text.replace(" ", "")) and lines:
        last = lines[-1]
        while last and _text_w(draw, last + "…", font) > max_w:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
    return lines


def _hex(color: str) -> tuple[int, int, int]:
    c = (color or "").lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    if len(c) != 6:
        return (139, 148, 158)
    try:
        return tuple(int(c[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    except ValueError:
        return (139, 148, 158)


def _gradient(size: tuple[int, int]) -> Image.Image:
    w, h = size
    img = Image.new("RGB", (1, h))
    px = img.load()
    for y in range(h):
        t = y / max(1, h - 1)
        px[0, y] = (
            round(BG_TOP[0] + (BG_BOTTOM[0] - BG_TOP[0]) * t),
            round(BG_TOP[1] + (BG_BOTTOM[1] - BG_TOP[1]) * t),
            round(BG_TOP[2] + (BG_BOTTOM[2] - BG_TOP[2]) * t),
        )
    return img.resize((w, h), Image.BILINEAR)


def _circle_avatar(data: bytes | None, size: int, fallback_letter: str):
    """裁成圆形头像；拿不到图就用首字母占位。"""
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    if data:
        try:
            src = Image.open(io.BytesIO(data)).convert("RGBA")
            src = src.resize((size, size), Image.LANCZOS)
            canvas.paste(src, (0, 0))
        except Exception:
            data = None
    if not data:
        draw = ImageDraw.Draw(canvas)
        draw.ellipse((0, 0, size - 1, size - 1), fill=(33, 38, 45))
        letter = (fallback_letter or "?")[:1].upper()
        font = _load_font(int(size * 0.5), bold=True)
        box = draw.textbbox((0, 0), letter, font=font)
        draw.text(
            ((size - (box[2] - box[0])) / 2 - box[0], (size - (box[3] - box[1])) / 2 - box[1]),
            letter,
            font=font,
            fill=ACCENT,
        )

    # 4 倍超采样蒙版，边缘更顺
    big = size * 4
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, big - 1, big - 1), fill=255)
    mask = mask.resize((size, size), Image.LANCZOS)

    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(canvas, (0, 0), mask)
    return out


def _human_count(n: Any) -> str:
    try:
        v = int(n)
    except (TypeError, ValueError):
        return "0"
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}M".replace(".0M", "M")
    if v >= 10_000:
        return f"{v / 1000:.1f}k".replace(".0k", "k")
    return f"{v:,}"


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _ago(value: str | None) -> str:
    dt = _parse_iso(value)
    if not dt:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - dt
    secs = int(delta.total_seconds())
    if secs < 0:
        return "just now"
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    days = secs // 86400
    if days < 30:
        return f"{days}d ago"
    if days < 365:
        return f"{days // 30}mo ago"
    return f"{days // 365}y ago"


def _license_name(data: dict) -> str:
    lic = data.get("license") or {}
    if not isinstance(lic, dict):
        return "—"
    name = lic.get("spdx_id") or lic.get("key") or ""
    if not name or name == "NOASSERTION":
        return "—"
    return str(name)


def render_card(
    data: dict,
    avatar_bytes: bytes | None = None,
    font_path: str | None = None,
) -> bytes:
    """把仓库数据画成 PNG 卡片，返回图片字节。"""
    img = _gradient((W, H)).convert("RGBA")
    draw = ImageDraw.Draw(img)

    # 外框
    draw.rounded_rectangle(
        (1, 1, W - 2, H - 2), radius=26, outline=BORDER, width=2
    )

    full_name = str(data.get("full_name") or "unknown/repo")
    owner, _, repo = full_name.partition("/")
    if not repo:
        owner, repo = full_name, ""

    # 头像
    avatar = _circle_avatar(avatar_bytes, AVATAR, owner)
    img.alpha_composite(avatar, (PAD, PAD))

    title_font = _load_font(42, bold=True, custom=font_path)
    owner_font = _load_font(42, custom=font_path)
    desc_font = _load_font(21, custom=font_path)
    label_font = _load_font(15, custom=font_path)
    value_font = _load_font(36, bold=True, custom=font_path)
    small_font = _load_font(18, custom=font_path)

    text_x = PAD + AVATAR + 20
    owner_text = _safe_text(owner, owner_font)
    repo_text = _safe_text(repo, title_font)

    draw.text((text_x, PAD + 4), owner_text + " / ", font=owner_font, fill=DIM)
    offset = _text_w(draw, owner_text + " / ", owner_font)
    draw.text((text_x + offset, PAD), repo_text, font=title_font, fill=FG)

    # 归档标记
    if data.get("archived"):
        badge_font = _load_font(15, bold=True, custom=font_path)
        label = "ARCHIVED"
        bw = _text_w(draw, label, badge_font) + 24
        bx = W - PAD - bw
        draw.rounded_rectangle(
            (bx, PAD + 8, bx + bw, PAD + 34), radius=13,
            fill=(60, 30, 30), outline=(120, 60, 60), width=1,
        )
        draw.text((bx + 12, PAD + 12), label, font=badge_font, fill=(255, 160, 160))

    # 描述
    desc = data.get("description") or "No description provided."
    desc = _safe_text(str(desc).strip(), desc_font)
    lines = _wrap(draw, desc, desc_font, W - text_x - PAD, 3)
    y = PAD + 60
    for line in lines:
        draw.text((text_x, y), line, font=desc_font, fill=DIM)
        y += 29

    # 三个大数字
    stats = [
        ("STARS", _human_count(data.get("stargazers_count"))),
        ("FORKS", _human_count(data.get("forks_count"))),
        ("ISSUES", _human_count(data.get("open_issues_count"))),
    ]
    sx = PAD
    for label, value in stats:
        draw.text((sx, 216), label, font=label_font, fill=FAINT)
        draw.text((sx, 236), value, font=value_font, fill=FG)
        sx += 260

    # 分隔线
    draw.line((PAD, 310, W - PAD, 310), fill=DIVIDER, width=2)

    # 底行：语言 / 许可 / 最近更新
    bottom_y = 334
    col2, col3 = PAD + 300, PAD + 600

    draw.text((PAD, bottom_y), "LANGUAGE", font=label_font, fill=FAINT)
    lang = data.get("language")
    if lang:
        color = _hex(LANG_COLORS.get(str(lang).lower(), "#8b949e"))
        draw.ellipse((PAD, bottom_y + 27, PAD + 14, bottom_y + 41), fill=color)
        draw.text(
            (PAD + 22, bottom_y + 20), _safe_text(str(lang), small_font),
            font=small_font, fill=FG,
        )
    else:
        draw.text((PAD, bottom_y + 20), "—", font=small_font, fill=FAINT)

    draw.text((col2, bottom_y), "LICENSE", font=label_font, fill=FAINT)
    draw.text((col2, bottom_y + 20), _license_name(data), font=small_font, fill=FG)

    draw.text((col3, bottom_y), "UPDATED", font=label_font, fill=FAINT)
    draw.text(
        (col3, bottom_y + 20),
        _ago(data.get("pushed_at") or data.get("updated_at")),
        font=small_font,
        fill=FG,
    )

    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()
