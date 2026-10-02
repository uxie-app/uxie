import React, { useEffect, useState } from "react";

type Decision = "allow" | "require_approval" | "deny";
type Agent = {
  id: string;
  name: string;
  role: string | null;
  instructions: string | null;
  is_default: boolean;
  tool_policy: Record<string, Decision>;
};
type ToolInfo = { name: string; default: Decision };

const DECISION_LABEL: Record<Decision, string> = {
  allow: "Allow",
  require_approval: "Ask me",
  deny: "Never",
};

// Human names for tools in the permissions grid.
const TOOL_LABEL: Record<string, string> = {
  gmail_search: "Search Gmail", gmail_read: "Read email", gmail_send: "Send email",
  gmail_reply: "Reply to email", gmail_draft: "Draft email",
  calendar_list_events: "Read calendar", calendar_check_availability: "Check availability",
  calendar_create_event: "Create events",
  drive_search: "Search Drive", drive_read: "Read Drive files", drive_list: "List Drive files",
  slack_search: "Search Slack", slack_list_channels: "List Slack channels",
  slack_read_channel: "Read Slack", slack_send_message: "Post to Slack",
  use_computer: "Use a cloud computer",
};

const api = () => window.miniflow as any;

export function AgentsTab() {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [tools, setTools] = useState<ToolInfo[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [role, setRole] = useState("");
  const [creating, setCreating] = useState(false);

  async function refresh() {
    try {
      const r = await api().listAgents();
      setAgents(r.agents ?? []);
      setTools(r.tools ?? []);
      setErr(null);
    } catch (e: any) {
      setErr(e?.message ?? String(e));
    }
  }
  useEffect(() => { refresh(); }, []);

  async function create() {
    if (!name.trim() || creating) return;
    setCreating(true);
    try {
      const a = await api().createAgent({ name: name.trim(), role: role.trim() || null });
      setName(""); setRole("");
      await refresh();
      setSelectedId(a.id);
    } catch (e: any) {
      setErr(e?.message ?? String(e));
    } finally {
      setCreating(false);
    }
  }

  const selected = agents.find(a => a.id === selectedId) ?? null;

  return (
    <div style={{ display: "flex", height: "100%" }}>
      <aside style={{
        width: 280, borderRight: "1px solid #e5e3df", overflow: "auto",
        background: "rgba(255,255,255,0.4)",
      }}>
        <div style={{ padding: "16px 16px 12px" }}>
          <div style={{ fontWeight: 700, fontSize: 15, marginBottom: 4 }}>Agents</div>
          <div style={{ fontSize: 11, color: "#888", lineHeight: 1.4, marginBottom: 12 }}>
            Uxie picks the right agent for each background task, or say its name: "ask my Research agent to…"
          </div>
          <label htmlFor="a-name" style={fieldLabel}>Name</label>
          <input id="a-name" type="text" value={name} placeholder="Research" maxLength={64}
                 onChange={(e) => setName(e.target.value)} style={{ ...input, marginBottom: 8 }} />
          <label htmlFor="a-role" style={fieldLabel}>What it's for</label>
          <input id="a-role" type="text" value={role} placeholder="Research companies and markets" maxLength={500}
                 onChange={(e) => setRole(e.target.value)}
                 onKeyDown={(e) => { if (e.key === "Enter") create(); }}
                 style={{ ...input, marginBottom: 8 }} />
          <button onClick={create} disabled={!name.trim() || creating}
                  style={{ ...btnPrimary, opacity: !name.trim() || creating ? 0.5 : 1 }}>
            Create agent
          </button>
          {err && <div style={{ marginTop: 8, color: "#d44a4a", fontSize: 11 }}>{err}</div>}
        </div>
        <hr style={{ border: "none", borderTop: "1px solid #e5e3df", margin: "0 16px" }} />
        {agents.map(a => (
          <button key={a.id} onClick={() => setSelectedId(a.id)} style={{
            display: "block", width: "100%", textAlign: "left", padding: "10px 16px", border: "none",
            borderBottom: "1px solid rgba(0,0,0,0.04)", cursor: "pointer",
            background: a.id === selectedId ? "rgba(0,0,0,0.06)" : "transparent",
          }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8 }}>
              <span style={{ fontSize: 13, fontWeight: 600, color: "#1a1a1a" }}>{a.name}</span>
              {a.is_default && <span style={pill("#5b6878")}>default</span>}
            </div>
            <div style={{
              fontSize: 11, color: "#666", marginTop: 2,
              overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
            }}>
              {a.role || "No description"}
            </div>
          </button>
        ))}
      </aside>
      <div style={{ flex: 1, overflow: "auto", padding: "20px 24px" }}>
        {selected
          ? <AgentEditor key={selected.id} agent={selected} tools={tools} onChanged={refresh}
                         onDeleted={() => { setSelectedId(null); refresh(); }} onError={setErr} />
          : <div style={{ color: "#888", fontSize: 13, fontStyle: "italic", paddingTop: 40 }}>
              Select an agent to edit its instructions and permissions.
            </div>}
      </div>
    </div>
  );
}

