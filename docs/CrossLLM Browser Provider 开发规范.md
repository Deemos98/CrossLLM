# CrossLLM Browser Provider 开发规范

## 1. 文档定位

本文档定义 CrossLLM 在 Phase4 完成后增加的 **Browser LLM Provider** 方案。

目标不是重新设计 CrossLLM，也不是引入 MCP，而是让 CrossLLM 能够把已经存在的网页端 LLM（第一目标为 ChatGPT Web）作为一种普通 Provider 使用。

本阶段只解决：

```text
CrossLLM
    ↓
Browser Provider
    ↓
ChatGPT 网页
    ↓
自动输入 Prompt
    ↓
等待回答完成
    ↓
自动获取回答文本
    ↓
返回 CrossLLM
```

网页端自身的 Memory、聊天历史、项目记忆等功能不属于 CrossLLM Browser Provider 的核心能力。

---

# 2. 核心原则

## 2.1 Browser Provider 必须服从现有 Provider 契约

Browser Provider 不得修改 CrossLLM Core 的 Round、Model State、Persistence、ACTIVE/CLOSED 等核心逻辑。

在 Core 看来：

```text
API Provider
Browser Provider
Mock Provider
```

都只是不同的 Provider 实现。

统一表现为：

```python
generate(prompt) -> response
```

因此：

```text
CrossLLM Core
      │
      ├── MockProvider
      ├── APIProvider
      └── ChatGPTWebProvider
```

Core 不需要知道回答来自 HTTP API 还是浏览器。

---

# 3. 第一目标范围

第一版本只支持：

- ChatGPT Web
- Playwright
- 用户手动完成第一次登录
- Persistent Browser Profile
- 自动输入 Prompt
- 自动发送
- 自动检测回答完成
- 自动获取最新回答纯文本/Markdown
- 返回 CrossLLM
- 支持 Round 1
- 支持 Round 2 及以后已有的 CrossLLM 上下文注入

第一版明确不实现：

- MCP
- 自动账号密码填写
- CAPTCHA 自动处理
- 绕过登录验证
- 网页 Memory 管理
- 自动分析 ChatGPT 历史会话
- 文件上传
- 图片上传
- 语音
- GPT 自带工具控制
- 浏览器通用 Agent
- ChatALL
- 多网站统一浏览器框架

---

# 4. 技术来源与复用策略

## 4.1 主参考：chatgpt-pro-web

Browser Provider 的核心实现优先参考：

```text
chatgpt-pro-web
```

重点参考以下部分：

```text
Playwright 初始化
Composer / 输入框定位
Stop Button 检测
Assistant Turn 定位
Copy 按钮提取
Clipboard 获取
Persistent Browser Profile
```

核心思想：

```text
启动/连接浏览器
        ↓
ChatGPT Web
        ↓
定位 Composer
        ↓
输入 Prompt
        ↓
发送
        ↓
等待生成完成
        ↓
定位最新 Assistant Turn
        ↓
Copy
        ↓
Clipboard
        ↓
返回文本
```

不要直接 Fork 整个项目。

只提取并改造其中与以下功能有关的代码：

```text
Browser initialization
Chat interaction
Completion detection
Response extraction
Session persistence
```

最终封装为 CrossLLM 自己的：

```text
ChatGPTWebProvider
```

---

## 4.2 辅助参考：chatgpt-py

`chatgpt-py` 仅作为辅助参考。

重点参考：

```text
browser
session
Python API
persistent state
页面生命周期管理
```

主要用于改进：

```text
Browser Session
Browser Profile
Python 封装方式
```

不要直接复制其完整功能体系。

尤其不需要引入：

```text
图片
文件
其他高级操作
```

除非未来明确产生需求。

---

## 4.3 不采用 ChatALL

本项目不使用 ChatALL 作为 Browser Provider 代码基础。

原因：

```text
ChatALL 的目标：
多模型 Web Client

CrossLLM 的目标：
多轮、多模型、可手动关闭模型的 LLM Orchestration
```

二者核心调度逻辑不同。

ChatALL 可以作为历史案例研究，但不纳入代码依赖。

---

# 5. 推荐目录结构

在 Phase4 当前 Provider 架构完成后增加：

```text
crossllm/
│
├── core/
│   ├── orchestrator.py
│   ├── round.py
│   ├── model_state.py
│   └── persistence.py
│
├── providers/
│   ├── base.py
│   │
│   ├── mock/
│   │   └── ...
│   │
│   ├── api/
│   │   └── ...
│   │
│   └── browser/
│       ├── base.py
│       └── chatgpt.py
│
├── browser/
│   ├── session.py
│   ├── chromium.py
│   └── selectors.py
│
└── docs/
    └── browser_provider.md
```

