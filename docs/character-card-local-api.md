# DZMM 写卡本地化：API 摸底（只读探测）

探测时间：本机登录态对 `https://www.dzmm.ai/studio/edit` 前端包反查 + tRPC 实测。  
入口页：[studio/edit](https://www.dzmm.ai/studio/edit)

## 结论

**可以本地写、本地发。**  
写卡不是 Game Studio 的 `gamefy/editor` + 容器文件，而是 **tRPC `studio.*` / `card.*`**，载荷为 **SillyTavern 风格 `chara_card_v3` JSON**（字段含 `name/description/personality/scenario/first_mes/character_book/...`，另有 `gamefy` 扩展）。

## 已确认可用的读接口（HTTP 200）

| Procedure | 作用 | 示例入参 |
|---|---|---|
| `studio.getCharacters` | 我的角色列表（含草稿/卡） | `{}` |
| `studio.getCharacterCard` | 按 ID 取完整卡 | `{ "id": 3374995 }` |
| `studio.getDraft` | 取草稿 | `{ "id": <draftId> }`（无效 id → 404「草稿不存在」） |
| `studio.getGameStats` | 创作侧统计 | `{}` |
| `studio.getMyVoices` / `studio.getVoices` | 音色 | `{}` |
| `card.getForChat` | 聊天用卡摘要 | `{ "cardId": 3374995 }` |

调用形态：

```
GET  /api/trpc/<procedure>?batch=1&input=<urlenc {"0":{"json":{...}}}>
POST /api/trpc/<procedure>?batch=1   body: {"0":{"json":{...}}}
```

鉴权：现有 `.env` cookie + Bearer（与本地桥相同）。

## 写/发（从前端包确认，尚未做写入实测）

| Procedure | 前端用途 |
|---|---|
| `studio.saveDraft` | 自动保存草稿 `{ rawData, id?, baseVersion? }` |
| `studio.deleteDraft` | 删草稿 `{ id }` |
| `studio.uploadOrUpdate` | **发布/更新角色卡** `{ rawData, db_id? }`（`createOrUpdateCharacter`） |
| `studio.hideCharacterCard` | 隐藏卡 `{ id }` |
| `studio.uploadCharacterImage` | 上传立绘/封面 |

图片相关：

- 公共桶：`pub_card_images`（Supabase storage）
- 辅助：`/api/character-card-image`

## 编辑模式（前端状态机）

- `create`：新建
- `draft:<id>`：草稿
- `character:<id>`：已发布卡再编辑  
发布成功后跳转 `/character/$id`。

## 与现有本地桥的关系

| 现有 `dzmm-local-dev` | 写卡 |
|---|---|
| `POST /api/gamefy/editor` + 容器 sync | **另一条产品线**（游戏包） |
| 需要已有 `character_id` | 写卡可先 `uploadOrUpdate` 拿新 ID，再绑 Game Studio |

## 已落地（本地控制台）

1. `lib/dzmm_character.py`：本地 `cards/*.json` + AI completions 写卡 + 云端只读 list/pull
2. 控制台侧栏「角色卡 → 本地写卡」：AI 写卡并保存、编辑、刷新列表、拉取云端到本地
3. API：`/api/card/list|get|new|save|ai|cloud|pull`
4. 云端写入已接：`POST /api/card/publish` → 本地图 + `uploadOrUpdate` / `saveDraft`；上架/下架请官网（控制台不提供按钮）；列表绿标读角色页 `isPublic`/`publishStatus`；游戏卡 pull 会被拒绝
5. **试玩（平台对话）**：见下节
6. **互动模板**：写卡 Tab「互动模板」→ `extensions.status_template`（+ 可选 `recommended_model`）；落盘 `extensions.json`；pull/publish 透传；**无新 tRPC**（仍走 saveDraft/uploadOrUpdate）

## 试玩（平台对话）

仅 **云端正式卡**（`data.db_id` / `_meta.cloudId` > 0，非草稿）可试玩。控制台点「试玩」后写卡区切为试玩面板。

### 平台接口（摸底 2026-08）

| 能力 | 调用 |
| --- | --- |
| 建会话 | tRPC `chat.createByCard` `{ cardId, fixedRandomIndex?, entryPoint }` → `{ chatId }`；`entryPoint` 须为平台枚举（本地用 `quick_chat`） |
| 删会话 | tRPC `chat.deleteChat` `{ chatId }`（官网聊天列表右键删除；控制台「返回写卡」会调） |
| 卡摘要 | tRPC `card.getForChat` / `card.getQuickChatPreview` |
| 模型列表 | tRPC `chat.models` `{ service: "chat" }` → `categories[].modelGroups[].contexts[]`（`internalName`） |
| 账号预设 | tRPC `preset.list` → `{ presets, settings.activePresetIds, settings.playerInfo }` |
| 历史消息 | tRPC `chat.getMessages` `{ chatId }` |
| 发消息 | `POST https://www.dzmm.ai/api/chat` JSON body（见下）→ **SSE** |
| 断线续传 | `GET /api/chat/{chatId}/stream/{generationId}` |

生成 body 要点：

```json
{
  "operation": "generate",
  "chatId": "...",
  "cardId": 123,
  "chatSettings": {
    "model": "x-apex-surge-0505-16k",
    "maxTokens": 2500,
    "deepThinking": false,
    "enableMemoryEnhance": false
  },
  "presetConfig": { "presetIds": ["…"], "playerInfo": "" },
  "prompts": [{ "role": "user|assistant", "content": "…" }],
  "content": "本轮用户输入"
}
```

SSE 行格式：`data: {"type":"init|token|step|complete|error","data":…}`  
- `token`：`data` 为增量字符串  
- `complete`：整轮结束载荷  

官网「上下文长度」16K/32K **不是** `maxTokens`，而是同一 `seriesKey` 下 `contexts[]` 的不同 `internalName`（如 `…-16k` vs 默认 32K，`maxContext` 字段）。  
`maxTokens` 随深度思考：开 3500 / 关 2500。另有 `enableMemoryEnhance`。预设以 `presetConfig.presetIds` 覆盖本轮。

### 本地代理

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/card/play/meta?cardId=` | 摘要 + 开场预览 |
| POST | `/api/card/play/start` | `{ cardId, chatHistoryIndex? }` → `chatId` + 首轮消息 |
| POST | `/api/card/play/delete` | `{ chatId }` → tRPC `chat.deleteChat`；「返回写卡」自动调用 |
| GET | `/api/card/play/models` | 聊天模型 |
| GET | `/api/card/play/presets` | 账号预设 + `displayName`（`user.getMe.fullName`，开场 `{{user}}` 用） |
| GET | `/api/card/play/messages?chatId=` | 拉历史 |
| GET | `/api/card/play/settings?chatId=` | tRPC `chat.getSettings` |
| POST | `/api/card/play/settings` | `{ chatId, settings }` → tRPC `chat.updateSettings`（title/style/maxTokens/model/imageGenerationModel/deepThinking/enableMemoryEnhance…） |
| POST | `/api/card/play/send` | 透传平台 SSE（body 含 model / maxTokens / style / deepThinking / enableMemoryEnhance / imageGenerationModel / presetIds / prompts / content） |

官网侧栏「设置」对应关系：

| UI | 接口 |
| --- | --- |
| 开启高亮 / 经典样式 | 本地显示偏好（非 tRPC；高亮键同官网 `chat_enable_highlight`） |
| 会话标题 / 风格 / 最大回复 Token / 图像模型 | `chat.updateSettings` |
| 风格枚举 | `standard` \| `creative` \| `divergent` \| `apex_dry` |
| 图像模型 | `anime` \| `iroha` |

实现：`lib/dzmm_character.py`（`play_*`）+ `console.py`；UI：`web/index.html` `#cardPlayPanel`。  
试玩消耗平台积分 / 受会员与模型权限约束；失败原样回传。

探针脚本：`tools/probe_chat_play.py`（产物 `.probe-character-api/chat_play_probe.json` 等）。

## 探测产物目录

`dzmm-local-dev/.probe-character-api/`（JS 包、probe JSON；勿提交敏感卡内容到公开仓库）