function AgentEditor({ agent, tools, onChanged, onDeleted, onError }: {
  agent: Agent;
  tools: ToolInfo[];
  onChanged: () => void;
  onDeleted: () => void;
  onError: (e: string) => void;
}) {
  const [instructions, setInstructions] = useState(agent.instructions ?? "");
  const [policy, setPolicy] = useState<Record<string, Decision>>(agent.tool_policy ?? {});
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  async function save() {
    setSaving(true); setSaved(false);
    try {
      await api().updateAgent(agent.id, { instructions: instructions || null, tool_policy: policy });
      setSaved(true);
      onChanged();
    } catch (e: any) {
      onError(e?.message ?? String(e));
    } finally {
      setSaving(false);
    }
  }

  async function remove() {
    if (!confirm(`Delete the ${agent.name} agent? Its tasks keep running under Uxie.`)) return;
    try {
      await api().deleteAgent(agent.id);
      onDeleted();
    } catch (e: any) {
      onError(e?.message ?? String(e));
    }
  }

  function setDecision(tool: string, value: string) {
    const next = { ...policy };
    if (value === "default") delete next[tool];
    else next[tool] = value as Decision;
    setPolicy(next);
    setSaved(false);
  }

  return (
    <div style={{ maxWidth: 720 }}>
      <header style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 12 }}>
        <div>
          <h2 style={{ fontSize: 18, marginBottom: 4 }}>{agent.name}</h2>
          <div style={{ fontSize: 12, color: "#666" }}>{agent.role || "No description"}</div>
        </div>
        {agent.is_default && <span style={pill("#5b6878")}>default</span>}
      </header>

      <section style={{ marginTop: 24 }}>
        <h3 style={sectionLabel}>Instructions</h3>
        <textarea id={`i-${agent.id}`} aria-label="Standing instructions" rows={4} maxLength={4000}
                  value={instructions}
                  placeholder="e.g. Always cite sources. Keep summaries under 10 bullets."
                  onChange={(e) => { setInstructions(e.target.value); setSaved(false); }}
                  style={{ ...input, width: "100%", resize: "vertical" }} />
      </section>

      <section style={{ marginTop: 24 }}>
        <h3 style={sectionLabel}>Permissions</h3>
        <div style={{ padding: 16, borderRadius: 8, border: "1px solid #e5e3df", background: "rgba(255,255,255,0.6)" }}>
          <div style={{ display: "grid", gridTemplateColumns: "1fr auto", gap: "8px 16px", alignItems: "center" }}>
            {tools.map(t => (
              <React.Fragment key={t.name}>
                <label htmlFor={`p-${agent.id}-${t.name}`} style={{ fontSize: 13, color: "#1a1a1a" }}>
                  {TOOL_LABEL[t.name] ?? t.name}
                </label>
                <select id={`p-${agent.id}-${t.name}`} value={policy[t.name] ?? "default"}
                        onChange={(e) => setDecision(t.name, e.target.value)} style={input}>
                  <option value="default">Default ({DECISION_LABEL[t.default]})</option>
                  <option value="allow">Allow</option>
                  <option value="require_approval">Ask me</option>
                  <option value="deny">Never</option>
                </select>
              </React.Fragment>
            ))}
          </div>
          <div style={{ fontSize: 11, color: "#888", marginTop: 12, lineHeight: 1.4 }}>
            "Ask me" pauses the task until you approve in Tasks. In voice commands, sending always asks first.
          </div>
        </div>
      </section>

      <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 24 }}>
        <button onClick={save} disabled={saving} style={btnPrimary}>{saving ? "Saving…" : "Save"}</button>
        {!agent.is_default && <button onClick={remove} style={btnDanger}>Delete</button>}
        {saved && <span style={{ fontSize: 11, color: "#3a8c6a" }}>Saved</span>}
      </div>
    </div>
  );
}

const pill = (color: string): React.CSSProperties => ({
  fontSize: 11, padding: "2px 8px", borderRadius: 10, background: color + "22", color,
  fontWeight: 600, textTransform: "uppercase", letterSpacing: 0.04,
});
const fieldLabel: React.CSSProperties = { display: "block", fontSize: 11, color: "#888", marginBottom: 4 };
const input: React.CSSProperties = {
  padding: "6px 8px", borderRadius: 6, border: "1px solid #e5e3df", fontFamily: "inherit",
  fontSize: 13, background: "rgba(255,255,255,0.8)", boxSizing: "border-box",
};
const btnPrimary: React.CSSProperties = {
  padding: "8px 14px", borderRadius: 6, border: "none", background: "#1a1a1a", color: "#fff",
  fontWeight: 600, cursor: "pointer", fontSize: 13,
};
const btnDanger: React.CSSProperties = {
  padding: "8px 14px", borderRadius: 6, border: "1px solid #d44a4a", background: "transparent",
  color: "#d44a4a", cursor: "pointer", fontSize: 13,
};
const sectionLabel: React.CSSProperties = {
  fontSize: 11, textTransform: "uppercase", letterSpacing: "0.05em", fontWeight: 700, color: "#888", marginBottom: 8,
};
