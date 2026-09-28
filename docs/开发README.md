# CrossLLM

多 LLM 跨轮次匿名讨论工作台 —— 让 3 个（或更多）模型就同一个问题多轮作答，并在后续轮次里**匿名互相阅读**对方的历史回答。

**这是个人研究项目，不是产品。**  
单用户、单进程假设，无账号体系，不提供生产级并发保障，不做部署编排。

后端使用 **FastAPI + asyncio + SSE**，前端使用**原生 HTML/CSS/JS**，零前端依赖、无构建步骤。

---

## 核心机制

CrossLLM 的核心不是“同时调用多个模型”，而是：

```text
用户问题
   ↓
Round 1
   ├─ Model A 独立回答
   ├─ Model B 独立回答
   └─ Model C 独立回答
          ↓
Round 2+
   ├─ Model A 读取匿名的 B/C 历史
   ├─ Model B 读取匿名的 A/C 历史
   └─ Model C 读取匿名的 A/B 历史
          ↓
各模型独立重新分析并回答
````

模型之间可以在后续轮次互相参考，但没有统一裁判替它们做最终决策。

---

## 架构红线（改动前必读）

这些不是风格偏好，而是项目的核心不变式：

| 不变式                                   | 含义                                                              |
| ------------------------------------- | --------------------------------------------------------------- |
| **三模型平级**                             | 没有 Leader / Judge / Arbiter，也没有投票、共识、排行、Elo、信誉分机制               |
| **R1 独立**                             | 第 1 轮各模型只看自己的指令和当前问题，看不到其他模型                                    |
| **R2+ 才可交叉阅读**                        | 第 2 轮起可以注入其他模型的**匿名**历史（Peer Context）                           |
| **model_id / candidate_id 分离**        | 内部保留真实 `model_id`；进入跨模型 Context 前统一转换为轮内 `candidate_id`，映射只存内部  |
| **匿名化硬校验**                            | CrossLLM 系统生成、准备进入多模型 Prompt 的内容，必须通过 `assert_no_identity_leak` |
| **CLOSED ≠ WRONG**                    | 单模型失败只标记该模型本轮失败，不取消整轮，也不覆盖历史 Round                              |
| **Raw Archive 不可变**                   | `sessions/` 是原始归档，只增不改                                          |
| **历史轮用户指令不进 Prompt**                  | 只有当前轮用户指令进入各模型当前请求                                              |
| **Evidence ≠ Fact**                   | MCP 结果、外部信息或模型意见不会自动升级为事实                                       |
| **Summary ≠ Project Memory**          | Conversation Summary 与长期 Project Memory 严格分离                    |
| **Peer Context ≠ Peer Review Result** | Peer Context 用于当前轮交叉阅读，不形成永久 Peer Memory                        |
| **Summary ≠ Compaction**              | 用户主动 Summary 与系统为了 Context Budget 执行的 Compaction 是不同机制          |

---

## Context Budget 与模型能力

CrossLLM 不要求用户手动填写模型的 Context Limit。

模型能力的基本来源优先级：

```text
Provider / 模型元数据
        ↓
context_limit / model_max_output
        ↓
Provider 查询失败
        ↓
models.yaml 中的 context_limit fallback
        ↓
仍不可得
        ↓
unknown / degraded
```

因此：

* `context_limit` 是**模型能力信息**，不是用户策略参数。
* `models.yaml` 中的 `context_limit` 可以留空。
* 如果 Provider 能取得模型能力，Provider 数据优先于 YAML。
* YAML 中填写的 `context_limit` 只作为无法取得 Provider 能力时的 fallback。
* CrossLLM 不要求用户理解或手动修改模型的 Context Limit。

### 输出预算

CrossLLM **不要求用户手动设置 request output 上限**。

项目内部严格区分：

```text
model_max_output
```

和：

```text
request_max_output
```

其中：

* `model_max_output` 表示模型能力。
* `request_max_output` 表示某一次请求实际采用的输出上限。
* UI 不提供 `request_max_output` 输入框。
* `models.yaml` 不使用 `max_output_tokens` 作为模型能力字段。
* 不允许使用 `context_limit × 90%`、固定 `16000`、固定 `8192` 等经验值伪造输出预算。
* 无法取得输出能力时记录 `unknown` / `degraded`，不得伪装成精确值。

`k` 只负责历史预算在：

```text
Lossless History
```

和：

```text
Compaction History
```

之间的分配。

```text
history_budget
        ↓
   ┌────┴────┐
   ↓         ↓
 k 部分    1-k 部分
   ↓         ↓
