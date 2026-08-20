# opencode + opensandbox Spike 报告

> 这份文档的目标：让一个**不了解背景的新人**读完就能明白——我们做了什么、为什么要做、每一步为什么这么做、结论是什么。

---

## 一、先搞清楚：我们在干嘛，为什么干

### 1.1 大背景

我们要做一个 **AI 运维 Bug Fix 工作流平台**（完整设计见项目根目录 `DESIGN.md`），核心是：

- 把「定位 bug → 修复 bug」拆成一个个**职能智能体**（比如：查日志的 agent、定位代码的 agent、改代码的 agent、跑测试的 agent……）。
- 这些 agent 用 **opencode** 来跑（opencode 本身就是一个写代码的 AI agent）。
- agent 需要跑测试、跑代码时，用 **opensandbox** 沙箱隔离，避免危险代码污染宿主机。

### 1.2 为什么先做 spike（而不是直接开工）

这个平台要写几千行代码。但在写代码之前，有 **3 个技术假设如果不成立，整个方案会塌**。spike 就是用最小成本（约 2 天）提前验证这 3 个假设，**避免带着错误假设去写几千行注定返工的代码**。

这 3 个假设，按「风险从高到低」排序：

| # | 假设 | 为什么风险高 |
|---|---|---|
| 1 | **opencode 能被 Python 驱动** | opencode 是 TypeScript/Go 写的，不是 Python 库；官方 Python SDK 还没正式发布。如果 Python 根本连不上 opencode，整个架构（每个节点 = 一个 opencode 会话）直接作废 |
| 2 | **opensandbox 能在我们的环境跑起来** | 我们的机器**没有 docker，只有 podman**。opensandbox 官方文档默认 docker，不知道能不能兼容 |
| 3 | **opencode agent 能通过 MCP 调用 opensandbox** | 这是「agent 用沙箱」这条链路的命门。agent 需要能主动调用一个「在沙箱里跑代码」的工具，这个调用机制（MCP）到底通不通，不试不知道 |

spike 的 3 个程序，就分别对应这 3 个假设。

---

## 二、结论速览（先看结果）

| # | 验证的问题 | 结论 | 意味着什么 |
|---|---|---|---|
| 1 | opencode 能被 Python 驱动 | ✅ **通过** | 用 HTTP+SSE 直连即可，**不需要等官方 SDK** |
| 2 | opensandbox 能用 podman 跑 | ✅ **通过** | podman 完全替代 docker，本地开发零障碍 |
| 3 | agent 能经 MCP 调沙箱 | ✅ **通过** | 工具接入走 MCP，架构命门打通 |
| 4 | session 能跨崩溃重启恢复 | ✅ **通过** | 断点续跑可 resume 原 session，不用重开 |

**三个假设全部成立，可以放心进入正式开发（M1）。**

---

## 三、逐个程序详解

### 程序 1：`01_opencode_python/server_http_sse.py`

#### 验证什么问题？

> **opencode 到底能不能被 Python 驱动？** 也就是：用 Python 能不能「建会话 → 发问题 → 拿到回答 → 拿到 token 用量」。

#### 为什么必须验证？

opencode 的**引擎**是 TypeScript/Go 写的，跑在 `opencode serve` 这个 server 进程里；「Python SDK」其实只是个客户端，而且**还没正式发布**（只有测试版）。

如果这个假设不成立，那「每个流程节点 = 一个 opencode 会话」这个最核心的设计就无从谈起。所以它是**风险最高**的一条，必须第一个验证。

#### 为什么用 HTTP+SSE，而不是 SDK？

因为我们发现 opencode 的 `opencode serve` 暴露了一套稳定的 HTTP 接口（`POST /session`、`POST /session/:id/message`、`GET /event`），用 Python 的 `httpx` 库直接调就行。**这条协议是稳定、有文档的，不依赖还没 GA 的 SDK。** 与其赌一个测试版 SDK，不如直接调底层协议。

#### 真实输入

程序里发的 prompt（就是问 opencode 的问题）：

