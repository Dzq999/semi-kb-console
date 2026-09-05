# SEMI-KB Console 项目迁移指南 — macOS

## 一、环境要求

### 必需软件

1. **Python 3.10+**
   - macOS 通常自带 Python，但版本可能较旧
   - 推荐使用 Homebrew 安装：
     ```bash
     brew install python@3.10
     ```
   - 验证：`python3 --version`

2. **Node.js 18+ 和 npm**
   - 使用 Homebrew 安装：
     ```bash
     brew install node
     ```
   - 验证：`node --version` 和 `npm --version`

3. **PostgreSQL 14+**
   - 使用 Homebrew 安装：
     ```bash
     brew install postgresql@14
     brew services start postgresql@14
     ```
   - 或下载图形化安装器：https://www.postgresql.org/download/macosx/
   - 验证：`psql --version`

4. **Git**（可选，用于克隆仓库）
   - macOS 自带，或通过 Xcode Command Line Tools 安装：
     ```bash
     xcode-select --install
     ```

### 推荐软件

- **Homebrew**（macOS 包管理器）：https://brew.sh/
- **Visual Studio Code**（代码编辑器）
- **Postico** 或 **pgAdmin**（PostgreSQL 图形化管理工具）

---

## 二、数据库准备

### 1. 创建数据库和用户

打开终端，执行以下命令：

```bash
# 进入 PostgreSQL 控制台（Homebrew 安装的 PostgreSQL 默认用当前系统用户）
psql postgres

# 在 psql 提示符下执行以下 SQL：
CREATE USER semi_kb_console WITH PASSWORD 'your_secure_password';
CREATE DATABASE semi_kb_console OWNER semi_kb_console;
\q
```

**重要**：记住你设置的密码，下一步要用。

### 2. 设置环境变量

编辑你的 shell 配置文件（根据使用的 shell 不同）：

**对于 zsh（macOS Catalina 及更高版本默认）**：

```bash
echo 'export SEMI_KB_DB_PASSWORD="your_secure_password"' >> ~/.zshrc
source ~/.zshrc
```

**对于 bash**：

```bash
echo 'export SEMI_KB_DB_PASSWORD="your_secure_password"' >> ~/.bash_profile
source ~/.bash_profile
```

验证：

```bash
echo $SEMI_KB_DB_PASSWORD
```

---

## 三、项目迁移步骤

假设你已经拿到项目压缩包或 Git 仓库。

### 1. 解压/克隆项目

```bash
# 如果是压缩包，解压到目标目录（例如 ~/Projects/semi-kb-console）
# 如果是 Git 仓库：
git clone <仓库地址> ~/Projects/semi-kb-console
cd ~/Projects/semi-kb-console
```

### 2. 配置 `.env` 文件

复制示例配置并按需修改：

```bash
cp .env.example .env
```

用文本编辑器打开 `.env`，检查以下配置项：

```env
# PostgreSQL 连接配置（用户名和数据库名默认都是 semi_kb_console）
SEMI_KB_DB_HOST=localhost
SEMI_KB_DB_PORT=5432
SEMI_KB_DB_NAME=semi_kb_console
SEMI_KB_DB_USER=semi_kb_console
# 密码从环境变量 SEMI_KB_DB_PASSWORD 读取，不要写在这里

# 可选：LLM API 配置（首次启动后也可在前端"系统设置"页录入）
# ANTHROPIC_API_KEY=sk-ant-...
```

**警告**：不要把密码、API Key 等敏感信息直接写入 `.env` 文件并提交到版本控制！

### 3. 给脚本添加执行权限

```bash
chmod +x scripts/*.sh
```

### 4. 迁移引擎数据

项目自带 `engine-seed` 目录作为初始引擎数据。首次部署执行：

```bash
./scripts/migrate-engine.sh
```

**如果从旧版 `semi-kb` 项目迁移**，可以指定源路径（仅执行一次）：

```bash
./scripts/migrate-engine.sh --source ~/old-projects/semi-kb
```

这会把旧项目的本体、经营模型、仿真、知识库等数据复制到 `data/engine`。迁移完成后，运行时不再依赖旧项目。

### 5. 初始化 PostgreSQL 数据库

```bash
./scripts/migrate-postgres.sh
```

