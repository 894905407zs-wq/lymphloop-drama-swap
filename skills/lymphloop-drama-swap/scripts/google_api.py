#!/usr/bin/env python3
"""Official Google REST adapter: Gemini Pro video analysis only (streamGenerateContent).

Durable local deduplication; no SDK or provider fallback. No TTS (user rule,
2026-10-24): reworded lines are re-voiced by Wan Controlled Edit instead.

This is an API worker, not a visual/ASR acceptance judge. Completed means bytes
arrived and passed container checks, not that the content passed editorial QA.
"""
from __future__ import annotations

import oscompat  # noqa: E402,F401  (first import: UTF-8 mode on Windows, see oscompat.py)
import argparse
import base64
import contextlib
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

# Share one exception/utility module when the CLI ledger imports this adapter.
if __name__ == "__main__":
    sys.modules.setdefault("google_api", sys.modules[__name__])

API = "https://generativelanguage.googleapis.com"
# One Gemini task only: fixed-contract video analysis. TTS is permanently removed.
MODELS = {"analyze": "gemini-3.1-pro-preview"}
UNKNOWN = {"submitting", "outcome_unknown", "stream_interrupted", "incomplete"}


class AdapterError(RuntimeError):
    pass


class HTTPFailure(AdapterError):
    def __init__(self, code, quota=None):
        self.code = code; self.quota = quota
        super().__init__(f"Google HTTP {code}; response body suppressed" + (f" (quota {quota})" if quota else ""))


