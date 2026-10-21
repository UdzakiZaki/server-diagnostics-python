#!/usr/bin/env python3

import argparse
import datetime
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

__version__ = "1.0.0"

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_UNKNOWN = 3

ALL_CHECKS = ["service", "port", "http", "disk", "config"]
DEFAULT_CHECKS = ["service", "port", "http", "disk"]
BODY_LIMIT = 65536


class CheckError(Exception):
    pass


def _result(name, status, message, started, details=None):
    return {
        "name": name,
        "status": status,
        "message": message,
        "duration_ms": round((time.monotonic() - started) * 1000, 1),
        "details": details or {},
    }


def _run(cmd, timeout):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise CheckError("command not found: %s" % cmd[0]) from exc
    except subprocess.TimeoutExpired as exc:
        raise CheckError(
            "command timed out after %ss: %s" % (timeout, " ".join(cmd))
        ) from exc


def _journal_tail(unit, timeout, lines=5):
    try:
        proc = _run(
            ["journalctl", "-u", unit, "-n", str(lines), "--no-pager", "-o", "cat"],
            timeout,
        )
    except CheckError:
        return []
    if proc.returncode != 0:
        return []
    return [line for line in proc.stdout.splitlines() if line.strip()]


def check_service(args):
    started = time.monotonic()
    if shutil.which("systemctl") is None:
        raise CheckError("systemctl not found: this is not a systemd host")

    proc = _run(
        [
            "systemctl", "show", args.service, "--no-pager",
            "-p", "LoadState", "-p", "ActiveState", "-p", "SubState",
            "-p", "UnitFileState", "-p", "Result",
        ],
        args.timeout,
    )
    if proc.returncode != 0:
        raise CheckError("systemctl show failed: %s" % (" ".join(proc.stderr.split()) or proc.returncode))

    props = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
    details = {
        "service": args.service,
        "load_state": props.get("LoadState"),
        "active_state": props.get("ActiveState"),
        "sub_state": props.get("SubState"),
        "unit_file_state": props.get("UnitFileState"),
        "result": props.get("Result"),
    }

    if details["load_state"] == "not-found":
        return _result("service", "fail", "unit %s not found" % args.service, started, details)

    if details["active_state"] == "active":
        message = "%s is active (%s)" % (args.service, details["sub_state"])
        if details["unit_file_state"] not in ("enabled", "static", "alias"):
            message += "; not enabled, it will not start after a reboot"
        return _result("service", "ok", message, started, details)

    details["journal_tail"] = _journal_tail(args.service, args.timeout)
    message = "%s is %s (%s), result=%s" % (
        args.service, details["active_state"], details["sub_state"], details["result"],
    )
    return _result("service", "fail", message, started, details)


def check_port(args):
    started = time.monotonic()
    details = {"host": args.host, "port": args.port, "timeout_s": args.timeout}
    target = "%s:%s" % (args.host, args.port)
    try:
        connect_started = time.monotonic()
        with socket.create_connection((args.host, args.port), timeout=args.timeout):
            pass
        details["connect_ms"] = round((time.monotonic() - connect_started) * 1000, 1)
    except ConnectionRefusedError:
        return _result(
            "port", "fail",
            "connection refused on %s: host is reachable, nothing listens on the port" % target,
            started, details,
        )
    except socket.timeout:
        return _result(
            "port", "fail",
            "no answer from %s within %ss: packets dropped (firewall?) or host unreachable"
            % (target, args.timeout),
            started, details,
        )
    except socket.gaierror as exc:
        raise CheckError("cannot resolve host %s: %s" % (args.host, exc)) from exc
    except OSError as exc:
        details["errno"] = exc.errno
        return _result(
            "port", "fail", "connect to %s failed: [%s] %s" % (target, exc.errno, exc.strerror),
            started, details,
        )
    return _result(
        "port", "ok", "%s accepts connections (%s ms)" % (target, details["connect_ms"]),
        started, details,
    )


