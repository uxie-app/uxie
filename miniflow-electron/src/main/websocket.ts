// WebSocket client to the Python backend — streams agent events to the renderer.

import WebSocket from "ws";
import { BrowserWindow } from "electron";
import { EventEmitter } from "events";

import { WS_URL } from "./api";
import { IpcChannels } from "../shared/types";
import {
  onTranscriptionInterim,
  onTranscriptionFinal,
  onAgentStatus,
  onActionResult,
  onActionChunk,
  onApprovalNeeded,
  onApprovalResolved,
} from "./widgetState";
import { showMeetingDetectedNotification } from "./meetingNotifications";
import { showTaskNotification } from "./taskNotifications";
import { clearSessionToken, syncSessionToken } from "./sessionToken";

let ws: WebSocket | null = null;
let getWindows: (() => BrowserWindow[]) = () => [];

export function setWindowProvider(fn: () => BrowserWindow[]) {
  getWindows = fn;
}

/** Emits "chunk" events for streamed dictation text so main/index.ts can
 *  pipe them directly to the Rust helper without an extra IPC hop. */
export const chunkBus = new EventEmitter();

export function connectWs(): void {
  if (ws && ws.readyState === WebSocket.OPEN) return;
  ws = new WebSocket(WS_URL);

  ws.on("open", () => {
    console.log("[ws] connected");
    // Engine keeps the JWT in memory only — hand it over on every connect
    // (covers app start and engine restarts).
    void syncSessionToken();
  });
  ws.on("close", () => {
    console.log("[ws] closed, reconnecting in 2s");
    ws = null;
    setTimeout(connectWs, 2000);
  });
  ws.on("error", (err) => console.error("[ws]", err.message));
  ws.on("message", (raw) => {
    let msg: { event: string; payload: unknown };
    try { msg = JSON.parse(raw.toString("utf8")); }
    catch { return; }

    // Fast path: streamed grammar-correction chunks go straight to the helper
    // via an in-process event bus. No IPC round-trip.
    if (msg.event === "action-chunk") {
      const chunk = (msg.payload as { chunk?: string } | null)?.chunk;
      if (chunk) chunkBus.emit("chunk", chunk);
      // Fall through so the renderer can still surface it in the UI preview.
    }

    // Drive the floating widget state machine. setMode handles show/hide
    // for us — no direct overlay calls from this file anymore.
    routeToWidgetState(msg.event, msg.payload);

    // Meeting detection fires a native macOS Notification — the user sees
    // it even when Uxie is in the background, which is the whole point.
    if (msg.event === "meeting:detected") {
      try {
        showMeetingDetectedNotification(msg.payload as any);
      } catch (e) {
        console.error("[meeting] notification failed:", e);
      }
    }

    if (msg.event === "auth-expired") {
      clearSessionToken(); // engine already signed out; drop the safeStorage copy
    }

    if (msg.event === "task-update") {
      try {
        showTaskNotification(msg.payload as any);
      } catch (e) {
        console.error("[tasks] notification failed:", e);
      }
    }

    forwardToRenderer(msg.event, msg.payload);
  });
}

function routeToWidgetState(event: string, payload: unknown): void {
  const p = (payload ?? {}) as Record<string, unknown>;
  switch (event) {
    case "transcription-interim":
      onTranscriptionInterim(typeof p.text === "string" ? p.text : "");
      break;
    case "transcription":
      onTranscriptionFinal(typeof p.text === "string" ? p.text : "");
      break;
    case "agent-status":
      onAgentStatus(typeof p.status === "string" ? p.status : String(p.status ?? ""));
      break;
    case "action-chunk":
      onActionChunk(typeof p.chunk === "string" ? p.chunk : "");
      break;
    case "action-result":
      onActionResult({
        ok: typeof p.ok === "boolean" ? p.ok : undefined,
        summary: typeof p.summary === "string" ? p.summary : undefined,
        error: typeof p.error === "string" ? p.error : undefined,
      });
      break;
    case "approval-needed":
      onApprovalNeeded({
        tool: String(p.tool ?? ""),
        summary: String(p.summary ?? ""),
        params: (p.params as Record<string, unknown>) ?? {},
      });
      break;
    case "approval-resolved":
      onApprovalResolved();
      break;
  }
}

function forwardToRenderer(event: string, payload: unknown): void {
  // Event name → renderer IPC channel. Anything not in this map is dropped.
  const channel: string | null =
    event === "agent-status"        ? IpcChannels.agentStatus :
    event === "action-result"       ? IpcChannels.actionResult :
    event === "action-chunk"        ? "agent:chunk" :
    event === "transcription"         ? "voice:transcription" :
    event === "transcription-interim" ? "voice:transcription-interim" :
    event === "transcription-error"   ? "voice:transcription-error" :
    event === "oauth-connected"     ? "oauth:connected" :
    event === "debug"               ? "debug:event" :
    event === "approval-needed"     ? "agent:approval-needed" :
    event === "approval-resolved"   ? "agent:approval-resolved" :
    event === "meeting:detected"    ? "meeting:detected" :
    event === "meeting:transcript-update" ? "meeting:transcript-update" :
    event === "meeting:interim-transcript" ? "meeting:interim-transcript" :
    event === "task-update"         ? "tasks:update" :
    event === "auth-expired"        ? "auth:expired" :
    null;
  if (!channel) return;
  // Broadcast to every live BrowserWindow exactly ONCE. We used to also loop
  // via getWindows() which double-fired every action, so the helper was
  // typing dictation text twice.
  const seen = new Set<number>();
  for (const w of BrowserWindow.getAllWindows()) {
    if (w.isDestroyed() || seen.has(w.id)) continue;
    seen.add(w.id);
    w.webContents.send(channel, payload);
  }
}
