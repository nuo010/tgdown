# Docker 运行说明（docker run）

## 1. 前提

- 已安装 [Docker](https://docs.docker.com/get-docker/)

## 2. 准备目录与配置

在宿主机上准备好两个目录：**data**（配置与会话）、**downloads**（下载文件），且与 data 同级。将 `config.json` 放入 data 目录并填好 `api_id`、`api_hash` 等。

示例（按你实际路径替换）：

```bash
mkdir -p /path/to/data /path/to/downloads
# 将 config.json 放到 /path/to/data/ 下
```

## 3. 构建镜像

在 **down 项目目录** 下执行：

```bash
cd /path/to/Python/downapp
docker build -t downapp:1.4 .
```

## 4. 首次登录（前台，完成 Telegram 登录）

必须先交互运行一次，按提示输入手机号和验证码，会话会写入挂载的 data 目录：

```bash
docker run --rm -it \
  -p 8765:8765 \
  -v /path/to/data:/data \
  -v /path/to/downloads:/downloads \
  downapp:1.4
```

看到 “Signed in successfully” 后按 **Ctrl+C** 退出。

## 5. 后台运行

登录成功后，以后用后台方式启动：

```bash
docker run -d --name downapp \
  -p 8765:8765 \
  -v /data/server/downapp/data:/data \
  -v /data/yp/downloads:/downloads \
  --restart=always \
  downapp:1.5
```

- **Web 界面**：浏览器打开 `http://本机IP:8765`
- **配置与会话**：在宿主机的 `data` 目录
- **下载文件**：在宿主机的 `downloads` 目录

## 6. 常用命令

| 操作     | 命令 |
|----------|------|
| 查看日志 | `docker logs -f telegram-down` |
| 停止     | `docker stop telegram-down` |
| 删除容器 | `docker rm telegram-down` |
| 重启     | `docker restart telegram-down` |
