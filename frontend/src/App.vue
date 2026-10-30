<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";

type HostsSource = "dns" | "checktmdb";
interface BackgroundTask {
  id: string; kind: string; actor: string; status: string;
  completed: number; total: number; current: string; cancel_requested: boolean;
  error: string | null; created_at: string; finished_at: string | null;
  request?: Record<string, unknown>; result?: Record<string, unknown> | null;
}
interface EmbyConfiguration { server_url: string; configured: boolean; auto_refresh: boolean }
const taskRecords = ref<BackgroundTask[]>([]);
const taskDetail = ref<BackgroundTask | null>(null);
const taskDetailId = ref("");
const taskError = ref("");
const taskLoading = ref(false);
const taskCancelling = ref("");
const taskNames: Record<string, string> = { "file.migrate": "文件迁移", "video.info.scan": "视频扫描", "metadata.apply": "影视刮削", "rename.apply": "批量重命名" };
const taskStatuses: Record<string, string> = { queued: "排队中", running: "执行中", success: "成功", partial: "部分成功", failed: "失败", cancelled: "已取消", interrupted: "重启中断 / 待核对" };
let taskGeneration = 0;
let taskTimer: ReturnType<typeof setTimeout> | undefined;
const embySaved = ref<EmbyConfiguration>({ server_url: "", configured: false, auto_refresh: false });
const embyForm = ref({ server_url: "", api_token: "", auto_refresh: false });
const embyLibraries = ref<{ id: string; name: string }[]>([]);
const embyLibrary = ref("");
const embyDirty = computed(() => embyForm.value.server_url.trim().replace(/\/+$/, "") !== embySaved.value.server_url || !!embyForm.value.api_token || embyForm.value.auto_refresh !== embySaved.value.auto_refresh);
const sourceNames: Record<HostsSource, string> = {
  dns: "本机 DNS 查询 + TLS 测速",
  checktmdb: "CheckTMDB 订阅（IPv4 + IPv6）",
};

interface Config {
  roots: { id: number; path: string }[];
  roots_version: string;
  tmdb_key_configured: boolean;
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
interface MediaFolderListing {
  path: string | null;
  parent: string | null;
  selectable: boolean;
  reason: string;
  entries: { name: string; path: string; blocked: boolean }[];
}
interface FileEntry {
  name: string;
  path: string;
  directory: boolean;
  blocked: boolean;
  size: number;
}
interface VideoTrack {
  codec: string; profile: string | null; level: number | null;
  width: number; height: number; fps: number | null; bit_rate: number | null;
  pixel_format: string | null; bit_depth: number | null; hdr: string;
  color_transfer: string | null; color_primaries: string | null;
}
interface VideoDetails {
  container: string | null; size_bytes: number; duration_seconds: number | null;
  bit_rate: number | null; estimated_bit_rate: number | null; videos: VideoTrack[];
  audios: { codec: string | null; profile: string | null; channels: number | null;
    layout: string | null; sample_rate: number | null; bit_rate: number | null;
    language: string | null }[];
  subtitles: { codec: string | null; language: string | null }[];
  risks: string[];
}
interface VideoInfoRow {
  name: string; path: string; details: VideoDetails | null; error: string | null;
  scanned_at: string | null; status: string;
}
interface VideoInfoStatus {
  available: boolean; running: boolean; current: string; finished_at: string | null;
  error: string; interval_minutes: number; recursive: boolean; extensions: string[];
  summary: { analyzed?: number; cached?: number; deferred?: number; failed?: number };
}
interface FileRequest {
  action: "rename" | "move";
  root: number;
  path: string;
  destination_root: number;
  destination: string;
  dry_run: boolean;
  roots_version?: string;
  preview_id?: string;
}
interface FilePlan {
  preview_id: string;
  source: string;
  destination: string | null;
  size: number;
  modified_ns: number;
  permanent: boolean;
}
interface DeleteFilesPreview {
  preview_id: string;
  files: { source: string; size: number }[];
}
interface DeleteFilesResult {
  deleted_count: number;
  outcome: "success" | "partial" | "failed";
  files: { path: string; status: "success" | "failed"; error: string | null }[];
}
interface MigrationRequest {
  source_folder: string;
  paths: string[];
  target_root: number;
  roots_version: string;
  preview_id?: string;
}
interface MigrationRow {
  path: string; source: string; destination: string;
  status: "ready" | "success" | "skipped" | "failed"; error: string | null;
}
interface MigrationPreview {
  preview_id: string; files: MigrationRow[]; ready_count: number; skipped_count: number;
}
interface MigrationResult {
  files: MigrationRow[]; moved_count: number; skipped_count: number; failed_count: number;
  outcome: "success" | "partial" | "failed";
}
interface RenameRule {
  id: string; name: string; pattern: string; template: string;
  enabled: boolean; priority: number;
}
interface RenameRow {
  source: string; name: string; new_name: string; draft_name?: string;
  status: string; reason: string; editable: boolean; rule?: string;
  episode?: number; notes?: string[];
  files: { source: string; target: string }[]; rollback_errors?: string[];
}
interface RenameReport {
  preview_id?: string; rows: RenameRow[]; ready_count: number; file_count: number;
  unchanged: boolean; reason?: string; checked_at?: string;
}
interface RenameResult {
  rows: RenameRow[]; renamed_count: number; outcome: "success" | "partial" | "failed";
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
interface OperationSummary {
  id: string;
  kind: string;
  actor: string;
  status: string;
  error: string | null;
  created_at: string;
}
interface Operation extends OperationSummary {
  request: unknown;
  result: unknown;
}
interface User {
  id: string;
  username: string;
  role: "admin" | "viewer";
  active: boolean;
}
interface MetadataRow {
  folder: string;
  folder_name?: string;
  match_source?: "manual" | "automatic";
  year?: string;
  manual_id?: number | string;
  query?: string;
  status: "matched" | "skipped" | "failed";
  reason?: string;
  title?: string;
  tmdb_id?: number;
  files?: string[];
  warnings?: string[];
}
interface MetadataMapping {
  id: string;
  media_type: "tv" | "movie";
  root: number | null;
  root_path: string;
  folder_path: string;
  folder_name: string;
  tmdb_id: number;
}
interface MetadataCleanupReport {
  dry_run: boolean;
  outcome: "success" | "partial";
  removed_count: number;
  invalid_count: number;
  rows: (MetadataMapping & { status: string; reason: string; removed: boolean })[];
}
interface MetadataCandidate {
  id: number;
  scraper_id: string;
  name?: string;
  title?: string;
  original_name?: string;
  original_title?: string;
  first_air_date?: string;
  release_date?: string;
  overview?: string;
}
interface MetadataSearchReport {
  query: string;
  media_type: "tv" | "movie";
  page: number;
  total_pages: number;
  total_results: number;
  results: MetadataCandidate[];
  warnings: string[];
}
interface ScraperConfig {
  id: string;
  name: string;
  enabled: boolean;
  priority: number;
  configured: boolean;
  key_source: "database" | "environment" | "none";
  api_key: string;
}
interface MetadataPlan {
  preview_id: string;
  rows: MetadataRow[];
  file_count: number;
  overwrite: boolean;
  outcome: "success" | "partial" | "failed";
}
interface MetadataResult {
  outcome: "success" | "partial" | "failed";
  rows: MetadataRow[];
  files: { path: string; status: string; backup?: string; error?: string }[];
}

const connected = ref(false);
const restoring = ref(true);
const username = ref("");
const password = ref("");
const currentUser = ref<User | null>(null);
const isAdmin = computed(() => currentUser.value?.role === "admin");
const config = ref<Config | null>(null);
const mediaRootsDraft = computed(() => config.value?.roots.map(item => item.path) ?? []);
const selectedRootPaths = ref<string[]>([]);
const mediaFolderListing = ref<MediaFolderListing | null>(null);
const mediaFolderError = ref("");
const mediaFolderDialog = ref<HTMLDialogElement | null>(null);
const folderPickerPurpose = ref<"root" | "migration">("root");
const mediaFolderAlreadyAdded = computed(() => mediaFolderListing.value?.path
  ? mediaRootsDraft.value.some(path => path === mediaFolderListing.value?.path) : false);
const workspaceTabs = ["dashboard", "configuration", "files", "migration", "tasks", "renaming", "hosts", "operations", "logs", "users", "settings", "downloads", "metadata", "video-info"] as const;
const tab = ref<typeof workspaceTabs[number]>("dashboard");
const configurationSections = [
  { id: "roots", name: "媒体目录" },
  { id: "renaming", name: "命名规则" },
  { id: "scrapers", name: "刮削器" },
  { id: "mappings", name: "影视匹配" },
  { id: "emby", name: "Emby 联动" },
  { id: "downloaders", name: "下载器（预留）" },
  { id: "system", name: "系统配置" },
] as const;
const configurationSection = ref<typeof configurationSections[number]["id"]>("roots");
const pageInfo = {
  dashboard: { title: "工作台", group: "概览", description: "媒体库状态与常用工具，一目了然。" },
  files: { title: "文件浏览", group: "媒体工具", description: "浏览媒体目录，整理文件与存放位置。" },
  migration: { title: "文件迁移", group: "媒体工具", description: "将下载目录中的文件移入媒体库，保留文件名，不覆盖已有文件。" },
  renaming: { title: "批量重命名", group: "媒体工具", description: "按规则统一命名，核对预览后再执行。" },
  metadata: { title: "影视刮削", group: "媒体工具", description: "匹配影视资料，为媒体补全 NFO 与海报。" },
  "video-info": { title: "视频分析", group: "媒体工具", description: "从编码、码率到音轨，了解视频与播放风险。" },
  configuration: { title: "配置管理", group: "管理", description: "集中管理媒体目录、命名规则与刮削配置。" },
  hosts: { title: "Hosts 更新", group: "系统工具", description: "检测域名连通性，预览并更新本机 Hosts。" },
  operations: { title: "操作记录", group: "系统工具", description: "追踪操作结果，查看执行详情与异常。" },
  tasks: { title: "任务中心", group: "系统工具", description: "跟踪后台进度、取消待执行工作，查看逐文件结果。" },
  logs: { title: "运行日志", group: "系统工具", description: "查看服务运行输出，定位任务与系统问题。" },
  users: { title: "用户管理", group: "系统工具", description: "管理登录用户及其访问权限。" },
  settings: { title: "系统与账号", group: "系统工具", description: "查看系统信息，管理当前账号。" },
  downloads: { title: "影视下载", group: "媒体工具", description: "预留：按网站匹配下载器，解析集数并选择下载到媒体目录。" },
} satisfies Record<typeof workspaceTabs[number], { title: string; group: string; description: string }>;
const currentPage = computed(() => pageInfo[tab.value]);

function syncTabFromHash() {
  const [page, section] = window.location.hash.slice(1).split("/");
  const target = workspaceTabs.find(item => item === page);
  if (target === "configuration") {
    configurationSection.value = configurationSections.find(item => item.id === section)?.id ?? "roots";
  }
  tab.value = target ?? "dashboard";
}

watch([tab, configurationSection], ([value, section], [previous]) => {
  const hash = value === "configuration" ? `#configuration/${section}` : `#${value}`;
  if (window.location.hash !== hash) window.history.pushState(null, "", hash);
  if (value === "metadata" && previous !== "metadata") {
    metadataRoot.value = -1; metadataDirectory.value = "";
  }
});
function openConfiguration(section: typeof configurationSection.value = "roots") {
  configurationSection.value = section;
  tab.value = "configuration";
}
function folderName(path: string) {
  return path.replace(/[\\/]+$/, "").split(/[\\/]/).pop() || path;
}
function rootLabel(path: string) {
  const name = folderName(path);
  const duplicates = config.value?.roots.filter(item => folderName(item.path) === name).length ?? 0;
  return duplicates > 1 ? `${name} — ${path}` : name;
}
function rootPath(id: number) {
  return config.value?.roots.find(item => item.id === id)?.path ?? "";
}
const root = ref(0);
const directory = ref("");
const entries = ref<FileEntry[]>([]);
const selected = ref<FileEntry | null>(null);
const action = ref<FileRequest["action"]>("rename");
const destinationRoot = ref(0);
const destination = ref("");
const selectedFilePaths = ref<string[]>([]);
const deletableFiles = computed(() => entries.value.filter(file => !file.directory && !file.blocked));
const fileDeleteDialog = ref<HTMLDialogElement | null>(null);
const fileDeletePreview = ref<DeleteFilesPreview | null>(null);
const fileDeleteRequest = ref<{ root: number; paths: string[]; roots_version: string; preview_id: string } | null>(null);
const fileDeleteError = ref("");
const fileDeleteResult = ref<DeleteFilesResult | null>(null);
const migrationSource = ref("");
const migrationTarget = ref(-1);
const migrationEntries = ref<FileEntry[]>([]);
const migrationSelected = ref<string[]>([]);
const migrationSelectable = computed(() => migrationEntries.value.filter(file => !file.directory && !file.blocked));
const migrationDialog = ref<HTMLDialogElement | null>(null);
const migrationPreview = ref<MigrationPreview | null>(null);
const migrationRequest = ref<MigrationRequest | null>(null);
const migrationError = ref("");
const migrationResult = ref<MigrationResult | null>(null);
const plan = ref<FilePlan | null>(null);
const plannedRequest = ref<FileRequest | null>(null);
const hostReport = ref<HostReport | null>(null);
const hostSource = ref<HostsSource>("dns");
const records = ref<OperationSummary[]>([]);
const recordsVersion = ref(0);
const operationDetails = ref<Record<string, { loading: boolean; error: string; data?: Operation }>>({});
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
const metadataRoot = ref(-1);
const metadataDirectory = ref("");
const metadataType = ref<"tv" | "movie">("tv");
const metadataLanguage = ref("zh-CN");
const scrapers = ref<ScraperConfig[]>([]);
const savedScrapers = ref<ScraperConfig[]>([]);
const scraperSettingsPanel = ref<HTMLElement | null>(null);
const scraperOrder = computed(() => [...scrapers.value].sort(
  (left, right) => left.priority - right.priority || left.id.localeCompare(right.id),
));
const executionScrapers = computed(() => [...savedScrapers.value].filter(item => item.enabled).sort(
  (left, right) => left.priority - right.priority || left.id.localeCompare(right.id),
));
const selectedScrapers = computed(() => executionScrapers.value.map(item => item.id));
const metadataOverwrite = ref(false);
const metadataPlan = ref<MetadataPlan | null>(null);
const metadataResult = ref<MetadataResult | null>(null);
const metadataMappings = ref<MetadataMapping[]>([]);
const selectedMappingIds = ref<string[]>([]);
const metadataTarget = ref<{ name: string; path: string } | null>(null);
const metadataMappingId = ref<number | string>("");
const metadataMappingPanel = ref<HTMLDetailsElement | null>(null);
const metadataSearchName = ref("");
const metadataSearchIsFolder = ref(false);
const metadataSearchReport = ref<MetadataSearchReport | null>(null);
const metadataSearchPanel = ref<HTMLElement | null>(null);
const metadataCleanupReport = ref<MetadataCleanupReport | null>(null);
const renameRules = ref<RenameRule[]>([]);
const savedRenameRules = ref("");
const selectedRuleIds = ref<string[]>([]);
const newRuleDialog = ref<HTMLDialogElement | null>(null);
const newRule = ref<RenameRule>({ id: "", name: "", pattern: "", template: "{episode:02d}", enabled: true, priority: 0 });
const renameRulesDirty = computed(() => JSON.stringify(renameRules.value) !== savedRenameRules.value);
const renameRoot = ref(-1);
const renameDirectory = ref("");
const renameForce = ref(false);
const renameReport = ref<RenameReport | null>(null);
const renameResult = ref<RenameResult | null>(null);
const videoInfoRoot = ref(-1);
const videoInfoRows = ref<VideoInfoRow[]>([]);
const videoInfoStatus = ref<VideoInfoStatus | null>(null);
const videoInfoLoading = ref(false);
const videoInfoError = ref("");
const videoStatusError = ref("");
const videoSort = ref<"bitrate" | "name">("bitrate");
const videoComparisons = ref<{ key: string; label: string; details: VideoDetails }[]>([]);
const videoDetails = ref<VideoInfoRow | null>(null);
const videoDetailsDialog = ref<HTMLDialogElement | null>(null);
async function showVideoDetails(row: VideoInfoRow) {
  videoDetails.value = row;
  await nextTick();
  videoDetailsDialog.value?.showModal();
}
const sortedVideoRows = computed(() => [...videoInfoRows.value].sort((left, right) =>
  videoSort.value === "name" ? left.name.localeCompare(right.name)
    : (right.details?.bit_rate ?? right.details?.estimated_bit_rate ?? -1)
      - (left.details?.bit_rate ?? left.details?.estimated_bit_rate ?? -1),
));
const videoStateNames: Record<string, string> = {
  missing: "待识别", changed: "文件已变化，历史结果待更新", saved: "已入库",
  failed: "识别失败", blocked: "链接禁止读取",
};
let videoInfoTimer: ReturnType<typeof setTimeout> | undefined;
let videoPollingEnabled = false;
let videoStatusPolling = false;
const renameEditsPending = computed(() => renameReport.value?.rows.some(
  row => (row.draft_name ?? row.new_name) !== row.new_name,
));
const renameStatus: Record<string, string> = {
  ready: "待重命名", unchanged: "无需修改", skipped: "跳过", conflict: "冲突",
  failed: "失败", renamed: "已重命名",
};
const currentMetadataMappings = computed(() =>
  metadataMappings.value.filter(item => item.media_type === metadataType.value),
);
const metadataMappingEdited = computed(() => metadataPlan.value?.rows.some(
  row => row.folder_name && row.manual_id !== (row.tmdb_id ?? ""),
));
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

function taskActive(task: BackgroundTask) { return task.status === "queued" || task.status === "running"; }
async function loadTaskDetail(id: string) {
  taskDetailId.value = id;
  const generation = taskGeneration;
  try {
    const data = await api<BackgroundTask>(`/tasks/${encodeURIComponent(id)}`);
    if (connected.value && generation === taskGeneration && taskDetailId.value === id) taskDetail.value = data;
  } catch (cause) {
    if (connected.value && generation === taskGeneration) taskError.value = String(cause);
  }
}
async function loadTasks() {
  if (taskLoading.value) return;
  const generation = taskGeneration;
  taskLoading.value = true;
  try {
    const data = await api<BackgroundTask[]>("/tasks");
    if (!connected.value || generation !== taskGeneration) return;
    taskRecords.value = data; taskError.value = "";
    if (taskDetailId.value && (!taskDetail.value || taskActive(taskDetail.value))) await loadTaskDetail(taskDetailId.value);
  } catch (cause) {
    if (connected.value && generation === taskGeneration) taskError.value = String(cause);
  } finally { taskLoading.value = false; }
}
async function pollTasks(generation: number) {
  if (generation !== taskGeneration || !connected.value || tab.value !== "tasks") return;
  await loadTasks();
  if (generation === taskGeneration && connected.value && tab.value === "tasks") taskTimer = setTimeout(() => { void pollTasks(generation); }, 2000);
}
watch([tab, connected], () => {
  taskGeneration++;
  if (taskTimer !== undefined) clearTimeout(taskTimer);
  if (!connected.value) {
    taskRecords.value = []; taskDetail.value = null; taskDetailId.value = ""; taskError.value = "";
    embyForm.value.api_token = "";
  }
  if (connected.value && tab.value === "tasks") void pollTasks(taskGeneration);
});
async function cancelTask(task: BackgroundTask) {
  if (!window.confirm("取消任务？当前文件或重命名文件组完成后停止，已完成的修改不会回滚。")) return;
  taskCancelling.value = task.id;
  const generation = taskGeneration;
  try {
    await api(`/tasks/${encodeURIComponent(task.id)}/cancel`, {});
    if (connected.value && generation === taskGeneration) await loadTasks();
  } catch (cause) {
    if (connected.value && generation === taskGeneration) taskError.value = String(cause);
  } finally { taskCancelling.value = ""; }
}
async function queueTask(kind: string, request: unknown) {
  const task = await api<BackgroundTask>("/tasks", { kind, request });
  taskDetail.value = task; taskDetailId.value = task.id;
  tab.value = "tasks";
  notice.value = "任务已提交，可离开页面；执行结果请在任务中心查看。";
}
async function loadEmby() {
  embySaved.value = await api<EmbyConfiguration>("/emby");
  embyForm.value = { ...embySaved.value, api_token: "" };
  embyLibraries.value = []; embyLibrary.value = "";
}
async function saveEmby() {
  await run(async () => {
    try {
      embySaved.value = await api<EmbyConfiguration>("/emby", embyForm.value, "PUT");
      embyForm.value.server_url = embySaved.value.server_url;
      embyLibraries.value = []; embyLibrary.value = "";
      notice.value = "Emby 配置已保存。";
    } finally { embyForm.value.api_token = ""; }
  });
}
async function clearEmbyToken() {
  if (!window.confirm("立即清除已保存的 Emby Token 并关闭自动刷新？")) return;
  await run(async () => {
    embySaved.value = await api<EmbyConfiguration>("/emby/token", undefined, "DELETE");
    embyForm.value.api_token = ""; embyForm.value.auto_refresh = false;
    embyLibraries.value = []; embyLibrary.value = "";
    notice.value = "Token 已清除，自动刷新已关闭。";
  });
}
async function testEmby() {
  await run(async () => {
    const data = await api<{ server_name: string; version: string }>("/emby/test", {});
    notice.value = `连接正常：${data.server_name} ${data.version}`;
  });
}
async function readEmbyLibraries() {
  await run(async () => {
    embyLibraries.value = await api<{ id: string; name: string }[]>("/emby/libraries");
    embyLibrary.value = "";
    notice.value = `已读取 ${embyLibraries.value.length} 个媒体库。`;
  });
}
async function refreshEmby(all: boolean) {
  if (!window.confirm(all ? "请求 Emby 刷新全部媒体库？" : "请求 Emby 刷新所选媒体库？")) return;
  await run(async () => {
    const data = await api<{ message: string }>("/emby/refresh", { library_id: all ? null : embyLibrary.value });
    notice.value = data.message;
  });
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
  selectedFilePaths.value = [];
  if (!config.value?.roots.length) { entries.value = []; return; }
  const data = await api<{ entries: FileEntry[] }>(
    `/files?root=${root.value}&path=${encodeURIComponent(directory.value)}&roots_version=${config.value.roots_version}`,
  );
  entries.value = data.entries;
}

function clearPlan() { plan.value = null; plannedRequest.value = null; }
function choose(file: FileEntry) {
  clearPlan();
  selected.value = file;
  destination.value = file.name;
}
async function enter(path: string) { directory.value = path; fileDeleteResult.value = null; await loadFiles(); }

async function loadWorkspace(reloadEditors = true) {
  config.value = await api<Config>("/config");
  selectedRootPaths.value = [];
  fileDeleteResult.value = null; fileDeletePreview.value = null; fileDeleteRequest.value = null;
  resetMigration();
  if (reloadEditors) hostSource.value = config.value.hosts_source;
  root.value = config.value.roots[0]?.id ?? 0;
  metadataRoot.value = -1;
  renameRoot.value = -1; renameDirectory.value = "";
  renameReport.value = null; renameResult.value = null;
  metadataDirectory.value = "";
  metadataPlan.value = null;
  metadataResult.value = null;
  metadataTarget.value = null; metadataMappingId.value = "";
  metadataCleanupReport.value = null;
  metadataSearchName.value = ""; metadataSearchReport.value = null;
  destinationRoot.value = root.value;
  directory.value = "";
  videoInfoRoot.value = config.value.roots[0]?.id ?? -1;
  videoInfoRows.value = [];
  connected.value = true;
  await loadRecords();
  await Promise.all([loadMetadataMappings(), loadFiles(), loadVideoStatus(),
    ...(reloadEditors ? [loadScrapers(), loadRenameRules(), ...(isAdmin.value ? [loadEmby()] : [])] : [])]);
}

function bitrate(value: number | null | undefined) {
  return value == null ? "未知" : `${(value / 1000000).toFixed(2)} Mbps`;
}

async function loadVideoStatus() {
  videoInfoStatus.value = await api<VideoInfoStatus>("/video-info/status");
}

async function refreshVideoRows() {
  if (videoInfoRoot.value < 0 || !connected.value) { videoInfoRows.value = []; return; }
  const selectedRoot = videoInfoRoot.value;
  const version = config.value?.roots_version;
  videoInfoLoading.value = true; videoInfoError.value = "";
  try {
    const data = await api<{ rows: VideoInfoRow[] }>(
      `/video-info?root=${selectedRoot}&roots_version=${version}`,
    );
    if (videoInfoRoot.value === selectedRoot && config.value?.roots_version === version) {
      videoInfoRows.value = data.rows;
    }
  } catch (cause) {
    videoInfoError.value = cause instanceof Error ? cause.message : String(cause);
  } finally { videoInfoLoading.value = false; }
}

async function pollVideoStatus() {
  if (videoStatusPolling) return;
  videoStatusPolling = true;
  videoStatusError.value = "";
  try {
    const previous = videoInfoStatus.value?.finished_at;
    await loadVideoStatus();
    if (tab.value === "video-info" && previous !== videoInfoStatus.value?.finished_at) {
      await refreshVideoRows();
    }
  } catch (cause) {
    videoStatusError.value = cause instanceof Error ? cause.message : String(cause);
  } finally {
    videoStatusPolling = false;
    if (videoPollingEnabled) videoInfoTimer = setTimeout(() => { void pollVideoStatus(); }, 3000);
  }
}

async function inspectVideos(force = false) {
  await run(async () => {
    if (videoInfoRoot.value < 0) throw new Error("请先选择媒体目录。");
    await queueTask("video.info.scan", { root: videoInfoRoot.value, roots_version: config.value?.roots_version, force });
  });
}

async function inspectAllVideos() {
  await run(async () => {
    await api("/tasks/video-scan-all", {});
    tab.value = "tasks";
    notice.value = "已按媒体目录提交扫描任务；未变化的视频会复用已有记录。";
  });
}

function compareVideo(row: VideoInfoRow) {
  if (!row.details || row.status !== "saved") return;
  const rootPath = config.value?.roots.find(item => item.id === videoInfoRoot.value)?.path ?? "";
  const key = `${rootPath}\\${row.path}`;
  if (videoComparisons.value.some(item => item.key === key)) return;
  if (videoComparisons.value.length >= 4) {
    error.value = "最多同时对比 4 个视频，请先移除一项。"; return;
  }
  videoComparisons.value.push({ key, label: key, details: row.details });
}

watch([tab, videoInfoRoot, connected], () => {
  if (connected.value && tab.value === "video-info") void refreshVideoRows();
});
watch([tab, connected], () => {
  videoPollingEnabled = connected.value && (tab.value === "video-info" || tab.value === "configuration");
  if (videoInfoTimer !== undefined) clearTimeout(videoInfoTimer);
  if (videoPollingEnabled) void pollVideoStatus();
});

async function saveMediaRoots(roots: string[]) {
  await api("/config/media-roots", { roots, roots_version: config.value?.roots_version }, "PUT");
  clearPlan(); selected.value = null;
  await loadWorkspace(false);
  notice.value = "媒体目录已更新并立即生效；旧预览已失效。";
}
async function removeMediaRoots(paths: string[]) {
  if (!paths.length || !window.confirm(`移除以下 ${paths.length} 个媒体目录？\n${paths.join("\n")}\n只取消访问，不删除磁盘文件。`)) return;
  const removed = new Set(paths);
  await run(() => saveMediaRoots(mediaRootsDraft.value.filter(path => !removed.has(path))));
}

async function browseMediaFolders(path?: string | null) {
  await run(async () => {
    mediaFolderError.value = "";
    try {
      mediaFolderListing.value = await api<MediaFolderListing>(
        `/config/media-folders${path ? `?path=${encodeURIComponent(path)}` : ""}`,
      );
    } catch (cause) {
      mediaFolderError.value = cause instanceof Error ? cause.message : String(cause);
      throw cause;
    }
  });
}
async function openMediaFolderDialog(purpose: "root" | "migration" = "root") {
  folderPickerPurpose.value = purpose;
  mediaFolderListing.value = null; mediaFolderError.value = "";
  await nextTick();
  mediaFolderDialog.value?.showModal();
  await browseMediaFolders();
}
async function addMediaFolder() {
  const listing = mediaFolderListing.value;
  if (!listing?.path || !listing.selectable || mediaFolderError.value) return;
  if (folderPickerPurpose.value === "root" && (mediaFolderAlreadyAdded.value || mediaRootsDraft.value.length >= 32)) return;
  const path = listing.path;
  await run(async () => {
    try {
      if (folderPickerPurpose.value === "migration") await loadMigrationFiles(path);
      else await saveMediaRoots([...mediaRootsDraft.value, path]);
      mediaFolderDialog.value?.close();
    } catch (cause) {
      mediaFolderError.value = cause instanceof Error ? cause.message : String(cause);
      throw cause;
    }
  });
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
    selectedRootPaths.value = []; mediaFolderListing.value = null; mediaFolderError.value = "";
    selectedFilePaths.value = []; fileDeletePreview.value = null; fileDeleteRequest.value = null; fileDeleteResult.value = null;
    resetMigration();
    selectedRuleIds.value = []; selectedMappingIds.value = [];
    entries.value = []; records.value = []; users.value = [];
    recordsVersion.value++; operationDetails.value = {};
    runtimeLog.value = null;
    scrapers.value = [];
    savedScrapers.value = [];
    renameRules.value = []; savedRenameRules.value = "";
    renameReport.value = null; renameResult.value = null;
    metadataPlan.value = null; metadataResult.value = null;
    metadataMappings.value = []; metadataTarget.value = null; metadataMappingId.value = "";
    metadataCleanupReport.value = null;
    metadataSearchName.value = ""; metadataSearchReport.value = null;
    clearPlan(); selected.value = null; hostReport.value = null;
  });
}

