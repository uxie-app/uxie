import React, { useCallback, useEffect, useRef, useState } from "react";

type TaskStatus = "queued" | "running" | "completed" | "failed" | "cancelled";

type TaskEvent = {
  seq: number;
  kind: "step_start" | "tool_call" | "tool_result" | "thinking" | "final_text" | "error"
      | "approval_needed" | "approval_resolved"
      | "computer_session" | "computer_actions" | "computer_takeover" | "computer_done" | "computer_result";
  data: any;
  created_at: string;
};

type Task = {
  id: string;
  prompt: string;
  status: TaskStatus;
  agent_name?: string | null;
  waiting?: boolean;
  waiting_for?: { type?: string; thread_id?: string; wake_at?: string } | null;
  approval_needed?: boolean;
  result_md: string | null;
  error: string | null;
  created_at: string;
  completed_at: string | null;
  events?: TaskEvent[];
};

const w = window as any;

const STATUS_COLORS: Record<TaskStatus, string> = {
  queued:    "#5b6878",
  running:   "#3367d6",
  completed: "#3a8c6a",
  failed:    "#d44a4a",
  cancelled: "#5b6878",
};

// What the user should see: "needs you" beats "running"; a parked task is
// still status=running on the server but reads as waiting.
function displayStatus(t: Task): { label: string; color: string } {
  if (t.approval_needed && !["completed", "failed", "cancelled"].includes(t.status)) {
    return { label: "needs you", color: "#F4A21B" };
  }
  if (t.waiting) return { label: "waiting", color: "#5b6878" };
  return { label: t.status, color: STATUS_COLORS[t.status] ?? "#888" };
}

function waitingText(w: Task["waiting_for"]): string {
  if (!w) return "";
  if (w.type === "gmail_reply") return "waiting for an email reply";
  if (w.type === "slack_reply") return "waiting for a Slack reply";
  if (w.type === "time" && w.wake_at) {
    return `waiting until ${new Date(w.wake_at).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" })}`;
  }
  return "waiting";
}

function StatusPill({ task }: { task: Task }) {
  const { label, color } = displayStatus(task);
  return (
    <span style={{
      fontSize: 11, padding: "2px 8px", borderRadius: 10,
      background: color + "22", color, fontWeight: 600,
      textTransform: "uppercase", letterSpacing: 0.04, whiteSpace: "nowrap",
    }}>
      {label}
    </span>
  );
}

function formatRelative(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 60)     return `${Math.floor(diff)}s ago`;
  if (diff < 3600)   return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400)  return `${Math.floor(diff / 3600)}h ago`;
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}

export function TasksTab() {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const r = await w.miniflow.listTasks();
      setTasks(Array.isArray(r?.tasks) ? r.tasks : []);
    } catch (e) {
      console.error("[tasks] list failed:", e);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // Push: engine relays Railway /tasks/stream. Polling below stays as fallback.
  useEffect(() => {
    const off = (window.miniflow as any).onTaskUpdate?.(() => refresh());
    return () => off?.();
  }, [refresh]);

  // Poll the list every 5s while there's any non-terminal task.
  useEffect(() => {
    const hasActive = tasks.some(t => t.status === "queued" || t.status === "running");
    if (!hasActive) return;
    const interval = window.setInterval(refresh, 5000);
    return () => window.clearInterval(interval);
  }, [tasks, refresh]);

  const selected = tasks.find(t => t.id === selectedId) ?? null;

  return (
    <div style={{ display: "flex", height: "100%" }}>
      <TaskList
        tasks={tasks}
        selectedId={selectedId}
        onSelect={setSelectedId}
        loading={loading}
        onCreated={(id) => { setSelectedId(id); refresh(); }}
      />
      <div style={{ flex: 1, overflow: "auto", padding: "20px 24px" }}>
        {selected
          ? <TaskDetail taskId={selected.id} onChanged={refresh} />
          : <EmptyDetail />}
      </div>
    </div>
  );
}

