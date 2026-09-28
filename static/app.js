/* CrossLLM 前端 —— Vanilla JS，零依赖（phase1_mock.md §17）。
 *
 * 定位：机制验证载体 + 个人可用工具，不做产品化打磨。
 *
 * 三个层次不要混淆（phase1_1 §2/§3）：
 *   1. 启用 / 已停用      —— 会话生命周期状态，长期有效
 *   2. 本轮参与勾选       —— 单次发送发给谁，是启用状态的子集
 *   3. 互评开关          —— 只决定是否注入 peer context，
 *                          不影响模型自己的普通聊天上下文
 *
 * 这里显示的是**真实 model_id 对应的展示名** —— 给人类用户看的。
 * 匿名化只作用于进入模型 context 的内容（不变量 A3/A4），与 UI 无关。
 */

'use strict';

// 展示名属于 UI 职责。Phase 1 固定池用这张表兜底；
// 配置驱动时后端 /api/session 会返回真实 id 列表，展示名回退用 id 本身。
const FALLBACK_LABELS = { qwen: 'Qwen', deepseek: 'DeepSeek', gpt: 'GPT' };
const STORAGE_KEY = 'crossllm.sessionId';

// ---------- 内部值 → 用户可见中文 ----------
//
// 内部状态与枚举值**一律不改**（后端契约、CSS class、请求体都依赖它们），
// 只在这里做显示层映射：内部数据模型 ≠ 用户显示文本。
const STATUS_LABELS = {
  pending: '等待中', streaming: '生成中', complete: '完成',
  error: '失败', skipped: '已跳过',
};
const KIND_LABELS = {
  fact: '已确认事实', decision: '决策', experiment: '实验结果',
  rejected: '已排除方案', open_question: '待解决问题',
  evidence_reference: '证据引用',
};
const SEMANTICS_LABELS = {
  opinion: '模型观点', decision: '用户决策',
  evidence: '证据', result: '实验结果',
};
const SOURCE_LABELS = {
  round: '轮次回答', conversation_summary: '对话摘要',
  mcp_evidence: 'MCP 证据', user_input: '用户输入',
};
const TRACE_LABELS = {
  found: '可定位到运行时记录',
  runtime_missing: '运行时记录缺失',
  runtime_corrupted: '运行时记录损坏',
  record_not_found: '未找到对应记录',
};

// 失败态卡片的正文文案：CSS 对空的 .card-body 会显示「等待输出…」占位，
// 而失败模型本就没有正文，因此必须写入明确文案，不能让占位残留在失败态下。
const FAILED_BODY_TEXT = '本模型本轮未能生成回答';

function labelOf(map, key) {
  if (!key) return '—';
  return map[key] || key;
}
function statusLabel(status) { return labelOf(STATUS_LABELS, status); }

const els = {
  sessionId: document.getElementById('session-id'),
  question: document.getElementById('question'),
  instruction: document.getElementById('instruction'),
  rounds: document.getElementById('rounds'),
  btnCreate: document.getElementById('btn-create'),
  btnSend: document.getElementById('btn-send'),
  btnReset: document.getElementById('btn-reset'),
  pickerBar: document.getElementById('picker-bar'),
  picker: document.getElementById('model-picker'),
  modeHint: document.getElementById('mode-hint'),
  peerReview: document.getElementById('peer-review'),
  sendBar: document.getElementById('send-bar'),
  // S3.6：对话摘要 UI 元素
  //   #summary-toggle  —— 摘要开关（勾选 → 输入框填入默认摘要提示词）
  //   #summary-hint    —— 明确提示"已进入摘要模式"
  //   #summary-results —— 摘要结果卡片（不进 #rounds）
  summaryToggle: document.getElementById('summary-toggle'),
  summaryHint: document.getElementById('summary-hint'),
  summaryResults: document.getElementById('summary-results'),
  summaryBanner: document.getElementById('summary-banner'),
  summaryBannerText: document.getElementById('summary-banner-text'),
  // S3.10 / S3.12：唯一 k 控件（会话级 k，随每次发送提交）
  //   k-slider / k-value —— 页面唯一的 k 控件，readRoundPolicy() 读它，
  //   不再有 per-request 的"本轮 k"覆盖（避免双 k 状态）。
  kSlider: document.getElementById('k-slider'),
  kValue: document.getElementById('k-value'),
  // S3.9：证据面板元素（用户入口在顶栏，面板自身默认收起）
  evidencePanel: document.getElementById('evidence-panel'),
  evidenceTbody: document.getElementById('evidence-tbody'),
  evidenceStats: document.getElementById('evidence-stats'),
  evidenceRefresh: document.getElementById('evidence-refresh'),
  evidenceToggle: document.getElementById('evidence-toggle'),
  evidenceEmpty: document.getElementById('evidence-empty'),
  // Phase 4.6 / M2：项目记忆最小 UI
  memoryPanel: document.getElementById('memory-panel'),
  memoryStats: document.getElementById('memory-stats'),
  memoryRefresh: document.getElementById('memory-refresh'),
  memoryComposeToggle: document.getElementById('memory-compose-toggle'),
  memoryComposeForm: document.getElementById('memory-compose-form'),
  memoryContent: document.getElementById('memory-content'),
  memoryKind: document.getElementById('memory-kind'),
  memorySemantics: document.getElementById('memory-semantics'),
  memorySource: document.getElementById('memory-source'),
  memoryRefs: document.getElementById('memory-refs'),
  memoryCandidateSubmit: document.getElementById('memory-candidate-submit'),
  memoryCandidateCancel: document.getElementById('memory-candidate-cancel'),
  memoryCandidates: document.getElementById('memory-candidates'),
  memoryNotice: document.getElementById('memory-notice'),
  memoryList: document.getElementById('memory-list'),
  pinBanner: document.getElementById('pin-banner'),
  pinBannerText: document.getElementById('pin-banner-text'),
  // 会话历史：只读导航（GET /api/sessions + GET /api/session/{id}）
  sessionHistory: document.getElementById('session-history'),
  sessionHistoryList: document.getElementById('session-history-list'),
  sessionHistoryRefresh: document.getElementById('session-history-refresh'),
  // UI 外壳（纯展示层：顶栏标题、底栏、窄窗口侧栏抽屉）
  app: document.getElementById('app'),
  sidebarToggle: document.getElementById('sidebar-toggle'),
  sidebarScrim: document.getElementById('sidebar-scrim'),
  sessionTitle: document.getElementById('session-title'),
  setupBar: document.getElementById('setup-bar'),
  conversation: document.getElementById('conversation'),
};