这会：
- 创建 Python 虚拟环境（`.venv`）
- 安装 Python 依赖（`backend/requirements.txt`）
- 执行 Alembic 数据库迁移（创建表结构）
- 验证 PostgreSQL 连接

**仅在从旧 SQLite 数据库导入时**，额外带参数：

```bash
./scripts/migrate-postgres.sh --migrate-sqlite --sqlite-source ./data/console.db
```

### 6. 启动后端和前端

```bash
./scripts/dev.sh
```

这个脚本会：
- 自动创建 Python 虚拟环境（`.venv`）
- 安装 Python 依赖（`backend/requirements.txt`）
- 安装前端依赖（`npm install`）
- 后台启动 FastAPI 后端（端口 8765）
- 后台启动 Vite 前端开发服务器（端口 5173）

启动完成后，访问：

- **前端**：http://127.0.0.1:5173
- **后端 API 文档**：http://127.0.0.1:8765/docs

### 7. 首次设置

1. 浏览器打开 http://127.0.0.1:5173
2. 首次进入会提示创建管理员账号，设置用户名和密码
3. 登录后，进入"系统设置"页面，录入：
   - **LLM API Key**（Anthropic API Key，必需，用于问答和智能编排）
   - **企业微信机器人凭据**（可选，如需接入企微群问答）
   - **QQ 邮箱 SMTP 授权码**（可选，如需自动发送日报邮件）

所有凭据由后端加密保存（Fernet），前端不回显明文。

---

## 四、日常使用

### 启动项目

```bash
./scripts/dev.sh
```

### 停止项目

```bash
./scripts/stop.sh
```

### 运行测试

```bash
./scripts/test.sh
```

测试使用 SQLite 内存数据库，不影响 PostgreSQL 正式数据。

### 查看日志

后端和前端都在后台运行，可以通过以下方式查看进程：

```bash
# 查看后端进程
lsof -iTCP:8765 -sTCP:LISTEN

# 查看前端进程
lsof -iTCP:5173 -sTCP:LISTEN

# 查看进程日志（如果需要前台运行以查看输出）
cd backend
../.venv/bin/python run.py
```

---

## 五、常见问题

### Q1: `./scripts/dev.sh` 报错 "Permission denied"

脚本没有执行权限。执行：

```bash
chmod +x scripts/*.sh
```

### Q2: PostgreSQL 连接失败 "password authentication failed"

检查：
1. 环境变量 `SEMI_KB_DB_PASSWORD` 是否正确：`echo $SEMI_KB_DB_PASSWORD`
2. 数据库用户和密码是否一致
3. PostgreSQL 服务是否启动：
   ```bash
   brew services list | grep postgresql
   # 如果未启动：
   brew services start postgresql@14
   ```

### Q3: 后端启动失败，提示"缺少控制台本地引擎"

先执行 `./scripts/migrate-engine.sh` 初始化引擎数据。

### Q4: `lsof: command not found`

macOS 应该自带 `lsof`。如果缺失，检查是否安装了 Xcode Command Line Tools：

```bash
xcode-select --install
```

或者使用 `netstat` 替代（需要安装 `net-tools`）。

### Q5: Python 版本不匹配

macOS 系统自带的 Python 可能是 2.7 或较旧的 3.x。确保使用 Python 3.10+：

```bash
python3 --version
# 如果版本过低：
brew install python@3.10
```

脚本已配置使用 `python3` 命令，会自动找到 Homebrew 安装的版本。

### Q6: 企微机器人不回复

1. 检查是否有到企微服务器的连接：
   ```bash
   lsof -iTCP -sTCP:ESTABLISHED | grep 112.90.14.200
   ```
   应该看到 1 条稳定连接。
2. 检查"系统设置"里企微 Bot ID 和 Secret 是否正确
3. 查看后端日志是否有 `WeCom AI bot authenticated` 或错误信息

### Q7: npm 依赖安装很慢

可以使用国内镜像加速：

```bash
npm config set registry https://registry.npmmirror.com
npm install --prefix frontend
```

### Q8: M1/M2 芯片兼容性问题

Apple Silicon（M1/M2）芯片使用 ARM 架构，大部分 Python 包都有原生支持。如果遇到兼容性问题：

```bash
# 确保使用 ARM64 原生 Python
arch -arm64 brew install python@3.10

# 或者在 Rosetta 2 模式下安装（兼容性更好，但性能略低）
arch -x86_64 brew install python@3.10
```