function TaskList({
  tasks, selectedId, onSelect, loading, onCreated,
}: {
  tasks: Task[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  loading: boolean;
  onCreated: (id: string) => void;
}) {
  const [prompt, setPrompt] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit() {
    const trimmed = prompt.trim();
    if (!trimmed || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const r = await w.miniflow.createTask(trimmed);
      if (r?.error) {
        setError(r.error);
      } else if (r?.id) {
        setPrompt("");
        onCreated(r.id);
      }
    } catch (e: any) {
      setError(String(e?.message ?? e));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <aside style={{
      width: 320, borderRight: "1px solid #e5e3df", overflow: "auto",
      background: "rgba(255,255,255,0.4)",
    }}>
      <div style={{ padding: "16px 16px 12px" }}>
        <div style={{ fontWeight: 700, fontSize: 15, marginBottom: 8 }}>Tasks</div>
        <textarea
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          onKeyDown={(e) => {
            if ((e.metaKey || e.ctrlKey) && e.key === "Enter") submit();
          }}
          placeholder="What should Uxie do in the background?"
          rows={3}
          style={{
            width: "100%", padding: 8, borderRadius: 6,
            border: "1px solid #e5e3df", fontFamily: "inherit", fontSize: 13,
            resize: "vertical", background: "rgba(255,255,255,0.6)",
          }}
        />
        <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 8 }}>
          <button
            onClick={submit}
            disabled={!prompt.trim() || submitting}
            style={{
              padding: "6px 14px", borderRadius: 6, border: "none",
              background: "#1a1a1a", color: "#fff", fontWeight: 600,
              fontSize: 12, cursor: submitting ? "default" : "pointer",
              opacity: !prompt.trim() || submitting ? 0.5 : 1,
            }}
          >
            {submitting ? "Starting…" : "Run in background"}
          </button>
          <span style={{ fontSize: 10, color: "#888" }}>⌘↵ to submit</span>
        </div>
        {error && (
          <div style={{ marginTop: 8, color: "#d44a4a", fontSize: 11 }}>{error}</div>
        )}
        <div style={{ marginTop: 12, fontSize: 11, color: "#888", lineHeight: 1.4 }}>
          Runs on Uxie's servers and keeps going when your Mac sleeps. Anything that sends, posts or buys asks you first.
        </div>
      </div>
      <hr style={{ border: "none", borderTop: "1px solid #e5e3df", margin: "0 16px" }} />
      <div style={{ padding: "8px 16px", fontSize: 11, textTransform: "uppercase", color: "#888", letterSpacing: 0.05 }}>
        History {loading && "·"}
      </div>
      {tasks.length === 0 ? (
        <div style={{ padding: "8px 16px 16px", fontSize: 12, color: "#888" }}>
          No tasks yet — type a prompt above and click Run.
        </div>
      ) : (
        tasks.map((t) => (
          <button
            key={t.id}
            onClick={() => onSelect(t.id)}
            style={{
              display: "block", width: "100%", textAlign: "left",
              padding: "10px 16px", border: "none",
              background: selectedId === t.id ? "rgba(0,0,0,0.06)" : "transparent",
              cursor: "pointer", borderBottom: "1px solid rgba(0,0,0,0.04)",
            }}
          >
            <div style={{
              fontSize: 13, color: "#1a1a1a", marginBottom: 4,
              overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
            }}>
              {t.prompt}
            </div>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <StatusPill task={t} />
              <span style={{ fontSize: 11, color: "#888" }}>
                {t.agent_name ? `${t.agent_name} · ` : ""}{formatRelative(t.created_at)}
              </span>
            </div>
          </button>
        ))
      )}
    </aside>
  );
}

function EmptyDetail() {
  return (
    <div style={{ color: "#888", fontSize: 13, paddingTop: 40 }}>
      Select a task to see its plan, tool calls, and result.
    </div>
  );
}

