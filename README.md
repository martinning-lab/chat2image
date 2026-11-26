# Chat2image（聊天生图）

在对话里说一句想要什么图，Agent 直接把图给你。这是一个 **Agent Skill**：装进支持 Skill 的客户端（WorkBuddy 等）之后，不用打开别的网页、不用手写提示词。

- **出图使用 GPT Image 2.6（Flare）**，服务异常时自动切换备用线路，不影响出图
- **文生图 / 图生图全支持**：直接说画面就出图；也能拿参考图改图、多图融合、换风格、保持人物不变
- **不会写提示词也能用**：它先问清用途、画风、关键细节，整理成一段专业提示词给你确认，**你点头才出图**
- 支持 1K / 2K / 4K，竖版、方版、横版比例可选
- 每张图出完直接告诉你这张花了多少、余额还剩多少；余额不足时给缴费入口
- 有参考图时会问你想参考它的哪一部分（人物形象 / 画风 / 文字字体 / 颜色 / 明暗 / 构图），再按你圈的范围读图，并把读到的要点贴回来给你改

## 装它之前要知道的三件事

1. 需要 **Python 3.8+**（装过 Python 就行，不需要额外 pip 包）。
2. 出图走云端接口，**按张付费**。装好后可以先用体验额度试几张，用完再充。
3. 客户端**不含任何模型 key**，也不含价格数字；价目与扣费一律以服务端返回为准。

## 安装（三个源任选其一）

同一个 zip 从三个渠道分发：**GitHub 与 Gitee 这轮同步到 0.2.10**；官网那条链接指向的文件以该页当前提供的版本为准，可能略滞后于 Release。从哪个源拿都不影响装法——**下载后一律以发布页附件的哈希清单核对 zip 本身的 SHA256**（两个平台同名 `c2i-0210-SHA256SUMS`，内容只有一行 = zip 本体的 sha256），对不上就停，别换个源凑。

| 源 | 地址 |
|---|---|
| 官网（自有服务器） | `https://chat2image.mashangai.com.cn/downloads/chat2image-latest.zip` |
| GitHub | 本仓库 **Releases** 页的最新 `chat2image-*.zip` |
| Gitee | `martinning/chat2image` 的 **Releases** 页的最新 `chat2image-*.zip` |

**只认发出去的 zip（Release 附件或官网下载链接），不要拿仓库源码自己拼装**——源码树不保证和发出去的包同步。

### 推荐：一句话交给 Agent

把下面这段原样发给你在用的 Agent：

```text
帮我装这个 Skill：聊天生图 Chat2image

从下面三个源里任选一个下载最新的 chat2image-*.zip（打不开就换下一个，别自己另找）：
1) https://chat2image.mashangai.com.cn/downloads/chat2image-latest.zip
2) GitHub martinning-lab/chat2image 的 Releases
3) Gitee martinning/chat2image 的 Releases

下载后先核对 zip 本身的 SHA256（发布页附件的哈希清单 `c2i-0210-SHA256SUMS`），对不上就停。
解压后按包里的 chat2image-*/INSTALL.md 安装，装完跑 bin/doctor.sh 并原样回报结果。
```

### 两道校验，别搞混

- 发布页附件 `c2i-0210-SHA256SUMS`（无扩展名）= **压缩包本身**的哈希，确认「下载到的 zip 没被换过」；
- 包内 `SHA256SUMS.txt` = **包内每个文件**的哈希，解压后 `cd` 进 `chat2image-<版本>/` 再跑 `shasum -a 256 -c SHA256SUMS.txt`。

任一道对不上就**停下来**，不要换个地方再找包——换来的包我们不为它负责。

### 手动安装（不想让 Agent 动手时）

```bash
unzip chat2image-<版本>.zip
cd chat2image-<版本>
bash install.sh                 # 建 .env、给脚本执行位、建配置目录
bash bin/doctor.sh              # 自检：地址、服务、余额
bash bin/gen.sh "一只在雨里打伞的柴犬，竖版 1K"
```

其他命令：`bin/balance.sh`（查余额）、`bin/recharge.sh --points 5`（充值）、`bin/jobs.sh`（查历史任务）、`bin/check-update.sh` / `bin/upgrade.sh`（检查与安装更新）。
Windows 没有 bash 时，用 `python scripts/client.py <子命令>` 等价调用，详见包内 `INSTALL.md`。

## 怎么用

```text
用 Chat2image 画一只橘猫，坐在窗台上，午后阳光，写实照片风格，2K
```

第一次用会问几个关键点（用途、画风等）；给了参考图会先问你要参考哪一部分。整理出的提示词会先给你确认，**你确认后才真正生成**。

## 它会写什么、不写什么

- 只在 `~/.config/chat2image/` 存两样东西：本机安装标识、访问凭证（权限 600）。**删掉这个目录 = 这台机器的身份丢了**。
- 只往 Skill 目录放一个 `.env`，里面只有中转站地址；上游服务的 key **永远不在这台机器上**。
- 不读取你的聊天内容，只处理你这次要生成的提示词与参考图。

## 更新

问你用的 Agent「查一下 Chat2image 的更新情况」即可。**更新由你决定**，不会自动装。

能不能检出更新，取决于服务端当前发布的版本；服务端没发新版本时，会提示已经是最新。

## 仓库与许可

本仓库开源的是**客户端**（MIT，见 [LICENSE](LICENSE)）；背后的生成服务与计费系统不开源，使用服务按张计费。
包的正式来源只有上面三个 Releases，其他渠道拿到的包我们不做保证。