def utc():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def file_sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def json_bytes(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def save(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique temp name: two processes saving the same file must never rename each other's temp away.
    temp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}.tmp")
    with temp.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.chmod(temp, 0o600)
    oscompat.replace(temp, path)


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _lock_holder(path):
    """Who wrote the lock file last (pid, command, since); informational only, the OS lock is the truth."""
    try:
        info = json.loads(Path(path).read_text(encoding="utf-8") or "{}")
    except Exception:
        return ""
    if not isinstance(info, dict) or not info.get("pid"):
        return ""
    return f" (held by pid {info['pid']}: {info.get('command', '?')}, since {info.get('since', '?')})"


@contextlib.contextmanager
def lock(path, wait=0.0, poll=0.5, what="this exact request"):
    """Exclusive OS lock (flock): released by the kernel when the holder dies, so a lock can never be held by a dead
    process; the lock file left behind is harmless. wait > 0 polls up to that many seconds for a LIVE holder to
    finish (its result is then reused from the cache, so nothing is posted twice). The holder's pid and command are
    written into the lock file for the error message."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as f:
        deadline = time.monotonic() + max(0.0, float(wait))
        while True:
            try:
                oscompat.lock_file(f, blocking=False)       # flock, or LockFileEx on Windows (same lock as swap.flock)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise AdapterError(f"Another worker owns {what}{_lock_holder(path)}; resume after it finishes") from None
                time.sleep(poll)
        try:
            f.truncate(0)
            f.write(json.dumps({"pid": os.getpid(), "command": " ".join([Path(sys.argv[0]).name, *sys.argv[1:2]]) if sys.argv else "?",
                                "since": utc()}).encode())
            f.flush()
        except OSError:
            pass
        try:
            yield
        finally:
            oscompat.unlock_file(f)


def credential():
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        if os.environ.get(name, "").strip():
            return os.environ[name].strip()
    service, account = "codex-video-analysis-gemini", "gemini-api-key"
    try:
        if sys.platform == "darwin":
            r = subprocess.run(["security", "find-generic-password", "-s", service,
                                "-a", account, "-w"], capture_output=True, text=True, timeout=30)
            return r.stdout.strip() if r.returncode == 0 else None
        if sys.platform == "win32":
            from keyring.backends.Windows import WinVaultKeyring
            return WinVaultKeyring().get_password(service, account)
        if sys.platform.startswith("linux"):
            from keyring.backends.SecretService import Keyring
            return Keyring().get_password(service, account)
    except Exception:
        pass
    return None


def required_key():
    try:
        import agent_native
        if agent_native.is_native():
            raise AdapterError(
                "External Gemini API is disabled in Antigravity native see/hear mode "
                "(set LYMPHLOOP_ALLOW_EXTERNAL_GEMINI=1 to override). Handle this step with the current agent."
            )
    except ImportError:
        pass
    key = credential()
    if not key:
        raise AdapterError("Google credential unavailable: use GEMINI_API_KEY / GOOGLE_API_KEY or the native credential store")
    return key


def trusted_url(url):
    p = urllib.parse.urlsplit(url)
    host = (p.hostname or "").lower()
    if (p.scheme != "https" or p.username or p.password or p.port not in (None, 443)
            or not (host == "generativelanguage.googleapis.com" or host.endswith(".googleapis.com")
                    or host.endswith(".googleusercontent.com"))):
        raise AdapterError("Unexpected media URL host; no request or credential forwarding")
    return host


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        host = trusted_url(newurl)
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new and host != "generativelanguage.googleapis.com":
            new.remove_header("X-goog-api-key")
            new.remove_header("Authorization")
        return new


class Transport:
    def __init__(self, key):
        self.key = key
        self.opener = urllib.request.build_opener(SafeRedirect())

    def open(self, url, *, method="GET", payload=None, body=None, headers=None, timeout=180):
        host = trusted_url(url)
        h = dict(headers or {})
        if host == "generativelanguage.googleapis.com":
            h["x-goog-api-key"] = self.key
        if payload is not None:
            body = json_bytes(payload)
            h["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=body, headers=h, method=method)
        try:
            return self.opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            # Never log a server body that could echo submitted inline data or credentials; only the quota id of a 429
            # is read out (2026-11-22: "FileStorageBytesPerProject" was taken for a rate limit for an hour).
            quota = None
            if exc.code == 429:
                try:
                    m = re.search(r'"quotaId"\s*:\s*"([A-Za-z0-9_-]{1,80})"', exc.read(65536).decode("utf-8", "replace"))
                    quota = m.group(1) if m else None
                except Exception: quota = None
            raise HTTPFailure(exc.code, quota) from None
        except (OSError, ValueError) as exc:
            raise AdapterError(f"Google transport interrupted ({type(exc).__name__})") from None

    def json(self, url, **kwargs):
        with self.open(url, **kwargs) as response:
            return json.load(response)


def safe(obj, secret=None):
    """For saved manifests/logs only; never retain inline media or signed URL queries."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k.lower().replace("-", "_") in {"key", "api_key", "x_goog_api_key", "authorization"}:
                out[k] = "[REDACTED]"
            elif k == "data" and isinstance(v, str):
                out[k] = {"redacted_base64": True, "encoded_characters": len(v), "sha256_encoded": sha(v.encode())}
            elif k.lower() in {"uri", "url", "session_url", "fileuri"} and isinstance(v, str) and v.startswith("https:"):
                p = urllib.parse.urlsplit(v)
                out[k] = urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, "", ""))
            else:
                out[k] = safe(v, secret)
        return out
    if isinstance(obj, list):
        return [safe(v, secret) for v in obj]
    if isinstance(obj, str) and secret:
        return obj.replace(secret, "[REDACTED]")
    return obj


def public_state(state):
    keys = ("status", "kind", "model", "request_dir", "request_id", "attempts",
            "output_path", "output_sha256", "output_bytes", "http_status",
            "error_type", "finish_reason", "cache_hit", "file", "token_count", "contract_sha256")
    return safe({k: state[k] for k in keys if k in state})


def file_description(path):
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise AdapterError(f"Missing input file: {p}")
    mime = oscompat.mime_type(p.name)
    if not mime:
        raise AdapterError(f"Unknown media MIME type: {p.suffix}")
    return {"path": str(p), "sha256": file_sha(p), "bytes": p.stat().st_size, "mime_type": mime}


def inline_image(desc, interactions=False):
    """generateContent inlineData part. `interactions` is kept only for callers that pass
    False; the Interactions API (TTS-only) is removed."""
    if interactions:
        raise AdapterError("Interactions API is removed; only generateContent inline images are supported")
    if not desc["mime_type"].startswith("image/"):
        raise AdapterError("Image reference must be an image")
    raw = Path(desc["path"]).read_bytes()
    if sha(raw) != desc["sha256"]:
        raise AdapterError("Input file changed after manifest binding")
    encoded = base64.b64encode(raw).decode("ascii")
    return {"inlineData": {"mimeType": desc["mime_type"], "data": encoded}}


