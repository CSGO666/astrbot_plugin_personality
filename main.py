import json
import os
import asyncio
import logging
from collections import defaultdict
from astrbot.api.event import filter, AstrMessageEvent # type: ignore
from astrbot.api.star import Context, Star, register # type: ignore

logger = logging.getLogger(__name__)

# ==================== 常量 ====================
TEST_LIST_PASSWORD = os.getenv("TEST_LIST_PASSWORD", "123456")
TEST_SWITCH_PASSWORD = os.getenv("TEST_SWITCH_PASSWORD", "654321")
TEST_MOVE_PASSWORD = os.getenv("TEST_MOVE_PASSWORD", TEST_LIST_PASSWORD)
LIST_AUTH_KEY = "_list_test"
ALL_AUTH_KEY = "_all"
ADMIN_QQS_ENV = os.getenv("PERSONA_ADMINS", "")


@register("astrbot_plugin_personality", "CSGO666", "自定义人设切换插件（群内独立版）", version="1.0.0")
class CustomPersona(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.pm = context.persona_manager
        self.cm = context.conversation_manager
        self.data_dir = os.path.dirname(os.path.abspath(__file__))

        # 用户级授权
        self.auth_file = os.path.join(self.data_dir, "persona_auth.json")
        self.authorized = self._load_json(self.auth_file, {})

        # 公开人设（显示在 /persona list，无密码）
        # 不在列表里的人设自动视为"有密码"
        self.public_file = os.path.join(self.data_dir, "persona_public_list.json")
        self.public_personas = list(self._load_json(self.public_file, []))

        # 迁移：删除已废弃的 persona_test_list.json
        legacy_test = os.path.join(self.data_dir, "persona_test_list.json")
        if os.path.exists(legacy_test):
            try:
                os.remove(legacy_test)
                logger.info("已删除已废弃的 persona_test_list.json")
            except Exception:
                logger.exception("删除 persona_test_list.json 失败")

        # 管理员
        self.admins_file = os.path.join(self.data_dir, "persona_admins.json")
        self.env_admins = {x.strip() for x in ADMIN_QQS_ENV.split(",") if x.strip()}
        self.file_admins = set(self._load_json(self.admins_file, []))
        self.admins = self.env_admins | self.file_admins

        # 群聊内每人独立人设：{群聊umo:qq: conversation_id}
        self.group_conv_file = os.path.join(self.data_dir, "persona_group_conversations.json")
        self.group_convs = self._load_json(self.group_conv_file, {})

        # 私聊期望人格：{私聊umo: persona_id}
        self.private_intent_file = os.path.join(self.data_dir, "persona_private_intent.json")
        self.private_intent = self._load_json(self.private_intent_file, {})

        # 按群 umo 的并发锁
        self._group_locks = defaultdict(asyncio.Lock)

    # ==================== 通用 IO ====================
    def _load_json(self, path, default):
        if not os.path.exists(path):
            return default
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            logger.exception("读取 %s 失败", path)
            return default

    def _save_json(self, path, data):
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            logger.exception("写入 %s 失败", path)

    def _save_authorized(self):
        self._save_json(self.auth_file, self.authorized)

    def _save_public(self):
        self._save_json(self.public_file, self.public_personas)

    def _save_admins(self):
        self._save_json(self.admins_file, sorted(self.file_admins))

    def _save_group_convs(self):
        self._save_json(self.group_conv_file, self.group_convs)

    def _save_private_intent(self):
        self._save_json(self.private_intent_file, self.private_intent)

    # ==================== 事件上下文 ====================
    def _get_qq_id(self, event):
        try:
            return str(event.message_obj.sender.user_id)
        except Exception:
            return event.unified_msg_origin.split(":")[-1]

    def _get_session_type(self, event):
        umo = getattr(event, "unified_msg_origin", "") or ""
        parts = umo.split(":")
        if len(parts) >= 2:
            t = parts[1].lower()
            if "group" in t:
                return "群聊"
            if any(k in t for k in ("friend", "private", "c2c", "direct")):
                return "私聊"
        return "会话"

    def _is_group(self, event):
        return self._get_session_type(event) == "群聊"

    # ==================== 群聊独立会话管理 ====================
    def _group_conv_key(self, event):
        return f"{event.unified_msg_origin}:{self._get_qq_id(event)}"

    def _get_group_lock(self, umo):
        return self._group_locks[umo]

    async def _get_or_create_group_conv(self, event, persona_id):
        key = self._group_conv_key(event)
        existing_cid = self.group_convs.get(key)

        if existing_cid:
            try:
                conv = await self._fetch_conversation(event.unified_msg_origin, existing_cid)
                if conv is not None:
                    if persona_id and getattr(conv, "persona_id", None) != persona_id:
                        await self._update_persona(event.unified_msg_origin, persona_id, existing_cid)
                    return existing_cid
            except Exception:
                logger.exception("验证已有群聊 conversation 失败，将创建新对话")

        cid = await self.cm.new_conversation(
            event.unified_msg_origin,
            persona_id=persona_id,
        )
        if not cid:
            return None

        self.group_convs[key] = cid
        self._save_group_convs()
        return cid

    async def _switch_to_group_conv(self, event):
        key = self._group_conv_key(event)
        cid = self.group_convs.get(key)
        if not cid:
            return
        lock = self._get_group_lock(event.unified_msg_origin)
        async with lock:
            try:
                await self.cm.switch_conversation(event.unified_msg_origin, cid)
            except Exception:
                logger.exception("切换群聊独立 conversation 失败")

    # ==================== 私聊校正 ====================
    async def _ensure_private_persona(self, event):
        umo = event.unified_msg_origin
        expected = self.private_intent.get(umo)
        if not expected:
            return
        actual = await self._get_persona_id(event)
        if actual == expected:
            return
        cid = await self._ensure_conversation_id(umo)
        if not cid:
            return
        try:
            await self._update_persona(umo, expected, cid)
            logger.info("私聊人格已校正/重建：umo=%s, cid=%s, pid=%s", umo, cid, expected)
        except Exception:
            logger.exception("私聊人格校正失败")

    # ==================== 消息拦截 ====================
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        if event.message_str.strip().startswith("/persona"):
            return
        await self._switch_to_group_conv(event)

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def on_private_message(self, event: AstrMessageEvent):
        if event.message_str.strip().startswith("/persona"):
            return
        await self._ensure_private_persona(event)

    # ==================== 会话读写 ====================
    async def _get_conversation_id(self, umo):
        try:
            return await self.cm.get_curr_conversation_id(umo)
        except Exception:
            logger.exception("获取会话 id 失败")
            return None

    async def _fetch_conversation(self, umo, cid):
        try:
            return await self.cm.get_conversation(umo, cid)
        except TypeError:
            try:
                return await self.cm.get_conversation(cid)
            except Exception:
                logger.exception("get_conversation 失败")
                return None
        except Exception:
            logger.exception("get_conversation 失败")
            return None

    def _extract_persona_id(self, conv):
        for attr in ("persona_id", "personality_id", "persona"):
            v = getattr(conv, attr, None)
            if v:
                return v
        return None

    async def _update_persona(self, umo, pid, cid):
        try:
            await self.cm.update_conversation_persona_id(umo, pid, cid)
            return
        except TypeError:
            await self.cm.update_conversation_persona_id(umo, pid)

    async def _get_persona_id(self, event):
        umo = event.unified_msg_origin
        if self._is_group(event):
            key = self._group_conv_key(event)
            cid = self.group_convs.get(key)
            if not cid:
                return None
        else:
            cid = await self._get_conversation_id(umo)
        if not cid:
            return None
        conv = await self._fetch_conversation(umo, cid)
        if conv is None:
            return None
        return self._extract_persona_id(conv)

    async def _get_current_persona_id(self, event):
        """统一入口：读取当前会话的人设（私聊优先 private_intent）。"""
        if self._is_group(event):
            return await self._get_persona_id(event)
        umo = event.unified_msg_origin
        return self.private_intent.get(umo) or await self._get_persona_id(event)

    async def _set_persona_id(self, event, pid):
        umo = event.unified_msg_origin
        if self._is_group(event):
            cid = await self._get_or_create_group_conv(event, pid)
            if not cid:
                return False, "无法创建群聊独立对话，请稍后重试。"
            lock = self._get_group_lock(umo)
            async with lock:
                try:
                    await self.cm.switch_conversation(umo, cid)
                except Exception as e:
                    logger.exception("切换群聊对话失败")
                    return False, f"切换群聊对话失败：{e}"
        else:
            cid = await self._ensure_conversation_id(umo)
            if not cid:
                return False, "无法创建/定位会话，请先发送一条普通消息建立会话后再试。"
            try:
                await self._update_persona(umo, pid, cid)
            except Exception as e:
                logger.exception("更新会话人设失败")
                return False, f"更新会话人设失败：{e}"
            if pid is None:
                self.private_intent.pop(umo, None)
            else:
                self.private_intent[umo] = pid
            self._save_private_intent()

        actual = await self._get_persona_id(event)
        if pid is None:
            if actual is not None:
                return False, f"写入成功但读回仍为 {actual}。"
        elif actual != pid:
            return False, f"写入后读回为 {actual!r}，与目标 {pid!r} 不符。"
        return True, ""

    async def _ensure_conversation_id(self, umo):
        cid = await self._get_conversation_id(umo)
        if cid:
            return cid
        for name in ("new_conversation", "create_conversation"):
            fn = getattr(self.cm, name, None)
            if fn is None:
                continue
            try:
                cid = await fn(umo)
                if cid:
                    return cid
            except Exception:
                logger.exception("%s(%s) 失败", name, umo)
        return None

    # ==================== 重置（含 LTM） ====================
    async def _reset_conversations(self, umo):
        fn = getattr(self.cm, "delete_conversations_by_user_id", None)
        if fn is not None:
            try:
                await fn(umo)
                logger.info("已重置会话（含 LTM）：%s", umo)
                return True, "已清空（含长期记忆）"
            except Exception:
                logger.exception("delete_conversations_by_user_id 失败，尝试 fallback")

        cid = await self._get_conversation_id(umo)
        if cid:
            try:
                await self.cm.delete_conversation(umo, cid)
                logger.info("已删除当前对话（fallback）：%s", umo)
                return True, "已清空（长期记忆可能未清）"
            except Exception:
                logger.exception("delete_conversation fallback 失败")
                return False, "清空失败"

        return True, "已清空"

    # ==================== 权限 / 状态 ====================
    def _is_admin(self, qq):
        return qq in self.admins

    def _is_public(self, pid):
        return pid in self.public_personas

    def _is_protected(self, pid):
        return pid not in self.public_personas

    def _is_authorized(self, qq, pid):
        return pid in self.authorized.get(qq, [])

    def _is_list_authorized(self, qq):
        return LIST_AUTH_KEY in self.authorized.get(qq, [])

    def _set_authorized(self, qq, pid):
        lst = self.authorized.setdefault(qq, [])
        if pid not in lst:
            lst.append(pid)
            self._save_authorized()

    def _check_move_password(self, is_admin, provided):
        if is_admin:
            return None
        if provided is None:
            return "❌ 需要密码"
        if provided != TEST_MOVE_PASSWORD:
            return "❌ 密码错误"
        return None

    # ==================== 状态转换 ====================
    def _set_public(self, pid):
        if pid in self.public_personas:
            return []
        self.public_personas.append(pid)
        self._save_public()
        return ["已加入公开列表"]

    def _set_protected(self, pid):
        if pid not in self.public_personas:
            return []
        self.public_personas.remove(pid)
        self._save_public()
        return ["已从公开列表移除（现在需要密码）"]

    # ==================== 管理员操作 ====================
    def _add_admin(self, qq):
        if qq in self.admins:
            return "exists"
        self.file_admins.add(qq)
        self.admins.add(qq)
        self._save_admins()
        return "ok"

    def _remove_admin(self, qq):
        if qq not in self.admins:
            return "not_found"
        if qq in self.env_admins:
            return "env_locked"
        self.file_admins.discard(qq)
        self.admins.discard(qq)
        self._save_admins()
        return "ok"

    # ==================== 授权操作 ====================
    async def _grant_async(self, qq, target):
        lst = self.authorized.setdefault(qq, [])
        added = []
        if target == ALL_AUTH_KEY:
            pending = [LIST_AUTH_KEY]
            all_p = await self.pm.get_all_personas()
            for p in all_p:
                if self._is_protected(p.persona_id):
                    pending.append(p.persona_id)
        else:
            pending = [target]

        for item in pending:
            if item not in lst:
                lst.append(item)
                added.append(item)
        if added:
            self._save_authorized()
        return added

    def _revoke(self, qq, target):
        if qq not in self.authorized:
            return False
        if target == ALL_AUTH_KEY:
            del self.authorized[qq]
            self._save_authorized()
            return True
        lst = self.authorized[qq]
        new_lst = [x for x in lst if x != target]
        if len(new_lst) == len(lst):
            return False
        if new_lst:
            self.authorized[qq] = new_lst
        else:
            del self.authorized[qq]
        self._save_authorized()
        return True

    # ==================== 主命令路由 ====================
    @filter.command("persona")
    async def persona_root(self, event: AstrMessageEvent):
        args = event.message_str.strip().split()
        qq = self._get_qq_id(event)
        is_admin = self._is_admin(qq)

        if len(args) < 2:
            yield event.plain_result(self._help_text(is_admin))
            return

        sub, rest = args[1], args[2:]

        handlers = {
            "list":      self._cmd_list,
            "list-s":    self._cmd_list_s,
            "publish":   self._cmd_publish,
            "password":  self._cmd_password,
            "admin":     self._cmd_admin,
            "grant":     self._cmd_grant,
            "revoke":    self._cmd_revoke,
            "auths":     self._cmd_auths,
            "name":      self._cmd_name,
            "unbind":    self._cmd_unbind,
            "view":      self._cmd_view,
            "clean":     self._cmd_clean,
            "reset":     self._cmd_reset,
            "debug":     self._cmd_debug,
        }

        handler = handlers.get(sub)
        if handler:
            async for r in handler(event, rest, qq, is_admin):
                yield r
            return

        async for r in self._cmd_switch(event, sub, rest, qq, is_admin):
            yield r

    # ==================== list ====================
    async def _cmd_list(self, event, args, qq, is_admin):
        pwd = args[0] if args else None
        all_p = await self.pm.get_all_personas()

        if pwd == TEST_LIST_PASSWORD:
            self._set_list_authorized(qq)
            lines = [f"- {p.persona_id} [需密码]" for p in all_p if self._is_protected(p.persona_id)]
            body = "\n".join(lines) if lines else "暂无人设"
            yield event.plain_result("【人设列表（需密码）】\n" + body)
            return
        elif pwd:
            yield event.plain_result("❌ 密码错误")
            return

        lines = [f"- {p.persona_id}" for p in all_p if self._is_public(p.persona_id)]
        if not lines:
            yield event.plain_result("暂无公开人设")
            return
        yield event.plain_result("【人设列表】\n" + "\n".join(lines))

    async def _cmd_list_s(self, event, args, qq, is_admin):
        if not is_admin and not self._is_list_authorized(qq):
            yield event.plain_result("🔒 请先使用 /persona list <密码> 授权查看。")
            return
        all_p = await self.pm.get_all_personas()
        lines = [f"- {p.persona_id} [需密码]" for p in all_p if self._is_protected(p.persona_id)]
        body = "\n".join(lines) if lines else "暂无人设"
        yield event.plain_result("【人设列表（需密码）】\n" + body)

    # ==================== publish / password ====================
    async def _cmd_publish(self, event, args, qq, is_admin):
        if not args:
            yield event.plain_result("❌ 用法：/persona publish <人设ID> [密码]")
            return
        target = args[0]
        pwd = args[1] if len(args) >= 2 else None
        err = self._check_move_password(is_admin, pwd)
        if err:
            yield event.plain_result(err)
            return

        all_p = await self.pm.get_all_personas()
        if target not in [p.persona_id for p in all_p]:
            yield event.plain_result(f"人设「{target}」不存在")
            return

        changed = self._set_public(target)
        if not changed:
            yield event.plain_result(f"ℹ️ 人设「{target}」已经是公开状态")
            return
        yield event.plain_result(
            f"✅ 已将「{target}」设为公开\n"
            "现在它会显示在 /persona list 中，切换无需密码。"
        )

    async def _cmd_password(self, event, args, qq, is_admin):
        if not args:
            yield event.plain_result("❌ 用法：/persona password <人设ID> [密码]")
            return
        target = args[0]
        pwd = args[1] if len(args) >= 2 else None
        err = self._check_move_password(is_admin, pwd)
        if err:
            yield event.plain_result(err)
            return

        all_p = await self.pm.get_all_personas()
        if target not in [p.persona_id for p in all_p]:
            yield event.plain_result(f"人设「{target}」不存在")
            return

        changed = self._set_protected(target)
        if not changed:
            yield event.plain_result(f"ℹ️ 人设「{target}」已经是密码保护状态")
            return
        yield event.plain_result(
            f"✅ 已将「{target}」设为有密码\n"
            "现在它只显示在 /persona list-s 中，切换/查看需要密码。"
        )

    # ==================== 管理员 / 授权 ====================
    async def _cmd_admin(self, event, args, qq, is_admin):
        if not is_admin:
            yield event.plain_result("🔒 仅管理员可用。")
            return
        if not args:
            yield event.plain_result(
                "用法：\n"
                "/persona admin add <QQ>     添加管理员\n"
                "/persona admin remove <QQ>  移除管理员\n"
                "/persona admin list         列出管理员"
            )
            return
        action = args[0]

        if action == "list":
            if not self.admins:
                yield event.plain_result("暂无管理员")
                return
            lines = []
            for a in sorted(self.admins):
                tags = []
                if a in self.env_admins:
                    tags.append("环境变量")
                if a == qq:
                    tags.append("你")
                tag = f"（{', '.join(tags)}）" if tags else ""
                lines.append(f"- {a}{tag}")
            yield event.plain_result("【管理员列表】\n" + "\n".join(lines))
            return

        if action in ("add", "remove"):
            if len(args) < 2:
                yield event.plain_result(f"用法：/persona admin {action} <QQ>")
                return
            target_qq = args[1]
            if action == "add":
                r = self._add_admin(target_qq)
                msg = {
                    "exists": f"ℹ️ {target_qq} 已是管理员",
                    "ok":     f"✅ 已添加管理员：{target_qq}",
                }[r]
            else:
                r = self._remove_admin(target_qq)
                msg = {
                    "not_found":  f"ℹ️ {target_qq} 不是管理员",
                    "env_locked": f"⚠️ {target_qq} 由环境变量 PERSONA_ADMINS 指定，无法通过指令移除。",
                    "ok":         f"✅ 已移除管理员：{target_qq}",
                }[r]
            yield event.plain_result(msg)
            return
        yield event.plain_result(f"❌ 未知操作：{action}")

    async def _cmd_grant(self, event, args, qq, is_admin):
        if not is_admin:
            yield event.plain_result("🔒 仅管理员可用。")
            return
        if len(args) < 2:
            yield event.plain_result("用法：/persona grant <QQ> <人设ID | _all>")
            return
        target_qq, target = args[0], args[1]
        if target == "_all":
            target = ALL_AUTH_KEY
        added = await self._grant_async(target_qq, target)
        if not added:
            yield event.plain_result(f"ℹ️ {target_qq} 已拥有 {target} 权限")
        else:
            yield event.plain_result(f"✅ 已授予 {target_qq}：{', '.join(added)}")

    async def _cmd_revoke(self, event, args, qq, is_admin):
        if not is_admin:
            yield event.plain_result("🔒 仅管理员可用。")
            return
        if len(args) < 2:
            yield event.plain_result("用法：/persona revoke <QQ> <人设ID | _all>")
            return
        target_qq, target = args[0], args[1]
        if target == "_all":
            target = ALL_AUTH_KEY
        if self._revoke(target_qq, target):
            yield event.plain_result(f"✅ 已撤销 {target_qq} 的 {target} 权限")
        else:
            yield event.plain_result(f"ℹ️ {target_qq} 没有 {target} 权限")

    async def _cmd_auths(self, event, args, qq, is_admin):
        if args:
            if not is_admin:
                yield event.plain_result("🔒 查看他人授权需要管理员权限。")
                return
            target_qq = args[0]
        else:
            target_qq = qq
        auths = self.authorized.get(target_qq, [])
        if not auths:
            yield event.plain_result(f"{target_qq} 暂无任何授权")
            return
        lines = []
        for a in auths:
            if a == LIST_AUTH_KEY:
                lines.append("- _list_test（可查看需密码列表）")
            else:
                lines.append(f"- {a}")
        yield event.plain_result(f"【{target_qq} 的授权】\n" + "\n".join(lines))

    # ==================== 查看 / 切换 / 解绑 ====================
    async def _cmd_name(self, event, args, qq, is_admin):
        stype = self._get_session_type(event)
        pid = await self._get_current_persona_id(event)
        if pid:
            yield event.plain_result(f"【{stype}】当前会话人设：{pid}")
        else:
            cfg = self.context.get_config()
            default = cfg.get("provider_settings", {}).get("default_personality", "default")
            yield event.plain_result(
                f"【{stype}】当前会话未绑定，使用全局默认人设：{default}"
            )

    async def _cmd_unbind(self, event, args, qq, is_admin):
        if self._is_group(event):
            key = self._group_conv_key(event)
            if key in self.group_convs:
                cid = self.group_convs.pop(key)
                self._save_group_convs()
                try:
                    await self.cm.delete_conversation(event.unified_msg_origin, cid)
                except Exception:
                    logger.exception("删除群聊独立对话失败")
                yield event.plain_result("✅ 已解绑你的群聊独立人设，将使用群默认。")
            else:
                yield event.plain_result("ℹ️ 你在此群尚未绑定独立人设。")
            return
        else:
            ok, msg = await self._set_persona_id(event, None)
            if ok:
                yield event.plain_result("✅ 已解绑当前会话人设，将使用全局默认。")
            else:
                yield event.plain_result(f"❌ 解绑失败：{msg}")

    async def _cmd_view(self, event, args, qq, is_admin):
        """
        /persona view            → 显示当前会话人设的提示词
        /persona view <ID>       → 显示指定人设的提示词
        """
        stype = self._get_session_type(event)

        # ---- 无参：查看当前会话人设 ----
        if not args:
            pid = await self._get_current_persona_id(event)

            # 未绑定：显示全局默认人设
            if not pid:
                cfg = self.context.get_config()
                default = cfg.get("provider_settings", {}).get("default_personality", "default")
                p = await self.pm.get_persona(default)
                if p:
                    yield event.plain_result(
                        f"【{stype}】当前未绑定人设，使用全局默认「{default}」\n"
                        f"{p.system_prompt or '(空)'}"
                    )
                else:
                    yield event.plain_result(
                        f"【{stype}】当前未绑定人设，全局默认「{default}」不存在"
                    )
                return

            p = await self.pm.get_persona(pid)
            if not p:
                yield event.plain_result(
                    f"【{stype}】当前绑定的人设「{pid}」已不存在"
                )
                return
            yield event.plain_result(
                f"【{stype}】当前人设「{pid}」的提示词：\n"
                f"{p.system_prompt or '(空)'}"
            )
            return

        # ---- 有参：查看指定人设 ----
        target = args[0]
        p = await self.pm.get_persona(target)
        if not p:
            yield event.plain_result(f"人设「{target}」不存在")
            return
        if self._is_protected(target) and not is_admin and not self._is_authorized(qq, target):
            yield event.plain_result(f"🔒 需密码，请先用 /persona {target} <密码> 切换授权")
            return
        yield event.plain_result(f"【{target} 人设】\n{p.system_prompt or '(空)'}")

    # ==================== 清理 / 重置 ====================
    async def _cmd_clean(self, event, args, qq, is_admin):
        if not is_admin:
            yield event.plain_result("🔒 仅管理员可用。")
            return
        if not self._is_group(event):
            yield event.plain_result(
                "ℹ️ 该指令用于清理群聊的独立对话，请在群聊中使用。"
            )
            return

        umo = event.unified_msg_origin
        prefix = umo + ":"

        removed = []
        for key in list(self.group_convs.keys()):
            if not key.startswith(prefix):
                continue
            cid = self.group_convs[key]
            conv = await self._fetch_conversation(umo, cid)
            if conv is None:
                self.group_convs.pop(key, None)
                removed.append(key.rsplit(":", 1)[-1])

        if removed:
            self._save_group_convs()
            yield event.plain_result(
                f"✅ 已清理 {len(removed)} 条失效映射：{', '.join(removed)}\n"
                "这些用户下次发言时会自动重建独立对话。"
            )
        else:
            yield event.plain_result("✅ 当前群聊的独立对话映射全部有效，无需清理。")

    async def _cmd_reset(self, event, args, qq, is_admin):
        umo = event.unified_msg_origin

        if self._is_group(event):
            if not is_admin:
                yield event.plain_result("🔒 重置群聊对话需要管理员权限。")
                return

            prefix = umo + ":"

            # 1. 记录每个人的 persona
            user_personas = {}
            for key, cid in list(self.group_convs.items()):
                if not key.startswith(prefix):
                    continue
                conv = await self._fetch_conversation(umo, cid)
                if conv is not None:
                    pid = self._extract_persona_id(conv)
                    if pid:
                        user_personas[key] = pid

            # 2. 清空本群所有对话（含 LTM）
            ok, ltm_status = await self._reset_conversations(umo)

            # 3. 重建带原 persona 的 conversation
            rebuilt = 0
            for key, pid in user_personas.items():
                try:
                    new_cid = await self.cm.new_conversation(umo, persona_id=pid)
                    if new_cid:
                        self.group_convs[key] = new_cid
                        rebuilt += 1
                    else:
                        self.group_convs.pop(key, None)
                except Exception:
                    logger.exception("重建群聊对话失败：%s", key)
                    self.group_convs.pop(key, None)

            # 4. 清掉本群其他无效映射
            for key in list(self.group_convs.keys()):
                if key.startswith(prefix) and key not in user_personas:
                    self.group_convs.pop(key, None)
            self._save_group_convs()

            if rebuilt:
                msg = f"✅ 已重置当前群聊对话，{rebuilt} 个用户的人设绑定已保留。\n"
            else:
                msg = "✅ 已重置当前群聊对话。\n"
            msg += f"• 对话上下文：{ltm_status}\n"
            msg += "• 长期记忆：已随对话一起清空"
            yield event.plain_result(msg)
        else:
            ok, ltm_status = await self._reset_conversations(umo)

            pid = self.private_intent.get(umo)
            bind_info = f"人设绑定已保留（{pid}）" if pid else "当前未绑定人设"

            msg = f"✅ 已重置当前私聊对话，{bind_info}。\n"
            msg += f"• 对话上下文：{ltm_status}\n"
            msg += "• 长期记忆：已随对话一起清空"
            yield event.plain_result(msg)

    # ==================== 调试 ====================
    async def _cmd_debug(self, event, args, qq, is_admin):
        if not is_admin:
            yield event.plain_result("🔒 仅管理员可用。")
            return

        lines = ["【调试信息】"]

        try:
            cm_methods = sorted(m for m in dir(self.cm) if not m.startswith("_"))
            relevant = [
                m for m in cm_methods
                if any(k in m.lower() for k in
                       ("memory", "reset", "clear", "delete", "conversation"))
            ]
            lines.append("")
            lines.append("== ConversationManager 相关方法 ==")
            lines.append(", ".join(relevant) if relevant else "(无)")
        except Exception as e:
            lines.append(f"获取失败：{e}")

        lines.append("")
        lines.append("== LTM 清理实现方式 ==")
        if hasattr(self.cm, "delete_conversations_by_user_id"):
            lines.append("✅ 使用 delete_conversations_by_user_id(umo)")
        else:
            lines.append("⚠️ delete_conversations_by_user_id 不存在，使用 fallback")

        lines.append("")
        lines.append("== 人设状态统计 ==")
        lines.append(f"• 公开：{len(self.public_personas)} 个")
        if self.public_personas:
            lines.append(f"   {', '.join(self.public_personas)}")
        lines.append(f"• 私聊期望人格：{len(self.private_intent)} 条")

        yield event.plain_result("\n".join(lines))

    # ==================== 切换人设 ====================
    async def _cmd_switch(self, event, target, args, qq, is_admin):
        all_p = await self.pm.get_all_personas()
        if target not in [p.persona_id for p in all_p]:
            yield event.plain_result(f"人设「{target}」无效")
            return

        if self._is_protected(target) and not is_admin and not self._is_authorized(qq, target):
            if not args:
                yield event.plain_result(f"🔒 需密码，请使用 /persona {target} <密码>")
                return
            if args[0] != TEST_SWITCH_PASSWORD:
                yield event.plain_result("❌ 密码错误")
                return
            self._set_authorized(qq, target)

        ok, msg = await self._set_persona_id(event, target)
        if not ok:
            yield event.plain_result(f"❌ 切换失败：{msg}")
            return

        stype = self._get_session_type(event)
        if self._is_group(event):
            yield event.plain_result(
                f"✅ 【{stype}】你在此群的独立人设已切换为：{target}\n"
                "该人设仅对你生效，不影响群内其他人。\n"
                "如需清空上下文，可发送 /persona reset（管理员）或 /reset。"
            )
        else:
            yield event.plain_result(
                f"✅ 【{stype}】当前会话人设已切换为：{target}\n"
                "如需清空上下文 + 长期记忆，可发送 /persona reset。"
            )

    # ==================== 帮助 ====================
    def _help_text(self, is_admin=False):
        base = (
            "【人设指令帮助】\n"
            "/persona list                   查看公开人设\n"
            "/persona list <密码>            查看需密码的人设\n"
            "/persona list-s                 查看需密码的人设（需先授权）\n"
            "/persona name                   查看当前会话绑定\n"
            "/persona view                   查看当前会话人设的提示词\n"
            "/persona view <人设ID>          查看指定人设的提示词\n"
            "/persona unbind                 解绑当前会话人设\n"
            "/persona reset                  重置当前会话（清空上下文与长期记忆，保留人设）\n"
            "/persona <人设ID>               切换人设\n"
            "\n"
            "⚠️ 群聊与私聊完全独立：\n"
            "   • 私聊：你和机器人的一对一对话\n"
            "   • 群聊：每个人在群里都相当于和机器人私聊\n"
            "     —— 人格独立、对话历史独立，互不可见\n"
            "   • 机器人在群里不知道其他成员说了什么\n"
            "切换后立即生效。"
        )
        if is_admin:
            base += (
                "\n\n【管理员指令 👑】\n"
                "人设状态（二选一，互斥）：\n"
                "  /persona publish <ID> [密码]    设为公开（显示在 list，无需密码）\n"
                "  /persona password <ID> [密码]   设为有密码（显示在 list-s）\n"
                "管理员管理：\n"
                "  /persona admin add <QQ>          添加管理员\n"
                "  /persona admin remove <QQ>       移除管理员\n"
                "  /persona admin list              列出管理员\n"
                "授权管理：\n"
                "  /persona grant <QQ> <ID|_all>    授予权限\n"
                "  /persona revoke <QQ> <ID|_all>   撤销权限\n"
                "  /persona auths [QQ]              查看授权\n"
                "维护：\n"
                "  /persona clean                   清理本群失效的独立对话映射\n"
                "  /persona reset                   重置当前群聊所有对话（保留人设绑定，含 LTM）\n"
                "  /persona debug                   查看调试信息\n"
                "\n"
                "管理员可省略密码直接操作。"
            )
        return base