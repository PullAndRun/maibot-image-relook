# -*- coding: utf-8 -*-
"""按问题重新看图：暴露给 planner 的 inspect_image 工具。"""

from __future__ import annotations

import base64
import mimetypes
import re
from pathlib import Path
from typing import Any

from maibot_sdk import Field, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType


class PluginSection(PluginConfigBase):
    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="1.0.3", description="配置版本")


class RelookSection(PluginConfigBase):
    lookback_hours: float = Field(default=24.0, description="默认向前查找图片的小时数")
    recent_message_limit: int = Field(default=40, description="拉取最近消息条数上限")
    max_images: int = Field(default=10, description="候选图片数量上限")
    llm_task: str = Field(default="vlm", description="识图使用的模型任务名，默认 vlm")
    temperature: float = Field(default=0.2, description="重看时的温度，偏低更稳")
    max_tokens: int = Field(default=1024, description="重看回答最大 token")


class ImageRelookConfig(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)
    relook: RelookSection = Field(default_factory=RelookSection)


_DATA_URL_RE = re.compile(r"^data:image/(?P<fmt>[a-zA-Z0-9.+-]+);base64,(?P<data>.+)$", re.DOTALL)
_PLUGIN_DIR = Path(__file__).resolve().parent
_IMAGE_EXTS = ("png", "jpeg", "jpg", "webp", "gif", "bmp")


def _looks_like_maibot_root(path: Path) -> bool:
    """用稳定标记判断是否像 MaiBot 根目录。"""
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return (resolved / "bot.py").is_file() and (resolved / "data").is_dir()


def _discover_maibot_roots(*hints: Path | None) -> list[Path]:
    """从多个线索发现可用的 MaiBot 根目录，避免写死 plugins/<name> 层级。"""
    candidates: list[Path] = []
    for hint in hints:
        if hint is None:
            continue
        try:
            current = hint.resolve()
        except OSError:
            continue
        # 既可能是文件也可能是目录
        if current.is_file():
            current = current.parent
        for _ in range(8):
            candidates.append(current)
            if current.parent == current:
                break
            current = current.parent

    # 常见相对布局：插件目录下的 ../../ 或 data/plugins/<id> 的上两级
    for base in list(candidates):
        candidates.extend(
            [
                base,
                base.parent,
                base.parent.parent,
                base.parent.parent.parent,
            ]
        )

    roots: list[Path] = []
    seen: set[str] = set()
    for item in candidates:
        try:
            resolved = item.resolve()
        except OSError:
            continue
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        if _looks_like_maibot_root(resolved):
            roots.append(resolved)
    return roots


def _safe_resolve_under_roots(path: Path, roots: list[Path]) -> Path | None:
    """resolve 后强制校验路径落在允许的根目录内。"""
    try:
        resolved = path.expanduser().resolve(strict=False)
    except OSError:
        return None
    for root in roots:
        try:
            root_resolved = root.resolve(strict=False)
            resolved.relative_to(root_resolved)
        except (OSError, ValueError):
            continue
        if resolved.is_file():
            return resolved
    return None


def _unwrap_capability(result: Any) -> Any:
    if isinstance(result, dict) and result.get("success") is False:
        return result
    return result


