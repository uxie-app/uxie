/**
 * @jest-environment jsdom
 */
import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { TasksTab } from "../../src/renderer/components/TasksTab";

const task = {
  id: "t1", prompt: "compare prices", status: "running", agent_name: "Research",
  approval_needed: true, result_md: null, error: null, created_at: new Date().toISOString(), completed_at: null,
  events: [
    { seq: 1, kind: "computer_session", created_at: "", data: { call_id: "c1", goal: "compare prices", view_url: "https://vnc/view", control_url: "https://vnc/control" } },
    { seq: 2, kind: "approval_needed", created_at: "", data: { id: "a1", name: "computer_takeover", args: {}, summary: "Take over: sign in to the shop" } },
  ],
};

function installMockApi() {
  const api = {
    listTasks: jest.fn(async () => ({ tasks: [task] })),
    getTask: jest.fn(async () => task),
    openExternal: jest.fn(async () => {}),
    approveTask: jest.fn(async () => ({ ok: true })),
    onTaskUpdate: jest.fn(() => () => {}),
  };
  (window as any).miniflow = api;
  return api;
}

describe("Tasks computer panel", () => {
  it("shows live view, needs-you status and take-over controls", async () => {
    const api = installMockApi();
    render(<TasksTab />);
    fireEvent.click(await screen.findByText("compare prices", { selector: "div" }));
    const frame = await screen.findByTitle("Live view of the agent's computer");
    expect(frame.getAttribute("src")).toBe("https://vnc/view");
    expect(screen.getAllByText("needs you").length).toBeGreaterThan(0);
    expect(screen.getByText("sign in to the shop")).toBeInTheDocument();

    fireEvent.click(screen.getByText("Take over"));
    expect(api.openExternal).toHaveBeenCalledWith("https://vnc/control");
    fireEvent.click(screen.getByText("I'm done — continue"));
    await waitFor(() => expect(api.approveTask).toHaveBeenCalledWith("t1", "a1", true, null));
  });
});