function TaskDetail({ taskId, onChanged }: { taskId: string; onChanged: () => void }) {
  const [task, setTask] = useState<Task | null>(null);
  const pollTimer = useRef<number | null>(null);

  const fetchOnce = useCallback(async () => {
    const r = await w.miniflow.getTask(taskId);
    if (r && !r.error) setTask(r);
    return r;
  }, [taskId]);

  // Initial fetch + adaptive polling.
  useEffect(() => {
    let cancelled = false;

    async function loop() {
      while (!cancelled) {
        const r = await fetchOnce();
        const status = r?.status;
        if (cancelled) return;
        if (status === "completed" || status === "failed" || status === "cancelled") {
          return;
        }
        // Active task: poll every 2s. Refresh the parent list too so the
        // sidebar status pill updates without re-renders cascading from poll.
        await new Promise(r => { pollTimer.current = window.setTimeout(r, 2000); });
        onChanged();
      }
    }
    loop();
    return () => {
      cancelled = true;
      if (pollTimer.current) window.clearTimeout(pollTimer.current);
    };
  }, [taskId, fetchOnce, onChanged]);

  if (!task) {
    return <div style={{ color: "#888", fontSize: 13 }}>Loading…</div>;
  }

  async function cancel() {
    if (!confirm("Cancel this task? Anything already in-flight will finish.")) return;
    await w.miniflow.cancelTask(taskId);
    await fetchOnce();
    onChanged();
  }

  const isTerminal = ["completed", "failed", "cancelled"].includes(task.status);

  return (
    <div>
      <header style={{
        display: "flex", justifyContent: "space-between", alignItems: "flex-start",
        gap: 12, marginBottom: 4,
      }}>
        <div style={{ flex: 1 }}>
          <h2 style={{ fontSize: 18, marginBottom: 6 }}>{task.prompt}</h2>
          <div style={{ fontSize: 12, color: "#666" }}>
            {[task.agent_name, `started ${formatRelative(task.created_at)}`, task.waiting ? waitingText(task.waiting_for) : ""]
              .filter(Boolean).join(" · ")}
          </div>
        </div>
        <StatusPill task={task} />
      </header>

      {!isTerminal && (
        <button onClick={cancel} style={btnSecondary}>Cancel</button>
      )}

      {task.error && (
        <div style={{
          marginTop: 16, padding: 12, borderRadius: 6,
          border: "1px solid #d44a4a", color: "#d44a4a", fontSize: 13,
        }}>
          {task.error}
        </div>
      )}

      <ComputerPanel task={task} onResolved={() => fetchOnce().then(() => onChanged())} />

      {task.result_md && (
        <section style={{ marginTop: 24 }}>
          <h3 style={sectionLabel}>Result</h3>
          <pre style={preBox}>{task.result_md}</pre>
        </section>
      )}

      {task.events && task.events.length > 0 && (
        <section style={{ marginTop: 24 }}>
          <h3 style={sectionLabel}>Activity</h3>
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {task.events.map(ev =>
              <EventRow
                key={ev.seq}
                ev={ev}
                taskId={task.id}
                isPending={isPendingApproval(task.events!, ev)}
                onResolved={() => fetchOnce().then(() => onChanged())}
              />
            )}
          </div>
        </section>
      )}
    </div>
  );
}