推荐职责：

### `providers/browser/chatgpt.py`

负责：

```text
ChatGPT Web 的 Provider 层
```

例如：

```python
class ChatGPTWebProvider:
    async def generate(self, prompt: str) -> str:
        ...
```

---

### `browser/session.py`

负责：

```text
Persistent Browser Profile
浏览器生命周期
Browser Context
连接/启动浏览器
```

---

### `browser/chromium.py`

负责：

```text
Playwright Chromium 初始化
```

---

### `browser/selectors.py`

集中维护：

```text
Composer selector
Assistant selector
Stop button selector
Copy button selector
```

避免 selector 分散在业务代码中。

ChatGPT Web DOM 改动时优先只修改这里。

---

# 6. Browser Provider 最小接口

建议 Provider 层至少形成以下逻辑：

```python
class ChatGPTWebProvider:
    async def generate(self, prompt: str) -> str:
        await self.ensure_browser()
        await self.ensure_page()
        await self.send_prompt(prompt)
        await self.wait_until_complete()
        return await self.extract_response()
```

内部职责分成：

```text
ensure_browser()
ensure_page()
send_prompt()
wait_until_complete()
extract_response()
```

禁止把所有逻辑写在一个 `generate()` 中。

---

# 7. Playwright 浏览器初始化

推荐使用 Persistent Context。

目标：

```text
第一次运行
    ↓
启动专用 Chromium Profile
    ↓
用户手动登录 ChatGPT
    ↓
保存登录状态
```

以后：

```text
启动 CrossLLM
    ↓
复用同一个 Browser Profile
    ↓
直接进入 ChatGPT
```

因此第一版本不做：

```text
自动输入邮箱
自动输入密码
自动处理验证码
```

而采用：

```text
第一次人工登录
+
之后自动复用 Session
```

这样比自动登录流程稳定得多。

---

# 8. Prompt 自动输入

目标：

```text
CrossLLM Prompt
        ↓
ChatGPT Composer
```

实现步骤：

```text
定位 Composer
    ↓
fill / insert prompt
    ↓
发送 Enter
```

如果 ChatGPT 页面未来输入区域由：

```text
<textarea>
```

改变成：

```text
contenteditable
```

只修改 Browser Adapter，不修改 CrossLLM Core。

---

# 9. 回答完成判断

不使用固定：

```python
sleep(10)
```

作为唯一完成标准。

优先采用 UI 状态判断。

核心判断：

```text
发送成功
    ↓
Stop Button 出现
    ↓
模型生成
    ↓
Stop Button 消失
    ↓
Copy Button 可用
    ↓
回答完成
```

推荐完成条件：

```text
Stop Button 已消失
+
当前 Assistant Turn 已存在
+
Copy Button 已出现/可用
```

必要时增加短时间内容稳定检测作为保险。

---

# 10. 为什么优先使用 Copy

优先方案：

```text
Assistant Turn
    ↓
Copy
    ↓
Clipboard
    ↓
文本
```

而不是直接解析大量 DOM。

原因：

```text
DOM 中可能包含：
按钮
思考状态
工具 UI
引用
隐藏节点
Markdown 渲染结构
```

Copy 按钮通常直接代表：

```text
这一轮回答已经形成可复制内容
```

因此可以把 Copy 当成非常重要的完成/提取信号。

---

# 11. Response Extraction

优先级：

```text
第一优先：
Copy Button → Clipboard

第二优先：
Assistant Turn → innerText

第三优先：
DOM 备用解析
```

最终返回：

```python
str
```

例如：

```text
"这是 ChatGPT 对问题的分析……"
```

Provider 不返回浏览器对象。

不把：

```text
Page
Locator
BrowserContext
```

泄露给 CrossLLM Core。

---

# 12. 最新回答定位

不得简单：

```python
page.locator(".assistant").first
```

必须定位**当前本轮最新 Assistant Turn**。

推荐逻辑：

```text
发送前记录已有 Assistant Turns 数量
                ↓
发送 Prompt
                ↓
等待新的 Assistant Turn
                ↓
取得最后一个 Assistant Turn
                ↓
读取该 Turn
```

这样不会误读取上一轮回答。

---

# 13. 与 CrossLLM Round 机制结合

Browser Provider 不负责 Memory。

CrossLLM Core 自己负责：

