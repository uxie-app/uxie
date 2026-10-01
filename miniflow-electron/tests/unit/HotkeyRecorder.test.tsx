/**
 * @jest-environment jsdom
 */
import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { HotkeySettings } from "../../src/renderer/components/HotkeyRecorder";

const DEFAULTS = {
  dictation: { mode: "hold_to_talk", modifier: "fn", key: null },
  command: { mode: "press_to_toggle", modifier: "option", key: "space" },
};

function installMockApi() {
  let hotkey: any = JSON.parse(JSON.stringify(DEFAULTS));
  const api = {
    getHotkey: jest.fn(async () => hotkey),
    setHotkey: jest.fn(async (hk: any) => { hotkey = hk; return hk; }),
    resetHotkey: jest.fn(async () => {
      hotkey = JSON.parse(JSON.stringify(DEFAULTS));
      return hotkey;
    }),
  };
  (window as any).miniflow = api;
  return api;
}

// The dictation recorder is the first aria-pressed toggle, command the second.
const dictationRecorder = () => screen.getAllByRole("button", { pressed: false })[0];

describe("HotkeySettings", () => {
  beforeEach(() => installMockApi());

  it("renders the current hotkey (default Fn)", async () => {
    render(<HotkeySettings />);
    await waitFor(() => expect(screen.getByText("Fn")).toBeInTheDocument());
  });

  it("reset button calls resetHotkey", async () => {
    const api = installMockApi();
    render(<HotkeySettings />);
    await screen.findByText("Fn");
    fireEvent.click(screen.getByText("Reset all to defaults"));
    await waitFor(() => expect(api.resetHotkey).toHaveBeenCalled());
  });

  it("enters listening mode on recorder click", async () => {
    render(<HotkeySettings />);
    await screen.findByText("Fn");
    fireEvent.click(dictationRecorder());
    await waitFor(() =>
      expect(screen.getByText(/Press any combination/i)).toBeInTheDocument()
    );
  });

  it("captures ⌘ + D for dictation and saves both bindings", async () => {
    const api = installMockApi();
    render(<HotkeySettings />);
    await screen.findByText("Fn");
    fireEvent.click(dictationRecorder());
    await screen.findByText(/Press any combination/i);
    fireEvent.keyDown(window, { code: "KeyD", key: "d", metaKey: true });
    await waitFor(() =>
      expect(api.setHotkey).toHaveBeenCalledWith({
        dictation: { mode: "hold_to_talk", modifier: "cmd", key: "d" },
        command: DEFAULTS.command,
      })
    );
  });

  it("rejects a bare letter with no modifier", async () => {
    const api = installMockApi();
    render(<HotkeySettings />);
    await screen.findByText("Fn");
    fireEvent.click(dictationRecorder());
    await screen.findByText(/Press any combination/i);
    fireEvent.keyDown(window, { code: "KeyA", key: "a" });
    await waitFor(() =>
      expect(screen.getByText(/must include exactly one modifier/i)).toBeInTheDocument()
    );
    expect(api.setHotkey).not.toHaveBeenCalled();
  });

  it("Escape cancels listening without saving", async () => {
    const api = installMockApi();
    render(<HotkeySettings />);
    await screen.findByText("Fn");
    fireEvent.click(dictationRecorder());
    await screen.findByText(/Press any combination/i);
    fireEvent.keyDown(window, { key: "Escape", code: "Escape" });
    await waitFor(() => expect(screen.getByText("Fn")).toBeInTheDocument());
    expect(api.setHotkey).not.toHaveBeenCalled();
  });
});
