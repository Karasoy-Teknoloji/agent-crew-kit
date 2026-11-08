# Browser MCP: connecting Claude Code to a running Chrome

Status: a reference setup (user scope, pinned versions). Optional, like everything in this repo.

## Purpose and the rule
Use a browser to verify a UI, read a page, or debug a local app that is already running. The team rule (`rules/WORKING-RULES.md:40`) stays: operations go through the API first (for example the license API, PAT in Vault); the browser is only for verifying UI, preferably with Playwright.

## The three tools
| Tool | Use it for | Connection model |
|---|---|---|
| Playwright MCP (`--extension`) | Scripted driving through the accessibility tree, test generation, your logged-in tabs | Browser extension "Playwright Extension" in Chrome/Edge; you pick the tab and approve each connection |
| Chrome DevTools MCP (`--autoConnect`) | Network, console, performance traces, debugging a running local app | Chrome's own remote debugging (Chrome 144+); Chrome asks you to allow the connection |
| Claude in Chrome | Your logged-in sessions, with its own extension permission model | Separate extension; not covered here (see `docs/onboarding.md`) |

## Install (user scope, pinned versions)
```
claude mcp add --scope user playwright -- npx -y @playwright/mcp@0.0.83 --extension
claude mcp add --scope user chrome-devtools -- npx -y chrome-devtools-mcp@1.10.1 --autoConnect
```
Equivalent entries under `mcpServers` in `~/.claude.json`:
```json
{
  "mcpServers": {
    "playwright": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@playwright/mcp@0.0.83", "--extension"]
    },
    "chrome-devtools": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "chrome-devtools-mcp@1.10.1", "--autoConnect"]
    }
  }
}
```
Needs Node.js (LTS) and npm. Restart Claude Code (or run `/mcp`) after adding. Both servers start Chrome-side work only when a tool is first used; adding them does not touch your browser.

## Playwright MCP: connect to your running browser
`--extension` = "Connect to a running browser instance (Edge/Chrome only). Requires the Playwright Extension to be installed" (`npx -y @playwright/mcp@0.0.83 --help`).
1. Install the **Playwright Extension** from the Chrome Web Store (https://chromewebstore.google.com/detail/playwright-extension/mmlmfjhmonkocbjadbfplnigmagldckm) in the Chrome/Edge profile you want to drive. (Older docs and chat call it "Playwright MCP Bridge"; the name in the current README and `--help` is "Playwright Extension".)
2. Keep that browser running. On the first browser tool call, the MCP opens a page where you select which tab Claude may connect to.
3. Approve the connection. Each connection needs your approval by default. The extension UI also shows a `PLAYWRIGHT_MCP_EXTENSION_TOKEN` that skips the prompt when put in the server's `env`; do NOT do this, keep the per-connection approval. (The token is stored in config files: treat it as a secret.)
4. With several profiles it connects to the last used profile that has the extension; pin one with `--profile-dir-name "Profile 1"` (value from "Profile Path" in `chrome://version`). The token is per profile.
5. Several clients can connect at once, each with its own tab group; a tab belongs to one client at a time.

## Chrome DevTools MCP: connect to your running Chrome
`--autoConnect` = automatically connect to a locally running Chrome 144+ (stable channel by default; `--channel=canary|beta|dev` for others), which must have the remote debugging server started via `chrome://inspect/#remote-debugging`.
1. Start Chrome yourself (the normal profile).
2. Open `chrome://inspect/#remote-debugging` and enable remote debugging.
3. On the first tool call Chrome shows a permission dialog; click **Allow** (you can also disallow there).
4. Notes from the official docs: it connects to Chrome's default profile and can access all open windows in that profile. No other tool may use the same debugging port.

Alternative without autoConnect: `--browser-url=http://127.0.0.1:9222`. Close all Chrome instances, then start Chrome with `--remote-debugging-port=9222 --user-data-dir=<separate dir>` (Linux: `/usr/bin/google-chrome --remote-debugging-port=9222 --user-data-dir=/tmp/chrome-profile-stable`). Chrome requires a non-default user data dir with the port, so this is a fresh profile (no logins), which is also safer. Any local application can connect to that open port; do not browse sensitive sites meanwhile. Use this for sandboxed clients, WSL (mirrored networking) or VM-to-host (SSH tunnel to port 9222).

## Check that it works
- `/mcp` in Claude Code lists `playwright` and `chrome-devtools` as connected.
- Playwright: ask "list my browser tabs" (tool `browser_tabs`), then select the tab in the page the MCP opens.
- DevTools: official first prompt is "Check the performance of https://developers.chrome.com" (Chrome shows the Allow dialog). Cheaper: ask it to list pages.

## Safety (read before use)
- These tools act inside your real, logged-in browser (cookies, sessions). DevTools MCP can "inspect, debug, and modify any browser or DevTools data" (official security disclaimer).
- The agent never types passwords or secrets into pages, never creates tokens or accounts and never changes security settings through the UI. Prepare it, then you do the final click.
- Do not let it read pages that display secrets or personal data. Page text and screenshots go to the model vendor's cloud; the team decision is no raw personal data to the model.
- Chrome DevTools MCP also sends usage statistics to Google by default and may send trace URLs to the CrUX API. Opt out with `--no-usage-statistics` (or env `CHROME_DEVTOOLS_MCP_NO_USAGE_STATISTICS`) and `--no-performance-crux`. Add them to the `args` if you want that; the pinned setup above does not.
- Close the tabs the agent opened when done. Never reuse tabs from another session or another client.
- Prefer a separate Chrome profile for agent work; with `--autoConnect` all windows of the default profile are reachable.

## Pinned versions and bumping
Both packages are pinned (`@playwright/mcp@0.0.83`, `chrome-devtools-mcp@1.10.1`) because `npx -y ...@latest` would run new code with access to your browser without review. To bump: read the changelog / release notes on GitHub (microsoft/playwright-mcp, ChromeDevTools/chrome-devtools-mcp) and `npx -y <pkg>@<new> --help` for changed flags, then update both the `claude mcp add` line and this file in one commit.

## Troubleshooting (from the official docs)
- `ProtocolError: Network.enable timed out` with `--autoConnect`: the handshake with Chrome failed; check that remote debugging is enabled, you clicked Allow and no other tool uses the port.
- `--autoConnect` needs Chrome 144+; up to Chrome 149 frozen/unloaded tabs can break the connection (the server forces all tabs to load), so avoid hundreds of open tabs.
- macOS `EPERM` / `Could not find DevToolsActivePort`: the terminal/host app lacks file access to Google Chrome (System Settings > Privacy & Security > Files & Folders). No restart needed.
- `Target closed`: Chrome failed to start; close running Chrome instances and use the latest stable Chrome.
- Sandboxed client cannot start Chrome: start Chrome manually and use `--browser-url`.
- VM to host refused (Host header validation): SSH-tunnel port 9222 and point `--browser-url` at `http://127.0.0.1:9222`.

Sources: https://github.com/microsoft/playwright-mcp (README), https://github.com/microsoft/playwright/tree/main/packages/extension, https://github.com/ChromeDevTools/chrome-devtools-mcp (README, `docs/advanced-usage.md`, `docs/troubleshooting.md`).