def check_http(args):
    started = time.monotonic()
    url = args.url or "http://%s:%s/" % (args.host, args.port)
    details = {"url": url, "expected_status": args.expect_status, "timeout_s": args.timeout}
    request = urllib.request.Request(
        url, headers={"User-Agent": "server-diagnostics/%s" % __version__}
    )

    try:
        with urllib.request.urlopen(request, timeout=args.timeout) as response:
            status, headers, body = response.status, response.headers, response.read(BODY_LIMIT)
    except urllib.error.HTTPError as exc:
        status, headers = exc.code, exc.headers
        try:
            body = exc.read(BODY_LIMIT)
        except Exception:
            body = b""
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, socket.timeout):
            return _result("http", "fail", "no HTTP answer within %ss" % args.timeout, started, details)
        if isinstance(reason, ConnectionRefusedError):
            return _result("http", "fail", "connection refused: nothing listens at %s" % url, started, details)
        if isinstance(reason, socket.gaierror):
            raise CheckError("cannot resolve host in %s: %s" % (url, reason)) from exc
        return _result("http", "fail", "request failed: %s" % reason, started, details)
    except socket.timeout:
        return _result("http", "fail", "no HTTP answer within %ss (read timeout)" % args.timeout, started, details)

    details.update(
        status=status,
        content_length=headers.get("Content-Length"),
        server=headers.get("Server"),
        elapsed_ms=round((time.monotonic() - started) * 1000, 1),
    )

    if status != args.expect_status:
        return _result(
            "http", "fail", "HTTP %s, expected %s" % (status, args.expect_status), started, details
        )

    if args.expect_text:
        text = body.decode("utf-8", errors="replace")
        details["expected_text"] = args.expect_text
        if args.expect_text not in text:
            return _result(
                "http", "fail",
                "HTTP %s, but the expected text was not found (wrong site or default page?)" % status,
                started, details,
            )

    return _result("http", "ok", "HTTP %s in %s ms" % (status, details["elapsed_ms"]), started, details)


def check_disk(args):
    started = time.monotonic()
    paths, low = [], []
    for path in args.disk_path:
        try:
            usage = shutil.disk_usage(path)
        except OSError as exc:
            raise CheckError("cannot read disk usage of %s: %s" % (path, exc)) from exc
        free_percent = round(usage.free * 100 / usage.total, 1)
        paths.append({
            "path": path,
            "total_gb": round(usage.total / 2**30, 2),
            "free_gb": round(usage.free / 2**30, 2),
            "free_percent": free_percent,
        })
        if free_percent < args.min_free_percent:
            low.append("%s: %s%% free" % (path, free_percent))

    details = {"min_free_percent": args.min_free_percent, "paths": paths}
    if low:
        return _result(
            "disk", "fail",
            "free space below %s%%: %s" % (args.min_free_percent, "; ".join(low)),
            started, details,
        )
    summary = ", ".join("%s %s%% free" % (p["path"], p["free_percent"]) for p in paths)
    return _result("disk", "ok", summary, started, details)


def check_config(args):
    started = time.monotonic()
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None or geteuid() != 0:
        raise CheckError("config check needs root (nginx -t reads protected files): run with sudo")

    proc = _run([args.nginx_bin, "-t"], args.timeout)
    output = (proc.stderr or proc.stdout).strip().splitlines()
    details = {"command": "%s -t" % args.nginx_bin, "returncode": proc.returncode, "output": output[-5:]}
    if proc.returncode == 0:
        return _result("config", "ok", "nginx configuration syntax is ok", started, details)
    first_error = next((line for line in output if "[emerg]" in line or "[alert]" in line), output[0] if output else "")
    return _result("config", "fail", "nginx -t failed: %s" % first_error, started, details)


CHECKS = {
    "service": check_service,
    "port": check_port,
    "http": check_http,
    "disk": check_disk,
    "config": check_config,
}


