// Native notifications for background tasks (engine relays Railway
// /tasks/stream as WS "task-update"). Only notify on the three moments the
// user cares about: finished, failed, or waiting for their approval.
import { Notification } from "electron";
import { popoverWindow } from "./tray";

type TaskUpdate = {
  id: string;
  status: string;
  approval_needed: boolean;
  prompt?: string;
  result_preview?: string;
};

export function showTaskNotification(t: TaskUpdate): void {
  if (!Notification.isSupported()) return;
  const subject = (t.prompt || "Background task").slice(0, 80);
  let title: string | null = null;
  let body = subject;
  if (t.approval_needed) {
    title = "Uxie needs your approval";
  } else if (t.status === "completed") {
    title = "Task done";
    body = t.result_preview ? t.result_preview.slice(0, 160) : subject;
  } else if (t.status === "failed") {
    title = "Task failed";
  }
  if (!title) return;
  const n = new Notification({ title, body });
  n.on("click", revealTasksTab);
  n.show();
}

function revealTasksTab(): void {
  const w = popoverWindow();
  if (!w || w.isDestroyed()) return;
  if (!w.isVisible()) w.show();
  w.focus();
  w.webContents.send("tasks:reveal", null);
}
