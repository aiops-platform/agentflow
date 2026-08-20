# agentflow 生产镜像：AIOps Bug Fix 工作流平台（REST API + executor）。
# 沙箱 opensandbox 跑在独立容器（opensandbox-server），本镜像只含编排/数据源/观测。
FROM python:3.11-slim

WORKDIR /app

# 先拷依赖清单 + 代码，一次 pip install 完成
COPY pyproject.toml ./
COPY agentflow/ ./agentflow/
COPY agents/ ./agents/
COPY data/ ./data/
COPY examples/ ./examples/

RUN pip install --no-cache-dir .

ENV PYTHONUNBUFFERED=1 \
    AGENTFLOW_WORKDIR=/data \
    AGENTFLOW_STATE_BACKEND=sqlite

EXPOSE 8000

# 生产入口：REST API（Alertmanager webhook 触发）。本地可覆盖为 CLI。
CMD ["uvicorn", "agentflow.server:app", "--host", "0.0.0.0", "--port", "8000"]