// S3.6：默认摘要提示词的**单一真源在后端**
// （core/summary_prompt.py → GET /api/config/summary-prompt）。
// 前端不再硬编码一份副本 —— 否则会出现"UI 显示的内容与后端实际用的不一致"。
// 启动 / 首次勾选时拉取并缓存。
const SUMMARY_DEFAULT_PROMPT_ENDPOINT = '/api/config/summary-prompt';

const state = {
  sessionId: null,
  models: [],
  // 每个正在生成的轮次一条独立连接：round_id → EventSource。
  // 旧实现只有一条连接，发送下一轮时会把上一轮关掉 —— 那会让
  // 上一轮的 UI 停在半路（后端其实还在跑）。并发轮次是允许的，
  // 因此连接必须按 (session, round) 隔离，互不影响。
  streams: new Map(),
  // pending 只表示"这一次发送请求在飞"，**不**表示整个会话
  // 正在生成。它在请求返回后即复位，因此不会阻止用户继续发送下一轮。
  pending: false,
  pinnedRoundIds: [],
  // S3.6：当前会话全部模型 latest 摘要（来自 GET /api/session/{id}）
  summaries: {},
  // S3.6：摘要模式状态机
  //   enabled     —— 当前是否处于摘要模式
  //   draftBackup —— 勾选前输入框里的内容（仅首次勾选时备份）
  //   userEdited  —— 勾选后用户是否改过输入框
  summaryMode: { enabled: false, draftBackup: null, userEdited: false },
  // S3.6：后端下发的默认摘要提示词（未取到前为 null）
  summaryDefaultPrompt: null,
  // S3.10：无损历史保留比例 k（0.01–0.90）
  k: 0.5,
  // S3.9：证据面板是否由用户展开（默认收起；顶栏「证据」按钮切换）
  evidenceOpen: false,
};

// ---------- 工具 ----------

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text; // 始终用 textContent，避免注入
  return node;
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!res.ok) {
    throw new Error(`请求失败 ${res.status}: ${await res.text()}`);
  }
  return res.json();
}

function labelFor(modelId) {
  return FALLBACK_LABELS[modelId] || modelId;
}

function selectedIds() {
  return state.models.filter((m) => m.selected && m.active).map((m) => m.id);
}

// ---------- 模型选择条 ----------

function renderPicker() {
  els.picker.innerHTML = '';
  state.models.forEach((m) => {
    const chip = el('label', 'chip');
    if (!m.active) chip.classList.add('is-closed');

    const box = document.createElement('input');
    box.type = 'checkbox';
    box.checked = m.selected;
    // phase1_1 §2.2：已停用模型的 checkbox 必须 disabled
    box.disabled = !m.active;
    box.addEventListener('change', () => {
      m.selected = box.checked;
      updateSendState();
    });
    chip.appendChild(box);
    chip.appendChild(el('span', 'chip-name', m.displayName));

    const toggle = el('button', 'chip-toggle', m.active ? '启用' : '已停用');
    toggle.type = 'button';
    toggle.title = '启用 / 已停用表示该模型是否继续参与后续轮次。已停用只代表停止参与，不代表该模型错误。';
    toggle.addEventListener('click', () => void onToggleLifecycle(m.id));
    chip.appendChild(toggle);

    els.picker.appendChild(chip);
  });
  updateSendState();
}

function updateSendState() {
  const n = selectedIds().length;
  els.btnSend.disabled = state.pending || n === 0;

  // 仍在生成中的轮次（可能有多个 —— 允许并发，不人为禁止）
  const running = Array.from(state.streams.keys()).sort((a, b) => a - b);
  const runningHint = running.length
    ? ` · 生成中：${running.map((r) => `第 ${r} 轮`).join('、')}`
    : '';

  // phase1_1 §2.3：0 个不允许发送；1 个单模型；≥2 个群发
  if (n === 0) {
    els.modeHint.textContent = '未选择模型，无法发送';
    els.modeHint.className = 'mode-hint is-warn';
  } else if (n === 1) {
    els.modeHint.textContent = `单模型对话（互评无对象，等价于关闭）${runningHint}`;
    els.modeHint.className = 'mode-hint';
  } else {
    els.modeHint.textContent = `群发：${n} 个模型${runningHint}`;
    els.modeHint.className = 'mode-hint';
  }
}

async function onToggleLifecycle(modelId) {
  const m = state.models.find((x) => x.id === modelId);
  if (!m) return;
  const nextActive = state.models.filter((x) => (x.id === modelId ? !m.active : x.active)).map((x) => x.id);
  if (nextActive.length === 0) {
    alert('至少保留一个启用状态的模型。');
    return;
  }
  try {
    const data = await api(`/api/session/${state.sessionId}/selection`, {
      method: 'POST',
      body: JSON.stringify({ active_models: nextActive }),
    });
    // 以后端返回为准
    const active = new Set(data.state.active_models);
    state.models.forEach((x) => {
      x.active = active.has(x.id);
      if (!x.active) x.selected = false; // 已停用自动取消勾选
    });
    renderPicker();
  } catch (err) {
    alert(err.message);
  }
}

// ---------- 会话 ----------

function syncModelsFromState(sessionState) {
  const active = new Set(sessionState.active_models || []);
  const known = new Map(state.models.map((m) => [m.id, m]));
  state.models = (sessionState.active_models || [])
    .concat(sessionState.closed_models || [])
    .map((id) => {
      const prev = known.get(id);
      return {
        id,
        displayName: labelFor(id),
        active: active.has(id),
        // 新模型默认勾选；已存在的保留用户上次的选择
        selected: prev ? prev.selected && active.has(id) : active.has(id),
      };
    });
}