```text
Round 1
Round 2
Round 3
...
```

例如：

```text
Round 1

ChatGPT Web
→ Answer A

Qwen API
→ Answer B

Gemini API
→ Answer C
```

用户关闭 Gemini。

Round 2：

```text
ChatGPT Web

自己的 Round 1：
Answer A

其他 ACTIVE：
Qwen：
Answer B

用户补充：
……

请重新分析。
```

然后 CrossLLM 将最终 Prompt 交给：

```text
ChatGPTWebProvider.generate()
```

Browser Provider 不需要理解：

```text
Round
ACTIVE
CLOSED
Memory
Voting
Consensus
```

这些全部属于 Core。

---

# 14. 网页 Memory 的处理原则

不依赖 ChatGPT Web 自身 Memory。

即使 ChatGPT 页面拥有：

```text
历史会话
Saved Memory
Project Context
```

CrossLLM 也不把这些作为系统依赖。

CrossLLM 自身需要的信息必须显式构造成：

```text
prompt
```

再传给 Browser Provider。

这样可以保证：

```text
API Provider
Browser Provider
Local Provider
```

行为一致。

---

# 15. 浏览器会话模型

第一版本建议：

```text
每一个网页模型
=
一个独立 Persistent Profile
```

例如：

```text
profiles/
├── chatgpt/
├── claude/
└── gemini/
```

当前只实现：

```text
profiles/chatgpt/
```

不要所有网站共享 Profile。

原因：

```text
账号
Cookie
Session
LocalStorage
站点状态
```

都应该隔离。

---

# 16. ChatGPT Web Provider 配置

推荐配置形式：

```json
{
  "id": "chatgpt-web",
  "type": "browser",
  "provider": "chatgpt",
  "enabled": true,
  "browser_profile": "profiles/chatgpt",
  "headless": false
}
```

第一版本：

```text
headless = false
```

优先用于开发和排障。

等稳定以后，再考虑是否支持 Headless。

---

# 17. 错误处理

Browser Provider 至少需要区分：

```text
BROWSER_START_FAILED
PAGE_LOAD_FAILED
LOGIN_REQUIRED
COMPOSER_NOT_FOUND
SEND_FAILED
RESPONSE_TIMEOUT
STOP_BUTTON_NOT_FOUND
COPY_BUTTON_NOT_FOUND
RESPONSE_EXTRACTION_FAILED
```

这些错误不能让 CrossLLM Core 崩溃。

Provider 应返回统一错误结果，由 Core 决定：

```text
继续其他模型
标记本模型失败
重试
等待人工处理
```

---

# 18. 超时与重试

第一版本不做复杂自动重试。

最低要求：

```text
页面打开超时
Prompt 发送超时
回答生成超时
文本提取失败
```

均有明确错误。

建议：

```text
第一次失败
    ↓
保存日志 / 截图
    ↓
Provider 标记失败
```

避免：

```text
无限重试
```

---

# 19. 调试信息

开发阶段建议保存：

```text
browser_provider.log
```

至少记录：

```text
Provider ID
Prompt 开始时间
发送时间
Stop Button 状态
Copy Button 状态
回答完成时间
提取文本长度
异常
```

发生网页 DOM 问题时，可以额外保存：

```text
screenshot
HTML snapshot
```

用于更新 selector。

---

# 20. 第一版开发顺序

严格按照以下顺序开发：

```text
1.
Playwright 初始化

↓
2.
Persistent Profile

↓
3.
手动登录 ChatGPT

↓
4.
Composer 定位

↓
5.
自动输入 Prompt

↓
6.
自动发送

↓
7.
Stop Button 检测

↓
8.
最新 Assistant Turn 定位

↓
9.
Copy Button

↓
10.
Clipboard 获取

↓
11.
返回 str

↓
12.
包装成 ChatGPTWebProvider

↓
13.
接入现有 Provider Manager

↓
14.
Round 1 测试

↓
15.
Round 2 互评测试
```

只有上述闭环稳定以后，才考虑：

```text
自动登录 / Session 恢复优化
```

---

# 21. 后续阶段：登录自动化

登录问题单独作为后续功能。

第一阶段目标：

```text
用户人工登录一次
        ↓
Persistent Profile
        ↓
之后自动复用
```

不是：

```text
自动填写账号密码
```

后续如果需要自动恢复登录，只处理：

```text
Session 仍有效
Session 失效
需要人工重新登录
```

CrossLLM 可以检测：

```text
LOGIN_REQUIRED
```

