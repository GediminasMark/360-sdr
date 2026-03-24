#!/usr/bin/env python3
"""
SDR 360° Scan WebSocket Server — hackrf_sweep backend
Kiekvienam krypties žingsniui: pilnas dažnių sweep → peak freq + dBm → WebSocket

Run:
    python3.14 sdr_server.py [--freq-min 100] [--freq-max 6000] [--lna 32] [--vga 40] [--driver hackrf]

Requires:
    pip install websockets
    brew install hackrf  (arba sistema apt install hackrf)
"""

import asyncio
import json
import math
import random
import subprocess
import time
import logging
import argparse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sdr360")

# ─── Defaults ───────────────────────────────────────────────────────────────
DEFAULTS = {
    "freq_min"   : 100,      # MHz
    "freq_max"   : 6000,     # MHz
    "bin_width"  : 1_000_000,# Hz — 1 MHz resolution
    "lna_gain"   : 32,       # dB, 0-40, 8dB steps
    "vga_gain"   : 40,       # dB, 0-62, 2dB steps
    "amp"        : 0,        # RF amp 0=off 1=on
    "threshold"  : -80.0,    # dBm — signalai žemiau šio lygio ignoruojami
    "scan_speed" : 1.0,      # seconds per full 360° rotation
    "directions" : 8,
    "ws_port"    : 8765,
}

# ─── Simulation fallback ────────────────────────────────────────────────────
SIM_SOURCES = [
    {"bearing": 22,  "freq": 433.92e6, "strength": -55, "spread": 25},
    {"bearing": 135, "freq": 915.0e6,  "strength": -70, "spread": 30},
    {"bearing": 270, "freq": 2450.0e6, "strength": -80, "spread": 20},
    {"bearing": 315, "freq": 868.0e6,  "strength": -62, "spread": 35},
]

def sim_scan(bearing: float):
    """Simulate a frequency sweep result for given bearing."""
    best_dbm = -110.0
    best_freq = 433.92e6
    for s in SIM_SOURCES:
        diff = abs(((bearing - s["bearing"]) + 540) % 360 - 180)
        if diff < s["spread"]:
            factor = math.cos((diff / s["spread"]) * math.pi / 2)
            dbm = s["strength"] + 20 * math.log10(max(factor, 1e-6))
            noise = random.uniform(-3, 3)
            if dbm + noise > best_dbm:
                best_dbm = dbm + noise
                best_freq = s["freq"]
    noise_floor = -100.0 + random.uniform(-5, 5)
    if best_dbm < noise_floor:
        best_dbm = noise_floor
        best_freq = random.uniform(100e6, 1000e6)
    return max(-110.0, min(-20.0, best_dbm)), best_freq


# ─── hackrf_sweep wrapper ────────────────────────────────────────────────────
def list_hackrf_devices() -> list:
    """Return list of connected HackRF devices via hackrf_info."""
    try:
        r = subprocess.run(["hackrf_info"], capture_output=True, timeout=5, text=True)
        out = r.stdout + r.stderr
        devices = []
        dev = {}
        for line in out.splitlines():
            if line.startswith("Index:"):
                if dev: devices.append(dev)
                dev = {"index": line.split(":")[1].strip()}
            elif "Serial number" in line:
                dev["serial"] = line.split(":")[-1].strip()
            elif "Board ID" in line:
                dev["board"] = line.split(":", 1)[-1].strip()
            elif "Firmware Version" in line:
                dev["firmware"] = line.split(":", 1)[-1].strip()
        if dev and "index" in dev:
            devices.append(dev)
        return devices
    except Exception:
        return []


