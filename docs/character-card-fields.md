# DZMM 平台写卡：填写项与分类（对照 studio/edit）

来源：前端包 i18n / 校验 / 默认 `rawData` 结构反查。

**各框「怎么写」**（官网帮助文案与占位示例）：见 `docs/character-card-writing-guide.md`。

## 向导模式

- **快速模式 (quick)**：精简基础项 + 必填图/名
- **完整模式 (full)**：走完整步骤

## 向导步骤（分类）

| 步骤 key | 界面含义 | 主要数据 |
| --- | --- | --- |
| `basic` | 基础信息 | 名称、简介、性格、场景、系统指令、创作者备注、标签、头像/立绘 |
| `interact` | 互动模板（本地控制台） | `extensions.status_template`、推荐模型 |
| `worldbook` | 世界设定 / 世界书 | `character_book` |
| `dialogue` | 开场对话 | `chat_history`（多轮 user/assistant）、建议回复 |
| `voice` | 音色 | `voice_settings` |
| `preview` | 预览与发布 | 校验汇总后 `uploadOrUpdate` |

## 1. 基础信息 `basic`

| 界面组件 | JSON 字段 | 说明 |
| --- | --- | --- |
| 角色名 | `data.name` | 必填；影响人设与 AI 自我认知 |
| 详细描述 | `data.description` | 背景、经历、能力、关系等 |
| 性格 | `data.personality` | 特质/情感/人际/价值观/行为等 |
| 场景 | `data.scenario` | 环境、氛围、时间、位置 |
| 系统指令 | `data.system_prompt` | 可执行的扮演规则 |
| 角色卡介绍 | `data.creator_notes` | 给人看；**AI 不可见** |
| 标签 | `data.tags[]` | |
| 创作者 | `data.creator` | |
| 版本 | `data.character_version` | |
| 头像 | `data.avatar_url` | 发布校验常要求 |
| 立绘/封面图 | `data.image_info[]` | `{url,name,isHidden,triggerKeywords}`，至少一个 |
| （兼容）开场白 | `data.first_mes` | 完整模式更偏向对话步骤；仍可能出现在导出结构里 |
| 备选问候 | `data.alternate_greetings[]` | 出现在 data 属性表中 |
| 扩展 | `data.extensions` | 含 `recommended_model`、`status_template` 等 |

## 1b. 互动模板 `interact`（专业模式 · 核心身份）

官网「互动模板」≠「详细描述·插入模板」。聊天页状态面板 / 剧情选项。

落盘：`卡/<名>/extensions.json` → `data.extensions`

| 字段 | 说明 |
| --- | --- |
| `extensions.recommended_model` | 新建聊天默认模型（可选） |
| `extensions.status_template` | 互动模板本体；未启用时省略 |

`status_template` 结构：

```json
{
  "version": 1,
  "templates": ["status_bar", "relationship", "inventory", "options"],
  "panels": [{ "id": "panel_1", "title": "自定义" }],
  "fields": [
    {
      "key": "time",
      "template": "status_bar",
      "label": "时间",
      "type": "text",
      "initial": "未知",
      "hint": "可选"
    }
  ],
  "options": { "count": 3, "hint": "可选" },
  "theme": null
}
```

| 项 | 值 |
| --- | --- |
| 官方模板 id | `status_bar` / `relationship` / `inventory` / `options` |
| field.type | `text` / `number` / `meter` / `list` |
| field.key | `^[a-z][a-z0-9_]{0,31}$` |
| 无新 tRPC | 随 `saveDraft` / `uploadOrUpdate` 的 rawData 上传 |

本地控制台：写卡 Tab「互动模板」编辑；pull/publish 透传。

## 2. 世界书 `worldbook`（世界设定）

根对象：

```json
"character_book": {
  "name": "世界设定",
  "entries": [],
  "extensions": {}
}
```

单条 entry（新建默认）：

| 字段 | 含义 |
| --- | --- |
| `id` | 数字 ID |
| `name` | 条目标题 |
| `keys` | 触发关键词数组；非 `constant` 时必填 |
| `content` | 条目正文；必填 |
| `enabled` | 是否启用 |
| `constant` | 常驻（不依赖关键词） |
| `insertion_order` | 插入顺序 |
| `position` | 插入位置（数字，默认约 4） |
| `priority` | 优先级 |
| `extensions` | 可含 `comment` 备注 |

校验：`entryMissingKeywords` / `entryMissingContent`。

## 3. 开场对话 `dialogue`

| 字段 | 结构 |
| --- | --- |
| `chat_history` | `[{ id, name, messages:[{role,content}] }]`；`role` 为 user/ai（或 assistant） |
| `suggested_replies` | 建议回复列表 |

校验：至少一段对话、至少一条 AI 消息、最后一条须为 AI。

快速模式里常把「开场对话」做成可编辑的消息流，而不是只填一个 `first_mes`。

## 4. 音色 `voice`

| 字段 | 说明 |
| --- | --- |
| `voice_settings` | 选中的音色与朗读选项（忽略英文/括号、只读引号等） |

另有公共/私有音色库、自定义音色（`studio.getVoices` / `getMyVoices`）。

## 5. 预览发布

汇总：名称、头像、图片数、世界书条目数、对话段数、音色是否配置；通过后 `studio.uploadOrUpdate`。

## 本地目录约定（本仓库）

`卡/<角色名>/` 按平台分类落盘，详见该目录 `README.md`。
