import React from "react";
import type { SidebarTab } from "../App";

interface Props {
  activeTab: SidebarTab;
  onTab: (t: SidebarTab) => void;
  isListening: boolean;
  onSettings: () => void;
}

export function Sidebar({ activeTab, onTab, isListening, onSettings }: Props) {
  return (
    <nav className="sidebar">
      <div className="traffic">
        <span className="dot close" onClick={() => window.miniflow.quit()} />
        <span className="dot min" />
        <span className="dot zoom" />
      </div>

      <div className="logo-row">
        <span className="wave-icon">〰</span>
        <span className="logo-text">Miniflow</span>
        <span className="pill-basic">Basic</span>
      </div>

      {isListening && (
        <div className="listening-pill">
          <span className="dot" />
          <span>Listening</span>
        </div>
      )}

      <div className={`nav-item ${activeTab === "home" ? "active" : ""}`} onClick={() => onTab("home")}>
        <span className="icon">⌂</span><span>Home</span>
      </div>
      <div className={`nav-item ${activeTab === "tasks" ? "active" : ""}`} onClick={() => onTab("tasks")}>
        <span className="icon">⚡</span><span>Tasks</span>
      </div>
      <div className={`nav-item ${activeTab === "agents" ? "active" : ""}`} onClick={() => onTab("agents")}>
        <span className="icon">◎</span><span>Agents</span>
      </div>
      <div className={`nav-item ${activeTab === "briefings" ? "active" : ""}`} onClick={() => onTab("briefings")}>
        <span className="icon">☀</span><span>Briefings</span>
      </div>
      <div className={`nav-item ${activeTab === "meetings" ? "active" : ""}`} onClick={() => onTab("meetings")}>
        <span className="icon">📝</span><span>Meetings</span>
      </div>
      <div className={`nav-item ${activeTab === "dictionary" ? "active" : ""}`} onClick={() => onTab("dictionary")}>
        <span className="icon">📖</span><span>Dictionary</span>
      </div>
      <div className={`nav-item ${activeTab === "snippets" ? "active" : ""}`} onClick={() => onTab("snippets")}>
        <span className="icon">✎</span><span>Snippets</span>
      </div>

      <div className="spacer" />

      <div className="pro-card">
        <div className="title-row">
          <span>Upgrade to Pro</span>
          <span className="sparkle">✨</span>
        </div>
        <p>Unlock all connectors, unlimited history & team features.</p>
        <button>Learn more</button>
      </div>

      <hr />

      <button className="sidebar-btn" onClick={onSettings}>
        <span>⚙</span><span>Settings</span>
      </button>
      <button className="sidebar-btn bottom">
        <span>?</span><span>Help</span>
      </button>
    </nav>
  );
}