Lossless   Compaction
```

`k` 不负责决定模型最大输出 Token。

---

## 快速开始

### 1. 创建虚拟环境

```bat
python -m venv .venv
```

### 2. 安装依赖

```bat
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 3. 配置模型

先使用 Mock 配置，不需要 API Key：

```bat
copy config\models.example.yaml config\models.yaml
```

如果需要真实模型，再创建：

```bat
copy .env.example .env
```

然后编辑 `.env`：

```env
OPENROUTER_API_KEY=
```

### 4. 启动

项目根目录提供：

```text
start_crossllm.bat
```

直接双击即可。

BAT 使用**相对项目目录**定位 `.venv`，不依赖某个固定 Windows 路径。

等价命令：

```bat
cd /d 项目根目录
.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8765
```

浏览器打开：

```text
http://127.0.0.1:8765
```

如果不存在 `config/models.yaml`，`app.py` 会回落到内置 Mock 模型池。

---

## 配置

| 文件                           | 作用                 | 是否进入版本库           |
| ---------------------------- | ------------------ | ----------------- |
| `config/models.yaml`         | 当前模型池与 Provider 配置 | 仅在不包含个人环境信息/密钥时使用 |
| `config/models.example.yaml` | Mock 配置模板          | 是                 |
| `config/mcp.yaml`            | 本机 MCP Server 配置   | **否，建议本机维护**      |
| `config/mcp.example.yaml`    | 可移植 MCP 配置模板       | 是                 |
| `.env`                       | 真实密钥及本地运行配置        | **否**             |
| `.env.example`               | 环境变量模板             | 是                 |

### 密钥规则

`config/models.yaml` 中只保存环境变量名称，不保存 API Key：

```yaml
provider:
  name: openrouter
  type: openai_compatible
  base_url: https://openrouter.ai/api/v1
  api_key_env: OPENROUTER_API_KEY
```

真实值放在：

```text
.env
```

启动时读取环境变量。

密钥不得出现在：

* 代码
* Git 仓库
* 配置文件
* 日志
* Session
* API 响应

---

## MCP 配置

`config/mcp.yaml` 是**本机运行配置**，不要直接假定它适合其他 Windows 环境。

首次配置建议从模板复制：

```bat
copy config\mcp.example.yaml config\mcp.yaml
```

模板应使用可移植路径形式。

实际本机配置时，可以根据自己的环境调整 MCP Server 命令和路径。

由于 CrossLLM 通过子进程启动 MCP Server，某些 Windows 环境下使用绝对路径更稳定。

检查 MCP Server 是否可用：

```bat
.venv\Scripts\python.exe scripts\manual\run_mcp_health.py
```

MCP 工具采用只读白名单机制；配置中的 `tool_name` 用于限制允许调用的工具。

---

## 运行测试

测试文件统一位于：

```text
test/
```

### 全量测试

```bat
.venv\Scripts\python.exe -m pytest
```

### 查看收集到的测试数量

```bat
.venv\Scripts\python.exe -m pytest --collect-only -q
```

### Phase 4 全链路 E2E

```bat
.venv\Scripts\python.exe scripts\run_phase4_e2e.py
```

主要覆盖：

```text
Session
→ Round
→ Context Budget
→ Context Selection
→ Summary
→ Peer Context
→ Compaction
→ MCP Evidence
→ Provider
→ Persistence
→ Logs
```

自动化测试不会主动发起真实 LLM 请求。

真实 Provider 测试使用 fake / stub 进行隔离。

真实模型调用主要通过人工验收进行。

---

## 目录结构

```text
app.py
    FastAPI 入口：HTTP 路由、SSE、静态资源托管

conftest.py
    pytest 本地测试环境配置

pytest.ini
    pytest 配置与测试目录

core/
    领域逻辑，不依赖 HTTP

    round_manager.py
        Round 编排、多模型并发、错误隔离

    anonymizer.py
        candidate_id 匿名化与身份泄漏校验

    prompt_builder.py
        模型 Prompt 组装

    context_budget.py
        Context Budget、Token 预算与 k

    context_selector.py
        无损区 / 压缩区选择

    valid_round.py
        Valid Round 判断

    summary_service.py
        Conversation Summary

    compaction.py
        Session Compaction

    peer_context.py
        Peer Context

    evidence_index.py
        MCP Evidence Index

    project_memory.py
        Project Memory

    pin_store.py
        Pin / Unpin

    storage.py
        Raw Archive 持久化

    model_capability.py
        Provider / 模型能力解析

    model_manager.py
        模型池、Provider 配置

    mcp_registry.py
        MCP Server 生命周期

providers/
    base.py
        Provider Protocol

    mock_provider.py
        Mock Provider

    real_provider.py
        OpenAI-compatible HTTP Provider

static/
    index.html
    app.js
    style.css

config/
    models.yaml
    models.example.yaml
    mcp.yaml
    mcp.example.yaml

scripts/
    run_phase4_e2e.py

    manual/
        run_mcp_e2e.py
        run_mcp_health.py

    wrappers/
        reddit_server.py

test/
    自动化测试

docs/
    设计文档、阶段说明、验收记录

reference/
    Phase 0 参考项目，只读，不参与本项目测试收集

sessions/
    运行期 Session 原始归档（gitignore）

projects/
    Project Memory 运行期数据（gitignore）
```