def run_checks(args):
    results = []
    for name in args.checks:
        started = time.monotonic()
        try:
            results.append(CHECKS[name](args))
        except CheckError as exc:
            results.append(_result(name, "error", str(exc), started))
        except Exception as exc:
            results.append(_result(name, "error", "unexpected %s: %s" % (type(exc).__name__, exc), started))
    return results


def summarize(results):
    statuses = {r["status"] for r in results}
    if "fail" in statuses:
        return "fail", EXIT_FAIL
    if "error" in statuses:
        return "unknown", EXIT_UNKNOWN
    return "ok", EXIT_OK


def positive_float(value):
    number = float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return number


def port_number(value):
    number = int(value)
    if not 1 <= number <= 65535:
        raise argparse.ArgumentTypeError("must be in 1..65535")
    return number


def percent(value):
    number = float(value)
    if not 0 <= number <= 100:
        raise argparse.ArgumentTypeError("must be in 0..100")
    return number


def parse_checks(value):
    names = [item.strip() for item in value.split(",") if item.strip()]
    unknown = [item for item in names if item not in ALL_CHECKS]
    if not names or unknown:
        raise argparse.ArgumentTypeError(
            "unknown check(s) %s; choose from %s" % (unknown or value, ",".join(ALL_CHECKS))
        )
    return names


def build_parser():
    parser = argparse.ArgumentParser(
        prog="diagnose.py",
        description="Health checks for a Linux web server. JSON report, exit code 0/1/2/3.",
    )
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
    parser.add_argument("--host", default="127.0.0.1", help="host for the port/http checks (default 127.0.0.1)")
    parser.add_argument("--port", type=port_number, default=80, help="TCP port (default 80)")
    parser.add_argument("--url", help="URL for the http check (default http://HOST:PORT/)")
    parser.add_argument("--service", default="nginx", help="systemd unit name (default nginx)")
    parser.add_argument("--expect-status", type=int, default=200, help="expected HTTP status (default 200)")
    parser.add_argument("--expect-text", help="text that must appear in the HTTP body")
    parser.add_argument("--disk-path", action="append", default=None, help="path to check, repeatable (default /)")
    parser.add_argument("--min-free-percent", type=percent, default=10.0, help="minimum free space in percent (default 10)")
    parser.add_argument("--nginx-bin", default="nginx", help="nginx binary for the config check (default nginx)")
    parser.add_argument("--checks", type=parse_checks, default=DEFAULT_CHECKS,
                        help="comma separated list from: %s (default %s)" % (",".join(ALL_CHECKS), ",".join(DEFAULT_CHECKS)))
    parser.add_argument("--timeout", type=positive_float, default=3.0, help="timeout in seconds for every check (default 3)")
    parser.add_argument("--output", help="also write the JSON report to this file")
    parser.add_argument("--text", action="store_true", help="print a human readable summary instead of JSON")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.disk_path is None:
        args.disk_path = ["/"]

    results = run_checks(args)
    overall, exit_code = summarize(results)

    report = {
        "tool": "server-diagnostics",
        "version": __version__,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "hostname": socket.gethostname(),
        "target": {"host": args.host, "port": args.port, "service": args.service},
        "overall": overall,
        "exit_code": exit_code,
        "checks": results,
    }
    if args.output:
        try:
            with open(args.output, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
        except OSError as exc:
            print("cannot write %s: %s" % (args.output, exc), file=sys.stderr)
            if exit_code == EXIT_OK:
                overall, exit_code = "unknown", EXIT_UNKNOWN
                report["overall"], report["exit_code"] = overall, exit_code

    if args.text:
        marks = {"ok": "OK   ", "fail": "FAIL ", "error": "ERROR"}
        for item in results:
            print("[%s] %-7s %s" % (marks[item["status"]], item["name"], item["message"]))
        print("overall: %s (exit code %s)" % (overall, exit_code))
    else:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
