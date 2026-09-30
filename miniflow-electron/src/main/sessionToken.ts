// Keeps the Uxie login token (JWT) in Electron safeStorage — encrypted with
// an OS-held key (macOS Keychain item owned by Uxie.app, Windows DPAPI) —
// instead of plaintext in ~/miniflow/uxie_auth.json. The engine only holds
// the token in memory, so we re-hand it over on every (re)connect.
import { app, safeStorage } from "electron";
import fs from "fs";
import path from "path";
import { invoke } from "./api";

const tokenFile = () => path.join(app.getPath("userData"), "session.enc");

export async function syncSessionToken(): Promise<void> {
  if (!safeStorage.isEncryptionAvailable()) return; // token stays in the engine's file
  try {
    // A fresh login (or a pre-upgrade install) leaves the token in the
    // engine's file. Adopt it: encrypt here, engine strips it from disk.
    const fresh = await invoke<{ token: string | null }>("take_file_token");
    if (fresh?.token) {
      fs.writeFileSync(tokenFile(), safeStorage.encryptString(fresh.token), { mode: 0o600 });
      return;
    }
    if (fs.existsSync(tokenFile())) {
      const token = safeStorage.decryptString(fs.readFileSync(tokenFile()));
      await invoke("set_session_token", { token });
    }
  } catch (e) {
    console.error("[session] token sync failed:", e);
  }
}

export function clearSessionToken(): void {
  try {
    fs.rmSync(tokenFile(), { force: true });
  } catch (e) {
    console.error("[session] clear failed:", e);
  }
}