async function createSession() {
  const question = els.question.value.trim();
  if (!question) {
    alert('请输入问题');
    return;
  }
  const data = await api('/api/session', { method: 'POST', body: JSON.stringify({ question }) });
  state.sessionId = data.session_id;
  localStorage.setItem(STORAGE_KEY, state.sessionId);
  els.sessionId.textContent = state.sessionId;
  els.rounds.innerHTML = '';
  syncModelsFromState(data.state);
  renderPicker();
  els.pickerBar.hidden = false;
  els.sendBar.hidden = false;
  // 已有会话后不再需要"建立会话"这一步（新建入口固定在侧栏）
  if (els.setupBar) els.setupBar.hidden = true;
  renderSessionTitle(question);
  // 第一轮指令默认就是原始问题
  els.instruction.value = question;
  // Phase 4.6 / M2：项目记忆面板（固定 project=default）
  if (els.memoryPanel) els.memoryPanel.hidden = false;
  refreshMemory();
  // 新会话成为当前项：刷新历史列表并高亮
  refreshSessionHistory();
  // S3.6：新会话尚无摘要（latest 从空开始）
  state.summaries = {};
  renderSummaryBanner();
  // S3.9：证据面板同样从空开始
  void refreshEvidence();
}

async function loadSession(sessionId) {
  const data = await api(`/api/session/${sessionId}`);
  state.sessionId = sessionId;
  els.sessionId.textContent = sessionId;
  els.question.value = data.state.original_question;
  syncModelsFromState(data.state);
  renderPicker();
  els.pickerBar.hidden = false;
  els.sendBar.hidden = false;
  if (els.setupBar) els.setupBar.hidden = true;
  renderSessionTitle(data.state.original_question);

  els.rounds.innerHTML = '';
  data.rounds.forEach((r) => {
    const shell = renderRoundShell(r.round_id, r.participants, r.user_instruction, r.peer_review);
    r.participants.forEach((modelId) => {
      const status = r.status[modelId] || 'pending';
      const card = createCard(modelId, status);
      card.querySelector('.card-body').textContent = (r.responses && r.responses[modelId]) || '';
      // 回放失败模型：后端不会为它保存正文，responses 为空 →
      // 不补文案的话 CSS 的 :empty 占位会在「失败」徽标下显示「等待输出…」。
      ensureFailureBody(card, status);
      shell.querySelector('.grid').appendChild(card);
    });
    els.rounds.appendChild(shell);
  });

  // S3.4：刷新固定状态（页面 reload 后恢复）
  state.pinnedRoundIds = Array.isArray(data.pins) ? data.pins : [];
  applyPinStyles();
  renderPinBanner();
  // Phase 4.6 / M2：项目记忆面板
  if (els.memoryPanel) els.memoryPanel.hidden = false;
  refreshMemory();

  // 打开一个会话后必须重新拉取**该会话**的摘要与证据：
  // 否则顶栏会残留上一个会话的状态，或该会话明明有摘要却显示为空。
  await refreshSessionSummaries();
  void refreshEvidence();
}

// ---------- 固定（无损历史）----------

async function togglePin(roundId) {
  // roundId: 整数；按钮文本已显示当前状态
  const isPinned = state.pinnedRoundIds.includes(roundId);
  try {
    const path = isPinned ? 'unpin' : 'pin';
    const res = await api(`/api/session/${state.sessionId}/${path}`, {
      method: 'POST',
      body: JSON.stringify({ round_id: roundId }),
    });
    state.pinnedRoundIds = (res.pinned_round_ids || []);
    applyPinStyles();
    renderPinBanner();
  } catch (err) {
    alert(`固定失败：${err.message}`);
  }
}

function applyPinStyles() {
  // 凡是已固定的轮次加 .is-pinned；按钮文本切换
  document.querySelectorAll('.round').forEach((shell) => {
    const rid = parseInt(shell.dataset.round, 10);
    const btn = shell.querySelector('.pin-toggle');
    if (!btn) return;
    const pinned = state.pinnedRoundIds.includes(rid);
    shell.classList.toggle('is-pinned', pinned);
    btn.textContent = pinned ? '取消固定' : '📌 固定';
    btn.classList.toggle('is-active', pinned);
    btn.title = pinned
      ? '该轮已固定：在自动上下文管理中保持原文优先'
      : '固定该轮：在自动上下文管理中保持原文优先';
  });
}

function renderPinBanner() {
  const banner = document.getElementById('pin-banner');
  const text = document.getElementById('pin-banner-text');
  if (!banner || !text) return;
  if (state.pinnedRoundIds.length === 0) {
    banner.hidden = true;
    banner.title = '';
    return;
  }
  banner.hidden = false;
  // 顶栏用紧凑 badge：可见文本只放数量，完整列表进 title
  text.textContent = `已固定 ${state.pinnedRoundIds.length} 轮`;
  banner.title =
    `已固定（在自动上下文中保持原文优先）：第 ${state.pinnedRoundIds.join('、')} 轮`;
}

// ---------- S3.6：对话摘要 ----------
//
// 设计来源（docs/phase4_memory.md > 代码）：
//   勾选摘要 → 输入框自动清空，并填入系统生成的摘要提示词
//   默认提示词可编辑；旧摘要不删除，最新成功者即 latest
//   完整流程：勾选 → 填入默认提示词 → 可修改 → 发送 → 生成 →
//             保存 summary_XXX.md → 成为该模型最新摘要
//   状态机 {enabled, draftBackup, userEdited}；默认提示词由后端提供
//
// 边界：摘要**不创建轮次**、不写 responses/、结果不进 #rounds 列表；
//       不做检索 / 向量化 / 记忆搜索。

/** 拉取后端默认摘要提示词（单一真源：core/summary_prompt.py）。 */
async function loadSummaryDefaultPrompt() {
  try {
    const data = await api(SUMMARY_DEFAULT_PROMPT_ENDPOINT);
    state.summaryDefaultPrompt = data && data.prompt ? data.prompt : null;
  } catch (err) {
    state.summaryDefaultPrompt = null;
  }
  return state.summaryDefaultPrompt;
}

function renderSummaryModeHint() {
  if (!els.summaryHint) return;
  els.summaryHint.hidden = !state.summaryMode.enabled;
}

/**
 * 摘要开关状态机。
 *
 * 勾选：备份输入框 → 清空 → 填入默认摘要提示词
 * 取消：未编辑过 → 恢复 draftBackup（无则清空）；编辑过 → 保留用户文本
 */
