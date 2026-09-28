# 🧠 CrossLLM

> **不搞自动投票，不打虚假胜负。让大模型隐去身份，在你面前“盲盒对抗与交叉找茬”。**

CrossLLM 是一个用于**架构设计、算法推演与高难度决策**的本地多模型交叉讨论工作台。

它不是那种替你做决定的“黑盒生产级系统”，而是一个**把决策权彻底交还给人类**的思维外骨骼。

---

### 💡 为什么做这个东西？（讲点人话）

市面上的很多“多 Agent 讨论”系统都有一个通病：**喜欢搞全自动投票、共识合成（Consensus）**。
但现实很残酷：当 3 个模型都一本正经地胡说八道时，它们合成出来的只会是一个“极其逼真的超级幻觉”。

CrossLLM 的逻辑完全不同：
1. **别互相抄作业**：第一轮大家各答各的，谁也别看谁。
2. **撕掉名牌（盲审）**：第二轮开始互相看对方的历史回答，但**抹去模型身份**（避免 Claude 盲目附和 GPT，或者国产模型抱团）。
3. **没有裁判**：不搞 Elo 打分，不搞自动投票选出“胜者”。**真伪与取舍，全由屏幕前的你来做最终裁判。**

---

### 🔄 核心推演流程

```text
               你抛出一个硬核问题
                      ↓
【Round 1: 独立盲答】
   ├─ Candidate A 独立生成方案
   ├─ Candidate B 独立生成方案
   └─ Candidate C 独立生成方案
                      ↓
【Round 2+: 匿名互审】
   ├─ A 阅读 B/C 的方案，指出逻辑缺陷并优化自身
   ├─ B 阅读 A/C 的方案，指出算法瓶颈并优化自身
   └─ C 阅读 A/B 的方案，挑出事实性错误并优化自身
                      ↓
            你阅读冲突，敲定事实，继续追问
```

---

### ✨ 核心设计亮点

* 🎭 **真·匿名对抗**：系统把底层真实的 `model_id` 与前端呈现的 `candidate_id` 物理隔离。模型只知道自己在反驳“另一个匿名专家”，有效杜绝立场偏见与从众效应。
* ⚡ **容错并发**：多模型在同一步骤内并行生成。哪怕某个服务商网络抖动或超时，绝不影响其他存活模型继续思考。
* 🗜️ **聪明但不暴力的历史管理**：
  * **不搞一刀切**：不会傻瓜式地“只保留最近 3 轮”。
  * **重要信息 Pin 住**：你敲定的关键架构，一键锁定（Pin），永远不被压缩。
  * **长文动态折叠（Compaction）**：根据每个模型实际可用的 Context 窗口，自动把过旧的回合压成摘要，保留近期的原汁原味。
  * **原始底稿全存盘**：哪怕界面上做了压缩，磁盘 `sessions/` 目录依然完整保留所有 Markdown 原始文本，随时可查。
* 🔍 **把 MCP 当参考，不当真理**：
  * 支持挂载 GitHub、知乎、文件等 MCP 扩展。
  * **铁律**：搜索出来的结果只是“参考证据”，系统不会无脑将其当作既成事实，把辨别权留给模型与你。

---

### 🚀 30 秒极速上手 (Windows)

专为 Windows 环境打造，自带极简启动脚本，**不需要装 Node.js，不需要折腾 npm**。

#### 1. 环境准备
```cmd
:: 创建并激活虚拟环境
python -m venv .venv

:: 安装依赖
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

#### 2. 免费免 Key 体验（Mock 模式）
不需要填任何 API Key，直接体验完整的多轮交互闭环：
```cmd
copy config\models.example.yaml config\models.yaml
```

#### 3. 一键启动
双击项目根目录下的 **`start_crossllm.bat`** 即可。

或者手动执行命令：
```cmd
.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8765
```
浏览器打开：`http://127.0.0.1:8765`

---

### 🔌 接入真实大模型

CrossLLM 采用标准的 **OpenAI 兼容协议**，可以无缝直连 OpenRouter、DeepSeek、SiliconFlow 等各大平台。

1. **配置密钥**（推荐使用环境变量，绝对防泄露）：
   ```cmd
   copy .env.example .env
   ```
   在 `.env` 中填写你的 Key（例如 `OPENROUTER_API_KEY=sk-xxxx`）。

2. **配置模型 (`config/models.yaml`)**：
   ```yaml
   provider:
     name: openrouter
     type: openai_compatible
     base_url: https://openrouter.ai/api/v1
     api_key_env: OPENROUTER_API_KEY  # 只填变量名，绝不硬编码真实 Key
   ```

> 💡 **告别参数玄学**：你**不需要**手动算模型的 Context Limit，也不需要纠结输出该设 4096 还是 8192。CrossLLM 会自动向 Provider 探针索取模型的真实窗口能力，并以此为基准智能调度。

---

### 🛠️ 外部工具增强 (MCP)

通过只读白名单控制外部工具，杜绝模型乱改本地文件：

```cmd
:: 复制本机配置模板
copy config\mcp.example.yaml config\mcp.yaml

:: 检查本地 MCP 服务健康状态
.venv\Scripts\python.exe scripts\manual\run_mcp_health.py
```

---

### 🧪 自动化测试

项目内置了完整的单元测试与端到端（E2E）回归链条：

```cmd
:: 运行全部本地测试（纯 Mock，不消耗任何真实 Token）
.venv\Scripts\python.exe -m pytest

:: 运行 Phase 4 全链路 E2E 验收
.venv\Scripts\python.exe scripts\run_phase4_e2e.py
```

---

### 📁 极简代码结构

```text
CrossLLM/
├─ app.py                 # FastAPI 入口
├─ start_crossllm.bat     # Windows 一键启动脚本
├─ core/                  # 核心引擎 (匿名路由、Prompt拼接、上下文动态压缩、MCP桥接)
├─ providers/             # 模型驱动 (Mock 测试驱动 + OpenAI 兼容驱动)
├─ static/                # 纯原生前端 (单 HTML + CSS + JS，零框架依赖)
├─ config/                # 配置文件模板
├─ docs/                  # 详细技术规范与设计白皮书
├─ sessions/              # [运行期] 对话原始 Markdown 与 JSON 归档 (不提交 Git)
└─ test/                  # 完备的自动化测试集
```

---

### ⚠️ 傲慢但诚实的免责声明 (Known Limitations)

1. **Windows 优先**：本项目基于 Windows 习惯打造并测试，没有适配多平台的野心，不提供 Docker 封装。
2. **单机自用**：单用户、本地单进程假设。没有账号密码系统，没有多租户隔离。
3. **不保姆，对人有要求**：这不是那种“丢一句话就直接出完满结果”的无脑玩具。它需要你具备基本的专业判断力，去阅读模型之间的分歧。

---

### 📖 进阶文档

想了解深层设计的开发者，可以直接查阅 `docs/` 目录：
* `docs/architecture_v0.1.md`：底层架构全景设计
* `docs/phase4_memory.md`：上下文预算分配、Pin 机制与 Compaction 规范
* `docs/HANDOFF.md`：项目交接清单与工程约束