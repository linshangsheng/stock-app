# 部署到云服务器（Ubuntu 22.04）

> **这份清单我没有在真实的服务器上跑过**（只在本机验证了代码和测试）。每一步都先照着做、看输出，哪一步和下面写的不一样就停下来看，不要硬往下走。

## 先想清楚的几件事

- **升级靠 git**：服务器上的软件目录是 `git clone` 下来的，页面上点「升级」是从 GitHub 拉新代码。所以**每次改完要先在你电脑上 `git push` 到 `main`**，云上才有新版本可升。
- **只监听本机**：软件保持 `127.0.0.1:8000`，对外只开 80 / 443（Caddy）和 22（SSH）。不要开 8000。
- **第一次登录必须马上改掉默认密码**（`root` / `123456`）；从公网登录时软件会强制你改。
- 账号没有「忘记密码」找回，管理员自己的密码要记住。

## 一、装环境（用有 sudo 的普通用户，下面以 `deploy` 为例）

```bash
sudo apt update && sudo apt install -y git python3-venv python3-pip build-essential
```

## 二、拿代码并装依赖

仓库是私有的话，要先给服务器配一把只读的「部署密钥」：`ssh-keygen -t ed25519`，把 `~/.ssh/id_ed25519.pub` 加到 GitHub 仓库的 Settings → Deploy keys（只读就够）。然后：

```bash
ssh-keyscan github.com >> ~/.ssh/known_hosts      # 让服务器信任 GitHub（升级时没法弹确认，不做这步会报 Host key verification failed）
git clone git@github.com:linshangsheng/stock-app.git ~/stock-app
cd ~/stock-app
python3 -m venv .venv
.venv/bin/pip install -r server/requirements.txt
```

> Python：我在 3.12 上开发和测试，Ubuntu 22.04 自带 3.10，**3.10 我没跑过**。装完先 `.venv/bin/python -m server.main` 启动一次，看有没有报错；有报错就装 3.12（deadsnakes 源）再重建 `.venv`。

## 三、数据

- 想沿用你电脑上的数据（行情库 1.6 GB + 个人库）：先停掉服务器上的软件，把电脑 `data` 文件夹里的 `*.db` 用 `scp` 传到服务器 `~/stock-app/data/`（个人库 `portfolio.db` 就是 root 账号的数据）。
- 不传的话，到页面「设置 → 数据更新」里初始化（A 股全量要几个小时，港股约几分钟）。
- **在服务器上先测一下数据源能不能连**：东方财富、蛋卷基金、雅虎财经（港股个股和美股）。连不上的话对应的功能会更新失败。

## 四、做成服务（开机自启 + 页面上点升级 / 重启能用）

```bash
sudo cp deploy/stock-app.service /etc/systemd/system/
# 如果你的用户名或路径不是 deploy / /home/deploy/stock-app，先改这个文件里的 User、WorkingDirectory、ExecStart
sudo systemctl daemon-reload
sudo systemctl enable --now stock-app
systemctl status stock-app            # 应该是 active (running)
journalctl -u stock-app -n 50         # 看日志
```

**为什么要 `STOCKAPP_SUPERVISED=1` 和 `Restart=always`**：页面上点「升级 / 重启」时，软件会自己退出（退出码 3），由 systemd 重新拉起。没有这两个设置的话，软件会尝试自己拉一个新进程再退出，在 systemd 下要么服务直接停掉（网站下线），要么新进程被一起杀掉。

## 五、HTTPS（Caddy）

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile      # 先把里面的「你的域名」换成真实域名
sudo systemctl reload caddy
```

（以上是 Caddy 官方文档里的安装方法，我没在你的服务器上验证。）阿里云 / 腾讯云的**安全组**只放行 22、80、443。浏览器打开 `https://你的域名`，用 `root` / `123456` 登录 → 马上改密码。HTTPS 配好后，「设置 → 我的账号」里的「安装」卡片会变成一键安装。

## 六、以后怎么更新

1. 在你电脑上改完、测完、提交，然后 `git push` 到 `main`。
2. 打开云上的网页，用 root 登录 → 设置 → 软件升级：先「检查更新」，有新提交就点「立即升级并重启」。进度环走完，页面自己刷新，大概十几秒。已登录的人不会被踢下线（会话存在数据库里）。

**升级会做的**：`git fetch` → 依赖清单变了就先 `pip install`（装失败时代码还没动）→ `git merge --ff-only`（只快进，不覆盖）→ 让 systemd 重启软件。`data/` 不在 git 里，升级不碰。

**升级做不到的**（要登录服务器手动做）：
- 操作系统和 Python 本身的更新、新装的系统软件包；
- 改 `/etc/caddy/Caddyfile`、`stock-app.service`（改完要 `sudo systemctl daemon-reload` / `reload caddy`）；
- 服务器上的软件目录里有你手改过的文件、而且新版本也改了它们：升级会停下来并说明是哪些文件（先 `git checkout -- 文件` 还原，或者提交）；
- 本地提交没推送、远程又有新提交（分叉）：升级会拒绝（怕丢提交）。服务器上不要直接改代码、不要提交。
- 有任务在跑（初始化、盘后任务链、备份、回测）时，升级会先问你，因为重启会中断它们。

## 七、备份

软件每天盘后任务链跑完会备份到 `data/backups/`（个人库、账号库、每个账号的个人库、不可重建的行情部分；A 股的「自动更新」要开着，默认是开的），**但这在同一块硬盘上，服务器坏了就一起没了**。定期把 `data/backups/` 用 `scp` / `rsync` 拉到你电脑上，或者在云服务商那里开快照。
