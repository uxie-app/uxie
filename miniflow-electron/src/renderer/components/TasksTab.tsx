import React, { useCallback, useEffect, useRef, useState } from "react";

type TaskStatus = "queued" | "running" | "completed" | "failed" | "cancelled";

type TaskEvent = {
  seq: number;
  kind: "step_start" | "tool_call" | "tool_result" | "thinking" | "final_text" | "error"
      | "approval_needed" | "approval_resolved";
  data: any;
  created_at: string;
};

type Task = {
  id: string;
  prompt: string;
  status: TaskStatus;
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
  cancelled: "#999",
};

function StatusPill({ status }: { status: TaskStatus }) {
  const color = STATUS_COLORS[status] ?? "#888";
  return (
    <span style={{
      fontSize: 11, padding: "2px 8px", borderRadius: 10,
      background: color + "22", color, fontWeight: 600,
      textTransform: "uppercase", letterSpacing: 0.04,
    }}>
      {status}
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
          v1.1 supports read-only tasks (search Gmail / Calendar / Drive). Sending and creating actions land in the next release.
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
              <StatusPill status={t.status} />
              <span style={{ fontSize: 11, color: "#888" }}>{formatRelative(t.created_at)}</span>
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
  const [polling, setPolling] = useState(false);
  const pollTimer = useRef<number | null>(null);

  const fetchOnce = useCallback(async () => {
    const r = await w.miniflow.getTask(taskId);
    if (r && !r.error) setTask(r);
    return r;
  }, [taskId]);

  // Initial fetch + adaptive polling.
  useEffect(() => {
    let cancelled = false;
    setPolling(true);

    async function loop() {
      while (!cancelled) {
        const r = await fetchOnce();
        const status = r?.status;
        if (cancelled) return;
        if (status === "completed" || status === "failed" || status === "cancelled") {
          setPolling(false);
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
            {formatRelative(task.created_at)} · {polling && "polling…"}
          </div>
        </div>
        <StatusPill status={task.status} />
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
          }}>Approve</button>
          <button onClick={() => decide(false)} disabled={busy} style={{
            padding: "6px 14px", borderRadius: 6, border: "1px solid #ccc",
            background: "transparent", fontSize: 12, cursor: "pointer",
          }}>Decline</button>
        </div>
      )}
      {error && (
        <div style={{ marginTop: 6, fontSize: 11, color: "#d44a4a" }}>{error}</div>
      )}
    </div>
  );
}


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
