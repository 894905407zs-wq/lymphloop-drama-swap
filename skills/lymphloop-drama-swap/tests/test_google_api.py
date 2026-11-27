#!/usr/bin/env python3
"""Offline contract tests. Fake transports only: no credentials or network."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from PIL import Image

import google_api as api


class Response(io.BytesIO):
    def __init__(self, data=b"", headers=None):
        super().__init__(data)
        self.headers = headers or {}


class FakeTransport:
    key = "FAKE_SECRET_FOR_REDACTION_TEST"
    def __init__(self, responses=(), streams=()):
        self.responses, self.streams = list(responses), list(streams)
        self.calls = []
    def json(self, url, **kw):
        self.calls.append((url, kw))
        result = self.responses.pop(0)
        if isinstance(result, Exception): raise result
        return result
    def open(self, url, **kw):
        self.calls.append((url, kw))
        result = self.streams.pop(0)
        if isinstance(result, Exception): raise result
        return result


def sse(text="answer", finish="STOP"):
    chunk = {"candidates":[{"content":{"parts":[{"text":text}]}, "finishReason":finish}]}
    return Response(b"data: "+json.dumps(chunk).encode()+b"\n\n")


TOKENS = {"totalTokens": 1000}
UPLOADED = {"uri": api.API+"/v1beta/files/test"}


class Contracts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        video = self.root/"v.mp4"; video.write_bytes(b"video")
        self.spec = {"kind":"analyze", "model":api.MODELS["analyze"], "prompt":"Local segment scope",
                     "images":[], "video":api.file_description(video), "sampling_fps":5}
    def tearDown(self):
        self.tmp.cleanup()
    def run_job(self, transport, spec=None, output="output.txt", **kw):
        with patch.object(api, "upload_file", return_value=UPLOADED):
            return api.execute(spec or self.spec, self.root/"state", self.root/output,
                               transport=transport, sleep=lambda _:None, **kw)
    def test_only_analyze_model(self):
        self.assertEqual(set(api.MODELS), {"analyze"})
        for kind in ("tts", "image", "video"):
            t = FakeTransport()
            with self.assertRaises(api.AdapterError):
                self.run_job(t, {"kind":kind, "model":"gemini-3.8-flash-tts", "prompt":"x"})
            self.assertEqual(t.calls, [])
        self.assertFalse(hasattr(api, "check_audio") or hasattr(api, "receive_interaction") or hasattr(api, "poll"))
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            api.main(["tts", "--prompt", "p", "--output", "o", "--state-dir", "s"])
    def test_success_cache_and_redaction(self):
        t = FakeTransport([TOKENS], [sse()])
        first = self.run_job(t)
        second = self.run_job(FakeTransport(), output="copy.txt")
        self.assertEqual(first["status"], "completed")
        self.assertTrue(second["cache_hit"])
        self.assertEqual(len(t.calls), 2)  # countTokens + one stream POST
        self.assertIn("Local segment scope", (Path(first["request_dir"])/"submitted_prompt.txt").read_text())
        for name in ("spec.json", "request_manifest.json", "response_chunks.jsonl", "state.json"):
            self.assertNotIn(FakeTransport.key, (Path(first["request_dir"])/name).read_text())
        self.assertEqual((self.root/"output.txt").read_text(), "answer")
        self.assertEqual((self.root/"output.txt").read_bytes(), (self.root/"copy.txt").read_bytes())
    def test_resume_completed_only(self):
        result = self.run_job(FakeTransport([TOKENS], [sse()]))
        done = api.resume(result["request_dir"], self.root/"resumed.txt", transport=FakeTransport())
        self.assertEqual(done["status"], "completed")
        self.assertEqual((self.root/"resumed.txt").read_text(), "answer")
        other = dict(self.spec, prompt="another scope")
        broken = self.run_job(FakeTransport([TOKENS], [api.AdapterError("timeout")]), spec=other)
        t = FakeTransport()
        with self.assertRaises(api.AdapterError): api.resume(broken["request_dir"], transport=t)
        self.assertEqual(t.calls, [])
    def test_unknown_timeout_never_reposts(self):
        result = self.run_job(FakeTransport([TOKENS], [api.AdapterError("timeout")]))
        self.assertEqual(result["status"], "outcome_unknown")
        with self.assertRaises(api.AdapterError): self.run_job(FakeTransport())
    def test_incomplete_stream_never_reposts(self):
        result = self.run_job(FakeTransport([TOKENS], [sse(finish="MAX_TOKENS")]))
        self.assertEqual(result["status"], "incomplete")
        with self.assertRaises(api.AdapterError): self.run_job(FakeTransport())
    def test_503_is_unknown_not_safe_retry(self):
        t = FakeTransport([TOKENS], [api.HTTPFailure(503)])
        result = self.run_job(t)
        self.assertEqual(result["status"], "outcome_unknown")
        self.assertEqual(len(t.calls), 2)
    def test_429_known_rejection_bounded_retry(self):
        t = FakeTransport([TOKENS], [api.HTTPFailure(429), sse()])
        result = self.run_job(t)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["attempts"],2)
    def test_429_stops_at_bound(self):
        t = FakeTransport([TOKENS], [api.HTTPFailure(429), api.HTTPFailure(429)])
        result = self.run_job(t)
        self.assertEqual(result["status"], "known_failed")
        again = self.run_job(FakeTransport())
        self.assertEqual(again["attempts"],2)
    def test_cache_corruption_refuses_regeneration(self):
        result = self.run_job(FakeTransport([TOKENS], [sse()]))
        Path(result["cache_path"]).write_bytes(b"bad")
        with self.assertRaises(api.AdapterError): self.run_job(FakeTransport())
    def test_identity_does_not_depend_on_input_path(self):
        a = self.root/"a.mp4"; b = self.root/"b.mp4"
        a.write_bytes(b"video"); b.write_bytes(b"video")
        spec = {"kind":"analyze", "model":api.MODELS["analyze"], "prompt":"scope", "sampling_fps":5, "images":[]}
        sa = {**spec, "video":api.file_description(a)}
        sb = {**spec, "video":api.file_description(b)}
        self.assertEqual(api.request_identity(sa), api.request_identity(sb))
    def test_exact_models_enforced_programmatically(self):
        t=FakeTransport()
        with self.assertRaises(api.AdapterError): self.run_job(t,{**self.spec,"model":"other-model"})
        self.assertEqual(t.calls,[])
    def test_registered_opaque_photo_passes_but_unregistered_opaque_image_is_blocked(self):
        root = Path(api.__file__).resolve().parents[1]
        good = {"kind":"analyze", "images":[{**api.file_description(root/"assets/product/stick-handheld-scale.jpg"), "role":"product_reference"}]}
        api.validate_references(good)
        other = self.root/"other.jpg"; Image.new("RGB",(64,64),(0,128,128)).save(other)
        bad = {"kind":"analyze", "images":[{**api.file_description(other), "role":"product_reference"}]}
        with self.assertRaises(api.AdapterError): api.validate_references(bad)
    def test_opaque_white_png_reference_blocked(self):
        p=self.root/"white.png"; Image.new("RGB",(2,2),"white").save(p)
        with self.assertRaises(api.AdapterError):
            api.validate_references({"kind":"analyze","images":[api.file_description(p)]})
    def test_unregistered_transparent_reference_needs_authorization(self):
        p=self.root/"new.png"; im=Image.new("RGBA",(2,2),(0,0,0,0)); im.putpixel((0,0),(1,2,3,255)); im.save(p)
        with self.assertRaises(api.AdapterError):
            api.validate_references({"kind":"analyze","images":[api.file_description(p)]})
    def test_non_product_role_rejected(self):
        root = Path(api.__file__).resolve().parents[1]
        desc = {**api.file_description(root/"assets/product/stick-handheld-scale.jpg"), "role":"scene_first_frame"}
        with self.assertRaises(api.AdapterError):
            api.validate_references({"kind":"analyze","images":[desc]})
    def test_safe_manifest(self):
        clean = api.safe({"data":"BASE64", "api_key":"foo", "uri":"https://x.googleapis.com/a?secret=yes",
                          "text":"Secret TESTKEY"},"TESTKEY")
        self.assertNotIn("BASE64",json.dumps(clean))
        self.assertNotIn("TESTKEY",json.dumps(clean))
        self.assertEqual(clean["uri"],"https://x.googleapis.com/a")
    def test_reject_external_media_host(self):
        for u in ["https://evil.com/a", "https://googleapis.com.evil/a", "http://generativelanguage.googleapis.com/a"]:
            with self.assertRaises(api.AdapterError): api.trusted_url(u)
    def test_upload_resumable_and_cache(self):
        source = self.root/"video.mp4"; source.write_bytes(b"test-mp4")
        obj = {"name":"files/file123", "state":"ACTIVE", "uri":api.API+"/v1beta/files/file123"}
        t = FakeTransport([{"file":obj},obj], [Response(headers={"X-Goog-Upload-URL":api.API+"/upload/session123"})])
        returned = api.upload_file(source,self.root/"state",t)
        self.assertEqual(returned,obj)
        again = FakeTransport([obj,obj])
        self.assertEqual(api.upload_file(source,self.root/"state",again),obj)
        self.assertTrue(all(x[1].get("method","GET")=="GET" for x in again.calls))
    def test_upload_start_waits_out_the_rate_limit(self):
        # 2026-11-22: 25 films at once, `plan` gave up after three 429s on the transcription WAV; the start is free to repeat
        source = self.root/"speech.wav"; source.write_bytes(b"wav-bytes")
        obj = {"name":"files/file429", "state":"ACTIVE", "uri":api.API+"/v1beta/files/file429"}
        t = FakeTransport([{"file":obj},obj], [api.HTTPFailure(429), api.HTTPFailure(503), Response(headers={"X-Goog-Upload-URL":api.API+"/upload/session429"})])
        waits = []
        self.assertEqual(api.upload_file(source,self.root/"state429",t,sleep=waits.append),obj)
        self.assertEqual(waits[:2],[10,20])
        always = FakeTransport([], [api.HTTPFailure(429)]*(api.UPLOAD_START_RETRIES+1))
        other = self.root/"speech2.wav"; other.write_bytes(b"other-bytes")
        with self.assertRaises(api.HTTPFailure): api.upload_file(other,self.root/"state429",always,sleep=lambda s: None)
        refused = FakeTransport([], [api.HTTPFailure(403)])
        third = self.root/"speech3.wav"; third.write_bytes(b"third-bytes")
        with self.assertRaises(api.HTTPFailure): api.upload_file(third,self.root/"state429",refused,sleep=lambda s: None)
        self.assertEqual(len(refused.calls),1)                      # a refusal is not repeated
    def test_full_store_is_pruned_once_then_the_upload_goes_on(self):
        # 2026-11-22: FileStorageBytesPerProject (20 GiB) was full; every upload over a few MB was refused
        source = self.root/"pro.mp4"; source.write_bytes(b"big-video")
        obj = {"name":"files/new", "state":"ACTIVE", "uri":api.API+"/v1beta/files/new"}
        t = FakeTransport([{"file":obj},obj], [api.HTTPFailure(429, "FileStorageBytesPerProject"), Response(headers={"X-Goog-Upload-URL":api.API+"/upload/s"})])
        with patch.object(api, "prune_store", return_value=(12, 9_000_000_000)) as prune, patch.object(api, "STORE_AUTO_PRUNE", True):
            self.assertEqual(api.upload_file(source,self.root/"stateprune",t,sleep=lambda s: None),obj)
        prune.assert_called_once()
    def test_prune_deletes_only_old_files(self):
        import datetime as dt
        now = dt.datetime.now(dt.timezone.utc)
        iso = lambda h: (now - dt.timedelta(hours=h)).isoformat().replace("+00:00", "Z")
        listing = {"files":[{"name":"files/a","createTime":iso(5),"sizeBytes":"100"},{"name":"files/b","createTime":iso(1),"sizeBytes":"50"}]}
        t = FakeTransport([listing], [Response()])
        self.assertEqual(api.prune_store(t, 3), (1, 100))
        self.assertEqual([c[1].get("method") for c in t.calls if "files/a" in c[0]], ["DELETE"])
    def test_fixed_analysis_contract_in_actual_payload(self):
        p=self.root/"v.mp4";p.write_bytes(b"video")
        spec={"kind":"analyze","model":api.MODELS["analyze"],"prompt":"Local segment scope: frames 10 to 20", "images":[], "video":api.file_description(p),"sampling_fps":5}
        with patch.object(api,"upload_file",return_value={"uri":api.API+"/v1beta/files/test"}):
            payload=api.payload_for(spec)
        prompt=payload["contents"][0]["parts"][-1]["text"]
        contract=(Path(api.__file__).resolve().parents[1]/"assets/prompts/detect-conflicts.txt").read_text()
        self.assertTrue(prompt.startswith(contract))
        self.assertEqual(prompt.count(contract),1)
        self.assertIn(spec["prompt"],prompt)
        normalized=api.normalize_spec(spec)
        self.assertEqual(api.normalize_spec(normalized),normalized)
        self.assertEqual(normalized["contract_sha256"],api.sha(contract.encode()))
    def test_sse_complete_and_partial(self):
        chunk = {"candidates":[{"content":{"parts":[{"text":"answer"}]}, "finishReason":"STOP"}]}
        d = self.root/"sse"; d.mkdir()
        state = {"kind":"analyze"}
        t = FakeTransport(streams=[Response(b"data: "+json.dumps(chunk).encode()+b"\n\n")])
        result = api.analysis_stream({},api.MODELS["analyze"],t,d,state)
        self.assertEqual(result["status"],"completed")
        chunk["candidates"][0]["finishReason"] = "MAX_TOKENS"
        t = FakeTransport(streams=[Response(b"data: "+json.dumps(chunk).encode()+b"\n\n")])
        self.assertEqual(api.analysis_stream({},api.MODELS["analyze"],t,d,{},)["status"],"incomplete")


if __name__ == "__main__":
    unittest.main()