onMounted(async () => {
  syncTabFromHash();
  window.addEventListener("hashchange", syncTabFromHash);
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
    roots_version: config.value?.roots_version,
  };
  await run(async () => {
    clearPlan();
    const result = await api<FilePlan>("/files/action", request);
    plannedRequest.value = { ...request, preview_id: result.preview_id };
    plan.value = result;
  });
}

async function execute() {
  if (!plannedRequest.value || !plan.value) return;
  const request = { ...plannedRequest.value, dry_run: false };
  if (!window.confirm(`确认执行文件操作？\n${plan.value.source}${plan.value.destination ? `\n→ ${plan.value.destination}` : ""}`)) return;
  await run(async () => {
    // Discard the plan before sending a mutation: network errors must not encourage replay.
    clearPlan();
    await api("/files/action", request);
    await loadFiles();
    notice.value = "操作已完成，并已记录到 PostgreSQL。";
  });
}

async function confirmFileDeletion(paths: string[]) {
  await run(async () => {
    if (!config.value || !paths.length) throw new Error("请先选择需要删除的文件。");
    const request = { root: root.value, paths: [...paths], roots_version: config.value.roots_version };
    fileDeleteError.value = ""; fileDeletePreview.value = null; fileDeleteRequest.value = null;
    const preview = await api<DeleteFilesPreview>("/files/delete-batch", { ...request, dry_run: true });
    fileDeletePreview.value = preview;
    fileDeleteRequest.value = { ...request, preview_id: preview.preview_id };
    await nextTick();
    fileDeleteDialog.value?.showModal();
  });
}
async function deleteFiles() {
  const request = fileDeleteRequest.value;
  if (!request) return;
  await run(async () => {
    fileDeleteRequest.value = null;
    fileDeleteResult.value = null;
    try {
      fileDeleteResult.value = await api<DeleteFilesResult>("/files/delete-batch", { ...request, dry_run: false });
    } catch (cause) {
      fileDeleteError.value = `${cause instanceof Error ? cause.message : String(cause)}。请关闭弹窗并刷新列表核对，不要直接重复删除。`;
      throw cause;
    }
    fileDeleteDialog.value?.close();
    await loadFiles();
    noticeTone.value = fileDeleteResult.value.outcome === "success" ? "success" : "warning";
    notice.value = `删除完成：成功 ${fileDeleteResult.value.deleted_count} 个，失败 ${fileDeleteResult.value.files.length - fileDeleteResult.value.deleted_count} 个。`;
  });
}

