<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";

type HostsSource = "dns" | "checktmdb";
const sourceNames: Record<HostsSource, string> = {
  dns: "本机 DNS 查询 + TLS 测速",
  checktmdb: "CheckTMDB 订阅（IPv4 + IPv6）",
};

interface Config {
  roots: { id: number; path: string }[];
  hosts_path: string;
  hosts_interval_seconds: number;
  hosts_source: HostsSource;
  hosts_subscription_urls: { A: string; AAAA: string };
  dns_servers: string[];
  dns_providers: string[];
  hosts_domains: string[];
  hosts_include_github: boolean;
  ip_family: string;
  log_path: string;
  log_retention_days: number;
  log_max_total_mb: number;
  log_max_file_mb: number;
}
interface FileEntry {
  name: string;
  path: string;
  directory: boolean;
  blocked: boolean;
  size: number;
}
interface FileRequest {
  action: "rename" | "move" | "delete";
  root: number;
  path: string;
  destination_root: number;
  destination: string;
  dry_run: boolean;
  confirm_delete?: string;
}
interface FilePlan {
  source: string;
  destination: string | null;
  size: number;
  modified_ns: number;
  permanent: boolean;
}
interface HostEntry { domain: string; ip: string; latency_ms: number | null; source: string; retained: boolean }
interface RunLog { time: string; level: string; domain: string; message: string }
interface HostReport {
  source: HostsSource;
  entries: HostEntry[];
  failures: string[];
  diagnostics: { domain: string; warnings: string[] }[];
  changed?: boolean;
  backup?: string | null;
  logs: RunLog[];
  outcome: "success" | "partial" | "failed";
  unresolved: { domain: string; record_type: string }[];
  can_apply: boolean;
  blocked_reason: string;
  exports: { ipv4: string; ipv6: string };
  operation_id?: string;
}
interface RuntimeLog {
  path: string;
  lines: string[];
  truncated: boolean;
  updated_at: string;
  error?: string | null;
}
class ApiError extends Error {
  constructor(message: string, public report?: HostReport) { super(message); }
}
interface Operation {
  id: string;
  kind: string;
  actor: string;
  status: string;
  request: unknown;
  result: unknown;
  error: string | null;
  created_at: string;
}
interface User {
  id: string;
  username: string;
  role: "admin" | "viewer";
  active: boolean;
}

const connected = ref(false);
const restoring = ref(true);
const username = ref("");
const password = ref("");
const currentUser = ref<User | null>(null);
const isAdmin = computed(() => currentUser.value?.role === "admin");
const config = ref<Config | null>(null);
const tab = ref<"dashboard" | "files" | "hosts" | "operations" | "logs" | "users" | "settings" | "downloads" | "metadata">("dashboard");
const root = ref(0);
const directory = ref("");
const entries = ref<FileEntry[]>([]);
const selected = ref<FileEntry | null>(null);
const action = ref<FileRequest["action"]>("rename");
const destinationRoot = ref(0);
const destination = ref("");
const deleteName = ref("");
const plan = ref<FilePlan | null>(null);
const plannedRequest = ref<FileRequest | null>(null);
const hostReport = ref<HostReport | null>(null);
const hostSource = ref<HostsSource>("dns");
const records = ref<Operation[]>([]);
const busy = ref(false);
const error = ref("");
const notice = ref("");
const noticeTone = ref<"success" | "warning">("success");
const users = ref<User[]>([]);
const newUser = ref({ username: "", password: "", role: "viewer" });
const userEdit = ref<User | null>(null);
const resetPassword = ref("");
const oldPassword = ref("");
const newPassword = ref("");
const runtimeLog = ref<RuntimeLog | null>(null);
const logError = ref("");
const logLoading = ref(false);
const followLog = ref(true);
const logLines = ref(200);
const logPanel = ref<HTMLElement | null>(null);
const hostRunning = ref(false);
const hostRunError = ref("");
const outcomeNames = { success: "成功", partial: "部分成功", failed: "失败" };
let logTimer: ReturnType<typeof setTimeout> | undefined;
let logRequest: AbortController | undefined;
const parent = computed(() => directory.value.split(/[\\/]/).slice(0, -1).join("\\"));

