"""100x client: talks JSON-RPC to the installed 100x MCP bridge (stdio) and uses its media.mjs for upload/download.

Credentials stay inside the bridge (macOS keychain `100x-codex`); this module never reads or prints them.
Bridge location: $X100_PLUGIN, else ~/.codex/plugin-sources/100x-mcp/plugins/100x.

CLI (free calls only):  x100.py models [--id 100x-minimax-h3] | balance | task <task_id>
"""
from __future__ import annotations
import json, os, subprocess, sys, threading
from pathlib import Path

PLUGIN = Path(os.environ.get("X100_PLUGIN", Path.home() / ".codex/plugin-sources/100x-mcp/plugins/100x"))
H3 = "100x-minimax-h3"
CARD_MODEL = "100x-nano-banana-pro"
# Node's fetch ignores HTTP(S)_PROXY unless told to; behind a proxy the bridge would otherwise time out.
NODE_ENV = {**os.environ, "NODE_USE_ENV_PROXY": "1"}


class X100Error(RuntimeError):
    def __init__(self, code: str, msg: str, payload=None):
        super().__init__(f"{code}: {msg}")
        self.code, self.payload = code, payload


class Client:
    def __init__(self):
        if not (PLUGIN / "scripts/bridge.mjs").exists():
            sys.exit(f"100x bridge not found at {PLUGIN} (set $X100_PLUGIN)")
        self.p = subprocess.Popen(["node", str(PLUGIN / "scripts/bridge.mjs")], cwd=PLUGIN, text=True,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=NODE_ENV)
        self.n, self.lock = 0, threading.Lock()
        self._rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                 "clientInfo": {"name": "ai-song-ad", "version": "0.1"}})
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        self.p.stdin.flush()

    def _rpc(self, method, params):
        with self.lock:
            self.n += 1
            i = self.n
            self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params}) + "\n")
            self.p.stdin.flush()
            while True:
                line = self.p.stdout.readline()
                if not line:
                    raise X100Error("BRIDGE_EXIT", "100x bridge closed")
                m = json.loads(line)
                if m.get("id") == i:
                    return m

    def call(self, name: str, **args) -> dict:
        """Free/read calls retry twice on a connection failure; paid generate calls never retry here."""
        import time
        for attempt in range(3):
            try:
                return self._call(name, **args)
            except X100Error as e:
                if name.startswith("100x_generate") or e.code not in ("100X_CONNECTION_FAILED", "BRIDGE_EXIT") or attempt == 2:
                    raise
                time.sleep(3 * (attempt + 1))

    def _call(self, name: str, **args) -> dict:
        m = self._rpc("tools/call", {"name": name, "arguments": args})
        if "error" in m:
            raise X100Error("RPC", m["error"].get("message", str(m["error"])))
        r = m.get("result", {})
        val = r.get("structuredContent")
        if val is None:
            txt = "".join(c.get("text", "") for c in r.get("content", []))
            val = json.loads(txt) if txt.strip().startswith(("{", "[")) else {"text": txt}
        if r.get("isError") or (isinstance(val, dict) and "error" in val):
            e = val.get("error", {}) if isinstance(val, dict) else {}
            raise X100Error(e.get("code", "100X_ERROR"), e.get("message", json.dumps(val, ensure_ascii=False)), val)
        return val

    def close(self):
        self.p.terminate()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    # convenience
    def models(self):
        return self.call("100x_list_models")["models"]

    def balance(self) -> float:
        return float(self.call("100x_get_balance")["available"])

    def quote(self, **a) -> dict:
        return self.call("100x_quote", **a)

    def generate(self, kind: str, quote_id: str, key: str, max_credits: float) -> dict:
        return self.call(f"100x_generate_{kind}", quote_id=quote_id, idempotency_key=key, max_credits=max_credits)

    def task(self, task_id: str) -> dict:
        return self.call("100x_get_task", task_id=task_id)


def variant(models: list, model_id: str, mode: str, resolution: str, aspect: str, seconds: int | None = None,
            images: int = 0) -> dict:
    """Return the live catalog variant that allows these settings, or exit with what the catalog offers."""
    m = next((m for m in models if m["id"] == model_id), None)
    if not m or not m.get("available"):
        sys.exit(f"{model_id} is not available in the live 100x catalog; stop (do not switch models)")
    for v in m["variants"]:
        if v["mode"] != mode or resolution.upper() not in [r.upper() for r in v["resolutions"]]:
            continue
        if v["aspect_ratios"] and aspect not in v["aspect_ratios"]:
            continue
        if seconds is not None and v["durations_seconds"] and seconds not in v["durations_seconds"]:
            continue
        if v.get("image_counts") and images not in v["image_counts"]:
            continue
        return v
    sys.exit(f"{model_id}: no live variant for mode={mode} res={resolution} aspect={aspect} sec={seconds} images={images}\n"
             + json.dumps(m["variants"], ensure_ascii=False)[:1500])


