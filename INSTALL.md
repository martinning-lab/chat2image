请你帮我安装「聊天生图 chat2image」这个 Skill。按顺序做，任一步成功就停下。

【第 0 步 · 先查是否已装】
检查目录：~/.workbuddy/skills/chat2image/
如果已存在，读**已装目录里**的 version.json 的 version，再读**这次要装的包内** version.json 的 version，
两个比对（不要凭印象、也不要拿外面听来的版本号当依据）：
- 两者相同 → 已经是这一版了，直接跳到第 4 步自检与第 6 步冒烟，不要重装。
- 已装的更旧（或已装的 version.json 读不出来）→ 按第 3 步覆盖安装（保留 ~/.config/chat2image 不要删）。
- 已装的更新 → 停下问我，不要降级覆盖。

【关于来源 · 只认一个】
这个 Skill 会通过三个渠道分发（GitHub / Gitee / 我们自己的服务器），
但**一次只从一个来源拿**：来源由你收到的那条消息/提示词指定，里面会写明地址或文件名。
- 只从提示词里给的那个来源拿包，**不要自己换源**、不要"这个打不开就试试另一个"。
- 打不开就直接告诉人打不开，由人来决定换哪个来源——**自动换源会让"这个包是谁给的"变得不可追溯**。
- 三个渠道的包内容完全一致（同一个 zip 的哈希相同），所以从哪拿都不影响安装结果。