async function api<T>(path: string, body?: unknown, method?: string): Promise<T> {
  const response = await fetch(`/api${path}`, {
    method: method ?? (body === undefined ? "GET" : "POST"),
    headers: {
      "X-MediaForge-Request": "1",
      ...(body === undefined ? {} : { "Content-Type": "application/json" }),
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  if (!response.ok) {
    if (response.status === 401) { connected.value = false; currentUser.value = null; }
    const data = await response.json();
    throw new ApiError(
      typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail), data.report,
    );
  }
  return response.json() as Promise<T>;
}

async function run(work: () => Promise<void>) {
  if (busy.value) return;
  busy.value = true;
  error.value = "";
  notice.value = "";
  noticeTone.value = "success";
  try { await work(); }
  catch (cause) { error.value = cause instanceof Error ? cause.message : String(cause); }
  finally { busy.value = false; }
}

async function loadFiles() {
  clearPlan();
  selected.value = null;
  if (!config.value?.roots.length) { entries.value = []; return; }
  const data = await api<{ entries: FileEntry[] }>(
    `/files?root=${root.value}&path=${encodeURIComponent(directory.value)}`,
  );
  entries.value = data.entries;
}

function clearPlan() { plan.value = null; plannedRequest.value = null; deleteName.value = ""; }
function choose(file: FileEntry) {
  clearPlan();
  selected.value = file;
  destination.value = file.name;
}
async function enter(path: string) { directory.value = path; await loadFiles(); }

async function loadWorkspace() {
  config.value = await api<Config>("/config");
  hostSource.value = config.value.hosts_source;
  root.value = config.value.roots[0]?.id ?? 0;
  destinationRoot.value = root.value;
  directory.value = "";
  connected.value = true;
  records.value = await api<Operation[]>("/operations");
  await loadFiles();
}

async function login() {
  await run(async () => {
    try {
      currentUser.value = await api<User>("/auth/login", { username: username.value, password: password.value });
      tab.value = "dashboard";
      await loadWorkspace();
    } finally { password.value = ""; }
  });
}

async function logout() {
  await run(async () => {
    await api("/auth/logout", {});
    connected.value = false;
    currentUser.value = null;
    config.value = null;
    entries.value = []; records.value = []; users.value = [];
    runtimeLog.value = null;
    clearPlan(); selected.value = null; hostReport.value = null;
  });
}

onMounted(async () => {
  try {
    const response = await fetch("/api/auth/me");
    if (response.ok) {
      currentUser.value = await response.json() as User;
      await run(loadWorkspace);
    } else if (response.status !== 401) {
      error.value = "服务暂不可用，请检查 Python 服务与数据库。";
    }
  } catch { error.value = "无法连接本机服务。"; }
  finally { restoring.value = false; }
});

async function preview() {
  if (!selected.value) return;
  const request: FileRequest = {
    action: action.value, root: root.value, path: selected.value.path,
    destination_root: destinationRoot.value, destination: destination.value, dry_run: true,
  };
  await run(async () => {
    clearPlan();
    const result = await api<FilePlan>("/files/action", request);
    plannedRequest.value = { ...request };
    plan.value = result;
  });
}

async function execute() {
  if (!plannedRequest.value || !plan.value) return;
  const request = { ...plannedRequest.value, dry_run: false, confirm_delete: deleteName.value };
  const expected = { ...plan.value };
  await run(async () => {
    const current = await api<FilePlan>("/files/action", { ...request, dry_run: true });
    if (current.modified_ns !== expected.modified_ns || current.size !== expected.size) {
      clearPlan();
      throw new Error("文件已变化，请重新预览");
    }
    // Discard the plan before sending a mutation: network errors must not encourage replay.
    clearPlan();
    await api("/files/action", request);
    await loadFiles();
    notice.value = "操作已完成，并已记录到 PostgreSQL。";
  });
}

async function hosts(dryRun: boolean) {
  if (!dryRun && !window.confirm(`确认更新 ${config.value?.hosts_path}？`)) return;
  await run(async () => {
    hostRunning.value = true;
    hostRunError.value = "";
    hostReport.value = null;
    try {
      hostReport.value = await api<HostReport>("/hosts/update", {
        dry_run: dryRun, source: hostSource.value,
      });
      if (hostReport.value.outcome === "failed") throw new Error(hostReport.value.blocked_reason);
      noticeTone.value = hostReport.value.outcome === "partial" ? "warning" : "success";
      notice.value = `执行${outcomeNames[hostReport.value.outcome]}。` + (dryRun ? "未写入 hosts 文件。"
        : hostReport.value.changed ? "已更新 hosts，备份信息见下方。" : "映射未变化，无需写入。");
    } catch (cause) {
      if (cause instanceof ApiError && cause.report) hostReport.value = cause.report;
      hostRunError.value = cause instanceof Error ? cause.message : String(cause);
      throw cause;
    } finally { hostRunning.value = false; }
  });
}

function exportHosts(family: "ipv4" | "ipv6") {
  const content = hostReport.value?.exports[family];
  if (!content) return;
  const url = URL.createObjectURL(new Blob([content], { type: "text/plain;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = `MediaForge_hosts_${family}.txt`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function stopLogTracking() {
  if (logTimer !== undefined) clearTimeout(logTimer);
  logTimer = undefined;
  logRequest?.abort();
  logRequest = undefined;
  logLoading.value = false;
}

async function readLogs() {
  if (logLoading.value || !connected.value || tab.value !== "logs") return;
  if (logTimer !== undefined) clearTimeout(logTimer);
  logLoading.value = true;
  const controller = new AbortController();
  logRequest = controller;
  try {
    const response = await fetch(`/api/logs?limit=${logLines.value}`, { signal: controller.signal });
    if (response.status === 401) {
      connected.value = false;
      currentUser.value = null;
      throw new Error("登录已失效，请重新登录");
    }
    if (!response.ok) {
      const data = await response.json();
      throw new Error(typeof data.detail === "string" ? data.detail : "读取日志失败");
    }
    runtimeLog.value = await response.json() as RuntimeLog;
    logError.value = runtimeLog.value.error ?? "";
    await nextTick();
    if (followLog.value && logPanel.value) logPanel.value.scrollTop = logPanel.value.scrollHeight;
  } catch (cause) {
    if (!controller.signal.aborted) {
      logError.value = cause instanceof Error ? cause.message : String(cause);
      followLog.value = false;
    }
  } finally {
    if (logRequest === controller) {
      logRequest = undefined;
      logLoading.value = false;
      if (followLog.value && connected.value && tab.value === "logs") {
        logTimer = setTimeout(readLogs, 2000);
      }
    }
  }
}

watch([tab, connected, followLog, logLines], () => {
  stopLogTracking();
  if (connected.value && tab.value === "logs") void readLogs();
});
onUnmounted(stopLogTracking);

async function showRecords() {
  tab.value = "operations";
  await run(async () => { records.value = await api<Operation[]>("/operations"); });
}

async function showUsers() {
  tab.value = "users";
  await run(async () => { users.value = await api<User[]>("/users"); });
}
async function createUser() {
  await run(async () => {
    try {
      await api("/users", newUser.value);
      users.value = await api<User[]>("/users");
      newUser.value.username = "";
      notice.value = "用户已创建。";
    } finally { newUser.value.password = ""; }
  });
}
async function saveUser() {
  if (!userEdit.value) return;
  const editing = { ...userEdit.value };
  await run(async () => {
    try {
      await api(`/users/${editing.id}`, {
        role: editing.role, active: editing.active,
        ...(resetPassword.value ? { password: resetPassword.value } : {}),
      }, "PUT");
      userEdit.value = null;
      if (editing.id === currentUser.value?.id) {
        connected.value = false;
        currentUser.value = null;
        notice.value = "账号已更新，请重新登录。";
      } else {
        users.value = await api<User[]>("/users");
        notice.value = "用户已更新，该用户原有会话已注销。";
      }
    } finally { resetPassword.value = ""; }
  });
}
async function changePassword() {
  await run(async () => {
    try {
      await api("/auth/password", { old_password: oldPassword.value, new_password: newPassword.value });
      connected.value = false;
      currentUser.value = null;
      notice.value = "密码已修改，所有旧会话已注销，请重新登录。";
    } finally { oldPassword.value = ""; newPassword.value = ""; }
  });
}
</script>

<template>
  <div v-if="!connected" class="login-page">
    <div class="login-brand"><span class="eyebrow">YOUR LOCAL MEDIA WORKSHOP</span><h1>MediaForge</h1><p>影视工坊 · 让媒体管理井然有序</p></div>
    <form v-if="!restoring" class="panel login-card" @submit.prevent="login">
      <h2>登录管理后台</h2><p>使用管理员创建的账号登录工作台</p>
      <label>用户名<input v-model="username" autocomplete="username" required maxlength="64" :disabled="busy" /></label>
      <label>密码<input v-model="password" type="password" autocomplete="current-password" required maxlength="128" :disabled="busy" /></label>
      <div v-if="error" class="message error" role="alert">{{ error }}</div>
      <div v-if="notice" class="message" :class="noticeTone">{{ notice }}</div>
      <button class="primary" :disabled="busy">{{ busy ? "正在登录…" : "登录" }}</button>
      <small>首次使用：在终端运行 mediaforge create-admin --username admin 创建管理员。无默认账号密码。</small>
    </form>
    <p v-else>正在恢复登录状态…</p>
  </div>
  <div v-else class="admin-layout">
    <aside class="sidebar">
      <div class="brand">MediaForge<small>影视管理工作台</small></div>
      <nav>
        <button :class="{ active: tab === 'dashboard' }" @click="tab = 'dashboard'">概览仪表盘</button>
        <button :class="{ active: tab === 'files' }" @click="tab = 'files'">文件管理</button>
        <button :class="{ active: tab === 'hosts' }" @click="tab = 'hosts'">Hosts 管理</button>
        <button :class="{ active: tab === 'operations' }" :disabled="busy" @click="showRecords">操作日志</button>
        <button :class="{ active: tab === 'logs' }" @click="tab = 'logs'">运行日志</button>
        <button :class="{ active: tab === 'downloads' }" @click="tab = 'downloads'">影视下载 <small>规划中</small></button>
        <button :class="{ active: tab === 'metadata' }" @click="tab = 'metadata'">元数据刮削 <small>规划中</small></button>
        <button v-if="isAdmin" :class="{ active: tab === 'users' }" :disabled="busy" @click="showUsers">用户管理</button>
        <button :class="{ active: tab === 'settings' }" @click="tab = 'settings'">系统信息</button>
      </nav>
      <div class="sidebar-footer">本机服务 · PostgreSQL<br />{{ currentUser?.role === "admin" ? "管理员" : "只读用户" }}</div>
    </aside>
    <main class="shell">
    <header>
      <div><span class="eyebrow">MEDIAFORGE CONSOLE</span><h1>影视管理工作台</h1></div>
      <div class="toolbar"><span class="badge">{{ currentUser?.username }} · {{ isAdmin ? "管理员" : "只读" }}</span><button :disabled="busy" @click="logout">退出登录</button></div>
    </header>
    <div v-if="error" class="message error" role="alert">{{ error }}</div>
    <div v-if="notice" class="message" :class="noticeTone" role="status">{{ notice }}</div>
    <template v-if="connected && config">
      <section v-if="tab === 'dashboard'">
        <div class="welcome"><h2>欢迎回来，{{ currentUser?.username }}</h2><p>整理媒体资产，维护刮削网络，所有变更可追踪。</p></div>
        <div class="stats">
          <div class="panel"><span>媒体目录</span><strong>{{ config.roots.length }}</strong></div>
          <div class="panel"><span>最近操作（最多 100 条）</span><strong>{{ records.length }}</strong></div>
          <div class="panel"><span>最近异常 / 执行中</span><strong>{{ records.filter(item => item.status !== "success").length }}</strong></div>
          <div class="panel"><span>Hosts 自动更新</span><strong>{{ config.hosts_interval_seconds ? "已开启" : "手动" }}</strong></div>
        </div>
        <section class="panel"><h2>工作模块</h2><div class="module-grid">
          <button @click="tab = 'files'"><b>文件管理</b><span>浏览、重命名、移动、永久删除</span></button>
          <button @click="tab = 'hosts'"><b>Hosts 管理</b><span>DNS 测速、CheckTMDB 订阅、托管区更新</span></button>
          <button @click="tab = 'downloads'"><b>影视下载</b><span>模块边界预留，尚未接入下载器</span></button>
          <button @click="tab = 'metadata'"><b>元数据刮削</b><span>模块边界预留，尚未接入刮削器</span></button>
        </div></section>
        <div class="message">文件修改需管理员权限；永久删除需完整文件名确认。Hosts 默认写入预览文件，不自动触碰系统配置。</div>
      </section>
      <section v-if="tab === 'files'" class="panel">
        <h2>文件管理</h2>
        <p>仅操作配置的媒体目录。支持普通文件，不覆盖目标、不递归删除目录。</p>
        <div v-if="!config.roots.length" class="message">
          尚未配置媒体目录。请在 .env 设置 MEDIAFORGE_MEDIA_ROOTS 后重启，例如 ["D:\\Media"]。
        </div>
        <template v-else>
          <div class="toolbar">
            <select v-model.number="root" :disabled="busy" @change="run(() => enter(''))">
              <option v-for="item in config.roots" :key="item.id" :value="item.id">{{ item.path }}</option>
            </select>
            <button :disabled="busy || !directory" @click="run(() => enter(parent))">上一级</button>
            <button :disabled="busy" @click="run(loadFiles)">刷新</button>
            <code>{{ directory || "根目录" }}</code>
          </div>
          <div class="table-wrap">
            <table><thead><tr><th>名称</th><th>类型</th><th>大小</th></tr></thead>
              <tbody><tr v-for="file in entries" :key="file.path" :class="{ selected: selected?.path === file.path }">
                <td><button class="link" :disabled="busy || file.blocked" @click="file.directory ? run(() => enter(file.path)) : choose(file)">{{ file.name }}</button></td>
                <td>{{ file.blocked ? "链接（禁止操作）" : file.directory ? "目录" : "文件" }}</td>
                <td>{{ file.directory ? "—" : (file.size / 1024 / 1024).toFixed(2) + " MB" }}</td>
              </tr><tr v-if="!entries.length"><td colspan="3">目录为空</td></tr></tbody>
            </table>
          </div>
          <div v-if="selected && isAdmin" class="action-box">
            <h3>已选择：{{ selected.name }}</h3>
            <div class="toolbar">
              <select v-model="action" :disabled="busy" @change="clearPlan">
                <option value="rename">重命名</option><option value="move">移动</option><option value="delete">永久删除</option>
              </select>
              <select v-if="action === 'move'" v-model.number="destinationRoot" :disabled="busy" @change="clearPlan">
                <option v-for="item in config.roots" :key="item.id" :value="item.id">{{ item.path }}</option>
              </select>
              <input v-if="action !== 'delete'" v-model="destination" :disabled="busy" :placeholder="action === 'rename' ? '新文件名（含扩展名）' : '目标相对路径（含文件名，父目录需存在）'" @input="clearPlan" />
              <button :disabled="busy" @click="preview">预览操作</button>
            </div>
            <p v-if="!isAdmin">当前为只读账号，可浏览目录，不能执行文件修改。</p>
            <div v-if="plan" class="preview">
              <p>来源：<code>{{ plan.source }}</code></p>
              <p v-if="plan.destination">目标：<code>{{ plan.destination }}</code></p>
              <template v-if="plan.permanent">
                <p class="danger">永久删除，不进入回收站！请输入完整文件名确认。</p>
                <input v-model="deleteName" :disabled="busy" :placeholder="selected.name" />
              </template>
              <button :class="{ destructive: plan.permanent }" :disabled="busy || (plan.permanent && deleteName !== selected.name)" @click="execute">确认执行</button>
            </div>
          </div>
        </template>
      </section>
      <section v-if="tab === 'hosts'" class="panel">
        <h2>Hosts 数据获取</h2>
        <div class="toolbar">
          <label for="hosts-source">本次获取方式</label>
          <select id="hosts-source" v-model="hostSource" :disabled="busy">
            <option value="dns">{{ sourceNames.dns }}</option>
            <option value="checktmdb">{{ sourceNames.checktmdb }}</option>
          </select>
          <small>仅影响本次手动执行，不修改定时任务配置。</small>
        </div>
        <p v-if="hostSource === 'dns'">优先使用 HTTPS DNS，查询失败或地址不可用时切换来源。使用本机 TCP + TLS 握手耗时选优，并校验证书与域名。</p>
        <template v-else>
          <p>同时下载两个订阅文件，校验 HTTPS 证书、公开 IP、地址族和允许的域名，不再做本机 TLS 测速。订阅映射在当前网络是否可用尚未验证。</p>
          <p><a :href="config.hosts_subscription_urls.A" target="_blank" rel="noopener noreferrer">IPv4 订阅文件</a> · <a :href="config.hosts_subscription_urls.AAAA" target="_blank" rel="noopener noreferrer">IPv6 订阅文件</a></p>
          <p>上游未包含的配置域名（包括 GitHub）仅保留已有托管映射，不额外发起 DNS 查询。任一订阅失败时保留该地址族旧映射，两个都失败则不写入。</p>
        </template>
        <dl><dt>目标文件</dt><dd><code>{{ config.hosts_path }}</code></dd>
          <template v-if="hostSource === 'dns'">
            <dt>查询源顺序</dt><dd>{{ config.dns_providers.join(" → ") }}</dd>
            <dt>普通 DNS / 地址族</dt><dd>{{ config.dns_servers.join(", ") }} / {{ config.ip_family }}</dd>
          </template>
          <dt>域名范围</dt><dd>{{ config.hosts_domains.length }} 个；GitHub {{ config.hosts_include_github ? "已开启" : "已关闭" }}</dd>
          <dt>自动获取方式</dt><dd>{{ sourceNames[config.hosts_source] }}</dd>
          <dt>定时更新</dt><dd>{{ config.hosts_interval_seconds ? config.hosts_interval_seconds + " 秒一次" : "已关闭（修改 .env 后重启可开启）" }}</dd></dl>
        <details><summary>如何开启自动订阅</summary><p>在 .env 设置以下两项后重启服务。定时执行会更新上述目标文件，首次建议保持预览文件路径。</p><pre>MEDIAFORGE_HOSTS_SOURCE=checktmdb
MEDIAFORGE_HOSTS_INTERVAL_SECONDS=3600</pre></details>
        <div class="toolbar">
          <button :disabled="busy" @click="hosts(true)">获取并预览（不写入）</button>
          <button v-if="isAdmin" :disabled="busy" @click="hosts(false)">重新获取并更新文件</button>
          <button @click="tab = 'logs'">追踪实时运行日志</button>
        </div>
        <p>首次建议保留 hosts.preview 目标。系统 hosts 需显式开启权限并以管理员身份运行；只替换 MediaForge 托管区，其他内容保持不变。</p>
        <p v-if="hostRunning" role="status">正在执行{{ sourceNames[hostSource] }}… 可切换“运行日志”查看最新进展。</p>
        <p v-if="hostRunError" class="message error" role="alert">执行失败：{{ hostRunError }}</p>
        <template v-if="hostReport">
          <h3>执行结果：<span :class="{ danger: hostReport.outcome === 'failed', warning: hostReport.outcome === 'partial' }">{{ outcomeNames[hostReport.outcome] }}</span></h3>
          <p>本次来源：{{ sourceNames[hostReport.source] }}</p>
          <p v-if="hostReport.unresolved.length" class="warning">未获取可用新映射：{{ hostReport.unresolved.map(item => `${item.domain} (${item.record_type})`).join("、") }}。仅保留已有托管映射，不代表旧 IP 仍可用。</p>
          <p v-if="!hostReport.can_apply" class="danger">{{ hostReport.blocked_reason }}</p>
          <p v-if="hostReport.backup">备份：<code>{{ hostReport.backup }}</code></p>
          <div class="table-wrap"><table><thead><tr><th>域名</th><th>IP</th><th>来源 / 状态</th><th>TLS 延迟</th></tr></thead>
            <tbody><tr v-for="entry in hostReport.entries" :key="entry.domain + entry.ip">
              <td>{{ entry.domain }}</td><td><code>{{ entry.ip }}</code></td><td :class="{ warning: entry.retained }">{{ entry.retained ? "保留旧映射（未验证）" : entry.source === "checktmdb" ? "CheckTMDB 订阅（未做本机测速）" : entry.source }}</td><td>{{ entry.latency_ms === null ? "—" : entry.latency_ms + " ms" }}</td>
            </tr></tbody></table></div>
          <div class="toolbar">
            <button :disabled="!hostReport.exports.ipv4" @click="exportHosts('ipv4')">导出本轮 IPv4</button>
            <button :disabled="!hostReport.exports.ipv6" @click="exportHosts('ipv6')">导出本轮 IPv6</button>
            <small>仅导出本轮获取的数据，不包含保留的旧映射；订阅数据未在本机测速。</small>
          </div>
          <h3>本次执行日志</h3>
          <div class="run-log" role="log"><div v-for="(line, index) in hostReport.logs" :key="index" :class="'log-' + line.level"><time>{{ new Date(line.time).toLocaleTimeString() }}</time> [{{ line.level }}] {{ line.domain ? `[${line.domain}] ` : "" }}{{ line.message }}</div></div>
          <details><summary>获取 / 测速诊断</summary><pre>{{ JSON.stringify(hostReport.diagnostics, null, 2) }}</pre></details>
        </template>
      </section>
      <section v-if="tab === 'logs'" class="panel">
        <h2>运行日志</h2>
        <p>路径：<code>{{ config.log_path }}</code></p>
        <p>保留 {{ config.log_retention_days }} 天 · 总量上限 {{ config.log_max_total_mb }} MiB · 单文件 {{ config.log_max_file_mb }} MiB 或跨日轮转。路径和保留策略在 .env 修改后重启生效。</p>
        <div class="toolbar">
          <button :disabled="logLoading" @click="readLogs">读取最新日志</button>
          <label><input v-model="followLog" type="checkbox" /> 自动追踪（每 2 秒刷新并滚动到底部）</label>
          <select v-model.number="logLines"><option :value="100">最近 100 行</option><option :value="200">最近 200 行</option><option :value="1000">最近 1000 行</option></select>
        </div>
        <p v-if="logError" class="message error" role="alert">{{ logError }}</p>
        <p v-if="runtimeLog"><small>文件更新时间：{{ new Date(runtimeLog.updated_at).toLocaleString() }}{{ runtimeLog.truncated ? " · 仅展示最新片段" : "" }}</small></p>
        <pre ref="logPanel" class="runtime-log" role="log" aria-label="最新运行日志">{{ runtimeLog?.lines.join("\n") || (logLoading ? "正在读取日志…" : "暂无日志") }}</pre>
        <p>读取的是当前活动日志文件；旧文件保留在同一目录。离开页面会停止追踪，不影响后台任务。</p>
      </section>
      <section v-if="tab === 'operations'" class="panel">
        <h2>最近 100 条操作</h2>
        <p>running 表示尚在执行或曾意外中断。数据库与文件系统不能组成单个事务，遇到中断请先核对磁盘，切勿盲目重复操作。</p>
        <button :disabled="busy" @click="showRecords">刷新记录</button>
        <div class="table-wrap"><table><thead><tr><th>时间</th><th>操作人 / 操作</th><th>状态</th><th>详情</th></tr></thead>
          <tbody><tr v-for="record in records" :key="record.id">
            <td>{{ new Date(record.created_at).toLocaleString() }}</td><td>{{ record.actor }} / {{ record.kind }}</td>
            <td :class="{ danger: record.status !== 'success' }">{{ record.status }}</td>
            <td><details><summary>{{ record.error || record.id }}</summary><pre>{{ JSON.stringify({ request: record.request, result: record.result }, null, 2) }}</pre></details></td>
          </tr><tr v-if="!records.length"><td colspan="4">暂无操作记录</td></tr></tbody>
        </table></div>
      </section>
      <section v-if="tab === 'users' && isAdmin" class="panel">
        <h2>用户管理</h2><p>管理员可修改文件、更新 hosts 和管理用户；只读用户仅可浏览、查询和查看日志。</p>
        <form class="toolbar" @submit.prevent="createUser">
          <input v-model="newUser.username" placeholder="用户名（3–64 位英文、数字、_.-）" required maxlength="64" :disabled="busy" />
          <input v-model="newUser.password" type="password" autocomplete="new-password" placeholder="初始密码（4–128 位）" required minlength="4" maxlength="128" :disabled="busy" />
          <select v-model="newUser.role" :disabled="busy"><option value="viewer">只读用户</option><option value="admin">管理员</option></select>
          <button :disabled="busy">创建用户</button>
        </form>
        <div class="table-wrap"><table><thead><tr><th>用户名</th><th>角色</th><th>状态</th><th>操作</th></tr></thead>
          <tbody><tr v-for="item in users" :key="item.id"><td>{{ item.username }}</td><td>{{ item.role === "admin" ? "管理员" : "只读" }}</td><td>{{ item.active ? "启用" : "停用" }}</td>
            <td><button :disabled="busy" @click="userEdit = { ...item }; resetPassword = ''">编辑</button></td></tr></tbody>
        </table></div>
        <form v-if="userEdit" class="action-box" @submit.prevent="saveUser">
          <h3>编辑 {{ userEdit.username }}</h3>
          <div class="toolbar">
            <select v-model="userEdit.role" :disabled="busy"><option value="viewer">只读用户</option><option value="admin">管理员</option></select>
            <label><input v-model="userEdit.active" type="checkbox" :disabled="busy" /> 启用</label>
            <input v-model="resetPassword" type="password" autocomplete="new-password" placeholder="新密码（4–128 位，留空不变）" minlength="4" maxlength="128" :disabled="busy" />
            <button :disabled="busy">保存并注销该用户旧会话</button><button type="button" :disabled="busy" @click="userEdit = null">取消</button>
          </div>
        </form>
      </section>
      <section v-if="tab === 'settings'" class="panel">
        <h2>系统信息</h2><p>首版配置由 .env 管理，修改后重启 Python 服务生效。此页面不会暴露数据库密码。</p>
        <dl><dt>后端 / 存储</dt><dd>Python FastAPI / PostgreSQL，独立 mediaforge schema</dd>
          <dt>前端</dt><dd>Vue 3 构建产物，由 Python 提供；运行时无需 Node</dd>
          <dt>媒体目录</dt><dd>{{ config.roots.map(item => item.path).join("、") || "未配置" }}</dd>
          <dt>Hosts 目标</dt><dd>{{ config.hosts_path }}</dd>
          <dt>Hosts 默认来源</dt><dd>{{ sourceNames[config.hosts_source] }}</dd>
          <dt>DNS 查询源</dt><dd>{{ config.dns_providers.join(" → ") }}</dd>
          <dt>日志文件</dt><dd>{{ config.log_path }}</dd>
          <dt>日志保留</dt><dd>{{ config.log_retention_days }} 天 / 总量 {{ config.log_max_total_mb }} MiB / 单文件 {{ config.log_max_file_mb }} MiB</dd></dl>
        <h3>修改我的密码</h3>
        <form class="toolbar" @submit.prevent="changePassword">
          <input v-model="oldPassword" type="password" autocomplete="current-password" placeholder="原密码" required maxlength="128" :disabled="busy" />
          <input v-model="newPassword" type="password" autocomplete="new-password" placeholder="新密码（4–128 位）" required minlength="4" maxlength="128" :disabled="busy" />
          <button :disabled="busy">修改密码并退出所有会话</button>
        </form>
      </section>
      <section v-if="tab === 'downloads' || tab === 'metadata'" class="panel">
        <h2>{{ tab === "downloads" ? "影视下载" : "元数据刮削" }}</h2>
        <span class="badge">规划中 · 尚未实现</span>
        <p>{{ tab === "downloads" ? "后续可接入 qBittorrent / Transmission，统一管理电影、电视剧下载及入库。" : "后续可接入 TMDB / TVDB，生成 Emby 所需的 NFO、海报和背景图。" }}</p>
      </section>
    </template>
    <footer>{{ busy ? "正在处理，请勿关闭页面或重复提交…" : "MediaForge · 下载 / 刮削接口预留，先让文件与网络井然有序。" }}</footer>
    </main>
  </div>
</template>
