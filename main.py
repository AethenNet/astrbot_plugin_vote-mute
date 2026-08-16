"""
AstrBot 投票禁言插件
插件名称: astrbot_plugin_vote-mute
作者: SummerDew
功能: QQ 群内通过投票方式禁言指定用户，支持多群独立配置、管理员介入、冷却时间等
"""
import asyncio
import time

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.message.components import At
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

PLUGIN_NAME = "astrbot_plugin_vote-mute"


class VoteMutePlugin(Star):
    """QQ 群投票禁言插件"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config

        # 每个群进行中的投票: {group_id: vote_data}
        self.active_votes: dict[str, dict] = {}
        # 每个群的冷却结束时间戳: {group_id: timestamp}
        self.cooldowns: dict[str, float] = {}
        # 投票倒计时任务: {group_id: asyncio.Task}
        self.vote_tasks: dict[str, asyncio.Task] = {}

        logger.info(f"[{PLUGIN_NAME}] 投票禁言插件已加载")

    # ============================================================
    # 配置相关
    # ============================================================
    def get_group_config(self, group_id: str) -> dict:
        """获取指定群的配置，没有单独配置则使用默认配置"""
        group_configs = self.config.get("group_configs", []) or []
        for gc in group_configs:
            if str(gc.get("group_id", "")).strip() == str(group_id).strip():
                return gc
        return self.config.get("default_config", {}) or {}

    def require_start_vote_prefix(self) -> bool:
        """发起投票禁言是否需要 '/' 前缀"""
        return bool(self.config.get("start_vote_prefix", True))

    def require_vote_action_prefix(self) -> bool:
        """同意/反对禁言是否需要 '/' 前缀"""
        return bool(self.config.get("vote_action_prefix", True))

    # ============================================================
    # 工具方法
    # ============================================================
    async def get_user_role(self, bot, group_id: str, user_id: str) -> str:
        """
        获取用户在群中的角色: owner / admin / member
        通过 OneBot API 查询群成员信息
        """
        try:
            info = await bot.get_group_member_info(
                group_id=int(group_id), user_id=int(user_id), no_cache=True
            )
            return info.get("role", "member")
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 获取用户角色失败: {e}")
            return "member"

    async def get_user_name(self, bot, group_id: str, user_id: str) -> str:
        """获取用户群昵称（优先群名片，其次昵称，最后 QQ 号）"""
        try:
            info = await bot.get_group_member_info(
                group_id=int(group_id), user_id=int(user_id), no_cache=True
            )
            return info.get("card") or info.get("nickname") or str(user_id)
        except Exception:
            return str(user_id)

    async def check_bot_admin(self, event: AiocqhttpMessageEvent) -> bool:
        """检查机器人自身是否有群管理员权限（群主或管理员）"""
        role = await self.get_user_role(
            event.bot, event.get_group_id(), event.get_self_id()
        )
        return role in ("owner", "admin")

    @staticmethod
    def is_admin_or_owner(role: str) -> bool:
        """判断是否为群主或管理员"""
        return role in ("owner", "admin")

    def extract_target_and_minutes(self, event: AiocqhttpMessageEvent):
        """
        从消息中提取目标用户 QQ 号和禁言分钟数。
        优先从 @ 组件获取目标用户，其次从文本数字中解析。
        返回: (target_user: str | None, minutes: int | None)
        """
        target = None
        minutes = None

        # 1. 从 At 消息组件提取被 @ 用户
        for seg in event.message_obj.message:
            if isinstance(seg, At) and str(seg.qq) != event.get_self_id():
                target = str(seg.qq)
                break

        # 2. 从纯文本中解析参数
        text = event.message_str.strip()
        # 移除指令前缀（/投票禁言 或 投票禁言）
        if text.startswith("/"):
            text = text[1:].strip()
        for cmd in ("投票禁言",):
            if text.startswith(cmd):
                text = text[len(cmd):].strip()
                break

        parts = text.split()
        for part in parts:
            if not part.isdigit():
                continue
            num = int(part)
            if target is None:
                # 还没有目标用户时，长数字视为 QQ 号，短数字视为分钟数
                if len(part) >= 5:
                    target = part
                else:
                    minutes = num
            else:
                # 已有目标用户（来自 @ 或之前的长数字），跳过与目标相同的 QQ 号
                if part == target:
                    continue
                # 剩余数字中，短数字视为分钟数；长数字视为另一个 QQ 号则忽略
                if minutes is None and len(part) < 5:
                    minutes = num

        return target, minutes

    @staticmethod
    def format_cooldown(remaining: float) -> str:
        """格式化冷却剩余时间"""
        mins = int(remaining // 60)
        secs = int(remaining % 60)
        if mins > 0:
            return f"{mins}分{secs}秒"
        return f"{secs}秒"

    def format_group_config(self, cfg: dict, group_id: str) -> str:
        """格式化群配置信息用于展示"""
        max_p = cfg.get("max_participants", 5)
        duration = cfg.get("vote_duration", 60)
        admin_ban = "开启" if cfg.get("admin_intervene_ban", False) else "关闭"
        admin_cancel = "开启" if cfg.get("admin_intervene_cancel", False) else "关闭"
        ban_timeout = "开启" if cfg.get("ban_on_timeout", False) else "关闭"
        cooldown = cfg.get("cooldown", 0)
        cooldown_text = f"{cooldown}秒" if cooldown > 0 else "无冷却"

        return (
            f"【本群投票禁言配置】\n"
            f"群号：{group_id}\n"
            f"参与人数上限：{max_p} 人\n"
            f"投票时长：{duration} 秒\n"
            f"管理员介入直接禁言：{admin_ban}\n"
            f"管理员介入直接取消：{admin_cancel}\n"
            f"超时未达上限直接禁言：{ban_timeout}\n"
            f"冷却时间：{cooldown_text}"
        )

    # ============================================================
    # 核心逻辑
    # ============================================================
    async def _execute_ban(self, bot, group_id: str, user_id: str, minutes: int):
        """调用 OneBot API 执行禁言，支持多种调用方式"""
        duration = minutes * 60
        # 方式1: 直接调用 set_group_ban
        try:
            await bot.set_group_ban(
                group_id=int(group_id), user_id=int(user_id), duration=duration
            )
            return
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] set_group_ban失败(方式1): {e}")
        # 方式2: 通过 call_api 调用
        try:
            await bot.call_api(
                "set_group_ban",
                group_id=int(group_id),
                user_id=int(user_id),
                duration=duration,
            )
            return
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] set_group_ban所有方式均失败: {e}")
            raise

    async def _send_group_msg(self, bot, group_id: str, message: str, event=None):
        """
        主动发送群消息，支持多种发送方式回退。
        优先使用 event.send(event.plain_result())（AstrBot 推荐方式，兼容性最好），
        再回退到 bot 的各种 API 调用方式。
        """
        # 方式0: 使用 event.send()（参考 AstrBot 官方插件的标准做法，兼容性最好）
        if event is not None:
            try:
                await event.send(event.plain_result(message))
                logger.info(f"[{PLUGIN_NAME}] 群消息已发送(方式0:event.send): {message[:40]}")
                return
            except Exception as e:
                logger.warning(f"[{PLUGIN_NAME}] event.send失败(方式0): {e}")
        # 方式1: 直接调用 send_group_msg（OneBot 标准 API）
        try:
            await bot.send_group_msg(group_id=int(group_id), message=message)
            logger.info(f"[{PLUGIN_NAME}] 群消息已发送(方式1): {message[:40]}")
            return
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] send_group_msg失败(方式1): {e}")

        # 方式2: 通过 call_api 调用 OneBot 标准接口
        try:
            await bot.call_api(
                "send_group_msg", group_id=int(group_id), message=message
            )
            logger.info(f"[{PLUGIN_NAME}] 群消息已发送(方式2): {message[:40]}")
            return
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] call_api失败(方式2): {e}")

        # 方式3: 使用消息链对象发送（部分 AstrBot 版本要求）
        try:
            from astrbot.core.message.components import Plain

            msg_chain = [Plain(message)]
            await bot.send_group_msg(group_id=int(group_id), message=msg_chain)
            logger.info(f"[{PLUGIN_NAME}] 群消息已发送(方式3): {message[:40]}")
            return
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 消息链发送失败(方式3): {e}")

        # 方式4: 通过 call_api 发送消息链
        try:
            from astrbot.core.message.components import Plain

            msg_chain = [Plain(message)]
            await bot.call_api(
                "send_group_msg", group_id=int(group_id), message=msg_chain
            )
            logger.info(f"[{PLUGIN_NAME}] 群消息已发送(方式4): {message[:40]}")
            return
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 所有群消息发送方式均失败: {e}")

    async def _settle_vote(self, group_id: str, reason: str = "timeout"):
        """
        结算投票。
        reason: timeout(超时) / agree_max(同意达上限) / admin_ban(管理员介入禁言) / admin_cancel(管理员介入取消)
        """
        logger.info(f"[{PLUGIN_NAME}] 群 {group_id} 开始结算投票，原因: {reason}")
        vote_data = self.active_votes.pop(group_id, None)
        if not vote_data:
            logger.warning(f"[{PLUGIN_NAME}] 群 {group_id} 无进行中的投票，结算中止")
            return

        # 取消倒计时任务
        task = self.vote_tasks.pop(group_id, None)
        if task and not task.done():
            task.cancel()

        bot = vote_data["bot"]
        event = vote_data.get("event")
        cfg = self.get_group_config(group_id)
        target_user = vote_data["target_user"]
        target_name = vote_data["target_name"]
        minutes = vote_data["minutes"]

        should_ban = False
        result_msg = ""

        if reason == "admin_ban":
            should_ban = True
            result_msg = "管理员介入，禁言已立即生效"
        elif reason == "admin_cancel":
            should_ban = False
            result_msg = "管理员介入，禁言已强制取消"
        elif reason == "agree_max":
            should_ban = True
            result_msg = "投票禁言已生效"
        elif reason == "timeout":
            if cfg.get("ban_on_timeout", False):
                should_ban = True
                result_msg = "投票禁言已生效"
            else:
                result_msg = "投票时间结束，参与同意禁言数不足"

        logger.info(
            f"[{PLUGIN_NAME}] 群 {group_id} 结算结果: should_ban={should_ban}, msg={result_msg}"
        )

        # 执行禁言
        if should_ban:
            try:
                await self._execute_ban(bot, group_id, target_user, minutes)
                logger.info(
                    f"[{PLUGIN_NAME}] 群 {group_id} 已禁言用户 {target_user}({target_name}) {minutes}分钟"
                )
            except Exception as e:
                logger.error(f"[{PLUGIN_NAME}] 禁言执行失败: {e}")
                result_msg += "\n（禁言执行失败，请检查机器人权限）"

        # 设置冷却时间
        cooldown = int(cfg.get("cooldown", 0))
        if cooldown > 0:
            self.cooldowns[group_id] = time.time() + cooldown
            logger.info(f"[{PLUGIN_NAME}] 群 {group_id} 已设置冷却 {cooldown} 秒")

        # 发送结算结果
        logger.info(f"[{PLUGIN_NAME}] 群 {group_id} 准备发送结算消息: {result_msg}")
        await self._send_group_msg(bot, group_id, result_msg, event=event)
        logger.info(f"[{PLUGIN_NAME}] 群 {group_id} 结算完成")

    async def _vote_countdown(self, group_id: str, duration: int):
        """
        投票倒计时协程，时间到后自动结算。
        包含完整的异常捕获，避免因异常导致静默失败。
        """
        logger.info(
            f"[{PLUGIN_NAME}] 群 {group_id} 投票倒计时启动，时长 {duration} 秒"
        )
        try:
            await asyncio.sleep(duration)
            logger.info(f"[{PLUGIN_NAME}] 群 {group_id} 投票倒计时结束，准备结算")
            if group_id in self.active_votes:
                await self._settle_vote(group_id, reason="timeout")
            else:
                logger.info(
                    f"[{PLUGIN_NAME}] 群 {group_id} 投票已提前结算，跳过超时处理"
                )
        except asyncio.CancelledError:
            logger.info(f"[{PLUGIN_NAME}] 群 {group_id} 投票倒计时被取消")
            raise
        except Exception as e:
            logger.error(
                f"[{PLUGIN_NAME}] 群 {group_id} 投票倒计时异常: {e}", exc_info=True
            )
            # 尝试发送错误通知
            try:
                if group_id in self.active_votes:
                    vote_data = self.active_votes.get(group_id)
                    if vote_data:
                        bot = vote_data.get("bot")
                        event = vote_data.get("event")
                        if bot:
                            await self._send_group_msg(
                                bot, group_id, "投票结算异常，请检查日志", event=event
                            )
            except Exception:
                pass

    async def handle_start_vote(self, event: AiocqhttpMessageEvent):
        """处理发起投票禁言"""
        group_id = event.get_group_id()
        sender_id = event.get_sender_id()

        # 检查机器人是否有群管理员权限
        if not await self.check_bot_admin(event):
            yield event.plain_result("无群管理员权限")
            return

        # 提取目标用户和禁言时长
        target, minutes = self.extract_target_and_minutes(event)
        if not target:
            yield event.plain_result(
                "请 @ 用户或输入 QQ 号\n用法：投票禁言 <@用户/QQ号> <分钟>"
            )
            return
        if not minutes or minutes <= 0:
            yield event.plain_result(
                "请设置有效的禁言时长（分钟）\n用法：投票禁言 <@用户/QQ号> <分钟>"
            )
            return

        # 检查是否已有进行中的投票
        if group_id in self.active_votes:
            vote_data = self.active_votes[group_id]
            yield event.plain_result(
                f"当前已有对 {vote_data['target_name']} 的投票禁言进行中"
            )
            return

        # 获取群配置和发送者角色
        cfg = self.get_group_config(group_id)
        cooldown = int(cfg.get("cooldown", 0))
        sender_role = await self.get_user_role(event.bot, group_id, sender_id)

        # 检查冷却（管理员/群主无视冷却）
        if cooldown > 0 and group_id in self.cooldowns:
            remaining = self.cooldowns[group_id] - time.time()
            if remaining > 0 and not self.is_admin_or_owner(sender_role):
                yield event.plain_result(
                    f"⌛投票正在冷却中… {self.format_cooldown(remaining)}"
                )
                return

        # 管理员/群主发起投票时立即清零冷却
        if self.is_admin_or_owner(sender_role):
            self.cooldowns.pop(group_id, None)

        # 获取目标用户名称
        target_name = await self.get_user_name(event.bot, group_id, target)
        vote_duration = int(cfg.get("vote_duration", 60))
        max_participants = int(cfg.get("max_participants", 5))

        # 创建投票数据（发起人默认投同意票）
        vote_data = {
            "target_user": target,
            "target_name": target_name,
            "minutes": minutes,
            "initiator": sender_id,
            "agree": {sender_id},
            "disagree": set(),
            "start_time": time.time(),
            "duration": vote_duration,
            "max_participants": max_participants,
            "group_id": group_id,
            "bot": event.bot,
            "event": event,
        }
        self.active_votes[group_id] = vote_data

        # ===== 定时结算逻辑（参考 AstrBot 插件标准写法，闭包捕获 event）=====
        async def _timeout_settle():
            """投票超时后自动结算，直接使用发起投票时的 event 发送消息"""
            try:
                await asyncio.sleep(vote_duration)
            except asyncio.CancelledError:
                logger.info(f"[{PLUGIN_NAME}] 群 {group_id} 超时结算任务被取消")
                return

            record = self.active_votes.get(group_id)
            if not record:
                return  # 已被提前结算（同意达上限或管理员介入）

            # 清理投票记录和任务引用
            self.active_votes.pop(group_id, None)
            self.vote_tasks.pop(group_id, None)

            cfg = self.get_group_config(group_id)
            should_ban = cfg.get("ban_on_timeout", False)

            if should_ban:
                # 超时直接禁言
                try:
                    await self._execute_ban(event.bot, group_id, target, minutes)
                    logger.info(
                        f"[{PLUGIN_NAME}] 群 {group_id} 超时已禁言用户 {target}({target_name}) {minutes}分钟"
                    )
                    await event.send(event.plain_result("投票禁言已生效"))
                except Exception as e:
                    logger.error(f"[{PLUGIN_NAME}] 群 {group_id} 超时禁言执行失败: {e}")
                    await event.send(
                        event.plain_result("投票禁言已生效\n（禁言执行失败，请检查机器人权限）")
                    )
            else:
                # 超时未达上限，告知结果
                await event.send(event.plain_result("投票时间结束，参与同意禁言数不足"))

            # 设置冷却时间
            cooldown = int(cfg.get("cooldown", 0))
            if cooldown > 0:
                self.cooldowns[group_id] = time.time() + cooldown
                logger.info(f"[{PLUGIN_NAME}] 群 {group_id} 已设置冷却 {cooldown} 秒")

        # 启动超时结算任务（与参考文件一致的 asyncio.create_task 方式）
        task = asyncio.create_task(_timeout_settle())

        def _on_task_done(t: asyncio.Task):
            if t.cancelled():
                return
            exc = t.exception()
            if exc:
                logger.error(
                    f"[{PLUGIN_NAME}] 群 {group_id} 超时结算任务异常: {exc}",
                    exc_info=exc,
                )

        task.add_done_callback(_on_task_done)
        self.vote_tasks[group_id] = task

        logger.info(
            f"[{PLUGIN_NAME}] 群 {group_id} 投票已创建，目标={target_name}，时长={minutes}分钟"
        )

        # 发送投票发起消息
        yield event.plain_result(
            f"已发起对 {target_name} 的禁言 {minutes}分钟投票\n"
            f"输入“同意禁言 / 反对禁言”参与投票，{vote_duration} 秒后结算"
        )

    async def handle_vote(self, event: AiocqhttpMessageEvent, agree: bool):
        """处理投票（同意/反对）"""
        group_id = event.get_group_id()
        sender_id = event.get_sender_id()

        # 没有进行中的投票则忽略
        if group_id not in self.active_votes:
            return

        vote_data = self.active_votes[group_id]
        cfg = self.get_group_config(group_id)
        sender_role = await self.get_user_role(event.bot, group_id, sender_id)

        # 管理员/群主介入（仅管理员/群主可触发，普通群员不生效）
        if self.is_admin_or_owner(sender_role):
            if agree and cfg.get("admin_intervene_ban", False):
                await self._settle_vote(group_id, reason="admin_ban")
                return
            if not agree and cfg.get("admin_intervene_cancel", False):
                await self._settle_vote(group_id, reason="admin_cancel")
                return

        # 记录投票（支持切换立场）
        if agree:
            vote_data["agree"].add(sender_id)
            vote_data["disagree"].discard(sender_id)
        else:
            vote_data["disagree"].add(sender_id)
            vote_data["agree"].discard(sender_id)

        agree_count = len(vote_data["agree"])
        disagree_count = len(vote_data["disagree"])
        max_participants = vote_data["max_participants"]

        elapsed = time.time() - vote_data["start_time"]
        remaining = max(0, int(vote_data["duration"] - elapsed))

        # 发送投票状态
        yield event.plain_result(
            f"禁言【{vote_data['target_name']}】：\n"
            f"同意 ({agree_count}/{max_participants})\n"
            f"反对 ({disagree_count}/{max_participants})\n"
            f"⏱️剩余 {remaining} 秒"
        )

        # 检查同意数是否达到上限（达到则立即结算，不额外回复状态）
        if agree_count >= max_participants:
            await self._settle_vote(group_id, reason="agree_max")

    async def handle_help(self, event: AiocqhttpMessageEvent):
        """处理投票禁言帮助指令"""
        start_prefix = "/" if self.require_start_vote_prefix() else ""
        action_prefix = "/" if self.require_vote_action_prefix() else ""

        help_text = (
            "【投票禁言插件使用帮助】\n"
            f"1. {start_prefix}投票禁言 <@用户/QQ号> <分钟> — 发起对指定用户的禁言投票\n"
            f"2. {action_prefix}同意禁言 — 投同意票\n"
            f"3. {action_prefix}反对禁言 — 投反对票\n"
            f"4. {start_prefix}投票禁言帮助 — 查看本帮助\n"
            f"5. {start_prefix}本群投票禁言配置 — 查看当前群的配置\n"
            "注意事项：\n"
            "• 机器人需要有群管理员权限才能执行禁言\n"
            "• 发起人默认投同意票\n"
            "• 同意人数达到上限时投票立即通过\n"
            "• 群管理员/群主可配置介入直接禁言或取消\n"
            "• 可在插件设置中配置各群独立参数"
        )
        yield event.plain_result(help_text)

    async def handle_group_config(self, event: AiocqhttpMessageEvent):
        """处理本群投票禁言配置查询指令"""
        group_id = event.get_group_id()
        cfg = self.get_group_config(group_id)
        yield event.plain_result(self.format_group_config(cfg, group_id))

    # ============================================================
    # 指令注册（需要 / 前缀时由这些指令处理）
    # ============================================================
    @filter.command("投票禁言")
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def cmd_start_vote(self, event: AiocqhttpMessageEvent):
        """投票禁言 <@用户/QQ号> <分钟>，发起群投票禁言"""
        if not self.require_start_vote_prefix():
            return
        async for msg in self.handle_start_vote(event):
            yield msg

    @filter.command("同意禁言")
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def cmd_agree(self, event: AiocqhttpMessageEvent):
        """同意当前投票禁言"""
        if not self.require_vote_action_prefix():
            return
        async for msg in self.handle_vote(event, agree=True):
            yield msg

    @filter.command("反对禁言")
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def cmd_disagree(self, event: AiocqhttpMessageEvent):
        """反对当前投票禁言"""
        if not self.require_vote_action_prefix():
            return
        async for msg in self.handle_vote(event, agree=False):
            yield msg

    @filter.command("投票禁言帮助")
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def cmd_help(self, event: AiocqhttpMessageEvent):
        """查看投票禁言插件使用帮助"""
        if not self.require_start_vote_prefix():
            return
        async for msg in self.handle_help(event):
            yield msg

    @filter.command("本群投票禁言配置")
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def cmd_group_config(self, event: AiocqhttpMessageEvent):
        """查看当前群的投票禁言配置"""
        if not self.require_start_vote_prefix():
            return
        async for msg in self.handle_group_config(event):
            yield msg

    # ============================================================
    # 事件监听器（不需要 / 前缀时由此处理）
    # ============================================================
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AiocqhttpMessageEvent):
        """
        监听所有群消息，在无前缀模式下解析指令。
        分别处理发起投票和投票动作的前缀开关。
        需要前缀时，指令由 @filter.command 处理，此监听器跳过。
        """
        text = event.message_str.strip()
        if text.startswith("/"):
            text = text[1:].strip()

        need_start_prefix = self.require_start_vote_prefix()
        need_action_prefix = self.require_vote_action_prefix()

        # 发起投票类指令（投票禁言、投票禁言帮助、本群投票禁言配置）
        if text == "投票禁言帮助":
            if need_start_prefix:
                return  # 需要前缀时由 @filter.command 处理
            async for msg in self.handle_help(event):
                yield msg
            return

        if text == "本群投票禁言配置":
            if need_start_prefix:
                return
            async for msg in self.handle_group_config(event):
                yield msg
            return

        if text.startswith("投票禁言"):
            if need_start_prefix:
                return
            async for msg in self.handle_start_vote(event):
                yield msg
            return

        # 投票动作类指令（同意禁言、反对禁言）
        if text == "同意禁言":
            if need_action_prefix:
                return
            async for msg in self.handle_vote(event, agree=True):
                yield msg
        elif text == "反对禁言":
            if need_action_prefix:
                return
            async for msg in self.handle_vote(event, agree=False):
                yield msg

    # ============================================================
    # 生命周期
    # ============================================================
    async def terminate(self):
        """插件卸载/禁用时清理资源"""
        for task in self.vote_tasks.values():
            if not task.done():
                task.cancel()
        self.active_votes.clear()
        self.vote_tasks.clear()
        logger.info(f"[{PLUGIN_NAME}] 投票禁言插件已卸载")
