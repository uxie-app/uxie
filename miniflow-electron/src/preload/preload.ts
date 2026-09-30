// Preload runs with sandbox: true, so require() is restricted to Electron's
// built-in modules. We cannot import from ../shared/types — inline the channel
// names here instead. Keep these strings in sync with src/shared/types.ts.

import { contextBridge, ipcRenderer, IpcRendererEvent } from "electron";

const CH = {
  listProviders: "llm:list",
  getStatus: "llm:status",
  setActive: "llm:setActive",
  setModel: "llm:setModel",
  setApiKey: "llm:setKey",
  clearApiKey: "llm:clearKey",
  revealKeysFile: "llm:revealFile",
  saveSmallestKey: "stt:setKey",
  getHotkey: "hotkey:get",
  setHotkey: "hotkey:set",
  resetHotkey: "hotkey:reset",
  startCapture: "voice:start",
  stopCapture: "voice:stop",
  audioChunk: "voice:chunk",
  transcription: "voice:transcription",
  transcriptionInterim: "voice:transcription-interim",
  transcriptionError: "voice:transcription-error",
  actionResult: "agent:action",
  agentStatus: "agent:status",
  getHistory: "history:get",
  clearHistory: "history:clear",
  executeCommand: "agent:execute",
  permGetAll: "perm:getAll",
  permRequest: "perm:request",
  windowPin: "window:pin",
  openExternal: "app:openExternal",
  quit: "app:quit",
  // Auth
  sendOtp: "auth:sendOtp",
  verifyOtp: "auth:verifyOtp",
  getUserStatus: "auth:userStatus",
  getUxieUser: "auth:uxieUser",
  logout: "auth:logout",
} as const;

function listen<T>(channel: string, cb: (value: T) => void) {
  const handler = (_e: IpcRendererEvent, value: T) => cb(value);
  ipcRenderer.on(channel, handler);
  return () => ipcRenderer.removeListener(channel, handler);
}