```
what is 6*7? reply with just the number
```

> 为什么用 `6*7` 这种幼儿园算术？因为答案是**确定的 `42`**，一眼就能判断 opencode 答对没答对。spike 要的是「验证链路通不通」，不是「验证模型聪明不聪明」，所以用最简单、答案最确定的问题。

#### 真实输出（实际运行结果）

```
[1] session created: ses_fe9f54e41ffekLs4caRz15dpMy
[2] message sent, http=200
[3] model: big-pickle / opencode
[4] answer text: 42
[5] tokens: {"total": 9161, "input": 59, "output": 14, "reasoning": 0,
             "cache": {"write": 0, "read": 9088}}
[6] step-finish tokens: {"total": 9161, "input": 59, "output": 14, ...}
[6] step-finish cost: 0
[7] event types: {'server.connected': 1, 'session.updated': 3, 'message.updated': 5,
                  'message.part.updated': 7, 'session.status': 4, 'session.diff': 1,
                  'message.part.delta': 6, 'session.idle': 1}
```

#### 这段输出说明了什么？

- **`answer text: 42`** → opencode 答对了，说明「Python 发问题 → 拿回答」整条链路通了。
- **`model: big-pickle / opencode`** → 这是 opencode 内置的**免费 Zen 模型**，没配任何 API key 也能跑。意味着开发联调阶段不需要真实 key。
- **`tokens` / `step-finish cost`** → 这就是我们后面做「监控」要的数据：每次调用用了多少 token、花多少钱。它已经在事件里现成地给出来了，直接喂给 Langfuse 即可。
- **`event types` 那一行** → 列出了 SSE 事件流里所有事件类型，证明「流式事件」是通的，agent 执行过程可以被实时观测。

#### ⚠️ 一个重要的发现

同步的 `POST /message` 返回里，**没有工具调用的记录**（只有 step-start / reasoning / text / step-finish）。要观测「agent 调用了什么工具」，必须去消费 **SSE 事件流**（`GET /event`）里的 `message.part.updated` 事件。

> 这个发现直接影响架构：`OpenCodeAdapter` 不能只看同步返回，必须挂一个 SSE 监听器来抓工具调用。

---

### 程序 2：`02_opensandbox/sandbox_run.py`

#### 验证什么问题？

> **opensandbox 沙箱能在我们的环境（只有 podman、没有 docker）里跑起来吗？** 也就是：建一个隔离沙箱 → 在里面跑 Python → 拿到结果 → 销毁。

#### 为什么必须验证？

agent 要「跑测试」「跑代码」来修 bug，这些代码是不可信的，必须在隔离沙箱里跑，否则一个 `rm -rf` 就能毁掉宿主机。opensandbox 是阿里的开源沙箱方案，但官方默认对接 **docker**，而我们机器上**没有 docker 只有 podman**。到底能不能兼容，必须实测。

#### 为什么用 podman 能替代 docker？

我们启动 podman 后发现，podman 会自动在 `/var/run/docker.sock` 上**转发一个 Docker 兼容的 socket**（`curl /version` 返回的是 Podman Engine 5.3.2）。opensandbox-server 默认就找这个 socket，所以**完全不用改 opensandbox 的代码，它以为自己在跟 docker 说话，其实背后是 podman**。

#### 真实输入

沙箱里跑的 Python 代码：

```python
import sys
print('python', sys.version.split()[0])
result = 6 * 7
result
```

> 这里打印 Python 版本，是为了**证明沙箱里的 Python 是独立于宿主机的**（沙箱里是 3.11.14，宿主机是 3.12.7）。`6*7` 同样是因为答案确定，方便判断对错。

#### 真实输出

```
[1] 创建沙箱: opensandbox/code-interpreter:v1.0.2 @ 127.0.0.1:8080
[2] 沙箱就绪
[3] 运行代码:
import sys
print('python', sys.version.split()[0])
result = 6 * 7
result

[4] exit_code: None
[4] stdout: ['python 3.11.14\n']
[5] result: ['42']
SPIKE 2 PASS ✅
```

