# ⚡ DZMM 本地开发控制台

<p align="center">
  <strong>🌉 Local Bridge · 游戏卡云端桥 · 角色卡本地写 / 试玩 · 一键同步发布</strong>
  <br/><br/>
  <a href="https://discord.gg/da9PMeAGGK"><img src="https://img.shields.io/badge/Discord-交流群-5865F2?style=for-the-badge&logo=discord&logoColor=white" alt="Discord"></a>
  <a href="https://github.com/Tomkk74/dzmm-gmide"><img src="https://img.shields.io/badge/GitHub-仓库-181717?style=for-the-badge&logo=github&logoColor=white" alt="GitHub"></a>
  <img src="https://img.shields.io/badge/Python-3-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/License-MIT-green?style=for-the-badge" alt="MIT">
</p>

> 💬 **交流群（Discord）** → [https://discord.gg/da9PMeAGGK](https://discord.gg/da9PMeAGGK)  
> 📦 **仓库** → [https://github.com/Tomkk74/dzmm-gmide](https://github.com/Tomkk74/dzmm-gmide)

本机 Web 控制台，两种模式互不混用：

| 模式 | 面向 | 典型流程 |
|:---|:---|:---|
| **游戏卡** | Game Studio 互动游戏 | 登录 → 绑 `character_id` → 拉容器 → 本地改 `publish/` → 预览 → sync → 发布 |
| **角色卡** | SillyTavern 风格写卡 | 登录 → 本地写 `卡/<卡名>/` → 保存 / 上云 → **试玩**（平台对话）→ 导出 PNG |

```text
游戏卡：  本机 publish/  ──sync──▶  Workbench 容器  ──发布──▶  线上玩家包
角色卡：  本机 卡/<名>/  ──保存/上云──▶  studio 正式卡  ──试玩──▶  平台 chat API
```

首次打开默认进入 **游戏卡 → 账号登录**；顶栏可切换到 **角色卡**。上次选择的模式会写入 `config.json`（`console_mode`），下次启动自动恢复。

---

## 🚀 快速开始

### 环境

- Windows：双击 `start.bat`；其它系统：`python start.py`
- [Python 3](https://www.python.org/)（安装时勾选 *Add to PATH*）
- DZMM 账号（游戏卡需对该卡有 Game Studio 权限；角色卡试玩 / 上云需已登录）

### 启动

**只用这一套入口**（不要再直接跑 `console.py`）：

```bat
git clone https://github.com/Tomkk74/dzmm-gmide.git
cd dzmm-gmide
start.bat
```

非 Windows：

```bat
python start.py
```

会清理旧端口、启动控制台；已登录且本地有 `publish/index.html` 时自动起游戏预览。  
浏览器打开：`http://127.0.0.1:8788/`  
可选参数：`--no-open` 不弹窗、`--no-preview` 只开控制台、`--no-kill` 不清理占用端口的旧进程。

凭据写在本机 `.env`，**不会**上传到本仓库。网页「账号登录」支持与官网对齐的多种方式（任选其一即可）：

| 方式 | 说明 |
|:---|:---|
| **邮箱密码** | 与官网相同；可勾选保存密码到本机 `.env` |
| **登录码** | 在已登录的手机 App / 其它设备打开登录码，截图或保存二维码图后上传 |
| **Telegram** | 获取二维码后用 Telegram 扫码，或向 Bot 发送 `/login 码`，本页自动轮询完成登录 |
| **Cookie** | 浏览器已登录官网时，从 DevTools Cookies 复制 `sb-rls-auth-token`（或整段 Cookie）粘贴 |

国内访问 `www.dzmm.ai` 易被拦时：在「账号登录」选国内线路，或点「测速并优选」（对齐官方 [线路页](https://dzmm-home.github.io/dzmm-addr/)）。换线路后需重新登录。

---

## 🃏 角色卡

侧栏：**角色卡 → 本地写卡**。成品落在 `卡/<卡名>/`（多文件拆分 + `card.json`）。

### 无记忆开聊（给 AI 写卡）

规范包在仓库根目录 [`_模板/`](_模板/README.md)。新对话复制 [`_模板/开聊提示词.txt`](_模板/开聊提示词.txt)，或：

```text
请先阅读并严格遵守：
- _模板/AGENTS.md
- _模板/填写顺序.md
- _模板/写作规范.md

然后按规范创作角色卡「卡名」：（创意简述）
```

Agent 入口：[`AGENTS.md`](AGENTS.md) → [`_模板/AGENTS.md`](_模板/AGENTS.md)  
字段说明：[`docs/character-card-fields.md`](docs/character-card-fields.md)  
写作规范（与模板同源）：[`docs/character-card-writing-guide.md`](docs/character-card-writing-guide.md)

### 控制台能力

| 能力 | 说明 |
|:---|:---|
| 本地编辑 | 基础字段、世界书条目、开场 / 建议回复、头像与封面等；改文件可同步进编辑器 |
| 云端列表 / 拉取 | 登录后拉取账号下角色卡到本地 |
| 保存 / 上云 | 本地保存；上云走 `studio.uploadOrUpdate`，并同步已有草稿版本（`studio.saveDraft`）；上架下架请官网 |
| **试玩** | 仅 **已上云的正式卡**（有 `db_id`）；草稿不可试玩 |
| 导出 PNG | 先保存卡；封面优先 `image_info`，否则头像；嵌入完整 `chara_card_v3`（`tEXt`：`chara` + `ccv3`） |

### 试玩要点

1. 卡已保存到云端（正式卡，非草稿）→ 点顶栏 **试玩**  
2. 工具栏：模型、上下文长度（模型 `contexts`）、深度思考、记忆增强、账号预设（弹层多选）、聊天设置  
3. 消息区占满中间；输入贴底。`{{user}}` 显示为当前账号昵称；旁白 `*…*` 斜体  
4. **返回写卡** 退出试玩（仅试玩模式显示）

聊天设置（弹层）：标题、回复风格、深度思考下的 maxTokens、生图模型等；部分展示偏好仅本机。  
接口摸底见 [`docs/character-card-local-api.md`](docs/character-card-local-api.md)。

### 世界书约定（模板）

| 条目 | 用途 |
|:---|:---|
| `001` | 世界观常驻 |
| `002` | 玩家设定常驻 |
| `003+` | 关键词触发 |

正文优先用 `{{user}}` / `{{char}}`，勿写死玩家名。

> `卡/*/` 默认不进 Git（仅保留 `卡/README.md` 与 `卡/AGENTS.md`）。私密成品请自行留在本机。

---

## 🎮 游戏卡（云端桥）

左侧功能区 + 右侧预览。改本地文件 → 预览 → 同步 / 发布。

```text
本机改 publish/  ──sync──▶  Workbench 容器  ──发布──▶  线上玩家包
     ▲                              │
     └──────── 拉取整包 ◀───────────┘
```

### 推荐顺序

```text
1 一键启动 → 2 账号登录 → 3 项目配置 → 4（可选）拉取容器 → 5 sync → 6 发布到线上
```

| 步骤 | 说明 |
|:---|:---|
| 一键启动 | `start.bat` / `python start.py`（唯一启动方式） |
| 账号登录 | 邮箱密码 / 登录码 / Telegram / Cookie；可写本机 `.env` |
| 项目配置 | `character_id`（Workbench 地址栏）、本地项目路径（含 `publish/`）、预览端口（默认 `8791`） |
| 拉取容器 | 云端整包落地；路径空则默认 `../{character_id}` |
| 预览 | 顶栏可切 **本地 / 云端**：本地读本机 `publish/`；云端持续镜像容器 `publish` 到旁路目录（不覆盖本地工程）再预览 |
| 官方助手 | 右侧面板对接 Workbench Chat（Claude / Codex Agent）；需有效游戏卡 `character_id`；改的是云端，可用「云端预览」看效果 |
| 同步 | 对照 `_sync_meta.json` 增量上传；**换卡 / 无 pull 基线会自动全量**；上传后会 **清理容器多余文件**（本地已删的远端也会删）；网络抖动自动重试；失败文件保留待重试；强制全量加 `--full` |
| 发布 | 悬浮按钮显示进度；完成后自动复位 |

### 界面截图

![总览：云端桥接](docs/images/01-overview.png)

![账号登录](docs/images/02-login.png)

![项目配置](docs/images/03-project.png)

![拉取容器](docs/images/04-pull.png)

![运行状态](docs/images/05-status.png)

![全屏与悬浮发布](docs/images/06-fullscreen-fab.png)

---

## ⌨️ 命令行（可选）

启动请只用 `start.bat` / `start.py`。下面是登录 / 拉取 / 同步等辅助命令：

| 脚本 | 作用 |
|:---|:---|
| `start.bat` / `start.py` | **唯一启动入口**（清端口 + 控制台 + 可选预览） |
| `status.bat` | 配置 / 登录态 |
| `pull.bat` | 拉取游戏容器 |
| `sync.bat` | 同步本地到容器 |

```bat
python start.py
python lib\dzmm_studio.py login --email you@mail.com --password "***"
python lib\dzmm_studio.py login --cookie "sb-rls-auth-token=..."
python lib\dzmm_studio.py login --code-image sign-in.png
python lib\dzmm_studio.py status --character-id <CHARACTER_ID>
python lib\pull_container.py --character-id <CHARACTER_ID>
python lib\dzmm_studio.py sync --character-id <CHARACTER_ID> --message "sync"
```

Telegram 登录请用网页控制台「账号登录 → Telegram」（需扫码/轮询）。

---

## 📂 目录结构

```text
.
├── start.py / start.bat       # 唯一启动入口
├── console.py                 # Web 控制台服务（由 start 拉起，勿直接当入口）
├── sync.bat / pull.bat …      # 登录 / 拉取 / 同步等辅助脚本
├── web/                       # 控制台前端（游戏卡 + 角色卡 / 试玩）
├── lib/
│   ├── dzmm_studio.py         # 登录、续期、游戏卡 sync / publish
│   ├── dzmm_character.py      # 角色卡本地 / 云端 / 试玩代理
│   ├── pull_container.py      # 游戏容器整包拉取
│   └── dzmm_preview_server.py
├── _模板/                     # 角色卡冷启动规范（勿改成品写这里）
├── 卡/                        # 角色卡成品：卡/<卡名>/
├── docs/                      # 字段说明、写卡规范、API 摸底、截图
├── config.example.json        # → 复制为 config.json
├── .env.example               # → 复制为 .env
└── LICENSE
```

| 本机文件（勿提交） | 说明 |
|:---|:---|
| `.env` | 邮箱 / 密码 / cookie |
| `config.json` | 游戏卡 `character_id`、项目路径、预览端口、`console_mode`（`game` / `card`） |

也可只在网页里保存配置，无需手改文件：

```bat
copy config.example.json config.json
copy .env.example .env
```

```json
{
  "character_id": 0,
  "project_path": "D:\\path\\to\\your-game",
  "preview_port": 8791
}
```

---

## 🛡️ 安全与注意

- 不要把 `.env` 发给任何人，也不要提交到 Git  
- 游戏卡「发布」会正式上线容器内容，操作前请确认  
- 角色卡上架 / 下架请在官网操作；控制台负责本地写、上云与试玩  
- 对接平台私有接口，平台改版后本工具可能需要更新  

有问题进 Discord： [https://discord.gg/da9PMeAGGK](https://discord.gg/da9PMeAGGK)

---

## ❓ 常见问题

<details>
<summary><b>打不开 8788？</b></summary>

确认已装 Python 3；用 `start.bat` / `python start.py` 启动（会自动清理旧端口）。改端口：`python start.py --port 8790`。
</details>

<details>
<summary><b>连接编辑器失败？（游戏卡）</b></summary>

检查账号是否对该 `character_id` 有 Studio 权限；Workbench 是否已选过模板。
</details>

<details>
<summary><b>预览空白？（游戏卡）</b></summary>

本地路径是否指向含 `publish/index.html` 的目录；先停止再启动预览；看「运行状态」报错。若 KV / 云端接口返回验证码（418），预览页会自动弹出官网验证页，完成后再试。
</details>

<details>
<summary><b>试玩按钮灰掉 / 404？（角色卡）</b></summary>

须为已上云正式卡（有云端 ID），草稿不可试玩。改过后需重启控制台进程以加载新路由；浏览器强刷缓存。
</details>

<details>
<summary><b>写卡区空白、只剩试玩壳？</b></summary>

刷新后应回到写卡；若仍异常，顶栏点「返回写卡」，或清站点本地存储后重开。
</details>

<details>
<summary><b>拉取游戏容器很慢？</b></summary>

可点「重拉失败文件」；网络不稳时重试。
</details>

---

## 📜 License

MIT © Tomkk74

<p align="center">
  <sub>觉得有用就给仓库点个 Star · 有问题进 Discord 交流群</sub>
  <br/>
  <a href="https://discord.gg/da9PMeAGGK">discord.gg/da9PMeAGGK</a>
</p>
