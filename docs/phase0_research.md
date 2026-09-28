# CrossLLM — Multi-LLM Cross-Review Workbench Phase 0：Reference Research

## Version: v0.1

## 1. 任务

本阶段只进行参考项目源码侦察，不实现 项目部署。

开始前必须阅读：

`docs/architecture_v0.1.md`

该文件是本项目总架构规范，本阶段不得擅自改变其中定义。

本阶段目标：

1. 获取三个 GitHub Reference Project。

2. 检查 LICENSE。

3. 分析与 CrossLLM  相关的源码。

4. 输出 `reference_analysis.md`。

5. 完成后停止，不进入 Phase 1。

---

## 2. Reference Projects

放入：

```text

reference/

├── ensemble/

├── llm-council/

└── Local-LLM-Arena/

项目：

raiyanyahya/ensemble

niveshdandyan/llm-council

sammy995/Local-LLM-Arena

优先使用浅克隆：

git clone --depth 1

这些仓库仅用于研究。

不得修改 reference 仓库。

不得自动将其安装为 CrossLLM  运行时依赖。

开始阶段不要启动这些项目。

3. Token / Context 控制

本任务重点是源码分析，不是输出大量操作日志。

命令

可能产生大量输出时必须限制。

优先使用：

head

tail

sed

rg

grep

find

git ls-files

git log --oneline -n N

不要输出完整：

安装日志

编译日志

测试日志

完整 git log

大型文件

超过约 100 行的命令输出必须过滤、分页或摘要。

文件

采用：

目录结构

→ 关键词搜索

→ 定位文件

→ 局部读取

不要递归读取整个仓库。

不要为了“完整理解”读取大量无关源码。

默认忽略

.git/

node_modules/

dist/

build/

target/

coverage/

cache/

vendor/

generated/

二进制文件

图片

字体

大型测试数据

除非确认其中存在当前分析必须的信息。

重复读取

已经完整读取过的文件，不要再次读取。

需要补充信息时只读取相关行。

4. 运行规则

当前阶段默认：

不安装依赖

不启动服务器

不运行完整测试

不执行 docker compose

不构建完整项目

只有当某个架构问题无法通过源码确认时，才允许执行最小必要操作。

如果必须安装或测试：

使用 quiet / minimal output。

只保留必要结果。

同类错误最多自动重试 2 次。

两次仍失败则停止重复尝试并记录原因。

不得因为某个项目存在 dependency warning、deprecated package 或其他问题而擅自修复 reference 仓库。

5. 源码侦察顺序

严格按照：

ensemble

↓

llm-council

↓

Local-LLM-Arena

首先检查实际存在的：

README

LICENSE

顶层目录

主要入口

package.json

pyproject.toml

requirements.txt

Dockerfile

docker-compose.yml

然后根据需求搜索相关实现。

6. 重点分析内容

ensemble

重点：

round

proposal

review

rebuttal

state

persistence

filesystem

resume

MCP

grounding

llm-council

重点：

multi-model

parallel

streaming

SSE

model selector

frontend

session

Local-LLM-Arena

重点：

model card

side-by-side

streaming

model selection

frontend

Ollama

对每个项目回答：

整体架构是什么？

多模型如何调用？

是否支持并发和 Streaming？

Round / Debate 如何组织？

Session / State / Response 如何保存？

UI 如何实现多模型并排展示？

是否支持 MCP / Web / Grounding？

哪些部分适合 MAD-Hub？

哪些部分只能参考？

哪些部分与 MAD-Hub 冲突？

尤其检查是否存在：

vote

consensus

final judge

winner selection

automatic synthesis

这些逻辑是否应被 MAD-Hub 采用，以 architecture_v0.1.md 为准，不得自行改变。

7. LICENSE

每个项目记录：

Project:

License:

Relevant restrictions:

Reuse recommendation:

分类：

Reuse if license permits

Adapt

Reference only

Do not use

未确认 LICENSE 前，不得假设代码可以直接复制。

不要修改 reference 项目。

8. 输出

只新增：

reference_analysis.md

报告结构：

# Reference Project Analysis

## 1. ensemble

### Architecture

### Relevant Modules

### Persistence

### MCP / Web

### Reusable Ideas

### Conflicts with MAD-Hub

### License

## 2. llm-council

### Architecture

### Relevant Modules

### Persistence

### MCP / Web

### Reusable Ideas

### Conflicts with MAD-Hub

### License

## 3. Local-LLM-Arena

### Architecture

### Relevant Modules

### Persistence

### MCP / Web

### Reusable Ideas

### Conflicts with MAD-Hub

### License

## 4. Comparison

| Capability | ensemble | llm-council | Local-LLM-Arena |

|---|---|---|---|

| Multi-model | | | |

| Parallel | | | |

| Streaming | | | |

| Round | | | |

| Persistence | | | |

| MCP | | | |

| Web | | | |

| UI | | | |

## 5. CrossLLM  Recommendation

### Reuse if license permits

### Adapt

### Reference only

### Do not use

## 6. Phase 1 Recommendation

9. 禁止事项

本阶段禁止：

实现 MAD-Hub 业务代码

修改 reference 仓库

安装 LangGraph

安装 AutoGen

安装 CrewAI

接入真实 LLM

配置真实 MCP

配置 Google / Bing / Yandex

创建完整 UI

实现 Model Router

实现 Round Manager

不要自行扩展产品需求。

10. 完成标准

以下全部满足才算 Phase 0 完成：

[ ] 三个 reference repo 已获取

[ ] LICENSE 已检查

[ ] 相关源码已完成必要分析

[ ] reference_analysis.md 已生成

[ ] reference 仓库未修改

[ ] MAD-Hub 业务代码未实现

[ ] 未引入大型 Agent Framework

[ ] 未进行无意义的大规模安装/测试

完成后只汇报：

三个项目最值得借鉴的部分。

与 MAD-Hub 的主要冲突。

推荐采用的组合。

Phase 1 建议。

不要输出大量命令日志。

然后 TRAE 聊天框里只需要这一句：

```text

请读取并严格遵守：

\CrossLLM\architecture_v0.1.md.md

执行 Phase 0，完成后停止，不要进入 Phase 1。