function resetMigration() {
  migrationSource.value = ""; migrationTarget.value = -1;
  migrationEntries.value = []; migrationSelected.value = [];
  migrationPreview.value = null; migrationRequest.value = null;
  migrationResult.value = null; migrationError.value = "";
}
async function loadMigrationFiles(folder = migrationSource.value) {
  const data = await api<{ source_folder: string; entries: FileEntry[] }>(
    `/files/migration/source?path=${encodeURIComponent(folder)}`,
  );
  migrationSource.value = data.source_folder;
  migrationEntries.value = data.entries;
  migrationSelected.value = [];
}
watch([migrationSource, migrationTarget], () => {
  migrationPreview.value = null; migrationRequest.value = null;
  migrationResult.value = null; migrationError.value = "";
});
async function confirmMigration() {
  await run(async () => {
    if (!config.value || !migrationSource.value || migrationTarget.value < 0 || !migrationSelected.value.length) {
      throw new Error("请选择下载目录、需要迁移的文件和目标媒体目录。");
    }
    const request: MigrationRequest = {
      source_folder: migrationSource.value, paths: [...migrationSelected.value],
      target_root: migrationTarget.value, roots_version: config.value.roots_version,
    };
    migrationError.value = ""; migrationPreview.value = null; migrationRequest.value = null;
    const preview = await api<MigrationPreview>("/files/migration", { ...request, dry_run: true });
    migrationPreview.value = preview;
    migrationRequest.value = { ...request, preview_id: preview.preview_id };
    await nextTick();
    migrationDialog.value?.showModal();
  });
}
async function migrateFiles() {
  const request = migrationRequest.value;
  if (!request) return;
  await run(async () => {
    migrationRequest.value = null; migrationResult.value = null;
    try {
      await queueTask("file.migrate", { ...request, dry_run: false });
    } catch (cause) {
      migrationError.value = `${cause instanceof Error ? cause.message : String(cause)}。请核对源目录、目标目录和操作记录，不要直接重复迁移。`;
      throw cause;
    }
    migrationDialog.value?.close();
    migrationSelected.value = [];
  });
}

watch([renameRoot, renameDirectory, renameForce], () => {
  renameReport.value = null; renameResult.value = null;
});
watch(() => JSON.stringify(renameRules.value), () => {
  renameReport.value = null; renameResult.value = null;
});

async function loadRenameRules() {
  renameRules.value = await api<RenameRule[]>("/renaming/rules");
  savedRenameRules.value = JSON.stringify(renameRules.value);
  selectedRuleIds.value = [];
}
async function addRenameRule() {
  error.value = "";
  newRule.value = {
    id: crypto.randomUUID(), name: "新规则", pattern: "(?P<episode>\\d{1,3})",
    template: "{episode:02d}", enabled: true,
    priority: renameRules.value.length,
  };
  await nextTick();
  newRuleDialog.value?.showModal();
}
function mergeRenameRules(data: RenameRule[]) {
  const baseline: RenameRule[] = JSON.parse(savedRenameRules.value || "[]");
  const drafts = new Map(renameRules.value.map(item => [item.id, item]));
  const previous = new Map(baseline.map(item => [item.id, item]));
  const byId = new Map(data.map(item => [item.id, item]));
  const orderChanged = JSON.stringify(renameRules.value.map(item => item.id)) !== JSON.stringify(baseline.map(item => item.id));
  const ordered = orderChanged
    ? [...renameRules.value.flatMap(item => { const saved = byId.get(item.id); return saved ? [saved] : []; }), ...data.filter(item => !drafts.has(item.id))]
    : data;
  renameRules.value = ordered.map((item, priority) => {
    const draft = drafts.get(item.id), saved = previous.get(item.id);
    if (!draft || !saved) return { ...item, priority };
    return {
      ...item, priority,
      name: draft.name !== saved.name ? draft.name : item.name,
      pattern: draft.pattern !== saved.pattern ? draft.pattern : item.pattern,
      enabled: draft.enabled !== saved.enabled ? draft.enabled : item.enabled,
    };
  });
  savedRenameRules.value = JSON.stringify(data);
  selectedRuleIds.value = selectedRuleIds.value.filter(id => byId.has(id));
}
async function createRenameRule() {
  await run(async () => {
    const result = await api<{ rules: RenameRule[] }>("/renaming/rules", newRule.value);
    mergeRenameRules(result.rules);
    newRuleDialog.value?.close();
    notice.value = "规则已新增并立即生效；其他未保存的编辑已保留。";
  });
}
async function deleteRenameRules(ids: string[]) {
  if (!ids.length || !window.confirm(`删除选中的 ${ids.length} 条命名规则？\n不会改动媒体文件，其他未保存的编辑将保留。`)) return;
  await run(async () => {
    const result = await api<{ rules: RenameRule[] }>("/renaming/rules/delete", { ids: [...ids] });
    mergeRenameRules(result.rules);
    notice.value = `已删除 ${ids.length} 条规则，立即生效。`;
  });
}
function moveRenameRule(index: number, direction: number) {
  const target = index + direction;
  if (target < 0 || target >= renameRules.value.length) return;
  const [rule] = renameRules.value.splice(index, 1);
  if (rule) renameRules.value.splice(target, 0, rule);
  renameRules.value.forEach((item, priority) => { item.priority = priority; });
}
async function saveRenameRules() {
  await run(async () => {
    renameRules.value.forEach((item, priority) => { item.priority = priority; });
    await api("/renaming/rules", { rules: renameRules.value }, "PUT");
    await loadRenameRules();
    notice.value = "正则规则已保存到数据库，旧预览已失效；规则变化后会重新检查目录。";
  });
}
async function openRenaming() {
  renameRoot.value = root.value; renameDirectory.value = directory.value;
  renameReport.value = null; renameResult.value = null;
  tab.value = "renaming";
  await nextTick();
  await previewRenaming();
}
async function selectRenameRoot() {
  renameDirectory.value = "";
  await nextTick();
  await previewRenaming();
}
function setRenameReport(report: RenameReport) {
  renameReport.value = { ...report, rows: report.rows.map(row => ({ ...row, draft_name: row.new_name })) };
}
async function previewRenaming() {
  await run(async () => {
    if (renameRoot.value < 0) throw new Error("请先选择本次需要重命名的媒体目录。");
    if (renameRulesDirty.value) {
      openConfiguration("renaming");
      throw new Error("正则规则已修改，请先在配置管理中保存到数据库再预览。");
    }
    renameReport.value = null; renameResult.value = null;
    const report = await api<RenameReport>("/renaming/preview", {
      root: renameRoot.value, path: renameDirectory.value, force: renameForce.value,
      roots_version: config.value?.roots_version,
    });
    setRenameReport(report);
    notice.value = report.unchanged ? "当前目录没有变化，已跳过，无文件改名。"
      : `预览完成，${report.ready_count} 个视频需要修改。请核对目标名；修改后需重新校验。`;
  });
}
async function reviewRenaming() {
  const report = renameReport.value;
  if (!report?.preview_id) return;
  await run(async () => {
    const edits = report.rows.filter(row => row.editable && (row.draft_name ?? row.new_name) !== row.new_name)
      .map(row => ({ source: row.source, new_name: row.draft_name ?? row.new_name }));
    setRenameReport(await api<RenameReport>("/renaming/review", { preview_id: report.preview_id, edits }));
    notice.value = "修改项已重新校验并生成新预览，请核对后确认执行。";
  });
}
async function applyRenaming() {
  const report = renameReport.value;
  if (!report?.preview_id || !report.ready_count || renameEditsPending.value || renameRulesDirty.value) return;
  if (!window.confirm(`确认按已校验的预览重命名 ${report.ready_count} 个视频及相关 NFO / 字幕，共 ${report.file_count} 个文件？仅当前文件夹，不处理子文件夹；冲突项跳过，不覆盖文件。`)) return;
  await run(async () => {
    renameReport.value = null;
    await queueTask("rename.apply", { preview_id: report.preview_id });
    metadataPlan.value = null;
  });
}

watch([metadataRoot, metadataDirectory, metadataType, metadataLanguage, metadataOverwrite], () => {
  metadataPlan.value = null;
  metadataResult.value = null;
});
watch(() => selectedScrapers.value.join(","), () => {
  metadataPlan.value = null; metadataResult.value = null; metadataSearchReport.value = null;
});
watch([metadataRoot, metadataDirectory], () => {
  metadataTarget.value = null; metadataMappingId.value = ""; metadataSearchReport.value = null;
});
watch(metadataType, () => { metadataMappingId.value = ""; selectedMappingIds.value = []; });
watch([metadataType, metadataLanguage, metadataSearchName], () => {
  metadataSearchReport.value = null;
});

async function searchMetadata(page = 1) {
  await run(async () => {
    await ensureScrapersReady();
    metadataSearchReport.value = null;
    metadataSearchReport.value = await api<MetadataSearchReport>("/metadata/search", {
      query: metadataSearchName.value, media_type: metadataType.value,
      language: metadataLanguage.value, page,
      parse_folder_name: metadataSearchIsFolder.value,
      scrapers: selectedScrapers.value,
    });
  });
}

async function loadScrapers() {
  const data = await api<Omit<ScraperConfig, "api_key">[]>("/metadata/scrapers");
  scrapers.value = data.map(item => ({ ...item, api_key: "" }));
  savedScrapers.value = scrapers.value.map(item => ({ ...item }));
  if (config.value) config.value.tmdb_key_configured = data.some(item => item.id === "tmdb" && item.configured);
}

async function ensureScrapersReady() {
  const selected = executionScrapers.value;
  const missing = selected.filter(item => !item.configured);
  if (!selected.length || missing.length) {
    openConfiguration("scrapers");
    await nextTick();
    scraperSettingsPanel.value?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    throw new Error(!selected.length ? "请在配置管理中至少启用一个刮削器并保存。"
      : `尚未配置 ${missing.map(item => item.name).join("、")} Key，请在“配置管理 → 刮削器配置”中填写并保存后执行。`);
  }
}

function moveScraper(id: string, direction: number) {
  const order = [...scraperOrder.value];
  const index = order.findIndex(item => item.id === id);
  const target = index + direction;
  if (index < 0 || target < 0 || target >= order.length) return;
  const [item] = order.splice(index, 1);
  if (!item) return;
  order.splice(target, 0, item);
  order.forEach((scraper, priority) => { scraper.priority = priority; });
}

async function saveScrapers() {
  await run(async () => {
    try {
      await api("/metadata/scrapers", {
        scrapers: scrapers.value.map(item => ({
          id: item.id, enabled: item.enabled, priority: item.priority,
          ...(item.api_key ? { api_key: item.api_key } : {}),
        })),
      }, "PUT");
      metadataPlan.value = null; metadataResult.value = null; metadataSearchReport.value = null;
      metadataCleanupReport.value = null;
      await loadScrapers();
      notice.value = "刮削器配置已保存到数据库并立即生效。Key 不会回显，修改后的配置请重新预览。";
    } finally { for (const item of scrapers.value) item.api_key = ""; }
  });
}

async function clearScraperKey(scraper: ScraperConfig) {
  if (!window.confirm(`立即清除 ${scraper.name} Key？需要重新填写并保存后才能使用该刮削器。其他未保存的编辑将保留。`)) return;
  await run(async () => {
    const drafts = new Map(scrapers.value.map(item => [item.id, { ...item }]));
    const baseline = new Map(savedScrapers.value.map(item => [item.id, item]));
    await api(`/metadata/scrapers/${scraper.id}/key`, undefined, "DELETE");
    scraper.api_key = "";
    metadataPlan.value = null; metadataResult.value = null; metadataSearchReport.value = null;
    metadataCleanupReport.value = null;
    await loadScrapers();
    scrapers.value = scrapers.value.map(item => {
      const draft = drafts.get(item.id), saved = baseline.get(item.id);
      if (!draft || !saved) return item;
      return {
        ...item,
        enabled: draft.enabled !== saved.enabled ? draft.enabled : item.enabled,
        priority: draft.priority !== saved.priority ? draft.priority : item.priority,
        api_key: item.id === scraper.id ? "" : draft.api_key,
      };
    });
    notice.value = `${scraper.name} Key 已清除并立即生效，其他编辑未提交。`;
  });
}

async function chooseMetadataCandidate(candidate: MetadataCandidate) {
  if (candidate.scraper_id !== "tmdb") {
    error.value = "当前目录手动关联使用 TMDB 主标识，请选择 TMDB 候选；其他来源参与信息补全。";
    return;
  }
  metadataMappingId.value = candidate.id;
  await saveMetadataMapping(metadataTarget.value?.path, candidate.id);
}

async function selectMetadataFolder(name: string, path: string) {
  openConfiguration("mappings");
  await nextTick();
  metadataTarget.value = { name, path };
  metadataMappingId.value = "";
  metadataSearchName.value = name;
  metadataSearchIsFolder.value = true;
  metadataSearchReport.value = null;
  metadataSearchPanel.value?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  await searchMetadata();
}