async function onSummaryToggle(checked) {
  const box = els.instruction;
  if (checked) {
    if (!state.summaryDefaultPrompt) await loadSummaryDefaultPrompt();
    if (!state.summaryDefaultPrompt) {
      // 拿不到默认提示词就不进入摘要模式（禁止前端伪造一份）
      if (els.summaryToggle) els.summaryToggle.checked = false;
      alert('无法获取默认摘要提示词，对话摘要功能暂时不可用');
      return;
    }
    state.summaryMode.draftBackup = box.value;      // 备份（仅首次勾选）
    state.summaryMode.userEdited = false;
    box.value = state.summaryDefaultPrompt;         // 清空 + 填入默认提示词
  } else {
    if (!state.summaryMode.userEdited) {
      box.value = state.summaryMode.draftBackup || '';
    }
    state.summaryMode.draftBackup = null;
  }
  state.summaryMode.enabled = !!checked;
  renderSummaryModeHint();
  updateSendState();
}

/** 发送后：摘要模式自动关闭，清空 draft_backup。 */
function resetSummaryMode() {
  state.summaryMode.enabled = false;
  state.summaryMode.draftBackup = null;
  state.summaryMode.userEdited = false;
  if (els.summaryToggle) els.summaryToggle.checked = false;
  renderSummaryModeHint();
}

function renderSummaryBanner() {
  if (!els.summaryBanner) return;
  // state.summaries: { model_id: {summary_id, status, source_valid_round, ...} }
  const models = Object.keys(state.summaries || {})
    .filter((m) => state.summaries[m]);

  // 尚未建立 / 打开会话时不给状态：此时"暂无摘要"没有意义
  if (!state.sessionId) {
    els.summaryBanner.hidden = true;
    els.summaryBanner.classList.remove('is-empty');
    els.summaryBanner.title = '';
    return;
  }

  // 该会话确实没有摘要：给明确文案，而不是留一个空 chip
  if (models.length === 0) {
    els.summaryBanner.hidden = false;
    els.summaryBanner.classList.add('is-empty');
    els.summaryBannerText.textContent = '暂无摘要';
    els.summaryBanner.title = '当前会话还没有生成对话摘要';
    return;
  }

  // 顶栏用紧凑 badge：可见文本只放"模型·来源轮次"，完整信息进 title
  els.summaryBanner.classList.remove('is-empty');
  els.summaryBannerText.textContent = models
    .map((m) => `${labelFor(m)}·第 ${state.summaries[m].source_valid_round} 轮`)
    .join(' / ');
  els.summaryBanner.title = models
    .map((m) => {
      const s = state.summaries[m];
      return `${labelFor(m)}：最新摘要基于第 ${s.source_valid_round} 轮`;
    })
    .join('\n');
  els.summaryBanner.hidden = false;
}

/** 从后端重新读取各模型 latest 摘要，刷新 banner。 */
async function refreshSessionSummaries() {
  if (!state.sessionId) return;
  try {
    const data = await api(`/api/session/${state.sessionId}/summary`);
    state.summaries = data.latest || {};
    renderSummaryBanner();
  } catch (err) {
    // banner 不是关键路径：读失败不阻断摘要结果展示
  }
}

/**
 * 发送一次摘要请求（"直接发送 / 修改提示词再发送"）。
 *
 * 走 POST /round {summary_mode: true}：后端把它当作摘要请求处理
 * （trigger="user"、提示词=输入框文本），**不创建轮次**。
 */
async function sendSummary() {
  const ids = selectedIds();
  if (ids.length === 0) return;
  const prompt = els.instruction.value.trim();

  state.pending = true;
  updateSendState();
  try {
    const data = await api(`/api/session/${state.sessionId}/round`, {
      method: 'POST',
      body: JSON.stringify({
        instruction: prompt,
        selected_models: ids,
        // 摘要只读该模型自己的历史，不消费 peer context
        peer_review: false,
        ...readRoundPolicy(),
        summary_mode: true,
      }),
    });
    renderSummaryResults(data, prompt || state.summaryDefaultPrompt);
    await refreshSessionSummaries();
    els.instruction.value = '';
    resetSummaryMode();
  } catch (err) {
    alert(`生成对话摘要失败：${err.message}`);
  } finally {
    state.pending = false;
    updateSendState();
  }
}

/** 把摘要结果渲染成独立区块（不与普通回答混排）。 */
function renderSummaryResults(data, promptText) {
  const host = els.summaryResults;
  if (!host) return;

  const section = el('section', 'summary-result');
  // 语义标记留在属性上：内部概念不写进用户可见文本
  section.dataset.kind = 'conversation_summary';

  const head = el('div', 'summary-result-head');
  head.appendChild(el('span', 'kind', '对话摘要'));
  head.appendChild(el('span', null, `${(data.results || []).length} 个模型`));
  head.appendChild(el('span', 'summary-result-meta',
    data.round_created ? '已创建轮次' : '未创建轮次（不影响轮次编号）'));
  section.appendChild(head);

  // 可审计：本次实际使用的摘要提示词（不等于模型生成的摘要正文）
  const details = el('details', 'summary-prompt-used');
  details.appendChild(el('summary', null, '本次使用的摘要提示词（可审计）'));
  details.appendChild(el('pre', 'card-body', promptText || '（空 → 使用后端默认提示词）'));
  section.appendChild(details);

  const grid = el('div', 'grid');
  (data.results || []).forEach((r) => {
    const status = r.status === 'success' ? 'complete'
      : (r.status === 'failed' ? 'error' : 'skipped');
    const card = createCard(r.model_id, status);
    const bodyText = r.status === 'success'
      ? (r.content || '')
      : `[未生成] ${r.error_message || statusLabel(status)}`;
    card.querySelector('.card-body').textContent = bodyText;

    // 追溯信息默认收起：用户先看到摘要正文，编号与落盘位置属于详情
    const trace = el('details', 'card-trace');
    trace.appendChild(el('summary', null, '详情（摘要编号 / 来源轮次 / 保存位置）'));
    trace.appendChild(el('pre', 'card-trace-body',
      `摘要编号：${r.summary_id || '—'}\n`
      + `来源有效轮次：${r.source_valid_round == null ? '—' : r.source_valid_round}\n`
      + `保存位置：${r.file_path || '—'}`));
    card.appendChild(trace);
    grid.appendChild(card);
  });
  section.appendChild(grid);
  section.appendChild(el('p', 'summary-result-note',
    '这是一份对话摘要，不是最终回答，也不计入有效轮次；'
    + '旧摘要不会被删除，最新生成的一份成为该模型的工作状态。'));

  host.appendChild(section);
  scrollConversationToEnd();
}

// ---------- 轮次 ----------