【第 1 步 · 拿到安装包】
安装包文件名：chat2image-0.2.10.zip
解压出来是 chat2image-0.2.10/ 这个目录（不是别的名字），后面的路径都按它算。
需要的文件就一个：chat2image-0.2.10.zip（大小以实际文件为准，几十 KB 量级）。
如果我是把 zip 发给你 / 放在下载目录，你在这些位置找最新的：
  ~/Downloads/WeChat Files/**/*.zip
  ~/Downloads/*.zip
  ~/Desktop/*.zip
找到多个就按修改时间取最新，先把文件名和大小报给我确认，再往下走。

【第 2 步 · 解压后核对文件完整】
- **不用去要任何校验值**：包里自带 SHA256SUMS.txt，列的是这个包内每个文件的 sha256。
- ⚠️ 别把两个名字搞混：
  · 发布页附件里的 `SHA256SUMS`（无扩展名）= **压缩包本身的哈希**，用来确认「下载到的 zip 没被换过」；
  · 包里的 `SHA256SUMS.txt` = 包内每个文件的哈希，**不会**有「zip 自己」那一行。
  两份各管一段：下载后先对前者，解压后再对后者。
- 先解压，然后 **cd 进解压出来的那个目录**（chat2image-0.2.10/）再跑校验——
  清单里是包内相对路径（如 bin/gen.sh），不在这一层跑会全部对不上。
- macOS / Linux 用这条：
    shasum -a 256 -c SHA256SUMS.txt      （没有 shasum 就用：sha256sum -c SHA256SUMS.txt）
- Windows 用这条（纯 PowerShell 没有 -c 的等价物，必须逐个比）：
    Get-Content .\SHA256SUMS.txt | ForEach-Object { $h,$f = $_ -split '\s+',2; $f=$f.TrimStart('*'); if(Test-Path $f){ $a=(Get-FileHash -Algorithm SHA256 $f).Hash.ToLower(); if($a -ne $h.ToLower()){ "不匹配: $f" } } else { "缺失: $f" } }
  （Windows 请在 **PowerShell 或 Git Bash** 里跑：cmd 不展开 ~，路径会找不到）
- 按结果分两档处理，**别一刀切**：
  ▸ **停下不要装**（明显损坏）：清单里报「缺失」，或关键文件缺失——version.json、
    SKILL.md、bin/gen.sh 任一不在；或三种校验命令都跑不了（command not found
    不等于通过，换另一种再试，都跑不了就停下告诉我「没法校验」）。
  ▸ **报一句警告后继续装**：文件齐全、且对不上的**不是**下面这些关键文件——
    `version.json`、`SKILL.md`、`bin/*.sh`、`scripts/client.py`、`install.sh`。
    这些以外（如示例图、文档）哈希不符，可能只是换行符差异，停下反而误伤；
    请明确告诉我「有 N 个文件哈希不符」，但接着做第 3 步。
  ▸ 🔴 **关键文件任一哈希不符 → 立刻停止，不要装**。理由很直接：这个 Skill 要收钱，
    校验不能是装饰——攻击者只要保住文件齐全，就能让哈希全不符还照装成功。
- 这份清单能证明什么、不能证明什么：能证明**解压出来的文件彼此自洽、传输中没损坏**；
  它**证明不了**「整个 zip 没被换成另一个包」——那需要包外另给一个 zip 哈希，按决定不做。

【第 3 步 · 解压安装】
- 解压到 ~/.workbuddy/skills/，确保最终路径是 ~/.workbuddy/skills/chat2image/
  （Windows 上是 %USERPROFILE%\.workbuddy\skills\chat2image\）
- 先确认有没有 bash：`bash --version`。
  ▸ 有 bash（macOS / Linux / Windows 装了 Git Bash 或 WSL）→ 进入该目录运行一次：bash install.sh
  ▸ **没有 bash（纯 PowerShell 的 Windows 常见）** → 不要卡住，按下面三件事手工做，
    它们就是 install.sh 做的全部：
      1. 把 .env.example 复制成 .env（里面只有中转站地址，不用改）
      2. 建配置目录：%USERPROFILE%\.config\chat2image\
      3. 不用改权限（Windows 不需要 chmod）
    并且**后面的命令改用 python 直调**（脚本调用的是同一份 client.py，等价）：
      python scripts/client.py welcome         ← 代替 bin/welcome.sh（首次进入判要不要打开欢迎页）
      python scripts/client.py doctor          ← 代替 bin/doctor.sh
      python scripts/client.py balance --json  ← 代替 bin/balance.sh
      python scripts/client.py gen --prompt "描述" --resolution 1k --aspect 3:4
                                                ← 代替 bin/gen.sh
      python scripts/client.py recharge --points 1
                                                ← 代替 bin/recharge.sh（余额不足要充值时）
    前提只有 Python 3.8+；找不到 python 就先装 Python。
- 它会建 .env（里面只有中转站地址）、给脚本加执行权限、建 ~/.config/chat2image 配置目录。
- 注意：不要修改 .env 里的 BASE_URL，也不要往里加任何上游服务商的 key——客户端根本用不到。

【第 4 步 · 自检】
运行：~/.workbuddy/skills/chat2image/bin/doctor.sh
     （没有 bash 时：python scripts/client.py doctor）
- 全绿 → 告诉我「装好了。想画图就直接说『用 Chat2image 帮我生成图片』，比如『用 Chat2image 画只橘猫当手机壁纸』」。
- 有 ❌ → 把 ❌ 那几行原文贴给我，不要自己猜着修。

【第 5 步 · 首次使用（说明，不用你做）】
- 用户第一次正式提需求时，Skill 会用侧边栏浏览器打开一个欢迎页（海报 + 全部说明都在页面里），
  只打开一次；以后不再打扰。
- 你**不要**提前替用户跑 welcome 命令，也不要让用户手动触发；那是 Skill 自己的第一步。
- 只有「还没有 key」这一条 ⚠️ 是正常的，第一次出图会自动建号。

【第 6 步 · 冒烟验证】
运行：~/.workbuddy/skills/chat2image/bin/balance.sh --json
     （没有 bash 时：python scripts/client.py balance --json）
- 能返回余额（哪怕是 0 点）就算装通了。
- 返回 NETWORK / 服务不可达 → 报告具体错误，不要反复重试。

【注意】
- 不需要注册、不需要填任何 key、不需要装任何依赖（只要 Python 3.8+）。
- 不要删除 ~/.config/chat2image/：里面有你的身份和 key，删了会变成新账号。
- WorkBuddy 没有「给个 Git 地址就自动装」的功能，只能下载解压，所以第 2 步的校验不能跳过。