async function editMetadataMapping(mapping: MetadataMapping) {
  await run(async () => {
    if (mapping.root === null) throw new Error("关联的媒体根目录不在当前白名单中，请先恢复配置或解除关联");
    metadataRoot.value = mapping.root;
    metadataDirectory.value = mapping.folder_path;
    await nextTick();
    metadataTarget.value = { name: mapping.folder_name, path: mapping.folder_path };
    metadataMappingId.value = mapping.tmdb_id;
    metadataSearchReport.value = null;
    metadataSearchName.value = mapping.folder_name;
    metadataSearchIsFolder.value = true;
    if (metadataMappingPanel.value) {
      metadataMappingPanel.value.open = true;
      metadataMappingPanel.value.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  });
}

async function loadMetadataMappings() {
  metadataMappings.value = await api<MetadataMapping[]>("/metadata/mappings");
  selectedMappingIds.value = selectedMappingIds.value.filter(id => metadataMappings.value.some(item => item.id === id));
}

async function saveMetadataMapping(path: string | undefined, value: number | string | undefined) {
  await run(async () => {
    const id = Number(value);
    if (path === undefined || metadataRoot.value < 0 || !Number.isSafeInteger(id) || id <= 0 || id > 2147483647) {
      throw new Error("请先选择影视文件夹，并填写有效的正整数 TMDB ID");
    }
    await api("/metadata/mappings", {
      media_type: metadataType.value, root: metadataRoot.value, path, tmdb_id: id,
      roots_version: config.value?.roots_version,
    });
    metadataPlan.value = null; metadataResult.value = null;
    await loadMetadataMappings();
    metadataCleanupReport.value = null;
    notice.value = "文件夹与 ID 的关联已保存，下次按实际目录自动使用。请重新预览后写入。";
  });
}

async function deleteMetadataMappings(ids: string[]) {
  if (!ids.length || !window.confirm(`解除选中的 ${ids.length} 条影视关联？之后恢复自动搜索，不删除媒体文件。`)) return;
  await run(async () => {
    await api("/metadata/mappings/delete", { ids: [...ids] });
    metadataPlan.value = null; metadataResult.value = null;
    await loadMetadataMappings();
    metadataCleanupReport.value = null;
    notice.value = "映射已删除，请重新预览。已有 NFO 和图片不会改变。";
  });
}

async function cleanupMetadataMappings(dryRun: boolean) {
  if (!dryRun && !window.confirm("检查所有关联并清理确认失效的记录？仅删除关联，不删除视频、NFO 或图片；网络失败或媒体根目录离线时保留。")) return;
  await run(async () => {
    metadataCleanupReport.value = null;
    metadataCleanupReport.value = await api<MetadataCleanupReport>("/metadata/mappings/cleanup", {
      dry_run: dryRun,
    });
    if (!dryRun) { metadataPlan.value = null; metadataResult.value = null; }
    await loadMetadataMappings();
    noticeTone.value = metadataCleanupReport.value.outcome === "partial" ? "warning" : "success";
    notice.value = dryRun ? `检查完成，确认失效 ${metadataCleanupReport.value.invalid_count} 条，未删除记录。`
      : `检查完成，已清理 ${metadataCleanupReport.value.removed_count} 条失效关联。`;
  });
}

async function selectMetadataRoot() {
  metadataDirectory.value = "";
  await nextTick();
  if (tab.value === "metadata") await previewMetadata();
  else {
    const folder = config.value?.roots.find(item => item.id === metadataRoot.value);
    if (folder) {
      const name = folder.path.split(/[\\/]/).filter(Boolean).at(-1) ?? folder.path;
      metadataTarget.value = { name, path: "" };
      metadataMappingId.value = "";
      metadataSearchName.value = name;
      metadataSearchIsFolder.value = true;
      metadataSearchReport.value = null;
    }
  }
}

async function previewMetadata() {
  await run(async () => {
    if (metadataRoot.value < 0) throw new Error("请先选择本次需要刮削的媒体目录。");
    await ensureScrapersReady();
    metadataPlan.value = null;
    metadataResult.value = null;
    metadataPlan.value = await api<MetadataPlan>("/metadata/preview", {
      root: metadataRoot.value, path: metadataDirectory.value, media_type: metadataType.value,
      language: metadataLanguage.value, overwrite: metadataOverwrite.value,
      scrapers: selectedScrapers.value,
      roots_version: config.value?.roots_version,
    });
    for (const row of metadataPlan.value.rows) row.manual_id = row.tmdb_id ?? "";
    noticeTone.value = metadataPlan.value.outcome === "success" ? "success" : "warning";
    notice.value = `预览完成，待写入 ${metadataPlan.value.file_count} 个文件；尚未修改媒体目录。`;
  });
}

async function applyMetadata() {
  const previewId = metadataPlan.value?.preview_id;
  if (!previewId || metadataMappingEdited.value || !window.confirm(
    metadataPlan.value?.overwrite ? "确认写入？已有文件将先备份再覆盖。" : "确认补齐缺失的 NFO 和图片？",
  )) return;
  await run(async () => {
    await ensureScrapersReady();
    metadataPlan.value = null;
    await queueTask("metadata.apply", { preview_id: previewId });
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
onUnmounted(() => {
  taskGeneration++;
  if (taskTimer !== undefined) clearTimeout(taskTimer);
  videoPollingEnabled = false;
  if (videoInfoTimer !== undefined) clearTimeout(videoInfoTimer);
  stopLogTracking();
  window.removeEventListener("hashchange", syncTabFromHash);
});

async function loadRecords() {
  const data = await api<OperationSummary[]>("/operations?summary=true");
  recordsVersion.value++;
  operationDetails.value = {};
  records.value = data;
}
async function loadOperation(id: string) {
  const existing = operationDetails.value[id];
  if (existing?.loading || (existing?.data && existing.data.status !== "running")) return;
  const version = recordsVersion.value;
  operationDetails.value[id] = { loading: true, error: "" };
  try {
    const data = await api<Operation>(`/operations/${encodeURIComponent(id)}`);
    if (connected.value && recordsVersion.value === version) {
      operationDetails.value[id] = { loading: false, error: "", data };
    }
  } catch (cause) {
    if (connected.value && recordsVersion.value === version) {
      operationDetails.value[id] = { loading: false, error: cause instanceof Error ? cause.message : String(cause) };
    }
  }
}
function toggleOperation(id: string, event: Event) {
  if (event.target instanceof HTMLDetailsElement && event.target.open) void loadOperation(id);
}
async function showRecords() {
  tab.value = "operations";
  await run(loadRecords);
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
    <div class="login-brand"><h1>MediaForge</h1></div>
    <form v-if="!restoring" class="panel login-card" @submit.prevent="login">
      <h2>登录</h2>
      <label>用户名<input v-model="username" autocomplete="username" required maxlength="64" :disabled="busy" /></label>
      <label>密码<input v-model="password" type="password" autocomplete="current-password" required maxlength="128" :disabled="busy" /></label>
      <div v-if="error" class="message error" role="alert">{{ error }}</div>
      <div v-if="notice" class="message" :class="noticeTone">{{ notice }}</div>
      <button class="primary" :disabled="busy">{{ busy ? "正在登录…" : "登录" }}</button>
    </form>
    <p v-else>正在恢复登录状态…</p>
  </div>
  <div v-else class="admin-layout">
    <aside class="sidebar">
      <div class="brand"><span class="brand-mark" aria-hidden="true">M</span><div>MediaForge<small>本机影视工作台</small></div></div>
      <nav aria-label="主导航">
        <button :class="{ active: tab === 'dashboard' }" :aria-current="tab === 'dashboard' ? 'page' : undefined" @click="tab = 'dashboard'">工作台</button>
        <div class="nav-section" role="group" aria-label="媒体工具">
        <span class="nav-group">媒体工具</span>
        <div class="nav-children">
        <button :class="{ active: tab === 'files' }" :aria-current="tab === 'files' ? 'page' : undefined" @click="tab = 'files'">文件浏览</button>
        <button v-if="isAdmin" :class="{ active: tab === 'migration' }" :aria-current="tab === 'migration' ? 'page' : undefined" @click="tab = 'migration'">文件迁移</button>
        <button :class="{ active: tab === 'renaming' }" :aria-current="tab === 'renaming' ? 'page' : undefined" @click="tab = 'renaming'">批量重命名</button>
        <button :class="{ active: tab === 'metadata' }" :aria-current="tab === 'metadata' ? 'page' : undefined" @click="tab = 'metadata'">影视刮削</button>
        <button :class="{ active: tab === 'video-info' }" :aria-current="tab === 'video-info' ? 'page' : undefined" @click="tab = 'video-info'">视频分析 <small>自动</small></button>
        <button :class="{ active: tab === 'downloads' }" :aria-current="tab === 'downloads' ? 'page' : undefined" @click="tab = 'downloads'">影视下载 <small>预留</small></button>
        </div>
        </div>
        <div v-if="isAdmin" class="nav-section" role="group" aria-label="配置与管理">
        <span class="nav-group">配置与管理</span>
        <div class="nav-children">
        <button :class="{ active: tab === 'configuration' }" :aria-current="tab === 'configuration' ? 'page' : undefined" @click="openConfiguration()">配置管理</button>
        </div>
        </div>
        <details class="nav-section nav-tools" :open="['hosts', 'users', 'settings', 'tasks', 'operations', 'logs'].includes(tab)">
          <summary>系统工具</summary>
          <div class="nav-children">
            <button :class="{ active: tab === 'hosts' }" :aria-current="tab === 'hosts' ? 'page' : undefined" @click="tab = 'hosts'">Hosts 更新</button>
            <button :class="{ active: tab === 'tasks' }" :aria-current="tab === 'tasks' ? 'page' : undefined" @click="tab = 'tasks'">任务中心</button>
            <button :class="{ active: tab === 'operations' }" :aria-current="tab === 'operations' ? 'page' : undefined" :disabled="busy" @click="showRecords">操作记录</button>
            <button :class="{ active: tab === 'logs' }" :aria-current="tab === 'logs' ? 'page' : undefined" @click="tab = 'logs'">运行日志</button>
            <button v-if="isAdmin" :class="{ active: tab === 'users' }" :aria-current="tab === 'users' ? 'page' : undefined" :disabled="busy" @click="showUsers">用户管理</button>
            <button :class="{ active: tab === 'settings' }" :aria-current="tab === 'settings' ? 'page' : undefined" @click="tab = 'settings'">系统与账号</button>
          </div>
        </details>
      </nav>
      <div class="sidebar-account" aria-label="当前登录账号">
        <div class="account-identity">
          <span class="account-avatar" aria-hidden="true">{{ currentUser?.username.slice(0, 1).toUpperCase() }}</span>
          <div class="account-details">
            <span class="account-name" :title="currentUser?.username">{{ currentUser?.username }}</span>
            <span class="account-role">{{ isAdmin ? "管理员" : "只读用户" }}</span>
          </div>
        </div>
        <button class="logout-button" :disabled="busy" @click="logout">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M10 4H4v16h6m4-12 4 4-4 4m-6-4h12" /></svg>
          退出登录
        </button>
      </div>
    </aside>
    <main class="shell" :data-page="tab">
    <header class="page-header">
      <div class="page-heading"><div class="page-title"><span class="page-group">{{ currentPage.group }}</span><span class="page-separator" aria-hidden="true">/</span><h1>{{ currentPage.title }}</h1></div><p>{{ currentPage.description }}</p></div>
    </header>
    <div v-if="error" class="message error" role="alert">{{ error }}</div>
    <div v-if="notice" class="message" :class="noticeTone" role="status">{{ notice }}</div>
    <template v-if="connected && config">
      <section v-if="tab === 'dashboard'">
        <div class="welcome"><h2>欢迎回来，{{ currentUser?.username }}</h2><a v-if="isAdmin" class="button-link" href="#configuration/roots">管理媒体目录 <span aria-hidden="true">↗</span></a></div>
        <div class="stats">
          <a class="panel stat-link" href="#files"><span>媒体目录</span><strong>{{ config.roots.length }}</strong></a>
          <a class="panel stat-link" href="#operations" @click="showRecords"><span>最近操作（最多 100 条）</span><strong>{{ records.length }}</strong></a>
          <a class="panel stat-link" href="#operations" @click="showRecords"><span>最近异常 / 执行中</span><strong>{{ records.filter(item => item.status !== "success").length }}</strong></a>
          <a class="panel stat-link" href="#hosts"><span>Hosts 自动更新</span><strong>{{ config.hosts_interval_seconds ? "已开启" : "手动" }}</strong></a>
        </div>
        <section class="tools-section"><div class="section-heading"><h2>常用工具</h2><span>选择工具，开始整理</span></div><div class="module-grid">
          <button class="module-card" data-tool="files" @click="tab = 'files'"><svg class="module-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M3 7V5h6l2 2h10v12H3V7Z" /></svg><b>文件浏览</b><span>查看文件、移动或单独重命名</span><span class="module-arrow" aria-hidden="true">↗</span></button>
          <button class="module-card" data-tool="renaming" @click="tab = 'renaming'"><svg class="module-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M4 6h14m-4-4 4 4-4 4M20 18H6m4-4-4 4 4 4M4 11v8m-2-4h4M20 5v8m-2-4h4" /></svg><b>批量重命名</b><span>按集号统一名称，保留原始格式</span><span class="module-arrow" aria-hidden="true">↗</span></button>
          <button class="module-card" data-tool="metadata" @click="tab = 'metadata'"><svg class="module-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><rect x="4" y="3" width="16" height="18" rx="2" /><path d="M8 3v18M16 3v18M4 8h4m-4 8h4M16 8h4m-4 8h4" /></svg><b>影视刮削</b><span>匹配影视，写入 NFO 与海报</span><span class="module-arrow" aria-hidden="true">↗</span></button>
          <button class="module-card" data-tool="video-info" @click="tab = 'video-info'"><svg class="module-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><rect x="3" y="4" width="18" height="14" rx="2" /><path d="M8 22h8m-4-4v4M7 13v-3m5 3V7m5 6v-4" /></svg><b>视频分析</b><span>查看编码、码率与播放风险</span><span class="module-arrow" aria-hidden="true">↗</span></button>
        </div></section>
      </section>
      <section v-if="tab === 'tasks'" class="panel">
        <div class="config-section-heading"><h2>后台任务（最近 100 条）</h2><button :disabled="taskLoading" @click="loadTasks">刷新任务</button></div>
        <p class="help-text">每 2 秒更新。取消在当前文件或整组重命名完成后生效，不回滚已完成的修改。服务重启后未完成任务标记为中断，不自动重试。</p>
        <p v-if="taskError" class="message error" role="alert">{{ taskError }}</p>
        <div class="table-wrap"><table><thead><tr><th>任务 / 时间</th><th>状态</th><th>进度</th><th>操作</th></tr></thead><tbody>
          <tr v-for="task in taskRecords" :key="task.id">
            <td><strong>{{ taskNames[task.kind] || task.kind }}</strong><p class="path-caption">{{ task.actor }} · {{ new Date(task.created_at).toLocaleString() }}</p></td>
            <td><span class="status-chip" :class="{ 'status-error': task.status === 'failed', 'status-warning': task.status !== 'success' && task.status !== 'failed' }">{{ taskStatuses[task.status] || task.status }}</span><p v-if="task.cancel_requested && taskActive(task)" class="help-text">等待安全停止</p></td>
            <td><progress v-if="taskActive(task)" :max="task.total || 1" :value="task.total ? task.completed : undefined" aria-label="任务进度"></progress><span v-if="task.total"> {{ task.completed }} / {{ task.total }}</span><p class="path-caption">{{ task.current || task.error || "—" }}</p></td>
            <td><div class="row-actions"><button @click="taskDetail = null; loadTaskDetail(task.id)">查看结果</button><button v-if="isAdmin && taskActive(task)" :disabled="task.cancel_requested || taskCancelling === task.id" @click="cancelTask(task)">取消任务</button></div></td>
          </tr>
          <tr v-if="!taskRecords.length"><td colspan="4">{{ taskLoading ? "正在加载…" : "暂无后台任务" }}</td></tr>
        </tbody></table></div>
        <div v-if="taskDetail" class="task-detail">
          <h3>{{ taskNames[taskDetail.kind] || taskDetail.kind }} · {{ taskStatuses[taskDetail.status] || taskDetail.status }}</h3>
          <p class="path-caption">任务编号：{{ taskDetail.id }}</p>
          <p v-if="taskDetail.error" class="message error">{{ taskDetail.error }}</p>
          <p v-if="taskActive(taskDetail)" class="help-text">任务尚未结束，结果将在完成后显示。</p>
          <pre v-else>{{ JSON.stringify(taskDetail.result, null, 2) }}</pre>
        </div>
      </section>
      <section v-if="tab === 'configuration'" class="configuration-nav">
        <p v-if="!isAdmin">只有管理员可以修改配置。</p>
        <nav v-else class="section-tabs" aria-label="配置分类">
          <button v-for="section in configurationSections" :key="section.id" :class="{ active: configurationSection === section.id }" :aria-current="configurationSection === section.id ? 'page' : undefined" @click="configurationSection = section.id">{{ section.name }}<span v-if="section.id === 'renaming' && renameRulesDirty" class="unsaved-dot" aria-label="有未保存的修改"></span></button>
        </nav>
      </section>
      <section v-if="isAdmin && tab === 'configuration' && configurationSection === 'roots'" class="panel">
        <h2>媒体目录</h2>
        <div>
          <div class="config-section-heading">
            <p class="help-text">新增、移除立即生效，无需再次保存；移除不会删除磁盘文件。</p>
            <button type="button" :disabled="busy || mediaRootsDraft.length >= 32" @click="openMediaFolderDialog('root')">添加文件夹</button>
          </div>
          <div class="row-actions selection-actions">
            <label class="check-control"><input type="checkbox" :checked="!!mediaRootsDraft.length && selectedRootPaths.length === mediaRootsDraft.length" :disabled="busy || !mediaRootsDraft.length" @change="selectedRootPaths = selectedRootPaths.length === mediaRootsDraft.length ? [] : [...mediaRootsDraft]" /><span>全选目录</span></label>
            <button type="button" class="danger-button" :disabled="busy || !selectedRootPaths.length" @click="removeMediaRoots(selectedRootPaths)">批量移除（{{ selectedRootPaths.length }}）</button>
          </div>
          <div class="table-wrap"><table><thead><tr><th class="selection-cell">选择</th><th>已保存的媒体目录（{{ mediaRootsDraft.length }} / 32）</th><th>操作</th></tr></thead><tbody>
            <tr v-for="path in mediaRootsDraft" :key="path">
              <td class="selection-cell"><input v-model="selectedRootPaths" type="checkbox" :value="path" :disabled="busy" :aria-label="`选择目录 ${path}`" /></td>
              <td><strong>{{ folderName(path) }}</strong><p class="path-caption"><code>{{ path }}</code></p></td><td><button type="button" class="danger-button" :disabled="busy" @click="removeMediaRoots([path])">移除目录</button></td>
            </tr>
            <tr v-if="!mediaRootsDraft.length"><td colspan="3">尚未选择目录，请点击“添加文件夹”。</td></tr>
          </tbody></table></div>
        </div>
      </section>
      <section v-if="tab === 'files'" class="panel">
        <h2>媒体文件</h2>
        <div v-if="!config.roots.length" class="message">
          尚未配置媒体目录，请由管理员在“配置管理”中添加文件夹。
          <a v-if="isAdmin" href="#configuration">前往配置管理</a>
        </div>
        <template v-else>
          <div class="toolbar">
            <label class="directory-field">媒体目录<select v-model.number="root" :disabled="busy" @change="run(() => enter(''))">
              <option v-for="item in config.roots" :key="item.id" :value="item.id" :title="item.path">{{ rootLabel(item.path) }}</option>
            </select></label>
            <button :disabled="busy || !directory" @click="run(() => enter(parent))">上一级</button>
            <button :disabled="busy" @click="run(loadFiles)">刷新</button>
            <button v-if="isAdmin" :disabled="busy" @click="openRenaming">批量重命名</button>
          </div>
          <p class="path-caption"><code>{{ rootPath(root) }}{{ directory ? "\\" + directory : "" }}</code></p>
          <div v-if="isAdmin" class="row-actions selection-actions">
            <label class="check-control"><input type="checkbox" :checked="!!deletableFiles.length && deletableFiles.slice(0, 1000).every(file => selectedFilePaths.includes(file.path))" :disabled="busy || !deletableFiles.length" @change="selectedFilePaths = deletableFiles.slice(0, 1000).every(file => selectedFilePaths.includes(file.path)) ? [] : deletableFiles.slice(0, 1000).map(file => file.path)" /><span>{{ deletableFiles.length > 1000 ? "选择前 1000 个文件" : "全选文件" }}</span></label>
            <button class="danger-button" :disabled="busy || !selectedFilePaths.length" @click="confirmFileDeletion(selectedFilePaths)">批量删除（{{ selectedFilePaths.length }}）</button>
            <span class="help-text">仅删除普通文件，不删除目录；每次最多 1000 个。</span>
          </div>
          <details v-if="fileDeleteResult" :open="fileDeleteResult.outcome !== 'success'" class="preview">
            <summary>删除结果：成功 {{ fileDeleteResult.deleted_count }} 个，失败 {{ fileDeleteResult.files.length - fileDeleteResult.deleted_count }} 个</summary>
            <p v-for="file in fileDeleteResult.files" :key="file.path" :class="{ danger: file.status === 'failed' }"><code>{{ file.path }}</code>：{{ file.status === "success" ? "已删除" : file.error }}</p>
          </details>
          <div class="table-wrap">
            <table><thead><tr><th v-if="isAdmin" class="selection-cell">选择</th><th>名称</th><th>类型</th><th>大小</th><th v-if="isAdmin">操作</th></tr></thead>
              <tbody><tr v-for="file in entries" :key="file.path" :class="{ selected: selected?.path === file.path }">
                <td v-if="isAdmin" class="selection-cell"><input v-if="!file.directory && !file.blocked" v-model="selectedFilePaths" type="checkbox" :value="file.path" :disabled="busy || (selectedFilePaths.length >= 1000 && !selectedFilePaths.includes(file.path))" :aria-label="`选择文件 ${file.name}`" /></td>
                <td><button class="link" :disabled="busy || file.blocked" @click="file.directory ? run(() => enter(file.path)) : choose(file)">{{ file.name }}</button></td>
                <td>{{ file.blocked ? "链接（禁止操作）" : file.directory ? "目录" : "文件" }}</td>
                <td>{{ file.directory ? "—" : (file.size / 1024 / 1024).toFixed(2) + " MB" }}</td>
                <td v-if="isAdmin"><button v-if="!file.directory && !file.blocked" class="danger-button" :disabled="busy" :aria-label="`删除文件 ${file.name}`" @click="confirmFileDeletion([file.path])">删除</button></td>
              </tr><tr v-if="!entries.length"><td :colspan="isAdmin ? 5 : 3">目录为空</td></tr></tbody>
            </table>
          </div>
          <div v-if="selected && isAdmin" class="action-box">
            <h3>已选择：{{ selected.name }}</h3>
            <div class="toolbar">
              <select v-model="action" :disabled="busy" aria-label="文件操作" @change="clearPlan">
                <option value="rename">重命名</option><option value="move">移动</option>
              </select>
              <select v-if="action === 'move'" v-model.number="destinationRoot" :disabled="busy" aria-label="目标目录" @change="clearPlan">
                <option v-for="item in config.roots" :key="item.id" :value="item.id" :title="item.path">{{ rootLabel(item.path) }}</option>
              </select>
              <input v-model="destination" :disabled="busy" :placeholder="action === 'rename' ? '新文件名（含扩展名）' : '目标相对路径（含文件名，父目录需存在）'" @input="clearPlan" />
              <button class="primary" :disabled="busy" @click="preview">预览操作</button>
            </div>
            <p v-if="!isAdmin">当前为只读账号，可浏览目录，不能执行文件修改。</p>
            <div v-if="plan" class="preview">
              <p>来源：<code>{{ plan.source }}</code></p>
              <p v-if="plan.destination">目标：<code>{{ plan.destination }}</code></p>
              <button class="primary" :disabled="busy" @click="execute">{{ plannedRequest?.action === "move" ? "确认移动" : "确认重命名" }}</button>
            </div>
          </div>
        </template>
        <dialog ref="fileDeleteDialog" class="folder-picker" aria-labelledby="file-delete-title" @cancel="busy && $event.preventDefault()">
          <h2 id="file-delete-title">永久删除 {{ fileDeletePreview?.files.length ?? 0 }} 个文件</h2>
          <div class="folder-picker-body">
            <p class="danger">不会进入回收站，无法撤销。请核对以下文件：</p>
            <ul class="delete-file-list"><li v-for="file in fileDeletePreview?.files" :key="file.source"><code>{{ file.source }}</code> · {{ (file.size / 1024 / 1024).toFixed(2) }} MB</li></ul>
            <p v-if="fileDeleteError" class="message error" role="alert">{{ fileDeleteError }}</p>
          </div>
          <div class="config-actions">
            <button :disabled="busy" @click="fileDeleteDialog?.close(); fileDeleteRequest = null">{{ fileDeleteError ? "关闭并核对" : "取消" }}</button>
            <button class="destructive" :disabled="busy || !fileDeleteRequest" @click="deleteFiles">确认永久删除</button>
          </div>
        </dialog>
      </section>
      <section v-if="tab === 'migration'" class="panel">
        <h2>从下载目录迁移文件</h2>
        <p v-if="!isAdmin">只有管理员可以浏览下载目录和迁移文件。</p>
        <template v-else>
          <p class="help-text">选择的是运行服务这台电脑上的目录，无需加入媒体目录配置。请先确保下载已完成；仅移动勾选的直属文件，不递归移动文件夹。</p>
          <div class="toolbar">
            <button :disabled="busy" @click="openMediaFolderDialog('migration')">选择下载目录</button>
            <button :disabled="busy || !migrationSource" @click="run(() => loadMigrationFiles())">刷新下载文件</button>
          </div>
          <p class="path-caption">下载目录：<code>{{ migrationSource || "尚未选择" }}</code></p>
          <p v-if="!config.roots.length" class="message">请先在<a href="#configuration/roots">配置管理</a>中添加目标媒体目录。</p>
          <div class="toolbar">
            <label class="directory-field">目标媒体目录<select v-model.number="migrationTarget" :disabled="busy || !config.roots.length" aria-label="迁移目标媒体目录">
              <option :value="-1" disabled>请选择目标媒体目录</option>
              <option v-for="item in config.roots" :key="item.id" :value="item.id" :title="item.path">{{ rootLabel(item.path) }}</option>
            </select></label>
            <button class="primary" :disabled="busy || !migrationSelected.length || migrationTarget < 0" @click="confirmMigration">迁移所选文件（{{ migrationSelected.length }}）</button>
          </div>
          <p v-if="migrationTarget >= 0" class="path-caption">目标目录：<code>{{ rootPath(migrationTarget) }}</code></p>
          <div class="row-actions selection-actions">
            <label class="check-control"><input type="checkbox" :checked="!!migrationSelectable.length && migrationSelectable.slice(0, 1000).every(file => migrationSelected.includes(file.path))" :disabled="busy || !migrationSelectable.length" @change="migrationSelected = migrationSelectable.slice(0, 1000).every(file => migrationSelected.includes(file.path)) ? [] : migrationSelectable.slice(0, 1000).map(file => file.path)" /><span>{{ migrationSelectable.length > 1000 ? "选择前 1000 个文件" : "全选下载文件" }}</span></label>
            <span class="help-text">同名文件跳过、不覆盖；成功迁移后源文件将移走，每次最多 1000 个。</span>
          </div>
          <div class="table-wrap"><table><thead><tr><th class="selection-cell">选择</th><th>下载文件</th><th>类型</th><th>大小</th></tr></thead><tbody>
            <tr v-for="file in migrationEntries" :key="file.path">
              <td class="selection-cell"><input v-if="!file.directory && !file.blocked" v-model="migrationSelected" type="checkbox" :value="file.path" :disabled="busy || (migrationSelected.length >= 1000 && !migrationSelected.includes(file.path))" :aria-label="`选择下载文件 ${file.name}`" /></td>
              <td>{{ file.name }}</td><td>{{ file.blocked ? "链接（禁止操作）" : file.directory ? "目录（不迁移）" : "文件" }}</td>
              <td>{{ file.directory ? "—" : (file.size / 1024 / 1024).toFixed(2) + " MB" }}</td>
            </tr>
            <tr v-if="!migrationEntries.length"><td colspan="4">{{ migrationSource ? "下载目录为空" : "请先选择下载目录" }}</td></tr>
          </tbody></table></div>
          <div v-if="migrationResult" class="preview">
            <h3>迁移结果：成功 {{ migrationResult.moved_count }} 个，跳过 {{ migrationResult.skipped_count }} 个，失败 {{ migrationResult.failed_count }} 个</h3>
            <div class="table-wrap"><table><thead><tr><th>文件 / 目标路径</th><th>结果</th><th>说明</th></tr></thead><tbody>
              <tr v-for="file in migrationResult.files" :key="file.path">
                <td>{{ file.path }}<p class="path-caption"><code>{{ file.destination }}</code></p></td>
                <td>{{ file.status === "success" ? "已迁移" : file.status === "skipped" ? "已跳过" : "失败" }}</td><td>{{ file.error || "源文件已移走" }}</td>
              </tr>
            </tbody></table></div>
          </div>
          <dialog ref="migrationDialog" class="folder-picker" aria-labelledby="migration-title" @cancel="busy && $event.preventDefault()">
            <h2 id="migration-title">确认文件迁移</h2>
            <div class="folder-picker-body">
              <p>待迁移 {{ migrationPreview?.ready_count ?? 0 }} 个，同名跳过 {{ migrationPreview?.skipped_count ?? 0 }} 个。保留文件名及扩展名，不覆盖目标文件。</p>
              <p>源目录：<code>{{ migrationRequest?.source_folder || migrationSource }}</code></p>
              <p>目标目录：<code>{{ rootPath(migrationTarget) }}</code></p>
              <ul class="delete-file-list"><li v-for="file in migrationPreview?.files" :key="file.path"><code>{{ file.path }}</code>：{{ file.status === "skipped" ? file.error : "待迁移" }}</li></ul>
              <p v-if="busy" role="status">正在提交后台任务，请勿重复提交。跨磁盘大文件可能耗时较长，可在任务中心查看进度。</p>
              <p v-if="!migrationPreview?.ready_count" class="warning">没有可迁移的文件，请调整选择或目标目录。</p>
              <p v-if="migrationError" class="message error" role="alert">{{ migrationError }}</p>
            </div>
            <div class="config-actions">
              <button :disabled="busy" @click="migrationDialog?.close(); migrationRequest = null">{{ migrationError ? "关闭并核对" : "取消" }}</button>
              <button class="primary" :disabled="busy || !migrationRequest || !migrationPreview?.ready_count" @click="migrateFiles">确认迁移</button>
            </div>
          </dialog>
        </template>
      </section>
      <section v-if="tab === 'renaming' || (tab === 'configuration' && isAdmin && configurationSection === 'renaming')" class="panel">
        <h2>{{ tab === "configuration" ? "命名规则" : "按集号整理文件名" }}</h2>
        <p v-if="tab === 'renaming'"><a href="#configuration/renaming">管理命名规则</a></p>
        <ol v-if="tab === 'renaming'" class="workflow" aria-label="重命名流程"><li :class="{ current: renameRoot < 0 }">选择目录</li><li :class="{ current: renameRoot >= 0 && !renameResult }">核对预览</li><li :class="{ current: !!renameResult }">确认重命名</li></ol>
        <p v-if="!isAdmin">只读账号不能维护规则或执行重命名。</p>
        <template v-else>
          <details v-if="tab === 'configuration'" open>
            <summary>规则列表{{ renameRulesDirty ? " · 有未保存修改" : "" }}</summary>
            <div class="config-section-heading">
              <p class="help-text">文件名固定为 01、02…，保留扩展名。新增、删除立即生效；编辑已有规则及排序仍需保存，至少保留一条规则。</p>
              <button :disabled="busy || renameRules.length >= 32" @click="addRenameRule">添加规则</button>
            </div>
            <div class="row-actions selection-actions">
              <label class="check-control"><input type="checkbox" :checked="!!renameRules.length && selectedRuleIds.length === renameRules.length" :disabled="busy || !renameRules.length" @change="selectedRuleIds = selectedRuleIds.length === renameRules.length ? [] : renameRules.map(rule => rule.id)" /><span>全选规则</span></label>
              <button class="danger-button" :disabled="busy || !selectedRuleIds.length || selectedRuleIds.length >= renameRules.length" @click="deleteRenameRules(selectedRuleIds)">批量删除（{{ selectedRuleIds.length }}）</button>
            </div>
            <div v-for="(rule, index) in renameRules" :key="rule.id" class="config-card">
              <div class="config-card-heading">
                <div class="row-actions">
                  <label class="check-control"><input v-model="selectedRuleIds" type="checkbox" :value="rule.id" :disabled="busy" :aria-label="`选择规则 ${index + 1}`" /><strong>规则 {{ index + 1 }}</strong></label>
                  <label class="check-control"><input v-model="rule.enabled" type="checkbox" :disabled="busy" :aria-label="`启用规则 ${index + 1}`" /><span>启用</span></label>
                </div>
                <div class="row-actions">
                  <button :disabled="busy || index === 0" :aria-label="`上移规则 ${index + 1}`" @click="moveRenameRule(index, -1)">上移</button>
                  <button :disabled="busy || index === renameRules.length - 1" :aria-label="`下移规则 ${index + 1}`" @click="moveRenameRule(index, 1)">下移</button>
                  <button class="danger-button config-delete" :disabled="busy || renameRules.length === 1" :aria-label="`删除规则 ${index + 1}`" @click="deleteRenameRules([rule.id])">删除</button>
                </div>
              </div>
              <div class="config-fields">
                <label class="config-field">规则名称<input v-model="rule.name" :disabled="busy" maxlength="80" /></label>
                <label class="config-field config-field-wide">正则表达式<textarea v-model="rule.pattern" class="code-input" :disabled="busy" rows="3" maxlength="2000"></textarea><span class="help-text">使用命名捕获组 (?P&lt;episode&gt;...) 提取集号。</span></label>
              </div>
            </div>
            <div class="config-actions">
              <span class="help-text" role="status">{{ renameRulesDirty ? "有未保存的修改" : "规则修改后需保存生效" }}</span>
              <button :disabled="busy" @click="run(loadRenameRules)">重新加载</button>
              <button class="primary" :disabled="busy || !renameRulesDirty" @click="saveRenameRules">保存编辑</button>
            </div>
          </details>
          <dialog ref="newRuleDialog" class="folder-picker" aria-labelledby="new-rule-title" @cancel="busy && $event.preventDefault()">
            <h2 id="new-rule-title">新增命名规则</h2>
            <form class="dialog-form" @submit.prevent="createRenameRule">
              <div class="folder-picker-body config-fields">
                <label class="config-field">规则名称<input v-model="newRule.name" required maxlength="80" :disabled="busy" /></label>
                <label class="config-field config-field-wide">正则表达式<textarea v-model="newRule.pattern" class="code-input" required rows="3" maxlength="2000" :disabled="busy"></textarea><span class="help-text">使用 (?P&lt;episode&gt;...) 提取集号。文件名固定为 01、02…，保留扩展名。</span></label>
                <label class="check-control"><input v-model="newRule.enabled" type="checkbox" :disabled="busy" /><span>启用</span></label>
                <p v-if="error" class="message error config-field-wide" role="alert">{{ error }}</p>
              </div>
              <div class="config-actions">
                <button type="button" :disabled="busy" @click="newRuleDialog?.close()">取消</button>
                <button class="primary" :disabled="busy">新增并生效</button>
              </div>
            </form>
          </dialog>
          <template v-if="tab === 'renaming'">
          <p v-if="!config.roots.length" class="message">请先在“配置管理”中添加媒体目录。<a href="#configuration">前往配置管理</a></p>
          <p v-if="renameRulesDirty" class="warning">命名规则有未保存的修改，请先到<a href="#configuration/renaming">命名规则</a>保存。</p>
          <div class="toolbar">
            <label class="directory-field">媒体目录<select v-model.number="renameRoot" :disabled="busy || !config.roots.length" @change="selectRenameRoot">
              <option :value="-1" disabled>请选择媒体目录</option>
              <option v-for="item in config.roots" :key="item.id" :value="item.id" :title="item.path">{{ rootLabel(item.path) }}</option>
            </select></label>
          </div>
          <p v-if="renameRoot >= 0" class="path-caption"><code>{{ rootPath(renameRoot) }}</code></p>
          <p v-if="renameDirectory">当前处理文件夹：<code>{{ renameDirectory }}</code>
            <button :disabled="busy" @click="selectRenameRoot">返回所选媒体目录</button>
          </p>
          <div class="toolbar">
            <button :class="{ primary: !renameReport }" :disabled="busy || renameRoot < 0 || renameRulesDirty" @click="previewRenaming">重新预览</button>
            <label class="check-control"><input v-model="renameForce" type="checkbox" :disabled="busy" /><span>本目录强制检查</span></label>
          </div>
          <p v-if="renameRoot < 0" class="empty-state">请选择媒体目录</p>
          <div v-if="renameReport" class="preview">
            <p v-if="renameReport.unchanged" class="message">{{ renameReport.reason }} · 上次检查 {{ renameReport.checked_at }}</p>
            <template v-else>
              <p>待修改 {{ renameReport.ready_count }} 个视频 · 共 {{ renameReport.file_count }} 个文件</p>
              <div class="table-wrap"><table><thead><tr><th>原文件名 / 命中规则</th><th>目标文件名（可修改）</th><th>状态 / 校验结果</th></tr></thead><tbody>
                <tr v-for="row in renameReport.rows" :key="row.source">
                  <td><code>{{ row.name }}</code><p>{{ row.rule || "未匹配" }}<span v-if="row.episode"> · 集数 {{ row.episode }}</span></p></td>
                  <td><input v-model="row.draft_name" :disabled="busy || !row.editable" maxlength="255" :aria-label="`${row.name} 的目标名`" />
                    <details v-if="row.files.length"><summary>相关文件 {{ row.files.length }} 项</summary><p v-for="file in row.files" :key="file.source"><code>{{ file.source }} → {{ file.target }}</code></p></details>
                  </td>
                  <td>{{ renameStatus[row.status] || row.status }}<p>{{ row.reason }}</p><p v-for="note in row.notes" :key="note">{{ note }}</p></td>
                </tr>
                <tr v-if="!renameReport.rows.length"><td colspan="3">当前文件夹没有可处理的视频。</td></tr>
              </tbody></table></div>
              <p v-if="renameEditsPending" class="warning">目标名有修改，必须重新校验并核对新预览后才能执行。</p>
              <div class="toolbar execution-actions">
                <button :disabled="busy || !renameEditsPending" @click="reviewRenaming">校验修改项</button>
                <button class="primary" :disabled="busy || renameEditsPending || !renameReport.ready_count || renameRulesDirty" @click="applyRenaming">确认重命名 {{ renameReport.ready_count }} 个视频</button>
                <span v-if="!renameReport.ready_count" class="help-text">无可执行项</span>
              </div>
            </template>
          </div>
          <div v-if="renameResult" class="preview">
            <h3>执行结果：{{ outcomeNames[renameResult.outcome] }}</h3>
            <p v-for="row in renameResult.rows" :key="row.source">
              <code>{{ row.name }} → {{ row.new_name }}</code> · {{ renameStatus[row.status] || row.status }} {{ row.reason }}
              <span v-for="failure in row.rollback_errors" :key="failure" class="danger">回滚失败：{{ failure }}</span>
            </p>
          </div>
          </template>
        </template>
      </section>
      <section v-if="tab === 'configuration' && isAdmin && configurationSection === 'system'" class="panel">
        <h2>系统配置概览 <span class="status-chip">只读</span></h2>
        <p class="help-text">Hosts 与日志设置来自服务端 .env，修改后需重启服务生效。</p>
        <dl><dt>Hosts 目标</dt><dd><code>{{ config.hosts_path }}</code></dd>
          <dt>默认获取方式</dt><dd>{{ sourceNames[config.hosts_source] }}</dd>
          <dt>查询源顺序</dt><dd>{{ config.dns_providers.join(" → ") }}</dd>
          <dt>DNS / 地址族</dt><dd>{{ config.dns_servers.join(", ") }} / {{ config.ip_family }}</dd>
          <dt>域名范围</dt><dd>{{ config.hosts_domains.length }} 个；GitHub {{ config.hosts_include_github ? "已开启" : "已关闭" }}</dd>
          <dt>Hosts 定时更新</dt><dd>{{ config.hosts_interval_seconds ? config.hosts_interval_seconds + " 秒一次" : "已关闭" }}</dd>
          <dt>日志文件</dt><dd>{{ config.log_path }}</dd>
          <dt>日志保留</dt><dd>{{ config.log_retention_days }} 天 / 总量 {{ config.log_max_total_mb }} MiB / 单文件 {{ config.log_max_file_mb }} MiB</dd>
        </dl>
      </section>
      <section v-if="tab === 'configuration' && isAdmin && configurationSection === 'system'" class="panel">
        <h2>自动视频识别 <span class="status-chip">只读</span></h2>
        <dl><dt>ffprobe</dt><dd>{{ videoInfoStatus?.available ? "已就绪" : "未安装" }}</dd>
          <dt>扫描周期</dt><dd>10 分钟</dd><dt>扫描范围</dt><dd>媒体目录直属视频</dd>
          <dt>支持格式</dt><dd>{{ videoInfoStatus?.extensions.join("、") }}</dd>
        </dl>
      </section>
      <section v-if="tab === 'video-info'" class="panel">
        <h2>视频列表 <span class="status-chip">后台自动识别</span></h2>
        <p v-if="videoInfoStatus && !videoInfoStatus.available" class="message error">未找到 ffprobe，无法识别视频。</p>
        <p v-if="videoInfoStatus?.running" class="message">后台正在识别：{{ videoInfoStatus.current }}</p>
        <p v-if="videoInfoStatus?.error" class="message error">{{ videoInfoStatus.error }}</p>
        <p v-if="videoStatusError" class="message error">{{ videoStatusError }}</p>
        <p v-if="videoInfoStatus?.finished_at" class="scan-summary">最近扫描 {{ new Date(videoInfoStatus.finished_at).toLocaleString() }} · 已识别 {{ videoInfoStatus.summary.analyzed ?? 0 }} / 缓存 {{ videoInfoStatus.summary.cached ?? 0 }} / 暂缓 {{ videoInfoStatus.summary.deferred ?? 0 }} / 失败 {{ videoInfoStatus.summary.failed ?? 0 }}</p>
        <div class="toolbar video-info-toolbar">
          <label class="directory-field">媒体目录<select v-model.number="videoInfoRoot" :disabled="busy || videoInfoLoading" aria-label="视频参数媒体目录">
            <option :value="-1" disabled>请选择媒体目录</option>
            <option v-for="item in config.roots" :key="item.id" :value="item.id" :title="item.path">{{ rootLabel(item.path) }}</option>
          </select></label>
          <label>排序<select v-model="videoSort"><option value="bitrate">总码率从高到低</option><option value="name">文件名</option></select></label>
          <button :disabled="busy || videoInfoLoading || videoInfoRoot < 0" @click="refreshVideoRows">刷新列表</button>
          <details v-if="isAdmin" class="more-actions"><summary>更多操作</summary><div class="more-actions-content">
            <button :disabled="busy || videoInfoLoading || videoInfoRoot < 0 || !videoInfoStatus?.available || videoInfoStatus?.running" @click="inspectVideos()">扫描缺失信息</button>
            <button :disabled="busy || videoInfoLoading || videoInfoRoot < 0 || !videoInfoStatus?.available || videoInfoStatus?.running" @click="inspectVideos(true)">重新识别本目录</button>
            <button :disabled="busy || !videoInfoStatus?.available || videoInfoStatus?.running" @click="inspectAllVideos">扫描全部目录</button>
            <small v-if="videoInfoStatus?.running">后台扫描中，请等待当前任务完成。</small>
            <small v-else-if="!videoInfoStatus?.available">需要先安装 ffprobe。</small>
          </div></details>
        </div>
        <p v-if="videoInfoRoot >= 0" class="path-caption"><code>{{ rootPath(videoInfoRoot) }}</code></p>
        <p v-if="videoInfoLoading" role="status">正在读取结果…</p>
        <p v-if="videoInfoError" class="message error">{{ videoInfoError }}</p>
        <div class="table-wrap"><table class="video-table"><thead><tr><th>视频</th><th>编码 / 画面</th><th>总码率 / HDR</th><th>风险提示</th><th>操作</th></tr></thead><tbody>
          <tr v-for="row in sortedVideoRows" :key="row.path">
            <td><strong class="file-name">{{ row.name }}</strong><p><span class="status-chip" :class="{ 'status-warning': row.status === 'changed', 'status-error': row.status === 'failed' || row.status === 'blocked' }">{{ videoStateNames[row.status] || row.status }}</span></p><p v-if="row.error" class="message error">{{ row.error }}</p></td>
            <td><template v-if="row.details"><p v-for="(video, index) in row.details.videos" :key="index"><strong>{{ video.codec }}</strong> · {{ video.bit_depth == null ? "位深未知" : video.bit_depth + "-bit" }}<br />{{ video.width }} × {{ video.height }}<br /><small>{{ video.fps == null ? "帧率未知" : video.fps.toFixed(2) + " fps" }}</small></p></template><span v-else>待识别</span></td>
            <td v-if="row.details"><strong>{{ bitrate(row.details.bit_rate ?? row.details.estimated_bit_rate) }}</strong><small v-if="row.details.bit_rate == null && row.details.estimated_bit_rate != null">（估算）</small>
              <p v-for="(video, index) in row.details.videos" :key="index">{{ video.hdr }}</p>
            </td><td v-else>—</td>
            <td v-if="row.details"><span class="status-chip" :class="{ 'status-warning': row.details.risks.length > 0 }">{{ row.details.risks.length ? row.details.risks.length + " 项" : "无" }}</span></td><td v-else>—</td>
            <td v-if="row.details"><div class="row-actions">
              <button @click="showVideoDetails(row)">查看详情</button>
              <button :disabled="row.status !== 'saved'" @click="compareVideo(row)">加入对比</button>
            </div></td><td v-else>等待识别</td>
          </tr><tr v-if="!videoInfoRows.length && !videoInfoLoading && !videoInfoError"><td colspan="5" class="empty-state">{{ videoInfoRoot < 0 ? "请选择媒体目录" : "暂无视频" }}</td></tr>
        </tbody></table></div>
        <dialog ref="videoDetailsDialog" class="details-drawer" aria-labelledby="video-details-title" @close="videoDetails = null">
          <template v-if="videoDetails?.details">
            <div class="drawer-header"><div><span class="eyebrow">只读技术参数</span><h2 id="video-details-title">视频详情</h2></div><button autofocus @click="videoDetailsDialog?.close()">关闭详情</button></div>
            <h3 class="file-name">{{ videoDetails.name }}</h3>
            <p class="help-text">识别时间：{{ videoDetails.scanned_at ? new Date(videoDetails.scanned_at).toLocaleString() : "未知" }}</p>
            <p v-if="videoDetails.status !== 'saved'" class="message warning">历史记录 · 待更新</p>
            <section v-for="(video, index) in videoDetails.details.videos" :key="index" class="detail-block">
              <h3>视频轨 {{ index + 1 }}</h3><dl>
                <dt>编码</dt><dd>{{ video.codec }} / {{ video.profile || "Profile 未知" }} / Level {{ video.level ?? "未知" }}</dd>
                <dt>画面</dt><dd>{{ video.width }} × {{ video.height }} / {{ video.fps == null ? "帧率未知" : video.fps.toFixed(3) + " fps" }}</dd>
                <dt>像素 / 位深</dt><dd>{{ video.pixel_format || "未知" }} / {{ video.bit_depth ?? "未知" }}-bit</dd>
                <dt>视频码率</dt><dd>{{ bitrate(video.bit_rate) }}</dd><dt>HDR</dt><dd>{{ video.hdr }}</dd>
              </dl>
            </section>
            <section class="detail-block"><h3>文件与总码率</h3><dl>
              <dt>容器</dt><dd>{{ videoDetails.details.container || "未知" }}</dd>
              <dt>大小</dt><dd>{{ (videoDetails.details.size_bytes / 1024 / 1024).toFixed(2) }} MiB</dd>
              <dt>时长</dt><dd>{{ videoDetails.details.duration_seconds == null ? "未知" : videoDetails.details.duration_seconds.toFixed(1) + " 秒" }}</dd>
              <dt>总码率</dt><dd>{{ bitrate(videoDetails.details.bit_rate ?? videoDetails.details.estimated_bit_rate) }}{{ videoDetails.details.bit_rate == null && videoDetails.details.estimated_bit_rate != null ? "（按大小 / 时长估算）" : "" }}</dd>
            </dl></section>
            <section class="detail-block"><h3>音轨与字幕</h3>
              <p v-for="(audio, index) in videoDetails.details.audios" :key="index">{{ audio.codec || "未知编码" }} / {{ audio.channels ?? "未知" }} 声道 / {{ audio.language || "语言未标记" }}</p>
              <p v-if="!videoDetails.details.audios.length">无音轨</p>
              <p>字幕：{{ videoDetails.details.subtitles.map(item => `${item.codec || "未知"} (${item.language || "语言未标记"})`).join("、") || "无内嵌字幕" }}</p>
            </section>
            <section class="detail-block"><h3>播放风险提示</h3>
              <p v-for="risk in videoDetails.details.risks" :key="risk" class="message warning">{{ risk }}</p>
              <p v-if="!videoDetails.details.risks.length">无</p>
            </section>
            <details class="help-details"><summary>完整技术参数（JSON）</summary><pre>{{ JSON.stringify(videoDetails.details, null, 2) }}</pre></details>
          </template>
        </dialog>
        <div v-if="videoComparisons.length" class="preview">
          <h3>跨目录视频对比（最多 4 个）</h3>
          <div class="table-wrap"><table class="comparison-table"><thead><tr><th>项目</th><th v-for="item in videoComparisons" :key="item.key">{{ item.label }} <button @click="videoComparisons = videoComparisons.filter(other => other.key !== item.key)">移除</button></th></tr></thead><tbody>
            <tr><td>总码率</td><td v-for="item in videoComparisons" :key="item.key">{{ bitrate(item.details.bit_rate ?? item.details.estimated_bit_rate) }}{{ item.details.bit_rate == null ? "（估算或未知）" : "" }}</td></tr>
            <tr><td>视频轨</td><td v-for="item in videoComparisons" :key="item.key"><p v-for="(video, index) in item.details.videos" :key="index">{{ video.codec }} / {{ video.profile }}<br />{{ video.width }} × {{ video.height }} / {{ video.fps == null ? "未知" : video.fps.toFixed(3) }} fps<br />{{ video.bit_depth ?? "未知" }}-bit / {{ video.hdr }}</p></td></tr>
            <tr><td>音轨 / 字幕</td><td v-for="item in videoComparisons" :key="item.key">{{ item.details.audios.map(audio => `${audio.codec} ${audio.channels ?? "?"}ch`).join("、") || "无" }}<p>{{ item.details.subtitles.map(subtitle => subtitle.codec).join("、") || "无内嵌字幕" }}</p></td></tr>
            <tr><td>风险提示</td><td v-for="item in videoComparisons" :key="item.key"><p v-for="risk in item.details.risks" :key="risk">{{ risk }}</p><span v-if="!item.details.risks.length">暂无已知参数风险</span></td></tr>
          </tbody></table></div>
        </div>
      </section>
      <section v-if="tab === 'hosts'" class="panel">
        <h2>Hosts 数据获取</h2>
        <div class="toolbar">
          <label class="directory-field" for="hosts-source">本次获取方式
          <select id="hosts-source" v-model="hostSource" :disabled="busy">
            <option value="dns">{{ sourceNames.dns }}</option>
            <option value="checktmdb">{{ sourceNames.checktmdb }}</option>
          </select></label>
        </div>
        <p v-if="hostSource === 'checktmdb'"><a :href="config.hosts_subscription_urls.A" target="_blank" rel="noopener noreferrer">IPv4 订阅</a> · <a :href="config.hosts_subscription_urls.AAAA" target="_blank" rel="noopener noreferrer">IPv6 订阅</a></p>
        <p>目标：<code>{{ config.hosts_path }}</code> · <a v-if="isAdmin" href="#configuration/system">配置</a></p>
        <div class="toolbar">
          <button class="primary" :disabled="busy" @click="hosts(true)">获取预览</button>
          <button v-if="isAdmin" :disabled="busy" @click="hosts(false)">获取并更新文件</button>
          <button @click="tab = 'logs'">查看运行日志</button>
        </div>
        <p v-if="hostRunning" role="status">正在获取…</p>
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
          </div>
          <h3>本次执行日志</h3>
          <div class="run-log" role="log"><div v-for="(line, index) in hostReport.logs" :key="index" :class="'log-' + line.level"><time>{{ new Date(line.time).toLocaleTimeString() }}</time> [{{ line.level }}] {{ line.domain ? `[${line.domain}] ` : "" }}{{ line.message }}</div></div>
          <details><summary>获取 / 测速诊断</summary><pre>{{ JSON.stringify(hostReport.diagnostics, null, 2) }}</pre></details>
        </template>
      </section>
      <section v-if="tab === 'logs'" class="panel">
        <h2>运行日志</h2>
        <p>路径：<code>{{ config.log_path }}</code></p>
        <div class="toolbar">
          <button :disabled="logLoading" @click="readLogs">读取最新日志</button>
          <label class="check-control"><input v-model="followLog" type="checkbox" /><span>自动追踪日志</span></label>
          <label>显示范围<select v-model.number="logLines"><option :value="100">最近 100 行</option><option :value="200">最近 200 行</option><option :value="1000">最近 1000 行</option></select></label>
        </div>
        <p v-if="logError" class="message error" role="alert">{{ logError }}</p>
        <p v-if="runtimeLog"><small>文件更新时间：{{ new Date(runtimeLog.updated_at).toLocaleString() }}{{ runtimeLog.truncated ? " · 仅展示最新片段" : "" }}</small></p>
        <pre ref="logPanel" class="runtime-log" role="log" aria-label="最新运行日志">{{ runtimeLog?.lines.join("\n") || (logLoading ? "正在读取日志…" : "暂无日志") }}</pre>
      </section>
      <section v-if="tab === 'operations'" class="panel">
        <h2>最近 100 条操作</h2>
        <button :disabled="busy" @click="showRecords">刷新记录</button>
        <div class="table-wrap"><table><thead><tr><th>时间</th><th>操作人 / 操作</th><th>状态</th><th>详情</th></tr></thead>
          <tbody><tr v-for="record in records" :key="record.id">
            <td>{{ new Date(record.created_at).toLocaleString() }}</td><td>{{ record.actor }} / {{ record.kind }}</td>
            <td><span class="status-chip" :class="{ 'status-error': record.status === 'failed', 'status-warning': record.status !== 'success' && record.status !== 'failed' }">{{ ({ success: "成功", failed: "失败", running: "执行中 / 待核对", partial: "部分成功" } as Record<string, string>)[record.status] || record.status }}</span></td>
            <td><details :key="`${record.id}-${recordsVersion}`" @toggle="toggleOperation(record.id, $event)">
              <summary>{{ record.error || record.id }}</summary>
              <p v-if="operationDetails[record.id]?.loading" role="status">正在加载详情…</p>
              <div v-else-if="operationDetails[record.id]?.error" class="message error" role="alert">{{ operationDetails[record.id]?.error }} <button @click="loadOperation(record.id)">重试</button></div>
              <pre v-else-if="operationDetails[record.id]?.data">{{ JSON.stringify({ request: operationDetails[record.id]?.data?.request, result: operationDetails[record.id]?.data?.result }, null, 2) }}</pre>
            </details></td>
          </tr><tr v-if="!records.length"><td colspan="4">暂无操作记录</td></tr></tbody>
        </table></div>
      </section>
      <section v-if="tab === 'users' && isAdmin" class="panel">
        <h2>用户列表</h2>
        <form class="toolbar" @submit.prevent="createUser">
          <label class="form-field">用户名<input v-model="newUser.username" autocomplete="off" placeholder="3–64 位英文、数字、_.-" required maxlength="64" :disabled="busy" /></label>
          <label class="form-field">初始密码<input v-model="newUser.password" type="password" autocomplete="new-password" placeholder="4–128 位" required minlength="4" maxlength="128" :disabled="busy" /></label>
          <label>账号权限<select v-model="newUser.role" :disabled="busy"><option value="viewer">只读用户</option><option value="admin">管理员</option></select></label>
          <button class="primary" :disabled="busy">创建用户</button>
        </form>
        <div class="table-wrap"><table><thead><tr><th>用户名</th><th>角色</th><th>状态</th><th>操作</th></tr></thead>
          <tbody><tr v-for="item in users" :key="item.id"><td>{{ item.username }}</td><td>{{ item.role === "admin" ? "管理员" : "只读" }}</td><td>{{ item.active ? "启用" : "停用" }}</td>
            <td><button :disabled="busy" @click="userEdit = { ...item }; resetPassword = ''">编辑</button></td></tr></tbody>
        </table></div>
        <form v-if="userEdit" class="action-box" @submit.prevent="saveUser">
          <h3>编辑 {{ userEdit.username }}</h3>
          <div class="toolbar">
            <label>账号权限<select v-model="userEdit.role" :disabled="busy"><option value="viewer">只读用户</option><option value="admin">管理员</option></select></label>
            <label class="check-control"><input v-model="userEdit.active" type="checkbox" :disabled="busy" /><span>启用账号：{{ userEdit.username }}</span></label>
            <label class="form-field">重置密码<input v-model="resetPassword" type="password" autocomplete="new-password" placeholder="4–128 位，留空不变" minlength="4" maxlength="128" :disabled="busy" /></label>
            <button class="primary" :disabled="busy">保存并注销旧会话</button><button type="button" :disabled="busy" @click="userEdit = null">取消</button>
          </div>
        </form>
      </section>
      <section v-if="tab === 'settings'" class="panel">
        <h2>系统信息</h2>
        <dl><dt>后端 / 存储</dt><dd>Python FastAPI / PostgreSQL，独立 mediaforge schema</dd>
          <dt>前端</dt><dd>Vue 3</dd>
          <dt>媒体目录</dt><dd>{{ config.roots.map(item => item.path).join("、") || "未配置" }}</dd>
          <dt>Hosts 目标</dt><dd>{{ config.hosts_path }}</dd>
          <dt>Hosts 默认来源</dt><dd>{{ sourceNames[config.hosts_source] }}</dd>
          <dt>DNS 查询源</dt><dd>{{ config.dns_providers.join(" → ") }}</dd>
          <dt>日志文件</dt><dd>{{ config.log_path }}</dd>
          <dt>日志保留</dt><dd>{{ config.log_retention_days }} 天 / 总量 {{ config.log_max_total_mb }} MiB / 单文件 {{ config.log_max_file_mb }} MiB</dd></dl>
      </section>
      <section v-if="tab === 'settings'" class="panel">
        <h2>账号安全</h2>
        <p class="help-text">修改密码后将退出登录，请使用新密码重新登录。</p>
        <form class="toolbar" @submit.prevent="changePassword">
          <label class="form-field">当前密码<input v-model="oldPassword" type="password" autocomplete="current-password" required maxlength="128" :disabled="busy" /></label>
          <label class="form-field">新密码<input v-model="newPassword" type="password" autocomplete="new-password" placeholder="4–128 位" required minlength="4" maxlength="128" :disabled="busy" /></label>
          <button class="primary" :disabled="busy">修改密码并退出登录</button>
        </form>
      </section>
      <section v-if="tab === 'metadata' || (tab === 'configuration' && isAdmin && (configurationSection === 'scrapers' || configurationSection === 'mappings'))" class="panel">
        <h2>{{ tab === "configuration" ? configurationSection === "scrapers" ? "刮削器" : "影视匹配" : "匹配影视信息" }}</h2>
        <ol v-if="tab === 'metadata'" class="workflow" aria-label="刮削流程"><li :class="{ current: metadataRoot < 0 }">选择目录</li><li :class="{ current: metadataRoot >= 0 && !metadataResult }">核对匹配</li><li :class="{ current: !!metadataResult }">确认写入</li></ol>
        <p v-if="!isAdmin">只读账号不能执行刮削，请使用管理员账号。</p>
        <template v-else>
          <p v-if="!config.roots.length && (tab === 'metadata' || configurationSection !== 'scrapers')" class="message">尚未配置媒体目录。<a href="#configuration/roots">添加媒体目录</a></p>
          <p v-if="tab === 'metadata'" class="help-text">当前来源：{{ executionScrapers.map(item => item.name).join(" → ") || "尚未启用" }} · <a href="#configuration/scrapers">配置刮削器</a> · <a href="#configuration/mappings">管理影视匹配</a></p>
          <div v-if="tab === 'metadata' || configurationSection === 'mappings'" class="toolbar" :class="{ 'config-filter-fields': tab === 'configuration' }">
            <label class="directory-field">媒体目录<select v-model.number="metadataRoot" :disabled="busy || !config.roots.length" @change="selectMetadataRoot">
              <option :value="-1" disabled>请选择媒体目录</option>
              <option v-for="item in config.roots" :key="item.id" :value="item.id" :title="item.path">{{ rootLabel(item.path) }}</option>
            </select></label>
            <label>类型<select v-model="metadataType" :disabled="busy"><option value="tv">电视剧</option><option value="movie">电影</option></select></label>
            <label>语言<select v-model="metadataLanguage" :disabled="busy"><option value="zh-CN">简体中文</option><option value="zh-TW">繁体中文</option><option value="en-US">English</option></select></label>
          </div>
          <p v-if="(tab === 'metadata' || configurationSection === 'mappings') && metadataRoot >= 0" class="path-caption"><code>{{ rootPath(metadataRoot) }}</code></p>
          <section v-if="tab === 'configuration' && configurationSection === 'scrapers'" ref="scraperSettingsPanel">
            <form @submit.prevent="saveScrapers">
              <p class="help-text">按从上到下的优先级使用已启用的刮削器，修改后统一保存。</p>
              <div v-for="(scraper, index) in scraperOrder" :key="scraper.id" class="config-card">
                <div class="config-card-heading">
                  <div class="row-actions">
                    <h3>{{ index + 1 }}. {{ scraper.name }}</h3>
                    <span class="status-chip" :class="{ 'status-warning': !scraper.configured }">{{ scraper.configured ? "Key 已配置" : "Key 未配置" }}</span>
                  </div>
                  <div class="row-actions">
                    <label class="check-control"><input v-model="scraper.enabled" type="checkbox" :disabled="busy" :aria-label="`启用 ${scraper.name}`" /><span>启用</span></label>
                    <button type="button" :disabled="busy || index === 0" :aria-label="`上移 ${scraper.name}`" @click="moveScraper(scraper.id, -1)">上移</button>
                    <button type="button" :disabled="busy || index === scraperOrder.length - 1" :aria-label="`下移 ${scraper.name}`" @click="moveScraper(scraper.id, 1)">下移</button>
                  </div>
                </div>
                <label class="config-field">API Key / Read Access Token
                  <input v-model="scraper.api_key" type="password" autocomplete="off" maxlength="2048" :disabled="busy" :aria-label="`${scraper.name} Key`" />
                  <span class="help-text">{{ scraper.configured ? "已保存的 Key 不回显；留空保留现值，输入新 Key 后保存以替换。" : "填写 Key 后点击下方“保存配置”。" }}{{ scraper.key_source === "environment" ? " 当前来源：环境变量。" : "" }}</span>
                </label>
                <div class="config-sensitive-action">
                  <button type="button" class="danger-button" :disabled="busy || !scraper.configured" @click="clearScraperKey(scraper)">清除 {{ scraper.name }} Key</button>
                  <span class="help-text">确认后立即清除，无需再次保存。</span>
                </div>
              </div>
              <div class="config-actions">
                <span class="help-text">Key 留空保留现值；数据库明文存储。</span>
                <button type="button" :disabled="busy" @click="run(loadScrapers)">重新加载</button>
                <button class="primary" :disabled="busy">保存配置</button>
              </div>
            </form>
            <p v-if="!scraperOrder.some(item => item.enabled)" class="warning">尚未启用刮削器，请勾选并保存。</p>
            <p v-for="scraper in scraperOrder.filter(item => item.enabled && !item.configured)" :key="scraper.id" class="warning">{{ scraper.name }} 尚未配置 Key，请填写并保存后执行搜索或刮削。</p>
          </section>
          <template v-if="tab === 'configuration' && configurationSection === 'mappings'">
          <div ref="metadataSearchPanel" class="config-card">
            <h3>搜索影视</h3>
            <form class="config-inline-form" @submit.prevent="searchMetadata()">
              <label class="config-field">剧名 / 电影名<input v-model="metadataSearchName" maxlength="255" required :disabled="busy" aria-label="TMDB 搜索名称" @input="metadataSearchIsFolder = false" /></label>
              <button :disabled="busy || !metadataSearchName.trim()">搜索影视</button>
            </form>
            <p v-if="metadataTarget" class="help-text">当前待关联文件夹：<code>{{ metadataTarget.path || metadataTarget.name }}</code>。点击候选的“关联此影视”后立即生效。</p>
            <p v-else class="warning">尚未选择目标文件夹；可以搜索，但保存关联前必须先选择媒体目录。</p>
            <template v-if="metadataSearchReport">
              <p v-for="warning in metadataSearchReport.warnings" :key="warning" class="warning">{{ warning }}</p>
              <p>搜索词：{{ metadataSearchReport.query }}。共 {{ metadataSearchReport.total_results }} 个候选，第 {{ metadataSearchReport.page }} / {{ metadataSearchReport.total_pages }} 页。</p>
              <div class="table-wrap"><table><thead><tr><th>来源 / ID</th><th>片名 / 原名</th><th>首播 / 上映日期</th><th>简介</th><th>操作</th></tr></thead><tbody>
                <tr v-for="candidate in metadataSearchReport.results" :key="`${candidate.scraper_id}-${candidate.id}`">
                  <td>{{ candidate.scraper_id }}<p><a v-if="candidate.scraper_id === 'tmdb'" :href="`https://www.themoviedb.org/${metadataSearchReport.media_type}/${candidate.id}`" target="_blank" rel="noopener noreferrer">{{ candidate.id }}</a><span v-else>{{ candidate.id }}</span></p></td>
                  <td>{{ candidate.name || candidate.title }}<p>{{ candidate.original_name || candidate.original_title }}</p></td>
                  <td>{{ candidate.first_air_date || candidate.release_date || "未知" }}</td>
                  <td><details><summary>{{ candidate.overview ? "查看简介" : "暂无简介" }}</summary><p>{{ candidate.overview }}</p></details></td>
                  <td><button :disabled="busy || !metadataTarget || candidate.scraper_id !== 'tmdb'" @click="chooseMetadataCandidate(candidate)">关联此影视</button></td>
                </tr><tr v-if="!metadataSearchReport.results.length"><td colspan="5">没有找到候选，可尝试原名或其他译名。</td></tr>
              </tbody></table></div>
              <div class="toolbar">
                <button :disabled="busy || metadataSearchReport.page <= 1" @click="searchMetadata(metadataSearchReport.page - 1)">上一页</button>
                <button :disabled="busy || metadataSearchReport.page >= metadataSearchReport.total_pages || metadataSearchReport.page >= 500" @click="searchMetadata(metadataSearchReport.page + 1)">下一页</button>
              </div>
              <details><summary>查看 API 返回的候选原始数据</summary><pre>{{ JSON.stringify(metadataSearchReport.results, null, 2) }}</pre></details>
            </template>
          </div>
          <details ref="metadataMappingPanel" class="config-card" open>
            <summary>手动填写 / 修改影视关联</summary>
            <p class="help-text">当前目标：<code>{{ metadataTarget ? metadataTarget.path || metadataTarget.name : "尚未选择，请先选择媒体目录" }}</code></p>
            <form @submit.prevent="saveMetadataMapping(metadataTarget?.path, metadataMappingId)">
              <label class="config-field config-field-short">TMDB ID
                <input v-model.number="metadataMappingId" type="number" min="1" max="2147483647" step="1" required :disabled="busy" aria-label="映射 TMDB ID" />
                <span class="help-text">直接输入已知 ID；搜索结果可直接点击关联，无需在此再次保存。</span>
              </label>
              <div class="config-actions">
                <span class="help-text">核对目标文件夹与 ID；保存后需重新预览刮削。</span>
                <button class="primary" :disabled="busy || !metadataTarget">保存关联</button>
              </div>
            </form>
          </details>
          <div class="config-section-heading">
            <h3>已保存的关联（{{ currentMetadataMappings.length }} 条）</h3>
            <button :disabled="busy" @click="run(loadMetadataMappings)">刷新列表</button>
          </div>
          <div class="row-actions selection-actions">
            <label class="check-control"><input type="checkbox" :checked="!!currentMetadataMappings.length && currentMetadataMappings.slice(0, 1000).every(item => selectedMappingIds.includes(item.id))" :disabled="busy || !currentMetadataMappings.length" @change="selectedMappingIds = currentMetadataMappings.slice(0, 1000).every(item => selectedMappingIds.includes(item.id)) ? [] : currentMetadataMappings.slice(0, 1000).map(item => item.id)" /><span>{{ currentMetadataMappings.length > 1000 ? "选择前 1000 条关联" : "全选关联" }}</span></label>
            <button class="danger-button" :disabled="busy || !selectedMappingIds.length" @click="deleteMetadataMappings(selectedMappingIds)">批量解除关联（{{ selectedMappingIds.length }}）</button>
          </div>
          <div class="table-wrap"><table><thead><tr><th class="selection-cell">选择</th><th>媒体根目录 / 文件夹</th><th>TMDB ID</th><th>操作</th></tr></thead><tbody>
            <tr v-for="mapping in currentMetadataMappings" :key="mapping.id">
              <td class="selection-cell"><input v-model="selectedMappingIds" type="checkbox" :value="mapping.id" :disabled="busy || (selectedMappingIds.length >= 1000 && !selectedMappingIds.includes(mapping.id))" :aria-label="`选择关联 ${mapping.folder_name}`" /></td>
              <td>{{ mapping.root_path }}<p>{{ mapping.folder_path }}</p></td><td>{{ mapping.tmdb_id }}</td>
              <td><div class="row-actions"><button :disabled="busy" @click="editMetadataMapping(mapping)">编辑</button><button class="danger-button" :disabled="busy" @click="deleteMetadataMappings([mapping.id])">解除关联</button></div></td>
            </tr><tr v-if="!currentMetadataMappings.length"><td colspan="4">暂无手动关联，可选择文件夹后搜索并指定 ID。</td></tr>
          </tbody></table></div>
          <details class="help-details"><summary>检查与清理失效关联</summary>
            <p class="help-text">仅清理关联记录，不删除媒体文件。</p>
            <div class="row-actions">
              <button :disabled="busy" @click="cleanupMetadataMappings(true)">检查失效关联</button>
              <button class="danger-button" :disabled="busy" @click="cleanupMetadataMappings(false)">检查并清理</button>
            </div>
            <div v-if="metadataCleanupReport" class="table-wrap"><table><thead><tr><th>文件夹 / ID</th><th>检查结果</th><th>处理</th></tr></thead><tbody>
              <tr v-for="row in metadataCleanupReport.rows" :key="row.id">
                <td>{{ row.root_path }}<p>{{ row.folder_path }} · {{ row.media_type }} / {{ row.tmdb_id }}</p></td>
                <td>{{ row.reason }}</td><td>{{ row.removed ? "已清理关联" : row.status === "missing_folder" || row.status === "missing_id" ? "待清理（仅检查）" : "保留" }}</td>
              </tr>
            </tbody></table></div>
          </details>
          </template>
          <template v-if="tab === 'metadata'">
          <label class="check-control"><input v-model="metadataOverwrite" type="checkbox" :disabled="busy" /><span>覆盖已有 NFO / 海报（自动备份）</span></label>
          <div class="toolbar">
            <button :class="{ primary: !metadataPlan }" :disabled="busy || metadataRoot < 0" @click="previewMetadata">重新预览</button>
          </div>
          <p v-if="metadataRoot < 0" class="empty-state">请选择媒体目录</p>
          <p v-if="metadataMappingEdited" class="warning">已编辑手动 ID，请先保存映射，再重新预览；当前预览不能直接写入。</p>
          <template v-if="metadataPlan">
            <h3>匹配结果</h3>
            <div class="table-wrap"><table><thead><tr><th>文件夹</th><th>匹配 / 状态</th><th>写入计划与提示</th><th>手动 TMDB ID</th></tr></thead>
              <tbody><tr v-for="row in metadataPlan.rows" :key="row.folder">
                <td>{{ row.folder_name || row.folder }}</td><td>{{ row.status === "matched" ? `${row.title} ${row.year || ""} · TMDB ${row.tmdb_id}` : row.status === "skipped" ? "已跳过" : "失败" }}<p v-if="row.match_source">{{ row.match_source === "manual" ? "使用手动映射" : "自动搜索" }}</p></td>
                <td><p v-if="row.reason">{{ row.reason }}</p><details v-if="row.files?.length"><summary>{{ row.files.length }} 个待写入文件</summary><pre>{{ row.files.join("\n") }}</pre></details><p v-for="warning in row.warnings" :key="warning" class="warning">{{ warning }}</p></td>
                <td><template v-if="row.folder_name">
                  <span>{{ row.tmdb_id || "尚未关联" }}</span>
                  <button :disabled="busy" @click="selectMetadataFolder(row.folder_name, row.folder)">前往配置关联</button>
                </template></td>
              </tr></tbody>
            </table></div>
            <div class="toolbar execution-actions">
              <button class="primary" :disabled="busy || !metadataPlan.file_count || metadataMappingEdited" @click="applyMetadata">确认写入 {{ metadataPlan.file_count }} 个文件</button>
              <span v-if="!metadataPlan.file_count" class="help-text">无可写入文件</span>
            </div>
          </template>
          <template v-if="metadataResult">
            <h3>写入结果：{{ outcomeNames[metadataResult.outcome] }}</h3>
            <details><summary>本批次匹配与跳过详情</summary>
              <div v-for="row in metadataResult.rows" :key="row.folder">
                <p>{{ row.folder_name || row.folder }}：{{ row.reason || row.title }} {{ row.year || "" }} {{ row.match_source === "manual" ? "（手动映射）" : "" }}</p>
                <p v-for="warning in row.warnings" :key="warning" class="warning">{{ warning }}</p>
              </div>
            </details>
            <div class="table-wrap"><table><thead><tr><th>文件</th><th>状态</th><th>备份 / 错误</th></tr></thead><tbody>
              <tr v-for="file in metadataResult.files" :key="file.path"><td>{{ file.path }}</td><td>{{ file.status === "success" ? "成功" : "失败" }}</td><td>{{ file.backup || "" }} {{ file.error || "" }}</td></tr>
            </tbody></table></div>
          </template>
          </template>
          <p><small>本产品使用 TMDB API，但未经 TMDB 认可或认证。元数据与图片来源：<a href="https://www.themoviedb.org/" target="_blank" rel="noopener noreferrer">The Movie Database (TMDB)</a>。</small></p>
        </template>
      </section>
      <section v-if="tab === 'configuration' && isAdmin && configurationSection === 'emby'" class="panel">
        <h2>Emby 联动</h2>
        <p class="help-text">使用 Emby 后台创建的 API Token。Token 仅保存到服务器数据库，不回传浏览器；远程连接建议使用 HTTPS。接口前缀按实际部署填写，例如 /emby。</p>
        <form @submit.prevent="saveEmby">
          <div class="config-fields">
            <label class="config-field">服务地址<input v-model="embyForm.server_url" type="url" required maxlength="2000" placeholder="http://127.0.0.1:8096/emby" :disabled="busy" /></label>
            <label class="config-field">API Token<input v-model="embyForm.api_token" type="password" autocomplete="new-password" maxlength="2048" :placeholder="embySaved.configured ? '已保存，留空保留原 Token' : '填写 Emby API Token'" :disabled="busy" /></label>
          </div>
          <label class="check-control"><input v-model="embyForm.auto_refresh" type="checkbox" :disabled="busy" /><span>迁移、刮削或批量重命名实际修改文件后，自动请求刷新全部媒体库</span></label>
          <div class="config-actions">
            <button class="primary" :disabled="busy || !embyDirty">保存 Emby 配置</button>
            <button type="button" :disabled="busy || !embySaved.configured || embyDirty" @click="testEmby">测试连接</button>
            <button type="button" class="danger-button" :disabled="busy || !embySaved.configured" @click="clearEmbyToken">清除 Token</button>
          </div>
        </form>
        <p v-if="embyDirty" class="help-text">有未保存的配置；请先保存，再测试连接或刷新媒体库。更换地址需重新填写 Token。</p>
        <h3>媒体库刷新</h3>
        <div class="toolbar">
          <label class="directory-field">Emby 媒体库<select v-model="embyLibrary" :disabled="busy || !embyLibraries.length || embyDirty"><option value="">请先读取并选择媒体库</option><option v-for="library in embyLibraries" :key="library.id" :value="library.id">{{ library.name }}</option></select></label>
          <button :disabled="busy || !embySaved.configured || embyDirty" @click="readEmbyLibraries">读取媒体库</button>
          <button :disabled="busy || !embyLibrary || embyDirty" @click="refreshEmby(false)">刷新所选库</button>
          <button :disabled="busy || !embySaved.configured || embyDirty" @click="refreshEmby(true)">刷新全部库</button>
        </div>
        <p class="help-text">这里只提交刷新请求，不强制替换已有元数据或图片。实际扫描进度请在 Emby 查看；自动刷新失败不回滚已完成的文件操作。</p>
      </section>
      <section v-if="tab === 'configuration' && isAdmin && configurationSection === 'downloaders'" class="panel">
        <h2>网站与下载器 <span class="badge">功能预留</span></h2>
        <div class="empty-state">
          <h3>下载器配置尚未开放</h3>
          <p>后续将支持网站域名与下载器的对应配置。目前不保存配置，也不连接网站或下载服务。</p>
          <p class="help-text">下载目标将统一使用已配置的媒体目录。</p>
        </div>
        <div class="config-actions"><a class="button-link" href="#downloads">查看影视下载页面</a><a class="button-link" href="#configuration/roots">管理媒体目录</a></div>
      </section>
      <section v-if="tab === 'downloads'" class="panel">
        <h2>下载来源 <span class="badge">功能预留</span></h2>
        <p class="message">当前仅展示规划流程，网址解析、集数爬取和下载均未实现，不会发起网络请求或创建下载任务。</p>
        <ol class="workflow" aria-label="计划下载流程"><li>提供网址</li><li>解析并选择集数</li><li>下载到媒体目录</li></ol>
        <div class="toolbar">
          <label class="form-field">影视页面 URL<input type="url" disabled placeholder="待开放：输入剧集或播放列表网址" /></label>
          <button class="primary" disabled>解析集数</button>
        </div>
        <p class="help-text">计划根据 URL 的网站域名自动匹配已配置的下载器。
          <a v-if="isAdmin" href="#configuration/downloaders">网站与下载器配置</a>
        </p>
        <div class="toolbar">
          <label class="form-field">匹配的下载器<input disabled placeholder="等待解析网址" /></label>
          <label class="directory-field">下载到媒体目录<select disabled>
            <option value="">{{ config.roots.length ? "待开放：选择媒体目录" : "尚未配置媒体目录" }}</option>
            <option v-for="item in config.roots" :key="item.id" :value="item.id" :title="item.path">{{ rootLabel(item.path) }}</option>
          </select></label>
        </div>
        <p class="help-text">下载目标仅限已配置的媒体目录，不提供自由路径输入。
          <a v-if="isAdmin" href="#configuration/roots">管理媒体目录</a>
        </p>
      </section>
      <section v-if="tab === 'downloads'" class="panel">
        <h2>选择集数</h2>
        <div class="toolbar">
          <label class="check-control"><input type="checkbox" disabled /><span>全选集数</span></label>
          <span class="help-text">尚未解析 · 已选 0 集</span>
        </div>
        <div class="table-wrap"><table><thead><tr><th>选择</th><th>集数</th><th>标题</th><th>可下载状态</th></tr></thead>
          <tbody><tr><td colspan="4" class="empty-state">解析功能接入后，将在这里展示集数，支持勾选需要下载的内容。</td></tr></tbody>
        </table></div>
        <div class="toolbar execution-actions">
          <button class="primary" disabled>下载所选集数</button>
          <span class="help-text">下载功能尚未开放</span>
        </div>
      </section>
    </template>
    <dialog v-if="connected && isAdmin" ref="mediaFolderDialog" class="folder-picker" aria-labelledby="folder-picker-title" @cancel="busy && $event.preventDefault()">
      <h2 id="folder-picker-title">{{ folderPickerPurpose === "migration" ? "选择下载目录" : "选择媒体文件夹" }}</h2>
      <div class="row-actions">
        <button :disabled="busy" @click="browseMediaFolders()">磁盘列表</button>
        <button :disabled="busy || !mediaFolderListing?.path" @click="browseMediaFolders(mediaFolderListing?.parent)">上一级</button>
      </div>
      <div class="folder-picker-body">
        <p>当前位置：<code>{{ mediaFolderListing?.path || "请选择磁盘" }}</code></p>
        <p v-if="busy">正在处理…</p>
        <p v-if="mediaFolderError" class="message error" role="alert">{{ mediaFolderError }}</p>
        <div v-if="mediaFolderListing" class="folder-list">
          <button v-for="folder in mediaFolderListing.entries" :key="folder.path" :disabled="busy || folder.blocked" @click="browseMediaFolders(folder.path)">
            {{ folder.name }}{{ folder.blocked ? "（链接，不可选择）" : "" }}
          </button>
          <p v-if="!mediaFolderListing.entries.length">无子文件夹</p>
        </div>
        <p v-if="mediaFolderListing?.reason" class="warning">{{ mediaFolderListing.reason }}；可继续进入子文件夹。</p>
        <p v-if="folderPickerPurpose === 'root' && mediaFolderAlreadyAdded" class="message">此文件夹已添加。</p>
      </div>
      <div class="config-actions">
        <button :disabled="busy" @click="mediaFolderDialog?.close()">取消</button>
        <button class="primary" :disabled="busy || !mediaFolderListing?.selectable || !!mediaFolderError || (folderPickerPurpose === 'root' && (mediaFolderAlreadyAdded || mediaRootsDraft.length >= 32))" @click="addMediaFolder">{{ folderPickerPurpose === "migration" ? "使用此下载目录" : "添加当前文件夹" }}</button>
      </div>
    </dialog>
    <footer>{{ busy ? "正在处理…" : "MediaForge" }}</footer>
    </main>
  </div>
</template>
