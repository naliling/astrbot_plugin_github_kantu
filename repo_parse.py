"""纯函数：把用户输入解析成仓库坐标。不依赖 AstrBot，可以单独跑测试。"""

from __future__ import annotations

import re

GITHUB_HOSTS = {"github.com", "www.github.com"}

_URL_RE = re.compile(r"^https?://([^/]+)/(.+)$", re.IGNORECASE)
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_DIGITS_RE = re.compile(r"^\d+$")

# 从一段聊天文本里捞 GitHub 仓库链接
_LINK_RE = re.compile(
    r"https?://(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)",
    re.IGNORECASE,
)

# 命令本身的写法（含别名），用于从整条消息里剥掉命令词
COMMAND_WORDS = {"repo", "kantu", "看图", "仓库"}


def parse_repo(text: str) -> tuple[str, str] | None:
    """解析出 (owner, repo)，认不出来返回 None。

    接受的写法：
        koishijs/koishi
        github.com/koishijs/koishi
        https://github.com/koishijs/koishi
        https://github.com/koishijs/koishi/tree/main
        https://github.com/koishijs/koishi.git
        koishijs/koishi?tab=readme-ov-file
    """
    if not text:
        return None
    raw = text.strip()
    if not raw:
        return None

    s = raw.split()[0]
    s = s.split("?")[0].split("#")[0]

    matched = _URL_RE.match(s)
    if matched:
        if matched.group(1).lower() not in GITHUB_HOSTS:
            return None
        s = matched.group(2)
    elif s.lower().startswith("github.com/"):
        s = s[len("github.com/") :]

    parts = [p for p in s.split("/") if p]
    if len(parts) < 2:
        return None

    owner, repo = parts[0], parts[1]
    if repo.lower().endswith(".git"):
        repo = repo[:-4]
    if not owner or not repo:
        return None
    # 纯数字开头基本是「1/2」这类误触，不当仓库处理
    if _DIGITS_RE.match(owner):
        return None
    if not _NAME_RE.match(f"{owner}/{repo}"):
        return None
    return owner, repo


def cache_key(owner: str, repo: str) -> str:
    """仓库的缓存键，大小写不敏感。"""
    return f"{owner.lower()}/{repo.lower()}"


def extract_repos(text: str, limit: int = 1) -> list[tuple[str, str]]:
    """从一段聊天文本里捞出 GitHub 仓库链接，去重、按出现顺序返回。

    只认完整链接，不认 owner/repo 这种裸写法——聊天里太容易误判。
    """
    if not text or limit <= 0:
        return []
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for matched in _LINK_RE.finditer(text):
        owner, repo = matched.group(1), matched.group(2)
        if repo.lower().endswith(".git"):
            repo = repo[:-4]
        if not owner or not repo or _DIGITS_RE.match(owner):
            continue
        if not _NAME_RE.match(f"{owner}/{repo}"):
            continue
        key = cache_key(owner, repo)
        if key in seen:
            continue
        seen.add(key)
        found.append((owner, repo))
        if len(found) >= limit:
            break
    return found


def strip_command(text: str) -> str:
    """从整条消息文本里剥掉命令词，返回参数部分。"""
    raw = (text or "").strip()
    if not raw:
        return ""
    chunks = raw.split(None, 1)
    head = chunks[0].lstrip("/!！.").lower()
    if head in COMMAND_WORDS:
        return chunks[1].strip() if len(chunks) > 1 else ""
    return raw