# Small media go inside the request instead of through the Files store (2026-11-22: three Codex threads, 25 films at
# once; every Flash shot check and every transcription chunk uploaded its own file, the upload endpoint answered 429 for
# minutes on end and plan / competitor / detect-pro stopped). A shot clip is 0.2-1.3 MB and a transcription chunk ~0.6
# MB; a generateContent request may carry 20 MB, so these never need the store. Large media (the Pro pass over the whole
# timecoded film) still upload.
INLINE_MAX_BYTES = int(float(os.environ.get("LYMPHLOOP_GEMINI_INLINE_MB", "8")) * 1024 * 1024)
REQUEST_INLINE_BUDGET = 14 * 1024 * 1024      # raw bytes inline per request (base64 adds a third; the limit is 20 MB)


def media_part(path, state_root, transport, budget=None, video_fps=None):
    """generateContent part for an audio/video file: inline when small (and `budget`, a one-item list of the raw bytes
    still allowed inline in this request, has room), else uploaded to the Files store. `budget` is reduced in place."""
    desc = file_description(path)
    room = REQUEST_INLINE_BUDGET if budget is None else budget[0]
    if desc["bytes"] <= INLINE_MAX_BYTES and desc["bytes"] <= room:
        if budget is not None: budget[0] -= desc["bytes"]
        raw = Path(desc["path"]).read_bytes()
        if sha(raw) != desc["sha256"]: raise AdapterError("Input file changed while it was read")
        part = {"inlineData": {"mimeType": desc["mime_type"], "data": base64.b64encode(raw).decode("ascii")}}
    else:
        up = upload_file(path, state_root, transport)
        part = {"fileData": {"fileUri": up["uri"], "mimeType": up.get("mimeType", desc["mime_type"])}}
    if video_fps is not None and desc["mime_type"].startswith("video/"): part["videoMetadata"] = {"fps": video_fps}
    return part


def file_metadata(data):
    obj = data.get("file", data)
    if not re.fullmatch(r"files/[A-Za-z0-9_-]+", obj.get("name", "")):
        raise AdapterError("Malformed Google file metadata; preserve upload state")
    return obj


UPLOAD_START_RETRIES = 5        # waits of 10, 20, 40, 60, 60 s: about three minutes before giving up
STORE_PRUNE_AGE_H = float(os.environ.get("LYMPHLOOP_GEMINI_PRUNE_HOURS", "3"))
STORE_AUTO_PRUNE = os.environ.get("LYMPHLOOP_GEMINI_AUTO_PRUNE") == "1"   # deleting remote copies only with the user's OK (off by default)


def prune_store(transport, older_than_h=STORE_PRUNE_AGE_H, sleep=time.sleep):
    """Delete Files-store copies older than `older_than_h` hours. The store holds 20 GiB per project and keeps each
    upload 48 h (2026-11-22: three Codex threads filled it in a night -- 18,694 files, 21.5 GB -- and every upload above
    a few MB was refused with FileStorageBytesPerProject). Every upload is a temporary copy of a local file; a call
    that finished hours ago no longer needs it, and upload_file re-uploads a cached file that is gone (404).
    Returns (files deleted, bytes freed)."""
    import datetime as dt
    from concurrent.futures import ThreadPoolExecutor
    files, token = [], None
    while True:
        page = transport.json(API + "/v1beta/files?pageSize=100" + (f"&pageToken={token}" if token else ""))
        files += page.get("files", []); token = page.get("nextPageToken")
        if not token: break
    now = dt.datetime.now(dt.timezone.utc)
    def age_h(f):
        try: return (now - dt.datetime.fromisoformat(str(f["createTime"]).replace("Z", "+00:00"))).total_seconds() / 3600
        except Exception: return 0.0
    old = [f for f in files if age_h(f) > older_than_h and str(f.get("name", "")).startswith("files/")]
    def rm(f):
        try:
            with transport.open(API + "/v1beta/" + f["name"], method="DELETE"): return int(f.get("sizeBytes") or 0)
        except HTTPFailure as exc:
            return 0 if exc.code != 404 else 0
    with ThreadPoolExecutor(16) as ex: freed = list(ex.map(rm, old))
    return sum(1 for b in freed if b), sum(freed)