def _extract_llm_text(result: Any) -> str:
    if result is None:
        return ""
    if isinstance(result, str):
        return result.strip()
    if not isinstance(result, dict):
        return str(result).strip()
    if result.get("success") is False:
        err = result.get("error") or result.get("message") or "LLM 调用失败"
        return f"[识图失败] {err}"
    for key in ("content", "response", "text", "output"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key in ("data", "result"):
        nested = result.get(key)
        if isinstance(nested, dict):
            text = _extract_llm_text(nested)
            if text:
                return text
        elif isinstance(nested, str) and nested.strip():
            return nested.strip()
    return ""


def _parse_image_payload(raw: Any) -> tuple[str, str] | None:
    """解析消息段里的图片为 (format, base64)。没有字节时返回 None。"""
    if raw is None:
        return None
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        match = _DATA_URL_RE.match(text)
        if match:
            fmt = match.group("fmt").lower().replace("jpg", "jpeg")
            return fmt, match.group("data")
        return "jpeg", text
    if not isinstance(raw, dict):
        return None

    image_format = str(raw.get("image_format") or raw.get("format") or "").strip().lower()
    image_base64 = raw.get("image_base64") or raw.get("base64")
    if isinstance(image_base64, str) and image_base64.strip():
        return (image_format or "jpeg").replace("jpg", "jpeg"), image_base64.strip()

    data = raw.get("data")
    if isinstance(data, str) and data.strip():
        return _parse_image_payload(data)
    if isinstance(data, dict):
        return _parse_image_payload(data)

    url = raw.get("url") or raw.get("image_url")
    if isinstance(url, dict):
        url = url.get("url")
    if isinstance(url, str) and url.strip().startswith("data:image/"):
        return _parse_image_payload(url)
    return None


def _iter_image_components(node: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(node, list):
        for item in node:
            found.extend(_iter_image_components(item))
        return found
    if not isinstance(node, dict):
        return found

    item_type = str(node.get("type") or "").strip().lower()
    if item_type == "image":
        found.append(node)
    elif item_type == "forward":
        found.extend(_iter_image_components(node.get("data")))
    elif item_type == "dict":
        nested = node.get("data")
        if isinstance(nested, dict) and str(nested.get("type") or "").lower() == "image":
            found.append(nested if "data" in nested else node)
        else:
            found.extend(_iter_image_components(nested))

    for key in ("raw_message", "content", "components", "message"):
        if key in node:
            found.extend(_iter_image_components(node[key]))
    return found


def _format_from_path(path: Path) -> str:
    suffix = path.suffix.lower().lstrip(".")
    if suffix == "jpg":
        return "jpeg"
    if suffix in {"png", "jpeg", "webp", "gif", "bmp"}:
        return suffix
    guessed, _ = mimetypes.guess_type(str(path))
    if guessed and guessed.startswith("image/"):
        return guessed.split("/", 1)[1].replace("jpg", "jpeg")
    return "png"


class ImageRelookPlugin(MaiBotPlugin):
    """当首遍图片描述不够用时，按当前问题重新调用 VLM 看图。"""

    config_model = ImageRelookConfig

    def __init__(self) -> None:
        super().__init__()
        self._maibot_roots: list[Path] = []

    async def on_load(self) -> None:
        self._maibot_roots = self._resolve_maibot_roots()
        self.ctx.logger.info(
            "图片重看插件已加载 roots=%s",
            [str(path) for path in self._maibot_roots] or ["<未发现>"],
        )

    async def on_unload(self) -> None:
        self.ctx.logger.info("图片重看插件已卸载")
        self._maibot_roots = []

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        del scope, config_data, version

    def _resolve_maibot_roots(self) -> list[Path]:
        hints: list[Path | None] = [_PLUGIN_DIR]
        paths = getattr(self.ctx, "paths", None)
        if paths is not None:
            hints.append(getattr(paths, "data_dir", None))
            hints.append(getattr(paths, "runtime_dir", None))
        roots = _discover_maibot_roots(*hints)
        if not roots:
            # 最后兜底：插件目录邻近的候选，即使标记不全也记录警告
            self.ctx.logger.warning("未能可靠发现 MaiBot 根目录，将仅依赖消息内嵌图片字节")
        return roots

    def _tool_result(self, name: str, content: str) -> dict[str, str]:
        return {"name": name, "content": content}

    def _read_image_file(self, path: Path) -> tuple[str, str] | None:
        safe = _safe_resolve_under_roots(path, self._maibot_roots)
        if safe is None:
            return None
        try:
            data = safe.read_bytes()
        except OSError as exc:
            self.ctx.logger.warning("读取图片失败 path=%s err=%s", safe, exc)
            return None
        if not data:
            return None
        return _format_from_path(safe), base64.b64encode(data).decode("ascii")

    async def _fetch_recent_messages(self, chat_id: str) -> list[dict[str, Any]]:
        hours = float(self.config.relook.lookback_hours)
        limit = int(self.config.relook.recent_message_limit)
        raw = await self.ctx.call_capability(
            "message.get_recent",
            chat_id=chat_id,
            hours=hours,
            limit=limit,
            limit_mode="latest",
            # Recent-message responses travel through the Runner RPC channel,
            # whose frame limit is 16 MiB.  Including image bytes here can
            # easily exceed that limit (especially when several images are in
            # the lookback window).  Image components still carry their hash;
            # `_resolve_component_image` loads the bytes locally from
            # `data/images` or the Images table when needed.
            include_binary_data=False,
        )
        raw = _unwrap_capability(raw)
        if isinstance(raw, dict):
            if raw.get("success") is False:
                raise RuntimeError(str(raw.get("error") or "获取最近消息失败"))
            messages = raw.get("messages")
            if isinstance(messages, list):
                return [m for m in messages if isinstance(m, dict)]
        if isinstance(raw, list):
            return [m for m in raw if isinstance(m, dict)]
        return []

    async def _load_bytes_by_hash(self, image_hash: str) -> tuple[str, str] | None:
        """用 hash 从 Images 表/磁盘加载图片，返回 (format, base64)。"""
        image_hash = str(image_hash or "").strip()
        if not image_hash:
            return None
        if not self._maibot_roots:
            self._maibot_roots = self._resolve_maibot_roots()

        # 1) 在已发现根目录下按约定路径找
        for root in self._maibot_roots:
            images_dir = root / "data" / "images"
            for ext in _IMAGE_EXTS:
                loaded = self._read_image_file(images_dir / f"{image_hash}.{ext}")
                if loaded:
                    return loaded

        # 2) 查数据库拿 full_path，再做根目录前缀校验
        try:
            result = await self.ctx.db.get(
                model_name="Images",
                filters={"image_hash": image_hash},
                limit=3,
                single_result=False,
            )
        except Exception as exc:
            self.ctx.logger.warning("按 hash 查 Images 失败: %s", exc)
            result = None

        rows: list[Any] = []
        if isinstance(result, dict):
            if result.get("success") is False:
                self.ctx.logger.warning("Images 查询失败: %s", result.get("error"))
            else:
                data = result.get("data", result.get("result", result.get("rows")))
                if isinstance(data, list):
                    rows = data
                elif isinstance(data, dict):
                    rows = [data]
        elif isinstance(result, list):
            rows = result

        for row in rows:
            if not isinstance(row, dict):
                continue
            if row.get("no_file_flag"):
                continue
            full_path = str(row.get("full_path") or "").strip()
            if not full_path:
                continue
            path = Path(full_path)
            if path.is_absolute():
                loaded = self._read_image_file(path)
                if loaded:
                    return loaded
                continue
            for root in self._maibot_roots:
                loaded = self._read_image_file(root / path)
                if loaded:
                    return loaded
        return None

    async def _resolve_component_image(self, comp: dict[str, Any], message_id: str) -> dict[str, Any] | None:
        parsed = _parse_image_payload(comp)
        image_hash = str(comp.get("hash") or "").strip()
        if parsed:
            fmt, b64 = parsed
            if len(b64) >= 64:
                return {
                    "format": fmt,
                    "base64": b64,
                    "hash": image_hash,
                    "message_id": message_id,
                }
        if image_hash:
            loaded = await self._load_bytes_by_hash(image_hash)
            if loaded:
                fmt, b64 = loaded
                return {
                    "format": fmt,
                    "base64": b64,
                    "hash": image_hash,
                    "message_id": message_id,
                }
        return None

    async def _collect_images_newest_first(
        self,
        messages: list[dict[str, Any]],
        *,
        prefer_msg_id: str = "",
    ) -> list[dict[str, Any]]:
        preferred: list[dict[str, Any]] = []
        ordered: list[dict[str, Any]] = []

        for message in messages:
            message_id = str(message.get("message_id") or "").strip()
            comps = _iter_image_components(message.get("raw_message") or message)
            for comp in comps:
                resolved = await self._resolve_component_image(comp, message_id)
                if not resolved:
                    continue
                if prefer_msg_id and message_id == prefer_msg_id:
                    preferred.append(resolved)
                ordered.append(resolved)

        # 消息通常旧->新；反转后 index=1 为最新图
        ordered.reverse()
        preferred.reverse()
        source = preferred or ordered

        deduped: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in source:
            key = item.get("hash") or item["base64"][:64]
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
            if len(deduped) >= int(self.config.relook.max_images):
                break
        return deduped

    async def _ask_vlm(self, question: str, image_format: str, image_base64: str) -> str:
        prompt = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "请根据用户问题直接观察图片并作答。"
                            "只回答问题需要的事实，不要空泛描述整张图。"
                            "如果看不清或图片里没有对应信息，就明确说看不清/没有。"
                            f"\n用户问题：{question}"
                        ),
                    },
                    {
                        "type": "image",
                        "image_format": image_format,
                        "image_base64": image_base64,
                    },
                ],
            }
        ]
        result = await self.ctx.llm.generate(
            prompt=prompt,
            model=str(self.config.relook.llm_task or "vlm"),
            temperature=float(self.config.relook.temperature),
            max_tokens=int(self.config.relook.max_tokens),
        )
        text = _extract_llm_text(result)
        return text or "[识图失败] 模型没有返回有效内容"

    @Tool(
        "inspect_image",
        description=(
            "按当前问题重新查看聊天里的图片。"
            "当首遍图片描述不够、缺失关键细节（数量、文字、颜色、位置、手指/掌纹/人数等），"
            "或用户追问图片细节时必须使用。"
            "不要用猜测代替看图；有图就调用本工具。"
            "参数：question=具体问题；image_index=从最近往前第几张图（从1开始，默认1）；"
            "也可传 msg_id 指定消息。"
        ),
        parameters=[
            ToolParameterInfo(
                name="question",
                param_type=ToolParamType.STRING,
                description="要向图片提出的具体问题，例如：掌纹有几条？图里有几根手指？",
                required=True,
            ),
            ToolParameterInfo(
                name="image_index",
                param_type=ToolParamType.INTEGER,
                description="从最近往前数第几张图，1=最近一张。默认 1。",
                required=False,
            ),
            ToolParameterInfo(
                name="msg_id",
                param_type=ToolParamType.STRING,
                description="可选。优先查看该消息里的图片。",
                required=False,
            ),
        ],
        visibility="visible",
    )
    async def handle_inspect_image(
        self,
        question: str = "",
        image_index: int = 1,
        msg_id: str = "",
        stream_id: str = "",
        chat_id: str = "",
        **kwargs: Any,
    ) -> dict[str, str]:
        tool_name = "inspect_image"
        q = str(question or "").strip()
        if not q:
            return self._tool_result(tool_name, "请提供要看图回答的具体问题")

        session_id = str(chat_id or stream_id or "").strip()
        if not session_id:
            return self._tool_result(tool_name, "缺少会话 ID，无法定位图片")

        prefer_msg_id = str(
            msg_id or kwargs.get("msg_id") or kwargs.get("message_id") or ""
        ).strip()

        # planner 有时会传 index=0
        raw_index = kwargs.get("index", image_index)
        try:
            index = int(raw_index if raw_index is not None else 1)
        except (TypeError, ValueError):
            index = 1
        if index < 1:
            index = 1

        try:
            messages = await self._fetch_recent_messages(session_id)
            images = await self._collect_images_newest_first(
                messages,
                prefer_msg_id=prefer_msg_id,
            )
        except Exception as exc:
            self.ctx.logger.exception("收集最近图片失败")
            return self._tool_result(tool_name, f"获取最近图片失败: {exc}")

        if not images:
            return self._tool_result(
                tool_name,
                f"最近 {self.config.relook.lookback_hours:g} 小时内没有找到可重看的图片"
                + (f"（msg_id={prefer_msg_id}）" if prefer_msg_id else ""),
            )

        if index > len(images):
            return self._tool_result(
                tool_name,
                f"只要到 {len(images)} 张候选图，无法取第 {index} 张。请把 image_index 设为 1~{len(images)}",
            )

        target = images[index - 1]
        try:
            answer = await self._ask_vlm(q, target["format"], target["base64"])
        except Exception as exc:
            self.ctx.logger.exception("重看图片失败")
            return self._tool_result(tool_name, f"重看图片失败: {exc}")

        meta = f"(第{index}张/共{len(images)}张"
        if target.get("hash"):
            meta += f", hash={target['hash'][:12]}"
        if target.get("message_id"):
            meta += f", msg={target['message_id']}"
        meta += ")"
        return self._tool_result(tool_name, f"{meta}\n问题：{q}\n观察结果：{answer}")


def create_plugin() -> ImageRelookPlugin:
    """创建图片重看插件实例。"""
    return ImageRelookPlugin()