const api = {
  // Host platform — exposed so the renderer can render OS-specific labels
  // ("fn" on Mac, "right alt" on Windows). Set once at preload time.
  platform: process.platform as "darwin" | "win32" | "linux",

  // LLM
  listProviders:  () => ipcRenderer.invoke(CH.listProviders),
  getLLMStatus:   () => ipcRenderer.invoke(CH.getStatus),
  setActiveLLM:   (id: string) => ipcRenderer.invoke(CH.setActive, id),
  setLLMModel:    (id: string, model: string, baseUrl: string | null) =>
    ipcRenderer.invoke(CH.setModel, id, model, baseUrl),
  setLLMKey:      (id: string, key: string) => ipcRenderer.invoke(CH.setApiKey, id, key),
  clearLLMKey:    (id: string) => ipcRenderer.invoke(CH.clearApiKey, id),
  revealKeysFile: () => ipcRenderer.invoke(CH.revealKeysFile),
  revealLog:        () => ipcRenderer.invoke("fs:revealLog"),
  openMiniflowDir:  () => ipcRenderer.invoke("fs:openMiniflowDir"),

  // Smallest AI (STT)
  saveSmallestKey: (k: string) => ipcRenderer.invoke(CH.saveSmallestKey, k),

  // Transcription events
  onTranscription: (cb: (p: { transcript: string; is_final: boolean; is_session?: boolean }) => void) =>
    listen(CH.transcription, cb),
  onTranscriptionInterim: (cb: (p: { transcript: string }) => void) =>
    listen(CH.transcriptionInterim, cb),
  onTranscriptionError: (cb: (err: string) => void) =>
    listen<string>(CH.transcriptionError, cb),

  // Execute a typed-in text command through the agent
  executeCommand: (text: string) => ipcRenderer.invoke(CH.executeCommand, text),

  // Permissions
  getPermissions:     () => ipcRenderer.invoke(CH.permGetAll),
  requestPermission:  (id: "microphone" | "accessibility" | "inputMonitoring") =>
    ipcRenderer.invoke(CH.permRequest, id),
  pinWindow:          (pinned: boolean) => ipcRenderer.invoke(CH.windowPin, pinned),

  // Hotkey
  getHotkey:      () => ipcRenderer.invoke(CH.getHotkey),
  setHotkey:      (h: unknown) => ipcRenderer.invoke(CH.setHotkey, h),
  resetHotkey:    () => ipcRenderer.invoke(CH.resetHotkey),

  // Voice
  sendAudioChunk: (base64: string) => ipcRenderer.invoke(CH.audioChunk, base64),
  onStartCapture: (cb: (p: { mode: "dictation" | "command" }) => void) =>
    listen<{ mode: "dictation" | "command" }>(CH.startCapture, (p) => cb(p ?? { mode: "dictation" })),
  onStopCapture:  (cb: (p: { mode: "dictation" | "command" }) => void) =>
    listen<{ mode: "dictation" | "command" }>(CH.stopCapture, (p) => cb(p ?? { mode: "dictation" })),
  onAction:       (cb: (a: any) => void) => listen<any>(CH.actionResult, cb),
  onAgentStatus:  (cb: (s: string) => void) => listen<string>(CH.agentStatus, cb),
  onDictationChunk: (cb: (chunk: string) => void) =>
    listen<{ chunk?: string }>("agent:chunk", (p) => cb(p?.chunk ?? "")),
  onDebugEvent: (cb: (e: { type: string; text: string; app: string; success?: boolean }) => void) =>
    listen("debug:event", cb),

  // History
  getHistory:     () => ipcRenderer.invoke(CH.getHistory),
  clearHistory:   () => ipcRenderer.invoke(CH.clearHistory),

  // Auth (Uxie backend)
  sendOtp:        (email: string, referralCode?: string) =>
    ipcRenderer.invoke(CH.sendOtp, email, referralCode),
  verifyOtp:      (email: string, code: string) =>
    ipcRenderer.invoke(CH.verifyOtp, email, code),
  getUserStatus:  () => ipcRenderer.invoke(CH.getUserStatus),
  getUxieUser:    () => ipcRenderer.invoke(CH.getUxieUser),
  logout:         () => ipcRenderer.invoke(CH.logout),

  // Approval widget
  onApprovalNeeded: (cb: (e: { tool: string; summary: string; params: Record<string, unknown> }) => void) =>
    listen("agent:approval-needed", cb),
  sendApproval: (approved: boolean, editedParams?: Record<string, unknown>) =>
    ipcRenderer.invoke("agent:resolve-approval", approved, editedParams ?? null),

  // Capsule widget — single channel for every state from docs/widgets.md.
  // Payload shape: { kind: "dictating" | "transcribing" | ... , ...fields }.
  // Renderer updates its UI; main owns the actual show/hide of the window.
  onWidgetState: (cb: (state: any) => void) => listen("widget:state", cb),

  // Renderer reports its measured natural height after each state morph so
  // the main process can resize the overlay window without a layout jump.
  reportWidgetSize: (height: number) => ipcRenderer.send("widget:resize", height),

  // MCP connector management
  getMCPStatus:        () => ipcRenderer.invoke("mcp:getStatus"),
  connectMCPServer:    (serverId: string, credentials: Record<string, string>) =>
    ipcRenderer.invoke("mcp:connectServer", serverId, credentials),
  disconnectMCPServer: (serverId: string) => ipcRenderer.invoke("mcp:disconnectServer", serverId),

  // OAuth connectors (Google, Slack)
  getConnectedProviders: () => ipcRenderer.invoke("oauth:getConnected"),
  startOAuth:            (provider: string) => ipcRenderer.invoke("oauth:start", provider),
  disconnectProvider:    (provider: string) => ipcRenderer.invoke("oauth:disconnect", provider),
  onOAuthConnected:      (cb: (provider: string) => void) => listen<string>("oauth:connected", cb),

  // App
  openExternal:   (url: string) => ipcRenderer.invoke(CH.openExternal, url),
  quit:           () => ipcRenderer.invoke(CH.quit),

  // Auto-updater (Settings → Check for updates)
  // checkForUpdate: hits GitHub Releases, returns { ok, current, updateInfo }.
  // downloadUpdate: fetches the newer DMG/EXE in the background.
  // installNow: quits and installs the downloaded update.
  // onUpdaterEvent: stream of lifecycle events (checking, available, progress, downloaded, error).
  checkForUpdate:   () => ipcRenderer.invoke("updater:check"),
  downloadUpdate:   () => ipcRenderer.invoke("updater:download"),
  installUpdateNow: () => ipcRenderer.invoke("updater:installNow"),
  getAppVersion:    () => ipcRenderer.invoke("updater:version"),
  onUpdaterEvent:   (cb: (e: { kind: string; payload: any }) => void) =>
    listen("updater:event", cb),

  // Helper
  requestType:    (text: string) => ipcRenderer.send("helper:type", text),

  // Meetings (Note Taker tab)
  listMeetings:        (limit?: number) => ipcRenderer.invoke("meetings:list", limit),
  getMeeting:          (id: number) => ipcRenderer.invoke("meetings:get", id),
  deleteMeeting:       (id: number) => ipcRenderer.invoke("meetings:delete", id),
  deleteAllMeetings:   () => ipcRenderer.invoke("meetings:deleteAll"),
  updateMeetingNotes:  (id: number, notes: string) => ipcRenderer.invoke("meetings:updateNotes", id, notes),
  startMeetingRecording: (id: number) => ipcRenderer.invoke("meetings:startRecording", id),
  stopMeetingRecording:  (id: number, transcript?: string) =>
    ipcRenderer.invoke("meetings:stopRecording", id, transcript),
  skipMeeting:         (id: number) => ipcRenderer.invoke("meetings:skip", id),
  structureMeeting:    (id: number) => ipcRenderer.invoke("meetings:structure", id),
  getAutoDetectMeetings: () => ipcRenderer.invoke("meetings:getAutoDetect"),
  setAutoDetectMeetings: (enabled: boolean) => ipcRenderer.invoke("meetings:setAutoDetect", enabled),
  getShareMeetingsWithAdmin: () => ipcRenderer.invoke("meetings:getShareAdmin"),
  setShareMeetingsWithAdmin: (enabled: boolean) => ipcRenderer.invoke("meetings:setShareAdmin", enabled),

  // Background tasks (v1.1.0)
  createTask:   (prompt: string) => ipcRenderer.invoke("tasks:create", prompt),
  listTasks:    () => ipcRenderer.invoke("tasks:list"),
  getTask:      (id: string) => ipcRenderer.invoke("tasks:get", id),
  cancelTask:   (id: string) => ipcRenderer.invoke("tasks:cancel", id),
  approveTask:  (id: string, toolCallId: string, approved: boolean, editedArgs?: any) =>
    ipcRenderer.invoke("tasks:approve", id, toolCallId, approved, editedArgs ?? null),

  // Scheduled tasks / Briefings (v1.2)
  listSchedules:  () => ipcRenderer.invoke("sched:list"),
  createSchedule: (body: any) => ipcRenderer.invoke("sched:create", body),
  patchSchedule:  (id: string, patch: any) => ipcRenderer.invoke("sched:patch", id, patch),
  deleteSchedule: (id: string) => ipcRenderer.invoke("sched:delete", id),
  fireSchedule:   (id: string) => ipcRenderer.invoke("sched:fire", id),

  // Referrals (v1.4)
  getReferralStats: () => ipcRenderer.invoke("referral:stats"),
  redeemReferral:   (code: string) => ipcRenderer.invoke("referral:redeem", code),
  onMeetingDetected:   (cb: (m: any) => void) => listen("meeting:detected", cb),
  onMeetingTranscriptUpdate: (cb: (m: any) => void) => listen("meeting:transcript-update", cb),
  onMeetingInterimTranscript: (cb: (m: any) => void) => listen("meeting:interim-transcript", cb),
  onMeetingsRefresh:   (cb: () => void) => listen("meetings:refresh", () => cb()),
  onMeetingsReveal:    (cb: () => void) => listen("meetings:reveal", () => cb()),
  onTaskUpdate:        (cb: (t: any) => void) => listen("tasks:update", cb),
  onTasksReveal:       (cb: () => void) => listen("tasks:reveal", () => cb()),

  // Dictionary (word → replacement)
  getDictionary:      () => ipcRenderer.invoke("dict:get"),
  addDictWord:        (from: string, to: string) => ipcRenderer.invoke("dict:add", from, to),
  removeDictWord:     (from: string) => ipcRenderer.invoke("dict:remove", from),

  // Snippets (trigger → expansion)
  getSnippets:        () => ipcRenderer.invoke("snip:get"),
  addSnippet:         (trigger: string, expansion: string) => ipcRenderer.invoke("snip:add", trigger, expansion),
  removeSnippet:      (trigger: string) => ipcRenderer.invoke("snip:remove", trigger),
};

contextBridge.exposeInMainWorld("miniflow", api);