#### 这段输出说明了什么？

- **`stdout: python 3.11.14`** → 沙箱里确实是独立的 Python 3.11 环境（和宿主机 3.12.7 不同），隔离生效。
- **`result: ['42']`** → 代码执行结果正确拿到。
- 整条「建沙箱 → 跑代码 → 取输出 → 销毁」链路通了。

#### 过程中踩过的坑（为什么最后要这样写）

1. **`timeout` 参数要传 `timedelta` 不是 int** → 一开始 `timeout=600` 直接报 `AttributeError`，改掉后解决。
2. **镜像必须指定 entrypoint** → code-interpreter 镜像不指定 `entrypoint=["/opt/opensandbox/code-interpreter.sh"]` 时，沙箱里的服务起不来，程序卡死。
3. **`sandbox_id` 属性不存在** → Sandbox 对象没有这个字段，删掉即可（不影响功能）。

---

### 程序 3 + 4：`03_integration/sandbox_mcp_server.py` + `run_agent_via_sandbox.py`

> 这两个程序要一起看：`sandbox_mcp_server.py` 是把 opensandbox 包装成 MCP 工具的**服务端**；`run_agent_via_sandbox.py` 是驱动 opencode agent 去调用这个工具的**客户端**。

#### 验证什么问题？

> **opencode agent 能不能通过 MCP 主动调用 opensandbox 沙箱？** 这是「agent 用沙箱」这条链路的命门，也是 3 个假设里最复杂的一个。

#### 为什么必须验证？

前面两个程序分别验证了「opencode 能跑」和「沙箱能跑」。但真正的场景是：**agent 在干活时，自己决定「这里需要跑一段代码」，然后主动去调沙箱工具**。这个「agent → 工具 → 沙箱」的联动，是整套架构的核心，也是最可能断的一环。

#### 为什么用 MCP（Model Context Protocol）？

MCP 是一个标准协议，用来让 AI agent 调用外部工具。opencode 原生支持 MCP（`opencode mcp add`）。我们把 opensandbox 包装成一个 MCP 工具 `run_python(code)`，这样：

- opencode agent 就能把 `run_python` 当作自己的一个「技能」来调用；
- 沙箱和 opencode 解耦，以后想换别的沙箱、或给别的 agent 用，都方便。

#### 真实输入

**第一步**：把 MCP 工具注册给 opencode：

```bash
opencode mcp add opensandbox -- spike/.venv/bin/python spike/03_integration/sandbox_mcp_server.py
```

**第二步**：给 agent 发的 prompt：

```
请使用 run_python 工具执行 `import hashlib; print(hashlib.sha256(b'opencode-spike').hexdigest())`，然后把输出的哈希值原样告诉我。
```

> **为什么这次不是 `6*7`，而是 SHA256？** 这是关键的一步。我们第一次用 `6*7` 测试时，发现一个陷阱：**模型能心算出 42，于是它根本没调工具，直接回答了**。这样根本证明不了「工具被调用了」。
>
> 所以换成了 SHA256 哈希——**模型不可能心算哈希值**，它要想答对，就**必须真的去调 run_python 工具**。这样只要答案正确，就铁证工具被调用了。

#### 真实输出

**agent 的最终回答**（`run_agent_via_sandbox.py` 的输出）：

```
[1] session: ses_fe9e72cebffe8mHer8DlJpRp6a
[3] message parts:
    [step-start] ...
    [reasoning] The output is: fb39a2da8d518de5c03120d9d01e5f40fab86478776617ac1e9924e4ef24b056
    [text] fb39a2da8d518de5c03120d9d01e5f40fab86478776617ac1e9924e4ef24b056
    [step-finish] tokens: {"total": 9923, "input": 138, "output": 121, ...}
[4] final text: fb39a2da8d518de5c03120d9d01e5f40fab86478776617ac1e9924e4ef24b056
```

**MCP 工具的调用日志**（`/tmp/mcp-run-python.log`，这是铁证）：

