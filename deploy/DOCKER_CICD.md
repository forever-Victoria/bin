# bin 网关 Docker 与 CI/CD

## 方案说明

这套配置把代码、Python 运行时和依赖打进 Docker 镜像，但不会把真实 `.env` 打进
镜像或提交到 Git。生产服务器单独保存 `.env`，更新应用时只替换镜像。

流水线分为三步：

1. 每次 PR/推送运行 Python 自动测试；
2. `main` 分支测试通过后构建镜像，推送到 GitHub Container Registry（GHCR）；
3. 只有仓库变量 `DEPLOY_ENABLED=true` 时，才通过 SSH 更新生产服务器。

第三步使用提交 SHA 对应的精确镜像标签，便于回滚，不会部署来源不明的 `latest`。

## 本地 Docker 运行

先安装 Docker Desktop，然后在 `bin` 仓库根目录执行：

```powershell
Copy-Item .env.example .env
# 编辑 .env，填写真实凭证；已有 .env 时不要覆盖
docker compose up -d --build
docker compose ps
docker compose logs -f bin-gateway
```

健康检查：

```powershell
Invoke-WebRequest http://127.0.0.1:8767/healthz
```

停止：

```powershell
docker compose down
```

`.env` 已同时被 `.gitignore` 和 `.dockerignore` 排除。

## 生产服务器首次准备

服务器需要安装 Docker Engine 与 Docker Compose v2。以下路径与流水线默认值一致：

```bash
sudo mkdir -p /opt/bin-gateway
sudo chown "$USER":"$USER" /opt/bin-gateway
cd /opt/bin-gateway
```

把 `deploy/.env.server.example` 复制为服务器上的 `/opt/bin-gateway/.env`，填写真实
凭证，并限制权限：

```bash
chmod 600 /opt/bin-gateway/.env
```

流水线只上传 `compose.yaml`，不会上传或覆盖服务器 `.env`。

## 当前仓库托管状态

当前主远端 `origin` 是：

```text
https://github.com/forever-Victoria/bin.git
```

原 Gitee 仓库作为备用远端保留：

```text
gitee  https://gitee.com/victoria-han/bin.git
```

`.github/workflows/ci-cd.yml` 会由 GitHub Actions 执行。若以后还要在 Gitee
运行同类流水线，可在“流水线/Gitee Go”页面导入相同三个阶段；Dockerfile、
Compose 和测试命令无需改变。

## GitHub Actions 配置

仓库 `Settings → Secrets and variables → Actions` 中配置：

### Secrets

| 名称 | 用途 |
|---|---|
| `DEPLOY_HOST` | 服务器 IP 或域名 |
| `DEPLOY_USER` | SSH 用户 |
| `DEPLOY_SSH_KEY` | 对应服务器公钥的私钥 |
| `GHCR_USER` | 能读取 GHCR 镜像的 GitHub 用户 |
| `GHCR_READ_TOKEN` | 具有 `read:packages` 权限的 GitHub Token |

### Variables

| 名称 | 示例 | 用途 |
|---|---|---|
| `DEPLOY_ENABLED` | `true` | 开启自动部署；未设置时只测试和构建镜像 |
| `DEPLOY_PATH` | `/opt/bin-gateway` | 服务器部署目录 |
| `DEPLOY_PORT` | `22` | SSH 端口 |

建议在 GitHub 创建名为 `production` 的 Environment，并为生产部署开启人工审批。

## 手动部署与回滚

服务器上可手动部署任意镜像：

```bash
cd /opt/bin-gateway
export BIN_GATEWAY_IMAGE=ghcr.io/<账号>/<仓库>:<完整提交SHA>
docker compose -f compose.yaml pull
docker compose -f compose.yaml up -d --remove-orphans
docker compose -f compose.yaml ps
```

回滚时把 `BIN_GATEWAY_IMAGE` 换成上一个成功提交的 SHA，重复上述命令即可。服务器
`.env` 与镜像相互独立，因此回滚镜像不会丢失语音服务凭证和现场阈值。