并暂停该 Provider，而不是强行自动登录。

---

# 22. 多网页 LLM 扩展

ChatGPT 成功后再考虑：

```text
ClaudeWebProvider
GeminiWebProvider
DeepSeekWebProvider
```

统一：

```python
class BrowserLLMProvider:
    async def generate(prompt: str) -> str:
        ...
```

而具体网页逻辑隔离：

```text
browser/
├── chatgpt.py
├── claude.py
└── gemini.py
```

不要为了多个网站提前建立复杂的 Universal Browser Agent。

---

# 23. MCP 不属于本阶段

本阶段：

```text
CrossLLM
    ↓
ChatGPTWebProvider
    ↓
Playwright
    ↓
Browser
```

不增加：

```text
MCP Client
MCP Server
Browser MCP
```

原因：

MCP 解决的是工具/上下文协议问题，而当前需求只有：

```text
输入 Prompt
获取回答
```

等 Browser Provider 本身稳定以后，如果其他项目确实需要 Browser Tool，再独立考虑 MCP Adapter。

---

# 24. 最终目标架构

```text
                         CrossLLM Core
                               │
                     Provider Manager
                               │
             ┌─────────────────┼─────────────────┐
             │                 │                 │
             ▼                 ▼                 ▼
        MockProvider       APIProvider     BrowserProvider
                                                │
                                                ▼
                                      ChatGPTWebProvider
                                                │
                           ┌────────────────────┼───────────────────┐
                           │                    │                   │
                           ▼                    ▼                   ▼
                       Playwright         Persistent Profile    Selectors
                           │
                           ▼
                     ChatGPT Web
                           │
                  ┌────────┴────────┐
                  │                 │
                  ▼                 ▼
             Composer          Assistant Turn
                  │                 │
                  ▼                 ▼
               Input           Stop / Copy
                                    │
                                    ▼
                               Clipboard
                                    │
                                    ▼
                                  str
                                    │
                                    ▼
                              CrossLLM Core
```

---

# 25. 核心开发结论

本需求的核心并不是开发一个浏览器 Agent，而是：

> **把网页 LLM 封装成与 API LLM 相同输入输出契约的 Provider。**

最小闭环：

```text
Prompt
  ↓
Playwright
  ↓
ChatGPT Web
  ↓
Send
  ↓
Wait
  ↓
Copy
  ↓
Text
  ↓
Provider Response
  ↓
CrossLLM
```

主要代码来源：

```text
第一参考：
chatgpt-pro-web

辅助参考：
chatgpt-py

不采用：
ChatALL
```

推荐实现策略：

```text
抄实现思路
≠
Fork 整个项目

借鉴：
Playwright 初始化
Composer 定位
Stop Button
Copy
Clipboard
Persistent Profile

自己实现：
ChatGPTWebProvider
```

最终要求：

```text
Core 不知道网页存在
Provider 不知道 Round 如何运作
Browser 层不负责 Memory
```

这三条必须保持。

---

# 26. Phase4 后的进入条件

只有在以下内容全部稳定后，才进入 Browser Provider：

```text
Phase4 Core Loop ✅
Provider 抽象 ✅
ACTIVE / CLOSED ✅
Round 1 ✅
Round 2 ✅
Persistence ✅
错误状态管理 ✅
```

进入 Browser Provider 后：

```text
阶段 A：
ChatGPT Web + 手动登录

阶段 B：
自动输入 + 自动获取

阶段 C：
接入 Round 1

阶段 D：
接入 Round 2

阶段 E：
Persistent Profile 稳定化

阶段 F：
登录恢复

阶段 G：
其他 Web LLM
```

不要在 Phase4 尚未完成时提前实现上述 Browser Provider。

---

# 27. 记忆锚点

后续继续开发本功能时，应默认以下决策已经确定：

**主代码参考：`chatgpt-pro-web`。**

**辅助参考：`chatgpt-py` 的 browser / session / Python API 设计。**

**不使用 ChatALL。**

**不使用 MCP。**

**第一版本只做 ChatGPT Web。**

**只要求：自动输入 Prompt → 自动等待 → 自动获取回答文本。**

**回答完成优先根据 Stop Button + Copy Button 判断。**

**回答正文优先通过 Copy → Clipboard 获取。**

**第一次登录允许人工完成，之后使用 Persistent Profile。**

**不依赖 ChatGPT 网页 Memory；CrossLLM 自己构造上下文。**

**最终必须封装成普通 `ChatGPTWebProvider`，不能污染 CrossLLM Core。**