async function sendRound() {
  const ids = selectedIds();
  if (ids.length === 0) return;

  // 摘要模式下，"发送"发出的是摘要请求（不创建轮次）
  if (state.summaryMode.enabled) {
    await sendSummary();
    return;
  }

  const instruction = els.instruction.value.trim();
  state.pending = true;
  updateSendState();
  try {
    const data = await api(`/api/session/${state.sessionId}/round`, {
      method: 'POST',
      body: JSON.stringify({
        instruction,
        selected_models: ids,
        peer_review: els.peerReview.checked,
        // S3.10：政策参数全链路透传（UI 滑块 = 请求体 k = 后端上下文预算 k）
        ...readRoundPolicy(),
        // summary_mode 恒为 false：它是"本次发送是否为摘要请求"的开关，
        // 勾选摘要时已在上方转到 sendSummary()，普通轮次不携带它。
        summary_mode: false,
      }),
    });

    const shell = renderRoundShell(
      data.round_id,
      data.participants,
      instruction,
      data.peer_review
    );
    els.rounds.appendChild(shell);
    data.participants.forEach((modelId) => {
      shell.querySelector('.grid').appendChild(createCard(modelId, 'streaming'));
    });

    // S3.4：新轮次也立即按当前固定列表同步按钮样式
    applyPinStyles();

    connectEvents(data.round_id, data.participants, shell);
    els.instruction.value = '';
    scrollConversationToEnd();
  } finally {
    state.pending = false;
    updateSendState();
  }
}

function createCard(modelId, status) {
  const card = el('article', 'card');
  card.dataset.model = modelId;

  const head = el('div', 'card-head');
  head.appendChild(el('span', 'name', labelFor(modelId)));
  // class 保留内部状态值（CSS 依赖它），可见文本走中文映射
  head.appendChild(el('span', `badge ${status}`, statusLabel(status)));
  card.appendChild(head);
  card.appendChild(el('pre', 'card-body', ''));
  return card;
}

function setCardStatus(card, status) {
  const badge = card.querySelector('.badge');
  badge.className = `badge ${status}`;
  badge.textContent = statusLabel(status);
  card.classList.toggle('is-error', status === 'error');
}

// 失败是终态：正文必须非空，否则 style.css 的 .card-body:empty::before
// 会在「失败」徽标下继续显示「等待输出…」占位。成功/生成中逻辑不受影响。
function ensureFailureBody(card, status) {
  if (status !== 'error') return;
  const body = card.querySelector('.card-body');
  if (body && !body.textContent.trim()) body.textContent = FAILED_BODY_TEXT;
}

function appendContent(card, text) {
  card.querySelector('.card-body').textContent += text;
}

function renderRoundShell(roundId, participants, instruction, peerReview) {
  const section = el('section', 'round');
  section.dataset.round = String(roundId);

  // docs/ui_naming.md §5：round → round-header（头部）/ round-content（正文）
  const header = el('div', 'round-header');
  header.appendChild(el('span', 'round-title', `第 ${roundId} 轮`));

  // 元信息做成 badge：参与模型数 + 平级互评开关（不与回答正文混排）
  const status = el('span', 'round-status');
  status.appendChild(el('span', 'badge',
    `${(participants || []).length} 个模型回答`));
  status.appendChild(el('span', `badge ${peerReview ? 'badge-on' : 'badge-off'}`,
    peerReview ? '平级互评 开' : '平级互评 关'));
  header.appendChild(status);

  // S3.4：固定 / 取消固定按钮（用户在自动上下文管理中的控制手段）
  const pinBtn = el('button', 'pin-toggle', '📌 固定');
  pinBtn.type = 'button';
  pinBtn.addEventListener('click', () => void togglePin(roundId));
  header.appendChild(pinBtn);

  section.appendChild(header);

  // 用户指令与模型回答之间要有明确主次：指令单独成块，模型回答在下
  const content = el('div', 'round-content');
  content.appendChild(el('div', 'round-content-label', '本轮指令'));
  content.appendChild(el('div', 'round-content-text', instruction || '（未提供指令）'));
  section.appendChild(content);

  section.appendChild(el('div', 'grid'));
  return section;
}

function closeEventStream() {
  // 关闭当前会话的**全部**连接 —— 只在切换会话 / 重置时用。
  // 普通"再发一轮"绝不能调用它：那会掐断上一轮仍在进行的流。
  state.streams.forEach((es) => es.close());
  state.streams.clear();
}

function connectEvents(roundId, participants, shell) {
  // 一条连接只服务一个轮次；各轮次互不关闭、事件互不串台。
  const es = new EventSource(`/api/session/${state.sessionId}/events/${roundId}`);
  state.streams.set(roundId, es);
  const closeThis = () => {
    es.close();
    state.streams.delete(roundId);
  };

  const finished = new Set();

  const handle = (payload) => {
    // 只在本轮自己的 shell 里找卡片 —— 不会写到其它轮次的卡片上
    const card = shell.querySelector(`.card[data-model="${payload.model}"]`);
    if (!card) return;

    if (payload.type === 'token') {
      appendContent(card, payload.content);
      return;
    }
    if (payload.type === 'done' || payload.type === 'error') {
      const failed = payload.type === 'error';
      setCardStatus(card, failed ? 'error' : 'complete');
      if (failed) {
        // 后端未给出错误详情时不得落下 "[错误] undefined"，直接用明确失败文案
        if (payload.error) appendContent(card, `\n\n[错误] ${payload.error}`);
        ensureFailureBody(card, 'error');
      }
      finished.add(payload.model);
      if (finished.size >= participants.length) {
        // 主动关闭，避免连接在流结束后自动重连导致事件重放
        closeThis();
        updateSendState();
        // 本轮结束后证据索引可能新增条目：同步刷新（失败不影响主流程）
        void refreshEvidence();
      }
    }
  };

  const onPayload = (e) => handle(JSON.parse(e.data));

  es.addEventListener('token', onPayload);
  es.addEventListener('done', onPayload);
  es.addEventListener('error', (e) => {
    // 连接级错误与服务端命名为 error 的事件同名，靠有无 data 区分。
    if (e.data === undefined) {
      closeThis();
      updateSendState();
      return;
    }
    onPayload(e);
  });
}

// ---------- 初始化 ----------