def upload_file(path, state_root, transport, wait_seconds=900, sleep=time.sleep):
    desc = file_description(path)
    d = Path(state_root).resolve() / "uploads" / desc["sha256"]
    d.mkdir(parents=True, exist_ok=True)
    sf = d / "state.json"
    # Same bytes requested by a parallel worker (two identical shots, the same QA source for two candidates, two
    # agents on one RUN): wait for it and reuse its upload instead of failing.
    with lock(d / ".lock", wait=wait_seconds + 1800, what="this upload"):
        state = read(sf) if sf.exists() else {"status": "new", "source": desc}
        def persist(): save(sf, state)
        if state.get("file", {}).get("name"):
            try:
                remote = file_metadata(transport.json(API + "/v1beta/" + state["file"]["name"]))
                state["file"] = remote; persist()
            except HTTPFailure as exc:
                if exc.code != 404:
                    raise
                state = {"status": "new", "source": desc, "previous_file_expired": True}; persist()
        if not state.get("file"):
            if not state.get("session_url"):
                if state["status"] in {"starting", "start_unknown"}:
                    # A previous worker died (or failed) while opening the resumable session; we hold the lock, so it is
                    # not running. Opening a session is free and creates no file until bytes are finalized, so a fresh
                    # session is safe (the orphan expires on its own). Nothing paid is ever re-posted here.
                    state["restarted_starts"] = state.get("restarted_starts", 0) + 1
                state["status"] = "starting"; persist()
                # 429 / 503 on opening the session is the per-minute limit, not a refusal (2026-11-22: three threads, 25
                # films at once; `plan` failed three times in a row on the transcription WAV while the store was a
                # quarter full and the same request passed minutes later). Opening a session is free, so wait and ask again.
                for attempt in range(UPLOAD_START_RETRIES + 1):
                    try:
                        with transport.open(API + "/upload/v1beta/files", method="POST",
                             payload={"file": {"display_name": Path(desc["path"]).name}}, headers={
                             "X-Goog-Upload-Protocol": "resumable", "X-Goog-Upload-Command": "start",
                             "X-Goog-Upload-Header-Content-Length": str(desc["bytes"]),
                             "X-Goog-Upload-Header-Content-Type": desc["mime_type"]}) as response:
                            url = response.headers.get("X-Goog-Upload-URL", "")
                            trusted_url(url)
                            state.update(session_url=url, status="session_ready"); persist()
                        break
                    except HTTPFailure as exc:
                        if exc.code == 429 and exc.quota == "FileStorageBytesPerProject" and STORE_AUTO_PRUNE and not state.get("store_pruned"):
                            state["store_pruned"] = list(prune_store(transport)); persist(); continue
                        if exc.code in (429, 503) and attempt < UPLOAD_START_RETRIES:
                            state["start_retries"] = state.get("start_retries", 0) + 1; persist()
                            sleep(min(60, 10 * 2 ** attempt)); continue
                        state["status"] = "start_unknown"; persist(); raise
                    except Exception:
                        state["status"] = "start_unknown"; persist(); raise
            offset = 0
            if state["status"] in {"uploading", "upload_unknown"}:
                with transport.open(state["session_url"], method="POST", body=b"",
                                    headers={"X-Goog-Upload-Command": "query"}) as response:
                    remote_state = response.headers.get("X-Goog-Upload-Status", "").lower()
                    raw = response.read()
                    if remote_state == "final" and raw:
                        state["file"] = file_metadata(json.loads(raw)); persist()
                    elif remote_state == "active":
                        offset = int(response.headers.get("X-Goog-Upload-Size-Received", "0"))
                    else:
                        raise AdapterError("Upload finalized without recoverable metadata; reconcile existing file, do not duplicate")
            if not state.get("file"):
                if not 0 <= offset <= desc["bytes"]:
                    raise AdapterError("Invalid remote upload offset")
                with open(desc["path"], "rb") as f:
                    f.seek(offset); body = f.read()
                state["status"] = "uploading"; persist()
                try:
                    state["file"] = file_metadata(transport.json(state["session_url"], method="POST", body=body,
                        timeout=1800, headers={"X-Goog-Upload-Offset": str(offset),
                        "X-Goog-Upload-Command": "upload, finalize", "Content-Length": str(len(body))}))
                    state["status"] = "processing"; persist()
                except Exception:
                    state["status"] = "upload_unknown"; persist(); raise
        deadline = time.monotonic() + wait_seconds
        while True:
            remote = file_metadata(transport.json(API + "/v1beta/" + state["file"]["name"]))
            state["file"] = remote
            status = remote.get("state", "")
            state["status"] = status.lower(); persist()
            if status == "ACTIVE" and remote.get("uri"):
                return remote
            if status == "FAILED":
                raise AdapterError("Google file processing failed; no generation submitted")
            if time.monotonic() >= deadline:
                raise AdapterError("Upload processing remains pending; rerun this upload to resume")
            sleep(min(5, max(0, deadline - time.monotonic())))


