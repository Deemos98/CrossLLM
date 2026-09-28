请对当前 CrossLLM 做一次 **纯 UI/UX 视觉重构**。

目标不是增加功能，而是解决当前 UI 的两个主要问题：

1. 当前暗色界面比较普通，层级感弱。
2. Session / Round / Summary / Project Memory 等区域横向堆叠，内容拥挤，不像成熟产品。

请参考成熟 Web App 的界面范式，重点参考 **Linear 的 sidebar + 清晰层级设计**以及 **shadcn/ui dashboard/sidebar 的卡片、侧栏、semantic color token 思路**。不要复制具体产品，只借鉴布局和视觉原则。

参考：

* Linear：强调 sidebar、header、panel 的层级、减少视觉噪声和提高信息密度。
* shadcn/ui：sidebar + card + background/card/muted/accent/border 的分层设计。

## 一、目标布局

将当前整体 UI 调整为：

```text
┌──────────────┬──────────────────────────────────────┐
│              │ Header / 当前 Session                │
│   Sidebar    ├──────────────────────────────────────┤
│              │                                      │
│ Session      │          Conversation                │
│ History      │                                      │
│              │   Round Card                         │
│ Project      │   ┌──────────┐ ┌──────────┐          │
│ Memory       │   │Candidate │ │Candidate │          │
│              │   │    A     │ │    B     │          │
│ Settings     │   └──────────┘ └──────────┘          │
│              │                                      │
│              │   Round Card                         │
│              │                                      │
├──────────────┴──────────────────────────────────────┤
│ Context / Controls / Composer                       │
└─────────────────────────────────────────────────────┘
```

### Sidebar

Session History 移到左侧 Sidebar。

Session 使用真正的卡片/列表项，而不是连续横向堆叠：

```text
TODAY

┌────────────────────────┐
│ FastAPI 教程测试       │
│ 17 rounds · 2 min ago  │
└────────────────────────┘

┌────────────────────────┐
│ 香蕉记忆测试           │
│ 5 rounds · 1 h ago     │
└────────────────────────┘

YESTERDAY
...
```

当前 Session 使用轻微 active background/highlight。

Session 数量多时 Sidebar 自己滚动，不影响主对话区域。

保留：

* 新建 Session
* Session 切换
* 当前 Session 标记
* 时间
* round 数
* 问题摘要

不得修改 Session 数据模型或 API 语义。

### Main Conversation

Round 改为真正的 Card。

每个 Round：

```text
┌────────────────────────────────────────────┐
│ R1                              2 Models    │
│────────────────────────────────────────────│
│ User Question                              │
│                                            │
│ ┌──────────────────┐ ┌──────────────────┐ │
│ │ Candidate A      │ │ Candidate B      │ │
│ │                  │ │                  │ │
│ │ answer...        │ │ answer...        │ │
│ └──────────────────┘ └──────────────────┘ │
│                                            │
│ metadata / tokens / cost / status           │
└────────────────────────────────────────────┘
```

重点：

* 多模型回答横向并列
* Round 是外层 Card
* Candidate 是内层 Card
* User Question 与模型回答有明显视觉层级
* Peer Review 使用独立 badge / section
* 不要让边框过多
* 不要每一行都画分割线

### Summary / Pin / MCP / Evidence / Project Memory

不要继续全部占用主页面纵向空间。

它们属于辅助信息，优先采用：

* compact toolbar
* badge
* collapsible panel
* drawer / side panel

其中 Project Memory 可以作为 Sidebar 的一个独立区域或可展开面板。

**当前不要实现 Project Memory M3 Retrieval / Context Injection。**

现有：

```text
Candidate Memory
Confirmed Memory
```

功能必须继续工作，只改变视觉呈现。

### Dark Theme

重新建立明确的层级：

```text
page background
    ↓
sidebar
    ↓
card
    ↓
nested card
    ↓
hover / active
```

不要使用“纯黑背景 + 大量亮边框”的方式。

采用类似成熟 dashboard 的 semantic token 思路：

* background
* sidebar
* card
* muted
* accent
* border
* foreground
* muted-foreground

颜色可以继续保持暗色，但要明显改善层级、对比度和可读性。

不要为了“高级感”加入大量渐变、玻璃拟态、发光、霓虹效果。

目标是：

**专业、克制、信息密度高、长时间阅读舒服。**

### Typography

改善：

* 标题层级
* Round 标题
* Candidate 标题
* metadata
* 辅助说明

避免所有文字大小几乎一样。

### Spacing

减少目前“每个区域都横向占一整行”的问题。

使用：

* 统一 8px spacing scale
* card padding
* section gap
* sidebar item gap

但不要为了留白而降低信息密度。

### Responsive

至少保证：

* 1920×1080 桌面显示舒服
* 1366×768 不发生严重拥挤
* 窄窗口时 Sidebar 可以折叠
* Candidate 卡片空间不足时可以纵向排列

## 二、严格禁止

这是一次 UI 重构，不要借机修改业务逻辑。

禁止修改：

* Context Selection
* Context Budget
* k
* Compaction
* Pin
* Summary 后端语义
* Peer Review
* MCP Evidence
* Project Memory 数据模型
* Project Memory M3 Retrieval / Injection
* Session 数据结构
* Round 并发 / SSE
* API 返回语义
* Prompt
* Memory 数据存储
* Raw Archive

不要删除现有功能。

不要因为 UI 重构而重写整个前端架构。

优先在现有 vanilla HTML/CSS/JS 基础上完成。

## 三、可以修改

允许修改：

* `static/index.html`
* `static/style.css`
* `static/app.js`

如确实需要其它文件，先说明原因，不要直接扩大范围。

## 四、验收要求

完成后必须：

1. 运行现有 pytest。
2. 运行 Phase 4 E2E。
3. 确认 Session History 仍可切换。
4. 确认 Round SSE 仍正常。
5. 确认 Summary UI 仍正常。
6. 确认 Project Memory Candidate / Confirm UI 仍正常。
7. 确认没有修改 Phase 4 的数据和语义。

另外请做一次真实浏览器 UI 验收，而不是只用 jsdom。

重点检查：

* 1920×1080
* 1366×768
* Session 很多时
* Round 很多时
* 两个 Candidate
* Summary 结果
* Project Memory
* 当前 Session 高亮
* Sidebar 滚动
* Main conversation 滚动

最终报告请给出：

* 修改文件
* UI 结构变化
* 是否修改业务逻辑
* pytest 结果
* E2E 结果
* 浏览器人工验收结果
* 如发现现有 UI/代码缺陷，只报告，不顺手修复无关问题。