function init() {
  // S3.6：默认摘要提示词来自后端（单一真源）；勾选摘要前先取一次
  void loadSummaryDefaultPrompt();

  els.btnCreate.addEventListener('click', () => {
    els.btnCreate.disabled = true;
    createSession()
      .catch((err) => alert(err.message))
      .finally(() => {
        els.btnCreate.disabled = false;
      });
  });

  els.btnSend.addEventListener('click', () => {
    sendRound().catch((err) => {
      alert(err.message);
      state.pending = false;
      updateSendState();
    });
  });

  els.btnReset.addEventListener('click', () => {
    closeEventStream();
    localStorage.removeItem(STORAGE_KEY);
    location.reload();
  });

  els.instruction.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey) && !els.btnSend.disabled) {
      sendRound().catch((err) => alert(err.message));
    }
  });

  const saved = localStorage.getItem(STORAGE_KEY);
  const restored = saved
    ? loadSession(saved).catch(() => localStorage.removeItem(STORAGE_KEY))
    : Promise.resolve();
  // 恢复完成后再拉列表，保证当前会话能正确高亮
  restored.finally(() => { refreshSessionHistory(); });
}

document.addEventListener('DOMContentLoaded', init);


// ---------- S3.6：摘要事件注册（状态机 + 默认提示词）----------

if (els.summaryToggle) {
  els.summaryToggle.addEventListener('change', (e) => {
    void onSummaryToggle(!!e.target.checked);
  });
}

// 勾选后用户改过输入框 → 取消勾选时保留其文本（不静默丢弃）
if (els.instruction) {
  els.instruction.addEventListener('input', () => {
    if (state.summaryMode.enabled) state.summaryMode.userEdited = true;
  });
}


// ---------- S3.9：MCP 证据面板（只读索引，不判定事实）----------
//
// 可达性：面板由顶栏「证据」按钮展开/收起。此前面板只能由自身内部的刷新按钮
// 置为可见，用户在页面上没有任何入口 → 永远看不到，现已修复为可达。
// 展示边界：只给「来源 / 工具 / 证据引用 / 是否被回答引用」；
// 内部记录编号与追溯状态收进行的悬停详情，不占主要视觉空间。

function applyEvidenceVisibility() {
  if (!els.evidencePanel) return;
  els.evidencePanel.hidden = !state.evidenceOpen;
  if (els.evidenceToggle) {
    els.evidenceToggle.setAttribute('aria-pressed', state.evidenceOpen ? 'true' : 'false');
    els.evidenceToggle.classList.toggle('is-active', state.evidenceOpen);
  }
}

async function refreshEvidence() {
  if (!state.sessionId || !els.evidencePanel) return;
  try {
    const data = await api(`/api/session/${state.sessionId}/evidence`);
    const entries = data.entries || [];
    els.evidenceStats.textContent =
      `共 ${data.total || 0} 条 · ${data.used_count || 0} 条被回答引用`;
    if (els.evidenceEmpty) els.evidenceEmpty.hidden = entries.length > 0;
    els.evidenceTbody.innerHTML = '';
    entries.forEach((e) => {
      const tr = document.createElement('tr');
      const cells = [
        `第 ${e.round_id} 轮`,
        e.mcp || '—',
        e.tool || '—',
        e.reference || '—',
        e.used_in_final_answer ? '已引用' : '未引用',
      ];
      cells.forEach((c, i) => {
        const td = document.createElement('td');
        td.textContent = c;
        if (i === 4) td.className = e.used_in_final_answer
          ? 'evidence-used-yes' : 'evidence-used-no';
        tr.appendChild(td);
      });
      // 内部编号与追溯状态属于详情，不做主要视觉内容
      tr.title = `追溯状态：${labelOf(TRACE_LABELS, e.trace_status)}`
        + (e.runtime_record_id ? ` · 运行时记录编号：${e.runtime_record_id}` : '');
      els.evidenceTbody.appendChild(tr);
    });
    applyEvidenceVisibility();
  } catch (err) {
    // 证据面板失败不影响主流程
    els.evidencePanel.hidden = true;
  }
}

if (els.evidenceToggle) {
  els.evidenceToggle.addEventListener('click', () => {
    state.evidenceOpen = !state.evidenceOpen;
    applyEvidenceVisibility();
    if (state.evidenceOpen) void refreshEvidence();
  });
}

if (els.evidenceRefresh) {
  els.evidenceRefresh.addEventListener('click', (e) => {
    // 该按钮位于 <summary> 内：阻止点击顺带折叠/展开面板
    if (e && e.preventDefault) e.preventDefault();
    if (e && e.stopPropagation) e.stopPropagation();
    refreshEvidence();
  });
}


// ---------- Phase 4.6 / M2：项目记忆最小 UI ----------
//
// 边界：
//   - 候选只在用户点击「＋ 生成记忆候选」并再次点「创建候选」后产生；
//     普通轮次 / 摘要 / 压缩 / 互评 / 工具调用都不触发
//   - 正式记忆只在用户点「确认保存」后产生
//   - 重复 / 冲突原样展示后端结论，前端不重新判断、不自动覆盖

// M1 裁决：M2 阶段固定使用 default project
const MEMORY_PROJECT_ID = 'default';

function _memoryApi(path, options) {
  return api(`/api/project/${MEMORY_PROJECT_ID}${path}`, options);
}

function _memoryRefsList(raw) {
  return (raw || '')
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);
}

function _memoryShowNotice(kind, text) {
  if (!els.memoryNotice) return;
  els.memoryNotice.hidden = false;
  els.memoryNotice.className = `memory-notice memory-notice-${kind}`;
  els.memoryNotice.textContent = text;
}

function _memoryClearNotice() {
  if (!els.memoryNotice) return;
  els.memoryNotice.hidden = true;
  els.memoryNotice.textContent = '';
}

function _memoryCard(title, lines, actions, variant) {
  const box = el('div', variant
    ? `memory-item memory-item-${variant}` : 'memory-item');
  box.appendChild(el('div', 'memory-item-title', title));
  lines.filter(Boolean).forEach((line) => {
    box.appendChild(el('div', 'memory-item-line', line));
  });
  (actions || []).forEach((btn) => box.appendChild(btn));
  return box;
}

