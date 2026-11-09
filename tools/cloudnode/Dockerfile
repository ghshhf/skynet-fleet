# wsrelay 的最小运行镜像：零第三方依赖，只要 Python 3.12。
#
# 为什么需要 Dockerfile：Zeabur / Koyeb 这类平台对「纯 Python 脚本」的自动识别
# 不一定能猜到启动命令，给一个 Dockerfile 就不用赌平台的探测逻辑。
#
# 端口：平台会注入 $PORT（Render/Railway/Zeabur 都是这个约定），
# 没注入时退到 8080，方便本地 `docker run -p 8080:8080`。
FROM python:3.12-slim

WORKDIR /app
COPY wsrelay.py proxy.py ./

ENV PYTHONUNBUFFERED=1
EXPOSE 8080

# 必须用 shell 形式，才能读到 ${PORT}；exec 保证信号能传到 python（容器优雅退出）
CMD ["sh", "-c", "exec python wsrelay.py --serve --port ${PORT:-8080}"]