// Live view of a task's cloud computer (computer_use.py). Shown while the
// session is active; "Take over" opens the interactive stream in the browser.
function ComputerPanel({ task, onResolved }: { task: Task; onResolved: () => void }) {
  const events = task.events ?? [];
  const session = [...events].reverse().find(e => e.kind === "computer_session");
  if (!session) return null;
  const callId = session.data?.call_id;
  const done = events.some(e => (e.kind === "computer_result" || e.kind === "computer_done")
    && e.data?.call_id === callId && e.seq > session.seq);
  const active = !done && !["completed", "failed", "cancelled"].includes(task.status);
  const takeover = [...events].reverse().find(e => e.kind === "approval_needed"
    && e.data?.name === "computer_takeover" && isPendingApproval(events, e));

  return (
    <section style={{ marginTop: 24 }}>
      <h3 style={sectionLabel}>Computer</h3>
      <div style={{ padding: 16, borderRadius: 8, border: "1px solid #e5e3df", background: "rgba(255,255,255,0.6)" }}>
        <div style={{ fontSize: 12, color: "#666", marginBottom: 12 }}>{session.data?.goal}</div>
        {active ? (
          <>
            {takeover && (
              <div style={{
                fontSize: 13, padding: 12, borderRadius: 8, marginBottom: 12,
                background: "rgba(244, 162, 27, 0.08)", border: "1px solid rgba(244, 162, 27, 0.4)",
              }}>
                {takeover.data?.summary?.replace(/^Take over: /, "") || "Uxie needs you to take over."}
              </div>
            )}
            <div style={{
              position: "relative", width: "100%", aspectRatio: "16 / 10", borderRadius: 6, overflow: "hidden",
              border: "1px solid rgba(0,0,0,0.06)", background: "rgba(0,0,0,0.04)",
            }}>
              <iframe
                title="Live view of the agent's computer"
                src={session.data?.view_url}
                style={{ position: "absolute", inset: 0, width: "100%", height: "100%", border: "none" }}
              />
            </div>
            <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
              <button style={btnPrimary} onClick={() => w.miniflow.openExternal(session.data?.control_url)}>
                Take over
              </button>
              {takeover && (
                <button style={btnSecondary} onClick={async () => {
                  await w.miniflow.approveTask(task.id, takeover.data?.id, true, null);
                  onResolved();
                }}>
                  I'm done — continue
                </button>
              )}
            </div>
            <div style={{ fontSize: 11, color: "#888", marginTop: 8 }}>
              Take over opens the computer in your browser so you can sign in or finish a step yourself.
            </div>
          </>
        ) : (
          <div style={{ fontSize: 12, color: "#888" }}>Session ended. The computer was shut down.</div>
        )}
      </div>
    </section>
  );
}

function isPendingApproval(events: TaskEvent[], target: TaskEvent): boolean {
  if (target.kind !== "approval_needed") return false;
  const targetId = target.data?.id;
  if (!targetId) return false;
  // Pending if no later approval_resolved event exists for the same id.
  return !events.some(
    e => e.kind === "approval_resolved" && e.data?.id === targetId,
  );
}