def check_hackrf() -> bool:
    """Return True if hackrf_sweep is available and HackRF is connected."""
    try:
        result = subprocess.run(
            ["hackrf_sweep", "-f", "100:101", "-w", "1000000", "-1",
             "-l", "32", "-g", "40"],
            capture_output=True, timeout=5
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


async def run_sweep_streaming(cfg: dict, on_progress):
    """
    Stream hackrf_sweep line by line.
    Calls on_progress(progress, cur_dbm, cur_freq_hz, peak_dbm, peak_freq_hz)
    every ~50ms. Returns (peak_dbm, peak_freq_hz) or None.
    """
    cmd = [
        "hackrf_sweep",
        "-f", f"{cfg['freq_min']}:{cfg['freq_max']}",
        "-w", str(int(cfg["bin_width"])),
        "-l", str(int(cfg["lna_gain"])),
        "-g", str(int(cfg["vga_gain"])),
        "-a", str(int(cfg["amp"])),
        "-1",
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except Exception as exc:
        log.error(f"Failed to start hackrf_sweep: {exc}")
        return None

    total_hz   = (cfg["freq_max"] - cfg["freq_min"]) * 1e6
    freq_start = cfg["freq_min"] * 1e6
    spectrum   = []          # accumulate all (freq_hz, dbm) points
    peak_dbm   = -200.0
    peak_freq  = 0.0
    cur_dbm    = -110.0
    cur_freq   = float(cfg["freq_min"]) * 1e6
    last_send  = time.monotonic()

    try:
        async for raw in proc.stdout:
            line  = raw.decode(errors="ignore").strip()
            parts = line.split(", ")
            if len(parts) < 7:
                continue
            try:
                hz_low  = float(parts[2])
                hz_high = float(parts[3])
                bin_w   = float(parts[4])
                vals    = [float(x) for x in parts[6:] if x]
                for i, dbm in enumerate(vals):
                    freq = hz_low + bin_w * (i + 0.5)
                    if freq > hz_high:
                        break
                    spectrum.append((freq, dbm))
                    if dbm > peak_dbm:
                        peak_dbm  = dbm
                        peak_freq = freq
                    if dbm > cur_dbm:
                        cur_dbm  = dbm
                        cur_freq = freq
                progress = min(1.0, (hz_high - freq_start) / total_hz)
            except (ValueError, IndexError):
                continue

            now = time.monotonic()
            if now - last_send >= 0.05:
                await on_progress(
                    progress,
                    max(-110.0, min(-20.0, cur_dbm)),
                    cur_freq,
                    max(-110.0, min(-20.0, peak_dbm)),
                    peak_freq,
                )
                cur_dbm   = -110.0
                last_send = now

        await proc.wait()
    except Exception as exc:
        log.error(f"sweep streaming error: {exc}")
        proc.kill()
        return None

    if peak_freq == 0.0:
        return None

    peaks = find_peaks(spectrum, n=cfg.get("max_peaks", 5),
                       min_sep_hz=cfg.get("peak_sep_hz", 20e6),
                       threshold=cfg.get("threshold", -80.0))
    return (max(-110.0, min(-20.0, peak_dbm)), peak_freq, peaks)


def find_peaks(spectrum: list, n: int = 5,
               min_sep_hz: float = 20e6, threshold: float = -80.0) -> list:
    """
    Find top N local peaks in spectrum above threshold,
    separated by at least min_sep_hz.
    Returns list of {"freq_mhz": f, "dbm": d} sorted by dBm desc.
    """
    # Filter above threshold first
    candidates = [(f, d) for f, d in spectrum if d >= threshold]
    # Sort by dBm descending, then pick separated peaks
    candidates.sort(key=lambda x: x[1], reverse=True)
    peaks = []
    for freq, dbm in candidates:
        if all(abs(freq - p[0]) >= min_sep_hz for p in peaks):
            peaks.append((freq, dbm))
        if len(peaks) >= n:
            break
    return [{"freq_mhz": round(f / 1e6, 2), "dbm": round(d, 1)} for f, d in peaks]


# ─── Scan engine ────────────────────────────────────────────────────────────
class ScanEngine:
    def __init__(self, cfg: dict, has_hw: bool):
        self.cfg     = cfg
        self.has_hw  = has_hw
        self.clients : set = set()
        self.paused  = False
        self.step    = 0
        self.count   = 0

    async def broadcast(self, msg: str):
        dead = set()
        for ws in self.clients:
            try:
                await ws.send(msg)
            except Exception:
                dead.add(ws)
        self.clients -= dead

    async def run(self):
        directions = int(self.cfg["directions"])

        while True:
            if self.paused or not self.clients:
                await asyncio.sleep(0.05)
                continue

            dir_idx = self.step
            bearing = dir_idx * (360 / directions)
            t0      = time.monotonic()

            # ── Signal scan_start so frontend can show empty beam outline
            await self.broadcast(json.dumps({
                "type"    : "scan_start",
                "dirIdx"  : dir_idx,
                "bearing" : bearing,
                "freq_min": self.cfg["freq_min"],
                "freq_max": self.cfg["freq_max"],
            }))

            if self.has_hw:
                # ── Streaming sweep with progress callbacks
                async def on_progress(progress, cur_dbm, cur_freq, peak_dbm, peak_freq):
                    await self.broadcast(json.dumps({
                        "type"      : "scan_progress",
                        "dirIdx"    : dir_idx,
                        "progress"  : round(progress, 3),
                        "cur_dbm"   : round(cur_dbm,  2),
                        "cur_freq"  : round(cur_freq / 1e6, 3),
                        "peak_dbm"  : round(peak_dbm, 2),
                        "peak_freq" : round(peak_freq / 1e6, 3),
                        "hit"       : cur_dbm >= self.cfg["threshold"],
                    }))

                result = await run_sweep_streaming(self.cfg, on_progress)
                sim = False
            else:
                result = None
                sim = True

            peaks = []
            if result:
                dbm, freq, peaks = result
            else:
                dbm, freq = sim_scan(bearing)
                sim = True
                # Sim: generate a few fake peaks
                if dbm >= self.cfg["threshold"]:
                    peaks = [{"freq_mhz": round(freq / 1e6, 2), "dbm": round(dbm, 1)}]

            # ── Final result for this direction
            await self.broadcast(json.dumps({
                "type"    : "scan_step",
                "dirIdx"  : dir_idx,
                "bearing" : bearing,
                "dbm"     : round(dbm, 2),
                "freq_mhz": round(freq / 1e6, 3),
                "peaks"   : peaks,
                "hit"     : len(peaks) > 0,
                "sim"     : sim,
                "ts"      : time.time(),
            }))

            self.step = (self.step + 1) % directions
            if self.step == 0:
                self.count += 1

            elapsed = time.monotonic() - t0
            budget  = self.cfg["scan_speed"] / directions
            await asyncio.sleep(max(0, budget - elapsed))

    async def handle_client_msg(self, data: str, websocket=None):
        try:
            msg = json.loads(data)
        except json.JSONDecodeError:
            return None

        t = msg.get("type")
        if t == "pause":
            self.paused = msg.get("value", True)
        elif t == "config":
            cfg = msg.get("data", {})
            if "scan_speed"  in cfg: self.cfg["scan_speed"]  = float(cfg["scan_speed"])
            if "freq_min"    in cfg: self.cfg["freq_min"]    = int(cfg["freq_min"])
            if "freq_max"    in cfg: self.cfg["freq_max"]    = int(cfg["freq_max"])
            if "lna_gain"    in cfg: self.cfg["lna_gain"]    = int(cfg["lna_gain"])
            if "vga_gain"    in cfg: self.cfg["vga_gain"]    = int(cfg["vga_gain"])
            if "threshold"   in cfg: self.cfg["threshold"]   = float(cfg["threshold"])
            log.info(f"Config updated: {cfg}")
        elif t == "list_devices":
            devices = list_hackrf_devices()
            return json.dumps({"type": "devices", "devices": devices})
        elif t == "reconnect":
            log.info("Reconnect request — scanning for HackRF...")
            devices = list_hackrf_devices()
            self.has_hw = check_hackrf()
            label = devices[0].get("board", "HackRF") + " #" + devices[0].get("index","0") if devices else ""
            log.info(f"Reconnect: has_hw={self.has_hw} devices={devices}")
            return json.dumps({
                "type"    : "device_status",
                "connected": self.has_hw,
                "devices" : devices,
                "label"   : label,
            })
        elif t == "status":
            return json.dumps({
                "type"     : "status",
                "sim"      : not self.has_hw,
                "paused"   : self.paused,
                "freq_min" : self.cfg["freq_min"],
                "freq_max" : self.cfg["freq_max"],
                "scan_speed": self.cfg["scan_speed"],
                "scans"    : self.count,
            })
        return None


# ─── WebSocket handler ───────────────────────────────────────────────────────
def make_handler(engine: ScanEngine):
    async def handler(websocket):
        engine.clients.add(websocket)
        log.info(f"Client connected ({len(engine.clients)} total)")

        await websocket.send(json.dumps({
            "type"      : "hello",
            "sim"       : not engine.has_hw,
            "freq_min"  : engine.cfg["freq_min"],
            "freq_max"  : engine.cfg["freq_max"],
            "lna_gain"  : engine.cfg["lna_gain"],
            "vga_gain"  : engine.cfg["vga_gain"],
            "threshold" : engine.cfg["threshold"],
            "scan_speed": engine.cfg["scan_speed"],
            "directions": engine.cfg["directions"],
        }))

        try:
            async for raw in websocket:
                reply = await engine.handle_client_msg(raw)
                if reply:
                    await websocket.send(reply)
        except Exception:
            pass
        finally:
            engine.clients.discard(websocket)
            log.info(f"Client disconnected ({len(engine.clients)} total)")

    return handler


# ─── Entry point ─────────────────────────────────────────────────────────────
async def main(args):
    cfg = dict(DEFAULTS)
    if args.freq_min  is not None: cfg["freq_min"]  = args.freq_min
    if args.freq_max  is not None: cfg["freq_max"]  = args.freq_max
    if args.lna       is not None: cfg["lna_gain"]  = args.lna
    if args.vga       is not None: cfg["vga_gain"]  = args.vga
    if args.speed     is not None: cfg["scan_speed"]= args.speed
    if args.port      is not None: cfg["ws_port"]   = args.port
    if args.amp:                   cfg["amp"]        = 1

    log.info("Checking HackRF...")
    has_hw = check_hackrf()
    if has_hw:
        log.info("HackRF ready ✓")
    else:
        log.warning("HackRF not found → simulation mode")

    engine = ScanEngine(cfg, has_hw)

    import websockets
    server = await websockets.serve(make_handler(engine), "0.0.0.0", cfg["ws_port"])

    log.info(f"WebSocket: ws://localhost:{cfg['ws_port']}")
    log.info(f"Mode: {'REAL hackrf_sweep' if has_hw else 'SIMULATION'}")
    log.info(f"Sweep: {cfg['freq_min']}–{cfg['freq_max']} MHz | Speed: {cfg['scan_speed']}s/rot")

    await asyncio.gather(engine.run(), server.wait_closed())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SDR 360° hackrf_sweep WebSocket server")
    parser.add_argument("--freq-min", type=int,   help="Sweep start MHz (default 100)")
    parser.add_argument("--freq-max", type=int,   help="Sweep end MHz (default 6000)")
    parser.add_argument("--lna",      type=int,   help="LNA gain 0-40 dB (default 32)")
    parser.add_argument("--vga",      type=int,   help="VGA gain 0-62 dB (default 40)")
    parser.add_argument("--amp",      action="store_true", help="Enable RF amp")
    parser.add_argument("--speed",    type=float, help="Seconds per full rotation (default 1.0)")
    parser.add_argument("--port",     type=int,   help="WebSocket port (default 8765)")
    args = parser.parse_args()
    asyncio.run(main(args))
