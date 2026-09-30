import React, { useEffect, useState } from "react";
import { Sidebar } from "./components/Sidebar";
import { HomeTab } from "./components/HomeTab";
import { DictionaryTab } from "./components/DictionaryTab";
import { SnippetsTab } from "./components/SnippetsTab";
import { MeetingsTab } from "./components/MeetingsTab";
import { TasksTab } from "./components/TasksTab";
import { BriefingsTab } from "./components/BriefingsTab";
import { SettingsModal } from "./components/SettingsModal";
import { Onboarding } from "./components/Onboarding";
import { useAudioCapture } from "./audio";

export type SidebarTab = "home" | "tasks" | "briefings" | "meetings" | "dictionary" | "snippets";

export function App() {
  const [tab, setTab] = useState<SidebarTab>("home");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [showOnboarding, setShowOnboarding] = useState<boolean | null>(null); // null = loading
  const [agentStatus, setAgentStatus] = useState<string>("idle");
  const [userName] = useState<string>("");
  const { capturing, mode: captureMode } = useAudioCapture();
  const isListening = capturing;
  const isProcessing = agentStatus === "processing";

  // Show onboarding if not signed in OR if permissions are missing.
  useEffect(() => {
    (async () => {
      try {
        const user = await (window.miniflow as any).getUxieUser();
        if (!user?.access_token) {
          setShowOnboarding(true);
          return;
        }
        const perms = (await (window.miniflow as any).getPermissions()) as { status: string }[];
        const anyMissing = perms.some((p) => p.status !== "granted");
        setShowOnboarding(anyMissing);
      } catch {
        setShowOnboarding(true);
      }
    })();
  }, []);

  useEffect(() => {
    const offStatus = window.miniflow.onAgentStatus((s) => setAgentStatus(s));
    const offAct = window.miniflow.onAction((a) => {
      if (a.action === "dictation" && a.success) {
        window.miniflow.requestType(a.message);
      }
    });
    // Clicking the meeting notification flips us to the Meetings tab.
    const offReveal = (window.miniflow as any).onMeetingsReveal?.(() =>
      setTab("meetings")
    );
    const offTasksReveal = (window.miniflow as any).onTasksReveal?.(() =>
      setTab("tasks")
    );
    return () => { offStatus(); offAct(); offReveal?.(); offTasksReveal?.(); };
  }, []);

  function closeOnboarding() {
    setShowOnboarding(false);
  }

  // Don't render anything until we know auth state
  if (showOnboarding === null) return null;

  if (showOnboarding) {
    return <Onboarding onDone={closeOnboarding} />;
  }

  return (
    <div className="app">
      <Sidebar
        activeTab={tab}
        onTab={setTab}
        isListening={isListening}
        onSettings={() => setSettingsOpen(true)}
      />
      <div className="content-wrap">
        <div className="content">
          {tab === "home" && (
            <HomeTab
              userName={userName}
              isListening={isListening}
              isProcessing={isProcessing}
              captureMode={captureMode === "meeting" ? "dictation" : captureMode}
            />
          )}
          {tab === "tasks"      && <TasksTab />}
          {tab === "briefings"  && <BriefingsTab />}
          {tab === "meetings"   && <MeetingsTab />}
          {tab === "dictionary" && <DictionaryTab />}
          {tab === "snippets"   && <SnippetsTab />}
        </div>
      </div>
      {settingsOpen && <SettingsModal onClose={() => setSettingsOpen(false)} />}
    </div>
  );
}

function EmptyTab({ title, hint }: { title: string; hint: string }) {
  return (
    <div className="home">
      <h1>{title}</h1>
      <p className="info-msg">{hint}</p>
    </div>
  );
}