function EventRow({
  ev, taskId, isPending, onResolved,
}: {
  ev: TaskEvent;
  taskId?: string;
  isPending?: boolean;
  onResolved?: () => void;
}) {
  const baseStyle: React.CSSProperties = {
    fontSize: 12, padding: "8px 10px", borderRadius: 6,
    background: "rgba(0,0,0,0.03)", border: "1px solid rgba(0,0,0,0.06)",
  };

  switch (ev.kind) {
    case "approval_needed":
      return (
        <ApprovalRow
          ev={ev}
          taskId={taskId!}
          isPending={!!isPending}
          onResolved={onResolved!}
        />
      );
    case "approval_resolved": {
      const ok = !!ev.data?.approved;
      return (
        <div style={{ ...baseStyle, background: ok ? "rgba(58, 140, 106, 0.06)" : "rgba(212, 74, 74, 0.06)" }}>
          <span style={{
            color: ok ? "#3a8c6a" : "#d44a4a",
            textTransform: "uppercase", fontSize: 10, letterSpacing: 0.05, fontWeight: 600,
          }}>
            {ok ? "APPROVED" : "DECLINED"}
          </span>{" "}
          <span style={{ color: "#666" }}>{ev.data?.name}</span>
        </div>
      );
    }
  }

  switch (ev.kind) {
    case "computer_actions": {
      const acts: any[] = ev.data?.actions ?? [];
      return (
        <div style={baseStyle}>
          <span style={{ color: "#7a5cd1", textTransform: "uppercase", fontSize: 10, letterSpacing: 0.05 }}>
            COMPUTER
          </span>{" "}
          <span style={{ color: "#666" }}>{acts.map(a => a.action.replace(/_/g, " ")).join(" → ")}</span>
        </div>
      );
    }
    case "computer_session":
      return (
        <div style={baseStyle}>
          <span style={{ color: "#888", textTransform: "uppercase", fontSize: 10, letterSpacing: 0.05 }}>
            STEP
          </span>{" "}
          {ev.data?.resumed ? "reconnected to cloud computer" : "started a cloud computer"}
        </div>
      );
    case "computer_takeover":
    case "computer_done":
    case "computer_result":
      return null; // surfaced in the Computer panel / Result section
    case "step_start":
      return (
        <div style={baseStyle}>
          <span style={{ color: "#888", textTransform: "uppercase", fontSize: 10, letterSpacing: 0.05 }}>
            STEP
          </span>{" "}
          {ev.data?.step ?? "started"}
        </div>
      );
    case "thinking":
      return (
        <div style={{ ...baseStyle, background: "rgba(51, 103, 214, 0.06)" }}>
          <span style={{ color: "#3367d6", textTransform: "uppercase", fontSize: 10, letterSpacing: 0.05 }}>
            THINKING
          </span>{" "}
          {ev.data?.text || ""}
        </div>
      );
    case "tool_call":
      return (
        <div style={{ ...baseStyle, background: "rgba(58, 140, 106, 0.06)" }}>
          <span style={{ color: "#3a8c6a", textTransform: "uppercase", fontSize: 10, letterSpacing: 0.05 }}>
            CALL
          </span>{" "}
          <code style={{ fontSize: 12 }}>{ev.data?.name}({Object.keys(ev.data?.args || {}).join(", ")})</code>
          {ev.data?.rejected && (
            <span style={{ marginLeft: 8, color: "#d44a4a", fontSize: 11 }}>(rejected — not allowed in background)</span>
          )}
        </div>
      );
    case "tool_result":
      return (
        <div style={baseStyle}>
          <span style={{ color: ev.data?.ok ? "#3a8c6a" : "#d44a4a", textTransform: "uppercase", fontSize: 10, letterSpacing: 0.05 }}>
            {ev.data?.ok ? "RESULT" : "ERR"}
          </span>{" "}
          <span style={{ color: "#666" }}>{ev.data?.name}</span>
          {ev.data?.result_preview && (
            <pre style={{ marginTop: 4, fontSize: 11, color: "#444", whiteSpace: "pre-wrap" }}>
              {String(ev.data.result_preview).slice(0, 600)}
            </pre>
          )}
        </div>
      );
    case "final_text":
      return null; // shown above in the Result section
    case "error":
      return (
        <div style={{ ...baseStyle, background: "rgba(212, 74, 74, 0.06)", color: "#d44a4a" }}>
          <span style={{ textTransform: "uppercase", fontSize: 10, letterSpacing: 0.05 }}>ERROR</span>{" "}
          {ev.data?.message || JSON.stringify(ev.data)}
        </div>
      );
    default:
      return null;
  }
}