```
CALL run_python code="import hashlib; print(hashlib.sha256(b'opencode-spike').hexdigest())"
RESULT stdout='fb39a2da8d518de5c03120d9d01e5f40fab86478776617ac1e9924e4ef24b056\n' result='' err=''
```

**预期哈希值**（我们自己用 Python 算的，用来核对）：

```
fb39a2da8d518de5c03120d9d01e5f40fab86478776617ac1e9924e4ef24b056
```

#### 这段输出说明了什么？

- MCP 日志里的 `CALL run_python code="import hashlib..."` → **agent 真的调用了 run_python 工具**（不是嘴上说说）。
- `RESULT stdout='fb39a2da...'` → 沙箱真实执行了代码，返回了哈希值。
- agent 最终回答的 `fb39a2da...` 和「预期哈希值」**完全一致** → 结果正确。
- 三个「fb39a2da」对上了 → 整条「agent 决策 → 调 MCP 工具 → 沙箱执行 → 结果回传 → agent 作答」链路完整跑通。

#### 一个反面教材（为什么不能只看同步返回）

注意程序输出里有一行 `[5] tool calls: 0` —— 同步的 `POST /message` 返回里**看不到工具调用**。但我们通过给 MCP server 加日志（`/tmp/mcp-run-python.log`）证明了工具**确实被调用了**。

> 这再次印证了程序 1 的发现：**要观测工具调用，必须看 SSE 事件流或工具自身的日志，不能只看同步返回。** 这也是为什么正式架构里必须挂 SSE 监听。

### 程序 5：`04_session_resume/resume_test.py`

#### 验证什么问题？

> **opencode 的 session 在 server 崩溃重启后，对话上下文还能不能恢复？**

#### 为什么必须验证？

这回答的是「断点续跑」的一个关键子问题：workflow 跑一半挂了，重启后是**重开一个新 session 从头来**，还是**恢复原来的 session 继续**？如果 session 能恢复，就能省 token、保上下文，断点续跑体验更好——所以值得单独验证。

#### 怎么验证的（方法）

1. 起一个 opencode server，建 session，让模型「记住一个口令 ZEPHYR-7421」。
2. **SIGKILL 掉 server**（模拟最粗暴的崩溃，不是优雅退出）。
3. 重启一个 server。
4. 看 session 还在不在列表里，然后在**同一个 session** 上问「口令是什么」。

> 为什么用 SIGKILL 而不是优雅退出？因为 SIGKILL 是最极端的场景——如果连它都能恢复，那正常重启肯定也能。

#### 真实输入

- turn1：`记住这个口令：ZEPHYR-7421。只回复'已记住'。`
- （SIGKILL + 重启 server）
- turn2（同一 session）：`我刚才让你记住的口令是什么？`

#### 真实输出

```
[1] server A 已启动 (port 4091)
[2] session: ses_fe9cf6608ffegbZU6HU8aBduAB
[3] turn1 回复: 已记住
[4] server A 已 SIGKILL（模拟崩溃）
[5] server B 已重启
[6] 重启后 session 仍在列表: True
[7] turn2 回复: ZEPHYR-7421
[8] 上下文跨重启保留: True
SPIKE 4: session resume 可行 ✅
```

#### 结论

**session 能跨崩溃重启恢复**——opencode 把 session 持久化在 SQLite（`~/.local/share/opencode/opencode.db`），重启后 session 和对话上下文都还在。

对架构的意义：断点续跑时**可以 resume 原 session**（checkpoint 里存 `session_id`），中断的节点能接着跑，不用重开。

---

## 四、环境相关的踩坑（新人会遇到的坑）

这些坑不解决，spike 根本跑不起来。记录在这里，供复现时参考。

### 坑 1：docker.io 被墙（最耽误时间的一个）

**现象**：`podman pull opensandbox/code-interpreter` 报 `dial tcp 108.160.172.200:443: i/o timeout`。

**原因**：所在网络访问不了 docker 官方仓库（docker.io）。

**解决**：给 podman 配国内镜像源。写入 podman 虚拟机内的 `/etc/containers/registries.conf.d/900-mirror.conf`：