def atomic_bytes(path, raw):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    with tmp.open("wb") as f:
        f.write(raw); f.flush(); os.fsync(f.fileno())
    oscompat.replace(tmp, path)


def copy_result(state, output):
    if state.get("status") != "completed":
        return state
    cached = Path(state["cache_path"])
    if not cached.is_file() or file_sha(cached) != state["output_sha256"]:
        raise AdapterError("Completed cache is missing or changed; do not pay to regenerate automatically")
    dest = Path(output).expanduser().resolve()
    if dest != cached:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and file_sha(dest) != state["output_sha256"]:
            raise AdapterError("Output path already contains different bytes; choose a new path")
        if not dest.exists():
            shutil.copy2(cached, dest)
    state["output_path"] = str(dest)
    return state


def analysis_stream(payload, model, transport, directory, state):
    endpoint = API + "/v1beta/models/" + model + ":streamGenerateContent?alt=sse"
    text, finish, usage = [], None, {}
    with transport.open(endpoint, method="POST", payload=payload, timeout=1800) as response:
        with (directory/"response_chunks.jsonl").open("w", encoding="utf-8") as chunks, (directory/"partial.txt").open("w", encoding="utf-8") as partial:
            for line in response:
                if not line.startswith(b"data:"):
                    continue
                chunk = json.loads(line[5:].strip())
                chunks.write(json.dumps(safe(chunk, transport.key), ensure_ascii=False)+"\n"); chunks.flush()
                usage = chunk.get("usageMetadata", usage)
                for candidate in chunk.get("candidates", []):
                    finish = candidate.get("finishReason", finish)
                    for part in candidate.get("content", {}).get("parts", []):
                        if part.get("text") and not part.get("thought"):
                            text.append(part["text"]); partial.write(part["text"]); partial.flush()
                state.update(status="streaming", finish_reason=finish, characters_received=sum(map(len,text)))
                save(directory/"state.json", state)
    answer = "".join(text)
    state.update(finish_reason=finish, usage=safe(usage))
    if finish != "STOP" or not answer:
        state["status"] = "incomplete"
    else:
        cached = directory/"result.txt"; atomic_bytes(cached, answer.encode())
        state.update(status="completed", cache_path=str(cached), output_sha256=file_sha(cached),
                     output_bytes=cached.stat().st_size, completed_at_utc=utc())
    save(directory/"state.json", state)
    return state


def normalize_spec(spec):
    spec = dict(spec)
    if spec.get("kind") not in MODELS:
        raise AdapterError("Unsupported Gemini task; only analyze is available (TTS is permanently removed)")
    if spec.get("model") != MODELS[spec["kind"]]:
        raise AdapterError("The exact official model for this task is pinned; overrides are forbidden")
    if spec["kind"] == "analyze":
        root = Path(__file__).resolve().parents[1]
        manifest = read(root / "assets/prompts/contract-lock.json")
        contract = (root / manifest["file"]).read_text(encoding="utf-8")
        if sha(contract.encode()) != manifest["sha256"]:
            raise AdapterError("Original user analysis contract changed; restore its locked bytes")
        note = (root / "assets/prompts/analysis-runtime-note.txt").read_text(encoding="utf-8")
        scope = spec["prompt"].replace(contract, "").replace(note, "").strip()
        # The caller adds only scope/context. It cannot replace the user's fixed contract.
        spec.update(prompt=contract + "\n\n" + note + ("\n\n" + scope if scope else ""),
                    contract_sha256=manifest["sha256"])
    return spec