async function refreshMemory() {
  if (!els.memoryPanel) return;
  try {
    const [candidates, memories] = await Promise.all([
      _memoryApi('/memory/candidates'),
      _memoryApi('/memory'),
    ]);
    els.memoryStats.textContent =
      `候选 ${candidates.total} · 已确认 ${memories.total}`;

    els.memoryCandidates.innerHTML = '';
    (candidates.candidates || []).forEach((c) => {
      const confirmBtn = el('button', 'memory-confirm', '确认保存');
      confirmBtn.type = 'button';
      confirmBtn.addEventListener('click', () => void confirmCandidate(c.candidate_id));
      const cancelBtn = el('button', 'memory-cancel', '取消');
      cancelBtn.type = 'button';
      cancelBtn.addEventListener('click', () => {
        _memoryClearNotice();
      });
      const candCard = _memoryCard(
        '记忆候选',
        [
          c.content,
          `类型：${labelOf(KIND_LABELS, c.kind)}`,
          `来源语义：${labelOf(SEMANTICS_LABELS, c.source_semantics)}`,
          `来源：${labelOf(SOURCE_LABELS, c.source)}`,
          c.source_refs && c.source_refs.length
            ? `引用来源：${c.source_refs.join('、')}`
            : '引用来源：—',
        ],
        [confirmBtn, cancelBtn],
        'candidate'
      );
      // 普通可见文本不出现内部 ID；ID 只留在 title 与 DOM data 属性上
      candCard.dataset.candidateId = c.candidate_id;
      candCard.title = `候选编号：${c.candidate_id}`;
      els.memoryCandidates.appendChild(candCard);
    });

    els.memoryList.innerHTML = '';
    (memories.entries || []).forEach((m) => {
      const memCard = _memoryCard(
        '已确认记忆',
        [
          m.content,
          `类型：${labelOf(KIND_LABELS, m.kind)}`,
          `来源语义：${labelOf(SEMANTICS_LABELS, m.source_semantics)}`,
        ],
        [],
        'confirmed'
      );
      // 普通可见文本不出现内部 ID；ID 只留在 title 与 DOM data 属性上
      memCard.dataset.memoryId = m.memory_id;
      memCard.title = `记忆编号：${m.memory_id}`;
      els.memoryList.appendChild(memCard);
    });
  } catch (err) {
    // 项目记忆面板失败不影响主流程
    if (els.memoryPanel) els.memoryPanel.hidden = true;
  }
}

async function createCandidate() {
  const content = (els.memoryContent && els.memoryContent.value || '').trim();
  if (!content) {
    alert('请先填写候选内容。');
    return;
  }
  if (els.memoryCandidateSubmit) els.memoryCandidateSubmit.disabled = true;
  try {
    await _memoryApi('/memory/candidate', {
      method: 'POST',
      body: JSON.stringify({
        kind: els.memoryKind ? els.memoryKind.value : 'decision',
        source_semantics: els.memorySemantics ? els.memorySemantics.value : 'decision',
        content,
        source: els.memorySource ? els.memorySource.value : 'user_input',
        source_refs: _memoryRefsList(els.memoryRefs && els.memoryRefs.value),
      }),
    });
    if (els.memoryContent) els.memoryContent.value = '';
    if (els.memoryComposeForm) els.memoryComposeForm.hidden = true;
    await refreshMemory();
  } catch (err) {
    alert(`创建记忆候选失败：${err.message}`);
  } finally {
    if (els.memoryCandidateSubmit) els.memoryCandidateSubmit.disabled = false;
  }
}

async function confirmCandidate(candidateId) {
  try {
    const res = await _memoryApi(
      `/memory/candidate/${candidateId}/confirm`,
      { method: 'POST' }
    );
    if (res.status === 'duplicate') {
      // 重复：明确告知，不伪装成新建成功
      _memoryShowNotice(
        'duplicate',
        '与已有项目记忆重复，未创建新的记忆。\n'
        + `对应条目：${res.duplicate_of}`
      );
    } else if (res.conflicts && res.conflicts.length) {
      // 冲突：新记忆已创建，但必须提示人工核对，且不得自动覆盖旧记忆
      _memoryShowNotice(
        'conflict',
        '检测到可能冲突的项目记忆。\n'
        + '系统不会自动覆盖旧内容，请人工核对。\n'
        + `可能冲突的已有条目：${res.conflicts.join('、')}\n`
        + `已创建新记忆：${res.entry && res.entry.memory_id}`
      );
    } else {
      _memoryShowNotice(
        'ok',
        `已确认并保存为项目记忆：${res.entry && res.entry.memory_id}`
      );
    }
    await refreshMemory();
  } catch (err) {
    _memoryShowNotice('error', `确认失败：${err.message}`);
  }
}

if (els.memoryComposeToggle) {
  els.memoryComposeToggle.addEventListener('click', () => {
    if (els.memoryComposeForm) els.memoryComposeForm.hidden = !els.memoryComposeForm.hidden;
  });
}
if (els.memoryCandidateSubmit) {
  els.memoryCandidateSubmit.addEventListener('click', () => void createCandidate());
}
if (els.memoryCandidateCancel) {
  els.memoryCandidateCancel.addEventListener('click', () => {
    if (els.memoryComposeForm) els.memoryComposeForm.hidden = true;
    if (els.memoryContent) els.memoryContent.value = '';
  });
}
if (els.memoryRefresh) {
  els.memoryRefresh.addEventListener('click', () => void refreshMemory());
}

// ---------- S3.10：k 滑块（无损历史保留比例；0.01–0.90）----------
// S3.12：页面上只有一个 k 控件（会话级 k），它同时是发送时使用的 k。
if (els.kSlider) {
  els.kSlider.addEventListener('input', (e) => {
    state.k = parseFloat(e.target.value);
    if (els.kValue) els.kValue.textContent = state.k.toFixed(2);
  });
  // 初始显示
  if (els.kValue) els.kValue.textContent = Number(els.kSlider.value).toFixed(2);
}

function readRoundPolicy() {
  // 每次发送读取会话 k 控件的当前值 → 请求体（前后端单一来源）。
  // S3.12：页面只有一个 k 控件，不存在 per-request 覆盖，
  // 因此 UI 显示值 = 请求 k = 后端上下文预算 k。
  // request_max_output 不再由 UI 提供：模型能力上限来自 Provider 元数据，
  // 未显式指定时本次请求的有效输出上限 = 模型能力上限（由后端预算层决定）。
  return {
    k: els.kSlider ? Number(els.kSlider.value) : 0.5,
  };
}

// ---------- UI 外壳（纯展示层：侧栏抽屉 / 顶栏标题）----------
//
// 只有布局职责，不承载任何业务语义：不改 state、不发请求。

