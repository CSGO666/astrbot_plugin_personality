# astrbot_plugin_personality

自定义人设切换插件，支持群聊内每人独立人设。

## 特性

- **群聊内每人独立**：群里每个人可以设置自己的人设，互不干扰，上下文也互相隔离
- **私聊会话独立**：私聊人格仅影响你与机器人的私聊
- **测试人设保护**：可把指定人设标记为"测试内容"，需密码才能查看/切换
- **管理员系统**：管理员可免密码操作，可授予/撤销他人权限
- **持久化**：切换后重启依然生效

## 指令

| 指令 | 说明 |
|---|---|
| `/persona list` | 查看人设 |
| `/persona list-s` | 查看测试人设列表（需授权） |
| `/persona to-test <ID> [密码]` | 将人设移入测试列表 |
| `/persona to-public <ID> [密码]` | 将人设移出测试列表 |
| `/persona name` | 查看当前会话绑定 |
| `/persona unbind` | 解绑当前会话人设 |
| `/persona view <ID>` | 查看提示词 |
| `/persona <ID>` | 切换人设 |
| `/persona admin add/remove/list` | 管理员管理 |
| `/persona grant <QQ> <ID\|_list_test\|_all>` | 授予权限 |
| `/persona revoke <QQ> <ID\|_list_test\|_all>` | 撤销权限 |
| `/persona auths [QQ]` | 查看授权 |

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `TEST_LIST_PASSWORD` | `123456` | 查看测试人设列表的密码 |
| `TEST_SWITCH_PASSWORD` | `654321` | 切换测试人设的密码 |
| `TEST_MOVE_PASSWORD` | 同 `TEST_LIST_PASSWORD` | 移入/移出测试列表的密码 |
| `PERSONA_ADMINS` | 空 | 逗号分隔的硬管理员 QQ，无法通过指令移除 |

## 数据文件

| 文件 | 作用 |
|---|---|
| `persona_auth.json` | 用户级授权记录 |
| `persona_test_list.json` | 测试人设名单 |
| `persona_admins.json` | 软管理员列表 |
| `persona_group_conversations.json` | 群聊内每人独立的 conversation 映射 |
| `persona_private_intent.json` | 私聊期望人格，用于消息到达时校正 |