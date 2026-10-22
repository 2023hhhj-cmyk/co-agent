# opencode CLI 的 Docker 执行

更新：2026-10-17。CLI 默认强制在 Docker 中执行。本地尚无 Docker，当前验证为模拟进程回归，未进行真实容器或模型调用，也未更新服务器。`docs/interview/` 未修改。

## 调用与隔离

所有 `OpenCodeTool` 默认创建 `SandboxedExecutor`，因此 Developer、FrontendExpert、CEO 浏览代码和 ProjectAnalyzer 的 CLI 调用统一进入容器。Docker、daemon、镜像或认证不可用时返回失败；没有宿主机 CLI 降级路径。

每次调用新建临时容器，仅将指定项目目录挂载到 `/workspace` 并允许读写。工作目录必须是该工具 `base_dir` 的真实子目录；拒绝父目录本身、越界符号链接、隐藏配置目录及 Docker mount 特殊字符。代码需要显式传入项目目录；不能省略目录后把整个 `/root` 挂进去。

容器根文件系统只读，临时目录使用 noexec/nosuid tmpfs，移除全部 Linux capabilities，启用 no-new-privileges 和 256 个进程上限。默认 2GB 内存、1 CPU；超时沿用工具配置（开发/前端 900 秒，CEO 30 秒等）。使用宿主进程 UID/GID，避免写回文件归其他用户所有；服务以 root 运行时容器同样是 root UID，但能力已移除，没有使用 privileged 模式。

不挂载 Docker socket、SSH 密钥、整个宿主机 home 或 opencode 配置目录。挂载的项目文件仍可被修改，其中已有的项目秘密也可能被读取；目录隔离不是防止项目内数据泄露的保证。

## 网络与认证

容器使用 bridge 网络，以便访问模型 API 和下载 npm/pip 依赖；没有禁止所有网络，也没有实现出站域名白名单。需要更严格的网络边界时应在服务器配置外部 egress 防火墙/代理。

按白名单读取 `OPENCODE_API_KEY`。优先级：进程环境 > 项目 Settings（`.env`）> 宿主机 `~/.local/share/opencode/auth.json` 中 `opencode-go` 的 key。只复制模型凭证，不复制平台数据库/飞书/企微凭证。

模型变量通过权限为 0600 的临时 env 文件传入；不把密钥值写进命令行参数，调用结束后删除文件。容器内工具仍能访问自身的模型环境变量，Docker 管理员也能查看容器环境；应使用专用、限额凭证。

宿主机 localhost 的模型代理无法通过容器内 localhost 访问，需要配置容器可访问的网关地址。登录凭据不会自动迁移；通过 `OPENCODE_API_KEY` 提供 opencode-go 凭证。

## 镜像准备

在实际执行 CLI 的机器上构建（只以 security/ 为构建上下文，不上传项目 `.env`）：

```bash
docker build -t muzhu-sandbox:latest -f security/Dockerfile.opencode security/
docker run --rm --network none muzhu-sandbox:latest opencode --version
```

镜像包含 curl、git、Python/venv、ripgrep 和 opencode 独立二进制，默认固定 CLI 版本 1.18.30。升级时显式传入 `--build-arg OPENCODE_VERSION=版本号`。执行器使用 `--pull=never`，不会在任务处理中自动安装或下载镜像。

`.env` 中配置：

```dotenv
OC_SANDBOX_IMAGE=muzhu-sandbox:latest
OC_SANDBOX_MEMORY=1g
OC_SANDBOX_CPUS=1
# opencode-go provider key；留空时回退读取宿主机 opencode 登录态。
OPENCODE_API_KEY=
OPENCODE_MODEL=opencode-go/deepseek-v4-pro
OPENCODE_AGENT=build
```

代码生成命令保持非交互 JSON 事件输出：

```bash
opencode run "<prompt>" --model "$OPENCODE_MODEL" --agent build \
  --format json --auto --dir /workspace
```

`--auto` 自动批准 `build` agent 的权限请求（等价于旧版的 acceptEdits），不使用宿主机交互确认。返回的 NDJSON 事件流会被解析：`text` 事件拼接为输出文本，`step_finish` 事件累加 tokens/cost，`error` 事件判定为失败。

超时或取消时先尝试 `docker rm --force` 删除本次命名容器，再终止 Docker 客户端；取消继续向上传播。daemon 无响应时清理可能失败，需按日志里的容器名排查。项目目录的修改保留，未实现任务失败后自动回滚。

## 验证

```bash
venv/bin/python -m pytest tests/test_security/test_cli_docker.py --no-cov -q
```

测试覆盖 Docker 缺失时拒绝、目录与符号链接越界、挂载范围、资源参数、凭证白名单与临时文件、opencode 命令参数、事件解析以及超时/取消清理。真实上线仍需构建镜像，并在专用项目目录运行最小代码生成任务验证认证、文件写回与用量。

参考：[opencode 文档](https://opencode.ai/docs/)、[CLI run](https://opencode.ai/docs/cli/)。