---

## 数据产物

### Session

```text
sessions/<session_id>/
```

保存当前 Session 的原始运行数据，包括：

* Prompt
* Model Response
* Summary
* Compaction
* MCP Runtime
* Evidence
* Context Policy

Raw Archive 原则：

```text
只增不改
```

历史原始数据不会因为 Context 超限而删除。

### Project Memory

```text
projects/<project_id>/memory/
```

用于跨 Session 长期复用的重要项目知识。

Project Memory 不会因为普通对话自动永久写入。

基本流程：

```text
历史对话 / 实验 / 用户决策 / Evidence
        ↓
Candidate
        ↓
用户确认
        ↓
Project Memory
```

---

## Phase 4 核心功能

### Context Management

根据当前模型能力、固定输入、输出预算和 `k` 决定当前 Prompt 可以携带多少历史。

### Pin

用户可以 Pin 某个历史 Round，使其优先保持原文，不参与自动 Compaction。

### Conversation Summary

用户可以主动让某个模型总结自己的当前工作状态。

Summary 允许保留：

* 观点
* 推测
* 争议
* 未验证假设
* 当前倾向
* 已尝试方案
* 已排除方案
* 未解决问题

Summary 不自动等于 Fact。

### Peer Context

R2+ 可以让模型看到其他 ACTIVE 模型的匿名历史。

当其他模型只有一个有效回答时：

```text
Final Answer
```

直接作为 Peer Context。

当其他模型存在多个有效回答时：

```text
最新 Conversation Summary
```

可作为 Peer Context。

CrossLLM 不建立永久 Peer Memory。

### Compaction

当完整历史无法继续保持在无损预算中时，对更旧历史进行压缩。

Compaction：

```text
≠ 删除历史
≠ Project Memory
≠ Summary UI
≠ Peer Review
```

原始历史仍然保存在 Raw Archive。

### MCP Evidence

MCP Runtime 与 MCP Evidence Index 分开。

```text
MCP Tool Call
    ↓
Runtime Record

Final Answer 使用了哪些外部结果
    ↓
Evidence Index
```

MCP 返回结果不会自动成为 Fact。

---

## 文档索引

| 文档                              | 内容                            |
| ------------------------------- | ----------------------------- |
| `docs/architecture_v0.1.md`     | 整体架构                          |
| `docs/phase4_memory.md`         | Phase 4 Context / Memory 权威规范 |
| `docs/HANDOFF.md`               | 项目交接说明与已知限制                   |
| `docs/mcp.md`                   | MCP 说明                        |
| `docs/mcp中期部署.md`               | MCP 部署记录                      |
| `docs/phase1_mock.md`           | Phase 1 Mock                  |
| `docs/phase2_real_llm.md`       | Phase 2 Real LLM              |
| `docs/phase3_mcp_deployment.md` | Phase 3 MCP                   |
| `docs/PHASE4_REFACTOR_PLAN.md`  | Phase 4 重构计划                  |

`docs/phase4_memory.md` 是 Phase 4 Context / Memory 语义的权威规范。

---

## 已知限制

* 单用户、单进程假设。
* 没有生产级并发与多用户隔离。
* 不提供账号体系。
* 不作为生产服务部署。
* 真实 LLM 的可用性和速率取决于对应 Provider。
* OpenRouter / 其他 Provider 的模型能力和可用性可能发生变化。
* Provider 元数据不可用时，模型能力可能进入 fallback 或 degraded 状态。
* 自动化测试不会验证真实第三方服务的全部网络行为。
* 真实 Provider 网络行为需要人工验收。
* `scripts/manual/` 下的部分诊断脚本需要根据本机环境调整。
* `config/mcp.yaml` 属于本机配置，不保证 clone 后可以直接运行。

````