def validate_references(spec):
    for desc in spec.get("images", []):
        if desc.get("role", "product_reference") != "product_reference":
            raise AdapterError("Image role must be product_reference")
        if file_sha(desc["path"]) != desc["sha256"]:
            raise AdapterError("Product reference changed after binding")
        root = Path(__file__).resolve().parents[1]
        profile = read(root/"assets/product/profile.json")
        active = set(profile["active_model_reference_files"])
        allowed_hashes = {entry["sha256"] for entry in profile["asset_manifest"]
                          if entry["file"] in active and entry.get("model_input_allowed")}
        registered = desc["sha256"] in allowed_hashes
        try:
            from PIL import Image
            with Image.open(desc["path"]) as im:
                im.load()
                if not registered:
                    # Unregistered run assets must still be true cut-outs; the registered handheld photo is a real opaque photo.
                    if "A" not in im.getbands() and "transparency" not in im.info:
                        raise ValueError()
                    lo, hi = im.convert("RGBA").getchannel("A").getextrema()
                    if lo != 0 or hi == 0:
                        raise ValueError()
        except ImportError:
            raise AdapterError("Pillow is required to verify product references") from None
        except Exception:
            raise AdapterError("Unregistered product references must decode and contain a transparent background; opaque/white assets are blocked") from None
        if not registered:
            authorization=desc.get('authorized_reference',{})
            run=Path(authorization.get('run_dir','')).resolve()
            record_path=Path(authorization.get('path','')).resolve()
            image_path=Path(desc['path']).resolve()
            if (not authorization or run not in record_path.parents or run not in image_path.parents
                or not (run/'run.json').is_file() or not record_path.is_file()
                or file_sha(record_path)!=authorization.get('sha256')):
                raise AdapterError("Product reference is not registered or bound to a user-authorized run asset")
            record=read(record_path)
            if (record.get('type')!='lymphloop_user_authorized_reference'
                or record.get('reference_sha256')!=desc['sha256']
                or record.get('view') not in profile.get('reference_views',{})
                or not record.get('user_instruction') or not record.get('visual_review')):
                raise AdapterError('Product reference lacks its exact reviewed user authorization')


def request_identity(spec):
    ident = dict(spec)
    if "images" in ident:
        ident["images"] = [{k:v for k,v in d.items() if k != "path"} for d in ident["images"]]
    if "video" in ident:
        ident["video"] = {k:v for k,v in ident["video"].items() if k != "path"}
    return sha(json_bytes(ident))


def execute(spec, state_root, output, **kwargs):
    spec = normalize_spec(spec)
    validate_references(spec)
    return _execute_request(spec, state_root, output, **kwargs)


def payload_for(spec, transport=None, state_root=None):
    spec = normalize_spec(spec)
    # normalize_spec has already rejected every kind except "analyze".
    if file_sha(spec["video"]["path"]) != spec["video"]["sha256"]:
        raise AdapterError("Video changed after manifest binding")
    uploaded = upload_file(spec["video"]["path"], state_root, transport)
    parts = [inline_image(d, False) for d in spec.get("images", [])]
    parts.extend([{"fileData": {"mimeType": spec["video"]["mime_type"], "fileUri": uploaded["uri"]},
                   "videoMetadata": {"fps": spec["sampling_fps"]}}, {"text": spec["prompt"]}])
    return {"contents": [{"role": "user", "parts": parts}], "generationConfig": {
        "temperature": 0.0, "maxOutputTokens": 65536, "responseMimeType": "text/plain",
        "mediaResolution": "MEDIA_RESOLUTION_HIGH", "thinkingConfig": {"thinkingLevel": "HIGH", "includeThoughts": False}}}


