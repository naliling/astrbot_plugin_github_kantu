"""GitHub解图 —— 聊天里贴 GitHub 仓库链接自动出卡片，也可以用 /kantu 命令查。

插件自己向 GitHub 拉取仓库数据，再用 Pillow 现场画图，卡片内容
（星标 / 分叉 / 议题 / 语言 / 许可 / 活跃度）完全由插件自己决定。
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import time
from typing import Any

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image
from astrbot.api.star import Context, Star

# 同目录的两个纯函数模块按「文件路径」直接加载，不走普通 import：
# 重载插件时 Python 的 sys.modules 里可能还留着上一版模块，普通 import 会拿到旧代码，
# 表现为「明明改了文件，却报 cannot import name」。
_HERE = os.path.dirname(os.path.abspath(__file__))


def _load_local(name: str):
    """按文件路径加载同目录模块，保证拿到的是磁盘上的最新代码。"""
    mod_name = f"astrbot_plugin_github_kantu_{name}"
    path = os.path.join(_HERE, f"{name}.py")
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载模块：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_repo_parse = _load_local("repo_parse")
_card_render = _load_local("card_render")

cache_key = _repo_parse.cache_key
extract_repos = _repo_parse.extract_repos
parse_repo = _repo_parse.parse_repo
strip_command = _repo_parse.strip_command
render_card = _card_render.render_card

__version__ = "0.1.0"

API_TMPL = "https://api.github.com/repos/{owner}/{repo}"
USER_AGENT = f"astrbot-plugin-github-kantu/{__version__}"

USAGE = (
    "用法：/kantu owner/repo\n"
    "例如：/kantu koishijs/koishi\n"
    "也可以直接贴链接：/kantu https://github.com/koishijs/koishi\n"
    "（聊天里直接贴 GitHub 仓库链接，也会自动出卡片）"
)


class GitHubKantuPlugin(Star):
    def __init__(self, context: Context, config: dict[str, Any] | None = None):
        super().__init__(context)
        cfg = config or {}

        self.token = str(cfg.get("github_token") or "").strip()
        self.proxy = str(cfg.get("proxy") or "").strip() or None
        self.font_path = str(cfg.get("font_path") or "").strip() or None
        self.insecure_tls = bool(cfg.get("insecure_tls", False))
        self.show_avatar = bool(cfg.get("show_avatar", True))

        self.auto_link = bool(cfg.get("auto_link_enabled", True))
        self.suppress_llm = bool(cfg.get("suppress_llm_on_card", False))
        self.max_links = self._int(cfg, "max_links_per_message", 1, 1)

        self.cooldown = self._int(cfg, "cooldown_seconds", 3, 0)
        self.cache_ttl = self._int(cfg, "cache_ttl_seconds", 1800, 0)
        self.timeout = self._int(cfg, "timeout_seconds", 12, 3)

        # cache_key -> (过期时间戳, PNG 字节)
        self._cache: dict[str, tuple[float, bytes]] = {}
        self._last_use: dict[str, float] = {}

    @staticmethod
    def _int(cfg: dict, name: str, default: int, minimum: int) -> int:
        try:
            return max(minimum, int(cfg.get(name, default)))
        except (TypeError, ValueError):
            return default

    async def initialize(self) -> None:
        logger.info(
            f"[github-kantu] v{__version__} 已加载"
            f"（自动识别链接：{'开' if self.auto_link else '关'}"
            f" / 缓存 {self.cache_ttl}s / 冷却 {self.cooldown}s"
            f"{' / 已配置 token' if self.token else ' / 未配置 token'}）"
        )

    async def terminate(self) -> None:
        self._cache.clear()
        self._last_use.clear()
        logger.info("[github-kantu] 已卸载，缓存已清空")

    # ---------------- 网络 ----------------

    def _headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _request_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "timeout": aiohttp.ClientTimeout(total=self.timeout),
            "headers": self._headers(),
            "allow_redirects": True,
        }
        if self.proxy:
            kwargs["proxy"] = self.proxy
        if self.insecure_tls:
            kwargs["ssl"] = False
        return kwargs

    async def _fetch_repo(self, owner: str, repo: str) -> tuple[str, Any]:
        """返回 (状态, 数据)。状态：ok / not_found / rate_limited:<提示> / error。"""
        url = API_TMPL.format(owner=owner, repo=repo)
        async with aiohttp.ClientSession() as sess:
            async with sess.get(url, **self._request_kwargs()) as resp:
                status = resp.status
                if status == 200:
                    try:
                        return "ok", await resp.json(content_type=None)
                    except Exception as exc:
                        logger.error(f"[github-kantu] 解析 JSON 失败：{exc}")
                        return "error", None
                if status == 404:
                    return "not_found", None
                if status in (403, 429):
                    remain = resp.headers.get("x-ratelimit-remaining")
                    reset = resp.headers.get("x-ratelimit-reset")
                    hint = ""
                    if remain == "0" and reset and reset.isdigit():
                        wait = max(0, int(reset) - int(time.time()))
                        hint = f"，约 {wait // 60 + 1} 分钟后恢复"
                    logger.warning(
                        f"[github-kantu] {owner}/{repo} 被限流 "
                        f"(status={status}, remaining={remain})"
                    )
                    return f"rate_limited{hint}", None
                logger.warning(f"[github-kantu] {owner}/{repo} 返回 {status}")
                return "error", None

    async def _fetch_avatar(self, url: str) -> bytes | None:
        if not url or not self.show_avatar:
            return None
        kwargs = {
            "timeout": aiohttp.ClientTimeout(total=min(self.timeout, 8)),
            "headers": {"User-Agent": USER_AGENT},
            "allow_redirects": True,
        }
        if self.proxy:
            kwargs["proxy"] = self.proxy
        if self.insecure_tls:
            kwargs["ssl"] = False
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(url, **kwargs) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        return data or None
        except Exception as exc:
            logger.debug(f"[github-kantu] 头像拉取失败：{exc}")
        return None

    async def _build_card(self, owner: str, repo: str) -> tuple[str, bytes | None]:
        """拿一张卡片（优先用缓存）。返回 (状态, PNG)。"""
        key = cache_key(owner, repo)
        cached = self._cache.get(key)
        if cached and cached[0] > time.time():
            return "ok", cached[1]

        state, data = await self._fetch_repo(owner, repo)
        if state != "ok" or not isinstance(data, dict):
            return state, None

        avatar = await self._fetch_avatar(
            ((data.get("owner") or {}).get("avatar_url") or "")
        )
        png = await asyncio.to_thread(render_card, data, avatar, self.font_path)

        if self.cache_ttl > 0:
            self._cache[key] = (time.time() + self.cache_ttl, png)
            self._prune()
        return "ok", png

    # ---------------- 命令 ----------------

    @filter.command("kantu", alias={"repo", "看图", "仓库"})
    async def kantu(self, event: AstrMessageEvent):
        """查看 GitHub 仓库卡片。用法：/kantu owner/repo"""
        arg = strip_command(self._message_text(event))
        if not arg:
            yield event.plain_result(USAGE)
            return

        parsed = parse_repo(arg)
        if not parsed:
            yield event.plain_result(
                f"这个仓库格式看不懂：{arg}\n应该写成 owner/repo，例如 koishijs/koishi"
            )
            return
        owner, repo = parsed

        if not self._pass_cooldown(event):
            yield event.plain_result(
                f"慢一点，{self._cooldown_left(event):.0f} 秒后再试"
            )
            return

        try:
            state, png = await self._build_card(owner, repo)
        except asyncio.TimeoutError:
            logger.warning(f"[github-kantu] 请求超时：{owner}/{repo}")
            yield event.plain_result("请求 GitHub 超时了，等会儿再试")
            return
        except Exception as exc:
            logger.error(f"[github-kantu] 请求异常：{owner}/{repo} -> {exc}")
            yield event.plain_result("拉取仓库数据失败了，等会儿再试")
            return

        if state == "not_found":
            yield event.plain_result(
                f"没找到 {owner}/{repo}，可能名字打错了，或者是私有仓库"
            )
            return
        if state.startswith("rate_limited"):
            yield event.plain_result(f"GitHub 接口访问太频繁了{state[len('rate_limited'):]}")
            return
        if not png:
            yield event.plain_result("拉取仓库数据失败了，等会儿再试")
            return

        yield event.chain_result([Image.fromBytes(png)])

    # ---------------- 被动识别链接 ----------------

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def auto_card(self, event: AstrMessageEvent):
        """聊天里出现 GitHub 仓库链接就自动出卡片。"""
        text = self._message_text(event)
        if not text:
            return
        # 只有消息里出现 github.com 时才打诊断日志，平时不刷屏
        hint = "github.com" in text.lower()

        if not self.auto_link:
            if hint:
                logger.info("[github-kantu] 自动识别已关闭（auto_link_enabled=false），跳过")
            return

        stripped = text.lstrip()
        # 命令消息交给命令处理器，别重复出图
        if stripped.startswith("/") or stripped.startswith("／"):
            if hint:
                logger.info("[github-kantu] 这是命令消息，交给 /kantu 处理，不重复出图")
            return
        if self._is_self(event):
            if hint:
                logger.info("[github-kantu] 这条消息是机器人自己发的，跳过")
            return

        repos = extract_repos(text, self.max_links)
        if not repos:
            if hint:
                logger.info(
                    f"[github-kantu] 见到 github.com 但没解析出 owner/repo，跳过：{stripped[:80]!r}"
                )
            return

        # 冷却期内静默跳过，不刷提示
        if not self._pass_cooldown(event):
            logger.info(
                f"[github-kantu] 冷却中（还剩 {self._cooldown_left(event):.1f}s），跳过"
            )
            return

        if self.suppress_llm:
            try:
                event.should_call_llm(False)
            except Exception:
                pass

        for owner, repo in repos:
            started = time.monotonic()
            try:
                state, png = await self._build_card(owner, repo)
            except asyncio.TimeoutError:
                logger.warning(f"[github-kantu] 自动卡片超时：{owner}/{repo}")
                continue
            except Exception as exc:
                logger.error(f"[github-kantu] 自动卡片失败：{owner}/{repo} -> {exc}")
                continue
            if state != "ok" or not png:
                logger.info(f"[github-kantu] 自动卡片跳过 {owner}/{repo}：{state}")
                continue
            # 直接用 event.send 发出去：不依赖管道把 handler 的 yield 结果再送一遍，
            # 也避开 result_decorate 给图片加「引用回复」装饰。
            try:
                await event.send(MessageChain(chain=[Image.fromBytes(png)]))
            except Exception as exc:
                logger.error(f"[github-kantu] 发送卡片失败：{owner}/{repo} -> {exc}")
                continue
            logger.info(
                f"[github-kantu] 已发出卡片 {owner}/{repo}"
                f"（{len(png)} 字节，耗时 {time.monotonic() - started:.2f}s）"
            )

    # ---------------- 小工具 ----------------

    def _cooldown_left(self, event: AstrMessageEvent) -> float:
        if self.cooldown <= 0:
            return 0.0
        gap = time.monotonic() - self._last_use.get(self._session_key(event), 0.0)
        return max(0.0, self.cooldown - gap)

    def _pass_cooldown(self, event: AstrMessageEvent) -> bool:
        """冷却过了就记一笔并返回 True。"""
        if self.cooldown > 0 and self._cooldown_left(event) > 0:
            return False
        self._last_use[self._session_key(event)] = time.monotonic()
        return True

    @staticmethod
    def _message_text(event: AstrMessageEvent) -> str:
        try:
            return event.get_message_str() or ""
        except Exception:
            return getattr(event, "message_str", "") or ""

    @staticmethod
    def _session_key(event: AstrMessageEvent) -> str:
        try:
            umo = getattr(event, "unified_msg_origin", None)
            if umo:
                return str(umo)
        except Exception:
            pass
        try:
            return str(event.get_session_id())
        except Exception:
            return "global"

    @staticmethod
    def _is_self(event: AstrMessageEvent) -> bool:
        """机器人自己发的消息不要触发，避免自己喂自己。"""
        try:
            sender = str(event.get_sender_id() or "")
            me = str(event.get_self_id() or "")
            return bool(sender) and sender == me
        except Exception:
            return False

    def _prune(self) -> None:
        if len(self._cache) <= 200:
            return
        now = time.time()
        for k in [k for k, v in self._cache.items() if v[0] <= now]:
            self._cache.pop(k, None)
        overflow = len(self._cache) - 200
        if overflow > 0:
            for k in list(self._cache)[:overflow]:
                self._cache.pop(k, None)