/** 窄窗口下侧栏作为浮层抽屉；宽屏该类无视觉效果（CSS 里只在窄屏生效）。 */
function setSidebarOpen(open) {
  if (!els.app) return;
  els.app.classList.toggle('is-sidebar-open', !!open);
}

if (els.sidebarToggle) {
  els.sidebarToggle.addEventListener('click', () => {
    const box = els.app;
    const isOpen = box && box.classList.contains('is-sidebar-open');
    setSidebarOpen(!isOpen);
  });
}
if (els.sidebarScrim) {
  els.sidebarScrim.addEventListener('click', () => setSidebarOpen(false));
}

/** 顶栏当前会话标题（展示用；文本来自已加载的原始问题）。 */
function renderSessionTitle(text) {
  if (!els.sessionTitle) return;
  const oneLine = String(text || '').replace(/\s+/g, ' ').trim();
  els.sessionTitle.textContent = oneLine || '未创建会话';
}

/**
 * 追加了新的轮次 / 摘要结果后，把对话区滚到最新。
 *
 * 纯视口管理：对话区现在是独立滚动容器（底栏固定在下方），
 * 用户从底栏发送时视线并不在对话区末尾，不滚动就等于"发出了但看不到"。
 */
function scrollConversationToEnd() {
  const box = els.conversation;
  if (!box || typeof box.scrollTo !== 'function') return;
  box.scrollTo({ top: box.scrollHeight, behavior: 'smooth' });
}


// ---------- Session History（只读导航）----------
//
// 边界：只做「看到历史会话 + 点开它」。
//   - 只读：GET /api/sessions（列表）+ GET /api/session/{id}（已有 loadSession）
//   - 不做检索、不做上下文注入、不做向量化、不做记忆搜索
//   - 不删除、不重命名、不自动写总结、不打标签
// 切换顺序固定为：关闭旧连接 → loadSession → 写 localStorage → 刷新高亮。

function questionSummary(question, max) {
  const oneLine = String(question || '').replace(/\s+/g, ' ').trim();
  if (!oneLine) return '（无问题）';
  const limit = max || 60;
  return oneLine.length > limit ? `${oneLine.slice(0, limit)}…` : oneLine;
}

/** 相对时间：状态文件存 ISO 8601；导航只关心"多久以前"。 */
function relativeTime(iso) {
  const t = Date.parse(String(iso || ''));
  if (Number.isNaN(t)) return '—';
  const diff = Date.now() - t;
  if (diff < 60000) return '刚刚';
  const minutes = Math.floor(diff / 60000);
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  return `${Math.floor(hours / 24)} 天前`;
}

/** 日期分组的 key（按本地时区切天）。 */
function dayKey(iso) {
  const t = Date.parse(String(iso || ''));
  if (Number.isNaN(t)) return 'unknown';
  const d = new Date(t);
  return `${d.getFullYear()}-${d.getMonth() + 1}-${d.getDate()}`;
}

/** 分组标题：今天 / 昨天 / 具体日期。 */
function dayLabel(iso) {
  const key = dayKey(iso);
  if (key === 'unknown') return '未知日期';
  if (key === dayKey(new Date().toISOString())) return '今天';
  if (key === dayKey(new Date(Date.now() - 86400000).toISOString())) return '昨天';
  const d = new Date(Date.parse(String(iso)));
  if (d.getFullYear() === new Date().getFullYear()) {
    return `${d.getMonth() + 1} 月 ${d.getDate()} 日`;
  }
  return `${d.getFullYear()} 年 ${d.getMonth() + 1} 月 ${d.getDate()} 日`;
}

function renderSessionHistory(sessions, currentId) {
  if (!els.sessionHistoryList) return;
  const list = Array.isArray(sessions) ? sessions : [];
  els.sessionHistoryList.innerHTML = '';

  // 条目顺序**完全沿用后端返回**（更新时间倒序）：本函数只按天插入分组标题，
  // 不做重排、不做过滤、不做排序。
  let lastKey = null;
  let host = null;
  list.forEach((s) => {
    const key = dayKey(s.updated_at);
    if (key !== lastKey) {
      lastKey = key;
      const group = el('div', 'sb-group');
      group.appendChild(el('div', 'sb-group-label', dayLabel(s.updated_at)));
      host = el('div', 'sb-group-items');
      group.appendChild(host);
      els.sessionHistoryList.appendChild(group);
    }

    const isCurrent = s.session_id === currentId;
    const item = el('button', 'session-item');
    item.type = 'button';
    if (isCurrent) item.classList.add('is-current');
    // 三个语义节点各自独立：问题摘要 / 轮次数 / 时间。
    // 会话编号属于排查信息，不写进悬停提示。
    item.appendChild(el('span', 'session-item-question',
      questionSummary(s.original_question)));
    item.appendChild(el('span', 'session-item-round',
      `共 ${s.current_round || 0} 轮`));
    item.appendChild(el('span', 'session-item-meta', relativeTime(s.updated_at)));
    if (isCurrent) item.appendChild(el('span', 'session-item-tag', '当前'));
    item.title = questionSummary(s.original_question, 160);
    item.addEventListener('click', () => void switchSession(s.session_id));
    host.appendChild(item);
  });

  if (els.sessionHistory) els.sessionHistory.hidden = list.length === 0;
}

async function refreshSessionHistory() {
  if (!els.sessionHistoryList) return;
  let list = [];
  try {
    list = await api('/api/sessions');
  } catch (err) {
    // 列表失败不影响主流程
    if (els.sessionHistory) els.sessionHistory.hidden = true;
    return;
  }
  renderSessionHistory(list, state.sessionId);
}

async function switchSession(sessionId) {
  if (!sessionId || sessionId === state.sessionId) return;
  closeEventStream();                       // 1. 先断开旧会话的连接
  // 旧流已不会再结束，pending 必须一并复位，否则发送按钮会一直禁用
  state.pending = false;
  updateSendState();
  await loadSession(sessionId);             // 2. 加载已有会话（含摘要 / 证据刷新）
  localStorage.setItem(STORAGE_KEY, sessionId); // 3. localStorage 指向新会话
  await refreshSessionHistory();            // 4. 刷新列表 → 当前项高亮
}

if (els.sessionHistoryRefresh) {
  els.sessionHistoryRefresh.addEventListener('click', () => {
    refreshSessionHistory();
  });
}
