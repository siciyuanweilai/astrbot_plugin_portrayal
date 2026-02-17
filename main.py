import json
import time
from datetime import datetime
from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.components import At
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from .core.config import PluginConfig
from .core.message import MessageManager
from .core.profile_service import UserProfileService
from .core.llm import LLMService
from .core.entry import EntryService

class PortrayalPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg = PluginConfig(config, context)
        self.msg = MessageManager(self.cfg)
        self.profile_service = UserProfileService()
        self.entry_service = EntryService(self.cfg)
        self.llm = LLMService(context, self.cfg)
        self.style = None
        self.history_file = self.cfg.data_dir / "analysis_history.json"

    async def initialize(self):
        """加载插件时调用"""
        try:
            import pillowmd

            self.style = pillowmd.LoadMarkdownStyles(self.cfg.style_dir)
        except Exception as e:
            logger.error(f"无法加载pillowmd样式：{e}")

    async def terminate(self):
        self.msg.clear_cache()

    def _get_history(self) -> dict:
        """读取历史记录"""
        if not self.history_file.exists():
            return {}
        try:
            with open(self.history_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"读取画像历史记录失败: {e}")
            return {}

    def _save_history(self, data: dict):
        """保存历史记录"""
        try:
            self.cfg.data_dir.mkdir(parents=True, exist_ok=True)
            with open(self.history_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存画像历史记录失败: {e}")

    def _check_cooldown(self, target_id: str) -> tuple[bool, str]:
        """
        检查用户是否在冷却中
        配置单位：天
        返回: (是否通过, 提示信息)
        """
        cooldown_days = self.cfg.message.analysis_cooldown
        if cooldown_days <= 0:
            return True, ""
        
        cooldown_seconds = cooldown_days * 24 * 60 * 60
        
        history = self._get_history()
        last_time_str = history.get(str(target_id))
        
        if not last_time_str:
            return True, ""

        try:
            dt_obj = datetime.strptime(last_time_str, "%Y-%m-%d %H:%M:%S")
            last_timestamp = dt_obj.timestamp()
        except ValueError:
            return True, ""

        current_time = time.time()
        
        if current_time - last_timestamp < cooldown_seconds:
            remaining_seconds = int(cooldown_seconds - (current_time - last_timestamp))
            
            d, remainder = divmod(remaining_seconds, 86400)
            h, remainder = divmod(remainder, 3600)
            m, s = divmod(remainder, 60)
            
            time_parts = []
            if d > 0: time_parts.append(f"{d}天")
            if h > 0: time_parts.append(f"{h}小时")
            if m > 0: time_parts.append(f"{m}分")
            
            if not time_parts:
                time_str = f"{s}秒"
            else:
                time_str = "".join(time_parts)

            return False, f"该群友在{cooldown_days}天内已被分析过了，请等待{time_str}后再试。"
            
        return True, ""

    def _update_cooldown(self, target_id: str):
        """更新用户的上次分析时间"""
        if self.cfg.message.analysis_cooldown <= 0:
            return
            
        history = self._get_history()
        history[str(target_id)] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._save_history(history)

    def _get_target_id(self, event: AiocqhttpMessageEvent) -> str | None:
        """
        获取分析目标ID
        逻辑：优先返回被 @ 的其他群友；
        如果没有群友被 @，但 Bot 自己被 @ 了，且配置允许分析Bot，则返回 Bot ID
        """
        self_id = event.get_self_id()
        at_list = [
            str(seg.qq) 
            for seg in event.get_messages() 
            if isinstance(seg, At)
        ]
        
        # 优先返回非 Bot 的 QQ
        for qq in at_list:
            if qq != self_id:
                return qq
        
        # 如果配置允许，且列表中包含 Bot ID，则返回 Bot ID
        if self.cfg.message.allow_analyze_self and self_id in at_list:
            return self_id
            
        return None

    async def send(self, event: AiocqhttpMessageEvent, message: str):
        if self.style:
            img = await self.style.AioRender(text=message, useImageUrl=True)
            img_path = img.Save(self.cfg.cache_dir)
            await event.send(event.image_result(str(img_path))) 
        else:
            await event.send(event.plain_result(message)) 
        event.stop_event()

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def get_portrayal(self, event: AiocqhttpMessageEvent):
        """
        画像 @群友 <查询轮数>
        """
        cmd = event.message_str.partition(" ")[0]
        prompt = self.entry_service.match_prompt_by_cmd(cmd)
        if not prompt:
            return
            
        # 获取目标ID
        target_id = self._get_target_id(event)
        
        # 如果没有指定人，也没有触发 Bot 分析，则默认分析发送者
        if not target_id:
            target_id = event.get_sender_id()

        # ---------- 检查冷却 ----------
        can_proceed, msg = self._check_cooldown(target_id)
        if not can_proceed:
            yield event.plain_result(msg)
            return

        # ---------- 用户画像 ----------
        profile = await self.profile_service.get_profile(event, target_id)

        # ---------- 查询轮数 ----------
        end_param = event.message_str.split(" ")[-1]
        query_rounds = self.cfg.message.get_query_rounds(end_param)

        yield event.plain_result(
            f"正在发起{query_rounds}轮查询来获取{profile.nickname}的聊天记录(含上下文)..."
        )

        # ---------- 消息 ----------
        result = await self.msg.get_user_texts(
            event,
            profile.user_id,
            max_rounds=query_rounds,
        )

        if result.is_empty:
            yield event.plain_result("没有查询到该群友的任何消息")
            return

        self._update_cooldown(target_id)

        yield event.plain_result(
            f"已查找到{result.scanned_messages}条群消息，提取到"
            f"{result.count}组{profile.nickname}的对话片段，正在分析..."
        )

        # ---------- LLM ----------
        try:
            content = await self.llm.generate_portrait(result.texts, profile, prompt)
        except Exception as e:
            logger.error(f"LLM 调用失败：{e}")
            yield event.plain_result(f"分析失败：{e}")

        # ---------- 发送 ----------
        await self.send(event, content)

    @filter.command("画像提示词", alias={"查看画像提示词"})
    async def get_prompt(
        self,
        event: AiocqhttpMessageEvent,
        command: str | None = None,
    ):
        """
        查看画像提示词 <命令>
        """
        text = self.entry_service.view_entry(command)
        if not text:
            yield event.plain_result(f"提示词【{command}】不存在")
            return
        await self.send(event, text)
        