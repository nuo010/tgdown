# tgdown — Telegram 视频下载

基于 Telethon 的 Telegram 群视频自动下载工具：监听指定群内的视频消息并自动下载到本地，支持 Web 状态面板、下载记录分页查询、可选代理与 AI 命名。

---

## 功能概览

- **群消息监听**：在指定 Telegram 群中，有视频消息时自动加入下载队列并下载
- **链接解析**：群消息正文中的 `t.me/用户名/消息ID` 链接会自动解析并加入下载队列
- **状态推送**：下载开始/完成/失败时可推送到该群（可关闭）
- **Web 面板**：实时查看正在下载、未下载列表、最近记录；数据库记录支持分页
- **持久化**：下载成功记录写入 SQLite，支持分页查询
- **可选代理**：支持 SOCKS5/SOCKS4/HTTP 代理
- **可选 AI 命名**：配置 OpenAI 兼容 API 后，可根据消息文案或原文件名生成更友好的本地文件名

---

## 环境要求

- Python 3.10+
- Telegram API 凭证：在 [my.telegram.org](https://my.telegram.org) 申请 `api_id` 与 `api_hash`

---

## 配置说明

所有配置存放在 **`data/config.json`**（本地运行时为项目下的 `data` 目录；Docker 运行时为挂载的 `/data`）。

### 字段说明

| 配置项 | 是否必填 | 默认值 | 说明 |
|--------|----------|--------|------|
| `api_id` | 是 | 无 | Telegram 应用 `api_id` |
| `api_hash` | 是 | 无 | Telegram 应用 `api_hash` |
| `target_group_name` | 否 | `"downapp"` | 监听的目标群名称，需与群标题一致 |
| `download_path` | 否 | `"./downloads"` | 下载保存目录，相对路径基于 `data` 的父目录 |
| `web_port` | 否 | `8765` | Web 面板端口 |
| `web_bind` | 否 | `"0.0.0.0"` | Web 绑定地址，仅本机访问可填 `127.0.0.1` |
| `concurrent_downloads` | 否 | `3` | 并发下载数 |
| `push_status_to_group` | 否 | `true` | 是否把状态消息推送到目标群 |
| `download_retries` | 否 | `2` | 下载失败或卡住时的重试次数 |
| `download_stall_seconds` | 否 | `600` | 连续多少秒无进度视为卡住并重试，`0` 表示关闭 |
| `cron_send_current_time_cron` | 否 | `""` | 定时发送当前时间的 cron 表达式，支持 5 或 6 字段 |
| `cron_push_download_progress_cron` | 否 | `""` | 定时推送下载进度的 cron 表达式，仅在有任务下载时推送 |
| `openai_api_key` | 否 | `""` | OpenAI 兼容接口的 API Key，用于 AI 命名 |
| `openai_base_url` | 否 | `""` | OpenAI 兼容接口地址，例如 `https://api.openai.com/v1` |
| `tg_proxy_type` | 否 | `""` | Telegram 代理类型，可选 `socks5`、`socks4`、`http` |
| `tg_proxy_host` | 否 | `""` | Telegram 代理地址 |
| `tg_proxy_port` | 否 | `0` | Telegram 代理端口 |
| `tg_proxy_username` | 否 | `""` | Telegram 代理用户名，没有可留空 |
| `tg_proxy_password` | 否 | `""` | Telegram 代理密码，没有可留空 |

### 配置示例

```json
{
  "api_id": 368,
  "api_hash": "e1ffa7e97d1545eb2d",
  "download_path": "./downloads",
  "web_port": 8765,
  "web_bind": "0.0.0.0",
  "target_group_name": "down",
  "concurrent_downloads": 3,
  "push_status_to_group": true,
  "download_retries": 2,
  "download_stall_seconds": 600,
  "cron_send_current_time_cron": "*/5 * * * *",
  "cron_push_download_progress_cron": "*/1 * * * *",
  "openai_api_key": "",
  "openai_base_url": "",
  "tg_proxy_type": "",
  "tg_proxy_host": "",
  "tg_proxy_port": 7893,
  "tg_proxy_username": "",
  "tg_proxy_password": ""
}

```

---

## 部署

## Docker 部署

```bash
cd tgdown
# 第一次使用需要初始化session信息，运行脚本后，输入手机号然后发送验证码，用验证码登录成功后获取到session就可以了
# 第一次使用先手动在挂载目录下创建配置文件 config.json 然后运行容器
docker run --rm -it \
  --name tgdown \
  --network host \
  -v "$(pwd)/data:/data" \
  xxgl/tgdown:1.0
```
![init](/docs/images/init.png)
![yzm](/docs/images/yzm.png)
![session](/docs/images/session.png)
```bash
cd tgdown
# 运行（挂载 data 和 downloads 目录，端口 8765）
docker run -d \
  --name tgdown \
  --network host \
  -v "$(pwd)/data:/data" \
  -v "$(pwd)/downloads:/downloads" \
  --restart=always \
  xxgl/tgdown:1.0
```
下载文件名称的两种格式
- 1、根据文案ai生成：文件名+时间
- 2、空文案：时间+视频文件名


![qun](/docs/images/qun.png)
![down](/docs/images/down.png)

## 许可证

见项目内 [LICENSE](LICENSE) 文件。
