# SEMI-KB Console 项目迁移指南 — Windows

## 一、环境要求

### 必需软件

1. **Python 3.10+**
   - 下载：https://www.python.org/downloads/
   - 安装时勾选 "Add Python to PATH"
   - 验证：`python --version`

2. **Node.js 18+ 和 npm**
   - 下载：https://nodejs.org/
   - 推荐 LTS 版本
   - 验证：`node --version` 和 `npm --version`

3. **PostgreSQL 14+**
   - 下载：https://www.postgresql.org/download/windows/
   - 或使用 PostgreSQL 官方安装程序（包含 pgAdmin）
   - 安装后记住超级用户密码

4. **PowerShell 5.1+**（Windows 10/11 自带）
   - 验证：`$PSVersionTable.PSVersion`

5. **Git**（可选，用于克隆仓库）
   - 下载：https://git-scm.com/download/win

### 可选软件

- **Visual Studio Code**（推荐的代码编辑器）
- **PostgreSQL pgAdmin**（图形化数据库管理工具，通常随 PostgreSQL 安装）

---

## 二、数据库准备

### 1. 创建数据库和用户

以管理员身份打开 PowerShell 或 cmd，进入 PostgreSQL 的 bin 目录（通常在 `C:\Program Files\PostgreSQL\<版本>\bin`）：

```powershell
# 登录 PostgreSQL（会提示输入密码）
.\psql -U postgres

# 在 psql 提示符下执行以下 SQL：
CREATE USER semi_kb_console WITH PASSWORD 'your_secure_password';
CREATE DATABASE semi_kb_console OWNER semi_kb_console;
\q
```

**重要**：记住你设置的密码，下一步要用。

### 2. 设置环境变量

在 PowerShell 中设置用户级环境变量：

```powershell
[Environment]::SetEnvironmentVariable('SEMI_KB_DB_PASSWORD', 'your_secure_password', 'User')
```

**或者**通过"系统属性 → 环境变量"图形界面添加用户变量 `SEMI_KB_DB_PASSWORD`。

设置后，**重启 PowerShell** 让环境变量生效。验证：

```powershell
$env:SEMI_KB_DB_PASSWORD
```

---

## 三、项目迁移步骤

假设你已经拿到项目压缩包或 Git 仓库。

### 1. 解压/克隆项目

```powershell
# 如果是压缩包，解压到目标目录（例如 D:\Projects\semi-kb-console）
# 如果是 Git 仓库：
git clone <仓库地址> D:\Projects\semi-kb-console
cd D:\Projects\semi-kb-console
```

### 2. 配置 `.env` 文件

复制示例配置并按需修改：

```powershell
Copy-Item .env.example .env
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

### 3. 迁移引擎数据

项目自带 `engine-seed` 目录作为初始引擎数据。首次部署执行：

```powershell
.\scripts\migrate-engine.ps1
```

**如果从旧版 `semi-kb` 项目迁移**，可以指定源路径（仅执行一次）：

```powershell
.\scripts\migrate-engine.ps1 -Source D:\old-projects\semi-kb
```

这会把旧项目的本体、经营模型、仿真、知识库等数据复制到 `data\engine`。迁移完成后，运行时不再依赖旧项目。

### 4. 初始化 PostgreSQL 数据库

```powershell
.\scripts\migrate-postgres.ps1
```

这会：
- 安装 Python 依赖（如果 venv 不存在）
- 执行 Alembic 数据库迁移（创建表结构）
- 验证 PostgreSQL 连接

**仅在从旧 SQLite 数据库导入时**，额外带参数：

```powershell
.\scripts\migrate-postgres.ps1 -MigrateSqlite -SqliteSource .\data\console.db
```

### 5. 启动后端和前端

```powershell
.\scripts\dev.ps1
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

### 6. 首次设置

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

```powershell
.\scripts\dev.ps1
```

### 停止项目

```powershell
.\scripts\stop.ps1
```

### 运行测试

```powershell
.\scripts\test.ps1
```

测试使用 SQLite 内存数据库，不影响 PostgreSQL 正式数据。

### 查看日志

后端和前端都在后台运行（`-WindowStyle Hidden`），日志输出不直接显示在终端。查看方法：

- **后端日志**：可在 `backend` 目录下临时修改 `run.py`，取消 `log_config` 注释，改为前台运行。
- **前端日志**：浏览器控制台（F12 → Console）。

---

## 五、常见问题

### Q1: `dev.ps1` 报错"无法加载，因为禁止运行脚本"

PowerShell 执行策略限制。以管理员身份运行 PowerShell：

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

### Q2: PostgreSQL 连接失败 "password authentication failed"

检查：
1. 环境变量 `SEMI_KB_DB_PASSWORD` 是否正确：`$env:SEMI_KB_DB_PASSWORD`
2. 数据库用户和密码是否一致：重新设置密码或环境变量
3. PostgreSQL 服务是否启动：`services.msc` 查看 `postgresql-x64-<版本>` 服务状态

### Q3: 后端启动失败，提示"缺少控制台本地引擎"

先执行 `.\scripts\migrate-engine.ps1` 初始化引擎数据。

### Q4: 前端页面空白或报 API 错误

1. 检查后端是否启动：`curl http://127.0.0.1:8765/api/health`
2. 检查浏览器控制台（F12）的错误信息
3. 确认防火墙没有阻止 8765 或 5173 端口

### Q5: 企微机器人不回复

1. 检查 `netstat -ano | findstr ":443"` 是否有到 `112.90.14.200:443` 的 `ESTABLISHED` 连接（WeCom 长连接）
2. 检查"系统设置"里企微 Bot ID 和 Secret 是否正确
3. 查看后端日志是否有 `WeCom AI bot authenticated` 或错误信息

### Q6: Python 依赖安装很慢

可以使用国内镜像加速：

```powershell
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

---

## 六、备份与恢复

### 备份 PostgreSQL 数据库

```powershell
pg_dump -U semi_kb_console -h localhost -d semi_kb_console > backup_$(Get-Date -Format 'yyyyMMdd_HHmmss').sql
```

### 恢复数据库

```powershell
psql -U semi_kb_console -h localhost -d semi_kb_console < backup_20260905_120000.sql
```

### 备份引擎数据

直接复制整个 `data\engine` 目录。

### 备份加密密钥

`data\.master.key` 是加密凭据的主密钥，**必须妥善保管**。丢失后无法解密已保存的 API Key 等凭据。

---

## 七、更新项目

如果从 Git 拉取更新：

```powershell
git pull origin main
.\scripts\dev.ps1
```

`dev.ps1` 会自动：
- 检查并安装新的 Python 依赖
- 检查并安装新的 npm 依赖
- 重启后端和前端

如果数据库结构有变化，启动时会自动执行 Alembic 升级（`main.py` lifespan 中已内置）。

---

## 八、生产部署建议

本指南面向开发和测试环境。生产部署需要额外考虑：

1. **HTTPS**：配置反向代理（Nginx / IIS）并启用 SSL 证书
2. **进程管理**：使用 Windows 服务或 NSSM 将后端注册为系统服务
3. **数据库**：PostgreSQL 配置远程访问、备份策略、性能优化
4. **密钥管理**：`.env` 和 `data\.master.key` 不得提交到版本控制
5. **日志**：配置集中日志收集（如 ELK / Splunk）
6. **监控**：后端健康检查端点 `/api/health`，配合监控系统（如 Prometheus）

---

**联系与支持**

遇到问题请检查项目 README.md 和本文档，或联系项目维护者。