---

## 六、备份与恢复

### 备份 PostgreSQL 数据库

```bash
pg_dump -U semi_kb_console -h localhost -d semi_kb_console > backup_$(date +%Y%m%d_%H%M%S).sql
```

如果需要输入密码，先设置 `PGPASSWORD` 环境变量：

```bash
export PGPASSWORD=$SEMI_KB_DB_PASSWORD
pg_dump -U semi_kb_console -h localhost -d semi_kb_console > backup_$(date +%Y%m%d_%H%M%S).sql
```

### 恢复数据库

```bash
psql -U semi_kb_console -h localhost -d semi_kb_console < backup_20260905_120000.sql
```

### 备份引擎数据

直接复制整个 `data/engine` 目录：

```bash
cp -r data/engine data/engine_backup_$(date +%Y%m%d)
```

### 备份加密密钥

`data/.master.key` 是加密凭据的主密钥，**必须妥善保管**。丢失后无法解密已保存的 API Key 等凭据。

```bash
# 备份到安全位置（不要提交到 Git）
cp data/.master.key ~/secure_backup/semi-kb-console-master.key
```

---

## 七、更新项目

如果从 Git 拉取更新：

```bash
git pull origin main
./scripts/dev.sh
```

`dev.sh` 会自动：
- 检查并安装新的 Python 依赖
- 检查并安装新的 npm 依赖
- 重启后端和前端

如果数据库结构有变化，启动时会自动执行 Alembic 升级（`main.py` lifespan 中已内置）。

---

## 八、生产部署建议

本指南面向开发和测试环境。生产部署需要额外考虑：

1. **HTTPS**：配置反向代理（Nginx）并启用 SSL 证书（Let's Encrypt）
2. **进程管理**：使用 systemd 或 launchd 将后端注册为系统服务
   - macOS launchd 示例：创建 `~/Library/LaunchAgents/com.semi-kb.backend.plist`
3. **数据库**：PostgreSQL 配置远程访问、备份策略（cron + pg_dump）、性能优化
4. **密钥管理**：`.env` 和 `data/.master.key` 不得提交到版本控制
5. **日志**：配置集中日志收集（如 ELK / Splunk）
6. **监控**：后端健康检查端点 `/api/health`，配合监控系统（如 Prometheus + Grafana）

### 使用 systemd（Linux 服务器）

如果部署到 Linux 服务器，可以创建 systemd 服务：

```ini
# /etc/systemd/system/semi-kb-backend.service
[Unit]
Description=SEMI-KB Console Backend
After=network.target postgresql.service

[Service]
Type=simple
User=your-user
WorkingDirectory=/path/to/semi-kb-console/backend
Environment="SEMI_KB_DB_PASSWORD=your_password"
ExecStart=/path/to/semi-kb-console/.venv/bin/python run.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

启用服务：

```bash
sudo systemctl daemon-reload
sudo systemctl enable semi-kb-backend
sudo systemctl start semi-kb-backend
```

---

## 九、macOS 特定注意事项

### 1. Gatekeeper 和安全性

首次运行某些命令时，macOS 可能会弹出安全警告。在"系统偏好设置 → 安全性与隐私"中允许运行。

### 2. 文件权限

macOS 对某些目录有特殊权限保护。确保项目目录在用户目录下（如 `~/Projects`），避免放在系统目录。

### 3. PostgreSQL 数据目录

Homebrew 安装的 PostgreSQL 数据目录在：

```bash
/opt/homebrew/var/postgresql@14  # Apple Silicon
# 或
/usr/local/var/postgresql@14      # Intel
```

### 4. 端口占用

如果 8765 或 5173 端口被其他程序占用：

```bash
# 查看占用端口的进程
lsof -iTCP:8765 -sTCP:LISTEN
lsof -iTCP:5173 -sTCP:LISTEN

# 杀死进程（替换 <PID> 为实际进程 ID）
kill <PID>
```

---

**联系与支持**

遇到问题请检查项目 README.md 和本文档，或联系项目维护者。

**macOS 常用快捷键**：
- 打开终端：Command + Space，输入 "Terminal"
- 查看隐藏文件：Command + Shift + . （在 Finder 中）
- 强制退出程序：Command + Option + Esc
