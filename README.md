# tgdown — Telegram 视频下载

基于 Telethon 的 Telegram 群视频自动下载工具：监听指定群内的视频消息并自动下载到本地，支持 Web 状态面板、下载记录分页查询、可选代理与 AI 命名。

---

## 功能概览

- **群消息监听**：在指定 Telegram 群中，有视频消息时自动加入下载队列并下载
- **链接解析**：群消息正文中的 `t.me/用户名/消息ID` 链接会自动解析并加入下载队列
- **状态推送**：下载开始/完成/失败时可推送到该群（可关闭）
- **Web 面板**：实时查看正在下载、未下载列表、最近记录；数据库记录支持分页
- **持久化**：下载成功记录写入 SQLite，支持分页查询
- **可选代理**：支持 SOCKS5/SOCKS4/HTTP 代理（需安装 PySocks）
- **可选 AI 命名**：配置 OpenAI 兼容 API 后，可根据消息文案或原文件名生成更友好的本地文件名

---

## 环境要求

- Python 3.10+
- Telegram API 凭证：在 [my.telegram.org](https://my.telegram.org) 申请 `api_id` 与 `api_hash`

---

## 配置说明

所有配置存放在 **`data/config.json`**（本地运行时为项目下的 `data` 目录；Docker 运行时为挂载的 `/data`）。

### 必填项

| 配置项     | 说明 |
|------------|------|
| `api_id`   | Telegram 应用 api_id（整数） |
| `api_hash` | Telegram 应用 api_hash（字符串） |

### 常用可选项

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `target_group_name` | `"downapp"` | 监听的群组名称（需与群标题完全一致） |
| `download_path` | `"./downloads"` | 下载保存目录（相对路径基于 data 的父目录） |
| `web_port` | `8765` | Web 面板端口 |
| `web_bind` | `"0.0.0.0"` | 绑定地址，仅本机访问可填 `127.0.0.1` |
| `concurrent_downloads` | `3` | 并发下载数 |
| `push_status_to_group` | `true` | 是否把下载状态推送到目标群 |
| `download_retries` | `2` | 下载失败或卡住时的重试次数 |
| `download_stall_seconds` | `600` | 多少秒无进度视为卡住并重试；`0` 表示不检测 |

### 代理（可选）

需要代理时安装：`pip install pysocks`，并在配置中增加：

```json
"tg_proxy_type": "socks5",
"tg_proxy_host": "127.0.0.1",
"tg_proxy_port": 1080,
"tg_proxy_username": "",
"tg_proxy_password": ""
```

### AI 文件名（可选）

若需根据消息文案或原文件名生成更好看的文件名，在 `data/config.json` 中配置 OpenAI 兼容 API：

```json
"openai_api_key": "your-api-key",
"openai_base_url": "https://api.openai.com/v1"
```

未配置时不影响下载，仅使用时间戳等规则命名。

### 配置示例

```json
{
  "api_id": 12345678,
  "api_hash": "your_api_hash_string",
  "target_group_name": "downapp",
  "download_path": "./downloads",
  "web_port": 8765,
  "web_bind": "0.0.0.0",
  "concurrent_downloads": 3,
  "push_status_to_group": true,
  "download_retries": 2,
  "download_stall_seconds": 600
}
```

---

## 使用与部署

### 1. 克隆与依赖

```bash
cd /path/to/your/workdir
git clone <仓库地址> tgdown
cd tgdown
pip install -r requirements.txt
```

### 2. 准备配置与目录

- 在项目下创建 `data` 目录，放入 `config.json`（或先在项目根目录放 `config.json`，首次运行时会复制到 `data/`）
- 确保 `config.json` 中包含正确的 `api_id` 和 `api_hash`

```bash
mkdir -p data
# 将 config.json 放入 data/ 并编辑
```

### 3. 首次运行与登录

```bash
python app.py
```

首次运行会要求输入手机号、验证码等完成 Telegram 登录，会话会保存在 `data/session`，之后无需重复登录。

### 4. 访问 Web 面板

浏览器打开：`http://<服务器IP>:8765`（默认端口 8765）。

可查看：

- 正在下载、未下载列表、最近下载记录
- 下载成功记录（数据库）— 支持分页

---

## Docker 部署


```bash
cd tgdown

# 构建镜像
docker build -t tgdown:latest .

# 运行（挂载 data 目录，端口 8765）
docker run -d \
  --name tgdown \
  --network host \
  -v "$(pwd)/data:/data" \
  --restart=always \
  xxgl/tgdown:1.0
```

**注意**：

- 部署前务必在宿主机准备好 `data/config.json`（及已登录的 `data/session*` 若需沿用）
- 下载目录：若 `download_path` 为相对路径，会落在容器内；若需宿主机目录，可在 `config.json` 中写绝对路径并再挂载该卷






## 许可证

见项目内 [LICENSE](LICENSE) 文件。