def _execute_request(spec, state_root, output, *, job=None, max_attempts=2, wait_seconds=0, transport=None, sleep=time.sleep):
    """spec is local paths + hashes + prompt. Output path is excluded from identity.

    The same content/model/settings within one state_root always shares one cache,
    even under a different job label/output path. max_attempts includes the first.
    wait_seconds is accepted for caller compatibility; the analysis stream is synchronous.
    """
    if not 1 <= max_attempts <= 3:
        raise AdapterError("max_attempts must be 1–3")
    # A renamed copy of an identical input still resolves to the same request cache.
    fingerprint = request_identity(spec)
    directory = Path(state_root).resolve()/"requests"/fingerprint
    directory.mkdir(parents=True, exist_ok=True)
    sf = directory/"state.json"
    with lock(directory/".lock"):
        state = read(sf) if sf.exists() else {"status":"new", "kind":spec["kind"], "model":spec["model"],
            "request_id":fingerprint, "request_dir":str(directory), "job":job, "attempts":0, "created_at_utc":utc()}
        if spec.get("contract_sha256"): state["contract_sha256"] = spec["contract_sha256"]
        if state["request_id"] != fingerprint:
            raise AdapterError("Request identity mismatch")
        if state["status"] == "completed":
            state["cache_hit"] = True
            copy_result(state, output); save(sf, state)
            return state
        transport = transport or Transport(required_key())
        if state["status"] in UNKNOWN or state["status"] == "streaming":
            raise AdapterError("Existing generation is unresolved/incomplete; recover it, never blind paid resubmission")
        if state["status"] == "known_failed" and not state.get("retryable"):
            return public_state(state)
        save(directory/"spec.json", safe(spec, transport.key))
        atomic_bytes(directory/"submitted_prompt.txt", spec["prompt"].encode("utf-8"))
        save(sf, state)
        payload = payload_for(spec, transport, state_root)
        save(directory/"request_manifest.json", {"request_id":fingerprint, "payload_sha256":sha(json_bytes(payload)),
                                                "payload":safe(payload, transport.key)})
        if not state.get("token_count"):
            count = transport.json(API+"/v1beta/models/"+spec["model"]+":countTokens", method="POST",
                payload={"generateContentRequest":{"model":"models/"+spec["model"], **payload}}, timeout=1800)
            state["token_count"] = count.get("totalTokens"); save(sf, state)
            if not isinstance(state["token_count"], int) or state["token_count"] > 1048576:
                raise AdapterError("Analysis exceeds model context or token count unavailable; split before submission")
        while state["attempts"] < max_attempts:
            state.update(status="submitting", attempts=state["attempts"]+1, submitted_at_utc=utc())
            save(sf, state)
            try:
                analysis_stream(payload, spec["model"], transport, directory, state)
            except HTTPFailure as exc:
                # Explicit client/rate-limit rejections are known. A 5xx/408 may hide an accepted job.
                known = exc.code in {400, 401, 403, 404, 413, 422, 429}
                state.update(status="known_failed" if known else "outcome_unknown", http_status=exc.code,
                             retryable=exc.code == 429, error_type="HTTPFailure")
                save(sf, state)
                if exc.code == 429 and state["attempts"] < max_attempts:
                    sleep(min(30, 5 * state["attempts"])); continue
                return state
            except Exception as exc:
                state["status"] = "stream_interrupted" if state.get("characters_received") else "outcome_unknown"
                state["error_type"] = type(exc).__name__; save(sf, state)
                return state
            copy_result(state, output); save(sf, state)
            return state
        return state


def resume(directory, output=None, wait_seconds=0, transport=None):
    """Re-deliver a completed cached analysis. A streamed analysis has no server id to
    poll, so an unfinished request is reported, never silently re-submitted (paid)."""
    directory = Path(directory).resolve()
    with lock(directory/".lock"):
        state = read(directory/"state.json")
        if state["status"] != "completed":
            raise AdapterError("Request is not completed and a streamed analysis cannot be polled; inspect it, never blind paid resubmission")
        copy_result(state, output or state.get("output_path", state["cache_path"]))
        save(directory/"state.json", state)
        return state


