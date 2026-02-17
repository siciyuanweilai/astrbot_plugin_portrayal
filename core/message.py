from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot.api import logger
from .config import PluginConfig


@dataclass
class MessageQueryResult:
    """
    消息查询结果对象
    """
    texts: list[str]
    scanned_messages: int
    from_cache: bool

    @property
    def count(self) -> int:
        return len(self.texts)

    @property
    def is_empty(self) -> bool:
        return not self.texts


class MessageManager:
    """
    带上下文感知的消息管理器
    """

    def __init__(self, config: PluginConfig):
        self.cfg = config.message
        self.per_page_count = 100 

    def clear_cache(self):
        pass

    def _get_sender_name(self, msg_data: dict[str, Any]) -> str:
        """获取消息发送者的最佳显示名称"""
        sender = msg_data.get("sender", {})
        user_id = str(sender.get("user_id", ""))
        
        name = sender.get("card")
        if not name:
            name = sender.get("nickname")
        if not name:
            name = f"用户_{user_id[-4:]}" if user_id else "未知用户"
        return name

    def _extract_text(self, msg_data: dict[str, Any]) -> str:
        """从消息对象中提取纯文本"""
        if "message" in msg_data and isinstance(msg_data["message"], list):
            return "".join(
                seg["data"]["text"] 
                for seg in msg_data["message"] 
                if seg.get("type") == "text"
            ).strip()
        
        if "raw_message" in msg_data:
            return str(msg_data["raw_message"]).strip()
        return ""

    async def get_user_texts(
        self,
        event: AiocqhttpMessageEvent,
        target_id: str,
        *,
        max_rounds: int,
    ) -> MessageQueryResult:
        """
        获取指定用户在群内的历史文本消息（包含上下文）
        """
        group_id = str(event.get_group_id())
        target_id = str(target_id)
        
        all_messages = []
        seen_ids = set() 
        message_seq = 0 

        logger.info(f"开始获取群 {group_id} 消息，目标用户: {target_id}，计划轮数: {max_rounds}")

        # ---------- 1. 分页拉取逻辑 ----------
        for round_idx in range(max_rounds):
            try:
                if round_idx > 0:
                    await asyncio.sleep(0.5)

                params = {
                    "group_id": group_id,
                    "count": self.per_page_count, 
                    "message_seq": message_seq,
                }
                params["reverseOrder"] = True

                result: dict[str, Any] = await event.bot.api.call_action(
                    "get_group_msg_history",
                    **params
                )

                messages = result.get("messages", [])
                if not messages:
                    break
                
                batch_added_count = 0
                cursor_seqs = []
                
                for msg in messages:
                    mid = msg.get("message_id")
                    if mid is not None:
                        mid_int = int(mid)
                        if mid_int in seen_ids:
                            continue
                        seen_ids.add(mid_int)
                        all_messages.append(msg)
                        batch_added_count += 1
                    else:
                        all_messages.append(msg)
                        batch_added_count += 1

                    # --- 2. 收集用于翻页的 seq ---
                    seq = msg.get("message_seq")
                    if seq is None:
                        seq = msg.get("message_id")
                    
                    if seq is not None:
                        cursor_seqs.append(int(seq))
                
                if not cursor_seqs:
                    break 

                min_seq = min(cursor_seqs) 
                
                if min_seq == message_seq and round_idx > 0:
                    break
                
                if batch_added_count == 0 and round_idx > 0:
                    break

                message_seq = min_seq

                if len(all_messages) > max_rounds * self.per_page_count * 1.5:
                    break

            except Exception as e:
                logger.error(f"获取群消息历史失败 (Round {round_idx}): {e}")
                break

        if not all_messages:
            return MessageQueryResult([], 0, False)

        # ---------- 2. 预处理与排序 ----------
        all_messages.sort(key=lambda x: int(x.get("time", 0)))

        valid_entries = []
        context_window = self.cfg.context_num

        # ---------- 3. 提取对话片段 ----------
        for i, msg in enumerate(all_messages):
            sender_info = msg.get("sender", {})
            sender_id = str(sender_info.get("user_id", ""))
            
            if sender_id == target_id:
                raw_text = self._extract_text(msg)
                if not raw_text: 
                    continue
                
                context_lines = []
                start_index = max(0, i - context_window)
                
                for ctx_msg in all_messages[start_index:i]:
                    c_name = self._get_sender_name(ctx_msg)
                    c_text = self._extract_text(ctx_msg)
                    if c_text:
                        if len(c_text) > 50:
                            c_text = c_text[:50] + "..."
                        context_lines.append(f"【{c_name}】: {c_text}")
                
                entry_str = ""
                if context_lines:
                    entry_str += "\n".join(context_lines) + "\n"
                
                entry_str += f"【主角】: {raw_text}"
                
                valid_entries.append(entry_str)

                if len(valid_entries) >= self.cfg.max_msg_count:
                    break

        return MessageQueryResult(
            texts=valid_entries,
            scanned_messages=len(all_messages),
            from_cache=False,
        )
        