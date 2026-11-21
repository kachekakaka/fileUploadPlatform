"""Independent protocol fixture. Never imports adapter code or calls Bilibili."""

import argparse
import base64
import json
import os
from pathlib import Path
import sys
import time
from uuid import uuid4
import zlib
import struct

BUILD = "core-protocol-v1-fixture"


def simulated_png():
    def chunk(name, content):
        return (
            struct.pack(">I", len(content))
            + name
            + content
            + struct.pack(">I", zlib.crc32(name + content))
        )

    return base64.b64encode(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 0, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00\x00\xff"))
        + chunk(b"IEND", b"")
    ).decode("ascii")


def save(path, value):
    temporary = path.with_suffix(".tmp")
    with temporary.open("x", encoding="utf-8") as file:
        json.dump(value, file)
        file.flush()
        os.fsync(file.fileno())
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["upload", "login", "validate", "renew", "query"])
    parser.add_argument("--identity-json")
    args = parser.parse_args()
    control = json.loads(Path(os.environ["BUE_FIXTURE_CONTROL"]).read_text(encoding="utf-8"))
    aid, bvid = control.get("aid", 123), control.get("bvid", "BV1test00001")
    archive = Path(control["archive"]) if "archive" in control else None
    if control.get("noisy_stderr"):
        sys.stderr.write("SYNTHETIC_COOKIE_DO_NOT_LOG=" + "s" * 300000)
        sys.stderr.flush()
    if args.operation != "upload":
        req = json.loads(sys.stdin.readline())
        root = Path(req["state_root"])
        root.mkdir(parents=True, exist_ok=True)
        mid = control.get("login_mid", req.get("expected_mid") or "42")
        failure = control.get("maintenance_failure") and args.operation != "validate"
        failure = failure or (args.operation == "validate" and control.get("invalid_credential"))
        result = {
            "operation_id": req["operation_id"],
            "protocol_version": 1,
            "engine_build_id": BUILD,
        }
        if req.get("interaction_mode") == "machine":
            print(
                json.dumps(
                    {
                        **result,
                        "type": "login_qr_ready",
                        "interaction_version": 1,
                        "seq": 1,
                        "challenge_id": str(uuid4()),
                        "png_base64": simulated_png(),
                        "expiry_source": "local",
                        "deadline_at": int(time.time()) + 30,
                    }
                ),
                flush=True,
            )
            gate = Path(control["qr_gate"])
            while not gate.exists():
                time.sleep(0.05)
        if args.operation == "query":
            if control.get("query_gate"):
                Path(req["result_path"]).with_name("query-started").touch()
                while not Path(control["query_gate"]).exists():
                    time.sleep(0.01)
            stored = None
            if archive and req.get("aid"):
                candidate = archive / f"{req['aid']}.json"
                if candidate.is_file():
                    stored = json.loads(candidate.read_text(encoding="utf-8"))
            if control.get("query_failure") or (archive and not stored):
                result.update(outcome="inconclusive", evidence=None, error={"code": "QUERY_FAILED"})
            else:
                result.update(
                    outcome="matched",
                    error=None,
                    evidence={
                        "platform": "bilibili",
                        "verified_mid": stored["mid"] if stored else req["expected_mid"],
                        "aid": stored["aid"] if stored else aid,
                        "bvid": stored["bvid"] if stored else bvid,
                        "video_keys": stored["video_keys"] if stored else req["video_keys"],
                        "query_verified": True,
                        "review_status": control.get("review_state", 0),
                        "queried_at": time.time(),
                    },
                )
            save(Path(req["result_path"]), result)
            print(json.dumps(result), flush=True)
            return
        if failure:
            result.update(outcome="failed", error={"code": "FIXTURE_FAILURE"})
        else:
            if req.get("coordinate_registration"):
                identified = {**result, "type": "account_identified", "verified_mid": mid}
                save(Path(req["result_path"]).with_name("identified.json"), identified)
                print(json.dumps(identified), flush=True)
                ack = json.loads(sys.stdin.readline())
                assert ack == {
                    "type": "maintenance_ready",
                    "operation_id": req["operation_id"],
                    "verified_mid": mid,
                }
            reference = (
                req["credential_reference"]
                if args.operation == "validate"
                else mid + "-" + req["operation_id"] + ".json"
            )
            if args.operation != "validate":
                save(root / reference, {"synthetic": True})
            result.update(
                outcome="succeeded",
                verified_mid=mid,
                display_name="fixture account",
                credential_reference=reference,
            )
        save(Path(req["result_path"]), result)
        print(json.dumps(result), flush=True)
        return
    identity = json.loads(args.identity_json)
    hello = {"identity": identity, "engine_build_id": BUILD, "type": "hello", "sequence": 0}
    print(json.dumps(hello), flush=True)
    line = sys.stdin.readline()
    if not line:
        return
    request = json.loads(line)
    assert request["identity"] == identity
    directory = Path(request["execution_directory"])
    save(directory / "fixture-received.json", {"identity": identity, "request": request})
    gate = control.get("gate")
    if gate:
        while not Path(gate).exists():
            time.sleep(0.01)
    if control.get("crash"):
        os._exit(51)
    response_code = control.get("submit_response_code")
    outcome = "uncertain" if response_code is not None else control.get("outcome", "succeeded")
    state = {
        "succeeded": "accepted",
        "retryable": "not_submitted",
        "permanent": "not_submitted",
        "uncertain": "unknown",
    }[outcome]
    history = []

    def record(phase, details):
        event = {
            "identity": identity,
            "engine_build_id": BUILD,
            "type": "progress",
            "sequence": len(history) + 1,
            "phase": phase,
            "at": time.time(),
            "verified_mid": None if phase == "request_accepted" else identity["expected_mid"],
            "details": details,
        }
        history.append(event)
        save(directory / f"event-{len(history):06}.json", event)
        print(json.dumps(event), flush=True)

    record("request_accepted", {})
    record("uploading", {"parts": len(request["parts"])})
    receipt = None
    if outcome == "succeeded" or control.get("submit_intent") or response_code is not None:
        keys = [f"fixture-key-{index}" for index in range(len(request["parts"]))]
        record("waiting_submit", {})
        record(
            "submit_intent",
            {
                "submit_api": "web",
                "video_keys": keys,
                "studio": {"videos": [{"filename": key} for key in keys]},
            },
        )
    if response_code is not None:
        record(
            "submit_response",
            {"code": response_code, "aid": None, "bvid": None, "video_keys": keys},
        )
    if outcome == "succeeded":
        if archive:
            # Independent loopback-test archive: a second add with the same ID fails.
            with (archive / f"{aid}.json").open("x", encoding="utf-8") as file:
                json.dump(
                    {
                        "aid": aid,
                        "bvid": bvid,
                        "mid": identity["expected_mid"],
                        "title": request["title"],
                        "parts": request["parts"],
                        "video_keys": keys,
                    },
                    file,
                )
        record("submit_response", {"code": 0, "aid": aid, "bvid": bvid, "video_keys": keys})
        receipt = {
            "platform": "bilibili",
            "verified_mid": identity["expected_mid"],
            "aid": aid,
            "bvid": bvid,
            "video_keys": keys,
            "submit_api": "web",
            "submit_code": 0,
            "query_verified": True,
            "queried_at": time.time(),
            "review_status": 0,
        }
    record("finished", {"outcome": outcome, "submission_state": state})
    result = {
        "identity": identity,
        "engine_build_id": BUILD,
        "type": "result",
        "sequence": len(history) + 1,
        "verified_mid": identity["expected_mid"],
        "outcome": outcome,
        "submission_state": state,
        "error": None if receipt else {"code": control.get("error_code", "FIXTURE_FAILURE")},
        "receipt": receipt,
        "history": history,
    }
    if control.get("wrong_mid"):
        result["identity"]["expected_mid"] = "99"
    if control.get("wrong_build"):
        result["engine_build_id"] = "unrelated-build"
    if control.get("conflicting_event"):
        result["history"][1]["details"]["parts"] = 99
    if control.get("bad_error"):
        result["error"] = {"code": "cookie=synthetic"}
    save(directory / "result.json", result)


if __name__ == "__main__":
    main()