def preflight(online=False):
    key = credential()
    try:
        from PIL import Image
        pillow_available = True
    except ImportError:
        pillow_available = False
    active_models = dict(MODELS)
    result = {"python":sys.version.split()[0], "python_supported":sys.version_info >= (3,10),
              "pillow_available":pillow_available,
              "credential_available":bool(key), "models":active_models, "network_checked":False,
              "generation_submitted":False, "adapter_dependencies":"Python standard library for REST; Pillow for product alpha and decoded image validation; native keyring optional off macOS"}
    if online and key:
        result["network_checked"] = True; result["model_availability"] = {}
        t = Transport(key)
        for model in dict.fromkeys(active_models.values()):
            try:
                data = t.json(API+"/v1beta/models/"+model)
                result["model_availability"][model] = {"available":True, "name":data.get("name")}
            except HTTPFailure as exc:
                result["model_availability"][model] = {"available":False, "http_status":exc.code}
            except AdapterError:
                result["model_availability"][model] = {"available":None, "error":"transport unavailable"}
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    pre = sub.add_parser("preflight"); pre.add_argument("--online", action="store_true")
    up = sub.add_parser("upload"); up.add_argument("file", type=Path); up.add_argument("--state-dir", type=Path, required=True)
    res = sub.add_parser("resume"); res.add_argument("request_dir", type=Path); res.add_argument("--output", type=Path)
    pr = sub.add_parser("prune-files", help="delete Files-store copies older than --hours (the store is 20 GiB; uploads are temporary copies)"); pr.add_argument("--hours", type=float, default=STORE_PRUNE_AGE_H)
    res.add_argument("--wait-seconds", type=int, default=0, help="Ignored; kept for command compatibility")
    for kind in MODELS:
        p = sub.add_parser(kind)
        p.add_argument("--prompt", type=Path, required=True, help="UTF-8 scope/context file (the fixed contract is prepended)")
        p.add_argument("--output", type=Path, required=True)
        p.add_argument("--state-dir", type=Path, required=True)
        p.add_argument("--job", help="Descriptive label only; content fingerprint remains the deduplication key")
        p.add_argument("--model", default=MODELS[kind], choices=[MODELS[kind]], help="Pinned official model; no silent fallback")
        p.add_argument("--max-attempts", type=int, default=2, choices=[1,2,3])
        p.add_argument("--wait-seconds", type=int, default=0, help="Ignored; the analysis stream is synchronous")
        p.add_argument("--reference", type=Path, action="append", default=[])
        p.add_argument("--video", type=Path, required=True)
        p.add_argument("--sampling-fps", type=float, default=5)
    args = parser.parse_args(argv)
    try:
        if args.command == "preflight":
            result = preflight(args.online)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["credential_available"] and result["python_supported"] and result["pillow_available"] and all(x.get("available") for x in result.get("model_availability", {}).values()) else 2
        if args.command == "prune-files":
            n, freed = prune_store(Transport(required_key()), args.hours)
            print(json.dumps({"deleted": n, "freed_gb": round(freed / 1e9, 2), "older_than_h": args.hours})); return 0
        if args.command == "upload":
            result = {"status":"completed", "file":upload_file(args.file, args.state_dir, Transport(required_key()))}
        elif args.command == "resume":
            result = resume(args.request_dir, args.output, args.wait_seconds)
        else:
            spec = {"kind":args.command, "model":args.model, "prompt":args.prompt.read_text(encoding="utf-8")}
            if not 0 < args.sampling_fps <= 30: raise AdapterError("sampling-fps must be greater than 0 and at most 30")
            spec["images"] = [{**file_description(p), "role": "product_reference"} for p in args.reference]
            spec.update(video=file_description(args.video), sampling_fps=args.sampling_fps)
            result = execute(spec, args.state_dir, args.output, job=args.job, max_attempts=args.max_attempts, wait_seconds=args.wait_seconds)
        print(json.dumps(public_state(result), ensure_ascii=False, indent=2))
        return 0 if result.get("status") == "completed" else 2
    except (AdapterError, OSError, ValueError) as exc:
        # Only our own fixed messages or exception type; file/network exceptions may carry secrets.
        message = str(exc) if isinstance(exc, AdapterError) else type(exc).__name__
        print(json.dumps({"status":"error", "error":message}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