function ApprovalRow({
  ev, taskId, isPending, onResolved,
}: {
  ev: TaskEvent;
  taskId: string;
  isPending: boolean;
  onResolved: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const initialArgs = ev.data?.args ?? {};
  const [edited, setEdited] = useState<Record<string, string>>(() =>
    Object.fromEntries(Object.entries(initialArgs).map(([k, v]) => [k, String(v ?? "")]))
  );

  async function decide(approved: boolean) {
    if (busy) return;
    setBusy(true); setError(null);
    try {
      // Diff edited values against the original args so we only ship
      // fields the user actually changed.
      const diff: Record<string, string> = {};
      for (const [k, v] of Object.entries(edited)) {
        if (String(initialArgs[k] ?? "") !== v) diff[k] = v;
      }
      const r = await (window.miniflow as any).approveTask(
        taskId, ev.data?.id, approved, approved && Object.keys(diff).length ? diff : null,
      );
      if (r?.error) setError(r.error);
      else if (r?.ok === false) setError(r.reason || "could not resolve");
      onResolved();
    } catch (e: any) {
      setError(String(e?.message ?? e));
    } finally {
      setBusy(false);
    }
  }

  const baseStyle: React.CSSProperties = {
    fontSize: 12, padding: 12, borderRadius: 8,
    background: isPending ? "rgba(244, 162, 27, 0.08)" : "rgba(0,0,0,0.03)",
    border: isPending ? "1px solid rgba(244, 162, 27, 0.4)" : "1px solid rgba(0,0,0,0.06)",
  };

  return (
    <div style={baseStyle}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 6 }}>
        <span style={{
          color: isPending ? "#b87100" : "#888",
          textTransform: "uppercase", fontSize: 10, letterSpacing: 0.05, fontWeight: 700,
        }}>
          {isPending ? "APPROVAL NEEDED" : "APPROVAL"}
        </span>
        <code style={{ fontSize: 11, color: "#666" }}>{ev.data?.name}</code>
      </div>
      <div style={{ marginBottom: 8, fontSize: 13 }}>
        {ev.data?.summary || JSON.stringify(ev.data?.args || {})}
      </div>

      {isPending && Object.entries(initialArgs).length > 0 && (
        <details style={{ marginBottom: 8 }}>
          <summary style={{ fontSize: 11, color: "#666", cursor: "pointer" }}>
            Edit params before approving
          </summary>
          <div style={{ display: "flex", flexDirection: "column", gap: 6, marginTop: 6 }}>
            {Object.entries(edited).map(([k, v]) => (
              <label key={k} style={{ display: "flex", flexDirection: "column", gap: 2, fontSize: 11 }}>
                <span style={{ color: "#888" }}>{k}</span>
                {(typeof initialArgs[k] === "string" && initialArgs[k].length > 60)
                  ? <textarea
                      value={v} onChange={(e) => setEdited({ ...edited, [k]: e.target.value })}
                      rows={3}
                      style={{ padding: 6, borderRadius: 4, border: "1px solid #ddd", fontFamily: "inherit", fontSize: 12 }}
                    />
                  : <input
                      value={v} onChange={(e) => setEdited({ ...edited, [k]: e.target.value })}
                      style={{ padding: 6, borderRadius: 4, border: "1px solid #ddd", fontSize: 12 }}
                    />}
              </label>
            ))}
          </div>
        </details>
      )}

      {isPending && (
        <div style={{ display: "flex", gap: 8 }}>
          <button onClick={() => decide(true)} disabled={busy} style={{
            padding: "6px 14px", borderRadius: 6, border: "none",
            background: "#1a1a1a", color: "#fff", fontWeight: 600, fontSize: 12, cursor: "pointer",
          }}>{ev.data?.name === "computer_takeover" ? "I'm done" : "Approve"}</button>
          <button onClick={() => decide(false)} disabled={busy} style={{
            padding: "6px 14px", borderRadius: 6, border: "1px solid #ccc",
            background: "transparent", fontSize: 12, cursor: "pointer",
          }}>{ev.data?.name === "computer_takeover" ? "Stop" : "Decline"}</button>
        </div>
      )}
      {error && (
        <div style={{ marginTop: 6, fontSize: 11, color: "#d44a4a" }}>{error}</div>
      )}
    </div>
  );
}


const btnPrimary: React.CSSProperties = {
  padding: "8px 14px", borderRadius: 6, border: "none",
  background: "#1a1a1a", color: "#fff", fontWeight: 600, cursor: "pointer", fontSize: 13,
};

const btnSecondary: React.CSSProperties = {
  marginTop: 8, padding: "6px 12px", borderRadius: 6,
  border: "1px solid #ccc", background: "transparent", color: "#1a1a1a",
  cursor: "pointer", fontSize: 12,
};

const sectionLabel: React.CSSProperties = {
  fontSize: 11, textTransform: "uppercase", letterSpacing: 0.05,
  color: "#888", fontWeight: 700, marginBottom: 8,
};

const preBox: React.CSSProperties = {
  whiteSpace: "pre-wrap", fontFamily: "inherit", fontSize: 13,
  padding: 12, borderRadius: 6, background: "rgba(0,0,0,0.04)",
  border: "1px solid rgba(0,0,0,0.06)", overflow: "auto",
};
