# Chat2image（聊天生图）

在对话里说一句想要什么图，Agent 帮你把图生成出来。这是一个 **Agent Skill**：装进你的 AI 助手（WorkBuddy 等支持 Skill 的客户端）之后，你不用打开别的网页、不用贴提示词，直接在对话里说「用 Chat2image 帮我生成一张……」。

- 支持 1K / 2K / 4K，竖版、方版、横版比例可选
- 支持**图生图**：给参考图，改风格、改构图、融合多张
- 生成前会先把你的需求整理成一段结构化提示词给你确认，你点头才调接口
- 每次生成后直接告诉你这一张花了多少、余额还剩多少；余额不足时给缴费入口

## 装它之前要知道的两件事

1. 需要 **Python 3.8+**（装过 Python 就行，不需要额外 pip 包）。
2. 出图走云端接口，**需要按张付费**。装好后可以先用体验额度试几张，用完再充。

## 安装（推荐：一句话交给 Agent）

把下面这段原样发给你在用的 Agent（把 `martinning-lab/chat2image` 原样保留）：

```text
帮我装这个 Skill：聊天生图 Chat2image

去 GitHub 上找 martinning-lab/chat2image 这个项目，从它的 Releases 页面下载最新的 chat2image-***.zip。

下载后按压缩包里的 chat2image-***/INSTALL.md 安装。
```

两点说明：

- **只认 Release 里的压缩包**，不要拿仓库源码自己拼装。
- 找不到这个仓库、或者下载下来对不上 Release 页给的 `SHA256SUMS` 校验值，**就停下来告诉他**，不要换个地方再找包——换来的包我们不为它负责。

压缩包解开后是 `chat2image-<版本>/`，里面有一份 `INSTALL.md`，Agent 照着它装就行；装完用一句「用 Chat2image 帮我生成图片」冒烟。

## 手动安装（不想让 Agent 动手时）

```bash
unzip chat2image-<版本>.zip
cd chat2image-<版本>
bash install.sh                 # 建配置、给脚本执行位
bash bin/doctor.sh              # 自检：地址、服务、余额
bash bin/gen.sh "一只在雨里打伞的柴犬，竖版 1K"
```

其他命令：`bin/balance.sh`（查余额）、`bin/recharge.sh --points 5`（充值）、`bin/jobs.sh`（查历史任务）、`bin/check-update.sh` / `bin/upgrade.sh`（检查与安装更新）。

## 它会写什么、不写什么

- 只在 `~/.config/chat2image/` 存两样东西：本机安装标识、访问凭证（权限 600）。**删掉这个目录 = 这台机器的身份丢了**。
- 只往 Skill 目录放一个 `.env`，里面只有中转站地址；上游服务的 key **永远不在这台机器上**。
- 不读取你的聊天内容，只处理你这次要生成的提示词与参考图。

## 更新

问你用的 Agent「检查一下 Chat2image 有没有新版本」即可。更新由你决定，不会自动装。

## 许可证

MIT，见 [LICENSE](LICENSE)。客户端代码开源；背后的生成服务与计费系统不开源，使用服务按张计费。