```toml
[[registry]]
prefix = "docker.io"
location = "docker.io"
[[registry.mirror]]
location = "docker.m.daocloud.io"
```

> 为什么这么做？podman 拉 `docker.io/xxx` 时，会先尝试 `location`，失败后走 `mirror` 列表。把 mirror 指向可达的 `docker.m.daocloud.io`，就能绕开被墙的官方源。

### 坑 2：npm 12 拦截 opencode 的 postinstall

**现象**：`npm i -g opencode-ai` 装完，运行 `opencode` 报「postinstall script was not run」。

**原因**：opencode 的 npm 包是个「壳」，真正的二进制靠 postinstall 脚本下载。npm 12 新增了安全机制，默认拦截 postinstall。

**解决**：

```bash
npm i -g opencode-ai --allow-scripts=opencode-ai
```

### 坑 3：opencode 免费模型

没配任何 key 时，opencode 会用内置的免费 Zen 模型（日志里显示 `modelID=big-pickle` / `providerID=opencode`）。**开发联调阶段可以直接用，省去配 key 的麻烦。**

---

## 五、如何自己跑一遍

### 前置（三个进程要先起来）

```bash
# 1. podman 虚拟机
podman machine start

# 2. opensandbox server（在 spike/.venv 里）
source spike/.venv/bin/activate
OPENSANDBOX_INSECURE_SERVER=YES opensandbox-server --config spike/sandbox.toml

# 3. opencode server（另开一个终端）
opencode serve --hostname 127.0.0.1 --port 4090
```

### 运行四个 spike

```bash
source spike/.venv/bin/activate

# 程序 1：opencode 能被 Python 驱动吗
python spike/01_opencode_python/server_http_sse.py

# 程序 2：opensandbox 能用 podman 跑吗
python spike/02_opensandbox/sandbox_run.py

# 程序 3：agent 能经 MCP 调沙箱吗（先注册 MCP 工具）
opencode mcp add opensandbox -- spike/.venv/bin/python spike/03_integration/sandbox_mcp_server.py
python spike/03_integration/run_agent_via_sandbox.py

# 程序 4：session 能跨崩溃重启恢复吗（脚本自己拉起/杀死 server，用独立端口 4091）
python spike/04_session_resume/resume_test.py
```

### 预期结果

- 程序 1 输出 `answer text: 42`。
- 程序 2 输出 `result: ['42']`。
- 程序 3 输出一段 `fb39a2da...` 的哈希，且 `/tmp/mcp-run-python.log` 里有 `CALL run_python` 记录。
- 程序 4 输出 `上下文跨重启保留: True`，turn2 回复里含 `ZEPHYR-7421`。

---

## 六、最终结论

| 问题 | 答案 |
|---|---|
| opencode 能被 Python 驱动吗 | ✅ 能，用 HTTP+SSE 直连，不需要等 SDK |
| opensandbox 能用 podman 跑吗 | ✅ 能，podman 的 docker 兼容 socket 直接可用 |
| agent 能经 MCP 调沙箱吗 | ✅ 能，工具链路完整跑通 |
| session 能跨崩溃重启恢复吗 | ✅ 能，断点续跑可 resume 原 session |
| **可以进入正式开发吗** | ✅ **可以，所有风险全部解除** |

### 对正式开发的四个关键提醒（写代码时要记住）

1. **监控必须挂 SSE 监听**：工具调用和 token/cost 都在 SSE 事件流里，同步返回拿不到完整的工具调用。
2. **代码执行类 agent 要强制用沙箱**：模型能心算时会跳过工具，system prompt 里要写「涉及代码执行必须用 run_python」。
3. **断点续跑要存 session_id**：opencode 的 session 能跨重启恢复，checkpoint 时把 `session_id` 一起落盘，中断的节点直接 resume。
4. **DeepSeek 还没接**：本次 spike 用的是 opencode 内置免费 Zen 模型验证链路，正式用 DeepSeek 还需配 `DEEPSEEK_API_KEY`。
