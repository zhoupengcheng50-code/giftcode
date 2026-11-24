# 越南后台 GMS 礼包码自动化配置工作台

🎮 支持多端自适应（手机、平板、电脑均可浏览与操作）的越南后台礼包码自动化工作台。

---

## ✨ 核心功能

1. **智能文档解析**：支持上传 `.docx` / `.xlsx` / `.txt` 或直接粘贴文字，自动识别 8 个档位及对应奖励道具与数量。
2. **多端自适应布局**：采用全新响应式设计，手机竖屏、平板和电脑桌面端均可顺畅浏览与操作。
3. **往期配置智能核对**：自动比对 GMS 后台往期配置，标红预警变动与新增道具。
4. **一键生成与合并分发**：自动化驱动批量生成各档礼包码，整合输出包含 8 个独立 Sheet 的标准 `.xls` 表格并支持手机/电脑直接下载。

---

## 🚀 部署与外网访问方式

### 方式一：上传到 GitHub 并一键免费部署（推荐，免登录外网直链）

本工作台已配备完整的 `Dockerfile` 与 `requirements.txt`，可直接推送到 GitHub，然后连接免费云平台托管，获得一个**永久、免登录、任何手机电脑随时点开可用**的外网链接。

#### 1. 推送到 GitHub
在当前目录打开终端，执行：
```bash
git init
git add .
git commit -m "feat: 越南礼包码自动化工作台"
git branch -M main
git remote add origin https://github.com/<你的用户名>/<你的仓库名>.git
git push -u origin main
```

#### 2. 一键部署到免费平台（以 Render 为例，免登录直接用）
1. 访问 [Render.com](https://render.com/)（或 [Zeabur](https://zeabur.com/)）。
2. 点击 **New +** -> **Web Service**。
3. 选择刚才推送的 GitHub 仓库。
4. 环境选择 **Docker**（平台会自动根据 Dockerfile 构建包含 Python 与 Playwright 的运行环境）。
5. 点击 **Deploy**。
6. 部署完成后，即可获得专属的永久外网 HTTPS 域名（例如 `https://vietnam-giftcode.onrender.com`），手机和电脑直接访问即可使用，完全免登录！

---

### 方式二：本地运行

双击同目录下的 `run.bat` 或 `启动工作台.bat`，即可在本机打开：
- 本机访问：`http://127.0.0.1:8888`
- 同 Wi-Fi 局域网内手机访问：`http://<电脑局域网IP>:8888`
