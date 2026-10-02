/**
 * @jest-environment jsdom
 */
import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { AgentsTab } from "../../src/renderer/components/AgentsTab";

function installMockApi() {
  const agents: any[] = [{ id: "a0", name: "Uxie", role: "General", instructions: null, is_default: true, tool_policy: {} }];
  const api = {
    listAgents: jest.fn(async () => ({ agents: [...agents], tools: [{ name: "gmail_send", default: "require_approval" }] })),
    createAgent: jest.fn(async (body: any) => {
      const a = { id: "a1", instructions: null, is_default: false, tool_policy: {}, ...body };
      agents.push(a);
      return a;
    }),
    updateAgent: jest.fn(async () => ({})),
    deleteAgent: jest.fn(async () => ({ ok: true })),
  };
  (window as any).miniflow = api;
  return api;
}

describe("AgentsTab", () => {
  it("lists agents and creates a new one", async () => {
    const api = installMockApi();
    render(<AgentsTab />);
    await screen.findByText("Uxie");
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Research" } });
    fireEvent.click(screen.getByText("Create agent"));
    await waitFor(() => expect(api.createAgent).toHaveBeenCalledWith({ name: "Research", role: null }));
    await screen.findByText("Research", { selector: "h2" });
  });

  it("saves a per-tool policy", async () => {
    const api = installMockApi();
    render(<AgentsTab />);
    fireEvent.click(await screen.findByText("Uxie"));
    fireEvent.change(await screen.findByLabelText("Send email"), { target: { value: "deny" } });
    fireEvent.click(screen.getByText("Save"));
    await waitFor(() => expect(api.updateAgent).toHaveBeenCalledWith("a0", { instructions: null, tool_policy: { gmail_send: "deny" } }));
  });
});
