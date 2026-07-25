# bin 网关 Docker 与 CI/CD

## 方案说明

这套配置把代码、Python 运行时和依赖打进 Docker 镜像，但不会把真实 `.env` 打进
镜像或提交到 Git。生产服务器单独保存 `.env`，更新应用时只替换镜像。

流水线分为三步：

1. 每次 PR/推送运行 Python 自动测试；
2. `main` 分支测试通过后构建镜像，推送到 GitHub Container Registry（GHCR）；
3. 只有仓库变量 `DEPLOY_ENABLED=true` 时，才把精确提交镜像压缩后通过 SSH
   直传生产服务器，并执行健康检查和失败回滚。

第三步使用提交 SHA 对应的精确镜像标签，便于回滚，不会部署来源不明的 `latest`。
服务器无需访问 GHCR 或 Docker Hub，适用于国内服务器访问海外镜像仓库不稳定的情况。

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

服务器需要安装 Docker Engine 与 Docker Compose v2。当前生产服务器使用：

```bash
sudo mkdir -p /home/admin/bin-gateway
sudo chown admin:admin /home/admin/bin-gateway
cd /home/admin/bin-gateway
```

把 `deploy/.env.server.example` 复制为服务器上的 `/home/admin/bin-gateway/.env`，填写真实
凭证，并限制权限：

```bash
chmod 600 /home/admin/bin-gateway/.env
```

流水线只上传 Compose 文件、部署脚本和镜像压缩包，不会上传或覆盖服务器 `.env`。

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

### Variables

| 名称 | 示例 | 用途 |
|---|---|---|
| `DEPLOY_ENABLED` | `true` | 开启自动部署；未设置时只测试和构建镜像 |
| `DEPLOY_PATH` | `/home/admin/bin-gateway` | 服务器部署目录 |
| `DEPLOY_PORT` | `22` | SSH 端口 |

建议在 GitHub 创建名为 `production` 的 Environment，并为生产部署开启人工审批。

## 手动部署与回滚

流水线调用 `deploy/deploy.sh` 完成以下操作：

1. 从压缩包加载精确提交镜像；
2. 记录当前运行镜像；
3. 使用新镜像重建容器；
4. 最多等待 60 秒检查 `/healthz`；
5. 检查失败时自动恢复上一镜像。

服务器 `.env` 与镜像相互独立，因此自动回滚镜像不会丢失语音服务凭证和现场阈值。
