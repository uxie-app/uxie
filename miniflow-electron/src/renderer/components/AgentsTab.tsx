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

const api = () => window.miniflow as any;

export function AgentsTab() {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [tools, setTools] = useState<ToolInfo[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [role, setRole] = useState("");

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
    if (!name.trim()) return;
    try {
      const a = await api().createAgent({ name: name.trim(), role: role.trim() || null });
      setName(""); setRole("");
      await refresh();
      setSelectedId(a.id);
    } catch (e: any) {
      setErr(e?.message ?? String(e));
    }
  }

  const selected = agents.find(a => a.id === selectedId) ?? null;

  return (
    <div className="home">
      <h1>Agents</h1>
      <div className="info-msg" style={{ marginBottom: 18 }}>
        Specialized agents with their own instructions and permissions. Uxie picks the
        right one for each background task, or say its name ("ask my Research agent to…").
      </div>

      <div className="section" style={{
        background: "var(--card-bg)", border: "1px solid var(--card-border)",
        borderRadius: 12, padding: 16,
      }}>
        <div className="stack" style={{ gap: 10 }}>
          <div className="field" style={{ marginBottom: 0 }}>
            <label htmlFor="a-name">Name</label>
            <input id="a-name" type="text" value={name} placeholder="Research"
                   maxLength={64} onChange={(e) => setName(e.target.value)} />
          </div>
          <div className="field" style={{ marginBottom: 0 }}>
            <label htmlFor="a-role">What it's for</label>
            <input id="a-role" type="text" value={role} placeholder="Research companies and markets"
                   maxLength={500} onChange={(e) => setRole(e.target.value)} />
          </div>
          <div className="row">
            <button className="btn-primary" onClick={create} disabled={!name.trim()}>Create agent</button>
          </div>
        </div>
        {err && <div className="error-msg">{err}</div>}
      </div>

      <div className="section">
        {agents.map(a => (
          <div key={a.id} style={{
            border: "1px solid var(--card-border)", borderRadius: 10, padding: 12, marginBottom: 8,
            background: a.id === selectedId ? "var(--card-bg)" : undefined,
          }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", cursor: "pointer" }}
                 onClick={() => setSelectedId(a.id === selectedId ? null : a.id)}>
              <div>
                <strong>{a.name}</strong>{a.is_default && <span style={{ fontSize: 11, color: "#888" }}> · default</span>}
                <div style={{ fontSize: 12, color: "#666" }}>{a.role}</div>
              </div>
              <span style={{ fontSize: 12, color: "#888" }}>{a.id === selectedId ? "Close" : "Edit"}</span>
            </div>
            {selected?.id === a.id && (
              <AgentEditor agent={a} tools={tools} onChanged={refresh}
                           onDeleted={() => { setSelectedId(null); refresh(); }} onError={setErr} />
            )}
          </div>
        ))}
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

  async function save() {
    setSaving(true);
    try {
      await api().updateAgent(agent.id, { instructions: instructions || null, tool_policy: policy });
      onChanged();
    } catch (e: any) {
      onError(e?.message ?? String(e));
    } finally {
      setSaving(false);
    }
  }

  async function remove() {
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
  }

  return (
    <div className="stack" style={{ gap: 10, marginTop: 12 }}>
      <div className="field" style={{ marginBottom: 0 }}>
        <label htmlFor={`i-${agent.id}`}>Standing instructions</label>
        <textarea id={`i-${agent.id}`} rows={3} maxLength={4000} value={instructions}
                  placeholder="e.g. Always cite sources. Keep summaries under 10 bullets."
                  onChange={(e) => setInstructions(e.target.value)} />
      </div>
      <div>
        <label style={{ display: "block", marginBottom: 6 }}>Permissions</label>
        {tools.map(t => (
          <div key={t.name} style={{ display: "flex", justifyContent: "space-between", fontSize: 12, padding: "3px 0" }}>
            <span>{t.name}</span>
            <select value={policy[t.name] ?? "default"} onChange={(e) => setDecision(t.name, e.target.value)}>
              <option value="default">Default ({DECISION_LABEL[t.default]})</option>
              <option value="allow">Allow</option>
              <option value="require_approval">Ask me</option>
              <option value="deny">Never</option>
            </select>
          </div>
        ))}
      </div>
      <div className="row">
        <button className="btn-primary" onClick={save} disabled={saving}>{saving ? "Saving…" : "Save"}</button>
        {!agent.is_default && <button className="btn-secondary" onClick={remove}>Delete</button>}
      </div>
    </div>
  );
}