def upload(path: Path, seconds: float | None = None) -> str:
    cmd = ["node", str(PLUGIN / "scripts/media.mjs"), "upload", str(path)]
    if seconds is not None:
        cmd.append(f"{seconds:.3f}")
    r = subprocess.run(cmd, capture_output=True, text=True, env=NODE_ENV)
    if r.returncode:
        raise X100Error("UPLOAD", (r.stderr or r.stdout).strip()[:300])
    out = r.stdout.strip()
    try:
        return json.loads(out)["asset_id"]
    except (ValueError, KeyError):
        return out.split()[-1]


def download(url: str, out: Path):
    out.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["node", str(PLUGIN / "scripts/media.mjs"), "download", url, str(out)], capture_output=True, text=True, env=NODE_ENV)
    if r.returncode or not out.exists():
        raise X100Error("DOWNLOAD", (r.stderr or r.stdout).strip()[:300])


def output_urls(task: dict) -> list[str]:
    outs = task.get("outputs") or task.get("result", {}).get("outputs") or []
    urls = []
    for o in outs:
        u = o if isinstance(o, str) else (o.get("url") or o.get("download_url"))
        if u:
            urls.append(u)
    return urls


DONE = {"succeeded", "success", "completed", "complete", "done", "finished"}
FAILED = {"failed", "failure", "error", "cancelled", "canceled", "rejected", "expired"}


def submit(c: Client, kind: str, qargs: dict, entry: dict, budget_left: list | None = None) -> dict:
    """Quote + submit once. `entry` is the ledger row (mutated): keeps quote_id/key/task_id so retries reuse them.
    A row that already has a task_id is never resubmitted."""
    if entry.get("task_id"):
        return entry
    if not entry.get("quote_id"):
        q = c.quote(**qargs)
        entry.update(quote_id=q["quote_id"], credits=q["credits"], quoted_at=q.get("expires_at"))
    if budget_left is not None:
        if entry["credits"] > budget_left[0]:
            raise X100Error("100X_BUDGET_EXCEEDED", f"needs {entry['credits']}, {budget_left[0]} left")
        budget_left[0] -= entry["credits"]
    try:
        r = c.generate(kind, entry["quote_id"], entry["key"], entry["credits"])
    except X100Error as e:
        if e.code in ("100X_QUOTE_CHANGED", "100X_QUOTE_EXPIRED", "QUOTE_EXPIRED"):
            q = c.quote(**qargs)
            entry.update(quote_id=q["quote_id"], credits=q["credits"])
            r = c.generate(kind, entry["quote_id"], entry["key"], entry["credits"])
        else:
            raise
    entry.update(task_id=r.get("task_id") or r.get("id"), status="submitted", submit_resp=r)
    return entry


def poll(c: Client, entries: list[dict], out_for, save_cb=None, timeout_s: int = 3600):
    """Poll submitted rows until each succeeds (downloaded to out_for(entry)) or fails."""
    import time
    t0, wait = time.time(), {}
    while True:
        pending = [e for e in entries if e.get("task_id") and e.get("status") not in ("downloaded", "failed", "download_missing")]
        if not pending or time.time() - t0 > timeout_s:
            return
        for e in pending:
            if time.time() < wait.get(e["task_id"], 0):
                continue
            try:
                t = c.task(e["task_id"])
            except X100Error as err:
                e["last_error"] = str(err)
                wait[e["task_id"]] = time.time() + 15
                continue
            st = str(t.get("status", "")).lower()
            e["status_raw"] = st
            if st in DONE:
                urls = output_urls(t)
                if not urls:
                    e.update(status="download_missing", task=t)
                else:
                    out = out_for(e)
                    if not out.exists():
                        download(urls[0], out)
                    e.update(status="downloaded", file=str(out), credits_charged=t.get("credits", e.get("credits")))
            elif st in FAILED:
                e.update(status="failed", error=t.get("error") or t)
            else:
                e["status"] = "running"
            wait[e["task_id"]] = time.time() + max(5, int(t.get("poll_after_ms", 15000)) / 1000)
            if save_cb:
                save_cb()
        time.sleep(3)


if __name__ == "__main__":
    a = sys.argv[1:] or ["balance"]
    with Client() as c:
        if a[0] == "models":
            ms = c.models()
            if "--id" in a:
                ms = [m for m in ms if m["id"] == a[a.index("--id") + 1]]
            print(json.dumps(ms, ensure_ascii=False, indent=1))
        elif a[0] == "balance":
            print(json.dumps(c.call("100x_get_balance"), ensure_ascii=False))
        elif a[0] == "task":
            print(json.dumps(c.task(a[1]), ensure_ascii=False, indent=1))
